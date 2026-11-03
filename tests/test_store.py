"""索引本身的单测：能读、能写、坏了不炸、同章覆盖。"""
import json

from app.store import Store, make_id


def test_make_id_is_stable_and_distinct():
    a = make_id("doc1", "第1章")
    assert a == make_id("doc1", "第1章")
    assert a != make_id("doc1", "第2章")
    assert a != make_id("doc2", "第1章")
    assert len(a) == 12


def test_put_then_list_round_trip(tmp_path):
    s = Store(tmp_path)
    s.put({"id": "x1", "chapter": "第一章 绪论", "filename": "a.pdf",
           "file": "a_第一章 绪论.json", "sections": 5, "quiz": 3})
    rows = s.list()
    assert len(rows) == 1
    assert rows[0]["chapter"] == "第一章 绪论"
    assert rows[0]["created"] and rows[0]["updated"]
    assert json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))[0]["id"] == "x1"


def test_same_chapter_overwrites_instead_of_piling_up(tmp_path):
    s = Store(tmp_path)
    s.put({"id": "x1", "chapter": "第一章", "quiz": 1})
    first = s.list()[0]["created"]
    s.put({"id": "x1", "chapter": "第一章", "quiz": 5})
    rows = s.list()
    assert len(rows) == 1
    assert rows[0]["quiz"] == 5
    assert rows[0]["created"] == first          # 首次生成时间保留


def test_newest_first(tmp_path):
    s = Store(tmp_path)
    s.put({"id": "old", "chapter": "旧"})
    s.put({"id": "new", "chapter": "新"})
    # updated 精度是秒，手动改一下保证顺序稳定
    rows = s._read()
    for r in rows:
        r["updated"] = "2020-01-01T00:00:00" if r["id"] == "old" else "2021-01-01T00:00:00"
    s._write(rows)
    assert [r["id"] for r in s.list()] == ["new", "old"]


def test_filter_by_chapter_or_filename(tmp_path):
    s = Store(tmp_path)
    s.put({"id": "a", "chapter": "机械波", "filename": "波动.pdf"})
    s.put({"id": "b", "chapter": "静电场", "filename": "电磁.pdf"})
    assert [r["id"] for r in s.list("波动")] == ["a"]
    assert [r["id"] for r in s.list("电磁")] == ["b"]
    assert s.list("不存在") == []


def test_broken_index_is_treated_as_empty(tmp_path):
    """手改坏了索引不能让服务起不来 —— 下次写入会重排。"""
    (tmp_path / "index.json").write_text("{ 这不是 JSON", encoding="utf-8")
    s = Store(tmp_path)
    assert s.list() == []
    s.put({"id": "a", "chapter": "第一章"})
    assert len(s.list()) == 1


def test_delete(tmp_path):
    s = Store(tmp_path)
    s.put({"id": "a", "chapter": "第一章"})
    assert s.delete("a") is True
    assert s.delete("a") is False
    assert s.list() == []


def test_file_of_refuses_to_leave_the_output_dir(tmp_path):
    """记录里的 file 字段被写成 ../ 也不能读到外面去。"""
    s = Store(tmp_path)
    outside = tmp_path.parent / "secret.json"
    outside.write_text("{}", encoding="utf-8")
    s.put({"id": "a", "chapter": "第一章", "file": "../secret.json"})
    assert s.file_of("a") is None


def test_file_of_returns_none_when_the_file_was_removed(tmp_path):
    s = Store(tmp_path)
    s.put({"id": "a", "chapter": "第一章", "file": "gone.json"})
    assert s.file_of("a") is None


def test_file_of_finds_the_real_file(tmp_path):
    s = Store(tmp_path)
    (tmp_path / "x.json").write_text("{}", encoding="utf-8")
    s.put({"id": "a", "chapter": "第一章", "file": "x.json"})
    assert s.file_of("a") == tmp_path / "x.json"
