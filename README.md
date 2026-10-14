# codex-search-shim

让 [Codex CLI](https://github.com/openai/codex) 里的 **GPT 和 Gemini 两类模型都能同时使用两套联网搜索**：

| 工具 | 搜索来源 | 执行方 | 特点 |
| --- | --- | --- | --- |
| `web_search` | Google（经 [Antigravity Tools](https://github.com/lbjlaq/Antigravity-Manager) 的 Gemini `googleSearch`） | 本中间层 | 返回 Gemini 整理的摘要 + 来源链接，约 5–13 秒 |
| `web_run` | OpenAI 独立搜索接口 `/alpha/search` | Gemini：本中间层；GPT：Codex 原生 | 返回大量原始结果（标题、URL、摘要），可 `open_url` 读网页，约 1–3 秒 |

纯 Python 标准库实现，无第三方依赖。另附一个 MCP 服务器 `mcp_search.py`，可把 Google 搜索作为 MCP 工具提供给 Codex、Cursor 等客户端。

## 为什么需要它

1. **Gemini 用不了 Codex 内置的 `web_search`。** Codex 会在 `/v1/responses` 请求里发送托管工具 `{"type":"web_search"}`。Antigravity 把它转成 Google `googleSearch`，但 Google 的 v1internal 接口不允许 `googleSearch` 和函数工具同时出现（报 400 `Please enable tool_config.include_server_side_tool_invocations`），而 Codex 总会带函数工具。
2. **GPT 用不到 Google。** OpenAI 托管搜索只在 OpenAI 服务端执行，开启 `supports_standalone_web_search` 后 GPT 只有走 OpenAI 的 `web_run`。
3. **一个 Codex provider 只有一个 `base_url`。** 想在同一个模型选择器里同时用 GPT（ChatGPT 登录）和 Gemini（Antigravity），需要一个按模型分流的代理。

## 工作原理

```text
Codex ──► codex-search-shim (127.0.0.1:8047) ──┬─► Antigravity Tools (127.0.0.1:8045)   Gemini / Claude 等模型
                                               └─► chatgpt.com/backend-api/codex         GPT 模型、/alpha/search
```

- **按模型分流**：模型在 Antigravity 的 `/v1/models` 列表里（或以 `gemini-`、`claude-`、`gpt-oss-` 开头）就发往 Antigravity，其余原样发往 OpenAI。
- **Gemini 请求**：把托管 `web_search` 换成两个函数工具 `web_search(query)` 和 `web_run(query | open_url)`。模型调用时由中间层执行：
  - `web_search` → 单独发一个只带 `googleSearch` 的 Gemini 请求（默认模型 `gemini-3.8-flash-low`），解析 `groundingMetadata` 得到来源；
  - `web_run` → 用 Codex 请求里自带的 ChatGPT 凭据（`Authorization`、`chatgpt-account-id`）调用 OpenAI `/alpha/search`。
- **GPT 请求**：额外注入一个函数工具 `web_search(query)`（Google），GPT 原生的 `web_run` 不动，由 Codex 调 `/v1/alpha/search`，中间层原样转发。
- 中间层在同一个流式响应里循环（最多 8 轮）：执行搜索 → 向 Codex 发出标准 `web_search_call` 事件（Codex 界面显示 `web search: ...`）→ 把结果作为 `function_call_output` 喂回模型 → 继续生成。
- 如果同一轮模型还调用了 Codex 自己的工具（shell、apply_patch 等），中间层结束本次响应让 Codex 执行；下一次请求里的 `web_search_call` 历史项会被还原为 `function_call` + `function_call_output`，结果来自本地持久缓存 `~/.cache/codex-search-shim/results.json`。

## 安装

前提：

- Python 3.9+；
- Antigravity Tools 已运行并开启反代（默认 `127.0.0.1:8045`），其 API key 位于 `~/.antigravity_tools/gui_config.json` 的 `proxy.api_key`（也可用环境变量 `ANTIGRAVITY_API_KEY` 指定）；
- Codex 使用 ChatGPT 登录（`~/.codex/auth.json`）。

```bash
git clone https://github.com/JerryBlackoo/codex-search-shim.git ~/Projects/codex-search-shim
python3 ~/Projects/codex-search-shim/shim.py --listen 127.0.0.1:8047 -v
```

### 开机自启（systemd 用户服务）

```bash
mkdir -p ~/.config/systemd/user
sed "s#@DIR@#$HOME/Projects/codex-search-shim#" ~/Projects/codex-search-shim/examples/codex-search-shim.service \
  > ~/.config/systemd/user/codex-search-shim.service
systemctl --user daemon-reload
systemctl --user enable --now codex-search-shim
loginctl enable-linger "$USER"   # 未登录时也运行
```

## 配置 Codex

参考 [`examples/config.toml`](examples/config.toml)，关键是自定义 provider 指向中间层，并沿用 ChatGPT 登录：

```toml
model_provider = "custom"
model_catalog_json = "/home/you/.codex/models_combined.json"

[model_providers.custom]
name = "Custom Node"
base_url = "http://127.0.0.1:8047/v1"
wire_api = "responses"
requires_openai_auth = true
supports_websockets = false          # 中间层只支持 HTTP 流式
supports_standalone_web_search = true  # GPT 使用 web_run（/alpha/search）
```

### 让 Gemini 出现在模型选择器里

用 `model_catalog_json` 提供一份合并后的模型目录：原有 GPT 条目 + Gemini 条目。Gemini 条目必须是**经典模式**，否则 Codex 会把工具放进代码模式（`exec` JS 工具），经 Antigravity 的 Gemini 处理不了，只会输出 `task ready`：

- 删除 `tool_mode`（不要 `code_mode_only`）和 `multi_agent_version`；
- `use_responses_lite = false`。

[`examples/gemini-catalog-entry.json`](examples/gemini-catalog-entry.json) 是一个条目示例。可以用 `codex debug models` 检查生效的目录。

修改配置或目录后：

- CLI：`codex app-server daemon restart`（TUI 的模型列表来自这个常驻进程的缓存）；
- Cursor / VS Code 的 Codex 扩展：执行 `Developer: Reload Window`。

## MCP：把 Google 搜索作为独立工具

`mcp_search.py` 是一个 stdio MCP 服务器，提供 `google_search(query)`。

Codex（`~/.codex/config.toml`）：

```toml
[mcp_servers.antigravity_search]
command = "/usr/bin/python3"
args = ["/home/you/Projects/codex-search-shim/mcp_search.py"]
startup_timeout_sec = 20
tool_timeout_sec = 120
```

Cursor（`~/.cursor/mcp.json`）：

```json
{
  "mcpServers": {
    "antigravity_search": {
      "type": "stdio",
      "command": "/usr/bin/python3",
      "args": ["/home/you/Projects/codex-search-shim/mcp_search.py"]
    }
  }
}
```

环境变量：`ANTIGRAVITY_URL`（默认 `http://127.0.0.1:8045`）、`ANTIGRAVITY_API_KEY`、`ANTIGRAVITY_SEARCH_MODEL`。

## 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--listen` | `127.0.0.1:8047` | 监听地址 |
| `--upstream` | `http://127.0.0.1:8045` | Antigravity Tools 地址 |
| `--openai-upstream` | `https://chatgpt.com/backend-api/codex` | GPT 请求的上游 |
| `--search-model` | `gemini-3.8-flash-low` | 执行 Google 搜索用的模型 |
| `--max-rounds` | `8` | 单次响应内最多搜索轮数 |
| `--cache-file` | `~/.cache/codex-search-shim/results.json` | 搜索结果缓存 |
| `--no-web-run` | 关 | 不给 Gemini 提供 OpenAI 的 `web_run` |
| `--no-google-for-openai` | 关 | 不给 GPT 注入 Google 的 `web_search` |
| `--google-for-web-run` | 关 | 把 Google 结果也合并进 GPT `web_run` 的返回（每次搜索多等 5–10 秒） |
| `-v` | 关 | 调试日志 |

## 已知限制与风险

- `/alpha/search` 是 OpenAI 未公开的内部接口，可能随时变化或失效；`web_run` 会消耗 ChatGPT 账号的搜索额度。
- Google 搜索走 Antigravity 账号，需遵守 Google / Antigravity 的使用条款；风险主要在这一侧。
- 只支持 HTTP 流式（SSE），不支持 Responses WebSocket。
- 请求体如果被压缩（`Content-Encoding`），中间层无法改写，会原样透传。
- 上下文压缩：Gemini 走 Codex 的本地压缩（普通 `/v1/responses` 摘要请求），已验证可用；GPT 的远程压缩 `/responses/compact` 原样转发给 OpenAI。Gemini 条目的 `context_window` 是 1M，若想更早压缩，可设 `model_auto_compact_token_limit`。
- 在 Codex 0.162.1、Antigravity Tools 4.x 上测试。

## 许可

MIT
