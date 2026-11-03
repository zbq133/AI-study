# 协作铁律

> **接手前先读 `GEMINI.md`（纪律 + 现状 + 下一棒）和 `docs/ROADMAP.md`（架构 / 硬约束 / 已知问题 /
> §11 接下来做什么）。** 这份文件只留最小骨架，方便 Antigravity / Gemini CLI / Claude Code
> 这类会自动读 `AGENTS.md` 的 agent 一眼看到底线。

1. 改前先提交：任何改动开始前先 git commit，保留可回滚点。
2. 改后必测：每次改动都要新增或更新测试；交付前 python -m pytest -q 必须全绿（213 passed + 3 skipped）。
3. 测试不全绿的代码不交付，也不进入下一次改动。
4. 做变异验证：把新写的判据故意改坏一条，确认对应测试变红；还是绿的说明断言是空的。
5. 提交信息用中文，前缀 feat: / fix: / test: / chore: / docs:
6. 改文件用编辑器（保持 LF、无 BOM），别用 PowerShell 的 Set-Content / Add-Content。
7. .env、out/、settings.json 不进仓库。

## 常用命令

    .\.venv\Scripts\python.exe -m pytest -q
    .\.venv\Scripts\python.exe -m app.serve      # → http://127.0.0.1:8765
    .\.venv\Scripts\python.exe -m app.cli "D:\path\a.pdf" "第3章" 5
    git log --oneline
