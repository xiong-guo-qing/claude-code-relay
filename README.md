# Claude Code Relay

一个 **Anthropic Messages API → OpenAI Chat Completions API** 的轻量中转服务。  
主要用途是：**让 Claude Code CLI 使用 OpenAI-compatible 后端模型**。

当前这版已经具备：

- Claude 风格接口
- OpenAI-compatible 上游转发
- SSE 流式输出
- `tool_use / tool_result` 基础兼容
- 图片透传（支持多图）
- 协议调试模式

---

## 1. 功能概览

本服务对外提供一个最小可用的 Anthropic 风格接口：

- `GET /health`
- `GET /v1/models`
- `POST /v1/messages`

然后把请求转换成 OpenAI-compatible 的：

- `/chat/completions`

---

## 2. 当前能力

### 已支持

- Anthropic `messages` 请求转 OpenAI `chat/completions`
- 文本对话
- SSE 流式返回
- Claude 风格事件流：
  - `message_start`
  - `content_block_start`
  - `content_block_delta`
  - `content_block_stop`
  - `message_delta`
  - `message_stop`
- `tool_use` 流式透传
- `input_json_delta` 输出
- 图片透传：
  - 支持 base64 图片
  - 支持 url 图片
  - 支持多图
  - 支持图文混合顺序保持
- 协议调试日志

### 当前设计原则

- relay 只做**最小必要协议转换**
- 不对 tool result 过度加工
- 不替大模型做额外总结
- 尽量不污染大模型上下文

---

## 3. 目录结构

```bash
claude-code-relay/
├── README.md
├── server.py
├── relayctl.py
├── run.sh
├── config.json
└── config.local.json   # 本地真实配置（默认不提交）
```

---

## 4. 配置说明

### `config.json`
仓库中的 `config.json` 是**模板配置**，适合提交到 GitHub。  
其中上游 API key 使用占位符，例如：

```json
"api_key": "YOUR_PROVIDER_API_KEY_HERE"
```

### `config.local.json`
本地实际运行时，推荐使用 `config.local.json` 保存真实密钥。  
`server.py` 会优先读取：

1. `config.local.json`
2. 如果不存在，再回退到 `config.json`

这样可以避免把真实密钥提交到仓库。

---

## 5. 配置示例

```json
{
  "server": {
    "host": "127.0.0.1",
    "port": 8080
  },
  "auth": {
    "api_key": "relay-local-dev"
  },
  "provider": {
    "base_url": "http://127.0.0.1:8090/v1",
    "api_key": "YOUR_PROVIDER_API_KEY_HERE",
    "chat_completions_path": "/chat/completions",
    "timeout_seconds": 300
  },
  "model_map": {
    "claude-sonnet-4-5": "gpt-5.4",
    "claude-3-7-sonnet": "gpt-5.4",
    "claude-opus-4-1": "gpt-5.4",
    "claude-opus-4": "gpt-5.4",
    "claude-3-5-sonnet": "gpt-5.4",
    "claude-3-5-haiku": "gpt-5.4",
    "default": "gpt-5.4"
  },
  "default_target_model": "gpt-5.4",
  "protocol_debug": true
}
```

---

## 6. 启动方式

### 方式一：直接运行

```bash
cd /home/xgq/claude-code-relay
python3 server.py
```

默认监听：

```bash
127.0.0.1:8080
```

### 方式二：使用 systemd user service

如果你的环境里已经配置了用户级 systemd 服务：

```bash
systemctl --user restart claude-code-relay.service
systemctl --user status claude-code-relay.service
```

---

## 7. 健康检查

```bash
curl http://127.0.0.1:8080/health
```

正常返回示例：

```json
{
  "ok": true,
  "service": "claude-code-relay",
  "provider": "openai-compatible"
}
```

---

## 8. 模型映射管理

relay 对外暴露哪些模型名，取决于：

- `config.json` / `config.local.json` 中的 `model_map`

可以使用 `relayctl.py` 快速管理。

### 添加映射

```bash
cd /home/xgq/claude-code-relay
python3 relayctl.py add claude-sonnet-4-6 gpt-5.4
```

### 删除映射

```bash
python3 relayctl.py del claude-opus-4
```

### 查看映射

```bash
python3 relayctl.py list
```

---

## 9. 接口说明

### `GET /health`
用于健康检查。

### `GET /v1/models`
返回 relay 当前暴露的模型列表。  
这些模型 ID 由 `model_map` 的 key 派生而来（不含 `default`）。

### `POST /v1/messages`
Anthropic 风格的消息接口。

需要携带 relay 自身的 API key，可使用以下任一请求头：

- `x-api-key`
- `anthropic-api-key`
- `Authorization: Bearer ...`

---

## 10. 协议调试模式

可以在配置中开启：

```json
"protocol_debug": true
```

开启后，日志中会输出：

- 入站 Anthropic 请求摘要
- 出站 OpenAI payload 摘要
- SSE 事件发射顺序
- 工具调用流式参数信息

日志前缀示例：

```text
[relay-protocol]
```

适合排查：

- tool_use 不生效
- input_json_delta 不完整
- 图片透传异常
- SSE 顺序不兼容

---

## 11. 图片透传说明

支持将 Anthropic 风格图片内容转换为 OpenAI 风格 `image_url`：

### 支持类型

- `source.type = base64`
- `source.type = url`

### 转换方式

Anthropic：

```json
{
  "type": "image",
  "source": {
    "type": "base64",
    "media_type": "image/png",
    "data": "..."
  }
}
```

转换后会变成 OpenAI 风格：

```json
{
  "type": "image_url",
  "image_url": {
    "url": "data:image/png;base64,..."
  }
}
```

### 特性

- 支持多图输入
- 支持图文混合
- 保持原始顺序

---

## 12. 工具调用兼容说明

当前版本已经支持基础工具调用链路：

- 模型发起 `tool_use`
- relay 转换并流式返回
- 使用 `input_json_delta` 发送工具参数
- 工具结果继续进入后续推理

已经验证可工作的场景包括：

- `WebSearch`
- `WebFetch`

---

## 13. 安全建议

### 不要把真实密钥写进仓库版 `config.json`
推荐做法：

- `config.json`：模板配置
- `config.local.json`：本地真实配置
- `.gitignore` 忽略 `config.local.json`

### 当前 `.gitignore` 建议忽略

- `config.local.json`
- `*.log`
- `__pycache__/`
- `server.py.bak.*`
- `server.py.before_revert_*`

---

## 14. 常见问题

### Q1：为什么我改了配置但没有生效？
先确认：

- 你改的是 `config.local.json` 还是 `config.json`
- `server.py` 是否已经重启
- 当前日志里是否有报错

### Q2：为什么 GitHub 里没有真实 API key？
这是故意的。  
真实 key 应该只放在本地 `config.local.json`，避免泄露。

### Q3：为什么要加 protocol debug？
因为 Claude Code / Anthropic Messages API 的兼容问题，很多时候出在：

- SSE 事件顺序
- tool_use 增量参数
- content block 组织方式
- 多模态 part 转换

协议日志能极大提升排查效率。

---

## 15. 当前仓库目标

这个仓库的目标不是做一个“超重型代理层”，而是：

> **做一个尽量干净、最小必要、协议兼容优先的 Claude Code relay**

重点是：

- 修协议
- 保持轻量
- 不乱补内容
- 不污染上下文

---

## 16. 后续可继续优化的方向

- 更细的 tool_result 兼容
- 更完整的流式工具调用边界处理
- 多模态异常输入保护
- 更详细的协议调试分级（摘要 / verbose）
- 更多 Anthropic 事件类型兼容

---

## 17. 仓库地址

```bash
https://github.com/xiong-guo-qing/claude-code-relay
```

---

如果你准备把它接到 Claude Code CLI，建议先做三件事：

1. 配好 `config.local.json`
2. 确认 `/health` 正常
3. 先测试：文本、工具、图片 三类场景
