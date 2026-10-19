# codex-search-shim

让 [Codex CLI](https://github.com/openai/codex) 里的 **Gemini 使用 Google 联网搜索**，并在同一个 Codex provider 下按模型分流到不同上游（GPT 原样透传）。

| 工具 | 搜索来源 | 执行方 | 特点 |
| --- | --- | --- | --- |
| `web_search` | Google（经 [Antigravity Tools](https://github.com/lbjlaq/Antigravity-Manager) 的 Gemini `googleSearch`） | 本中间层 | 返回 Gemini 整理的摘要 + 来源链接，约 5–13 秒 |

纯 Python 标准库实现，无第三方依赖。另附一个 MCP 服务器 `mcp_search.py`，可把 Google 搜索作为 MCP 工具提供给 Codex、Cursor 等客户端。

## 为什么需要它

1. **Gemini 用不了 Codex 内置的 `web_search`。** Codex 会在 `/v1/responses` 请求里发送托管工具 `{"type":"web_search"}`。Antigravity 把它转成 Google `googleSearch`，但 Google 的 v1internal 接口不允许 `googleSearch` 和函数工具同时出现（报 400 `Please enable tool_config.include_server_side_tool_invocations`），而 Codex 总会带函数工具。
2. **一个 Codex provider 只有一个 `base_url`。** 想在同一个模型选择器里同时用 GPT（ChatGPT 登录）和 Gemini（Antigravity），需要一个按模型分流的代理；GPT 请求原样转发，不做工具注入或改写。

## 工作原理

```text
Codex ──► codex-search-shim (127.0.0.1:8047) ──┬─► Antigravity Tools (127.0.0.1:8045)   Gemini / Claude 等模型
                                               └─► chatgpt.com/backend-api/codex         GPT 模型（透明转发）
```

- **按模型分流**：模型在 Antigravity 的 `/v1/models` 列表里（或以 `gemini-`、`claude-`、`gpt-oss-` 开头）就发往 Antigravity，其余原样发往 OpenAI。
- **Gemini / Antigravity 请求**：把托管 `web_search` 换成函数工具 `web_search(query)`。模型调用时由中间层执行：单独发一个只带 `googleSearch` 的 Gemini 请求（默认模型 `gemini-3.8-flash-high`），解析 `groundingMetadata` 得到来源。代码模式下若 Codex 未带托管搜索工具，中间层仍会额外注入 Google `web_search`。
- **GPT 请求**：body / headers / 模型名一律原样透明转发（含 `/v1/responses` 与 Codex 原生 `/alpha/search`），不注入工具、不改历史、不强制 `stream`。
- **`/alpha/search` 防护**：若客户端向该路径发送非 `gpt-` 模型请求，中间层直接返回 HTTP 400，不会改写模型名、也不会转发。
- 对 Antigravity 流量，中间层在同一个流式响应里循环（最多 8 轮）：执行搜索 → 向 Codex 发出标准 `web_search_call` 事件（Codex 界面显示 `web search: ...`）→ 把结果作为 `function_call_output` 喂回模型 → 继续生成。
- 如果同一轮模型还调用了 Codex 自己的工具（shell、apply_patch 等），中间层结束本次响应让 Codex 执行；下一次请求里的 `web_search_call` 历史项会被还原为 `function_call` + `function_call_output`，结果来自本地持久缓存 `~/.cache/codex-search-shim/results.json`。

## 安装

前提：

- Python 3.9+；
- Antigravity Tools 已运行并开启反代（默认 `127.0.0.1:8045`），其 API key 位于 `~/.antigravity_tools/gui_config.json` 的 `proxy.api_key`（也可用环境变量 `ANTIGRAVITY_API_KEY` 指定）；
- 若要在同一 provider 下使用 GPT，Codex 需使用 ChatGPT 登录（`~/.codex/auth.json`）。本中间层只透传 Codex 已有请求，不会自行读取该文件去调用搜索。

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
supports_standalone_web_search = true  # 允许 Codex 对 GPT 使用原生独立搜索（由中间层透明转发）
```

### 让 Gemini 出现在模型选择器里

用 `model_catalog_json` 提供一份合并后的模型目录：原有 GPT 条目 + Gemini 条目。Gemini 条目可以和 GPT 用完全相同的运行模式：

- `tool_mode = "code_mode_only"`、`use_responses_lite = true`、`multi_agent_version = "v2"`；
- `supports_reasoning_summaries = true`（Antigravity 会返回推理摘要）；
- `base_instructions` 建议用 GPT 条目的系统指令改写：换掉模型名，把 `commentary` / `final` 频道的说法改成“进度更新 / 最终回复”，并补充本部署的工具说明（Google `web_search`、PDF 截图替代办法、子 agent 用法）。

[`examples/gemini-catalog-entry.json`](examples/gemini-catalog-entry.json) 是一个条目示例。可以用 `codex debug models` 检查生效的目录。

### 工具格式翻译（Gemini 与 GPT 行为一致的关键）

Antigravity 不支持 Codex 的几种工具格式，中间层对发往 Antigravity 的请求做双向翻译：

| Codex 格式 | Antigravity 的问题 | 中间层处理 |
| --- | --- | --- |
| 代码模式的 `additional_tools` 输入项（`exec`、`wait`、多 agent 工具） | 整项被丢弃，模型看不到工具 | 展开成顶层工具 |
| `custom` 自由格式工具（`exec`、`apply_patch`） | 模型无法调用，只输出 `task ready` | 改成只有一个字符串参数 `input` 的函数工具（语法定义附在描述里）；模型调用后还原为 `custom_tool_call` |
| 命名空间工具（`collaboration.spawn_agent` 等） | 不识别 | 展平为 `collaboration__spawn_agent`，调用时还原 `namespace` |
| 多 agent 消息 `agent_message`（子 agent 的任务、回报） | 被丢弃，子 agent 收不到任务 | 转成普通用户消息 |
| 历史里的 `custom_tool_call(_output)` | 不识别 | 转成 `function_call(_output)` |

代码模式下中间层仍会额外注入 Google 的 `web_search`；不会替任何模型去调用 OpenAI 搜索，也不会改写发往 OpenAI 的 GPT 请求。

**跨轮推理摘要**：发往 Antigravity 前，历史里的 `reasoning` 项会被换成 `developer` 角色的短附注（只含 Antigravity 返回的 `summary_text`，最多约 4000 字符）。这样下一轮仍能看到上一轮的结论和思路，但不是 OpenAI 那种完整 encrypted reasoning；若摘要里没写下具体数字/名称，后面也无法「回忆」出来。

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
| `--openai-upstream` | `https://chatgpt.com/backend-api/codex` | GPT 请求的上游（透明转发） |
| `--search-model` | `gemini-3.8-flash-high` | 执行 Google 搜索并总结结果的模型；换成 `gemini-3.8-flash-low` 更快但总结更简略 |
| `--max-rounds` | `8` | 单次响应内最多搜索轮数 |
| `--cache-file` | `~/.cache/codex-search-shim/results.json` | 搜索结果缓存 |
| `--no-google-for-code-mode` | 关 | 不给代码模式的 Gemini 额外注入 Google 的 `web_search` |
| `-v` | 关 | 调试日志 |

## 已知限制与风险

- Google 搜索走 Antigravity 账号，需遵守 Google / Antigravity 的使用条款。
- GPT 流量按 Codex ChatGPT 登录方式原样透明转发到 OpenAI 后端（不注入工具、不改 body）；本中间层不代非 GPT 模型调用 OpenAI 独立搜索，也不会伪造模型名。
- 只支持 HTTP 流式（SSE），不支持 Responses WebSocket。
- 请求体如果被压缩（`Content-Encoding`），中间层无法改写，会原样透传。
- 上下文压缩：Gemini 走 Codex 的本地压缩（普通 `/v1/responses` 摘要请求），已验证可用；GPT 的远程压缩 `/responses/compact` 原样转发给 OpenAI。Gemini 条目的 `context_window` 是 1M，若想更早压缩，可设 `model_auto_compact_token_limit`。
- Gemini 与 GPT 仍有的差异：
  - 跨轮推理不是完整还原：Antigravity 丢弃历史里的 `reasoning` 项，也没有 OpenAI 的 `encrypted_content`；中间层会把各轮 **reasoning summary** 转成 developer 附注喂回模型，便于记住结论和中间判断，但仍是摘要而非完整思维链；
  - `priority` 服务等级只对 OpenAI 有效；
  - GPT 发给子 agent 的消息是 OpenAI 加密的，Gemini 子 agent 读不到 GPT 父 agent 的任务正文（同模型之间不受影响）。
- 在 Codex 0.162.1、Antigravity Tools 4.x 上测试。

## 许可

MIT
