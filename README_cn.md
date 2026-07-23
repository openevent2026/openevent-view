# openevent-view

[English version](README.md)

`openevent-view` 是 OpenEvent 历史消息查看 Web 服务。它通过 `openevent-sdk`
调用 OpenEvent `Fetch`/`GetStatus`，不直接访问服务端存储。

## 功能

- 内置前端页面：`GET /`
- 历史消息 API：`GET/POST /v1/messages`
- 默认倒序显示，最新消息在前
- 前端不暴露 `from_seq` 和 `limit` 输入，分页由 `cursor` 自动维护
- 可选按 `channel_id` 和 `only_my_recipient` 过滤
- 每条消息展示 OpenEvent 顶层字段，并将 `payload` 按 JSON 树展开；非 JSON payload 按 UTF-8 文本或 base64 显示

## 运行

`openevent-view` 依赖当前 Python 环境中已安装的 `openevent-sdk>=0.4.1` 和
`PyYAML`。仓库中的 `openevent-sdk/` 子模块只用于查看源码和 API 参考；
`openevent-view` 不会从该子模块自动导入或安装 SDK。

如果当前环境尚未安装 `openevent-sdk`，请从常规包来源把
`openevent-sdk>=0.4.1` 安装到当前 Python 环境后再运行或测试
`openevent-view`。不要从本仓库的 `openevent-sdk/` 子模块安装；该子模块只作为源码参考。

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

需要指定安装路径时，通过 `INSTALL_ARGS` 传递 `pip install` 参数：

```bash
make install INSTALL_ARGS="--target /opt/openevent-view"
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

`cursor` 为空时返回最新一页；非空时必须为正整数。响应中的 `next_cursor` 可用于加载更早消息。

```json
{
  "messages": [],
  "next_cursor": null
}
```
