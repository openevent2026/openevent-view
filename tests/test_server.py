import json
import socket
import threading
import types
import unittest
from http.client import HTTPConnection

import grpc

from openevent.view.config import parse_config
from openevent.view.history import HistoryService
from openevent.view.server import _grpc_to_http, create_server


def message(seq, payload=b'{"n":1}'):
    return types.SimpleNamespace(
        seq=seq,
        uuid=1000 + seq,
        ts_ms=1700000000000 + seq,
        channel_id=7,
        principal=100,
        recipients=[100],
        payload=payload,
        object_keys=[types.SimpleNamespace(object_id=900 + seq, object_token="secret")],
    )


class FakeClient:
    def __init__(self):
        self.messages = [message(1)]

    def get_status(self, principal, token):
        return types.SimpleNamespace(min_seq=1, max_seq=1)

    def get_channel(self, principal, token, channel_id):
        return types.SimpleNamespace(
            channel=types.SimpleNamespace(name="events", protocol="chat.v1")
        )

    def fetch(
        self,
        principal,
        token,
        from_seq,
        limit,
        only_my_recipient=False,
        channels=(),
    ):
        messages = [
            item
            for item in self.messages
            if item.seq >= from_seq and (not channels or item.channel_id in channels)
        ][:limit]
        return types.SimpleNamespace(
            messages=messages,
            next_seq=messages[-1].seq + 1 if messages else 2,
            last_seq=1,
        )


class FakeRpcError(grpc.RpcError):
    def __init__(self, status_code):
        self._status_code = status_code

    def code(self):
        return self._status_code


class ServerTests(unittest.TestCase):
    def setUp(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        config = parse_config({"server": {"port": port}})
        self.history = HistoryService(FakeClient(), config.history)
        self.server = create_server(config, self.history)
        self.server.server_activate()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.history.close()
        self.thread.join()

    def request(self, method, path, body=None):
        conn = HTTPConnection("127.0.0.1", self.server.server_address[1])
        payload = None if body is None else json.dumps(body)
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        conn.request(method, path, payload, headers)
        response = conn.getresponse()
        data = response.read()
        result = response.status, dict(response.getheaders()), data
        conn.close()
        return result

    def request_json(self, method, path, body=None):
        status, headers, data = self.request(method, path, body)
        return status, headers, json.loads(data) if data else None

    def test_only_documented_routes(self):
        status, _, _ = self.request("GET", "/v1/messages")
        self.assertEqual(status, 404)
        status, _, _ = self.request("GET", "/healthz")
        self.assertEqual(status, 404)

    def test_messages_response_is_no_store_and_hides_object_tokens(self):
        status, headers, body = self.request_json(
            "POST", "/v1/messages", {"principal": "100", "token": "t"}
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(body["messages"][0]["seq"], "1")
        self.assertEqual(body["messages"][0]["object_ids"], ["901"])
        self.assertNotIn("secret", json.dumps(body))

    def test_payload_response_is_no_store_and_returns_complete_content(self):
        status, headers, body = self.request_json(
            "POST", "/v1/messages/1/payload", {"principal": "100", "token": "t"}
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertFalse(body["message"]["payload"]["truncated"])
        self.assertEqual(body["message"]["payload"]["json"], {"n": 1})

    def test_post_requires_json_body(self):
        status, _, body = self.request_json("POST", "/v1/messages")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_ARGUMENT")

    def test_payload_route_rejects_zero_sequence(self):
        status, _, _ = self.request_json(
            "POST", "/v1/messages/0/payload", {"principal": "1", "token": "t"}
        )
        self.assertEqual(status, 400)

    def test_non_advancing_open_event_cursor_maps_to_bad_gateway(self):
        class BadFetchClient(FakeClient):
            def fetch(self, *args, **kwargs):
                return types.SimpleNamespace(messages=[], next_seq=1, last_seq=1)

        self.history.close()
        self.history = HistoryService(BadFetchClient(), self.server.config.history)
        self.server.history_service = self.history
        status, _, body = self.request_json(
            "POST", "/v1/messages", {"principal": "100", "token": "t"}
        )

        self.assertEqual(status, 502)
        self.assertEqual(body["error"]["code"], "BAD_GATEWAY")

    def test_unlisted_upstream_grpc_errors_map_to_bad_gateway(self):
        for code in (grpc.StatusCode.INVALID_ARGUMENT, grpc.StatusCode.RESOURCE_EXHAUSTED):
            with self.subTest(code=code):
                self.assertEqual(
                    _grpc_to_http(FakeRpcError(code)),
                    (502, code.name),
                )

    def test_rejects_oversized_request_body(self):
        conn = HTTPConnection("127.0.0.1", self.server.server_address[1])
        conn.request(
            "POST",
            "/v1/messages",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(
                    self.server.config.server.max_request_body_bytes + 1
                ),
            },
        )
        response = conn.getresponse()
        body = json.loads(response.read())
        conn.close()

        self.assertEqual(response.status, 413)
        self.assertEqual(body["error"]["code"], "REQUEST_TOO_LARGE")

    def test_frontend_includes_non_destructive_error_and_filter_context(self):
        status, _, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'id="errorBanner"', page)

        status, _, script = self.request("GET", "/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"only messages addressed to this principal", script)
        self.assertIn(b'["objects", (message.object_ids || []).join', script)
        self.assertNotIn(b"list.replaceChildren(node)", script)


if __name__ == "__main__":
    unittest.main()
