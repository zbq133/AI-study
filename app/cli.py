from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .llm import LLM
from .pdf import extract_ir
from .pipeline import build_lesson
from .schema import build_sections

load_dotenv()
REQUIRED = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: python -m app.cli <pdf路径> <章节名> [题数]")
        return 2
    pdf_path, chapter = sys.argv[1], sys.argv[2]
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 5

    ir = extract_ir(pdf_path)
    print(f"解析完成：{ir.page_count} 页 / {len(ir.blocks)} 个块")

    secs = build_sections(ir)
    print("可用章节（前 20 条）：")
    for s in secs[:20]:
        print("   ", " > ".join(s.path), f"p{s.page_start}-{s.page_end}")

    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        print("\n缺少环境变量：" + ", ".join(missing))
        print("把 .env.example 复制成 .env 填好 key 再跑（解析阶段不受影响）。")
        return 3

    llm = LLM(os.environ["LLM_BASE_URL"], os.environ["LLM_API_KEY"],
              os.environ["LLM_MODEL"])
    lesson = build_lesson(llm, ir, chapter, n)

    out = Path("out") / f"{Path(pdf_path).stem}_{chapter}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(lesson.model_dump_json(indent=2), encoding="utf-8")

    print(f"\n讲义已生成：{out}")
    print(f"调用 {llm.calls} 次，输入 {llm.prompt_tokens} / 输出 {llm.completion_tokens} tokens")
    print(f"习题 {len(lesson.quiz)} 道（已通过独立解题验证）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())