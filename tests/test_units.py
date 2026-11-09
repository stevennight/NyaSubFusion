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
    # 作品设置不参与合并：只是新建一集时复制过去的默认值（见 test_series_settings）
    assert cfg.project.context == "" and cfg.asr.model == "small"   # 用户层
    assert cfg.llm.model == "cli-model"        # 命令行最高
    assert files[-1] == "命令行" and not any("series" in f for f in files)
    assert cfg.pick("asr.model", "project.series") == {"asr.model": "small", "project.series": "show"}
    assert config.series_effective("show").asr.model == "medium"   # 作品的默认设置本身：用户层 + 作品设置


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


def test_fuse_dup_needs_most_of_the_text():
    """同时的字幕只说到画面字的一小段（“仮設。” 对 “おれ仮設におった”）：是另一句话，留着送去翻译；
    字幕基本都说到了（写法不同的“1kg”“１キロ”只差两三个字）还是算重复。"""
    ja = lang.get("ja")
    subs = [{"id": "c1", "start": 1.0, "end": 3.0, "text": "仮設。"},
            {"id": "c2", "start": 5.0, "end": 8.0, "text": "今年は１キロ８，０００円。"},
            {"id": "c3", "start": 10.0, "end": 12.0, "text": "落ち着きます？"}]
    ocr = [_ocr("o1", 1.0, 3.0, "おれ仮設におった"),
           _ocr("o2", 5.0, 8.0, "1kg ＝ 8,000円", rect=(0.6, 0.3, 0.9, 0.4), zone="middle"),
           _ocr("o3", 10.0, 12.0, "本が好きなので落ち着きます")]
    out = fuse(subs, ocr, [], FUSION, ja, 30)
    kept = {it["id"] for it in out["items"]}
    assert "o1" in kept and "o3" in kept and not ocr[0].get("dup_of")
    assert "o2" not in kept and ocr[1]["dup_of"] == ["c2"]


def test_fuse_keeps_speech_when_screen_only_quotes_a_few_words():
    """没有字幕的预告：旁白一整句，画面上只打出其中两三个字（「伝統工芸」「建築」）。这句话单独成条、那几个字按重复去掉，
    不能整句被当成“画面字已经有了”丢掉。一句长话配几行采访硬字幕的（每行都是这句话的一段、合起来说到了大半），
    以及硬字幕之外只多了“ね。そう、あ、…うん”语气词的，照旧用硬字幕。"""
    ja = lang.get("ja")
    ocr = [_ocr("o1", 10.0, 12.5, "伝統工芸", rect=(0.08, 0.55, 0.49, 0.72), zone="middle"),
           _ocr("o2", 15.8, 17.4, "建築", rect=(0.05, 0.76, 0.28, 0.95)),
           _ocr("o3", 40.0, 43.0, "昔 昭和の夜汽車というのは"),
           _ocr("o4", 43.0, 46.0, "こういうほのかな明かりを"),
           _ocr("o5", 46.0, 49.0, "つけてたということで"),
           _ocr("o6", 60.0, 62.0, "踊り食いね")]
    asr = [{"id": "a1", "start": 10.0, "end": 17.0, "text": "日本の伝統工芸をはじめ、各地の食文化、建築まで、"},
           {"id": "a2", "start": 40.0, "end": 49.0, "text": "昔昭和の夜汽車っていうのはこういうね、ほのかな明かりをですね、つけてたということで"},
           {"id": "a3", "start": 60.0, "end": 62.0, "text": "ね。そう、あ、踊り食いね、うん。"}]
    out = fuse([], ocr, asr, FUSION, ja, 100)
    kept = {it["id"]: it for it in out["items"]}
    assert kept["a1"]["src"] == "asr" and "o1" not in kept and "o2" not in kept
    assert ocr[0]["dup_of"] == ["a1"] and ocr[1]["dup_of"] == ["a1"]
    assert {"o3", "o4", "o5", "o6"} <= set(kept) and "a2" not in kept and "a3" not in kept


def test_fuse_keeps_bottom_interview_caption_shorter_than_speech():
    """采访硬字幕把话缩短了（语音还多说了两句）：底部居中的是这句话的字幕，不是摘出的几个字，照旧用字幕，语音附为参考。"""
    ja = lang.get("ja")
    ocr = [_ocr("o632", 1378.7, 1385.6, "豆を煮た弁当も何も\n雪の中に迷い込んでしまった", rect=(0.27, 0.83, 0.73, 0.98), lines=2)]
    asr = [{"id": "a281", "start": 1378.9, "end": 1385.2,
            "text": "豆を煮た弁当もなにも雪の中さ舞い込んでしまって、どこに置いたか忘れてしまって、見えないくなってしまって、"}]
    out = fuse([], ocr, asr, FUSION, ja, 2000)
    kept = {it["id"]: it for it in out["items"]}
    assert "o632" in kept and "a281" not in kept and not ocr[0].get("dup_of")


def test_fuse_rescues_left_aligned_caption_line_from_sign_box():
    """字幕和旁边的价格牌连成一框、整框判成实物上的字：两行字幕左对齐，短的那行不居中，也要一起救回来；牌子上的字不要。"""
    ja = lang.get("ja")
    e = _ocr("o268", 688.7, 694.1, "わにで\n(ワニが)ほんと好き590油\nお正月くらいしか食べられないからかんす",
             rect=(0.22, 0.64, 0.92, 0.98), lines=3)
    e.update(kind="scene", pieces=[[0.8047, 0.6389, 0.9187, 0.7493, "わにで"], [0.6687, 0.7972, 0.7773, 0.9292, "590"],
                                      [0.2234, 0.8361, 0.5344, 0.9083, "(ワニが)ほんと好き"],
                                      [0.807, 0.8653, 0.9152, 0.9715, "かんす"],
                                      [0.2211, 0.9028, 0.7781, 0.9833, "お正月くらいしか食べられないから"]])
    asr = [{"id": "a1", "start": 688.0, "end": 694.0, "text": "美味しいんですよ。美味しい。 ほんと好き。ほんとお正月ぐらいしか食べれない。"}]
    out = fuse([], [e], asr, FUSION, ja, 1000)
    it = next(it for it in out["items"] if it["id"] == "o268")
    assert it["text"] == "(ワニが)ほんと好き\nお正月くらいしか食べられないから"


def test_fuse_joins_one_caption_split_in_two():
    """同一行字幕被识别切成前后两条（背景一变跟踪就断了）：合成一条，不显示两遍；位置不同、隔得远的不合。"""
    ja = lang.get("ja")
    ocr = [_ocr("o842", 1876.9, 1880.2, "国連が昆虫食は世界の飢えを救う", rect=(0.16, 0.89, 0.84, 0.99)),
           _ocr("o844", 1880.2, 1881.3, "国連が 昆虫食は 世界の飢えを救う", rect=(0.17, 0.9, 0.84, 0.98)),
           _ocr("o900", 1900.0, 1902.0, "ありがとう"),
           _ocr("o901", 1905.0, 1907.0, "ありがとう")]
    out = fuse([], ocr, [], FUSION, ja, 2000)
    kept = {it["id"]: it for it in out["items"]}
    assert "o844" not in kept and kept["o842"]["end"] == 1881.3 and ocr[1]["dup_of"] == ["o842"]
    assert {"o900", "o901"} <= set(kept)


def test_fuse_unrelated_speech_under_screen_text_stands_alone():
    """标题卡挂了十几秒，旁白同时说“ぜひご覧ください”，和卡上的字一个词都对不上：是另一句话，单独成条。
    有广播字幕（含「♬〜」）的地方、英文的话配日文画面字、只是“うん”这种应答的，照旧附为参考。"""
    ja = lang.get("ja")
    subs = [{"id": "c1", "start": 30.0, "end": 45.0, "text": "♬〜"}]
    ocr = [_ocr("o1", 0.0, 12.0, "毎週 水曜 夜７時３０分放送"),
           _ocr("o2", 20.0, 24.0, "とてもラッキーですね"),
           _ocr("o3", 30.0, 40.0, "自分たちが育てた米を原料にして作る甘酒"),
           _ocr("o4", 50.0, 55.0, "来年の夏頃には運転手になれるかもしれない")]
    asr = [{"id": "a1", "start": 9.0, "end": 10.5, "text": "ぜひご覧ください。"},
           {"id": "a2", "start": 20.5, "end": 23.0, "text": "We are very lucky."},
           {"id": "a3", "start": 32.0, "end": 34.0, "text": "生姜蜂蜜ってのがあるから。"},
           {"id": "a4", "start": 52.0, "end": 52.5, "text": "うん。"}]
    out = fuse(subs, ocr, asr, FUSION, ja, 100)
    kept = {it["id"]: it for it in out["items"]}
    assert kept["a1"]["src"] == "asr" and not kept["o1"].get("asr_hint")
    assert "a2" not in kept and kept["o2"]["asr_hint"] == "We are very lucky."
    assert "a3" not in kept and "a4" not in kept and kept["o4"]["asr_hint"] == "うん。"


def test_fuse_drops_music_only_subs():
    """只有「♬〜」的字幕不要（一挂十几秒，还会把并进来的片名拉长）；带歌词的留着；放音乐时语音认出的杂音照旧不单独成条。"""
    from nyasub.fusion.fuse import music_only

    assert music_only("♬〜") and music_only("♪ ～") and not music_only("♬今夜 どろぶねで") and not music_only("（笑い声）")
    ja = lang.get("ja")
    subs = [{"id": "c1", "start": 1.0, "end": 16.0, "text": "♬〜"},
            {"id": "c2", "start": 20.0, "end": 23.0, "text": "♬今夜 どろぶねで"}]
    asr = [{"id": "a1", "start": 3.0, "end": 9.0, "text": "あーあーあー"}]
    out = fuse(subs, [], asr, FUSION, ja, 30)
    assert [it["id"] for it in out["items"]] == ["c2"] and out["stats"]["subs_music"] == 1


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
