"""Post short alert messages to a webhook such as ntfy."""

from __future__ import annotations

import logging
import urllib.request
from collections.abc import Callable

log = logging.getLogger(__name__)


def make_notifier(get_url: Callable[[], str | None]) -> Callable[[str, str], None]:
    """Return notify(title, message). Failures are logged and never raised."""

    def notify(title: str, message: str) -> None:
        url = get_url()
        if not url:
            return
        req = urllib.request.Request(
            url,
            data=message.encode(),
            headers={"Title": title},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10).close()
        except Exception as exc:
            log.warning("scope alert failed: %s", exc)

    return notify
