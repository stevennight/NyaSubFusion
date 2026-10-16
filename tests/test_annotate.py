"""注音与转写（M2）：振假名、拼音、注音符号、罗马字、读音词典、ASS 注音定位。"""
import json
import re

import pysubs2
import pytest

from nyasub import cli, lang
from nyasub.core import config
from nyasub.core.config import AnnotateCfg, LayerCfg, ProfileCfg
from nyasub.lang import ruby
from nyasub.render import ass, layout, srt

from . import fakellm

pytest.importorskip("sudachipy")
pytest.importorskip("pypinyin")

JA, ZH = lang.get("ja"), lang.get("zh-Hans")


def furi(words):
    """注音词 → 漢[かん]字[じ] 形式，方便断言。"""
    return " ".join("".join(f"{s[0]}[{s[1]}]" if len(s) > 1 else s[0] for s in w["segs"]) for w in words)


# ---------------- 工具 ----------------


def test_align_okurigana():
    assert ruby.align_okurigana("引き上げる", "ひきあげる") == [["引", "ひ"], ["き"], ["上", "あ"], ["げる"]]
    assert ruby.align_okurigana("穏やか", "オダヤカ") == [["穏", "おだ"], ["やか"]]
    assert ruby.align_okurigana("今朝", "けさ") == [["今朝", "けさ"]]          # 熟字训：整词注音
    assert ruby.align_okurigana("日の出", "ひので") == [["日", "ひ"], ["の"], ["出", "で"]]
    assert ruby.align_okurigana("ウニ漁", "うにりょう") == [["ウニ"], ["漁", "りょう"]]
    assert ruby.align_okurigana("ありがとう", "ありがとう") == [["ありがとう"]]  # 没有汉字：不注音


def test_romaji_and_tones():
    assert ruby.romaji("きょうと") == "kyouto"
    assert ruby.romaji("がっこう") == "gakkou"
    assert ruby.romaji("まっちゃ") == "matcha"
    assert ruby.romaji("げんいん") == "gen'in"
    assert ruby.romaji("ラーメン") == "raamen"
    assert ruby.tone_style("zhǎng", "number") == "zhang3"
    assert ruby.tone_style("zhǎng", "none") == "zhang"
    assert ruby.tone_style("de", "number") == "de"


def test_chunkify_survives_punctuation_changes():
    words = ZH.annotate("你好，世界。")
    lines = ruby.chunkify(["你好　世界"], words)  # 显示时逗号换成空格、句号去掉
    assert [c["text"] for c in lines[0]] == ["你", "好", "　", "世", "界"]
    assert lines[0][2]["key"] is None


# ---------------- 日语 ----------------


def test_ja_reading_overrides_and_flags():
    words = JA.annotate("日の出前に積丹岬へ")
    assert "出前[でまえ]" in furi(words)  # 分词器读错的例子
    fixed = JA.annotate("日の出前に積丹岬へ", [{"src": "日の出", "reading": "ひので", "proper": False}])
    assert furi(fixed).startswith("日[ひ]の出[で] 前[まえ]")
    cape = next(w for w in fixed if w["t"] == "積丹岬")
    assert cape["p"] and cape["guess"]  # 专有名词、读音来自分词器 → 待确认
    known = JA.annotate("積丹岬へ", [{"src": "積丹岬", "reading": "しゃこたんみさき", "from": "dict"}])
    assert not known[0]["guess"] and known[0]["src"] == "dict"


def test_ja_scope_rules():
    words = {w["t"]: w for w in JA.annotate("今年の漁師は五十年も海に出る")}
    diff = AnnotateCfg(kind="furigana", scope="difficult")
    assert JA.ruby_visible(words["漁師"], diff, True)
    assert not JA.ruby_visible(words["海"], diff, True)           # 小学二年级的字
    assert not JA.ruby_visible(words["五十"], AnnotateCfg(kind="furigana"), True)  # 数字默认不注
    assert JA.ruby_visible(words["五十"], AnnotateCfg(kind="furigana", numbers=True), True)
    assert not JA.ruby_visible(words["漁師"], AnnotateCfg(kind="furigana", scope="difficult", known_words=["漁師"]), True)
    assert JA.ruby_text(["漁", "りょう"], AnnotateCfg(script="katakana")) == "リョウ"
    assert JA.ruby_text(["漁", "りょう"], AnnotateCfg(script="romaji")) == "ryou"


def test_ja_transliterate():
    assert JA.transliterate(JA.annotate("冬になると海が荒れてね")) == "fuyu ni naru to umi ga arete ne"
    assert JA.transliterate(JA.annotate("食べられなかった")) == "taberarenakatta"
    assert JA.transliterate(JA.annotate("今日は佐藤さんへ")) == "kyou wa Satou-san e"


# ---------------- 中文 ----------------


def test_zh_pinyin_zhuyin():
    words = ZH.annotate("银行行长去重庆")
    toned = [w["segs"][0][1] for w in words]
    assert toned == ["yín", "háng", "háng", "zhǎng", "qù", "chóng", "qìng"]
    assert ZH.ruby_text(words[1]["segs"][0], AnnotateCfg(kind="zhuyin")) == "ㄏㄤˊ"
    de = ZH.annotate("我的")[1]["segs"][0]
    assert ZH.ruby_text(de, AnnotateCfg(kind="zhuyin")).startswith("˙")  # 轻声点在前
    assert ZH.transliterate(ZH.annotate("你好，世界。"), "number") == "ni3 hao3, shi4 jie4."


def test_zh_custom_reading():
    words = ZH.annotate("长发", [{"src": "长发", "reading": "cháng fà"}])
    assert [w["segs"][0][1] for w in words] == ["cháng", "fà"]


# ---------------- 排版与输出 ----------------


def _cue(cid, text, tr, start=0.0, end=4.0):
    return {"id": cid, "start": start, "end": end, "type": "dialogue", "sources": [], "onscreen": False,
            "orig": {"lang": "ja", "text": text}, "tr": {"zh-Hans": {"text": tr}}, "flags": []}


def _tokens(cues):
    return {c["id"]: {"orig": JA.annotate(c["orig"]["text"]), "tr": {"zh-Hans": ZH.annotate(c["tr"]["zh-Hans"]["text"])}}
            for c in cues}


def test_layout_furigana_first_scope_and_translit():
    cues = [_cue("q1", "漁師が来た", "渔夫来了"), _cue("q2", "漁師が帰った", "渔夫回去了", 5, 9)]
    prof = ProfileCfg(name="t", layers=[
        LayerCfg(role="original", annotate=AnnotateCfg(kind="furigana", scope="first")),
        LayerCfg(role="transliteration", style="translit"),
        LayerCfg(role="translation", annotate=AnnotateCfg(kind="pinyin", tone="number")),
    ])
    items = layout.build(cues, prof, config.Config(), "ja", _tokens(cues))
    orig1, tl1, tr1 = (lines for _, _, lines in items[0]["layers"])
    assert orig1[0][0] == ("漁師", "りょうし")
    assert layout.plain(tl1[0]) == "ryoushi ga kita"
    assert ("渔", "yu2") in tr1[0]
    orig2 = items[1]["layers"][0][2]
    assert all(rt is None or c != "漁師" for c, rt in orig2[0])  # 首次出现才注：第二条不再注「漁師」
    assert any(rt == "かえ" for _, rt in orig2[0])


def test_srt_paren_and_ass_positioning(tmp_path):
    cues = [_cue("q1", "漁師が港へ向かう", "渔夫去港口")]
    prof = ProfileCfg(name="t", layers=[LayerCfg(role="original", style="orig_main",
                                                 annotate=AnnotateCfg(kind="furigana", scope="difficult"))])
    cfg = config.Config()
    items = layout.build(cues, prof, cfg, "ja", _tokens(cues))
    p = tmp_path / "a.srt"
    srt.write(p, items)
    assert "漁師(りょうし)が港(みなと)へ向(む)かう" in p.read_text(encoding="utf-8-sig")

    pa = tmp_path / "a.ass"
    ass.write(pa, items, prof, cfg.render.styles, 16 / 9, [])
    events = pysubs2.load(str(pa)).events
    pos = [(e.plaintext, re.search(r"\\an(\d)\\pos\((\d+),(\d+)\)", e.text).groups()) for e in events]
    base = dict((t, xy) for t, xy in pos if t in ("漁師", "港", "向"))
    rubies = dict((t, xy) for t, xy in pos if t in ("りょうし", "みなと", "む"))
    assert base["漁師"][1] == rubies["りょうし"][1]          # 注音与底字同一中心 x
    assert int(rubies["りょうし"][2]) < int(base["漁師"][2])  # 注音在上方
    assert base["漁師"][0] == "2" and next(xy for t, xy in pos if t == "が")[0] == "1"


def test_styles_merge_with_defaults(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text("[render.styles.sub]\nsize = 30\n", encoding="utf-8")
    cfg, _ = config.load(f)
    assert cfg.render.styles["sub"].size == 30 and cfg.render.styles["sub"].font == "Yu Gothic"
    assert "main" in cfg.render.styles


# ---------------- 流水线 ----------------


def test_pipeline_annotate_stage(sample, api_config, monkeypatch, isolated_home):
    fakellm.install(monkeypatch)
    learn = api_config.with_name("learn.toml")
    learn.write_text(api_config.read_text(encoding="utf-8") + """
[[render.profiles]]
name = "learn"
filename = "{stem}.learn.{ext}"
layers = [
  { role = "original", style = "orig_main", annotate = { kind = "furigana", scope = "all" } },
  { role = "transliteration", style = "translit" },
]
""", encoding="utf-8")
    args = ["run", str(sample), "--no-asr", "--no-ocr", "--config", str(learn)]
    assert cli.main(args) == 0
    out = sample.with_name("sample.learn.ass").read_text(encoding="utf-8-sig")
    assert "ふかうらまち" in out and "Aomoriken" in out
    calls = len(fakellm.CALLS)

    # 只改读音词典：重做注音和输出，不重新翻译
    (isolated_home / "readings.json").write_text(json.dumps(
        {"entries": [{"lang": "ja", "src": "日本海", "reading": "にほんかい"}]}, ensure_ascii=False), encoding="utf-8")
    assert cli.main(args) == 0
    assert len(fakellm.CALLS) == calls
    out = sample.with_name("sample.learn.ass").read_text(encoding="utf-8-sig")
    assert "にほんかい" in out and "にっぽん" not in out
    assert "Nihonkai" in out  # 读音词典条目默认按专有名词处理（首字母大写）
