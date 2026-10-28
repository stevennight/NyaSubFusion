"""字幕里的说话人线索：一条字幕里的换行是换了一个人，还是一个人的话折行。
各种字幕格式给的线索不一样（广播字幕的颜色和位置、ASS 的样式、SRT 的“- ”），都没有就不下结论。"""
from nyasub.core import segments
from nyasub.llm.prompts import lines_text
from nyasub.sources.subtitle import load_cues, voices

YELLOW, WHITE = "#FFFF00FF", "#FFFFFFFF"


def test_broadcast_lines_by_color_and_alignment():
    # 采访者在左、回答的人在右，左右两边都没对齐：两个人（碰巧中心差不多，不能看中心）
    assert voices([("（取材者）百姓たちが作り上げた野菜。", (WHITE, 552, 3192, 216)),
                   ("そう　農家が。", (WHITE, 1432, 2392, 216))]) == [0, 1]
    # 旁白右对齐折行、左对齐缩进一个字折行：同一个人
    assert voices([("長さ３．５メートル　重さ８００グラムほどの", (YELLOW, 792, 3592, 216)),
                   ("特殊な道具を使います。", (YELLOW, 1912, 3592, 216))]) is None
    assert voices([("それでも", (YELLOW, 552, 1192, 216)), ("僅かに残る雪菜農家の中では　最年少。", (YELLOW, 632, 3352, 216))]) is None
    # 颜色不一样（旁白黄色、说话的人白色）：换了人
    assert voices([("そして…。", (YELLOW, 1512, 2232, 216)), ("川でとった鮭を使って作る　とばです。", (WHITE, 552, 3272, 216))]) == [0, 1]


def test_plain_text_markers_and_no_hint():
    # SRT 这类纯文字：只能看行首的说话人标记
    assert voices([("- 行くぞ", None), ("- うん", None)]) == [0, 1]
    assert voices([("これ　一美さん。", None), ("（康子）甘いやろ。", None)]) == [0, 1]
    # 什么线索都没有：不下结论，交给大模型
    assert voices([("鴨が飛び込むと　網の下が外れて", None), ("身動きできなくなる仕掛け。", None)]) is None


def test_ass_styles_mark_speakers(tmp_path):
    p = tmp_path / "a.ass"
    p.write_text("[Script Info]\nPlayResX: 1920\nPlayResY: 1080\n\n[V4+ Styles]\n"
                 "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
                 "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
                 "Style: A,Arial,50,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,40,1\n"
                 "Style: B,Arial,50,&H0000FFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,40,1\n\n"
                 "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                 "Dialogue: 0,0:00:01.00,0:00:03.00,A,,0,0,0,,おはよう\\N元気？\n"
                 "Dialogue: 0,0:00:01.00,0:00:03.00,B,,0,0,0,,うん\n", encoding="utf-8")
    cue = load_cues(str(p))[0]
    assert cue["text"] == "おはよう\n元気？\nうん" and cue["voices"] == [0, 0, 1]


def test_prompt_marks_speaker_change():
    assert lines_text({"text": "（取材者）百姓たちが作り上げた野菜。\nそう　農家が。", "voices": [0, 1]}) == \
        "（取材者）百姓たちが作り上げた野菜。 ‖ そう　農家が。"
    assert lines_text({"text": "藩は\n餌場である周辺の田んぼを守りました。"}) == "藩は / 餌場である周辺の田んぼを守りました。"


def test_split_inside_caption_rejoined_by_speaker():
    """大模型把一条字幕里两个人的话分成了两屏：合回一屏时两个人的话换行隔开；同一个人的折行接成一句。"""
    items = {"c470": {"id": "c470", "src": "subs", "start": 3111.6, "end": 3115.0,
                      "text": "（取材者）百姓たちが作り上げた野菜\nそう　農家が。", "voices": [0, 1]},
             "c51": {"id": "c51", "src": "subs", "start": 385.6, "end": 390.8, "text": "藩は\n餌場である周辺の田んぼを守りました。"}}
    two = {"id": "q1", "start": 3111.6, "end": 3115.0, "sources": ["c470"],
           "orig": {"lang": "ja", "text": "（取材者）百姓たちが作り上げた野菜｜そう　農家が。"}, "tr": {}}
    one = {"id": "q2", "start": 385.6, "end": 390.8, "sources": ["c51"],
           "orig": {"lang": "ja", "text": "藩は｜餌場である周辺の田んぼを守りました。"}, "tr": {}}
    assert segments.card_bars(two, items) == [1] and segments.voice_change(two, items, 1) is True
    assert segments.card_bars(one, items) == [1] and segments.voice_change(one, items, 1) is None


def test_unsure_layout_goes_to_model():
    from nyasub.sources.subtitle import speaker_check

    # 位置只错开一点、颜色一样：看不出来，交给大模型（附上各行的位置和颜色）
    lines = [("（取材者）この冬場の。", (WHITE, 1112, 2652, 216)), ("はい。", (WHITE, 1692, 2052, 216))]
    assert voices(lines) is None
    chk = speaker_check(lines, 3840)
    assert [x["text"] for x in chk] == ["（取材者）この冬場の。", "はい。"] and chk[1]["pos"] == "44%–53%"
    # 线索都确定（对齐的折行 / 明显分开）：不用问
    assert speaker_check([("それでも", (YELLOW, 552, 1192, 216)), ("僅かに残る", (YELLOW, 632, 3352, 216))], 3840) is None
    # 纯文字、没有任何标记的两行：看不出来
    assert speaker_check([("鴨が飛び込むと", None), ("身動きできなくなる仕掛け。", None)], 1920)


def test_split_two_speakers_by_speech_timing():
    from nyasub.fusion.speakers import split_speakers

    words = lambda t0, text, step=0.1: [{"w": ch, "s": round(t0 + k * step, 3)} for k, ch in enumerate(text)]
    segs = [{"start": 3108.5, "end": 3112.2, "words": words(3110.3, "百姓たちが作り上げた野菜")},
            {"start": 3114.0, "end": 3114.8, "words": words(3114.045, "そう農家が")}]
    subs = [{"id": "c470", "start": 3111.6, "end": 3115.0, "text": "（取材者）百姓たちが作り上げた野菜。\nそう　農家が。", "voices": [0, 1]},
            {"id": "c471", "start": 3115.4, "end": 3121.0, "text": "雪菜の歴史は\n江戸時代中ごろに遡ります。"}]
    out = split_speakers(subs, segs)
    assert [(c["id"], c["start"], c["end"], c["text"]) for c in out[:2]] == [
        ("c470", 3111.6, 3114.045, "（取材者）百姓たちが作り上げた野菜。"), ("c470b", 3114.045, 3115.0, "そう　農家が。")]
    assert out[1]["split_from"] == "c470" and "voices" not in out[0]
    assert out[2]["id"] == "c471" and out[2]["text"] == "雪菜の歴史は\n江戸時代中ごろに遡ります。"  # 一个人的折行不拆
    # 没有语音识别：按字数估；太短拆不开的保持原样
    est = split_speakers([dict(subs[0])], [])
    assert est[1]["start"] == round(3111.6 + 3.4 * 15 / 20, 3)  # 按规范化后的字数（不算括号、标点）：15 / 20
    assert len(split_speakers([dict(subs[0], end=3112.3)], [])) == 1


def test_model_judges_unsure_items():
    from nyasub.llm.speakers import judge

    class Fake:
        def chat_json(self, system, user):
            self.user = user
            return {"items": [{"id": "c1", "voices": [1, 1, 0]}, {"id": "c2", "voices": [0, 0]}, {"id": "c3", "voices": [0]}]}

    subs = [{"id": "c1", "start": 0, "end": 3, "text": "あ\nい\nう", "speaker_check": [{"text": "あ"}, {"text": "い"}, {"text": "う"}]},
            {"id": "c2", "start": 3, "end": 5, "text": "え\nお", "speaker_check": [{"text": "え"}, {"text": "お"}]},
            {"id": "c3", "start": 5, "end": 7, "text": "か\nき", "speaker_check": [{"text": "か"}, {"text": "き"}]}]
    llm = Fake()
    assert judge(llm, subs, [{"start": 0.5, "end": 2, "text": "あいう"}]) == (3, 1)
    assert subs[0]["voices"] == [0, 0, 1]                     # 按出场先后重新编号
    assert "voices" not in subs[1] and "voices" not in subs[2]  # 一个人 / 回答的行数不对：不拆
    assert "语音识别听到的" in llm.user and "前一条字幕" in llm.user


def test_long_line_split_by_model_and_timed_by_speech():
    from nyasub.core import config
    from nyasub.llm.translate import split_long

    cfg = config.Config()
    long_tr = "春天就把它围在塑料大棚里，再放进蜜蜂让它自行授粉，每年都用这种方法来保护种子"
    cue = {"id": "q9", "start": 100.0, "end": 108.0, "type": "narration", "sources": ["c9"], "flags": [],
           "orig": {"lang": "ja", "text": "春になるとビニールハウスに囲って中にミツバチを放して自家受粉するように毎年種を守ります"},
           "tr": {"zh-Hans": {"text": long_tr}}}
    bad = dict(cue, id="q10", orig=dict(cue["orig"]), tr={"zh-Hans": {"text": long_tr}})

    class Fake:
        def chat_json(self, system, user):
            return {"items": [
                {"id": "q9", "orig": "春になるとビニールハウスに囲って｜中にミツバチを放して自家受粉するように｜毎年種を守ります",
                 "tr": "春天就把它围在塑料大棚里｜再放进蜜蜂让它自行授粉｜每年都用这种方法来保护种子"},
                {"id": "q10", "orig": "春になると｜ハウスで守ります", "tr": "甲｜乙"}]}   # 改了原文的字：不用
    split_long(Fake(), [cue, bad], {}, __import__("nyasub").lang.get("ja"), "zh-Hans", cfg)
    assert cue["orig"]["text"].count("｜") == 2 and cue["tr"]["zh-Hans"]["text"].count("｜") == 2
    assert "｜" not in bad["orig"]["text"]
    # 每段的时间：用语音识别的逐字时间找每段开口的时刻
    words = lambda t0, text: [{"w": ch, "s": round(t0 + k * 0.1, 3)} for k, ch in enumerate(text)]
    segs = [{"start": 100.0, "end": 108.0, "words": words(100.2, "春になるとビニールハウスに囲って") + words(103.5, "中にミツバチを放して")
             + words(106.4, "毎年種を守ります")}]
    assert segments.speech_parts(cue, segs) == [[100.0, 103.5], [103.5, 106.4], [106.4, 108.0]]
    assert segments.speech_parts(cue, []) is None
