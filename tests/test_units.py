"""各模块的单元测试（不需要 ffmpeg / 模型）。"""
from nyasub import lang
from nyasub.core import config
from nyasub.core.config import LayerCfg, ProfileCfg
from nyasub.fusion.fuse import fuse
from nyasub.llm.translate import add_quality_flags, apply_result, merge_terms
from nyasub.render import layout

# ---------------- 语言包 ----------------


def test_lang_aliases_and_detect():
    assert lang.canonical("zh") == "zh-Hans"
    assert lang.canonical("jpn") == "ja"
    assert lang.get("fr").word_based  # 没有专门语言包的语言走通用规则
    assert lang.detect("今日はいい天気ですね") == "ja"
    assert lang.detect("今天天气很好") == "zh-Hans"
    assert lang.detect("Hello there, nice weather") == "en"
    assert lang.detect("안녕하세요 반갑습니다") == "ko"


def test_zh_punctuation_rules():
    zh = lang.get("zh-Hans")
    assert zh.display("今天天气很好，我们出去玩吧。", {}) == "今天天气很好　我们出去玩吧"
    assert zh.display("今天天气很好，我们出去玩吧。", {"comma_to_space": False}) == "今天天气很好，我们出去玩吧"
    assert zh.display("真的吗？！", {}) == "真的吗？！"


def test_wrap_rules():
    zh = lang.get("zh-Hans")
    lines = zh.wrap("这是一句非常非常长的中文字幕　需要在合适的地方断开才好看", 16)
    assert all(len(l) <= 16 for l in lines) and len(lines) == 2
    en = lang.get("en")
    lines = en.wrap("This is a fairly long English subtitle line that needs to be wrapped nicely", 42)
    assert len(lines) == 2 and all(len(l) <= 42 for l in lines)
    assert abs(len(lines[0]) - len(lines[1])) < 12  # 两行尽量等长
    ja = lang.get("ja")
    assert all(not l.startswith(("、", "。")) for l in ja.wrap("あいうえおかきくけこさしすせそ、たちつてと", 15))


# ---------------- 配置 ----------------


def test_config_layers(tmp_path, isolated_home):
    (isolated_home / "config.toml").write_text('[llm]\nmodel = "user-model"\n[asr]\nmodel = "small"\n', encoding="utf-8")
    sdir = isolated_home / "series" / "show"
    sdir.mkdir(parents=True)
    (sdir / "series.toml").write_text('[project]\ncontext = "作品说明"\n[asr]\nmodel = "medium"\n', encoding="utf-8")
    proj = tmp_path / "project.toml"
    proj.write_text('[project]\nseries = "show"\n', encoding="utf-8")
    cfg, files = config.load(None, proj, {"llm": {"model": "cli-model"}})
    assert cfg.project.context == "作品说明"   # 作品层生效
    assert cfg.asr.model == "medium"           # 作品层覆盖用户层
    assert cfg.llm.model == "cli-model"        # 命令行最高
    assert files[-1] == "命令行"
    assert cfg.pick("asr.model", "project.series") == {"asr.model": "medium", "project.series": "show"}


def test_llm_mode_auto(monkeypatch):
    cfg = config.Config()
    assert cfg.llm_mode() == "export"
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    cfg = config.Config.model_validate({"llm": {"model": "m"}})
    assert cfg.llm_mode() == "api"


# ---------------- 融合 ----------------

FUSION = config.FusionCfg().model_dump()


def _ocr(id_, start, end, text, rect=(0.3, 0.82, 0.7, 0.92), zone="bottom", lines=1, score=0.95):
    return {"id": id_, "start": start, "end": end, "text": text, "rect": list(rect), "zone": zone,
            "lines": lines, "score": score, "vertical": False}


def test_fuse_rules():
    ja = lang.get("ja")
    subs = [{"id": "c1", "start": 1.0, "end": 3.0, "text": "この港では"}]
    ocr = [
        _ocr("o1", 1.0, 3.0, "この港では"),                    # 与字幕重复 → 去掉
        _ocr("o2", 3.0, 5.0, "昔から鯛漁が盛んです"),           # 半句在画面字 → 台词
        _ocr("o3", 6.0, 8.0, "北海道・積丹町", rect=(0.04, 0.07, 0.27, 0.15), zone="top"),  # 说明
    ]
    asr = [
        {"id": "a1", "start": 9.0, "end": 10.5, "text": "よいしょっと"},            # 没有字幕覆盖 → 单独成条
        {"id": "a2", "start": 1.1, "end": 2.9, "text": "全然ちがう内容の言葉です"},  # 对不上 → 附为语音参考
        {"id": "a3", "start": 20.0, "end": 22.0, "text": "ご視聴ありがとうございました"},  # 幻觉 → 丢弃
    ]
    out = fuse(subs, ocr, asr, FUSION, ja, 30)
    kinds = {it["id"]: it["src"] for it in out["items"]}
    assert kinds == {"c1": "subs", "o2": "telop", "o3": "info", "a1": "asr"}
    assert ocr[0]["dup_of"] == ["c1"]
    c1 = next(it for it in out["items"] if it["id"] == "c1")
    assert "全然" in c1["asr_hint"]
    assert out["stats"]["asr_total"] == 2


# ---------------- 翻译结果校验 ----------------


def test_apply_result_and_flags():
    chunk = [
        {"id": "c1", "src": "subs", "start": 0, "end": 2, "text": "A"},
        {"id": "o2", "src": "telop", "start": 2, "end": 4, "text": "B", "ocr_score": 0.7},
        {"id": "a3", "src": "asr", "start": 5, "end": 6, "text": "C"},
    ]
    res = {"cues": [{"src": ["c1", "o2"], "type": "dialogue", "orig": "AB", "tr": "甲乙"},
                    {"src": ["c1"], "orig": "重复引用应被忽略", "tr": "x"}], "drop": []}
    cues, missing = apply_result(chunk, res, "zh-Hans", "ja")
    assert len(cues) == 1 and missing == ["a3"]
    c = cues[0]
    assert (c["start"], c["end"]) == (0, 4)
    assert c["onscreen"] and "ocr_low_conf" in c["flags"]
    c["tr"]["zh-Hans"]["text"] = "这是一句在四秒内根本念不完的非常非常长的中文译文" * 2  # 4 秒 48 字
    add_quality_flags(cues)
    assert "cps_high" in c["flags"] and c["review"] == "todo"


def test_merge_terms_prefers_known():
    known = [{"src": "積丹", "tr": "积丹（人工）"}]
    new = [{"src": "積丹", "tr": "积丹（模型）"}, {"src": "岬", "tr": "岬"}]
    assert [t["tr"] for t in merge_terms(known, new)] == ["积丹（人工）", "岬"]


# ---------------- 排版 ----------------


def _cue(**kw):
    c = {"id": "q1", "start": 0.0, "end": 6.0, "type": "dialogue", "sources": ["c1"], "onscreen": False,
         "orig": {"lang": "ja", "text": "前半の文です｜後半の文です"},
         "tr": {"zh-Hans": {"text": "前半句｜后半句"}, "en": {"text": "First half｜second half"}}, "flags": []}
    c.update(kw)
    return c


def _profile(*layers, **kw):
    return ProfileCfg(name="t", layers=list(layers), **kw)


def _txt(lines):
    return [layout.plain(l) for l in lines]


def test_layout_split_by_bar_and_layers():
    cfg = config.Config.model_validate({"project": {"targets": ["zh-Hans", "en"]}})
    prof = _profile(LayerCfg(role="translation"), LayerCfg(role="translation", lang="en", style="sub"),
                    LayerCfg(role="original", show="auto", style="sub"))
    items = layout.build([_cue()], prof, cfg, "ja")
    assert len(items) == 2                          # 按“｜”拆成先后两条
    assert items[0]["end"] == items[1]["start"]
    first = [(ly.role, code, _txt(lines)) for ly, code, lines in items[0]["layers"]]
    assert first == [("translation", "zh-Hans", ["前半句"]), ("translation", "en", ["First half"]),
                     ("original", "ja", ["前半の文です"])]


def test_layout_auto_original_and_info():
    cfg = config.Config()
    prof = _profile(LayerCfg(role="translation"), LayerCfg(role="original", show="auto"))
    onscreen = _cue(onscreen=True, orig={"lang": "ja", "text": "原文"}, tr={"zh-Hans": {"text": "译文"}})
    info = _cue(id="q2", type="info", rect=[0.1, 0.1, 0.3, 0.15], orig={"lang": "ja", "text": "地名"},
                tr={"zh-Hans": {"text": "地名译文"}})
    items = layout.build([onscreen, info], prof, cfg, "ja")
    main = [i for i in items if i["kind"] == "main"]
    near = [i for i in items if i["kind"] == "info"]
    assert [ly.role for ly, _, _ in main[0]["layers"]] == ["translation"]  # 画面上有原文 → 不重复显示
    assert near[0]["rect"] and _txt(near[0]["layers"][0][2]) == ["地名译文"]
    off = layout.build([info], _profile(LayerCfg(role="translation"), info_mode="off"), cfg, "ja")
    assert off == []


def test_layout_missing_translation_falls_back_to_original():
    cfg = config.Config()
    prof = _profile(LayerCfg(role="translation"), LayerCfg(role="translation", lang="en"))
    items = layout.build([_cue(tr={}, orig={"lang": "ja", "text": "訳なし"})], prof, cfg, "ja")
    assert [(code, _txt(lines)) for _, code, lines in items[0]["layers"]] == [("ja", ["訳なし"])]  # 原文只显示一次
    # 有中文、没有英文：英文层留空，不拿原文顶替
    items = layout.build([_cue(tr={"zh-Hans": {"text": "有译文"}})], prof, cfg, "ja")
    assert [(code, _txt(lines)) for _, code, lines in items[0]["layers"]] == [("zh-Hans", ["有译文"])]
