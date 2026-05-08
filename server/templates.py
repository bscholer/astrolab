"""Discover and load canned pipeline templates from disk.

Templates live as YAML files under <repo>/templates/<id>.yaml and parse into
the same Pydantic Template model used by the runtime. Convention: filename
stem == template id. The catalog walks the directory once per process and
caches results; the cache is tiny (a few dozen templates at most) and
templates rarely change at runtime.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from .models import Template

DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"


class TemplateNotFound(LookupError):
    pass


def template_dir() -> Path:
    return DEFAULT_TEMPLATE_DIR


@lru_cache(maxsize=1)
def list_templates() -> list[Template]:
    """Return every YAML template under templates/ as parsed Template objects."""
    out: list[Template] = []
    if not DEFAULT_TEMPLATE_DIR.is_dir():
        return out
    for f in sorted(DEFAULT_TEMPLATE_DIR.glob("*.yaml")):
        with f.open() as fh:
            data = yaml.safe_load(fh)
        try:
            tmpl = Template.model_validate(data)
        except Exception as exc:
            raise RuntimeError(f"failed to parse template {f.name}: {exc}") from exc
        out.append(tmpl)
    return out


def load_template(template_id: str) -> Template:
    for t in list_templates():
        if t.id == template_id:
            return t
    raise TemplateNotFound(template_id)


def reload() -> None:
    """Drop the template cache. Tests use this between fixtures; production
    won't hot-reload templates without a process restart."""
    list_templates.cache_clear()
