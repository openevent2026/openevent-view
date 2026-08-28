from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    request_timeout_seconds: float = 10.0
    max_request_body_bytes: int = 65536


@dataclass(frozen=True)
class OpenEventConfig:
    target: str = "127.0.0.1:9527"
    rpc_timeout_seconds: float = 10.0
    channel_cache_size: int = 4096
    channel_lookup_workers: int = 8


@dataclass(frozen=True)
class HistoryConfig:
    default_limit: int = 100
    max_limit: int = 1000
    fetch_batch_size: int = 1000


@dataclass(frozen=True)
class ViewConfig:
    version: str
    server: ServerConfig
    openevent: OpenEventConfig
    history: HistoryConfig


def load_config(path: str | Path | None = None) -> ViewConfig:
    if path is None:
        return parse_config({})
    with Path(path).open("r", encoding="utf-8") as file:
        raw = yaml.safe_load(file)
    return parse_config(raw or {})


def parse_config(raw: Any) -> ViewConfig:
    data = _object(raw, "config")
    version = _non_empty_string(data.get("version", "v1"), "version")
    if version != "v1":
        raise ConfigError("version must be v1")

    server_data = _object(data.get("server", {}), "server")
    openevent_data = _object(data.get("openevent", {}), "openevent")
    history_data = _object(data.get("history", {}), "history")

    history = HistoryConfig(
        default_limit=_positive_int(
            history_data.get("default_limit", HistoryConfig.default_limit),
            "history.default_limit",
        ),
        max_limit=_positive_int(
            history_data.get("max_limit", HistoryConfig.max_limit),
            "history.max_limit",
        ),
        fetch_batch_size=_positive_int(
            history_data.get("fetch_batch_size", HistoryConfig.fetch_batch_size),
            "history.fetch_batch_size",
        ),
    )
    if history.default_limit > history.max_limit:
        raise ConfigError("history.default_limit must be <= history.max_limit")
    if history.max_limit > 1000:
        raise ConfigError("history.max_limit must be <= 1000")
    if history.fetch_batch_size > 1000:
        raise ConfigError("history.fetch_batch_size must be <= 1000")

    workers = _positive_int(
        openevent_data.get(
            "channel_lookup_workers", OpenEventConfig.channel_lookup_workers
        ),
        "openevent.channel_lookup_workers",
    )
    if workers > 64:
        raise ConfigError("openevent.channel_lookup_workers must be <= 64")

    return ViewConfig(
        version=version,
        server=ServerConfig(
            host=_non_empty_string(
                server_data.get("host", ServerConfig.host), "server.host"
            ),
            port=_port(server_data.get("port", ServerConfig.port), "server.port"),
            request_timeout_seconds=_positive_number(
                server_data.get(
                    "request_timeout_seconds", ServerConfig.request_timeout_seconds
                ),
                "server.request_timeout_seconds",
            ),
            max_request_body_bytes=_positive_int(
                server_data.get(
                    "max_request_body_bytes", ServerConfig.max_request_body_bytes
                ),
                "server.max_request_body_bytes",
            ),
        ),
        openevent=OpenEventConfig(
            target=_non_empty_string(
                openevent_data.get("target", OpenEventConfig.target),
                "openevent.target",
            ),
            rpc_timeout_seconds=_positive_number(
                openevent_data.get(
                    "rpc_timeout_seconds", OpenEventConfig.rpc_timeout_seconds
                ),
                "openevent.rpc_timeout_seconds",
            ),
            channel_cache_size=_positive_int(
                openevent_data.get(
                    "channel_cache_size", OpenEventConfig.channel_cache_size
                ),
                "openevent.channel_cache_size",
            ),
            channel_lookup_workers=workers,
        ),
        history=history,
    )


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{field} must be an object")
    return value


def _non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{field} must be a non-empty string")
    return value


def _positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{field} must be a positive integer")
    return value


def _positive_number(value: Any, field: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ConfigError(f"{field} must be a positive number")
    return float(value)


def _port(value: Any, field: str) -> int:
    port = _positive_int(value, field)
    if port > 65535:
        raise ConfigError(f"{field} must be between 1 and 65535")
    return port
