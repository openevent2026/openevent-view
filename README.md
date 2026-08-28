# openevent-view

[中文版](README_cn.md)

`openevent-view` is a web service for viewing OpenEvent historical messages. It
uses `openevent-sdk` to call OpenEvent `Fetch` / `GetStatus` and does not access
server storage directly.

## Features

- Built-in frontend page: `GET /`
- Historical message API: `POST /v1/messages`
- Descending messages, with newest messages first
- The frontend does not expose `from_seq` or `limit` inputs; pagination is
  maintained automatically with `cursor`
- Optional filtering by `channel_id` and `only_my_recipient`
- Each message shows OpenEvent top-level fields. Payloads up to 16 KiB are
  expanded as parsed JSON, UTF-8 text, or base64; larger payloads show a preview
  and can be opened in a separate full-content page

## Run

`openevent-view` depends on `openevent-sdk>=0.6.0`, `PyYAML`, and
`orjson>=3.10` being installed in the current Python environment.

If `openevent-sdk` is missing, install `openevent-sdk>=0.6.0` from your normal
package source before running or testing `openevent-view`.

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
```

## API

```http
POST /v1/messages
Content-Type: application/json

{
  "principal": "10001",
  "token": "tok_xxx",
  "cursor": null,
  "channel_id": "10001",
  "only_my_recipient": false
}
```

When `cursor` is omitted or `null`, the API returns the newest page. Otherwise,
pass the returned `next_cursor` object unchanged to load older messages.
Omitting `only_my_recipient` defaults it to `false`. Every request first calls
`GetStatus` with its `principal/token`, so invalid credentials return `401` even
when the cursor is already at the history boundary. Credentials are accepted
only in the JSON body; there is no authenticated GET variant.

Use the read-only detail endpoint for a complete payload:

```http
POST /v1/messages/123/payload
Content-Type: application/json

{
  "principal": "10001",
  "token": "tok_xxx"
}
```

Its page is `GET /message?seq=123`. The page always opens in a new tab, and
credentials are never placed in the URL or browser storage.

```json
{
  "messages": [],
  "next_cursor": null
}
```
