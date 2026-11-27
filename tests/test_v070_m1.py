"""v0.7.0 M1 底座重构测试：双层术语表与自动迁移、纯语音模式感知、monologue 独白与去纪录片化提示词。"""
import json

from nyasub import lang
from nyasub.core import config as config_mod
from nyasub.core import glossary as gl_mod
from nyasub.llm import prompts


def test_migrate_bloated_series_glossary(tmp_path):
    """多集膨胀的旧版作品术语表（>80 条且 >=2 集）首次读取时自动备份并精简：保留人工/编辑/跨集共现条目。"""
    sdir = tmp_path / "新日本風土記"
    gpath = sdir / "glossary.json"
    terms = [
        {"src": "声 松たか子", "tr": "朗读 松隆子", "type": "其他", "from": "manual"},
        {"src": "共通地名", "tr": "共通地名", "type": "地名", "from": "ep1"},
        {"src": "共通地名", "tr": "共通地名", "type": "地名", "from": "ep2"},
        {"src": "编辑过的词", "tr": "编辑译法", "type": "人名", "from": "ep1", "edited": True},
    ]
    for i in range(60):
        terms.append({"src": f"ep1_person_{i}", "tr": f"路人A{i}", "type": "人名", "from": "ep1"})
    for i in range(60):
        terms.append({"src": f"ep2_person_{i}", "tr": f"路人B{i}", "type": "人名", "from": "ep2"})
    sdir.mkdir(parents=True)
    gpath.write_text(json.dumps({"terms": terms}, ensure_ascii=False), encoding="utf-8")

    migrated = gl_mod.load_series_terms(gpath)
    kept_srcs = {t["src"] for t in migrated}
    assert kept_srcs == {"声 松たか子", "共通地名", "编辑过的词"}

    # 原文件和归档文件都已安全备份
    assert (sdir / "backup" / "glossary.v1.json").exists()
    archived = json.loads((sdir / "backup" / "glossary.v1.archived_episode_terms.json").read_text(encoding="utf-8"))
    assert len(archived["terms"]) == 120
    # 已标记 version=2，再次读取不会重复迁移
    raw = json.loads(gpath.read_text(encoding="utf-8"))
    assert raw["version"] == gl_mod.SCHEMA_VERSION


def test_should_sync_to_series_by_content_kind():
    cfg = config_mod.Config()
    cfg.project.series = "作品A"
    assert gl_mod.should_sync_to_series(cfg) is True  # auto + auto 兼容旧行为

    cfg.project.content_kind = "documentary"
    assert gl_mod.should_sync_to_series(cfg) is False  # 纪录片不污染作品表

    cfg.project.content_kind = "variety"
    assert gl_mod.should_sync_to_series(cfg) is False

    cfg.project.content_kind = "anime"
    assert gl_mod.should_sync_to_series(cfg) is True

    cfg.project.glossary_sync = "episode"
    assert gl_mod.should_sync_to_series(cfg) is False

    cfg.project.content_kind = "documentary"
    cfg.project.glossary_sync = "series"
    assert gl_mod.should_sync_to_series(cfg) is True


def test_asr_only_mode_glossary_and_organize_prompts():
    """纯语音（无字幕、无 OCR）工程：glossary_input 必须包含 ASR 文本，organize 切换为主来源语气。"""
    asr_items = [
        {"id": "a1", "src": "asr", "start": 15.8, "end": 20.0, "text": "あの時、俺たちは選ばなければ生き残れなかった。"},
        {"id": "a2", "src": "asr", "start": 26.8, "end": 28.9, "text": "いや！信乃姉様！"},
    ]
    assert prompts.is_asr_only(asr_items) is True

    gl_in = prompts.glossary_input(asr_items, [])
    assert "あの時、俺たちは選ばなければ生き残れなかった。" in gl_in
    assert "いや！信乃姉様！" in gl_in

    gl_sys = prompts.glossary_system(lang.get("ja"), lang.get("zh-Hans"), asr_only=True)
    assert "语音识别" in gl_sys

    org_sys = prompts.organize_system(lang.get("ja"), "", asr_only=True, content_kind="anime")
    assert "唯一主来源" in org_sys
    assert "monologue" in org_sys

    line = prompts.item_line(asr_items[0], asr_only=True)
    assert "无字幕覆盖" not in line


def test_translate_rules_preserve_first_person_and_support_monologue():
    rules = prompts.translate_rules(lang.get("zh-Hans"), {})
    assert "绝不能写成“我”" not in rules
    assert "忠实保留原文人称" in rules
    assert "monologue" in rules
    assert prompts.KIND["monologue"] == "独白"


def test_glossary_text_includes_structured_character_fields():
    txt = prompts.glossary_text([{
        "src": "犬塚信乃",
        "reading": "いぬづかしの",
        "tr": "犬冢信乃",
        "from": "manual",
        "aliases": ["信乃"],
        "relations": "幼年被当女孩养，滨路当时叫信乃姐姐",
    }])
    assert "犬塚信乃（いぬづかしの） → 犬冢信乃" in txt
    assert "幼年被当女孩养" in txt
    assert "别名：信乃" in txt
