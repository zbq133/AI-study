from app.pipeline import _render_sources, _solve_independently, build_lesson
from app.schema import (Block, DocIR, LessonSection, Outline, OutlineItem,
                        QuizItem, QuizPack, SourceRef, VerifyResult, build_sections)

QUOTE = "这是一句可以在原文里逐字找到的句子"
FAKE = "这句话在原文里根本不存在"


class FakeLLM:
    """按内容而非按位置作答：选项被打乱也照样挑中「乙」。"""

    def __init__(self, blind_to=None):
        self.calls = 0
        self.blind_to = blind_to
        self.verify_orders = []
        self.quiz_prompts = []

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
            self.quiz_prompts.append(user)
            return QuizPack(items=[
                QuizItem(type="single", stem="好题：下列说法正确的是？",
                         options=["甲", "乙", "丙", "丁"], answer="B",
                         source_ref=SourceRef(page=1, quote=QUOTE)),
                QuizItem(type="single", stem="坏题：下列说法正确的是？",
                         options=["甲", "乙", "丙", "丁"], answer="A",
                         source_ref=SourceRef(page=1, quote=FAKE))])
        if name == "VerifyResult":
            self.verify_orders.append(user)
            if "好题" not in user or (self.blind_to and self.blind_to in user):
                return VerifyResult(answer="C")
            for line in user.splitlines():
                s = line.strip()
                if len(s) > 3 and s[1] == "." and "乙" in s:
                    return VerifyResult(answer=s[0])
            return VerifyResult(answer="?")
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


def test_quiz_prompt_carries_source_with_page_numbers():
    """出题时原文必须喂进去。

    真跑时三题都给了 source_ref、三题逐字校验全落空：模型被要求"引用原文"，
    但 prompt 里只有讲义，它只能凭记忆把那句话重写一遍再填进去，页码也是编的。
    """
    llm = FakeLLM()
    build_lesson(llm, make_ir(), "第1章", n_quiz=2)
    assert llm.quiz_prompts
    prompt = llm.quiz_prompts[0]
    assert "[p1]" in prompt
    assert QUOTE in prompt


def test_render_sources_covers_only_the_chapter_pages():
    ir = DocIR(doc_id="d1", filename="t.pdf", page_count=2, blocks=[
        Block(id="b1", kind="heading", text="第1章 绪论", page=1, level=1,
              section_path=["第1章 绪论"]),
        Block(id="b2", kind="para", text="第一章的正文", page=1, section_path=["第1章 绪论"]),
        Block(id="b3", kind="heading", text="第2章 收尾", page=2, level=1,
              section_path=["第2章 收尾"]),
        Block(id="b4", kind="para", text="第二章的正文", page=2, section_path=["第2章 收尾"])])
    first = [s for s in build_sections(ir) if s.page_start == 1]
    text = _render_sources(ir, first)
    assert "[p1] 第一章的正文" in text
    assert "第二章的正文" not in text



    lesson = build_lesson(FakeLLM(), make_ir(), "第1章", n_quiz=5)
    stems = [q.stem for q in lesson.quiz]
    assert len(stems) == len(set(stems))


def test_verify_sees_two_different_option_orders():
    """同一道题要被解两次，第二次的选项顺序必须变过。"""
    llm = FakeLLM()
    build_lesson(llm, make_ir(), "第1章", n_quiz=2)
    good = [u for u in llm.verify_orders if "好题" in u]
    assert len(good) == 2
    assert good[0] != good[1]
    assert "A. 甲" in good[0] and "A. 乙" in good[1]


def test_quiz_dropped_when_answer_flips_after_shuffle():
    """打乱后答不上来的题说明是蒙位置，不能算验证通过。"""
    llm = FakeLLM(blind_to="A. 乙")
    lesson = build_lesson(llm, make_ir(), "第1章", n_quiz=2)
    assert lesson.quiz == []


def test_quiz_keeps_item_answered_by_content():
    """答案跟着内容走而不是跟着字母走时，打乱与否都应通过。"""
    lesson = build_lesson(FakeLLM(), make_ir(), "第1章", n_quiz=2)
    assert [q.stem for q in lesson.quiz] == ["好题：下列说法正确的是？"]
    assert lesson.quiz[0].verified is True


class _NoRefLLM(FakeLLM):
    """模型干脆不给出处。

    真跑「第十章 波动」时撞上的正是这种：两道题 `source_ref` 全是 null，
    答案自洽、独立解也通过，于是被当成合格题收进讲义。
    """

    def json(self, system, user, model_cls, temperature=0.3, retries=2):
        if getattr(model_cls, "__name__", "") == "QuizPack":
            self.calls += 1
            return QuizPack(items=[
                QuizItem(type="single", stem="好题但没出处：下列说法正确的是？",
                         options=["甲", "乙", "丙", "丁"], answer="B",
                         source_ref=None)])
        return super().json(system, user, model_cls, temperature, retries)


def test_quiz_dropped_without_source_ref():
    """没有出处的题学生点不回原文，要和「出处对不上」一样丢弃。

    这道题答案自洽、两次独立解都能通过，唯一的毛病是没给 quote ——
    如果只在「给了 ref 但校验失败」时才丢，它就会漏进讲义。
    """
    lesson = build_lesson(_NoRefLLM(), make_ir(), "第1章", n_quiz=2)
    assert lesson.quiz == []


class _Echo:
    def __init__(self, answer):
        self.answer = answer

    def json(self, system, user, model_cls, temperature=0.3, retries=2):
        assert model_cls is VerifyResult
        return VerifyResult(answer=self.answer)


def test_fill_item_compares_text_not_letters():
    """填空题答案里有字母时不能被抠成选项字母。"""
    q = QuizItem(type="fill", stem="写出质能方程", answer="E=mc^2")
    assert _solve_independently(_Echo(" E = mc^2 "), q) is True
    assert _solve_independently(_Echo("F=ma"), q) is False
    assert _solve_independently(_Echo(""), q) is False


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