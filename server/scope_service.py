"""Client for any service that follows the scope service spec (v0.1).

Paths come from the service's own OpenAPI file, looked up by operationId, so the
client holds no scope-specific routes. Features come from the capabilities list.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

SUPPORTED_MAJOR = "0"
TIMEOUT_S = 20

Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


class ScopeServiceError(Exception):
    def __init__(
        self, message: str, *, code: str = "service_error", status: int | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _http_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ScopeServiceError(f"cannot reach service: {exc}", code="unreachable") from exc


class ScopeServiceClient:
    def __init__(
        self, base_url: str, token: str | None = None, transport: Transport | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._transport = transport or _http_transport
        self._routes: dict[str, tuple[str, str]] | None = None
        self._info: dict[str, Any] | None = None

    def _request(self, method: str, path: str, query: dict | None = None, body: Any = None) -> Any:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(
                {k: str(v).lower() if isinstance(v, bool) else v for k, v in query.items()}
            )
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        status, raw = self._transport(method, url, headers, data)
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        if status >= 400:
            detail = parsed.get("detail") if isinstance(parsed, dict) else None
            code = parsed.get("code") if isinstance(parsed, dict) else None
            raise ScopeServiceError(
                detail if isinstance(detail, str) else f"service returned {status}",
                code=code or "service_error",
                status=status,
            )
        return parsed

    def _route(self, operation_id: str) -> tuple[str, str]:
        if self._routes is None:
            spec = self._request("GET", "/openapi.json")
            self._routes = {
                op["operationId"]: (method.upper(), path)
                for path, methods in spec.get("paths", {}).items()
                for method, op in methods.items()
                if isinstance(op, dict) and "operationId" in op
            }
        if operation_id not in self._routes:
            raise ScopeServiceError(f"service has no operation {operation_id}", code="unsupported")
        return self._routes[operation_id]

    def info(self) -> dict[str, Any]:
        """Return the capabilities document. Refuse a service with another major version."""
        if self._info is None:
            info = self._request("GET", "/api/capabilities")
            version = str(info.get("spec_version", ""))
            if version.split(".")[0] != SUPPORTED_MAJOR:
                raise ScopeServiceError(
                    f"service spec {version or 'unknown'} is not supported "
                    f"(need {SUPPORTED_MAJOR}.x)",
                    code="unsupported_version",
                )
            self._info = info
        return self._info

    def has(self, capability: str) -> bool:
        return capability in self.info().get("capabilities", [])

    def call(self, operation_id: str, payload: dict | None = None) -> Any:
        method, path = self._route(operation_id)
        if method in ("GET", "DELETE"):
            return self._request(method, path, query=payload)
        return self._request(method, path, body=payload)
