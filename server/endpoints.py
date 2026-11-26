"""Decorators that publish a function as an HTTP route and as an MCP tool.

``@http_endpoint`` registers the route (and its OpenAPI entry) from pydantic
models; ``@mcp_tool`` registers the same function as an MCP tool whose input
schema comes from the function's payload model. Both return the function
unchanged. ``mount_endpoints(app)`` installs everything on a FastAPI app.
"""

from __future__ import annotations

import asyncio
import hmac
import inspect
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, get_type_hints

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger("astrolab.endpoints")

TOKEN_ENV = "ASTROLAB_API_TOKEN"
MCP_PROTOCOL_VERSION = "2025-06-18"

HttpMethod = str


class AuthMode(StrEnum):
    NONE = "none"
    TOKEN = "token"


@dataclass(frozen=True)
class StatusHint:
    status: int
    code: str


class ErrorBody(BaseModel):
    detail: str
    code: str


class ToolAnnotations(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    title: str | None = None
    read_only_hint: bool | None = Field(None, serialization_alias="readOnlyHint")
    destructive_hint: bool | None = Field(None, serialization_alias="destructiveHint")
    idempotent_hint: bool | None = Field(None, serialization_alias="idempotentHint")
    open_world_hint: bool | None = Field(None, serialization_alias="openWorldHint")


@dataclass(frozen=True)
class HttpSpec:
    func: Callable[..., Any]
    path: str
    operation_id: str
    method: HttpMethod
    payload_model: type[BaseModel] | None
    response_model: type[BaseModel] | None
    description: str
    tags: list[str]
    error_map: dict[type[Exception], StatusHint]
    authentication: AuthMode
    status_code: int


@dataclass(frozen=True)
class ToolSpec:
    func: Callable[..., Any]
    name: str
    title: str
    description: str
    annotations: ToolAnnotations | None
    error_map: dict[type[Exception], str | Callable[[Exception], str]]
    authentication: AuthMode
    payload_model: type[BaseModel] | None = None
    response_model: type[BaseModel] | None = None


_HTTP: list[HttpSpec] = []
_TOOLS: dict[str, ToolSpec] = {}


class _EndpointError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _models_from_signature(func: Callable[..., Any]) -> tuple[type[BaseModel] | None, type | None]:
    hints = get_type_hints(func)
    ret = hints.pop("return", None)
    params = [p for p in inspect.signature(func).parameters if p in hints]
    if len(params) > 1:
        raise TypeError(f"{func.__name__}: handlers take at most one payload model")
    payload = hints[params[0]] if params else None
    if payload is not None and not (isinstance(payload, type) and issubclass(payload, BaseModel)):
        raise TypeError(f"{func.__name__}: payload must be a pydantic model")
    return payload, ret


def http_endpoint(
    *,
    path: str,
    operation_id: str,
    method: HttpMethod,
    description: str,
    tags: list[str],
    payload_model: type[BaseModel] | None = None,
    response_model: type[BaseModel] | None = None,
    error_map: dict[type[Exception], StatusHint] | None = None,
    authentication: AuthMode = AuthMode.NONE,
    status_code: int = 200,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def register(func: Callable[..., Any]) -> Callable[..., Any]:
        if any(s.operation_id == operation_id for s in _HTTP):
            raise ValueError(f"duplicate operation_id {operation_id}")
        _HTTP.append(
            HttpSpec(
                func=func,
                path=path,
                operation_id=operation_id,
                method=method.upper(),
                payload_model=payload_model,
                response_model=response_model,
                description=description,
                tags=tags,
                error_map=error_map or {},
                authentication=authentication,
                status_code=status_code,
            )
        )
        return func

    return register


def mcp_tool(
    *,
    name: str,
    title: str,
    description: str,
    annotations: ToolAnnotations | None = None,
    error_map: dict[type[Exception], str | Callable[[Exception], str]] | None = None,
    authentication: AuthMode = AuthMode.NONE,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def register(func: Callable[..., Any]) -> Callable[..., Any]:
        if name in _TOOLS:
            raise ValueError(f"duplicate tool {name}")
        payload, ret = _models_from_signature(func)
        _TOOLS[name] = ToolSpec(
            func=func,
            name=name,
            title=title,
            description=description,
            annotations=annotations,
            error_map=error_map or {},
            authentication=authentication,
            payload_model=payload,
            response_model=ret if isinstance(ret, type) and issubclass(ret, BaseModel) else None,
        )
        return func

    return register


def _lookup(mapping: dict[type[Exception], Any], exc: Exception) -> Any | None:
    for cls, value in mapping.items():
        if isinstance(exc, cls):
            return value
    return None


_bearer = HTTPBearer(auto_error=False)


def _check_token(supplied: str | None) -> None:
    expected = os.environ.get(TOKEN_ENV)
    if not expected:
        raise _EndpointError(503, "auth_not_configured", f"{TOKEN_ENV} is not set")
    if supplied is None or not hmac.compare_digest(supplied, expected):
        raise _EndpointError(401, "unauthorized", "missing or invalid bearer token")


def _require_token(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    _check_token(creds.credentials if creds else None)


def _build_route(spec: HttpSpec) -> Callable[..., Any]:
    is_async = inspect.iscoroutinefunction(spec.func)

    def translate(exc: Exception) -> None:
        hint = _lookup(spec.error_map, exc)
        if hint is None:
            raise exc
        raise _EndpointError(hint.status, hint.code, str(exc) or hint.code) from exc

    if is_async:

        async def route(payload: Any = None) -> Any:
            try:
                return await (spec.func(payload) if spec.payload_model else spec.func())
            except Exception as exc:
                translate(exc)

    else:

        def route(payload: Any = None) -> Any:
            try:
                return spec.func(payload) if spec.payload_model else spec.func()
            except Exception as exc:
                translate(exc)

    params: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    if spec.payload_model is not None:
        ann: Any = (
            Annotated[spec.payload_model, Query()] if spec.method == "GET" else spec.payload_model
        )
        params.append(
            inspect.Parameter("payload", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=ann)
        )
        annotations["payload"] = ann
    route.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
    route.__annotations__ = annotations
    route.__name__ = spec.operation_id
    return route


def _error_responses(spec: HttpSpec) -> dict[int | str, dict[str, Any]]:
    by_status: dict[int, list[str]] = {}
    for hint in spec.error_map.values():
        by_status.setdefault(hint.status, []).append(hint.code)
    if spec.authentication is AuthMode.TOKEN:
        by_status.setdefault(401, []).append("unauthorized")
    if spec.payload_model is not None:
        by_status.setdefault(422, []).append("invalid_request")
    return {
        status: {"model": ErrorBody, "description": ", ".join(sorted(set(codes)))}
        for status, codes in by_status.items()
    }


def _mount_http(app: FastAPI) -> None:
    for spec in _HTTP:
        app.add_api_route(
            spec.path,
            _build_route(spec),
            methods=[spec.method],
            operation_id=spec.operation_id,
            response_model=spec.response_model,
            status_code=spec.status_code,
            description=spec.description,
            tags=list(spec.tags),
            responses=_error_responses(spec),  # type: ignore[arg-type]
            dependencies=[Depends(_require_token)] if spec.authentication is AuthMode.TOKEN else [],
        )


def tool_manifest() -> list[dict[str, Any]]:
    tools = []
    for spec in sorted(_TOOLS.values(), key=lambda s: s.name):
        schema = (
            spec.payload_model.model_json_schema()
            if spec.payload_model
            else {"type": "object", "properties": {}}
        )
        tool: dict[str, Any] = {
            "name": spec.name,
            "title": spec.title,
            "description": spec.description,
            "inputSchema": schema,
        }
        if spec.response_model is not None:
            tool["outputSchema"] = spec.response_model.model_json_schema()
        if spec.annotations is not None:
            tool["annotations"] = spec.annotations.model_dump(by_alias=True, exclude_none=True)
        tools.append(tool)
    return tools


def _rpc_error(rpc_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _tool_result(text: str, *, is_error: bool, structured: Any = None) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if structured is not None:
        result["structuredContent"] = structured
    return result


async def _call_tool(
    spec: ToolSpec, arguments: dict[str, Any], bearer: str | None
) -> dict[str, Any]:
    try:
        if spec.authentication is AuthMode.TOKEN:
            _check_token(bearer)
        payload = spec.payload_model.model_validate(arguments) if spec.payload_model else None
        args = (payload,) if spec.payload_model else ()
        if inspect.iscoroutinefunction(spec.func):
            value = await spec.func(*args)
        else:
            value = await asyncio.to_thread(spec.func, *args)
    except _EndpointError as exc:
        return _tool_result(str(exc), is_error=True)
    except ValidationError as exc:
        return _tool_result(f"Invalid arguments: {exc.errors(include_url=False)}", is_error=True)
    except Exception as exc:
        message = _lookup(spec.error_map, exc)
        if message is None:
            log.exception("mcp tool %s failed", spec.name)
            return _tool_result("Internal error.", is_error=True)
        return _tool_result(message(exc) if callable(message) else message, is_error=True)
    data = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return _tool_result(
        json.dumps(data), is_error=False, structured=data if isinstance(data, dict) else None
    )


async def _mcp_endpoint(request: Request) -> Response:
    try:
        msg = await request.json()
    except ValueError:
        return JSONResponse(_rpc_error(None, -32700, "parse error"), status_code=400)
    if not isinstance(msg, dict):
        return JSONResponse(_rpc_error(None, -32600, "batches are not supported"), status_code=400)
    rpc_id, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if rpc_id is None:
        return Response(status_code=202)
    if method == "initialize":
        result: dict[str, Any] = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "astrolab", "version": request.app.version},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tool_manifest()}
    elif method == "tools/call":
        spec = _TOOLS.get(params.get("name", ""))
        if spec is None:
            return JSONResponse(_rpc_error(rpc_id, -32602, f"unknown tool {params.get('name')}"))
        auth = request.headers.get("authorization", "")
        bearer = auth[7:] if auth.lower().startswith("bearer ") else None
        result = await _call_tool(spec, params.get("arguments") or {}, bearer)
    else:
        return JSONResponse(_rpc_error(rpc_id, -32601, f"unknown method {method}"))
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})


def mount_endpoints(app: FastAPI) -> None:
    @app.exception_handler(_EndpointError)
    async def _handle(_: Request, exc: _EndpointError) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _handle_invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        detail = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'][1:]) or 'request'}: {e['msg']}"
            for e in exc.errors()
        )
        return JSONResponse({"detail": detail, "code": "invalid_request"}, status_code=422)

    _mount_http(app)
    app.add_api_route("/mcp", _mcp_endpoint, methods=["POST"], include_in_schema=False)
