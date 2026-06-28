import unittest
from dataclasses import dataclass

from openevent.view.config import HistoryConfig, PayloadConfig
from openevent.view.history import HistoryQuery, HistoryService, encode_payload, parse_history_query


@dataclass
class FakeStatus:
    min_seq: int
    max_seq: int


@dataclass
class FakeMessage:
    seq: int
    ts_ms: int
    channel_id: int
    principal: int
    recipients: list[int]
    payload: bytes


@dataclass
class FakeFetchResponse:
    messages: list[FakeMessage]
    next_seq: int
    last_seq: int


@dataclass
class FakeChannel:
    channel_id: int
    name: str


@dataclass
class FakeChannelResponse:
    channel: FakeChannel


class FakeClient:
    def __init__(self, messages, channels=None):
        self.messages = messages
        self.channels = channels or {}
        self.fetch_calls = []

    def get_status(self, principal, token):
        if not self.messages:
            return FakeStatus(min_seq=0, max_seq=0)
        return FakeStatus(min_seq=min(message.seq for message in self.messages), max_seq=max(message.seq for message in self.messages))

    def get_channel(self, principal, token, channel_id):
        return FakeChannelResponse(FakeChannel(channel_id, self.channels.get(channel_id, "")))

    def fetch(self, principal, token, from_seq, limit, only_my_recipient=False, channels=()):
        channels = tuple(channels)
        self.fetch_calls.append((from_seq, limit, only_my_recipient, channels))
        matches = [message for message in self.messages if message.seq >= from_seq]
        if channels:
            matches = [message for message in matches if message.channel_id in channels]
        if only_my_recipient:
            matches = [message for message in matches if principal in message.recipients]
        batch = matches[:limit]
        status = self.get_status(principal, token)
        next_seq = batch[-1].seq + 1 if len(matches) > len(batch) else status.max_seq + 1
        return FakeFetchResponse(messages=batch, next_seq=next_seq, last_seq=status.max_seq)


class HistoryTests(unittest.TestCase):
    def test_payload_json(self):
        payload = encode_payload(b'{"kind":"sync.record","data":{"text":"hi"}}', PayloadConfig())
        self.assertEqual(payload["encoding"], "utf-8")
        self.assertEqual(payload["json"]["kind"], "sync.record")

    def test_payload_text_fallback(self):
        payload = encode_payload(b"not-json", PayloadConfig())
        self.assertEqual(payload["text"], "not-json")
        self.assertEqual(payload["json"], None)
        self.assertIn("json_error", payload)

    def test_desc_query_returns_latest_first(self):
        client = FakeClient(
            [
                FakeMessage(1, 10, 100, 1, [], b'{"n":1}'),
                FakeMessage(2, 20, 100, 1, [], b'{"n":2}'),
                FakeMessage(3, 30, 100, 1, [], b'{"n":3}'),
            ],
            channels={100: "orders"},
        )
        service = HistoryService(
            client,
            HistoryConfig(default_limit=2, max_limit=1000, fetch_batch_size=2, max_scan_messages=10),
            PayloadConfig(),
        )
        result = service.query(HistoryQuery(1, "tok", None, 2, "desc", None, False))
        self.assertEqual([item["seq"] for item in result["messages"]], [3, 2])
        self.assertEqual(result["messages"][0]["channel_name"], "orders")
        self.assertEqual(result["next_cursor"], "2")
        self.assertTrue(result["has_more"])

    def test_desc_channel_filter(self):
        client = FakeClient(
            [
                FakeMessage(1, 10, 100, 1, [], b'{"n":1}'),
                FakeMessage(2, 20, 200, 1, [], b'{"n":2}'),
                FakeMessage(3, 30, 100, 1, [], b'{"n":3}'),
            ]
        )
        service = HistoryService(
            client,
            HistoryConfig(default_limit=2, max_limit=1000, fetch_batch_size=3, max_scan_messages=10),
            PayloadConfig(),
        )
        result = service.query(HistoryQuery(1, "tok", None, 2, "desc", 100, False))
        self.assertEqual([item["seq"] for item in result["messages"]], [3, 1])
        self.assertTrue(all(call[3] == (100,) for call in client.fetch_calls))

    def test_asc_query_uses_last_seq_for_more(self):
        client = FakeClient(
            [
                FakeMessage(1, 10, 100, 1, [], b'{"n":1}'),
                FakeMessage(2, 20, 100, 1, [], b'{"n":2}'),
                FakeMessage(3, 30, 100, 1, [], b'{"n":3}'),
            ]
        )
        service = HistoryService(
            client,
            HistoryConfig(default_limit=2, max_limit=1000, fetch_batch_size=2, max_scan_messages=10),
            PayloadConfig(),
        )
        result = service.query(HistoryQuery(1, "tok", None, 2, "asc", None, False))
        self.assertEqual([item["seq"] for item in result["messages"]], [1, 2])
        self.assertEqual(result["next_cursor"], "3")
        self.assertTrue(result["has_more"])

    def test_asc_channel_filter_uses_page_limit(self):
        client = FakeClient(
            [
                FakeMessage(1, 10, 100, 1, [], b'{"n":1}'),
                FakeMessage(2, 20, 100, 1, [], b'{"n":2}'),
                FakeMessage(3, 30, 100, 1, [], b'{"n":3}'),
            ]
        )
        service = HistoryService(
            client,
            HistoryConfig(default_limit=2, max_limit=1000, fetch_batch_size=10, max_scan_messages=10),
            PayloadConfig(),
        )
        result = service.query(HistoryQuery(1, "tok", None, 2, "asc", 100, False))
        self.assertEqual([item["seq"] for item in result["messages"]], [1, 2])
        self.assertEqual(result["next_cursor"], "3")
        self.assertEqual(client.fetch_calls[0], (1, 2, False, (100,)))

    def test_parse_query_defaults(self):
        query = parse_history_query({"principal": "10", "token": "tok"}, HistoryConfig(default_limit=7))
        self.assertEqual(query.limit, 7)
        self.assertEqual(query.order, "desc")
        self.assertIsNone(query.cursor)

    def test_parse_query_rejects_zero_limit(self):
        with self.assertRaises(ValueError):
            parse_history_query({"principal": "10", "token": "tok", "limit": 0}, HistoryConfig())


if __name__ == "__main__":
    unittest.main()
