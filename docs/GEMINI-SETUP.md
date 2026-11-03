# Gemini CLI 在本机的配置记录

2026-11-03 配好并验证通过。以后要重装或换机器，照这份抄。

## 一条命令验证

```powershell
cd "D:\AI study"
gemini -p "只回答三个字：接通了。不要调用任何工具。"
```

看到 `接通了。` 就是好的。不用手动设任何环境变量，也不用 `--model`。

## 装了什么

```
npm i -g @google/gemini-cli     # 0.66.0
```

命令落在 `C:\Users\13304\AppData\Roaming\npm\gemini.ps1`（`gemini.cmd` 同名）。

## 用户级环境变量（已 setx，永久）

| 变量 | 值 | 为什么 |
|---|---|---|
| `GEMINI_API_KEY` | `AQ.Ab8RN6…zVNw` | 主人从 AI Studio 领的 key |
| `GEMINI_MODEL` | `gemini-3-flash-preview` | 见下面「坑 2」 |
| `GEMINI_CLI_TRUST_WORKSPACE` | `true` | 免去每次的信任目录交互 |
| `HTTPS_PROXY` / `HTTP_PROXY` | `http://127.0.0.1:7897` | Google 直连不通 |
| `NO_PROXY` | `localhost,127.0.0.1,::1` | 别把本地请求也塞进代理 |
| `NODE_USE_ENV_PROXY` | `1` | **Node 24 的 fetch 默认不读 `*_PROXY`**，必须开这个开关 |

另外 `C:\Users\13304\.gemini\settings.json` 固定了模型与认证方式：

```json
{
  "selectedAuthType": "gemini-api-key",
  "model": { "name": "gemini-3-flash-preview" }
}
```

## 踩过的四个坑

### 坑 1 · `AQ.` 开头的 key 不是老式 API key

正规老格式是 `AIza` 开头。主人这个 `AQ.Ab8RN6…` 是 **Vertex AI express key**，
直接拿去打 `generativelanguage.googleapis.com` 会得到：

```
401 ACCESS_TOKEN_TYPE_UNSUPPORTED
```

它能用 —— 但要先把项目里的 **Agent Platform API（`aiplatform.googleapis.com`）启用**
（本项目：`589506483124`）。启用后 `generativelanguage` 这条线就认它了。

> 走 `aiplatform.googleapis.com` 那条路要 project 开 billing，绑卡才行；
> 走 `generativelanguage.googleapis.com` 有免费层，不用绑卡。**用后者。**

### 坑 2 · `gemini-2.5-flash` 对新用户已停

```
404 This model models/gemini-2.5-flash is no longer available to new users.
```

`GET /v1beta/models` 里它还列着 50 个模型，但生成接口对老模型关了对新用户的门。
实测能用的：`gemini-3-flash-preview`（免费层）、`gemini-3.1-pro-preview`（要配额）。
`gemini-flash-latest` 会 503（高负载），`gemini-2.5-flash-lite` 404。

### 坑 3 · Node 24 的 fetch 不认代理

表现是 `TypeError: fetch failed sending request`，重试两次就放弃。
Node 24 需要 `NODE_USE_ENV_PROXY=1` 才会读 `HTTPS_PROXY`。（Node 18/20 行为不同。）

### 坑 4 · 信任目录

非交互模式下会直接退出 55：

```
Gemini CLI is not running in a trusted directory.
```

`GEMINI_CLI_TRUST_WORKSPACE=true` 或 `--skip-trust`。

## 免费层的现实：5 次/分钟

```
429 Quota exceeded for metric: generate_content_free_tier_requests, limit: 5, model: gemini-3-flash
Please retry in 45.892108051s.
```

一次「读代码 + 改 + 跑测试」的任务，CLI 内部要发好几次请求（路由、工具调用、总结），
**几乎必然撞限**，撞了就原地等 46 秒。这对交互式探索挺磨人，对「想清楚了一次性给一条指令」
反而合适 —— 所以用它的正确姿势是：

- 一次给一个完整的小目标，别让它自己试探；
- 让它把方案写进文件再执行，减少来回；
- 重活仍走 CLI 的 `python -m app.cli` / `app.serve`，那是本地算力，不烧配额。

想跑得更顺就得给项目开 billing（Vertex 那条路已经打通，只差绑卡）。

## 本会话内怎么调用（给 DSH 里的自动化用）

DSH 启动的 pwsh 子进程继承的是 DSH 进程启动时的旧环境，读不到刚 `setx` 的值，
所以每次要这样注入一遍：

```powershell
foreach ($n in 'GEMINI_API_KEY','GEMINI_MODEL','GEMINI_CLI_TRUST_WORKSPACE','HTTPS_PROXY','HTTP_PROXY','NODE_USE_ENV_PROXY') {
  Set-Item -Path "Env:$n" -Value ([Environment]::GetEnvironmentVariable($n,'User'))
}
gemini -p "..."
```

主人自己新开的 PowerShell 窗口不用这一步，环境变量天然就在。
