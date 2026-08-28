# openevent-view

[English version](README.md)

`openevent-view` 是 OpenEvent 历史消息查看 Web 服务。它通过 `openevent-sdk`
调用 OpenEvent `Fetch`/`GetStatus`，不直接访问服务端存储。

## 功能

- 内置前端页面：`GET /`
- 历史消息 API：`POST /v1/messages`
- 按倒序显示，最新消息在前
- 前端不暴露 `from_seq` 和 `limit` 输入，分页由 `cursor` 自动维护
- 可选按 `channel_id` 和 `only_my_recipient` 过滤
- 每条消息展示 OpenEvent 顶层字段。不超过 16 KiB 的 payload 展示解析后的 JSON、UTF-8 文本或 base64；更大的 payload 只显示预览，并可在单独页面查看完整内容

## 运行

`openevent-view` 依赖当前 Python 环境中已安装的 `openevent-sdk>=0.6.0`、
`PyYAML` 和 `orjson>=3.10`。

如果当前环境尚未安装 `openevent-sdk`，请从常规包来源把
`openevent-sdk>=0.6.0` 安装到当前 Python 环境后再运行或测试 `openevent-view`。

构建 wheel：

```bash
make build
```

wheel 会生成到：

```text
dist/openevent_view-0.1.0-py3-none-any.whl
```

安装：

```bash
make install
```

开发方式启动：

```bash
PYTHONPATH=src python -m openevent.view --config openevent-view.yaml
```

不传配置时使用默认值，监听 `127.0.0.1:8080`，OpenEvent 目标为 `127.0.0.1:9527`。

也可以覆盖监听地址：

```bash
PYTHONPATH=src python -m openevent.view --host 0.0.0.0 --port 8080
```

安装后可直接使用命令入口：

```bash
openevent-view --config openevent-view.yaml
```

## 配置

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

`cursor` 省略或为 `null` 时返回最新一页；非空时原样传回响应中的 `next_cursor` 对象以加载更早消息。
`only_my_recipient` 省略时为 `false`。每次查询都会先用本次 `principal/token` 调用 `GetStatus`，因此错误凭据返回
`401`，即使游标已经位于历史边界也不会返回空页。凭据只接受 JSON body，不提供带鉴权的 GET 变体。

完整 Payload 使用只读详情接口：

```http
POST /v1/messages/123/payload
Content-Type: application/json

{
  "principal": "10001",
  "token": "tok_xxx"
}
```

详情页地址是 `GET /message?seq=123`。页面始终在新标签中打开，凭据不会进入 URL 或浏览器存储。

```json
{
  "messages": [],
  "next_cursor": null
}
```
