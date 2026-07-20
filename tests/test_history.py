import unittest
import threading
from dataclasses import dataclass
from time import sleep

from openevent.view.config import HistoryConfig, PayloadConfig
from openevent.view.history import (
    HistoryQuery,
    HistoryService,
    UpstreamProtocolError,
    encode_payload,
    parse_history_query,
)


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
        return FakeChannelResponse(
            FakeChannel(channel_id, self.channels.get(channel_id, f"channel-{channel_id}"))
        )

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


class ScriptedFetchClient(FakeClient):
    def __init__(self, messages, responses):
        super().__init__(messages)
        self.responses = list(responses)

    def fetch(self, principal, token, from_seq, limit, only_my_recipient=False, channels=()):
        self.fetch_calls.append((from_seq, limit, only_my_recipient, tuple(channels)))
        if not self.responses:
            raise AssertionError("unexpected Fetch call")
        return self.responses.pop(0)


class ConcurrentChannelClient(FakeClient):
    def __init__(self, messages, fail_channel_id=None):
        super().__init__(messages)
        self.fail_channel_id = fail_channel_id
        self.get_channel_calls = []
        self.active_channel_calls = 0
        self.max_active_channel_calls = 0
        self.lock = threading.Lock()

    def get_channel(self, principal, token, channel_id):
        with self.lock:
            self.get_channel_calls.append((principal, token, channel_id))
            self.active_channel_calls += 1
            self.max_active_channel_calls = max(
                self.max_active_channel_calls,
                self.active_channel_calls,
            )
        try:
            sleep(0.02)
            if channel_id == self.fail_channel_id:
                raise RuntimeError("GetChannel failed")
            return FakeChannelResponse(FakeChannel(channel_id, f"channel-{channel_id}"))
        finally:
            with self.lock:
                self.active_channel_calls -= 1


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
            HistoryConfig(default_limit=2, max_limit=1000, fetch_batch_size=2),
            PayloadConfig(),
        )
        result = service.query(HistoryQuery(1, "tok", None, 2, "desc", None, False))
        self.assertEqual([item["seq"] for item in result["messages"]], [3, 2])
        self.assertEqual(result["messages"][0]["channel_name"], "orders")
        self.assertEqual(result["next_cursor"], "2")

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
            HistoryConfig(default_limit=2, max_limit=1000, fetch_batch_size=3),
            PayloadConfig(),
        )
        result = service.query(HistoryQuery(1, "tok", None, 2, "desc", 100, False))
        self.assertEqual([item["seq"] for item in result["messages"]], [3, 1])
        self.assertTrue(all(call[3] == (100,) for call in client.fetch_calls))

    def test_desc_query_continues_through_sparse_empty_windows(self):
        client = FakeClient(
            [
                FakeMessage(85, 850, 100, 1, [], b'{"n":85}'),
                FakeMessage(100, 1000, 200, 1, [], b'{"n":100}'),
            ]
        )
        service = HistoryService(
            client,
            HistoryConfig(default_limit=2, max_limit=1000, fetch_batch_size=10),
            PayloadConfig(),
        )
        result = service.query(HistoryQuery(1, "tok", None, 2, "desc", 100, False))
        self.assertEqual([item["seq"] for item in result["messages"]], [85])
        self.assertIsNone(result["next_cursor"])

    def test_desc_query_does_not_report_more_after_history_start(self):
        client = FakeClient(
            [
                FakeMessage(1, 10, 100, 1, [], b'{"n":1}'),
                FakeMessage(2, 20, 200, 1, [], b'{"n":2}'),
                FakeMessage(3, 30, 200, 1, [], b'{"n":3}'),
            ]
        )
        service = HistoryService(
            client,
            HistoryConfig(default_limit=5, max_limit=1000, fetch_batch_size=10),
            PayloadConfig(),
        )
        result = service.query(HistoryQuery(1, "tok", None, 5, "desc", 100, False))
        self.assertEqual([item["seq"] for item in result["messages"]], [1])
        self.assertIsNone(result["next_cursor"])

    def test_desc_query_completes_short_window_without_omissions(self):
        messages = [
            FakeMessage(seq, seq * 10, 100, 1, [], f'{{"n":{seq}}}'.encode())
            for seq in range(1, 5)
        ]
        client = ScriptedFetchClient(
            messages,
            [
                FakeFetchResponse(messages=[messages[2]], next_seq=4, last_seq=4),
                FakeFetchResponse(messages=[messages[3]], next_seq=5, last_seq=4),
            ],
        )
        service = HistoryService(
            client,
            HistoryConfig(default_limit=2, max_limit=1000, fetch_batch_size=4),
            PayloadConfig(),
        )

        result = service.query(HistoryQuery(1, "tok", None, 2, "desc", None, False))

        self.assertEqual([item["seq"] for item in result["messages"]], [4, 3])
        self.assertEqual(result["next_cursor"], "3")
        self.assertEqual([(call[0], call[1]) for call in client.fetch_calls], [(3, 2), (4, 1)])

        next_client = ScriptedFetchClient(
            messages,
            [FakeFetchResponse(messages=messages[:2], next_seq=3, last_seq=4)],
        )
        next_service = HistoryService(
            next_client,
            HistoryConfig(default_limit=2, max_limit=1000, fetch_batch_size=4),
            PayloadConfig(),
        )
        next_result = next_service.query(HistoryQuery(1, "tok", 3, 2, "desc", None, False))
        self.assertEqual([item["seq"] for item in next_result["messages"]], [2, 1])

    def test_desc_query_continues_empty_page_inside_window(self):
        messages = [
            FakeMessage(seq, seq * 10, 100, 1, [], f'{{"n":{seq}}}'.encode())
            for seq in range(1, 5)
        ]
        client = ScriptedFetchClient(
            messages,
            [
                FakeFetchResponse(messages=[], next_seq=4, last_seq=4),
                FakeFetchResponse(messages=[messages[3]], next_seq=5, last_seq=4),
                FakeFetchResponse(messages=[messages[1]], next_seq=3, last_seq=4),
            ],
        )
        service = HistoryService(
            client,
            HistoryConfig(default_limit=2, max_limit=1000, fetch_batch_size=4),
            PayloadConfig(),
        )

        result = service.query(HistoryQuery(1, "tok", None, 2, "desc", None, False))

        self.assertEqual([item["seq"] for item in result["messages"]], [4, 2])
        self.assertEqual([(call[0], call[1]) for call in client.fetch_calls], [(3, 2), (4, 1), (2, 1)])

    def test_desc_query_rejects_non_advancing_fetch_cursor(self):
        messages = [FakeMessage(1, 10, 100, 1, [], b'{"n":1}')]
        client = ScriptedFetchClient(
            messages,
            [FakeFetchResponse(messages=[], next_seq=1, last_seq=1)],
        )
        service = HistoryService(
            client,
            HistoryConfig(default_limit=1, max_limit=1000, fetch_batch_size=1),
            PayloadConfig(),
        )

        with self.assertRaises(UpstreamProtocolError):
            service.query(HistoryQuery(1, "tok", None, 1, "desc", None, False))

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
            HistoryConfig(default_limit=2, max_limit=1000, fetch_batch_size=2),
            PayloadConfig(),
        )
        result = service.query(HistoryQuery(1, "tok", None, 2, "asc", None, False))
        self.assertEqual([item["seq"] for item in result["messages"]], [1, 2])
        self.assertEqual(result["next_cursor"], "3")

    def test_asc_query_continues_empty_short_page(self):
        messages = [
            FakeMessage(seq, seq * 10, 100, 1, [], f'{{"n":{seq}}}'.encode())
            for seq in range(1, 5)
        ]
        client = ScriptedFetchClient(
            messages,
            [
                FakeFetchResponse(messages=[], next_seq=3, last_seq=4),
                FakeFetchResponse(messages=messages[2:], next_seq=5, last_seq=4),
            ],
        )
        service = HistoryService(
            client,
            HistoryConfig(default_limit=2, max_limit=1000, fetch_batch_size=4),
            PayloadConfig(),
        )

        result = service.query(HistoryQuery(1, "tok", None, 2, "asc", None, False))

        self.assertEqual([item["seq"] for item in result["messages"]], [3, 4])
        self.assertEqual([call[0] for call in client.fetch_calls], [1, 3])

    def test_asc_query_rejects_non_advancing_fetch_cursor_inside_tail(self):
        messages = [FakeMessage(1, 10, 100, 1, [], b'{"n":1}')]
        client = ScriptedFetchClient(
            messages,
            [FakeFetchResponse(messages=[], next_seq=1, last_seq=1)],
        )
        service = HistoryService(
            client,
            HistoryConfig(default_limit=1, max_limit=1000, fetch_batch_size=1),
            PayloadConfig(),
        )

        with self.assertRaises(UpstreamProtocolError):
            service.query(HistoryQuery(1, "tok", None, 1, "asc", None, False))

    def test_asc_query_allows_cursor_beyond_current_tail(self):
        messages = [
            FakeMessage(1, 10, 100, 1, [], b'{"n":1}'),
            FakeMessage(2, 20, 100, 1, [], b'{"n":2}'),
        ]
        client = ScriptedFetchClient(
            messages,
            [FakeFetchResponse(messages=[], next_seq=3, last_seq=2)],
        )
        service = HistoryService(
            client,
            HistoryConfig(default_limit=1, max_limit=1000, fetch_batch_size=1),
            PayloadConfig(),
        )

        result = service.query(HistoryQuery(1, "tok", 10, 1, "asc", None, False))

        self.assertEqual(result["messages"], [])
        self.assertIsNone(result["next_cursor"])

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
            HistoryConfig(default_limit=2, max_limit=1000, fetch_batch_size=10),
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

    def test_parse_query_rejects_zero_cursor(self):
        with self.assertRaisesRegex(ValueError, "cursor must be a positive integer"):
            parse_history_query(
                {"principal": "10", "token": "tok", "cursor": 0},
                HistoryConfig(),
            )

    def test_channel_names_are_loaded_concurrently_and_cached(self):
        messages = [
            FakeMessage(seq, seq * 10, seq, 1, [], b"{}")
            for seq in range(1, 5)
        ]
        client = ConcurrentChannelClient(messages)
        service = HistoryService(
            client,
            HistoryConfig(default_limit=4, max_limit=1000, fetch_batch_size=4),
            PayloadConfig(),
            channel_cache_size=8,
            channel_lookup_workers=2,
        )
        query = HistoryQuery(1, "tok", None, 4, "asc", None, False)
        first = service.query(query)
        second = service.query(query)
        service.close()

        self.assertEqual(client.max_active_channel_calls, 2)
        self.assertEqual(len(client.get_channel_calls), 4)
        self.assertEqual(first["messages"][0]["channel_name"], "channel-1")
        self.assertEqual(second["messages"][3]["channel_name"], "channel-4")

    def test_channel_name_failure_fails_query(self):
        messages = [FakeMessage(1, 10, 100, 1, [], b"{}")]
        client = ConcurrentChannelClient(messages, fail_channel_id=100)
        service = HistoryService(
            client,
            HistoryConfig(default_limit=1, max_limit=1000, fetch_batch_size=1),
            PayloadConfig(),
        )
        with self.assertRaisesRegex(RuntimeError, "GetChannel failed"):
            service.query(HistoryQuery(1, "tok", None, 1, "asc", None, False))
        service.close()

    def test_empty_channel_name_fails_query(self):
        messages = [FakeMessage(1, 10, 100, 1, [], b"{}")]
        client = FakeClient(messages, channels={100: ""})
        service = HistoryService(
            client,
            HistoryConfig(default_limit=1, max_limit=1000, fetch_batch_size=1),
            PayloadConfig(),
        )
        with self.assertRaisesRegex(UpstreamProtocolError, "empty channel name"):
            service.query(HistoryQuery(1, "tok", None, 1, "asc", None, False))
        service.close()

    def test_channel_name_cache_is_lru_bounded(self):
        messages = [
            FakeMessage(seq, seq * 10, seq, 1, [], b"{}")
            for seq in range(1, 4)
        ]
        client = ConcurrentChannelClient(messages)
        service = HistoryService(
            client,
            HistoryConfig(default_limit=1, max_limit=1000, fetch_batch_size=1),
            PayloadConfig(),
            channel_cache_size=2,
        )
        for cursor in (1, 2, 3, 1):
            service.query(HistoryQuery(1, "tok", cursor, 1, "asc", None, False))
        service.close()

        self.assertEqual([call[2] for call in client.get_channel_calls], [1, 2, 3, 1])


if __name__ == "__main__":
    unittest.main()
