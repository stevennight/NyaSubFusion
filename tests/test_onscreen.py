"""没人说出来的画面字（菜名、标题）：不和台词合成一条；说明译文不压住画面上的字。

用例来自《新日本風土記》：旁白在说“郷土料理の治部煮風に仕立てた鴨鍋。”，画面上打出菜名“鴨治部すき”。
"""
from nyasub import lang
from nyasub.core import config
from nyasub.core.config import LayerCfg, ProfileCfg
from nyasub.fusion.fuse import fuse
from nyasub.llm.translate import apply_result, make_cue
from nyasub.render import ass, layout

FUSION = config.FusionCfg().model_dump()


def _ocr(id, start, end, text, rect=(0.37, 0.86, 0.63, 0.98), zone="bottom"):
    return {"id": id, "start": start, "end": end, "text": text, "rect": list(rect), "zone": zone, "score": 0.99,
            "lines": text.count("\n") + 1}


SUBS = [{"id": "c18", "start": 136.6, "end": 142.5, "text": "郷土料理の治部煮風に仕立てた鴨鍋。"},
        {"id": "c19", "start": 142.5, "end": 145.8, "text": "ただの鴨ではありません。"}]


def test_fuse_marks_screen_text_over_other_speech():
    ocr = [_ocr("o32", 141.4, 146.1, "鴨治部すき"),
           _ocr("o40", 200.0, 203.0, "だから元気出んのよ")]          # 同时只有“（笑い声）”：不算有人在说别的话
    subs = SUBS + [{"id": "c30", "start": 200.5, "end": 202.5, "text": "（笑い声）"}]
    out = fuse(subs, ocr, [], FUSION, lang.get("ja"), 300)
    items = {it["id"]: it for it in out["items"]}
    assert items["o32"].get("over_speech") is True
    assert not items["o40"].get("over_speech")


def _items(**over):
    o32 = {"id": "o32", "src": "telop", "start": 141.4, "end": 146.1, "text": "鴨治部すき",
           "rect": [0.37, 0.86, 0.63, 0.98], "over_speech": True}
    o32.update(over)
    return [dict(SUBS[0], src="subs"), o32, dict(SUBS[1], src="subs")]


def test_unspoken_label_merged_into_narration_is_split_out():
    res = {"cues": [{"src": ["c18", "o32"], "type": "narration", "orig": "郷土料理の治部煮風に仕立てた鴨鍋。｜鴨治部すき",
                     "tr": "以乡土料理治部煮风味烹制的鸭锅｜鸭治部锅"},
                    {"src": ["c19"], "type": "narration", "orig": "ただの鴨ではありません。", "tr": "这可不是普通的鸭子"}]}
    cues, missing = apply_result(_items(), res, "zh-Hans", "ja")
    assert not missing
    by = {tuple(c["sources"]): c for c in cues}
    nar, card = by[("c18",)], by[("o32",)]
    assert (nar["start"], nar["end"], nar["tr"]["zh-Hans"]["text"]) == (136.6, 142.5, "以乡土料理治部煮风味烹制的鸭锅")
    assert (card["start"], card["type"], card["tr"]["zh-Hans"]["text"]) == (141.4, "info", "鸭治部锅")
    assert card["rect"] == [0.37, 0.86, 0.63, 0.98]


def test_split_keeps_real_merges():
    # 1. 半句字幕、半句画面字（没有 over_speech 标记）：是同一句话，保持合并
    res = {"cues": [{"src": ["c18", "o32"], "type": "narration", "orig": "郷土料理の治部煮風に仕立てた鴨鍋。｜鴨治部すき",
                     "tr": "甲｜乙"}, {"src": ["c19"], "orig": "x", "tr": "y"}]}
    cues, _ = apply_result(_items(over_speech=False), res, "zh-Hans", "ja")
    assert any(c["sources"] == ["c18", "o32"] for c in cues)
    # 2. 台词里本来就说到了这个词：那一段是字幕的内容，不能当成画面字拆走
    items = [{"id": "c224", "src": "subs", "start": 1530.4, "end": 1537.5,
              "text": "網の形にちなんで四手網漁。\n昔は「虫踏み」と呼ばれていました。"},
             {"id": "o489", "src": "telop", "start": 1532.4, "end": 1538.4, "text": "虫踏み\n四手網漁", "over_speech": True}]
    res = {"cues": [{"src": ["c224", "o489"], "type": "narration",
                     "orig": "網の形にちなんで四手網漁。｜昔は「虫踏み」と呼ばれていました。", "tr": "甲｜乙"}]}
    cues, _ = apply_result(items, res, "zh-Hans", "ja")
    assert [c["sources"] for c in cues] == [["c224", "o489"]]


def test_split_label_partly_kept_and_multi_part():
    # 大模型只保留了画面字的一部分（“冬を食べる／坂網鴨”只留“坂網鴨”）
    items = [{"id": "c20", "src": "subs", "start": 148.1, "end": 156.0, "text": "３００年続く伝統の猟でとられた\n極上　かつレアな鴨肉です。"},
             {"id": "o33", "src": "telop", "start": 149.1, "end": 158.1, "text": "冬を食べる\n坂網鴨", "over_speech": True}]
    res = {"cues": [{"src": ["c20", "o33"], "orig": "３００年続く伝統の猟でとられた｜極上 かつレアな鴨肉です。｜坂網鴨",
                     "tr": "以延续三百年的传统捕猎所得｜顶级而稀有的鸭肉｜坂网鸭"}]}
    cues, _ = apply_result(items, res, "zh-Hans", "ja")
    assert {c["tr"]["zh-Hans"]["text"] for c in cues} == {"以延续三百年的传统捕猎所得｜顶级而稀有的鸭肉", "坂网鸭"}
    # 两行的地名标签被分成两段：一起拆出来
    items = [{"id": "c100", "src": "subs", "start": 789.7, "end": 794.6, "text": "山を越えた日本海側\n島根県の五十猛漁港。"},
             {"id": "o235", "src": "info", "start": 791.1, "end": 796.7, "text": "五十猛\n三次", "over_speech": True}]
    res = {"cues": [{"src": ["c100", "o235"], "orig": "山を越えた日本海側｜島根県の五十猛漁港｜五十猛｜三次", "tr": "甲｜乙｜丙｜丁"}]}
    cues, _ = apply_result(items, res, "zh-Hans", "ja")
    assert {c["tr"]["zh-Hans"]["text"] for c in cues} == {"甲｜乙", "丙｜丁"}


def test_info_rect_is_union_of_sources():
    a = {"id": "o29", "src": "telop", "start": 120, "end": 127, "text": "あの手この手の物語", "rect": [0.04, 0.82, 0.66, 1.0]}
    b = {"id": "o30", "src": "telop", "start": 120, "end": 127, "text": "冬を食べる", "rect": [0.0, 0.6, 0.36, 0.93]}
    assert make_cue([a, b], "冬を食べる｜あの手この手の物語", "品尝冬天", "zh-Hans", "info", "ja")["rect"] == [0.0, 0.6, 0.66, 1.0]


def test_info_translation_avoids_other_screen_text():
    """标题只识别出下半部分的框：贴着框的上方会压到没识别全的大字（另一个框），要换个位置。"""
    cfg = config.Config()
    prof = ProfileCfg(name="t", layers=[LayerCfg(role="translation")])
    cue = {"id": "q1", "start": 120, "end": 127, "type": "info", "onscreen": True, "rect": [0.04, 0.82, 0.66, 1.0],
           "orig": {"lang": "ja", "text": "あの手この手の物語"}, "tr": {"zh-Hans": {"text": "各式各样的故事"}}, "sources": ["o29"]}
    other = {"start": 120, "end": 127, "rect": [0.0, 0.55, 0.4, 0.8]}  # “冬を食べる”的大字
    items = layout.build([cue], prof, cfg, "ja")
    px, py, used, placed = ass.place(items, prof, cfg.render.styles, 16 / 9, [other])
    p = placed[0]
    st = cfg.render.styles["info"]
    h = st.size * 1.25
    top = p["y"] - h if p["an"] in (1, 2, 3) else p["y"]
    assert top + h <= 0.55 * py or top >= 0.8 * py  # 没有压到另一个框
    assert 0 <= top and top + h <= py
