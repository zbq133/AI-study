# 协作铁律

1. 改前先提交：任何改动开始前先 git commit，保留可回滚点。
2. 改后必测：每次改动都要新增或更新测试；交付前 python -m pytest -q 必须全绿。
3. 测试不全绿的代码不交付，也不进入下一次改动。
4. 提交信息用中文，前缀 feat: / fix: / test: / chore: / docs:
5. .env、out/ 不进仓库。

## 常用命令

    .\.venv\Scripts\python.exe -m pytest -q
    .\.venv\Scripts\python.exe -m app.cli "D:\path\a.pdf" "第3章" 5
    git log --oneline