# GEMINI.md

> 这个项目由一位人类 + 多个模型轮值开发。前面的工作已经跑通、并被 87 条测试锁住。
> **接手的第一个动作：读完本文件，再读 `docs/ROADMAP.md`，再动代码。**
>
> 你的前任留下一份交接文档 `docs/ROADMAP.md`（十节：现状 / 架构 / 踩过的坑 / 硬约束 /
> 三阶段计划 / 前端建议 / API 改动清单 / 协作规范 / 已知问题）。那份是主文档，本文件是纪律。

---

## 一句话项目

把大学课件 PDF（教材章节 / 论文 / PPT 导出的图片课件）变成两份东西：**讲义**（不复述原文，
讲清为什么，每句带出处）和**习题**（答案经打乱选项重解验证，出处必须能在原文逐字找到）。

卖点不是"AI 会讲"，是**"AI 讲的每句话你都能点回去看原文"**。

---

## 铁律（违反任何一条，改动会被退回）

1. **改前先提交。** 任何改动开始前先 `git commit`，保留可回滚点。
2. **改后必测。** 新逻辑配新测试；交付前 `.\.venv\Scripts\python.exe -m pytest -q` 必须全绿。
3. **做变异验证。** 故意把新写的判据改坏一条，确认对应测试变红。
   测试全绿但你把它改坏了还绿 —— 那是空断言，等于没测。
   ```
   string replace 源码 → 跑 pytest → 必须非 0 → write_bytes 还原原文件
   ```
4. **提交信息用中文**，前缀 `feat:` / `fix:` / `test:` / `chore:` / `docs:`。
5. **改文件用 `edit` 工具**，别用 PowerShell 的 `Set-Content` / `Select-String -Replace`：
   它会把 LF 变 CRLF 并加 BOM，`git diff` 直接整文件翻红。万不得已要按 bytes 读写：
   ```python
   p.write_bytes(p.read_bytes().replace(b"\r\n", b"\n"))
   ```
6. **别为了出题数量放松校验。** 题少是对的，题假才是坏的。
7. **`.env` 和 `out/` 不进仓库。**

---

## 先读这些（按顺序）

| # | 文件 | 看什么 |
|---|---|---|
| 1 | `docs/ROADMAP.md` | 主交接文档，尤其 §3「已经踩平的地」和 §4「硬约束」 |
| 2 | `docs/DECISIONS.md` | 关键决策的来龙去脉：为什么段落要合并、为什么出题要喂原文 |
| 3 | `app/schema.py` | 数据模型 + 出处校验（`locate_quote` 就在这改） |
| 4 | `tests/` 里的测试名 | 87 条测试的名字就是"哪些行为被锁住了"的清单 |

---

## 现状（截至 commit `4a5d53e`）

- `app/` 7 个模块约 1650 行：`pdf.py`（解析/排序/合并段落/判标题）、`ocr.py`（RapidOCR 兜底）、
  `schema.py`（模型 + 切章 + 校验）、`pipeline.py`（全流程编排）、`prompts.py`、`llm.py`、
  `cli.py`、`serve.py`（FastAPI 本地服务）。
- `tests/` 56 条，全绿。
- 后端内核约 90%，Web 外壳约 60%，**界面约 15%**（一个单页表单 + 进度条 + `<pre>` 结果）。

真跑实测（15 页双栏综述 `12. Summary and recommendations`）：

```
解析  2.8 秒 → 711 块 / 23 章
生成 45.6 秒 → 13 次调用 / 输入 19136 / 输出 6883 tokens
结果  5 节讲义 · 8 条"能…"学习目标 · 3 题全过独立重解验证
出处  quote 179 / 192 / 146 字，都是完整英文句子
```

跑起来：

```powershell
cd "D:\AI study"
.\.venv\Scripts\python.exe -m pytest -q          # 必须 68 passed
.\.venv\Scripts\python.exe -m app.serve          # → http://127.0.0.1:8765
```

---

## ~~交接第一棒：P0-1 给 `SourceRef` 补 bbox~~（已完成，commit 见 git log）

**为什么**：`SourceRef` 只有 `page` + `quote`。前端想"点出处跳到原文并高亮那一行"需要坐标。
`Block.bbox` 明明有，但 `verify_refs` 校验完就把位置丢了 —— 而校验过程本身**已经知道** quote
落在哪几行。

**落地形态**（比草案改了两处）：

```python
class SourceRef(BaseModel):
    page: int
    quote: str
    blocks: list[str] = []                                    # 落在哪些块（记 Block.id，不是下标）
    bbox: Optional[tuple[float, float, float, float]] = None  # 合并后的外框
```

- `_reading_order_ok` 拆成 `_locate(needle, blocks) -> tuple[int, int] | None`（返回首尾下标）
  + `_reading_order_ok`（薄包装，保留旧名字给旧调用点）+ `locate_quote(needle, blocks) -> list[int]`
  （返回下标列表，调用方要的是"哪几块"）+ `_merge_bbox(blocks, idxs)`。
- `blocks` 存 `Block.id` 而不是下标：跨页/重排后下标会漂，id 不会；调试时也一眼能对上。
- `verify_refs` 命中后返回**新的** `SourceRef`（带 blocks+bbox），不再是原对象直通。
  `make_quiz` 因此要把返回值赋回 `q.source_ref = ok[0]`，否则 enrich 过的结果丢了。

**验收（已达成）**：`tests/test_schema.py` 四条 + `tests/test_pipeline.py` 两条断言 +
`tests/test_serve.py` 断言落盘 JSON 带 blocks/bbox；变异四条全部击杀。

下一棒是 §5 的 P0-2（拆静态文件）、P0-3（生成记录索引），然后是 P1 的三栏界面。

---

## 本机运行环境（别改）

- `gemini` 0.66.0 已装好，`cd "D:\AI study"; gemini` 直接可用，**不需要** `--model` 或任何参数。
  配置细节见 `docs/GEMINI-SETUP.md`。
- 模型固定 `gemini-3-flash-preview`。`gemini-2.5-flash` 对新用户已停（404），别改回去。
- **免费层 5 次请求/分钟。** 撞了会原地等 46 秒。所以：一次给一个完整的小目标，
  别用试探式的一问一答;  把方案先写进文件再执行;  重活交给 `python -m app.cli` 和
  `python -m app.serve`（本地算力，不烧配额）。
- 网络走 `127.0.0.1:7897` 代理，已配在用户环境变量里。

## 明确不要做的事

| 别做 | 为什么 |
|---|---|
| 引 Vite / React / npm 构建链 | 现在 `python -m app.serve` → 刷新即见效。前端复杂度（三栏 + 高亮 + 自测）原生 DOM 够用。超过 2000 行再议。 |
| 把 `_paragraphs` / `_order` / `_toc_key` 简化掉 | 每一条都是真 PDF 上暴露过 bug 才加的，对应测试锁着。看不懂就先看 `docs/ROADMAP.md` §3。 |
| 把 PPT 课件按页切章 | PPT 一页 50~90 字，按页切会让模型拿 246 字的考纲讲一整节 → 膨胀 26.4 倍。必须整本一章。 |
| 用 OCR 文本校验公式 | `λ`→`入`、`ν`→`v` 是常态。`DocIR.ocr_pages` 记着哪些页是识别出来的。 |
| 改成全异步 / 换掉 ThreadPoolExecutor | 一次生成 40~70 秒，现在的线程池 + SSE 已经够，改 asyncio 只会引入新坑。 |
| 加"更聪明的"内容过滤 / 温和话术 | 这个项目里没有这种东西的容身之处。 |
| 直接改 `out/` 里的产物当数据源 | 产物是结果，不是状态。状态要进 `out/index.json`（P0-3 待做）。 |

---

## 完成标准

一次改动算完成，当且仅当：

1. `.\.venv\Scripts\python.exe -m pytest -q` 退出码 **0**；
2. 新判据做过变异验证，对应的测试确实会变红；
3. `git status` 干净，提交信息是中文且前缀正确；
4. `docs/ROADMAP.md` 的「已知问题清单」里，被你解决的那一条状态同步更新。
