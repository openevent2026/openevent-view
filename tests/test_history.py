import base64
import types
import unittest

import grpc

from openevent.view.config import HistoryConfig
from openevent.view.history import (
    HistoryService,
    MessageNotFound,
    PayloadQuery,
    RequestError,
    UpstreamProtocolError,
    encode_payload,
    parse_history_query,
    parse_payload_query,
    parse_seq,
)


def message(seq, channel=7, payload=b'{"n":1}', principal=100, recipients=(100,)):
    return types.SimpleNamespace(
        seq=seq,
        uuid=seq + 1000,
        ts_ms=1700000000000 + seq,
        channel_id=channel,
        principal=principal,
        recipients=list(recipients),
        payload=payload,
        object_keys=[
            types.SimpleNamespace(object_id=900 + seq, object_token="secret-token")
        ],
    )


class FakeRpcError(grpc.RpcError):
    def __init__(self, status_code):
        self._status_code = status_code

    def code(self):
        return self._status_code


class FakeClient:
    def __init__(self, messages, min_seq=1, max_seq=None):
        self.messages = sorted(messages, key=lambda item: item.seq)
        self.min_seq = min_seq
        self.max_seq = max_seq if max_seq is not None else (
            self.messages[-1].seq if self.messages else 0
        )
        self.calls = []
        self.channels = {}

    def get_status(self, principal, token):
        self.calls.append(("status", principal, token))
        return types.SimpleNamespace(min_seq=self.min_seq, max_seq=self.max_seq)

    def fetch(
        self,
        principal,
        token,
        from_seq,
        limit,
        only_my_recipient=False,
        channels=(),
    ):
        self.calls.append(("fetch", from_seq, limit, only_my_recipient, channels))
        visible = [
            item
            for item in self.messages
            if item.seq >= from_seq
            and (not channels or item.channel_id in channels)
            and (
                not only_my_recipient
                or principal in {int(recipient) for recipient in item.recipients}
            )
        ][:limit]
        next_seq = visible[-1].seq + 1 if visible else self.max_seq + 1
        return types.SimpleNamespace(
            messages=visible,
            next_seq=next_seq,
            last_seq=self.max_seq,
        )

    def get_channel(self, principal, token, channel_id):
        self.calls.append(("channel", channel_id))
        name, protocol = self.channels.get(
            channel_id, (f"channel-{channel_id}", "chat.v1")
        )
        return types.SimpleNamespace(
            channel=types.SimpleNamespace(name=name, protocol=protocol)
        )


class ShortPageClient(FakeClient):
    def fetch(self, *args, **kwargs):
        from_seq = kwargs["from_seq"]
        self.calls.append(
            (
                "fetch",
                from_seq,
                kwargs["limit"],
                kwargs["only_my_recipient"],
                kwargs["channels"],
            )
        )
        scripted = {
            2: ([self.messages[0]], 3),
            3: ([], 4),
            4: ([self.messages[1]], 5),
        }
        messages, next_seq = scripted[from_seq]
        return types.SimpleNamespace(
            messages=messages,
            next_seq=next_seq,
            last_seq=self.max_seq,
        )


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.config = HistoryConfig(default_limit=2, max_limit=10, fetch_batch_size=2)

    def make_service(self, client, config=None):
        service = HistoryService(client, config or self.config)
        self.addCleanup(service.close)
        return service

    def test_query_authenticates_at_the_history_boundary(self):
        client = FakeClient([message(1)], min_seq=1, max_seq=1)
        result = self.make_service(client).query(
            parse_history_query(
                {
                    "principal": "100",
                    "token": "t",
                    "cursor": {"before_seq": "1"},
                },
                self.config,
            )
        )
        self.assertEqual(result["messages"], [])
        self.assertEqual(client.calls[0][0], "status")

    def test_descending_cursor_scans_multiple_windows_without_skipping(self):
        config = HistoryConfig(default_limit=3, max_limit=10, fetch_batch_size=2)
        client = FakeClient([message(seq) for seq in range(1, 6)])
        service = self.make_service(client, config)

        first = service.query(parse_history_query({"principal": "100", "token": "t"}, config))
        second = service.query(
            parse_history_query(
                {
                    "principal": "100",
                    "token": "t",
                    "cursor": first["next_cursor"],
                },
                config,
            )
        )

        self.assertEqual([item["seq"] for item in first["messages"]], ["5", "4", "3"])
        self.assertEqual(first["next_cursor"], {"before_seq": "3"})
        self.assertEqual([item["seq"] for item in second["messages"]], ["2", "1"])
        self.assertIsNone(second["next_cursor"])

    def test_short_fetch_pages_are_completed_before_descending_selection(self):
        client = ShortPageClient([message(2), message(4)], min_seq=1, max_seq=4)
        result = self.make_service(client).query(
            parse_history_query({"principal": "100", "token": "t"}, self.config)
        )

        self.assertEqual([item["seq"] for item in result["messages"]], ["4", "2"])
        self.assertEqual(
            [call[1] for call in client.calls if call[0] == "fetch"], [3, 4, 2]
        )

    def test_recipient_filter_scans_past_nonmatching_messages(self):
        client = FakeClient(
            [
                message(1, recipients=(100,)),
                message(2, recipients=(100,)),
                message(3, recipients=(200,)),
            ]
        )
        result = self.make_service(client).query(
            parse_history_query(
                {
                    "principal": "100",
                    "token": "t",
                    "only_my_recipient": True,
                },
                self.config,
            )
        )

        self.assertEqual([item["seq"] for item in result["messages"]], ["2", "1"])
        self.assertIsNone(result["next_cursor"])

    def test_channel_filter_at_boundary_still_loads_display_metadata(self):
        client = FakeClient([], min_seq=1, max_seq=5)
        result = self.make_service(client).query(
            parse_history_query(
                {
                    "principal": "100",
                    "token": "t",
                    "channel_id": "7",
                    "cursor": {"before_seq": "1"},
                },
                self.config,
            )
        )

        self.assertEqual(result["channel"]["channel_name"], "channel-7")
        self.assertIn(("channel", 7), client.calls)

    def test_message_projection_hides_object_tokens(self):
        client = FakeClient([message(1)])
        result = self.make_service(client).query(
            parse_history_query({"principal": "100", "token": "t"}, self.config)
        )

        serialized = result["messages"][0]
        self.assertEqual(serialized["object_ids"], ["901"])
        self.assertNotIn("object_keys", serialized)
        self.assertNotIn("secret-token", str(serialized))

    def test_payload_lookup_returns_full_payload(self):
        payload = b"x" * 20000
        client = FakeClient([message(2, payload=payload)], min_seq=1, max_seq=2)
        result = self.make_service(client).get_payload(
            2, PayloadQuery(principal=100, token="t")
        )

        self.assertEqual(result["message"]["payload"]["text"], "x" * 20000)
        self.assertFalse(result["message"]["payload"]["truncated"])
        self.assertEqual(result["message"]["object_ids"], ["902"])

    def test_payload_lookup_hides_permission_denied_as_not_found(self):
        class DeniedClient(FakeClient):
            def fetch(self, *args, **kwargs):
                raise FakeRpcError(grpc.StatusCode.PERMISSION_DENIED)

        service = self.make_service(DeniedClient([message(2)], min_seq=1, max_seq=2))
        with self.assertRaises(MessageNotFound):
            service.get_payload(2, PayloadQuery(principal=100, token="t"))

    def test_large_payload_preview_preserves_byte_boundaries(self):
        text_payload = b"a" * 8191 + "€".encode("utf-8") + b"b" * 9000
        preview = encode_payload(text_payload)
        self.assertEqual(preview["encoding"], "utf-8")
        self.assertEqual(preview["preview"]["head_bytes"], 8191)
        self.assertEqual(preview["preview"]["head"], "a" * 8191)

        binary_preview = encode_payload(b"\xff" * 17000)
        self.assertEqual(binary_preview["encoding"], "base64")
        self.assertEqual(
            len(base64.b64decode(binary_preview["preview"]["head"])), 8192
        )

    def test_strict_history_query_parsing(self):
        with self.assertRaises(RequestError):
            parse_history_query({"principal": 100, "token": "t"}, self.config)
        with self.assertRaises(RequestError):
            parse_history_query(
                {
                    "principal": "100",
                    "token": "t",
                    "cursor": {"before_seq": "01"},
                },
                self.config,
            )
        with self.assertRaises(RequestError):
            parse_history_query(
                {"principal": "100", "token": "t", "limit": True}, self.config
            )
        self.assertEqual(
            parse_payload_query({"principal": "100", "token": "t"}).principal, 100
        )
        self.assertEqual(parse_seq("42"), 42)

    def test_fetch_progress_is_required(self):
        class BadClient(FakeClient):
            def fetch(self, *args, **kwargs):
                return types.SimpleNamespace(
                    messages=[],
                    next_seq=kwargs["from_seq"],
                    last_seq=self.max_seq,
                )

        service = self.make_service(BadClient([message(1), message(2), message(3)]))
        with self.assertRaises(UpstreamProtocolError):
            service.query(
                parse_history_query({"principal": "100", "token": "t"}, self.config)
            )


if __name__ == "__main__":
    unittest.main()
