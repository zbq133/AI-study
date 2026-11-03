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
| 代码 | `app/` 7 个 Python 模块约 1650 行 + `app/web/` 三个静态文件约 8 KB，`tests/` 72 条测试全绿 |
| 后端内核 | **约 90%**：解析 → 切章 → 大纲 → 逐节讲 → 出题 → 独立验证 → 落盘，端到端真跑通过 |
| 外壳 | **约 65%**：CLI + 本地 Web 服务（`app/serve.py`，上传/解析/生成/SSE 进度/静态资源） |
| 界面 | **约 15%**：一个单页表单 + 进度条 + `<pre>` 结果，能用但不像产品（已拆成 `app/web/`，改起来不再是在 Python 字符串里摸） |
| 最新 commit | `e1e28a7 feat: 出处记下落在哪几个块和它的矩形` |
| 运行环境 | Python **3.12**（3.15 装不上 onnxruntime，见 §4） |

**真跑实测数据**（`12. Summary and recommendations`，15 页双栏综述）：

```
解析  2.8 秒 → 711 块 / 23 章（原生文字层，没走 OCR）
生成 45.6 秒 → 13 次模型调用 / 输入 19136 / 输出 6883 tokens
结果  5 节讲义 · 8 条"能…"学习目标 · 3 道题全部通过独立重解验证
出处  quote 179 / 192 / 146 字，都是完整英文句子
```

跑起来的两种方式：

```powershell
# 网页（推荐）
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
| `app/serve.py` | `app`（FastAPI） | 本地 Web 服务 |

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

### P0 — 不做就没法继续

#### P0-1 `SourceRef` 补上 bbox（前端高亮的唯一前置）

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
- 端点：`GET /api/lessons?q=&limit=`、`GET /api/lessons/{id}`（记录 + 讲义本体）、`DELETE /api/lessons/{id}`（连文件一起删）。
- 记录在、文件被手删 → `410 讲义文件被删了，重新生成一次`，不是 500。
- 左栏顶部加"最近生成"，点一条就把讲义渲染出来；**原文栏只在原 PDF 还在内存里时才有内容**（重启后 doc_id 就没了，这时列表能看、出处点不回原文）。

**验收**：重启服务后历史记录还在，能量出来、能打开。

---

### P1 — 用起来像个产品

#### P1-1 三栏界面 + 原文高亮联动

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

| # | 改动 | 所属 | 破坏性 |
|---|---|---|---|
| 0 | `GET /static/{name}` 发 `app/web/` 里的 css/js | ✅ P0-2 已完成 | 新增 |
| 1 | `SourceRef` 加 `blocks: list[str]`、`bbox` | ✅ P0-1 已完成 | 否（有默认值） |
| 2 | `_reading_order_ok` → `locate_quote` 返回位置 | ✅ P0-1 已完成 | 内部函数，不影响外部 |
| 3 | `GET /api/docs/{id}/layout` 返回本章页的 blocks | ✅ P1-1 已完成 | 新增 |
| 4 | `POST /api/jobs/{id}/cancel` | P1-3 | 新增 |
| 5 | `GET /api/lessons` 列表 / `GET /api/lessons/{id}` | P0-3 | 新增 |
| 6 | `on_step` 增加 `stage="verify"` / `"ocr"` | 6.5 | 否 |
| 7 | `make_quiz(..., on_step=None)` 新参数 | 6.5 | 否（默认 None） |

---

## 8 · 协作规范

> 如果接手的是 **Gemini CLI**，项目根目录的 `GEMINI.md` 会被它自动读走 —— 那份是同内容的精简纪律版
> （铁律 + 第一棒任务 + 禁止事项），这份 ROADMAP 是完整版。两份一起改，别只改一份。

**接手前**（30 分钟）：

```powershell
cd "D:\AI study"
.\.venv\Scripts\python.exe -m pytest -q          # 必须 68 passed（P0-1 完成时）
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
| P0 | ~~`SourceRef` 没有 bbox，前端无法高亮~~ | `app/schema.py:71` | 已做（`blocks` + `bbox`，e1e28a7） |
| ~~P1~~ | ~~讲义里的 `$公式$` 在界面上原样显示~~ | `app/web/` | 已接本地 KaTeX（离线 vendor） |
| P1 | ~~生成不能取消~~ | `app/serve.py` | 已做（`cancel_evt` + 调用前查，P1-3） |
| P1 | ~~错误只有一句 traceback~~ | `app/errors.py` | 已做（kind + title + hint，P1-5） |
| P1 | ~~历史记录无索引，重启即丢~~ | `app/store.py` | 已做（`out/index.json`，P0-3） |
| P1 | 出题阶段（30 秒）进度不动 | `app/pipeline.py:136` | 待做 |
| P1 | OCR 阶段无页级进度 | `app/ocr.py` | 待做 |
| P1 | 成本只显示调用次数，没折成钱 | `app/web/app.js` | 待做（P1-4 余下部分） |
| P2 | PPT 场景 quote 只有 9–15 字 | 校验语义 | 待定方案（P2-1） |
| P2 | 公式 OCR 错认（`λ`→`入`） | `app/ocr.py` | 待定方案（P2-2） |
| P2 | `knowledge_point` 可能长到 48 字 | prompt | 待收窄 |
| P2 | 中文题干配英文原文（读着割裂） | prompt | 待定 |

---

## 10 · 一句话给接手的人

这个项目的价值全在**「每句话都能点回原文」**上。界面怎么美化都在其次，
只要记住：**任何时候用户在讲义里看到一句判断，都该能在两次点击内看到支撑它的那一行原文。**
做不到这一点，它就是个会把书读薄的聊天框。
