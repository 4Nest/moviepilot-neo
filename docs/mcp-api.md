# MCP 接入说明

NEO 内置 MCP（Model Context Protocol）服务端，外部 Agent / MCP 客户端可通过标准的
JSON-RPC 2.0 over Streamable HTTP 调用媒体搜索、订阅与下载能力。

## 连接与认证

| 配置项 | 值 |
| --- | --- |
| 服务地址 | `https://<后端地址>/api/v1/mcp`（反向代理有前缀时需一并保留） |
| 传输方式 | Streamable HTTP，POST JSON-RPC 2.0 消息 |
| 认证请求头 | `X-API-KEY: <API_TOKEN>` |
| 请求类型 | `Content-Type: application/json` |
| 协议版本 | `2025-11-25`、`2025-06-18`、`2024-11-05`，服务端按客户端请求协商 |

支持的方法：`initialize`、`notifications/initialized`、`ping`、`tools/list`、`tools/call`；
`DELETE /api/v1/mcp` 终止会话（无状态实现，返回 204）。

## 工具清单

| 工具 | 参数 | 说明 |
| --- | --- | --- |
| `media.search` | `title`（必填）、`source` | 按标题搜索媒体，返回 TMDB/豆瓣/Bangumi 等 ID |
| `subscribe.list` | `state`（R/P/S） | 订阅列表 |
| `subscribe.add` | `title`（必填）、`year`、`type`、`season`、`tmdbid`、`media_source`、`media_id` | 识别媒体并添加订阅 |
| `subscribe.delete` | `id`（必填） | 删除订阅并广播 `SubscribeDeleted` 事件 |
| `download.list` | `name` | 正在下载的任务列表 |
| `download.control` | `hash`、`action`（start/stop/delete） | 控制下载任务 |
| `system.status` | 无 | 版本、订阅数、下载中任务数 |

工具结果以 JSON 文本形式放在 `result.content[].text`；参数错误返回 JSON-RPC `-32602`，
业务执行异常返回 `isError: true` 的文本内容。

## 调用示例

```bash
curl -s http://127.0.0.1:3001/api/v1/mcp \
  -H "X-API-KEY: $API_TOKEN" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"system.status","arguments":{}}}'
```

## REST 便捷入口

调试或脚本场景也可使用等价 REST 接口（同样使用 `X-API-KEY` 认证）：

- `GET /api/v1/mcp/tools`：工具列表
- `GET /api/v1/mcp/tools/{name}`：工具详情
- `POST /api/v1/mcp/tools/call`：调用工具，请求体 `{"tool_name": "...", "arguments": {...}}`