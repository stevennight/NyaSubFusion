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
