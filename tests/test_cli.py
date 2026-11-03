"""CLI 的错误处理和参数解析：人话报错、不给章节名先列目录。"""
import sys

from app import cli
from app.errors import ChapterNotFound
from app.schema import Block, DocIR


QUOTE = "这是一句可以在原文里逐字找到的句子"


def _ir():
    return DocIR(doc_id="d1", filename="t.pdf", page_count=1, blocks=[
        Block(id="b1", kind="heading", text="第1章 绪论", page=1, level=1,
              section_path=["第1章 绪论"]),
        Block(id="b2", kind="para", text=QUOTE, page=1,
              section_path=["第1章 绪论"])])


class _FakeLLM:
    def __init__(self, *args, **kwargs):
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0


def _setup(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://x")
    monkeypatch.setenv("LLM_API_KEY", "sk-x")
    monkeypatch.setenv("LLM_MODEL", "m1")
    monkeypatch.setattr(cli, "extract_ir", lambda path: _ir())
    monkeypatch.setattr(cli, "LLM", _FakeLLM)


def test_no_chapter_lists_sections_then_stops(capsys, monkeypatch):
    """只给 PDF 不填章节名：打印可用章节后停下，不再甩用法/报错。"""
    _setup(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["app.cli", "t.pdf"])
    rc = cli.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "可用章节" in out
    assert "第1章 绪论" in out
    assert "给个章节名" in out


def test_unknown_chapter_reports_nicely(capsys, monkeypatch):
    """章节名对不上：打印 title + hint，不再甩 traceback。"""
    _setup(monkeypatch)

    def boom(*args, **kwargs):
        raise ChapterNotFound("没找到章节：第9章")

    monkeypatch.setattr(cli, "build_lesson", boom)
    monkeypatch.setattr(sys, "argv", ["app.cli", "t.pdf", "第9章"])
    rc = cli.main()
    out = capsys.readouterr().out
    assert rc == 5
    assert "没找到这一章" in out               # ChapterNotFound.title
    assert ChapterNotFound.hint in out          # hint 也打出来
    assert "Traceback" not in out


def test_bad_quiz_count_reports_nicely(capsys, monkeypatch):
    """题数不是数字：人话提示，不裸 ValueError。"""
    _setup(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["app.cli", "t.pdf", "第1章", "abc"])
    rc = cli.main()
    out = capsys.readouterr().out
    assert rc == 2
    assert "题数要写数字" in out
    assert "Traceback" not in out
