# openevent-view

[中文版](README_cn.md)

`openevent-view` is a web service for viewing OpenEvent historical messages. It
uses `openevent-sdk` to call OpenEvent `Fetch` / `GetStatus` and does not access
server storage directly.

## Features

- Built-in frontend page: `GET /`
- Historical message API: `GET/POST /v1/messages`
- Descending order by default, with newest messages first
- The frontend does not expose `from_seq` or `limit` inputs; pagination is
  maintained automatically with `cursor`
- Optional filtering by `channel_id` and `only_my_recipient`
- Each message shows OpenEvent top-level fields. `payload` is expanded as a JSON
  tree when possible; non-JSON payloads are shown as UTF-8 text or base64

## Run

`openevent-view` depends on `openevent-sdk>=0.4.1` and `PyYAML` being installed
in the current Python environment. The `openevent-sdk/` submodule is included
only for source browsing and API reference; `openevent-view` does not import or
install the SDK from that submodule automatically.

If `openevent-sdk` is missing, install `openevent-sdk>=0.4.1` into the current
Python environment from your normal package source before running or testing
`openevent-view`. Do not install it from this repository's `openevent-sdk/`
submodule; that submodule is only a source reference.

Build the wheel:

```bash
make build
```

The wheel is generated at:

```text
dist/openevent_view-0.1.0-py3-none-any.whl
```

Install:

```bash
make install
```

To specify an install location, pass `pip install` arguments through
`INSTALL_ARGS`:

```bash
make install INSTALL_ARGS="--target /opt/openevent-view"
```

Start in development mode:

```bash
PYTHONPATH=src python -m openevent.view --config openevent-view.yaml
```

Without a configuration file, defaults are used: listen on `127.0.0.1:8080` and
connect to OpenEvent at `127.0.0.1:9527`.

The listen address can also be overridden:

```bash
PYTHONPATH=src python -m openevent.view --host 0.0.0.0 --port 8080
```

After installation, use the command entry point directly:

```bash
openevent-view --config openevent-view.yaml
```

## Configuration

```yaml
version: v1

server:
  host: 127.0.0.1
  port: 8080
  request_timeout_seconds: 10
  max_request_body_bytes: 65536

openevent:
  target: 127.0.0.1:9527
  rpc_timeout_seconds: 10
  channel_cache_size: 4096
  channel_lookup_workers: 8

history:
  default_limit: 100
  max_limit: 1000
  fetch_batch_size: 1000
  default_order: desc

payload:
  parse_json: true
  include_text: true
  text_max_bytes: 65536
```

## API

```http
POST /v1/messages
Content-Type: application/json

{
  "principal": 10001,
  "token": "tok_xxx",
  "cursor": null,
  "order": "desc",
  "channel_id": 10001,
  "only_my_recipient": false
}
```

When `cursor` is empty, the API returns the newest page. A provided cursor must
be a positive integer. Use `next_cursor` from the response to load older
messages.

```json
{
  "messages": [],
  "next_cursor": null
}
```
