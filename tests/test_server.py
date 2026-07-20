import json
import socket
import threading
import unittest
from dataclasses import dataclass
from dataclasses import replace
from http.client import HTTPConnection

import grpc

from openevent.view.config import ViewConfig, parse_config
from openevent.view.history import UpstreamProtocolError
from openevent.view.server import _grpc_to_http, create_server


@dataclass
class FakeHistoryService:
    config: ViewConfig

    def query(self, query):
        return {
            "messages": [
                {
                    "seq": 2,
                    "ts_ms": 20,
                    "channel_id": 100,
                    "principal": query.principal,
                    "recipients": [],
                    "payload": {
                        "encoding": "utf-8",
                        "text": "{\"n\":2}",
                        "json": {"n": 2},
                        "truncated": False,
                        "size_bytes": 7,
                    },
                }
            ],
            "next_cursor": "2",
        }


class ProtocolErrorHistoryService:
    def query(self, query):
        raise UpstreamProtocolError("OpenEvent Fetch next_seq did not advance")


class FakeRpcError(grpc.RpcError):
    def __init__(self, status_code):
        self._status_code = status_code

    def code(self):
        return self._status_code


class ServerTests(unittest.TestCase):
    def setUp(self):
        base_config = parse_config({"server": {"host": "127.0.0.1"}})
        self.config = replace(base_config, server=replace(base_config.server, port=0))
        self.server = create_server(self.config, FakeHistoryService(self.config))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_healthz(self):
        conn = HTTPConnection("127.0.0.1", self.server.server_port)
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(resp.read()), {"ok": True})
        conn.close()

    def test_messages_post(self):
        conn = HTTPConnection("127.0.0.1", self.server.server_port)
        conn.request(
            "POST",
            "/v1/messages",
            body=json.dumps({"principal": 1, "token": "tok"}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.read())
        self.assertEqual(body["messages"][0]["payload"]["json"], {"n": 2})
        self.assertEqual(body["next_cursor"], "2")
        conn.close()

    def test_upstream_protocol_error_maps_to_bad_gateway(self):
        self.server.history_service = ProtocolErrorHistoryService()
        conn = HTTPConnection("127.0.0.1", self.server.server_port)
        conn.request(
            "POST",
            "/v1/messages",
            body=json.dumps({"principal": 1, "token": "tok"}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        self.assertEqual(resp.status, 502)
        body = json.loads(resp.read())
        self.assertEqual(body["error"]["code"], "BAD_GATEWAY")
        conn.close()

    def test_deadline_exceeded_maps_to_gateway_timeout(self):
        self.assertEqual(
            _grpc_to_http(FakeRpcError(grpc.StatusCode.DEADLINE_EXCEEDED)),
            (504, "DEADLINE_EXCEEDED"),
        )

    def test_rejects_oversized_request_body(self):
        conn = HTTPConnection("127.0.0.1", self.server.server_port)
        conn.request(
            "POST",
            "/v1/messages",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(self.config.server.max_request_body_bytes + 1),
            },
        )
        resp = conn.getresponse()
        self.assertEqual(resp.status, 413)
        self.assertEqual(json.loads(resp.read())["error"]["code"], "REQUEST_TOO_LARGE")
        conn.close()

    def test_rejects_negative_content_length(self):
        conn = HTTPConnection("127.0.0.1", self.server.server_port)
        conn.request(
            "POST",
            "/v1/messages",
            headers={"Content-Type": "application/json", "Content-Length": "-1"},
        )
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        self.assertEqual(json.loads(resp.read())["error"]["code"], "INVALID_ARGUMENT")
        conn.close()

    def test_partial_request_body_times_out(self):
        self.server.config = replace(
            self.config,
            server=replace(self.config.server, request_timeout_seconds=0.05),
        )
        client = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=1)
        try:
            client.sendall(
                b"POST /v1/messages HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 10\r\n"
                b"Connection: close\r\n\r\n"
                b"{}"
            )
            response = b""
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response += chunk
        finally:
            client.close()

        self.assertIn(b" 408 ", response)
        self.assertIn(b'"code":"REQUEST_TIMEOUT"', response)


if __name__ == "__main__":
    unittest.main()
