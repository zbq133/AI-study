from app.pipeline import build_lesson
from app.schema import (Block, DocIR, LessonSection, Outline, OutlineItem,
                        QuizItem, QuizPack, SourceRef, VerifyResult)

QUOTE = "这是一句可以在原文里逐字找到的句子"
FAKE = "这句话在原文里根本不存在"


class FakeLLM:
    def __init__(self):
        self.calls = 0

    def json(self, system, user, model_cls, temperature=0.3, retries=2):
        self.calls += 1
        name = getattr(model_cls, "__name__", "")
        if name == "Outline":
            return Outline(sections=[OutlineItem(title="知识点A", points=["考点1"], pages=[1])])
        if name == "LessonSection":
            return LessonSection(title="知识点A", explanation_md="讲解正文 $E=mc^2$",
                                 key_points=["关键点1"], pitfalls=["易错点1"],
                                 source_refs=[SourceRef(page=1, quote=QUOTE),
                                              SourceRef(page=1, quote=FAKE)])
        if name == "QuizPack":
            return QuizPack(items=[
                QuizItem(type="single", stem="好题：下列说法正确的是？",
                         options=["甲", "乙", "丙", "丁"], answer="B",
                         source_ref=SourceRef(page=1, quote=QUOTE)),
                QuizItem(type="single", stem="坏题：下列说法正确的是？",
                         options=["甲", "乙", "丙", "丁"], answer="A",
                         source_ref=SourceRef(page=1, quote=FAKE))])
        if name == "VerifyResult":
            return VerifyResult(answer="B" if "好题" in user else "C")
        raise AssertionError("未预期的 model_cls: " + name)


def make_ir():
    return DocIR(doc_id="d1", filename="t.pdf", page_count=1, blocks=[
        Block(id="b1", kind="heading", text="第1章 绪论", page=1, level=1,
              section_path=["第1章 绪论"]),
        Block(id="b2", kind="para", text=QUOTE, page=1, section_path=["第1章 绪论"])])


def test_build_lesson_drops_fake_refs_and_bad_quiz():
    lesson = build_lesson(FakeLLM(), make_ir(), "第1章", n_quiz=2)
    assert len(lesson.sections) == 1
    assert [r.quote for r in lesson.sections[0].source_refs] == [QUOTE]
    assert len(lesson.quiz) == 1
    assert lesson.quiz[0].stem.startswith("好题")
    assert lesson.quiz[0].verified is True


def test_quiz_retry_does_not_duplicate_items():
    lesson = build_lesson(FakeLLM(), make_ir(), "第1章", n_quiz=5)
    stems = [q.stem for q in lesson.quiz]
    assert len(stems) == len(set(stems))


def test_build_lesson_summary_fields():
    lesson = build_lesson(FakeLLM(), make_ir(), "第1章", n_quiz=2)
    assert lesson.summary_one_liner == "关键点1"
    assert lesson.summary_bullets == ["关键点1"]
    assert lesson.learning_objectives == ["考点1"]


def test_build_lesson_unknown_chapter_raises():
    try:
        build_lesson(FakeLLM(), make_ir(), "第9章", n_quiz=1)
    except ValueError as exc:
        assert "没找到章节" in str(exc)
    else:
        raise AssertionError("应当抛出 ValueError")