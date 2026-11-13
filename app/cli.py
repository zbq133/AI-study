from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .errors import StudyError, describe
from .llm import LLM
from .pdf import extract_ir
from .pipeline import build_lesson
from .schema import build_sections, is_reference_title

load_dotenv()
REQUIRED = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")


def _report(exc: BaseException) -> None:
    """把失败说成人话：title 一句话 + hint 怎么办；认不出的错误才亮 detail。"""
    info = describe(exc)
    print(f"{info['title']}")
    if info["hint"]:
        print(info["hint"])
    if info["kind"] == "unknown":
        print(info["detail"])


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python -m app.cli <pdf路径> [章节名或序号] [题数]")
        return 2
    pdf_path = sys.argv[1]
    chapter = sys.argv[2] if len(sys.argv) > 2 else ""
    try:
        n = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    except ValueError:
        print(f"题数要写数字：{sys.argv[3]}")
        return 2

    try:
        ir = extract_ir(pdf_path)
    except StudyError as exc:
        print()
        _report(exc)
        return 4
    print(f"解析完成：{ir.page_count} 页 / {len(ir.blocks)} 个块")

    secs = build_sections(ir)
    shown = [(i, s) for i, s in enumerate(secs, 1) if not is_reference_title(s.title)]
    print(f"可用章节（共 {len(shown)} 条）：")
    # 全部列出来，别只印前 20 条：序号本来就允许选到 len(secs)，印 20 条会让人
    # 以为这本书只有 20 章，后面的章节"不存在"。
    for i, s in shown:
        print(f"  {i:>2}. {' > '.join(s.path)}  p{s.page_start}-{s.page_end}")
    skipped = len(secs) - len(shown)
    if skipped:
        print(f"  （另有 {skipped} 条参考文献段已隐藏，序号仍是它在全书里的位置）")

    if not chapter:
        print("\n给个章节名或序号再跑一遍就会生成讲义，例如：")
        print('  python -m app.cli "<pdf路径>" "第1章" 5')
        return 0

    section_n: int | None = None
    if chapter.isdigit():                      # 允许直接写列表里的序号
        idx = int(chapter)
        if not 1 <= idx <= len(secs):
            print(f"\n序号 {idx} 超出范围 1-{len(secs)}。")
            return 5
        section_n = idx
        chapter = secs[idx - 1].title

    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        print("\n缺少环境变量：" + ", ".join(missing))
        print("把 .env.example 复制成 .env 填好 key 再跑（解析阶段不受影响）。")
        return 3

    llm = LLM(os.environ["LLM_BASE_URL"], os.environ["LLM_API_KEY"],
              os.environ["LLM_MODEL"])
    try:
        kwargs = {"section_n": section_n} if section_n is not None else {}
        lesson = build_lesson(llm, ir, chapter, n, **kwargs)
    except StudyError as exc:
        print()
        _report(exc)
        return 5

    out = Path("out") / f"{Path(pdf_path).stem}_{chapter}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(lesson.model_dump_json(indent=2), encoding="utf-8")

    print(f"\n讲义已生成：{out}")
    print(f"调用 {llm.calls} 次，输入 {llm.prompt_tokens} / 输出 {llm.completion_tokens} tokens")
    print(f"习题 {len(lesson.quiz)} 道（已通过独立解题验证）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())