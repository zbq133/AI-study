# AI Study

把大学课件（PDF，后续 PPT / Word / 图片）变成不复述原文的讲义，以及答案经过独立验证的习题。

流程：PDF 文字层（没有就 OCR）-> IR -> 章节切分 -> 大纲规划 -> 逐节讲解 -> 出题 -> 独立解题验证。

**卖点不是"能生成"，是每句话都能点回原文**：讲义的每条出处都记着页码、落在哪几个块、矩形坐标，
右边栏把原文按页摊开，点一下「原文 p5」就跳到那一行。编出来的引用会被逐字校验打掉，题也一起扔。

## 第一次

    python -m venv .venv                # 需要 Python 3.12（见下）
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    copy .env.example .env              # 填 DeepSeek 的 key

> 为什么不能用 3.15：`onnxruntime`（OCR 用的）还没有 3.15 的轮子，`pip install` 会
> `ResolutionImpossible`。3.13 暂时装不上（rapidocr-onnxruntime 要求 <3.13）。macOS / Linux 把 `.venv\Scripts\python.exe`
> 换成 `.venv/bin/python` 即可。

## 用客户端（不碰浏览器）

    .\.venv\Scripts\python.exe -m app.desktop

双击就是一扇窗口：没有地址栏、没有标签页，关掉窗口进程就退。它做的事很简单 —— 在后台把
本地服务起在那个端口上，然后开一个 Windows 原生窗口（WebView2）指过去。

- 再双击一次不会起第二份服务：端口上已经有本程序在跑就只开窗口，复用那份数据。
- 端口被别的程序占着就自动换一个空闲的，不用你操心。
- 主题 / 字号 / 折叠状态存在 `~/.ai-study/webview`（普通浏览器是它自己的 localStorage），
  所以客户端和网页版的界面偏好是分开的两套。
- 讲义里的文字可以选中复制。

**打成 exe 发给别人**：

    powershell -ExecutionPolicy Bypass -File build_desktop.ps1
    # → dist\AI Study\AI Study.exe（连目录一起拷；数据在 exe 旁边生成）

用目录版（onedir）而不是单文件：这程序带着 onnxruntime 和 PyMuPDF，单文件版每次启动都要先解
几百 MB 到临时目录，冷启动十几秒，杀毒软件还会盯着解压动作。目录版是秒开。


    .\start.ps1            # Windows；macOS / Linux 用 ./start.sh

打开 <http://127.0.0.1:8765>：

1. 选一个 PDF 上传 → 自动解析（扫描件会走 OCR，一页两三秒）
2. 左栏点一个章节 → 右栏出现这一章的原文
3. 填题数 → 生成。进度条走的是真进度（大纲 → 第 n/N 节 → 出题），中途可以点「停」
4. 讲义里每条「原文 pN」都能点，右边跟着高亮；习题先自己选，点提交才判对错
5. 左栏「最近生成」存着以前做过的讲义（`out/index.json`），重启服务也还在；每条都标了占用大小，点「管理」可以勾着批量删（勾上「顺带删掉没在用的 PDF」能真腾出空间）
6. 左下角 ⚙ **设置**：五个标签 —— 模型接入（地址 / 密钥 / 模型下拉 / 思考等级）、语言风格（严谨认真 / 生动风趣 / 幽默诙谐）、颜色（深色 / 浅色 / 自定义）、字号（小 / 中 / 大，立刻生效）、版本（v1.1 + 「更新」按钮）
7. 顶栏「导出讲义」把当前这份存成 PDF（A4，带公式、出处页码和习题答案），
   走的是系统下载目录；客户端和网页版都能用

设置写进项目根的 `settings.json`（已在 `.gitignore`），优先级高于 `.env`；
界面上永远只回 `sk-…ec75` 这种提示，**不会回明文密钥**。改完不用重启。

服务只监听 `127.0.0.1`，不对外。要给别人用，得自己架在反向代理后面并加鉴权 —— 现在没有登录。

`GET /api/health` 是探活接口（版本、运行时长、任务数、历史数、成本上限），
日志落在 `out/server.log`（2 MB 轮转，留两份），出问题先看它，别只靠终端里滚过去的报错。

## 用命令行

    .\.venv\Scripts\python.exe -m app.cli "D:\path\a.pdf"           # 只给 PDF：先看可用章节
    .\.venv\Scripts\python.exe -m app.cli "D:\path\a.pdf" "第3章" 5
    .\.venv\Scripts\python.exe -m app.cli "D:\path\a.pdf" 18 3      # 也可以直接填章节序号

只给 PDF 会先打印「可用章节」然后停下，等你填章节名再跑一遍；
章节名对不上、模型出错都会用人话报错（不再甩 traceback）。

## 环境变量

放项目根的 `.env`（`python-dotenv` 启动时读，网页和命令行都吃）。改完要重启服务。

| 变量 | 必填 | 说明 |
|---|---|---|
| `LLM_BASE_URL` | 是 | 例：`https://api.deepseek.com` |
| `LLM_API_KEY` | 是 | 别提交进仓库（`.env` 已在 `.gitignore`） |
| `LLM_MODEL` | 是 | 例：`deepseek-chat` |
| `LESSON_MAX_CALLS` | 否 | 一次生成最多调几次模型，默认 `40`；到顶就停并说清楚 |
| `LESSON_MAX_TOKENS` | 否 | 一次生成最多烧多少 token，默认 `200000` |
| `LLM_REASONING_EFFORT` | 否 | 思考等级 `low` / `medium` / `high`；模型不认时会自动按默认重发一次 |

上限在**每次调用之前**查，所以停下时不会再多花一次钱。写歪了（不是正整数）退回默认值 ——
配置错的表现应该是"按默认跑"，不是"起不来"。

## 测试

    .\.venv\Scripts\python.exe -m pytest -q

214 条通过 + 3 条跳过，全部离线（模型用假桩）。行覆盖 **93%**，覆盖解析、章节切分、引用校验、
生成管线、Web 端点、前端判分逻辑、设置、索引、错误分类、预算与取消、客户端外壳。
真模型的回归要手动开：`STUDY_LIVE=1` + 有 key 时 `tests/test_live.py` 才会跑真实链路（会花钱）。

## 目录

    app/
      pdf.py        PDF → Block（双栏排序、段落合并、跨页页眉摘除、OCR 兜底）
      ocr.py        RapidOCR 包一层，整页图转成和文字层同构的行
      schema.py     Block / DocIR / SourceRef / Lesson，章节切分与引用校验
      pipeline.py   大纲 → 逐节讲解 → 出题 → 打乱选项独立重解
      prompts.py    三份系统提示词
      llm.py        OpenAI 兼容客户端 + 校验失败回灌重试 + 成本闸 + 取消 + 思考等级降级
      errors.py     失败分类：kind / title / hint
      settings.py   设置优先级 settings.json > 环境变量，永不回明文密钥
      store.py      out/index.json 索引（原子写）
      serve.py      FastAPI（上传、解析、布局、生成、SSE 进度、历史、设置、探活）
      desktop.py    客户端外壳：后台起服务 + pywebview 原生窗口
      cli.py        命令行入口
      web/          三栏界面（原生 DOM，无构建链）+ grade.js + vendor/katex
    tests/          214 passed + 3 skipped（共 217 条）
    docs/
      ROADMAP.md    现状、硬约束、完整计划、已知问题、接下来做什么
      DECISIONS.md  为什么这么做（每条都带踩过的坑）
      GEMINI-SETUP.md  用别的模型接手时的环境说明
    GEMINI.md       接手纪律 + 现状 + 下一棒（Gemini CLI 会自动读）
    out/            成品：讲义 JSON + index.json + server.log（不进仓库）
    uploads/        上传的 PDF（不进仓库）

## 常见问题

**扫描件解析出来是空的 / 报"这份 PDF 里没有文字"**
PPT 导出的图片课件没有文字层，走 OCR。一台老机器上一页要两三秒，整本十几分钟；进度会卡在
"解析"阶段不动是正常的（还没有页级进度）。

**公式在讲义里能显示，在原文栏里是 `λ`/`入` 混着的**
原文栏显示的是 OCR 认出来的字，OCR 认数学符号本来就不可靠。讲义的公式是模型按上下文重写的，
所以能显示；但**别拿 OCR 的公式当校对依据**。

**习题一题都没有**
出题要求逐字引用原文，抄不出来就扔。PPT 类课件一页只有几十字，最长的连续片段也就十几字，
这类材料出题本来就难 —— 宁可空着，也不给一道出处是编的题。

**生成到一半报"超出这次的成本上限"**
`LESSON_MAX_CALLS` / `LESSON_MAX_TOKENS` 到了。调大环境变量重跑，或者把题数调小。

**端口被占**
`Get-NetTCPConnection -LocalPort 8765 -State Listen` 看是谁，停掉再起。之前起过的进程没退干净
最常见 —— 页面看着是新的（HTML 每次读盘），但接口还是旧的。
