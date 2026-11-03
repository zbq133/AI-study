"""设置文件本身的行为：存哪、谁优先、密钥会不会漏出去。"""

import json

import pytest

from app import settings as conf


@pytest.fixture()
def box(tmp_path, monkeypatch):
    """把配置文件挪到临时目录，并清掉自己机器上的环境变量（.env 会被 load_dotenv 读进来）。"""
    monkeypatch.setattr(conf, "SETTINGS_PATH", tmp_path / "settings.json")
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
                                "reasoning_effort": ""}
    assert conf.public()["has_key"] is False
    assert conf.public()["key_hint"] == ""


def test_effort_list_is_offered_to_the_ui(box):
    values = [e["value"] for e in conf.public()["efforts"]]
    assert values == ["", "low", "medium", "high"]
