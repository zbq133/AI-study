# AI Study

把大学课件（PDF，后续 PPT / Word / 图片）变成不复述原文的讲义，以及答案经过独立验证的习题。

流程：PDF 文字层 -> IR -> 章节切分 -> 大纲规划 -> 逐节讲解 -> 出题 -> 独立解题验证。

## 快速开始

    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    copy .env.example .env      # 填 DeepSeek key
    .\.venv\Scripts\python.exe -m app.cli "D:\path\a.pdf" "第3章" 5

先看它打印的「可用章节」；列表乱了先调解析器，别急着往下跑。

## 测试

    .\.venv\Scripts\python.exe -m pytest -q