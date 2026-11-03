"""设置文件本身的行为：存哪、谁优先、密钥会不会漏出去。"""

import json

import pytest

from app import settings as conf


@pytest.fixture()
def box(tmp_path, monkeypatch):
    """把配置文件挪到临时目录，并清掉自己机器上的环境变量（.env 会被 load_dotenv 读进来）。"""
    monkeypatch.setattr(conf, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(conf, "_CACHE_KEY", ("", -2, -2))   # 缓存别跨用例串
    for name in conf.ENV_OF.values():
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_save_then_load_round_trip(box):
    conf.save({"base_url": "https://api.deepseek.com", "model": "deepseek-chat"})
    got = conf.load()
    assert got["base_url"] == "https://api.deepseek.com"
    assert got["model"] == "deepseek-chat"


def test_empty_value_means_fall_back_to_env(box, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "from-env")
    conf.save({"model": "manual"})
    assert conf.effective()["model"] == "manual"
    conf.save({"model": ""})                 # 清空 = 让位给 .env
    assert conf.effective()["model"] == "from-env"
    assert "model" not in conf.load()


def test_settings_win_over_env(box, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://env.example")
    conf.save({"base_url": "https://file.example"})
    assert conf.effective()["base_url"] == "https://file.example"
    assert conf.source_of("base_url") == "settings"


def test_public_never_returns_the_key(box):
    conf.save({"api_key": "sk-1234567890abcdef"})
    pub = conf.public()
    assert pub["has_key"] is True
    assert pub["key_hint"] == "sk-…cdef"
    assert "sk-1234567890abcdef" not in json.dumps(pub, ensure_ascii=False)


def test_broken_file_is_treated_as_empty(box):
    (box / "settings.json").write_text("{oops", encoding="utf-8")
    assert conf.load() == {}
    assert conf.effective()["model"] == ""


def test_unknown_fields_are_ignored(box):
    conf.save({"model": "m", "hacker": "x"})
    assert conf.load() == {"model": "m"}


def test_nothing_set_means_all_empty(box):
    assert conf.effective() == {"base_url": "", "api_key": "", "model": "",
                                "reasoning_effort": "", "style": ""}
    assert conf.public()["has_key"] is False
    assert conf.public()["key_hint"] == ""
    assert conf.public()["style"] == "rigorous"      # 空 = 界面按默认那档亮着


def test_effort_list_is_offered_to_the_ui(box):
    values = [e["value"] for e in conf.public()["efforts"]]
    assert values == ["", "low", "medium", "high"]


def test_load_is_cached_until_the_file_changes(box, monkeypatch):
    """设置弹层一次要调五次 load，不能每次都读盘。"""
    conf.save({"model": "m1"})
    conf.load()
    reads = []
    real = conf.Path.read_text

    def spy(self, *a, **kw):
        reads.append(1)
        return real(self, *a, **kw)

    monkeypatch.setattr(conf.Path, "read_text", spy)
    conf.public()
    assert reads == [], "public() 又读了一遍盘"
    # 文件真变了要重新读（把 mtime 往后拨，免得被文件系统分辨率坑）
    p = conf.path()
    p.write_text('{"model": "m2"}', encoding="utf-8")
    import os
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert conf.load()["model"] == "m2"


def test_save_invalidates_the_cache(box):
    conf.save({"model": "m1"})
    assert conf.load()["model"] == "m1"
    conf.save({"model": "m2"})          # 同一秒内连续保存也要看得见
    assert conf.load()["model"] == "m2"
