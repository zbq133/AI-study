# GEMINI.md

> 这个项目由一位人类 + 多个模型轮值开发，代码被 **217 条测试**（+3 条默认跳过的真模型回归）锁着。
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

## 现在到哪了（2026-11-04，HEAD = `092accb`）

| 项 | 数字 |
|---|---|
| 代码 | `app/` 12 个模块 **2710 行**；`app/web/` 4 个文件 **1673 行** + 本地 KaTeX 548 KB |
| 测试 | **214 passed + 3 skipped**（共 217 条，`tests/` 12 个文件 3119 行）；行覆盖 **93%** |
| HTTP 端点 | **17 个**（上传 / 解析 / 布局 / 生成 / 取消 / 任务 / SSE / 历史 / 批量删 / 设置 / 探活） |
| 提交数 | 55 |
| 完成度 | 单人本机可用 **约 95%**；交给别人长期跑 **约 82%** |

**能做的事已经比"能用"多了**：三栏界面（目录 / 讲义 / 原文）、点出处高亮原文、习题先做后判、
公式走本地 KaTeX、历史记录落 `out/index.json` 并且能勾着批量删（带每条占用大小）、
生成可取消、成本有闸、错误分门别类、设置界面**五个标签**（模型接入 / 语言风格 / 颜色 / 字号 / 版本）、
默认浅色、原文栏默认收起且正文只占四分之三宽、探活 + 日志 + 一键启动，以及**双击即开的 Windows 客户端**。

**最近一次全链路真跑**（15 页双栏综述 `12. Summary and recommendations`）：

```
解析  4.2 秒 → 680 块 / 23 章（原生文字层，没走 OCR）
生成 46.1 秒 → 11 次调用 / 输入 18962 / 输出 6938 tokens
结果  5 节讲义 · 8 条"能…"学习目标 · 2 题全过独立重解验证
出处  36 条 source_refs 全部带 blocks + bbox（缺的 0 条）
```

**扫描课件**（12 页 PPT，`第十章 波动.pdf`，整本 1656 字）：

```
解析  102 秒（12 页全走 OCR）→ 97 块 / 1 章「第十章」
生成  58.9 秒 → 14 次调用 / 6 节 / 3 题全过
已知上限：quote 只有 9–15 字（一页五六十字，切完就剩这么长）
```

---

## 三条命令

```powershell
cd "D:\AI study"
.\.venv\Scripts\python.exe -m pytest -q      # 必须 214 passed, 3 skipped
.\.venv\Scripts\python.exe -m app.serve      # → http://127.0.0.1:8765（或直接跑 start.ps1）
.\.venv\Scripts\python.exe -m app.desktop    # 客户端窗口（等价的 exe 在 dist\AI Study\）
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
| 4 | `app/serve.py` | 18 个端点 + 任务/取消/成本闸/设置，一个文件看完全部外壳 |
| 5 | `tests/` 里的测试名 | 217 条测试名就是"哪些行为被锁住了"的清单 |

---

## 架构地图（行数即体量）

```
app/
  pdf.py      419  extract_ir：文字层 → 行 → 分栏排序(_order) → 并段落(_paragraphs) → 判标题
                   · 页眉页脚靠 _running_lines 跨页重复检测摘掉
                   · 图片型页面走 ocr.py；OCR 页不并段落、整本并成一章
                   · content_id() 用文件内容 sha256 当 doc_id（换机器也认得出同一份）
  ocr.py       87  RapidOCR 懒加载，200 dpi，结果缓存到 out/ocr/<doc_id>/p<N>.json
  schema.py   257  Block/DocIR/Section/Lesson…；build_sections 切章、verify_refs 校验出处
                   · locate_quote + _merge_bbox 把 quote 映射到块 id 与矩形
  pipeline.py 281  全流程编排：plan_outline → write_section → make_quiz → build_lesson
                   · 出题必须喂原文（_render_sources），无出处/出处对不上直接丢
                   · _solve_independently 打乱选项再解一次；致命错误上抛不吞
  prompts.py  102  system prompt（大纲 / 讲义 / 出题+验证）+ 三段语言风格（style_block）
  llm.py      106  带 JSON 校验重试、错误分类、成本闸(max_calls/max_tokens)、取消、思考等级降级
  errors.py   157  StudyError 家族：kind/title/hint，人看得懂的那种；is_fatal 判"该不该中止"
  settings.py 143  设置优先级 settings.json > 环境变量；带 mtime 缓存；永不回明文密钥
  store.py    121  out/index.json 索引：原子写、坏索引当空、加锁、批量删、file 只取文件名防穿越
  export.py   320  讲义→PDF：LaTeX 兜底成 Unicode、Markdown 子集转 HTML、PyMuPDF Story 排版
                   · 字体塞 Archive + @font-face，subsets_fonts 后 19 MB→84 KB
                   · 别用临时文件中转：Windows 上句柄没放，19 MB 垃圾删不掉 → 写 BytesIO
  serve.py    790  FastAPI：19 端点、线程池任务、SSE 进度、日志、探活、按页渲图（/page/N.png）
  desktop.py  218  客户端外壳：后台起 uvicorn + pywebview 原生窗口；端口复用/让位
                   · _ensure_streams()：窗口版 exe 没有控制台，sys.stdout 是 None
  cli.py       98  命令行入口
app/web/
  index.html  211 / app.css 367 / app.js 1039 / grade.js 56（纯判分函数，node 可直接测）
  vendor/katex/    本地 KaTeX 0.19.1（23 个文件 548 KB，离线可用）
tests/       3119  214 passed + 3 skipped
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
| 自审 11 条（doc_id 按内容、判后留解析、加锁、致命错误上抛、验魔数…） | 多处 | 9d0e49a |
| 历史批量删（带占用大小、PDF 引用计数） | `/api/lessons/purge` + `store.delete_many` | 3372e95 |
| 客户端 exe（pywebview + pyinstaller） | `app/desktop.py`、`build_desktop.ps1` | 95a5ef5 |
| 设置五个标签 + 语言风格三段提示词 | `prompts.style_block` + `#setStyle` | 3590c70 / 2d8cc60 |
| 默认浅色 / 对钩 / 强调条、外壳禁选文本 | `app.css` + `app.js` | 396ea19 / 5b0f0bf / e3c5550 |
| 当前打开的那条在列表里加深 | `.hist-item.on` + `markHistory` | 092accb |

---

## 交接第一棒（按 ROADMAP §11 的顺序，别跳）

> 原来排在前面的三件（出题阶段进度、`reasoning_note` 回前端、自审的 11 条）都已经做完，
> 见 `9d0e49a`。现在只剩下面这条，再接 §11 的第二、三优先。

### ① 成本折成钱（约 2 小时）

`store.py` 已经记 `prompt_tokens` / `completion_tokens`。加一张价格表（每百万 token，
**从环境变量可覆盖**，别写死在 JS 里），在生成结果和 `/api/lessons/{id}` 里各显示
一行「这次大约 ¥0.03」。价格缺失就只显示 token，不要猜。

测试要有两条：**价格已知时算得对**、**没配价格时不显示钱**（而不是显示 ¥0.00）。

### ② 出处细化到行（会碰数据模型，约半天）

`SourceRef.bbox` 现在是**段落块的合并矩形**：一段几百字时，同一段里多处引用指向同一个框，
点不同出处高亮的是同一片。要精确到"那一句"，得在 `verify_refs` 里把 quote 再映射到
行（`Block` 目前没有行信息，得先让 `_paragraphs` 留下行级 bbox）。

改完必须更新 `tests/test_schema.py` 里对 `bbox` 的断言，并确认老讲义（只有块级 bbox）
打开时不报错 —— 这是线上数据的兼容问题，不是可选优化。

### ③ PPT 场景的 quote 太短（产品级，先想清楚再动手）

一页五六十字，切完最长的连续片段就 9–15 字，校验能过但"出处"信息量很低。
两条路都别急着选：放宽 quote 下限等于放松校验；允许跨行拼接要改 `verify_refs` 的语义。
**先把「这样够不够用」想明白，再动代码。**

### 之后是 ROADMAP §11 的第二、三优先

公式 OCR（不可信就先别做宣称）。**§11 里"不建议现在做"的三条别碰。**

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
| 引 Vite / React / npm 构建链 | 现在 `python -m app.serve` → 刷新即见效。前端 1700 行，原生 DOM 够用。超过 2500 行再议。 |
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
