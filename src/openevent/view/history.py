from __future__ import annotations

import base64
import json
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

from .config import HistoryConfig, PayloadConfig


class RequestError(ValueError):
    code = "INVALID_ARGUMENT"


class UpstreamProtocolError(RuntimeError):
    code = "BAD_GATEWAY"


class OpenEventClientProtocol(Protocol):
    def get_status(self, principal: int, token: str) -> Any:
        ...

    def get_channel(self, principal: int, token: str, channel_id: int) -> Any:
        ...

    def fetch(
        self,
        principal: int,
        token: str,
        from_seq: int,
        limit: int,
        only_my_recipient: bool = False,
        channels: tuple[int, ...] = (),
    ) -> Any:
        ...


@dataclass(frozen=True)
class HistoryQuery:
    principal: int
    token: str
    cursor: int | None
    limit: int
    order: str
    channel_id: int | None
    only_my_recipient: bool


class HistoryService:
    def __init__(
        self,
        client: OpenEventClientProtocol,
        history_config: HistoryConfig,
        payload_config: PayloadConfig,
        channel_cache_size: int = 4096,
        channel_lookup_workers: int = 8,
    ):
        self._client = client
        self._history = history_config
        self._payload = payload_config
        self._channel_cache_size = channel_cache_size
        self._channel_names: OrderedDict[tuple[int, int], str] = OrderedDict()
        self._channel_names_lock = threading.Lock()
        self._channel_executor = ThreadPoolExecutor(
            max_workers=channel_lookup_workers,
            thread_name_prefix="openevent-view-channel",
        )

    def close(self) -> None:
        self._channel_executor.shutdown(wait=True, cancel_futures=True)

    def query(self, query: HistoryQuery) -> dict[str, Any]:
        if query.order == "asc":
            return self._query_asc(query)
        return self._query_desc(query)

    def _query_asc(self, query: HistoryQuery) -> dict[str, Any]:
        from_seq = query.cursor or 1
        collected: list[Any] = []
        next_seq = from_seq
        has_more = False
        channels = _query_channels(query)

        while len(collected) < query.limit:
            request_from = next_seq
            batch_limit = min(self._history.fetch_batch_size, query.limit - len(collected))
            response = self._client.fetch(
                query.principal,
                query.token,
                from_seq=request_from,
                limit=batch_limit,
                only_my_recipient=query.only_my_recipient,
                channels=channels,
            )
            batch = list(response.messages)
            response_next_seq = int(response.next_seq)
            response_last_seq = int(response.last_seq)
            if request_from <= response_last_seq and response_next_seq <= request_from:
                raise UpstreamProtocolError("OpenEvent Fetch next_seq did not advance")

            next_seq = response_next_seq
            has_more = next_seq <= response_last_seq
            for message in batch:
                if query.channel_id is None or int(message.channel_id) == query.channel_id:
                    collected.append(message)
                    if len(collected) >= query.limit:
                        break
            if not has_more:
                break

        return {
            "messages": self._messages_to_dicts(query, collected),
            "next_cursor": str(next_seq) if has_more else None,
        }

    def _query_desc(self, query: HistoryQuery) -> dict[str, Any]:
        status = self._client.get_status(query.principal, query.token)
        max_seq = int(status.max_seq)
        min_seq = int(status.min_seq)
        if max_seq <= 0 or min_seq <= 0:
            return {
                "messages": [],
                "next_cursor": None,
            }

        end_seq = max_seq if query.cursor is None else min(query.cursor - 1, max_seq)
        if end_seq < min_seq:
            return {
                "messages": [],
                "next_cursor": None,
            }

        collected: list[Any] = []
        window_end = end_seq
        channels = _query_channels(query)

        while window_end >= min_seq and len(collected) < query.limit:
            remaining = query.limit - len(collected)
            window_size = min(self._history.fetch_batch_size, remaining)
            window_start = max(min_seq, window_end - window_size + 1)
            window_messages = self._fetch_desc_window(
                query,
                channels,
                window_start,
                window_end,
                remaining,
            )
            for message in sorted(window_messages, key=lambda item: int(item.seq), reverse=True):
                if query.channel_id is None or int(message.channel_id) == query.channel_id:
                    collected.append(message)
                    if len(collected) >= query.limit:
                        break
            window_end = window_start - 1

        if len(collected) >= query.limit:
            next_cursor = str(int(collected[-1].seq))
            if int(next_cursor) <= min_seq:
                next_cursor = None
        else:
            next_cursor = None

        return {
            "messages": self._messages_to_dicts(query, collected),
            "next_cursor": next_cursor,
        }

    def _fetch_desc_window(
        self,
        query: HistoryQuery,
        channels: tuple[int, ...],
        window_start: int,
        window_end: int,
        remaining: int,
    ) -> list[Any]:
        fetch_from = window_start
        messages: list[Any] = []
        while fetch_from <= window_end:
            response = self._client.fetch(
                query.principal,
                query.token,
                from_seq=fetch_from,
                limit=min(remaining, window_end - fetch_from + 1),
                only_my_recipient=query.only_my_recipient,
                channels=channels,
            )
            response_next_seq = int(response.next_seq)
            if response_next_seq <= fetch_from:
                raise UpstreamProtocolError("OpenEvent Fetch next_seq did not advance")
            messages.extend(
                message
                for message in response.messages
                if fetch_from <= int(message.seq) <= window_end
            )
            fetch_from = response_next_seq
        return messages

    def _messages_to_dicts(self, query: HistoryQuery, messages: list[Any]) -> list[dict[str, Any]]:
        channel_names = self._load_channel_names(query.principal, query.token, messages)
        return [self._message_to_dict(message, channel_names) for message in messages]

    def _message_to_dict(self, message: Any, channel_names: dict[int, str]) -> dict[str, Any]:
        channel_id = int(message.channel_id)
        return {
            "seq": int(message.seq),
            "ts_ms": int(message.ts_ms),
            "channel_id": channel_id,
            "channel_name": channel_names[channel_id],
            "principal": int(message.principal),
            "recipients": [int(item) for item in message.recipients],
            "payload": encode_payload(bytes(message.payload), self._payload),
        }

    def _load_channel_names(self, principal: int, token: str, messages: list[Any]) -> dict[int, str]:
        names: dict[int, str] = {}
        channel_ids = sorted({int(message.channel_id) for message in messages})
        missing: list[int] = []
        for channel_id in channel_ids:
            cached = self._get_cached_channel_name(principal, channel_id)
            if cached is None:
                missing.append(channel_id)
            else:
                names[channel_id] = cached

        if missing:
            futures = {
                channel_id: self._channel_executor.submit(
                    self._get_channel_name,
                    principal,
                    token,
                    channel_id,
                )
                for channel_id in missing
            }
            for channel_id, future in futures.items():
                name = future.result()
                names[channel_id] = name
                self._cache_channel_name(principal, channel_id, name)
        return names

    def _get_channel_name(self, principal: int, token: str, channel_id: int) -> str:
        response = self._client.get_channel(principal, token, channel_id)
        channel = getattr(response, "channel", None)
        name = getattr(channel, "name", None)
        if not name:
            raise UpstreamProtocolError("OpenEvent GetChannel returned an empty channel name")
        return str(name)

    def _get_cached_channel_name(self, principal: int, channel_id: int) -> str | None:
        key = (principal, channel_id)
        with self._channel_names_lock:
            name = self._channel_names.get(key)
            if name is not None:
                self._channel_names.move_to_end(key)
            return name

    def _cache_channel_name(self, principal: int, channel_id: int, name: str) -> None:
        key = (principal, channel_id)
        with self._channel_names_lock:
            self._channel_names[key] = name
            self._channel_names.move_to_end(key)
            while len(self._channel_names) > self._channel_cache_size:
                self._channel_names.popitem(last=False)


def parse_history_query(data: dict[str, Any], history_config: HistoryConfig) -> HistoryQuery:
    principal = _required_uint64(data.get("principal"), "principal")
    token = _required_str(data.get("token"), "token")
    cursor = _optional_positive_uint64(data.get("cursor"), "cursor")
    raw_limit = _optional_uint64(data.get("limit"), "limit")
    limit = raw_limit if raw_limit is not None else history_config.default_limit
    if limit < 1 or limit > history_config.max_limit:
        raise RequestError(f"limit must be between 1 and {history_config.max_limit}")
    order = data.get("order") or history_config.default_order
    if order not in {"asc", "desc"}:
        raise RequestError("order must be asc or desc")
    channel_id = _optional_uint64(data.get("channel_id"), "channel_id")
    only_my_recipient = _optional_bool(data.get("only_my_recipient"), "only_my_recipient")
    return HistoryQuery(
        principal=principal,
        token=token,
        cursor=cursor,
        limit=limit,
        order=order,
        channel_id=channel_id,
        only_my_recipient=only_my_recipient,
    )


def _query_channels(query: HistoryQuery) -> tuple[int, ...]:
    if query.channel_id is None:
        return ()
    return (query.channel_id,)


def encode_payload(payload: bytes, config: PayloadConfig) -> dict[str, Any]:
    result: dict[str, Any] = {
        "encoding": "utf-8",
        "text": None,
        "json": None,
        "truncated": False,
        "size_bytes": len(payload),
    }
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        result["encoding"] = "base64"
        encoded = base64.b64encode(payload).decode("ascii")
        result["text"] = _truncate_text(encoded, config.text_max_bytes, result)
        return result

    if config.include_text:
        result["text"] = _truncate_text(text, config.text_max_bytes, result)
    if config.parse_json:
        try:
            result["json"] = json.loads(text)
        except json.JSONDecodeError:
            result["json_error"] = "payload is not valid JSON"
    return result


def _truncate_text(text: str, max_bytes: int, result: dict[str, Any]) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    result["truncated"] = True
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _required_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RequestError(f"{field} must be a non-empty string")
    return value


def _required_uint64(value: Any, field: str) -> int:
    parsed = _optional_uint64(value, field)
    if parsed is None:
        raise RequestError(f"{field} is required")
    return parsed


def _optional_uint64(value: Any, field: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise RequestError(f"{field} must be an unsigned integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError as exc:
            raise RequestError(f"{field} must be an unsigned integer") from exc
    else:
        raise RequestError(f"{field} must be an unsigned integer")
    if parsed < 0:
        raise RequestError(f"{field} must be an unsigned integer")
    if parsed > 2**64 - 1:
        raise RequestError(f"{field} must fit uint64")
    return parsed


def _optional_positive_uint64(value: Any, field: str) -> int | None:
    parsed = _optional_uint64(value, field)
    if parsed == 0:
        raise RequestError(f"{field} must be a positive integer")
    return parsed


def _optional_bool(value: Any, field: str) -> bool:
    if value in (None, ""):
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise RequestError(f"{field} must be a boolean")
