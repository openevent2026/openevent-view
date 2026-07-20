from __future__ import annotations

import json
import logging
import socket
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlparse

import grpc

from .config import ViewConfig
from .history import HistoryService, RequestError, UpstreamProtocolError, parse_history_query


LOGGER = logging.getLogger(__name__)


class JsonResponseError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class ViewHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        config: ViewConfig,
        history_service: HistoryService,
    ):
        super().__init__(server_address, handler_class)
        self.config = config
        self.history_service = history_service

    def get_request(self):
        request, client_address = super().get_request()
        request.settimeout(self.config.server.request_timeout_seconds)
        return request, client_address


def make_handler() -> type[BaseHTTPRequestHandler]:
    class ViewRequestHandler(BaseHTTPRequestHandler):
        server: ViewHTTPServer

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/":
                    self._send_html(_load_static_text("index.html"))
                elif parsed.path == "/static/app.css":
                    self._send_static(_load_static_bytes("app.css"), "text/css; charset=utf-8")
                elif parsed.path == "/static/app.js":
                    self._send_static(
                        _load_static_bytes("app.js"),
                        "application/javascript; charset=utf-8",
                    )
                elif parsed.path == "/healthz":
                    self._send_json({"ok": True})
                elif parsed.path == "/v1/messages":
                    data = _query_to_dict(parsed.query)
                    data.update(_headers_to_auth(self.headers))
                    self._handle_messages(data)
                else:
                    raise JsonResponseError(404, "NOT_FOUND", "not found")
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path != "/v1/messages":
                    raise JsonResponseError(404, "NOT_FOUND", "not found")
                data = self._read_json_body()
                data = {**data, **_headers_to_auth(self.headers)}
                self._handle_messages(data)
            except Exception as exc:
                self._handle_error(exc)

        def _handle_messages(self, data: dict[str, Any]) -> None:
            query = parse_history_query(data, self.server.config.history)
            result = self.server.history_service.query(query)
            self._send_json(result)

        def _read_json_body(self) -> dict[str, Any]:
            content_type = self.headers.get("Content-Type", "")
            if "application/json" not in content_type:
                raise JsonResponseError(415, "UNSUPPORTED_MEDIA_TYPE", "Content-Type must be application/json")
            content_length = self.headers.get("Content-Length")
            if content_length is None:
                return {}
            try:
                length = int(content_length)
            except ValueError as exc:
                raise JsonResponseError(400, "INVALID_ARGUMENT", "invalid Content-Length") from exc
            if length < 0:
                raise JsonResponseError(400, "INVALID_ARGUMENT", "Content-Length must not be negative")
            if length > self.server.config.server.max_request_body_bytes:
                raise JsonResponseError(413, "REQUEST_TOO_LARGE", "request body is too large")
            raw = self.rfile.read(length)
            if not raw:
                return {}
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise JsonResponseError(400, "INVALID_ARGUMENT", "request body must be valid JSON") from exc
            if not isinstance(body, dict):
                raise JsonResponseError(400, "INVALID_ARGUMENT", "request body must be a JSON object")
            return body

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, JsonResponseError):
                self._send_error_json(exc.status, exc.code, exc.message)
                return
            if isinstance(exc, (socket.timeout, TimeoutError)):
                self._send_error_json(408, "REQUEST_TIMEOUT", "request timed out")
                return
            if isinstance(exc, RequestError):
                self._send_error_json(400, exc.code, str(exc))
                return
            if isinstance(exc, UpstreamProtocolError):
                self._send_error_json(502, exc.code, str(exc))
                return
            if isinstance(exc, grpc.RpcError):
                status, code = _grpc_to_http(exc)
                message = exc.details() or code
                self._send_error_json(status, code, message)
                return
            LOGGER.exception("request failed")
            self._send_error_json(500, "INTERNAL", "internal server error")

        def _send_html(self, body: str) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            encoded = body.encode("utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_static(self, body: bytes, content_type: str) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, body: dict[str, Any], status: int = 200) -> None:
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_error_json(self, status: int, code: str, message: str) -> None:
            self._send_json({"error": {"code": code, "message": message}}, status=status)

        def log_message(self, format: str, *args: Any) -> None:
            LOGGER.info("%s %s", self.command, urlparse(self.path).path)

    return ViewRequestHandler


def create_server(config: ViewConfig, history_service: HistoryService) -> ViewHTTPServer:
    return ViewHTTPServer(
        (config.server.host, config.server.port),
        make_handler(),
        config,
        history_service,
    )


def _query_to_dict(query: str) -> dict[str, Any]:
    parsed = parse_qs(query, keep_blank_values=True)
    result: dict[str, Any] = {}
    for key, values in parsed.items():
        if values:
            result[key] = values[-1]
    return result


def _headers_to_auth(headers: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    principal = headers.get("X-OpenEvent-Principal")
    token = headers.get("X-OpenEvent-Token")
    authorization = headers.get("Authorization")
    if principal:
        result["principal"] = principal
    if token:
        result["token"] = token
    elif authorization and authorization.startswith("Bearer "):
        result["token"] = authorization[len("Bearer ") :].strip()
    return result


def _grpc_to_http(exc: grpc.RpcError) -> tuple[int, str]:
    code = exc.code()
    mapping = {
        grpc.StatusCode.UNAUTHENTICATED: (401, "UNAUTHENTICATED"),
        grpc.StatusCode.PERMISSION_DENIED: (403, "PERMISSION_DENIED"),
        grpc.StatusCode.NOT_FOUND: (404, "NOT_FOUND"),
        grpc.StatusCode.UNAVAILABLE: (503, "UNAVAILABLE"),
        grpc.StatusCode.DEADLINE_EXCEEDED: (504, "DEADLINE_EXCEEDED"),
        grpc.StatusCode.INVALID_ARGUMENT: (400, "INVALID_ARGUMENT"),
        grpc.StatusCode.RESOURCE_EXHAUSTED: (400, "RESOURCE_EXHAUSTED"),
    }
    return mapping.get(code, (502, code.name if code is not None else "BAD_GATEWAY"))


def _load_static_text(name: str) -> str:
    return resources.files("openevent.view.static").joinpath(name).read_text(encoding="utf-8")


def _load_static_bytes(name: str) -> bytes:
    return resources.files("openevent.view.static").joinpath(name).read_bytes()
