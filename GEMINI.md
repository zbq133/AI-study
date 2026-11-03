# GEMINI.md

> 这个项目由一位人类 + 多个模型轮值开发，代码被 **216 条测试**（+3 条默认跳过的真模型回归）锁着。
> **接手的第一个动作：读完本文件，再读 `docs/ROADMAP.md`，再动代码。**
>
> 本文件是**纪律 + 现状 + 下一棒**；`docs/ROADMAP.md` 是完整交接文档（架构 / 踩过的坑 /
> 硬约束 / 完整计划 / 设计建议 / 已知问题）；`docs/DECISIONS.md` 记着每个决定的来龙去脉。
> 三份一起改，别只改一份。

---

## 一句话项目

把大学课件 PDF（教材章节 / 论文 / PPT 导出的图片课件）变成两份东西：**讲义**（不复述原文，
讲清为什么，每句带出处）和**习题**（答案经打乱选项重解验证，出处必须能在原文逐字找到）。

卖点不是"AI 会讲"，是**"AI 讲的每句话你都能点回去看原文"**。

---

## 现在到哪了（2026-11-03，HEAD = `1acdcea`）

| 项 | 数字 |
|---|---|
| 代码 | `app/` 12 个模块 **2164 行**；`app/web/` 4 个文件 **1195 行** + 本地 KaTeX 548 KB |
| 测试 | **213 passed + 3 skipped**（共 216 条，`tests/` 13 个文件） |
| HTTP 端点 | **16 个**（上传 / 解析 / 布局 / 生成 / 取消 / 任务 / SSE / 历史 / 设置 / 探活） |
| 提交数 | 34 |
| 完成度 | 单人本机可用 **约 92%**；交给别人长期跑 **约 75%** |

**能做的事已经比"能用"多了**：三栏界面（目录 / 讲义 / 原文）、点出处高亮原文、习题先做后判、
公式走本地 KaTeX、历史记录落 `out/index.json`、生成可取消、成本有闸、错误分门别类、
设置界面（模型 / 思考等级 / 深色浅色自定义 / 字号 / 版本，四个标签）、原文栏可折叠、探活 + 日志 + 一键启动。

**最近一次全链路真跑**（15 页双栏综述 `12. Summary and recommendations`）：

```
解析  4.2 秒 → 680 块 / 23 章（原生文字层，没走 OCR）
生成 46.1 秒 → 11 次调用 / 输入 18962 / 输出 6938 tokens
结果  5 节讲义 · 8 条"能…"学习目标 · 2 题全过独立重解验证
出处  36 条 source_refs 全部带 blocks + bbox（缺的 0 条）
```

---

## 三条命令

```powershell
cd "D:\AI study"
.\.venv\Scripts\python.exe -m pytest -q      # 必须 213 passed, 3 skipped
.\.venv\Scripts\python.exe -m app.serve      # → http://127.0.0.1:8765（或直接跑 start.ps1）
.\.venv\Scripts\python.exe -m app.cli "C:\path\to.pdf" 18 3   # 命令行跑一章，3 道题
```

`STUDY_LIVE=1` 才跑那 3 条真模型回归（会真花钱，一分钟内）。

---

## 铁律（违反任何一条，改动会被退回）

1. **改前先提交。** 任何改动开始前先 `git commit`，保留可回滚点。
2. **改后必测。** 新逻辑配新测试；交付前 `pytest` 必须全绿。
3. **做变异验证。** 故意把新写的判据改坏一条，确认对应测试变红。
   测试全绿但你把它改坏了还绿 —— 那是空断言，等于没测。
   ```
   string replace 源码 → 跑 pytest → 必须非 0 → write_bytes 还原原文件
   ```
   替换串**必须唯一**（先 `assert text.count(old) == 1`）：上一轮就因为同一行在文件里出现两次，
   `replace(..., 1)` 只改到一处，变异"通过"了，其实什么都没验证。
4. **提交信息用中文**，前缀 `feat:` / `fix:` / `test:` / `chore:` / `docs:`。
5. **改文件用 `edit` 工具**，别用 PowerShell 的 `Set-Content` / `Add-Content` / `Select-String -Replace`：
   它们会把 LF 变 CRLF 并加 BOM，`git diff` 直接整文件翻红。万不得已按 bytes 读写：
   ```python
   p.write_bytes(p.read_bytes().replace(b"\r\n", b"\n"))
   ```
6. **别为了出题数量放松校验。** 题少是对的，题假才是坏的。
7. **`.env`、`out/`、`settings.json` 不进仓库。**

---

## 先读这些（按顺序）

| # | 文件 | 看什么 |
|---|---|---|
| 1 | `docs/ROADMAP.md` | 主交接文档：§3「已经踩平的地」、§4「硬约束」、§9「已知问题」、**§11「接下来做什么」** |
| 2 | `docs/DECISIONS.md` | 每个决定的来龙去脉（为什么段落要合并、为什么出题要喂原文、为什么 .hide 缺了一下午） |
| 3 | `app/schema.py` | 数据模型 + 出处校验（`SourceRef.blocks/bbox`、`locate_quote`） |
| 4 | `app/serve.py` | 16 个端点 + 任务/取消/成本闸/设置，一个文件看完全部外壳 |
| 5 | `tests/` 里的测试名 | 216 条测试名就是"哪些行为被锁住了"的清单 |

---

## 架构地图（行数即体量）

```
app/
  pdf.py      399  extract_ir：文字层 → 行 → 分栏排序(_order) → 并段落(_paragraphs) → 判标题
                   · 页眉页脚靠 _running_lines 跨页重复检测摘掉
                   · 图片型页面走 ocr.py；OCR 页不并段落、整本并成一章
  ocr.py       87  RapidOCR 懒加载，200 dpi，结果缓存到 out/ocr/<doc_id>/p<N>.json
  schema.py   257  Block/DocIR/Section/Lesson…；build_sections 切章、verify_refs 校验出处
                   · locate_quote + _merge_bbox 把 quote 映射到块 id 与矩形
  pipeline.py 257  全流程编排：plan_outline → write_section → make_quiz → build_lesson
                   · 出题必须喂原文（_render_sources），无出处/出处对不上直接丢
                   · _solve_independently 打乱选项再解一次
  prompts.py   60  三段 system prompt（大纲 / 讲义 / 出题+验证）
  llm.py      106  带 JSON 校验重试、错误分类、成本闸(max_calls/max_tokens)、取消、思考等级降级
  errors.py   135  StudyError 家族：kind/title/hint，人看得懂的那种
  settings.py 109  设置优先级 settings.json > 环境变量；永不回明文密钥
  store.py     94  out/index.json 索引：原子写、坏索引当空、file 只取文件名防穿越
  serve.py    586  FastAPI：17 端点、线程池任务、SSE 进度、日志、探活
  desktop.py  200  客户端外壳：后台起 uvicorn + pywebview 原生窗口；端口复用/让让位
                   · _ensure_streams()：窗口版 exe 没有控制台，sys.stdout 是 None
  cli.py       73  命令行入口
app/web/
  index.html  149 / app.css 234 / app.js 756 / grade.js 56（纯判分函数，node 可直接测）
  vendor/katex/    本地 KaTeX 0.19.1（23 个文件 548 KB，离线可用）
tests/       3086  213 passed + 3 skipped
```

数据流：

```
PDF ─extract_ir→ DocIR ─build_sections→ [Section] ─select_chapter→ 本章
    ─plan_outline→ 大纲 ─write_section→ 讲义(带 source_refs)
    ─make_quiz→ 题 ─verify_refs + 打乱重解→ 留下的题 ─build_lesson→ Lesson JSON → out/
```

---

## 已经做过的（**别再做一遍**）

| 功能 | 落点 | commit |
|---|---|---|
| 出处带块 id + bbox | `schema.py` `verify_refs` | e1e28a7 |
| 界面拆成 `app/web/` 静态文件 | `serve.py` `/static/{name:path}` | b9c4af3 |
| 三栏 + 点出处高亮原文 | `/api/docs/{id}/layout` + `app.js` | 59c8465 |
| 公式接本地 KaTeX | `app/web/vendor/katex` | 5bc8dc1 |
| 习题先做后判 | `app/web/grade.js` + `renderQuiz` | 5bc8dc1 |
| 错误分类（kind/title/hint） | `app/errors.py` | 8200211 |
| 取消 + 成本闸 | `LLM._check` + `/cancel` | 3bd7c3a |
| 历史索引 | `app/store.py` + `out/index.json` | 06bc6fe |
| 探活 / 日志 / 启动脚本 | `/api/health`、`out/server.log`、`start.ps1` | a8105aa |
| 真模型回归 | `tests/test_live.py`（`STUDY_LIVE=1`） | dcb7735 |
| 设置界面（模型/思考等级） | `app/settings.py`、`/api/settings` | b062faf |
| 颜色栏 + 原文折叠 | 色板变量 + `localStorage` | cea7786 / 710a77c |
| `.hide` 规则补上 | `app.css` | 2442ed5 |
| 浅色对比度 / 按钮 / 模型下拉 | 角色变量 + `<select>` | 1acdcea |

---

## 交接第一棒（按 ROADMAP §11 的顺序，别跳）

### ① 出题阶段要有进度（约 2 小时）

讲义写完、进度停在 82%，然后**三十秒不动** —— 用户会以为卡死。
`build_lesson` 已有 `on_step`，是 `make_quiz` 没接。

```python
# app/pipeline.py
def make_quiz(llm, lessons, n, page_text, blocks=None, source="", on_step=None):
    ...
    for i, q in enumerate(pack.items, 1):
        if on_step is not None:
            on_step(stage="verify", index=i, total=len(pack.items))
```

`app/serve.py` 的 `_do_lesson` 里把 82% → 95% 摊开。测试：`FakeLLM` 收 `on_step` 调用序列。

### ② 成本折成钱（约 2 小时）

`store.py` 已经记 `prompt_tokens` / `completion_tokens`。加一张价格表（每百万 token，
**从环境变量可覆盖**，别写死在 JS 里），在生成结果和 `/api/lessons/{id}` 里各显示
一行「这次大约 ¥0.03」。价格缺失就只显示 token，不要猜。

### ③ `reasoning_note` 要显示（约 1 小时）

模型不认 `reasoning_effort` 时，`LLM._create` 会静默重发并置 `reasoning_note`，
但 `serve.py` 没把它带进 `job.result`。用户选了「高」却毫无变化 = 以为坏了。

### 之后是 ROADMAP §11 的第二、三优先

细粒度 bbox（会碰数据模型，改完更新 `tests/test_schema.py` 的 bbox 断言）→
PPT quote 太短 → 公式 OCR。**§11 里"不建议现在做"的三条别碰。**

---

## 本机运行环境（别改）

- Python **3.12**（`.\.venv`）。3.15 装不上 onnxruntime，旧的 3.15 环境留在 `.venv-315` 备查，别删也别用。
- 服务默认 `127.0.0.1:8765`。**改完 Python 要重启服务**；改 `app/web/` 下的静态文件只要刷新
  （每个响应都带 `cache-control: no-cache`）。起服务前先看端口有没有被旧进程占着：
  `Get-NetTCPConnection -LocalPort 8765 -State Listen`，占着就先杀掉再起。
- `gemini` 0.66.0 已装好，`cd "D:\AI study"; gemini` 直接可用，不用带任何参数。
  模型固定 `gemini-3-flash-preview`，**免费层 5 次请求/分钟**，撞了原地等 46 秒。
  所以：一次给一个完整的小目标，别试探式问答；重活交给本地 `python -m app.cli` / `app.serve`。
  配置细节见 `docs/GEMINI-SETUP.md`。
- 网络走 `127.0.0.1:7897` 代理（已配在用户环境变量里）。

### 工具坑（都踩过）

- **PowerShell 里 `python -c "…"` 带中文或反斜杠会被转义吃掉**（`\a`→BEL、`\n`→换行），
  把文件写坏且看不出来。**一律用 `write` 工具写成 `.py` 再跑。**
- 终端里中文可能显示成乱码，那是 GBK 控制台的问题，文件本身是 UTF-8，别据此判断文件坏了。
- `edit` 工具替换含结尾换行的片段时，要把换行一起给，否则会把下一行接上来
  （`def test_x(...):    with TestClient(...)`）。

---

## 明确不要做的事

| 别做 | 为什么 |
|---|---|
| 引 Vite / React / npm 构建链 | 现在 `python -m app.serve` → 刷新即见效。前端 1200 行，原生 DOM 够用。超过 2000 行再议。 |
| 把 `_paragraphs` / `_order` / `_toc_key` / `_running_lines` 简化掉 | 每一条都是真 PDF 上暴露过 bug 才加的，对应测试锁着。看不懂先看 ROADMAP §3。 |
| 把 PPT 课件按页切章 | PPT 一页 50–90 字，按页切会让模型拿 246 字的考纲讲一整节 → 膨胀 26.4 倍。必须整本一章。 |
| 用 OCR 文本校验公式 | `λ`→`入`、`ν`→`v` 是常态。`DocIR.ocr_pages` 记着哪些页是识别出来的。 |
| 改成全异步 / 换掉 ThreadPoolExecutor | 一次生成 40–70 秒，线程池 + SSE 已够，改 asyncio 只会引入新坑。 |
| 把 `out/` 里的产物当数据源 | 产物是结果，状态在 `out/index.json`（`app/store.py`）。 |
| 只改接口不看控件形态 | "服务端一直是对的"也可能被用户报成 bug：模型换不了，根因是 `<datalist>` 不出下拉。 |
| 加"更聪明的"内容过滤 / 温和话术 | 这个项目里没有这种东西的容身之处。 |

---

## 完成标准

一次改动算完成，当且仅当：

1. `.\.venv\Scripts\python.exe -m pytest -q` 退出码 **0**；
2. 新判据做过变异验证，对应的测试确实会变红；
3. 改了前端就 `node --check app/web/app.js`，并在真服务上看一眼（静态文件不用重启）；
4. `git status` 干净，提交信息是中文且前缀正确；
5. `docs/ROADMAP.md` §9 里被你解决的那一条，状态同步更新。
