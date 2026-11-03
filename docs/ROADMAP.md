# AI Study · 开发计划与前端设计建议

> 交接文档。给下一个接手的人（或模型）：**先读完这页，再动代码**。
> 这里记的是「为什么现在写成这样」「哪些坑已经填过」「接下来往哪走」。
> 与 `docs/DECISIONS.md` 配合看：那份记关键决策的来龙去脉，这份记当下状态与待办。

---

## 0 · 这是什么

把大学课件 PDF（教材章节 / 论文 / PPT 导出的图片课件）变成两份东西：

1. **讲义** —— 不复述原文，讲清「是什么、为什么、卡在哪」，每个论点带出处。
2. **习题** —— 答案经过**独立重解**验证，且出处必须能在原文里**逐字**找到。

一句话定位：**可溯源的自动讲义**。卖点不是"AI 会讲"，是"AI 讲的每句话你都能点回去看原文"。
这句话决定了前端该怎么设计（见 §6）。

---

## 1 · 现状快照

| 项 | 状态 |
|---|---|
| 代码 | `app/` 12 个 Python 模块 **2164 行** + `app/web/` 四个文件 **1195 行**（另含本地 KaTeX 548 KB）+ `tests/` 11 个文件 **2160 行** |
| 测试 | **197 passed + 3 skipped**（共 200 条；skip 的三条是真模型回归，`STUDY_LIVE=1` 才跑） |
| HTTP 端点 | **17 个**（见 §7） |
| 后端内核 | **约 95%**：解析 → 切章 → 大纲 → 逐节讲 → 出题 → 独立验证 → 落盘，端到端真跑通过；错误分类、取消、成本闸、历史索引都已就位 |
| 外壳 | **约 90%**：CLI + 本地 Web 服务 + 探活 + 日志 + 一键启动脚本 |
| 界面 | **约 85%**：三栏（目录 / 讲义 / 原文）、出处高亮联动、习题先做后判、设置界面（四个标签：模型接入 / 颜色 / 字号 / 版本）、原文可折叠 |
| 最新 commit | `1acdcea fix: 浅色主题的灰字压深、设置按钮放大、模型换成能点的下拉` |
| 提交数 | 34 |
| 运行环境 | Python **3.12**（3.15 装不上 onnxruntime，见 §4） |

**还差什么**（按主人视角的完成度，不是行数）：

- 当"自己这台电脑上能用"：**约 92%**。剩下的是体验细节（见 §9）。
- 当"能交给别人长期跑"：**约 75%**。缺的是多用户 / 配额 / 反向代理与 HTTPS / 版本升级迁移，这些不是当前目标。

**真跑实测数据**（`12. Summary and recommendations`，15 页双栏综述，最近一次全链路）：

```
解析  4.2 秒 → 680 块 / 23 章（原生文字层，没走 OCR；跨页重复的页眉页脚已摘掉）
生成 46.1 秒 → 11 次模型调用 / 输入 18962 / 输出 6938 tokens
结果  5 节讲义 · 8 条"能…"学习目标 · 2 道题全部通过独立重解验证
出处  36 条 source_refs 全部带 blocks + bbox（缺 bbox 的 0 条）
```

跑起来的两种方式：

```powershell
# 网页（推荐，也可以直接双击 start.ps1）
.\.venv\Scripts\python.exe -m app.serve        # → http://127.0.0.1:8765

# 命令行
.\.venv\Scripts\python.exe -m app.cli "C:\path\to.pdf" 18 3
```

---

## 2 · 架构与数据流

```
PDF ──extract_ir──> DocIR ──build_sections──> [Section] ──select_chapter──> 本章
                      │                                                        │
                      │                              ┌──── plan_outline ───────┤
                      │                              │                         ▼
                      │                        write_section × N ────> [LessonSection]
                      │                              │
                      └──────────────────────────────┴──── make_quiz ────> [QuizItem]
                                                                              │
                                                          _solve_independently（打乱重解）
                                                                              ▼
                                                                          Lesson（JSON）
```

**核心模块**（改之前先读这些函数的 docstring，都写了为什么）：

| 文件 | 关键入口 | 职责 |
|---|---|---|
| `app/pdf.py` | `extract_ir(path, *, ocr=True, ocr_cache="out/ocr")` | 抽块、排序、合并段落、判标题；无文字层时调 OCR |
| `app/ocr.py` | `ocr_page_cached(page, doc_id, pno, cache_dir, dpi)` | RapidOCR 兜底，结果按页缓存 |
| `app/schema.py` | `build_sections` / `select_chapter` / `verify_refs` | 数据模型 + 切章 + 出处校验 |
| `app/pipeline.py` | `build_lesson(llm, ir, chapter, n_quiz=5, on_step=None)` | 全流程编排 |
| `app/prompts.py` | `OUTLINE_SYS` / SECTION_SYS / QUIZ_SYS / VERIFY_SYS | 提示词 |
| `app/llm.py` | `LLM.json(system, user, model_cls, ...)` | OpenAI 兼容客户端，JSON 校验失败自动修复重试 |
| `app/serve.py` | `app`（FastAPI） | 本地 Web 服务（17 个端点） |
| `app/desktop.py` | `run()` / `main(argv)` | 客户端外壳：后台起 uvicorn，pywebview 开原生窗口；8765 上已有本程序就复用 |

**数据模型要点**（`app/schema.py`）：

```python
class Block(BaseModel):          # 一个段落（不是一行）
    id: str; kind: BlockKind; text: str; page: int
    level: int = 0               # heading 的层级
    section_path: list[str]      # 从一级标题到自己的路径
    bbox: Optional[tuple[float, float, float, float]] = None   # ← 有坐标，但没往下传

class SourceRef(BaseModel):
    page: int                    # ← 只有页码和引文，没有 bbox
    quote: str
```

**`Block.bbox` 存在但 `SourceRef` 没有 bbox** —— 这是前端做原文高亮的唯一硬阻塞，见 §5 P0-1。

---

## 3 · 已经踩平的地，别重新发明

以下每一条都是真跑时暴露、修过、并且**加了测试 + 变异验证**的。改动前先看对应测试，别把它们改回去。

### 3.1 段落必须合并，否则喂给模型的是断在句中的碎片
`_page_lines` 出来的是**每一行**，不是段落。15 页的综述有 1374 个"块"，一页 92 块。
`_paragraphs()` 按五条判据合并（同栏 / 同字号 / 上一行行末是否填满 / 是否有首行缩进 / 行距），合并后 1374 → **688 块**。
判据全部写在 `app/pdf.py:136` 附近，`_glue()` 负责还原 `high-tempe-` + `rature` 这类断词。
**OCR 页不合并** —— PPT 一行就是一个要点。

### 3.2 分栏要先把横跨两栏的图注摘出去
旧判据"跨中线的行不超过 2 条才判分栏"，遇到一页有 3 条全宽图注就失效 → 左右栏按 y 混排成 `L1 R1 L2 R2…`，后半页每句话都接错上下文。
现在先按 `_WIDE_RATIO = 0.75` 摘出 `wide`，剩下的才参与分栏。

### 3.3 TOC 和正文标题的标点不一样
书签写 `1 Introduction`，正文写 `1. Introduction`，去掉空白后仍然不等 → 22 条书签全部落空，层级全靠猜。
`_toc_key()` 把空格和各类标点全去掉再比，15 个正文章节标题全部命中。

### 3.4 出题必须把原文喂进去
要求模型"逐字引用原文"，但 prompt 里只有讲义 —— 模型只能凭记忆把那句话重写一遍，页码也是编的。真跑时三题出处**全部校验失败**。
现在 `QUIZ_USER` 分两段：【讲义】决定考什么，【原文】只能从这里逐字抄，每行带 `[pN]` 前缀。
`_render_sources()` 只喂本章的页，不喂全书。

### 3.5 没有出处、或出处对不上的题，一律丢弃
`if not q.source_ref: continue` —— 模型干脆不给 ref 时不能放行。
`if not verify_refs(...): continue` —— 声称有出处但原文里找不到的，丢掉。
两条都在**独立解题之前**，省得白解一遍。

### 3.6 "独立验证"是打乱选项顺序再解一次
同一个模型同一个 key 重解只能证明自洽。现在把选项左旋一位，两次答案必须指向**同一份选项内容**。
这样模型蒙位置、背答案都会暴露。

### 3.7 图片型课件：OCR 之后整本并成一章
PPT 导出的整页图，文字层是空的。`ocr_page()` 用 RapidOCR 补，200dpi 一页约 10 秒。
标题判定**不能套教材那套字号启发式**：OCR 的框高是估的，竖排标签、示意图标注、公式碎片全会被当成标题。
最终方案：OCR 页不切章，整本并成一章（有"第 N 章"字样才切）。理由见 §4。

---

## 4 · 硬约束（这些不是 bug，别"修"）

### 4.1 PPT 和教材是两种输入
| | 一页字数 | 一整章 |
|---|---|---|
| 教材/论文 | 300–800 | 十几页 |
| PPT 课件 | **50–90** | 整本 1656 字 |

「第十章 波动」整本 12 页只有 1656 字，最大一页 246 字。按页切章会让模型拿 246 字的考纲讲一整节 → 讲义膨胀 **26.4 倍**、公式一个都对不上原文。
**所以 PPT 场景必须整本一章。** 想让 PPT 的讲义更细，得换输入（先 OCR + 补图，或让模型承认"这里原文没展开"），不能靠调 prompt。

### 4.2 OCR 出来的公式不可信
`λ`→`入`、`ν`→`v`、`u=λ/T`→`u===v`。正文识别很好（置信度 0.97–1.00），公式基本全废。
**不要**用 OCR 文本去校验公式，也不要在讲义里把 OCR 的公式当真值。`DocIR.ocr_pages` 记着哪些页是识别出来的。

### 4.3 PPT 场景的 quote 天生就短
一页 50–90 字，切成 3–4 个块后最长的连续片段就十几字。真跑时摘到的是「（仅在固体中传播）」「周期或频率只决定于波源的振动！」这种。
**这不是模型偷懒**。想改善只能改校验语义（允许跨行拼接并标注），见 §5 P2-1。

### 4.4 venv 必须是 Python 3.12/3.13
`onnxruntime` 没有 3.15 的轮子，用 3.15 建 venv 会 `ResolutionImpossible`。旧的 3.15 venv 改名留在 `.venv-315`，没删。

### 4.5 改文件别用 PowerShell 的 `Set-Content` / `Select-String -Replace`
会把 LF 变 CRLF 并加 BOM，`git diff` 直接整文件翻红。要用 `edit` 工具，或按 bytes 读写：

```python
p.write_bytes(p.read_bytes().replace(b"\r\n", b"\n"))
```

### 4.6 一次生成是 40–70 秒、13–15 次调用
同步跑必超时。服务里所有耗时操作都在 `ThreadPoolExecutor` 里，前端靠 SSE 看进度。不要改成同步。

---

## 5 · 开发计划

排序原则：**先补数据、再改界面、最后调质量**。跳过 P0 直接写前端，会写出一堆没有数据可显示的控件。

> **进度总览（2026-11-03）**：P0 三条全绿；P1 九条里八条做完，只剩 P1-4 的「成本折成钱」；
> P2 四条（PPT quote 太短 / 公式 OCR / 批量 / 导出）一条没动，都在 §6 后半段讨论过。
> 每条的落地形态都写在标题下面 —— **接手时先确认它是不是已经做了**，别再实现一遍。

### P0 — 不做就没法继续

#### P0-1 `SourceRef` 补上 bbox（前端高亮的唯一前置）✅（2026-11-03 已做）

**为什么**：`SourceRef` 只有 `page` + `quote`。前端想"点出处跳到原文并高亮那一行"，需要坐标。
`Block.bbox` 明明有，但 `verify_refs` 校验完就把它丢了 —— 而**校验过程本身已经知道 quote 落在哪几行**。

**✅ 已完成**，落地形态比原草案改了两处：

```python
class SourceRef(BaseModel):
    page: int
    quote: str
    blocks: list[str] = []                                    # 落在哪些块（记 Block.id，不是下标）
    bbox: Optional[tuple[float, float, float, float]] = None  # 合并后的外框
```

- `_reading_order_ok` 拆成 `_locate(needle, blocks) -> tuple[int, int] | None`（首尾下标）
  + `_reading_order_ok`（薄包装，旧调用点不动）+ `locate_quote(needle, blocks) -> list[int]`
  + `_merge_bbox(blocks, idxs)`（取非空 bbox 的 min x0 / min y0 / max x1 / max y1）。
- **`blocks` 存 `Block.id` 而不是页内下标**：下标跨页会重排，id 不会漂；调试时也能直接对上。
- `verify_refs` 命中后返回**新的** `SourceRef`（带 blocks+bbox）。调用方必须把返回值接住 ——
  `make_quiz` 里就是 `q.source_ref = ok[0]`，否则 enrich 过的对象被丢掉。
- 无几何信息（`blocks=None`）时原样返回旧对象，纯文本调用不受影响。

**验收（已达成）**：`tests/test_schema.py` 四条 + `tests/test_pipeline.py` 两条断言 +
`tests/test_serve.py` 断言落盘 JSON 带 blocks/bbox；变异四条（`locate_quote` 恒空 /
`_merge_bbox` 只取第一块 / `verify_refs` 不写 blocks / `_locate` 去掉行序检查）全部击杀。

#### P0-2 把界面拆成静态文件（不引构建链）

~~现在 `INDEX_HTML` 是 `serve.py` 里一个 7743 字符的三引号字符串。~~ **✅ 已完成。**

落地形态（比草案改了一处）：

```
app/web/index.html   1562 字节
app/web/app.css      1910 字节
app/web/app.js       4500 字节
```

- `serve.py` 从 505 行降到 325 行，`INDEX_HTML` 整个删掉。
- **没有用 `StaticFiles`**，改成自己写一个 `GET /static/{name}`：静态文件服务要的是
  "每个响应都带 no-cache"，`StaticFiles` 不方便插手；而本地开发是边改边刷的，
  缓存住一个旧 `app.js` 能让人怀疑人生。路径越界用
  `path = (WEB_DIR / name).resolve()` + `if WEB_DIR not in path.parents: 404` 拦。
- MIME 类型写成常量表 `WEB_TYPES`，**不交给 `mimetypes` 猜**：Starlette 的 FileResponse
  在 `media_type=None` 时会 `guess_type(path)`，而 Windows 的 mimetypes 读注册表，
  某些机器把 `.js` 登记成 `text/plain` → 浏览器严格 MIME 检查直接拒绝执行，表现是
  "页面出来了但按钮全不响应"。测试里把 `starlette.responses.guess_type` 打桩成
  `text/plain` 来锁这条。

**验收（已达成）**：`tests/test_serve.py` 五条（首页引上 css/js、css/js 类型正确、
no-cache、越界 404、类型不随系统猜测变）；变异四条全部击杀（去掉越界检查 /
`media_type=None` / 去掉 no-cache / 首页不引 css）。真起服务冒烟：
`GET /` 200 text/html 1562 字节、`/static/app.css` 200 text/css、`/static/app.js`
200 text/javascript、`/static/nope.css` 404，全部 `cache-control: no-cache`。

**明确不要做的事**：不引入 Vite / React / npm。
理由：现在 `python -m app.serve` → 刷新浏览器就能看到改动，没有构建等待。引入打包器会让"改一行等 30 秒 build"成为日常，而这个项目的前端复杂度（三栏 + 高亮 + 自测）用原生 DOM + 一点点事件委托完全够。等到超过 2000 行再议。

#### P0-3 生成记录要有索引 ✅（2026-11-03 已做）

现在成品是散在 `out/` 里的 JSON 文件，重启服务后 `DOCS` 内存字典清空，之前的 doc_id 全废，用户找不到自己生成过什么。

**落地形态**：`app/store.py` + `out/index.json`（没用 sqlite —— 文件就几百条，读取整份重写更简单，也不多一个并发问题）。

- 一条讲义一行：`{id, doc_id, chapter, filename, file, sections, quiz, calls, prompt_tokens, completion_tokens, seconds, created, updated}`。
- **id 由 `sha1(doc_id + chapter)[:12]` 决定**：同一份 PDF 的同一章重新生成就是覆盖，不留一堆同名文件；`created` 保留首次时间，`updated` 每次刷新。
- 写入走临时文件 + `os.replace`，中途崩了不会留下半截 JSON；索引被人手改坏了就当空的用（下次写入重排），不让它把服务拖死。
- `file` 字段只按文件名解析（`out_dir / Path(file).name`），记录被写成 `../secret.json` 也读不到外面。
- 端点：`GET /api/lessons?q=&limit=`、`GET /api/lessons/{id}`（记录 + 讲义本体）、`DELETE /api/lessons/{id}`（连文件一起删）、`POST /api/lessons/purge`（批量删，可选连上传的 PDF 一起删）。列表里每条都带 `bytes` / `pdf_bytes`，`total_bytes` 是合计 —— 占地方的是讲义还是 PDF，打开就知道。
- 记录在、文件被手删 → `410 讲义文件被删了，重新生成一次`，不是 500。
- 左栏顶部加"最近生成"，点一条就把讲义渲染出来；**原文栏只在原 PDF 还在内存里时才有内容**（重启后 doc_id 就没了，这时列表能看、出处点不回原文）。

**验收**：重启服务后历史记录还在，能量出来、能打开。

---

### P1 — 用起来像个产品

#### P1-1 三栏界面 + 原文高亮联动 ✅（2026-11-03 已做）

**✅ 已完成**（方案 A：文字版原文，不是 pdf.js）。详见 §6.2 / §6.3。

- 后端加 `GET /api/docs/{id}/layout?pages=11,13-15`，把原文按页摊开成
  `[{page, blocks:[{id, kind, text, level}]}]`，块 id 与 `source_ref.blocks` 同一套编号。
  不传 `pages` 就是全部（15 页 / 680 块约 200 KB，本地够用）。
- 前端三栏：左目录（点一下切章）、中讲义（`explanation_md` 走一个 40 行的极简
  Markdown 渲染：段落 / 标题 / 列表 / 粗体 / 行内代码），公式交给本地 KaTeX、
  右原文（本章页 + 所有出处落在的页）。
- 讲义里每条 `source_refs` 变成一枚「原文 pN」chip，点它：先清掉旧高亮，再给
  `blocks` 里所有块加 `.hl`，滚第一块到视口中间。块 id 直接当 `data-id` 用。
- 实测：15 页综述解析出 680 块，layout 也是 680 块；拿上一份讲义的 **37 条出处**
  去 layout 里查，**一条都不缺**。

#### P1-2 习题自测模式

**✅ 已完成**。详见 §6.4。

- 默认把答案藏起来：选项是可点的按钮，多选会标「这题是多选」；点「提交」才判分，
  对的选项染绿、选错的染红，下面才展开「答案 X + 解析 + 原文出处」。
- 「看答案」可以直接摊开（不判分），「再做一次」清空重来。
- 判分规则放在 `app/web/grade.js` 这个纯函数模块里（浏览器里是 `window.Grade`，
  node 里能 `require`），所以 `tests/test_web.py` 用 node 直接测它：多选顺序无关、
  全角 ＡＣ 也认、填空题先掏空白再比、`/` 或 `|` 分隔的多种写法命中任意一个都算对。
- 填空题（`type: fill`）用输入框，参考答案按文本比对。

#### P1-3 生成可以取消 ✅（2026-11-03 已做）
现在点"生成"之后只能干等 45 秒，中途反悔没有出口。

落地形态（和原计划不同，比"在 `on_step` 里查标志"更彻底）：

```python
# app/llm.py —— 每一层都能自己判断该不该停
LLM(..., max_calls=40, max_tokens=200_000, should_stop=job.cancel_evt.is_set)

def _check(self):
    if self.should_stop is not None and self.should_stop():
        raise Cancelled()
    if self.max_calls is not None and self.calls >= self.max_calls:
        raise BudgetExceeded(...)          # kind="budget"，hint 指向环境变量
```

- `Job` 上挂 `threading.Event()`，`POST /api/jobs/{id}/cancel` 只做一件事：`set()`。
- 检查放在**调用之前**，所以停下时不会再多发一次请求；代价是 token 上限最多超出一次调用的量。
- 解析路径（`extract_ir(..., should_stop=...)`）同样每页查一次 —— OCR 一本扫描件要一两分钟，只有生成能停是不够的。
- 上限来自 `LESSON_MAX_CALLS`（默认 40）/ `LESSON_MAX_TOKENS`（默认 200000），写歪了退回默认值，不让服务起不来。
- 前端 `watch()` 里统一挂"停"按钮，任务结束自动收起；对已结束的任务点"停"返回 200 但不改状态。

#### P1-4 顶部显示成本 ✅（部分，2026-11-03）
每次生成 13 次调用、输入 19136 / 输出 6883 tokens。按 deepseek-chat 的价格算一下大概几毛钱。
进度条旁边那行已经有 `模型调用 N 次`（`job.snapshot()` 里的 `calls`），**还差把 token 折成钱显示出来**。

#### P1-5 错误要能看懂 ✅（2026-11-03 已做）
落地成 `app/errors.py`：`kind`（程序认）+ `title`/`hint`（人看）+ `detail`（排查）。
`wrap_llm_error` 按**类名**和 `status_code` 两道判据把 openai 的一堆异常收敛成 auth/quota/timeout/down 四种
（不 import openai 的具体类，换版本不会连带崩掉）；`describe()` 给前端结构化的 `error_info`。

#### P1-6 能交给别人跑 ✅（2026-11-03 已做）
原来只有 `python -m app.serve`，出了问题全靠翻终端里滚过去的 traceback。

- **`GET /api/health`**：`ok` / `version` / `uptime` / `llm{ready,missing,model}` / `jobs{running,total}` /
  `docs` / `lessons` / `limits{max_calls,max_tokens}`。**故意不读 PDF、不碰模型** —— 探活接口要便宜到能每秒打一次。
  索引坏了 `lessons` 记 `-1`，但 `ok` 仍是 `true`：探活不该因为缓存文件坏了变红。
- **日志落盘**：`setup_logging()` 把 `logging` 挂到 `out/server.log`（`RotatingFileHandler`，2 MB × 2 份），
  请求级中间件记 `方法 路径 -> 状态 耗时`；4xx/5xx 记 WARNING，静态资源和 SSE 心跳不记（太吵）。
  只在 `main()` 里挂 —— 测试 import 模块不该在磁盘上留文件。重复调用幂等。
- **`start.ps1` / `start.sh`**：先查 `.venv` 在不在、`.env` 在不在、8765 被谁占着，然后才起服务。
  报错要说清楚下一步做什么，不是抛一段 traceback 让用户猜。
- **README 重写**：网页五步流程、命令行、环境变量表、128 条测试、目录结构、六条常见问题
  （扫描件空、公式 OCR 不可信、习题一题都没有、成本上限、端口被占、venv 必须 3.12）。
- 版本号落在 `app/__init__.py` 的 `__version__`（当前 `1.0`），`/api/health` 直接报出去；设置里的「版本」标签读的就是它，旁边的「更新」按钮点一下会重新问一次，不一致就刷界面。

#### P1-7 真模型回归 ✅（2026-11-03 已做）

128 条测试全是假桩，跑得快、不花钱，但它们证明不了"真模型现在还按我们的格式回话"。
模型侧一改（`response_format` 变了、字段名变了、限流策略变了），假桩全绿而线上全崩。

`tests/test_live.py` 三道闸，默认 skip，`STUDY_LIVE=1` + 配好 key 才跑（两次调用、二三十秒、几毛钱）：

1. **一次 outline 调用**：`llm.json(OUTLINE_SYS, ..., Outline)` 能不能过 pydantic —— 验 key、JSON 模式、字段名。
2. **一条最短真链路**：造一份 5 行英文 PDF → `extract_ir` → `build_sections` → `build_lesson(n_quiz=1)`，
   然后**把每一条出处（讲义 + 习题）重新丢回 `verify_refs` 逐字校验**。这条测的是项目的卖点本身：
   讲义可以写得不好，但不能出现"点回去对不上原文"的引用。
3. **假 key**：必须抛 `StudyError`（`kind` 是 `llm_auth` / `llm_down` 之类），带 title 和 hint，
   不能是裸的 `AuthenticationError` 到处乱飞。

踩过的坑：第一版写 `llm.api_key = "假的"` 然后期待失败 —— `api_key` 只在 `__init__` 里用来建 client，
改属性没用，那次调用拿的还是真 key，测试**假绿**了一次。要拿假 key 就必须重新构造 LLM。


#### P1-8 设置界面：接谁的模型，界面上就能改 ✅（2026-11-03 已做）

在这之前，"换个模型试试"要打开 `.env` 改一行再重启服务 —— 而这件事每天都会做。

**落地形态**：左下角一颗「⚙ 设置」钉子，点开是个居中弹层，第一栏叫「① 模型接入」：

| 项 | 说明 |
| --- | --- |
| 接入地址 | `base_url`，保存前校验必须以 `http://` / `https://` 开头 |
| API 密钥 | `password` 输入框，**只显示尾四位**（`sk-…ec75`）；留空表示"不改" |
| 模型 | 输入框 + `datalist`：服务端会去 `{base_url}/models` 问一次真实列表，问不到也能手填 |
| 思考等级 | 四档（默认 / 低 / 中 / 高），落成 `reasoning_effort` 传给模型 |

**配置优先级**：`settings.json`（设置界面写的）> 环境变量（`.env`）> 空。
空串等于"没设置"，删掉那个键就回落到 `.env` —— 界面上把密钥清空不该把配置弄坏。
每个字段下面标着它现在来自哪儿（「来自设置界面」/「来自 .env」），不然改完不生效没法查。

**为什么落文件而不落数据库**：就四个字段，`settings.json` 原子写（`.tmp` + `os.replace`）比 sqlite 少一层依赖；
文件坏了当空读，配置坏不该让服务起不来。文件进 `.gitignore`（里面可能有密钥）。

**思考等级不保证生效**：不是所有模型都认 `reasoning_effort`。生产里第一次被拒就**摘掉这个参数重发一次**，
并在 `llm.reasoning_note` 里记一句 "当前模型不接受「思考等级」" —— 界面上照实说，
不许假装已经生效（那比没有这个开关更坏）。

**改完不用重启**：`_missing()` / `effective()` 每次现读，`/api/config` 与 `/api/health` 都跟着变，
生成任务用的是当下的配置。


#### P1-9 颜色可调 + 原文栏可折叠 ✅（2026-11-03 已做）

**颜色**：第二栏「② 颜色」，三档（深色 / 浅色 / 自定义），自定义能挑四个主色（背景、面板、正文、强调）。

能不能真的换皮，取决于样式表的结构 —— 所以先做了一次**色板化**：`app.css` 里 62 个颜色字面量，
把成体系的 81 处换成 CSS 变量（`--bg` / `--panel` / `--line` / `--fg` / `--dim` / `--accent` …），
`:root` 是深色、`:root[data-theme="light"]` 只覆盖变量。剩下的零散颜色（KaTeX 报错色、
几处渐层）保持原样，它们本来就不该跟着主题走。

- 主题存在 `localStorage`（`study.theme` = `{mode, base, custom}`），**不占服务端配置**：
  换主题是这台电脑这块屏幕的事，跟着账号走反而奇怪。
- `index.html` 的 `<head>` 里有一小段内联脚本先套主题 —— 否则每次刷新暗色页面会先闪一下白。
- 自定义是往 `<html>` 写内联变量，优先级高于两套主题；切模式前先清掉内联，不然会串。
- 浅色主题把高亮色也换了一套（`--hl-bg` 从深绿底变成浅绿底），否则「点出处高亮」在浅色下会糊成一片。

**折叠**：讲义栏标题右侧一颗「收起原文 / 展开原文」。
`.cols.no-src` 把栅格从 `200px 1fr 1fr` 换成 `200px 1fr` 并隐藏 `#source`，讲义直接吃掉整行。
两点容易忘：

1. **点出处时如果原文栏是收起的，要先展开再跳** —— `scrollIntoView` 打在一个 `display:none` 的块上是无声的。
2. 折叠状态也记在 `localStorage`，下次打开保持原样。

测试 147 → **150**，变异 5 条全杀（正文背景改回字面量、浅色主题改名、收起不换栅格、
点出处不先展开、首屏脚本不读主题）。

**主人用完之后的三条反馈（同日补修）**：

- **浅色下字太淡**：第一次色板化只换了"成体系的背景/边框/正文"，还剩 20 处角色色是字面量
  （`#b8c8da` 小节标题、`#cbb98a` 易错点、`#7fb3dd` 出处标签、`#dcebf9` 选中选项）——
  它们本来都是"深底上的浅字"，换白底就成白纸上的浅灰蓝。补了七个角色变量
  （`--chip-*` / `--opt-on-*` / `--bad-bg|fg` / `--btn-*` / `--top-line`），深色值照抄原字面量，
  浅色值重挑。**判断标准写成测试**：`test_light_theme_text_is_dark_enough_to_read` 按 WCAG
  算 `fg/dim/dim2/dim3` 在 `panel`、`bg` 上的对比度，要求 ≥10 / 7 / 6 / 5 —— 以后调色板别再把字调糊。
- **设置按钮太小**：`13.5px` + `7px` 内边距 → `17px` + `12px 22px`，加投影、`:hover` 边框走强调色。
- **"模型无法切换"**：**服务端一直是好的**（实测 POST 换模型后 `/api/config` 立刻变），
  坏在控件形态 —— `<input list>` + `<datalist>` 在 Chromium 里不出可点的下拉，必须手打全名。
  换成真 `<select>`：用 `/models` 的返回填 `<option>`（当前模型不在列表里也插到首位，否则切完显示空白），
  末尾永远有「手动填写…」，选中它才露出自由输入框。

测试 150 → **153 passed + 3 skipped**（三条新用例：浅色对比度、按钮尺寸、模型必须是 select 且 datalist 消失）。


---

### P2 — 质量

#### P2-1 PPT 场景 quote 太短的解法
两条路，**倾向第二条**：

1. 放松下限（现在 `len(needle) < 6` 就丢）—— 等于放松校验，不推荐。
2. **允许跨行拼接并标注**：`quote` 可以跨相邻块（同一栏、y 单调递增），前端高亮时显示成多行高亮。`verify_refs` 的语义从"必须是一段连续字符串"放宽到"必须是阅读顺序连续的若干完整块"，但仍然拒绝双栏拼接（y 回跳）。

写测试前先想清楚"跨行"和"跨栏"的边界：同栏相邻两行的边界拼接，与合法引用在字符串层面无法区分 —— 只能靠 y 单调性判断，这个边界要接受。

#### P2-2 公式 OCR
`λ`→`入` 这类错认没法靠后处理救。可选：
- 换 OCR 后端（PaddleOCR / 商业 API）
- 加公式专用识别（如 pix2tex）单独跑公式区域
- **最省事**：在讲义里标注"本节公式来自 OCR，可能有误"，并给原文截图（前端有 PDF 渲染之后，点公式直接看原图）

#### P2-3 多章节批量
现在一次只能一章。批量跑 N 章要排队 + 总进度，并且注意 `_generate_lock` 是全局的，批量时别把用户卡死。

#### P2-4 导出
- Markdown（讲义原文，最省事）
- Anki 卡（题干 + 答案 + 出处，`csv` 分号分隔）
- PDF（前端已渲染的话直接打印样式）

#### P1-10 客户端版（不碰浏览器）✅（2026-11-03 已做）

网页版还是网页版，外面套一层原生窗口：`app/desktop.py` 在后台线程里起 uvicorn，再用
**pywebview**（Windows 上就是系统自带的 WebView2）开一个 1360×900 的窗口指向它。

- **不引 Electron**：项目本来就是 Python，Electron 等于为了一个窗口再拖一套 Node 运行时进来。
- **端口复用**：先探 8765，`/api/health` 回的对象里同时有 `jobs` 和 `llm` 才认"这是本程序" ——
  只认 200 不够，别人家服务也可能回 200。认得就复用（不再起第二份），端口空着就用它，
  被别人占了就临时挑一个。
- `text_select=True`：讲义要能选中复制，窗口不能是那种选不了字的壳。
- `private_mode=False` + `storage_path=~/.ai-study/webview`：否则主题、字号档每次重开都丢。
- **窗口版 exe 没有控制台**：`sys.stdout` 是 `None`，uvicorn 的日志格式器会调 `sys.stdout.isatty()`
  当场炸掉。`_ensure_streams()` 把空流接到 devnull，并把 `log_config=None` 传给 uvicorn，
  日志改由 `serve.setup_logging()` 落在 `out/server.log`。

打包（`build_desktop.ps1`，产物 `dist\AI Study\AI Study.exe`，359 MB）：

- **onedir 而不是 onefile**：单文件每次启动都要把几百 MB 解到临时目录，冷启动十几秒，还容易被杀毒盯。
- `--collect-all pymupdf` 不能省：`import pymupdf` 会激活 `pymupdf.layout`，它要读
  `pymupdf/layout/resources/onnx/*.yaml`，PyInstaller 默认不收这些数据。
- `run_desktop.py` 外面包了一层 try/except 写 `crash.log` —— 窗口版出错时屏幕上只有
  "Unhandled exception in script"，没有这个文件等于闭着眼睛修。
- **`build_desktop.ps1` 必须存成 UTF-8 带 BOM**：PowerShell 5.1 否则按 GBK 读，中文注释直接解析失败。
  用编辑器保存一次就会掉 BOM，要补 `b'\xef\xbb\xbf'` 前缀。

数据目录跟着工作目录走：exe 在 `dist\AI Study\` 里跑，`out/`、`uploads/`、`settings.json`
就生成在那儿。想接着用网页版的历史，把这三样拷到 exe 旁边。

---

## 6 · 前端设计建议（重点）

### 6.1 现在的界面差在哪

现在是一个四张卡片的纵向表单：上传 → 选章节 → 进度 → `<pre>` 结果。

问题不在难看，在**它把这个软件最大的卖点浪费了**：

- **讲义和原文是分离的**。出处 `SourceRef` 在数据里有，界面上只显示成一个孤零零的 `[p11]` 文本。
  用户想知道"这句话原文怎么说的"，只能自己去翻 PDF，那这个软件的"可溯源"就等于零。
- **习题不能做**。三道题连答案一起糊在 `<pre>` 里，第一眼就看见答案，没有自测价值。
- **结果是一大坨文字**。`explanation_md` 是 Markdown（含 `$公式$`），直接塞进 `<pre>` 显示的是 `$E=mc^2$` 原文。

### 6.2 目标形态：三栏

```
┌────────────┬──────────────────────────┬────────────────────┐
│ ① 目录     │ ② 讲义                   │ ③ 原文             │
│            │                          │                    │
│ ▾ 第十章   │ 学习目标                 │ [p11] ──────────── │
│   · 波动   │  · 能说出机械波与…       │ …electrolysis, hi… │
│            │                          │  pulsed discharge… │ ← 高亮
│ 共 12 页   │ ─────────────────────    │ …                  │
│ 907 字     │ 1/5 机械波的几个概念     │                    │
│            │ 讲解正文… [p6]           │                    │
│ 生成 3 题  │                          │                    │
│            │ ─────────────────────    │                    │
│ [重新生成] │ 2/5 横波与纵波           │                    │
│            │ …                        │                    │
└────────────┴──────────────────────────┴────────────────────┘
```

- **左栏（目录）**：章节树，每项带 `p{start}-{end}` 和字数。参考文献段默认折叠/隐藏（后端已经在 `sections` 里过滤了，`hidden` 字段给出数量）。当前章高亮。
- **中栏（讲义）**：学习目标 → 逐节讲解 → 习题。`explanation_md` 要**渲染 Markdown**，`$…$` 用 KaTeX（已放进 `app/web/vendor/katex/`，548 KB，离线可用，别换 CDN —— 换台电脑未必有网）。
- **右栏（原文）**：跟中栏联动。默认显示本章第一页。

**响应式**：窄屏（< 1100px）塌成单栏，原文变成底部抽屉，点出处时滑出来。讲义阅读体验优先，别为了塞三栏把字号压到 13px。

### 6.3 关键交互：出处 ↔ 原文高亮联动

这是整个前端最该做对的一件事。

**数据从哪来**：P0-1 完成后，每个 `SourceRef` 有 `page` + `bbox` + `blocks`。

**两种实现，建议先做第一种**：

**方案 A · 文字版原文（零依赖，1 天）**
后端返回本章所有 blocks（已经是合并好的段落，带 `page` / `bbox` / `kind`），前端按阅读顺序渲染成一个可滚动的文字流：

```
[p11]  ─────────────────────────────
       The electrical methods, such as electrolysis, high-voltage
       pulsed discharge, and electromagnetic induction, are hindered…
       ↑ 命中行整行加底色 + 左侧竖线
```

- 优点：不用 pdf.js，不用处理 wasm / 字体 / 渲染性能。文字可选中、可搜索、可 Ctrl+F。
- 缺点：看不到图、看不到公式原貌。
- 对论文和教材够用，**对 PPT 课件不够**（它有大量图片）。

**方案 B · 真 PDF 渲染（+2–3 天）**
pdf.js 渲染页面 canvas，按 `bbox` 在上面画半透明矩形覆盖层。
坐标换算：`bbox` 存的是 **PDF 点（pt）**，页面尺寸是 `page.rect`；pdf.js 的 viewport 会做缩放，注意 `scale` 与 `rotation`。
> 参考：本机已有的 `dsh-paper-reader` 插件踩过 pdf.js 6.x 的坑 —— 图像解码挪进 wasm，`getDocument` 不传 `wasmUrl` 时扫描件整页渲染成纯白。要用 pdf.js 6.x 就把 wasm 一起 vendor 进来。

**交互细节**：
- 讲义里每个出处渲染成小方块角标 `[p11]`，`cursor: pointer`，hover 有 tooltip 显示 quote 前 40 字。
- 点角标 → 右栏滚到对应位置，命中行高亮 1.5 秒渐隐到常亮底色。
- 右栏高亮区的左上角反向显示「在讲义 §2/5」的返回链接，点回来。
- 键盘：`j` / `k` 在出处之间跳转，`Esc` 取消当前高亮。
- 找不到坐标时（旧数据）角标仍然显示页码，但不可点，样式变灰 —— 别骗用户。

### 6.4 习题自测模式

现在的 `<pre>` 把题干、选项、答案一起铺开，等于没有自测。

**做法**：

```
3/3  下列说法正确的是？
      A. 电解法的高能耗是其规模化应用的阻碍
      B. 机械破碎得到的黑粉表面不含有机粘结剂
      C. 热处理只能去除电解液
      [提交]
```

- 提交后才展开：正确答案 + 「本答案经打乱选项顺序重解验证」的勾 + 出处高亮联动。
- 选项点击 → 立即给对错反馈（单选）；多选要提交。
- 答错的题进"错题本"，最后给 `2/3 正确` + 逐题解析。
- 记录答题结果（localStorage 就够，不用上后端），下次打开同一份讲义能接着做。
- **`verified` 字段**要显示出来：`true` 是"重解过"，这是产品的差异化，别藏起来。

### 6.5 进度条要让人相信它在干活

现在的进度是 `outline=5%` → `section 5%→80%` → `quiz=82%` → `done=95%`。有两个问题：

- **出题阶段（82%→95%）是黑箱**：一次 `make_quiz` 要 30 秒，中间 3 道题各解两遍，进度一动不动。后端可以在 `_solve_independently` 前后加回调，报"正在验证第 1/3 题"。
- **OCR 阶段完全没进度**：一页 10 秒、12 页两分钟，现在只有一个"解析 PDF"的死阶段名。`ocr_page_cached` 有页码，直接报 `OCR 第 3/12 页`。

**具体改动**：`build_lesson` 的 `on_step(**info)` 已经打通，往 `make_quiz` 里也传一个 `on_step`，加两类 stage：`verify`（index/total）、`ocr`（index/total）。

### 6.6 技术选型

| 需求 | 建议 | 理由 |
|---|---|---|
| 框架 | **无**（原生 DOM + 事件委托） | 见 P0-2，别引构建链 |
| Markdown | `marked` 或 `markdown-it`（CDN，15KB gzip） | 手写 Markdown 解析得不偿失 |
| 公式 | KaTeX 0.19.1（本地 vendor，548KB 含字体） | 比 MathJax 快、体积小；`$…$` 行内、`$$…$$` 独立行；离线可用 |
| PDF | pdf.js（本地 vendor，不要 CDN） | 离线可用 + 版本可控 + 要带 wasm |
| 状态 | 一个全局 `state = {}` + `render()` | 页面只有一个视图，不需要状态库 |
| 样式 | CSS 自定义属性 + Grid | 暗色主题已经在用了（`#0f1419`），继续 |
| 字体 | 系统字体栈 | 别引 Web Font，中文字体包太大 |

### 6.7 视觉与状态清单（细节）

- 暗色背景 `#0f1419`，卡片 `#151b23`，边框 `#232c38`，主色 `#3f8fd6`。现在这套已经不错，延续。
- 空状态要说话：「还没有上传课件」+ 一句提示，别留空白卡片。
- 加载中骨架屏 > 转圈。
- 中文字体下字号别小于 14px，讲义正文 15–16px，`line-height: 1.75`。
- **OCR 页要打标记**：`ir.ocr_pages` 非空时，右栏对应页顶部加一条淡黄提示「本页文字由 OCR 识别，公式可能不准」。诚实比好看重要。
- 生成中禁用上传按钮，避免两个 doc 打架。
- 长讲义要有回到顶部 / 目录浮动导航。

---

## 7 · 需要的 API 与数据模型改动（清单）

**当前已实现的 16 个端点**（`app/serve.py`，接手时以代码为准）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 单页界面（`app/web/index.html`） |
| GET | `/static/{name:path}` | 静态资源，**每个响应都带 `cache-control: no-cache`**，MIME 走白名单表 |
| POST | `/api/docs` | 上传 PDF（≤80 MB，非 .pdf 直接 400） |
| POST | `/api/docs/{doc_id}/parse` | 解析成 DocIR + 切章；已解析过直接回缓存结果 |
| GET | `/api/docs/{doc_id}/layout?pages=11,13-15` | 原文按页摊开成 `{page, blocks:[{id,kind,text,level}]}` |
| POST | `/api/lessons` | 起一个生成任务，返回 `job_id` |
| GET | `/api/lessons?q=&limit=` | 历史列表（读 `out/index.json`） |
| GET | `/api/lessons/{lesson_id}` | 单份讲义（记录 + 本体；文件没了 410） |
| DELETE | `/api/lessons/{lesson_id}` | 删记录连文件一起删 |
| POST | `/api/lessons/purge` | 批量删（`{ids, drop_pdfs}`），顺带删没人再用的上传 PDF |
| POST | `/api/jobs/{job_id}/cancel` | 置取消位；生成/解析的下一次心跳就会停 |
| GET | `/api/jobs/{job_id}` | 任务快照（进度、成本、错误分类、是否可取消） |
| GET | `/api/jobs/{job_id}/events` | SSE 进度流（0.4 秒一帧） |
| GET | `/api/config` | 够不够跑（缺哪些环境变量、当前模型） |
| GET | `/api/settings` | 接入地址 / 是否已存密钥（只回 `sk-…ec75` 这种提示）/ 模型列表 / 思考等级 |
| POST | `/api/settings` | 改设置（写 `settings.json`，**永不回明文密钥**，改完不用重启） |
| GET | `/api/health` | 探活：版本、运行时长、任务数、历史数、上限 |

| # | 改动 | 所属 | 破坏性 |
|---|---|---|---|
| 0 | `GET /static/{name}` 发 `app/web/` 里的 css/js | ✅ P0-2 已完成 | 新增 |
| 1 | `SourceRef` 加 `blocks: list[str]`、`bbox` | ✅ P0-1 已完成 | 否（有默认值） |
| 2 | `_reading_order_ok` → `locate_quote` 返回位置 | ✅ P0-1 已完成 | 内部函数，不影响外部 |
| 3 | `GET /api/docs/{id}/layout` 返回本章页的 blocks | ✅ P1-1 已完成 | 新增 |
| 4 | `POST /api/jobs/{id}/cancel` | ✅ P1-3 已完成 | 新增 |
| 5 | `GET /api/lessons` 列表 / `GET /api/lessons/{id}` | ✅ P0-3 已完成 | 新增 |
| 6 | `on_step` 增加 `stage="verify"` / `"ocr"` | 未做（§6.5，进度条再细一档） | 否 |
| 7 | `make_quiz(..., on_step=None)` 新参数 | 未做（同上） | 否（默认 None） |
| 8 | `GET /api/settings` / `POST /api/settings` | ✅ P1-8 已完成 | 新增 |

---

## 8 · 协作规范

> 如果接手的是 **Gemini CLI**，项目根目录的 `GEMINI.md` 会被它自动读走 —— 那份是同内容的精简纪律版
> （铁律 + 第一棒任务 + 禁止事项），这份 ROADMAP 是完整版。两份一起改，别只改一份。

**接手前**（30 分钟）：

```powershell
cd "D:\AI study"
.\.venv\Scripts\python.exe -m pytest -q          # 必须 197 passed, 3 skipped
.\.venv\Scripts\python.exe -m app.serve          # 打开 127.0.0.1:8765 走一遍
Get-Content docs\DECISIONS.md                     # 看关键决策的来龙去脉
```

**改代码时**（`AGENTS.md` 里的铁律，一直被遵守）：

1. **改前先提交**，留回滚点。
2. **改后必测**：新逻辑配新测试，`pytest` 全绿才算完成。
3. **做变异验证**：故意把新写的判据改坏一条，确认对应测试变红。测试全绿但改坏了还绿，那是空断言。
   （脚本模式：string replace → 跑 pytest → 必须非 0 → `write_bytes` 还原原文件）
4. **提交信息中文**，前缀 `feat:` / `fix:` / `test:` / `chore:` / `docs:`。
5. **改文件用 `edit` 工具**，别用 PowerShell 写文件（CRLF/BOM，见 §4.5）。
6. **别为了出题数量放松校验**。题少是对的，题假的才是坏的。

**判断"能不能提交"的唯一标准**：`pytest` 退出码为 0，且 `git status` 干净。

---

## 9 · 已知问题清单

| 严重度 | 问题 | 位置 | 状态 |
|---|---|---|---|
| P0 | ~~`SourceRef` 没有 bbox，前端无法高亮~~ | `app/schema.py` | 已做（`blocks` + `bbox`，e1e28a7） |
| P1 | ~~讲义里的 `$公式$` 在界面上原样显示~~ | `app/web/` | 已接本地 KaTeX（离线 vendor 548 KB） |
| P1 | ~~生成不能取消~~ | `app/serve.py` | 已做（`cancel_evt` + 调用前查，P1-3） |
| P1 | ~~错误只有一句 traceback~~ | `app/errors.py` | 已做（kind + title + hint，P1-5） |
| P1 | ~~历史记录无索引，重启即丢~~ | `app/store.py` | 已做（`out/index.json`，P0-3） |
| P1 | ~~`.hide` 规则缺失，答案和解析一直摆着~~ | `app/web/app.css` | 已修（2442ed5） |
| P1 | ~~浅色主题灰字读不清~~ | `app/web/app.css` | 已修（补 7 个角色变量 + 对比度测试） |
| P1 | ~~出题阶段（约 30 秒）进度条不动~~ | `app/pipeline.py` 的 `make_quiz` | 已做（`on_step(stage="verify")`，82%→94%） |
| P1 | **OCR 阶段没有页级进度** | `app/ocr.py` | 待做：`ocr_page_cached` 可回调页码 |
| P1 | **成本只显示调用次数，没折成钱** | `app/web/app.js` | 待做（P1-4 余下）：按模型单价表折算，价格要可配置 |
| P1 | **出处粒度是"段落块"，不是"那一句"** | `app/schema.py` 的 `_merge_bbox` | 待做：把 bbox 细化到行（同一段里多条 ref 现在指向同一个框） |
| P2 | PPT 场景 quote 只有 9–15 字 | 校验语义 | 待定方案（P2-1：允许跨行拼接并标注） |
| P2 | 公式 OCR 错认（`λ`→`入`、`ν`→`v`） | `app/ocr.py` | 待定方案（P2-2：公式页单独走模型或多模态） |
| P2 | 正文里两个孤立 `$` 会被当公式 | `app/web/app.js` 的 `paintMath` | 已知，实测 26 条行内无误伤，先接受 |
| P2 | `knowledge_point` 可能长到 48 字 | prompt | 待收窄到 30 字以内 |
| P2 | 中文题干配英文原文（读着割裂） | prompt | 待定：按原文语种出题，或题干跟随讲义语种 |
| P2 | ~~思考等级不被模型支持时是静默降级~~ | `app/llm.py` 的 `_create` | 已做（`reasoning_note` 随结果回给前端） |

---

## 11 · 接下来做什么（给接手的人排个序）

按"用户能感觉到 / 改动风险 / 依赖关系"三样排的。**别跳着做**，前面几条会改变后面的实现方式。

### 第一优先：把已经有的做得更可信（各 0.5 天）

1. **出题阶段要有进度**（§9 第一条）。现在 5 节讲义写完、进度停在 82%，然后三十秒不动 ——
   主人会以为卡死了。做法：`make_quiz(llm, lessons, n, page_text, blocks=None, source="", on_step=None)`，
   内部在"审第 i/n 题"时回调一次；`serve.py` 的 `_do_lesson` 把 82% 到 95% 摊开。
2. **成本折成钱**（P1-4 余下）。`store.py` 里已记 `prompt_tokens` / `completion_tokens`，
   加一张 `PRICES = {"deepseek-chat": (输入单价, 输出单价)}`（每百万 token，可从环境变量覆盖），
   在 `/api/lessons/{id}` 和生成结果里各显示一行「这次大约花了 ¥0.03」。
   别把价格写死在 JS 里 —— 换模型就错。
3. **`reasoning_note` 要显示**。`LLM` 在模型不认 `reasoning_effort` 时会静默重发一次并置
   `reasoning_note`，但 `serve.py` 现在没把它带进 `job.result`。用户选了「高」却毫无变化，会以为坏了。

### 第二优先：让"点回原文"更精确（1 天）

4. **bbox 细化到行**。现在同一段里多条 `source_refs` 指向同一个 `bbox`（段落合并后一段几百字），
   点不同 chip 高亮的是同一大片。做法：`_locate` 已经返回首尾块下标，再在其中按字符偏移定位到具体行
   （`Block` 里没存行信息，要么在 `_paragraphs` 合并时留一份行位移表，要么按 `quote` 宽度估）。
   **注意**：这是这一轮唯一会碰数据模型的地方，改完要把 `tests/test_schema.py` 的 bbox 断言一起更新。

### 第三优先：质量硬骨头（各 1–2 天，要先定方案）

5. **PPT 场景的 quote 太短**（9–15 字，校验很勉强）。两条路：
   ① 允许跨行/跨块拼接，`SourceRef` 记 `blocks` 列表 + 每段的字偏移，`verify_refs` 校验"拼接后可还原"；
   ② 给 PPT 单独出「填空/判断」题型，天然不需要长引用。**倾向 ①**，因为它同时修好了教材场景。
6. **公式 OCR**：`λ`→`入` 这类错误没法靠后处理救。可行方向是「公式块交给多模态模型转 LaTeX」，
   但会引入新依赖和新成本，做之前先用 30 页真实课件量一遍到底有多少公式页。

### 不建议现在做

- **多用户 / 登录 / 配额**：当前是单人本地工具，加这些会把 150 条测试的假设全部推翻。
- **换成 React / Vite**：`app/web/` 现在 1200 行，原生 DOM + 事件委托完全够；`python -m app.serve`
  改一行刷新即见效，引构建链等于给自己加 30 秒的反馈延迟。
- **把 `out/index.json` 换 sqlite**：几百条记录，整份读写更简单，也没有并发问题。

---

## 12 · 一句话给接手的人

这个项目的价值全在**「每句话都能点回原文」**上。界面怎么美化都在其次，
只要记住：**任何时候用户在讲义里看到一句判断，都该能在两次点击内看到支撑它的那一行原文。**
做不到这一点，它就是个会把书读薄的聊天框。

