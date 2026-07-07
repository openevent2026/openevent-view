from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Protocol

from .config import HistoryConfig, PayloadConfig


class RequestError(ValueError):
    code = "INVALID_ARGUMENT"


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
    ):
        self._client = client
        self._history = history_config
        self._payload = payload_config

    def query(self, query: HistoryQuery) -> dict[str, Any]:
        if query.order == "asc":
            return self._query_asc(query)
        return self._query_desc(query)

    def _query_asc(self, query: HistoryQuery) -> dict[str, Any]:
        from_seq = query.cursor or 1
        collected: list[Any] = []
        scanned = 0
        next_seq = from_seq
        has_more = False
        channels = _query_channels(query)

        while len(collected) < query.limit and scanned < self._history.max_scan_messages:
            batch_limit = min(self._history.fetch_batch_size, query.limit - len(collected))
            response = self._client.fetch(
                query.principal,
                query.token,
                from_seq=next_seq,
                limit=batch_limit,
                only_my_recipient=query.only_my_recipient,
                channels=channels,
            )
            batch = list(response.messages)
            scanned += len(batch)
            next_seq = int(response.next_seq)
            has_more = next_seq <= int(response.last_seq)
            for message in batch:
                if query.channel_id is None or int(message.channel_id) == query.channel_id:
                    collected.append(message)
                    if len(collected) >= query.limit:
                        break
            if not has_more or not batch:
                break

        has_more = has_more or scanned >= self._history.max_scan_messages
        return {
            "messages": self._messages_to_dicts(query, collected),
            "next_cursor": str(next_seq) if has_more else None,
            "has_more": has_more,
            "order": "asc",
            "scanned": scanned,
        }

    def _query_desc(self, query: HistoryQuery) -> dict[str, Any]:
        status = self._client.get_status(query.principal, query.token)
        max_seq = int(status.max_seq)
        min_seq = int(status.min_seq)
        if max_seq <= 0 or min_seq <= 0:
            return {
                "messages": [],
                "next_cursor": None,
                "has_more": False,
                "order": "desc",
                "scanned": 0,
            }

        end_seq = max_seq if query.cursor is None else min(query.cursor - 1, max_seq)
        if end_seq < min_seq:
            return {
                "messages": [],
                "next_cursor": None,
                "has_more": False,
                "order": "desc",
                "scanned": 0,
            }

        collected: list[Any] = []
        scanned = 0
        window_end = end_seq
        channels = _query_channels(query)

        while (
            window_end >= min_seq
            and len(collected) < query.limit
            and scanned < self._history.max_scan_messages
        ):
            remaining_scan = self._history.max_scan_messages - scanned
            window_size = min(self._history.fetch_batch_size, remaining_scan)
            window_start = max(min_seq, window_end - window_size + 1)
            response = self._client.fetch(
                query.principal,
                query.token,
                from_seq=window_start,
                limit=window_size,
                only_my_recipient=query.only_my_recipient,
                channels=channels,
            )
            scanned += window_end - window_start + 1
            batch = [
                message
                for message in response.messages
                if window_start <= int(message.seq) <= window_end
            ]
            for message in reversed(batch):
                if query.channel_id is None or int(message.channel_id) == query.channel_id:
                    collected.append(message)
                    if len(collected) >= query.limit:
                        break
            window_end = window_start - 1
            if not batch and window_start == min_seq:
                break

        page_full = len(collected) >= query.limit
        scan_exhausted = scanned >= self._history.max_scan_messages and window_end >= min_seq
        if page_full:
            next_cursor = str(int(collected[-1].seq))
            has_more = int(next_cursor) > min_seq
        elif scan_exhausted:
            next_cursor = str(window_end + 1)
            has_more = True
        else:
            next_cursor = None
            has_more = False

        return {
            "messages": self._messages_to_dicts(query, collected),
            "next_cursor": next_cursor if has_more else None,
            "has_more": has_more,
            "order": "desc",
            "scanned": scanned,
        }

    def _messages_to_dicts(self, query: HistoryQuery, messages: list[Any]) -> list[dict[str, Any]]:
        channel_names = self._load_channel_names(query.principal, query.token, messages)
        return [self._message_to_dict(message, channel_names) for message in messages]

    def _message_to_dict(self, message: Any, channel_names: dict[int, str]) -> dict[str, Any]:
        channel_id = int(message.channel_id)
        return {
            "seq": int(message.seq),
            "ts_ms": int(message.ts_ms),
            "channel_id": channel_id,
            "channel_name": channel_names.get(channel_id),
            "principal": int(message.principal),
            "recipients": [int(item) for item in message.recipients],
            "payload": encode_payload(bytes(message.payload), self._payload),
        }

    def _load_channel_names(self, principal: int, token: str, messages: list[Any]) -> dict[int, str]:
        names: dict[int, str] = {}
        channel_ids = sorted({int(message.channel_id) for message in messages})
        for channel_id in channel_ids:
            try:
                response = self._client.get_channel(principal, token, channel_id)
            except Exception:
                continue
            channel = getattr(response, "channel", None)
            name = getattr(channel, "name", None)
            if name:
                names[channel_id] = str(name)
        return names


def parse_history_query(data: dict[str, Any], history_config: HistoryConfig) -> HistoryQuery:
    principal = _required_uint64(data.get("principal"), "principal")
    token = _required_str(data.get("token"), "token")
    cursor = _optional_uint64(data.get("cursor"), "cursor")
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
