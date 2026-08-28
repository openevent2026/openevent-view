from __future__ import annotations

import logging
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlparse

import grpc
import orjson

from .config import ViewConfig
from .history import (
    HistoryService,
    MessageNotFound,
    RequestError,
    UpstreamProtocolError,
    parse_history_query,
    parse_payload_query,
    parse_seq,
)


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
                    self._send_static(
                        _load_static_bytes("app.css"), "text/css; charset=utf-8"
                    )
                elif parsed.path == "/static/app.js":
                    self._send_static(
                        _load_static_bytes("app.js"),
                        "application/javascript; charset=utf-8",
                    )
                elif parsed.path == "/message":
                    values = parse_qs(parsed.query, keep_blank_values=True)
                    if set(values) != {"seq"} or len(values["seq"]) != 1:
                        raise RequestError("seq must be a canonical uint64 decimal string")
                    parse_seq(values["seq"][0])
                    self._send_html(_load_static_text("message.html"))
                else:
                    raise JsonResponseError(404, "NOT_FOUND", "not found")
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/v1/messages":
                    data = self._read_json_body()
                    query = parse_history_query(data, self.server.config.history)
                    self._send_json(self.server.history_service.query(query))
                    return

                prefix = "/v1/messages/"
                suffix = "/payload"
                if parsed.path.startswith(prefix) and parsed.path.endswith(suffix):
                    seq = parse_seq(parsed.path[len(prefix) : -len(suffix)])
                    query = parse_payload_query(self._read_json_body())
                    self._send_json(self.server.history_service.get_payload(seq, query))
                    return

                raise JsonResponseError(404, "NOT_FOUND", "not found")
            except Exception as exc:
                self._handle_error(exc)

        def _read_json_body(self) -> dict[str, Any]:
            content_length = self.headers.get("Content-Length")
            if content_length is None:
                raise JsonResponseError(400, "INVALID_ARGUMENT", "request body is required")
            try:
                length = int(content_length)
            except ValueError as exc:
                raise JsonResponseError(400, "INVALID_ARGUMENT", "invalid Content-Length") from exc
            if length < 0:
                raise JsonResponseError(400, "INVALID_ARGUMENT", "Content-Length must not be negative")
            if length > self.server.config.server.max_request_body_bytes:
                raise JsonResponseError(413, "REQUEST_TOO_LARGE", "request body is too large")
            try:
                body = orjson.loads(self.rfile.read(length))
            except orjson.JSONDecodeError as exc:
                raise JsonResponseError(400, "INVALID_ARGUMENT", "request body must be valid JSON") from exc
            if not isinstance(body, dict):
                raise JsonResponseError(400, "INVALID_ARGUMENT", "request body must be a JSON object")
            return body

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, JsonResponseError):
                self._send_error_json(exc.status, exc.code, exc.message)
            elif isinstance(exc, (socket.timeout, TimeoutError)):
                self._send_error_json(408, "REQUEST_TIMEOUT", "request timed out")
            elif isinstance(exc, RequestError):
                self._send_error_json(400, exc.code, str(exc))
            elif isinstance(exc, MessageNotFound):
                self._send_error_json(404, exc.code, str(exc))
            elif isinstance(exc, UpstreamProtocolError):
                self._send_error_json(502, exc.code, str(exc))
            elif isinstance(exc, grpc.RpcError):
                status, code = _grpc_to_http(exc)
                self._send_error_json(status, code, exc.details() or code)
            else:
                LOGGER.exception("request failed")
                self._send_error_json(500, "INTERNAL", "internal server error")

        def _send_html(self, body: str) -> None:
            self._send_bytes(body.encode("utf-8"), "text/html; charset=utf-8")

        def _send_static(self, body: bytes, content_type: str) -> None:
            self._send_bytes(body, content_type)

        def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, body: dict[str, Any], status: int = 200) -> None:
            self._send_bytes(
                orjson.dumps(body), "application/json; charset=utf-8", status
            )

        def _send_error_json(self, status: int, code: str, message: str) -> None:
            self._send_json({"error": {"code": code, "message": message}}, status)

        def log_message(self, format: str, *args: Any) -> None:
            LOGGER.info("%s %s", self.command, urlparse(self.path).path)

    return ViewRequestHandler


def create_server(config: ViewConfig, history_service: HistoryService) -> ViewHTTPServer:
    return ViewHTTPServer(
        (config.server.host, config.server.port), make_handler(), config, history_service
    )


def _grpc_to_http(exc: grpc.RpcError) -> tuple[int, str]:
    code = exc.code()
    mapping = {
        grpc.StatusCode.UNAUTHENTICATED: (401, "UNAUTHENTICATED"),
        grpc.StatusCode.PERMISSION_DENIED: (403, "PERMISSION_DENIED"),
        grpc.StatusCode.NOT_FOUND: (404, "NOT_FOUND"),
        grpc.StatusCode.UNAVAILABLE: (503, "UNAVAILABLE"),
        grpc.StatusCode.DEADLINE_EXCEEDED: (504, "DEADLINE_EXCEEDED"),
    }
    return mapping.get(code, (502, code.name if code is not None else "BAD_GATEWAY"))


def _load_static_text(name: str) -> str:
    return resources.files("openevent.view.static").joinpath(name).read_text(
        encoding="utf-8"
    )


def _load_static_bytes(name: str) -> bytes:
    return resources.files("openevent.view.static").joinpath(name).read_bytes()
