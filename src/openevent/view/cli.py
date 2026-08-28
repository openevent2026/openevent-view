from __future__ import annotations

import argparse
import logging

from .config import ConfigError, ViewConfig, load_config
from .history import HistoryService
from .server import create_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openevent-view")
    parser.add_argument("--config", help="Path to openevent-view YAML config")
    parser.add_argument("--host", help="Override HTTP listen host")
    parser.add_argument("--port", type=int, help="Override HTTP listen port")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    try:
        from openevent.sdk import OpenEventClient
    except ImportError as exc:
        raise SystemExit(
            "failed to import openevent-sdk; install openevent-sdk>=0.6.0 "
            "before starting openevent-view"
        ) from exc

    config = load_config(args.config)
    if args.host is not None or args.port is not None:
        config = _override_server(config, args.host, args.port)

    client = OpenEventClient(
        config.openevent.target,
        timeout=config.openevent.rpc_timeout_seconds,
    )
    history_service = HistoryService(
        client,
        config.history,
        channel_cache_size=config.openevent.channel_cache_size,
        channel_lookup_workers=config.openevent.channel_lookup_workers,
    )
    server = create_server(config, history_service)
    address = f"http://{config.server.host}:{config.server.port}/"
    logging.getLogger(__name__).info("openevent-view listening on %s", address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("shutdown requested")
    finally:
        server.server_close()
        history_service.close()
        client.close()
    return 0


def _override_server(
    config: ViewConfig, host: str | None, port: int | None
) -> ViewConfig:
    from dataclasses import replace

    selected_host = config.server.host if host is None else host
    selected_port = config.server.port if port is None else port
    if not selected_host:
        raise ConfigError("--host must be a non-empty string")
    if selected_port < 1 or selected_port > 65535:
        raise ConfigError("--port must be between 1 and 65535")

    return replace(
        config,
        server=replace(
            config.server,
            host=selected_host,
            port=selected_port,
        ),
    )
