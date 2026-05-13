"""Unit tests for ProjectManager behavior that the API-level suite can't
hit deterministically.

The API tests in `test_projects_api.py` exercise the FastAPI app end-to-end
and depend on the worker pool's exact scheduling. The collapse path in
`ProjectManager.patch` (see issue #34) is a transactional decision per
PATCH; we stub the JobManager so the test stays independent of how fast
the downscale node settles.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

import nodes.basic  # noqa: F401  registers `downscale` for params-schema lookup
from server.models import Job, Template
from server.projects import ProjectManager


@dataclass
class _FakeJobRecord:
    id: str
    status: str  # "queued" | "running" | "completed" | "interrupted" | ...


class _FakeJobManager:
    """Minimal stand-in for JobManager.

    Each submit returns a fresh job_id. Jobs default to 'queued' on
    submit; cancel() flips them to 'interrupted' (returns True) only if
    they were still live, matching the real manager's contract. Tests
    flip status to 'completed' manually to simulate jobs that settled
    before the next PATCH.
    """

    def __init__(self) -> None:
        self.records: dict[str, _FakeJobRecord] = {}
        self.submit_calls: list[dict] = []
        self.cancel_calls: list[str] = []

    def submit(self, template: Template, job: Job, *, force: bool = False) -> str:
        jid = str(uuid.uuid4())
        self.records[jid] = _FakeJobRecord(id=jid, status="queued")
        self.submit_calls.append({"job_id": jid, "force": force, "job": job})
        return jid

    def get(self, job_id: str) -> _FakeJobRecord | None:
        return self.records.get(job_id)

    def cancel(self, job_id: str) -> bool:
        self.cancel_calls.append(job_id)
        rec = self.records.get(job_id)
        if rec is None or rec.status not in ("queued", "running"):
            return False
        rec.status = "interrupted"
        return True

    def mark_completed(self, job_id: str) -> None:
        self.records[job_id].status = "completed"


def _make_template() -> Template:
    return Template.model_validate(
        {
            "id": "tpl",
            "version": 1,
            "description": "smoke",
            "nodes": [
                {"id": "ds", "kind": "downscale",
                 "params": {"target_size_px": 64}},
            ],
            "outputs": {"thumb": "ds.image"},
        }
    )


def _make_job() -> Job:
    return Job.model_validate(
        {
            "template_id": "tpl",
            "template_version": 1,
            "inputs": {
                "ds.image": {
                    "node_hash": "ext",
                    "port": "image",
                    "path": "/tmp/in.png",
                    "type": "image/png",
                }
            },
        }
    )


@pytest.fixture
def pm(tmp_path: Path) -> tuple[ProjectManager, _FakeJobManager]:
    fake = _FakeJobManager()
    pm = ProjectManager(fake, db_path=tmp_path / "catalog.sqlite")  # type: ignore[arg-type]
    return pm, fake


def test_project_for_job_finds_owning_project_across_history(pm) -> None:
    """The /api/system dashboard uses this to render the project name on
    each active job. Hit it across the initial render, a settled-then-
    new history entry, and an unknown job id."""
    manager, jobs = pm
    project = manager.create(
        name="M31",
        template=_make_template(),
        base_job=_make_job(),
        source_session_ids=[],
    )
    initial_job_id = project.history[0].job_id

    # Original render's job is reachable.
    found = manager.project_for_job(initial_job_id)
    assert found is not None and found.id == project.id

    # Settle, then patch to grow history. Both history entries' jobs
    # should resolve to the same project.
    jobs.mark_completed(initial_job_id)
    manager.patch(project.id, overrides={"ds": {"target_size_px": 32}})
    new_job_id = project.history[-1].job_id
    assert manager.project_for_job(new_job_id) is not None
    assert manager.project_for_job(new_job_id).id == project.id

    # Unknown ids return None rather than raising.
    assert manager.project_for_job("does-not-exist") is None


def test_rapid_patches_collapse_interrupted_history(pm) -> None:
    """A debounced slider drag fires N PATCHes in rapid succession; each
    cancels the prior in-flight job. Without collapse this would leave
    N-1 'interrupted' tombstones the user can never revert to. The
    collapse rule replaces the prior row in place when its job is (or is
    about to be) interrupted, so history grows by 1, not N.
    """
    manager, jobs = pm
    project = manager.create(
        name="rapid-drag",
        template=_make_template(),
        base_job=_make_job(),
        source_session_ids=[],
    )
    # Initial render settled to completed before the user starts tweaking.
    jobs.mark_completed(project.history[0].job_id)
    assert len(project.history) == 1

    # Five rapid patches; each prior job is still 'queued' (worker hasn't
    # touched it). cancel() flips each to 'interrupted' and returns True,
    # so subsequent patches see a collapse-eligible prior entry.
    sizes = [60, 48, 36, 24, 12]
    for size in sizes:
        manager.patch(
            project.id,
            overrides={"ds": {"target_size_px": size}},
        )

    # Acceptance criterion from issue #34: 1-second slider drag firing
    # 5 PATCHes results in history growing by 1, not 5.
    assert len(project.history) == 2, (
        f"expected collapse to a single drag entry, got "
        f"{[(h.seq, h.label) for h in project.history]}"
    )
    final = project.history[-1]
    assert final.overrides == {"ds": {"target_size_px": 12}}
    # current_seq tracks the replaced row so undo (Cmd-Z to seq 0) is
    # still a single step.
    assert project.current_seq == final.seq

    # Auto-label compares against the entry BEFORE the replaced one
    # (initial render with empty overrides), not the last intermediate.
    assert "12" in final.label
    assert "24" not in final.label and "36" not in final.label

    # Undo/redo across the collapsed entry still works.
    assert manager.revert(project.id, 0).current_overrides() == {}
    assert manager.revert(project.id, final.seq).current_overrides() == {
        "ds": {"target_size_px": 12}
    }


def test_settled_prior_appends_new_entry(pm) -> None:
    """When the prior history entry's job ran to completion (the user
    waited for the render to settle), the next PATCH must append a new
    entry: the audit trail of intentional, settled edits stays intact."""
    manager, jobs = pm
    project = manager.create(
        name="settled",
        template=_make_template(),
        base_job=_make_job(),
        source_session_ids=[],
    )
    jobs.mark_completed(project.history[0].job_id)

    # First settled patch: complete its job before the next PATCH.
    project = manager.patch(
        project.id, overrides={"ds": {"target_size_px": 32}}
    )
    jobs.mark_completed(project.history[-1].job_id)
    assert len(project.history) == 2

    # Second settled patch: prior is 'completed', not interrupted, and
    # cancel() returns False (terminal). collapse=False -> append.
    project = manager.patch(
        project.id, overrides={"ds": {"target_size_px": 16}}
    )
    assert len(project.history) == 3
    assert project.current_seq == 2


def test_collapsed_entry_persists_across_rehydrate(pm, tmp_path: Path) -> None:
    """Collapse UPDATEs the existing project_history row in place rather
    than INSERTing a new one. After a rehydrate the DB-loaded history
    must match the in-memory state, not carry phantom tombstones."""
    manager, jobs = pm
    project = manager.create(
        name="rehydrate-check",
        template=_make_template(),
        base_job=_make_job(),
        source_session_ids=[],
    )
    jobs.mark_completed(project.history[0].job_id)
    for size in [60, 48, 36, 24, 12]:
        manager.patch(
            project.id, overrides={"ds": {"target_size_px": size}}
        )
    pid = project.id
    assert len(project.history) == 2

    # Drop the in-memory map and reload from disk.
    manager.reset_for_tests(db_path=manager._db_path)
    manager.rehydrate()
    reloaded = manager.get(pid)
    assert reloaded is not None
    assert len(reloaded.history) == 2
    assert reloaded.history[-1].overrides == {"ds": {"target_size_px": 12}}
    assert reloaded.current_seq == reloaded.history[-1].seq


def test_collapse_preserves_published_flag(pm) -> None:
    """If the prior entry had `published=True` (unusual, since you don't
    publish a tombstone, but possible via direct API), the collapsed
    replacement carries that flag forward rather than silently demoting
    the entry to unpublished."""
    manager, jobs = pm
    project = manager.create(
        name="published-collapse",
        template=_make_template(),
        base_job=_make_job(),
        source_session_ids=[],
    )
    jobs.mark_completed(project.history[0].job_id)
    project = manager.patch(
        project.id, overrides={"ds": {"target_size_px": 32}}
    )
    # Pin the in-flight entry as published.
    project.history[-1].published = True

    manager.patch(project.id, overrides={"ds": {"target_size_px": 16}})
    assert project.history[-1].published is True


# ---- template upgrade --------------------------------------------------------


def _template_v2_with_extra_node() -> Template:
    """v2: keeps `ds` bit-identical, adds `ds2` downstream. Mirrors the
    auto_bp_shift insertion shape we plan to ship."""
    return Template.model_validate(
        {
            "id": "tpl",
            "version": 2,
            "description": "smoke v2",
            "nodes": [
                {"id": "ds", "kind": "downscale",
                 "params": {"target_size_px": 64}},
                {"id": "ds2", "kind": "downscale",
                 "params": {"target_size_px": 32},
                 "inputs": {"image": "ds.image"}},
            ],
            "outputs": {"thumb": "ds2.image"},
        }
    )


def test_upgrade_template_appends_history_and_bumps_version(pm) -> None:
    """Upgrade swaps the project's frozen template, submits a new job,
    and appends a `template_upgrade` history entry."""
    manager, jobs = pm
    project = manager.create(
        name="upgrade-smoke",
        template=_make_template(),  # v1
        base_job=_make_job(),
        source_session_ids=[],
    )
    jobs.mark_completed(project.history[0].job_id)

    updated, new_job_id, dropped = manager.upgrade_template(
        project.id, new_template=_template_v2_with_extra_node()
    )
    assert updated.template.version == 2
    assert updated.base_job.template_version == 2
    assert dropped == []
    assert len(updated.history) == 2
    final = updated.history[-1]
    assert final.kind == "template_upgrade"
    assert final.snapshot == {
        "from_version": 1,
        "to_version": 2,
        "dropped_overrides": [],
    }
    assert final.job_id == new_job_id
    assert "v1" in final.label and "v2" in final.label


def test_upgrade_template_carries_overrides_forward_when_node_persists(pm) -> None:
    """An override on a node that still exists in the new template must
    survive the upgrade (the user's slider tweaks aren't lost)."""
    manager, jobs = pm
    project = manager.create(
        name="upgrade-overrides",
        template=_make_template(),
        base_job=_make_job(),
        source_session_ids=[],
    )
    jobs.mark_completed(project.history[0].job_id)
    # User tweaks ds.target_size_px down to 8.
    project = manager.patch(project.id, overrides={"ds": {"target_size_px": 8}})
    jobs.mark_completed(project.history[-1].job_id)

    updated, _job_id, dropped = manager.upgrade_template(
        project.id, new_template=_template_v2_with_extra_node()
    )
    assert dropped == []
    assert updated.current_overrides() == {"ds": {"target_size_px": 8}}
    submitted_job = jobs.submit_calls[-1]["job"]
    assert submitted_job.param_overrides == {"ds": {"target_size_px": 8}}
    assert submitted_job.template_version == 2


def test_upgrade_template_drops_overrides_for_removed_nodes(pm) -> None:
    """Overrides on nodes that no longer exist in the new template are
    surfaced as `dropped` rather than silently broken."""
    manager, jobs = pm
    # Build a template that has `ds` AND `extra`, set override on `extra`,
    # then upgrade to a template that drops `extra`.
    tpl_v1 = Template.model_validate(
        {
            "id": "tpl",
            "version": 1,
            "nodes": [
                {"id": "ds", "kind": "downscale",
                 "params": {"target_size_px": 64}},
                {"id": "extra", "kind": "downscale",
                 "params": {"target_size_px": 32},
                 "inputs": {"image": "ds.image"}},
            ],
            "outputs": {"thumb": "extra.image"},
        }
    )
    tpl_v2 = Template.model_validate(
        {
            "id": "tpl",
            "version": 2,
            "nodes": [
                {"id": "ds", "kind": "downscale",
                 "params": {"target_size_px": 64}},
            ],
            "outputs": {"thumb": "ds.image"},
        }
    )
    project = manager.create(
        name="upgrade-drop",
        template=tpl_v1,
        base_job=_make_job(),
        source_session_ids=[],
    )
    jobs.mark_completed(project.history[0].job_id)
    project = manager.patch(
        project.id, overrides={"extra": {"target_size_px": 4}}
    )
    jobs.mark_completed(project.history[-1].job_id)

    updated, _job_id, dropped = manager.upgrade_template(
        project.id, new_template=tpl_v2
    )
    assert updated.current_overrides() == {}
    assert len(dropped) == 1
    assert "extra" in dropped[0]


def test_upgrade_template_rejects_same_or_older_version(pm) -> None:
    manager, jobs = pm
    project = manager.create(
        name="upgrade-reject",
        template=_make_template(),  # v1
        base_job=_make_job(),
        source_session_ids=[],
    )
    jobs.mark_completed(project.history[0].job_id)

    same = _make_template()  # v1
    with pytest.raises(ValueError, match="must move forward"):
        manager.upgrade_template(project.id, new_template=same)


def test_upgrade_template_rejects_id_mismatch(pm) -> None:
    manager, _ = pm
    project = manager.create(
        name="upgrade-id-mismatch",
        template=_make_template(),
        base_job=_make_job(),
        source_session_ids=[],
    )
    other = Template.model_validate(
        {
            "id": "different",
            "version": 2,
            "nodes": [
                {"id": "ds", "kind": "downscale",
                 "params": {"target_size_px": 64}},
            ],
            "outputs": {"thumb": "ds.image"},
        }
    )
    with pytest.raises(ValueError, match="template id mismatch"):
        manager.upgrade_template(project.id, new_template=other)


def test_upgrade_template_persists_across_rehydrate(pm) -> None:
    """The new template_json + template_version must persist; reload from
    DB and confirm the project comes back on v2."""
    manager, jobs = pm
    project = manager.create(
        name="upgrade-persist",
        template=_make_template(),
        base_job=_make_job(),
        source_session_ids=[],
    )
    jobs.mark_completed(project.history[0].job_id)
    manager.upgrade_template(
        project.id, new_template=_template_v2_with_extra_node()
    )
    pid = project.id

    manager.reset_for_tests(db_path=manager._db_path)
    manager.rehydrate()
    reloaded = manager.get(pid)
    assert reloaded is not None
    assert reloaded.template.version == 2
    assert reloaded.base_job.template_version == 2
    assert reloaded.history[-1].kind == "template_upgrade"
    assert reloaded.history[-1].snapshot is not None
    assert reloaded.history[-1].snapshot["from_version"] == 1
    assert reloaded.history[-1].snapshot["to_version"] == 2
