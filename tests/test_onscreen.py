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
    # 2. 台词里本来就说到了这个词：那一段是字幕的内容，不能当成画面字拆走；
    #    画面上的标签本身单独成一条说明（只显示译文），台词的文字和时间不受影响
    items = [{"id": "c224", "src": "subs", "start": 1530.4, "end": 1537.5,
              "text": "網の形にちなんで四手網漁。\n昔は「虫踏み」と呼ばれていました。"},
             {"id": "o489", "src": "telop", "start": 1532.4, "end": 1538.4, "text": "虫踏み\n四手網漁", "over_speech": True}]
    res = {"cues": [{"src": ["c224", "o489"], "type": "narration",
                     "orig": "網の形にちなんで四手網漁。｜昔は「虫踏み」と呼ばれていました。", "tr": "甲｜乙"}]}
    cues, _ = apply_result(items, res, "zh-Hans", "ja")
    assert [c["sources"] for c in cues] == [["c224"], ["o489"]]
    assert cues[0]["orig"]["text"] == "網の形にちなんで四手網漁。｜昔は「虫踏み」と呼ばれていました。"
    assert (cues[0]["start"], cues[0]["end"]) == (1530.4, 1537.5)
    assert cues[1]["type"] == "info" and cues[1]["tr"] == {}  # 译文之后补翻


def test_orphan_caption_split_out():
    # 采访时画面上打出的问题概括：大模型并进了台词，但台词里没用它的内容 → 单独成一条，不再把台词的结束时间拉长
    items = [{"id": "c145", "src": "subs", "start": 1058.4, "end": 1062.467, "text": "（取材者）シンプルな疑問として\n収穫した時に出荷しても➡"},
             {"id": "c146", "src": "subs", "start": 1062.467, "end": 1064.801, "text": "売れるわけですよね。"},
             {"id": "o212", "src": "telop", "start": 1061.057, "end": 1065.057, "text": "収穫して出荷したらいいのでは？", "over_speech": True}]
    res = {"cues": [{"src": ["c145", "c146", "o212"], "type": "dialogue",
                     "orig": "（取材者）シンプルな疑問として｜収穫した時に出荷しても売れるわけですよね。", "tr": "甲｜乙"}]}
    cues, missing = apply_result(items, res, "zh-Hans", "ja")
    assert not missing
    assert [c["sources"] for c in cues] == [["c145", "c146"], ["o212"]]
    assert cues[0]["end"] == 1064.801
    assert cues[1]["orig"]["text"] == "収穫して出荷したらいいのでは？" and cues[1]["type"] == "info"


def test_labels_at_separate_places_stay_separate():
    # 老照片上分别标在两个人身上的「父」「啓全さん」：大模型合成了一条“父\n啓全さん”（译成“父亲 启全”像是父亲叫启全），拆回两条。
    # 同一块字的上下两行（人名条）、滚动的片尾字幕（同一行字换了位置）不拆
    items = [{"id": "o939", "src": "info", "start": 2166.0, "end": 2171.4, "text": "父", "rect": [0.55, 0.23, 0.59, 0.3]},
             {"id": "o940", "src": "info", "start": 2166.0, "end": 2171.4, "text": "啓全さん", "rect": [0.32, 0.62, 0.46, 0.69]},
             {"id": "o1229", "src": "info", "start": 3348.0, "end": 3353.6, "text": "清志さんの妻", "rect": [0.12, 0.62, 0.4, 0.68]},
             {"id": "o1230", "src": "info", "start": 3348.0, "end": 3353.6, "text": "吉田 貴美子 さん", "rect": [0.12, 0.68, 0.4, 0.74]},
             {"id": "o1347", "src": "telop", "start": 3494.9, "end": 3497.0, "text": "ディレクター 佐野 達也", "rect": [0.72, 0.83, 0.99, 0.98]},
             {"id": "v158", "src": "telop", "start": 3494.9, "end": 3497.0, "text": "レクター 佐野 達也\n田才 孝子", "rect": [0.0, 0.83, 0.23, 0.96]}]
    res = {"cues": [{"src": ["o939", "o940"], "type": "info", "orig": "父\n啓全さん"},
                    {"src": ["o1229", "o1230"], "type": "info", "orig": "清志さんの妻\n吉田 貴美子 さん"},
                    {"src": ["o1347", "v158"], "type": "info", "orig": "ディレクター 佐野達也\n田才孝子"}]}
    cues, missing = apply_result(items, res, "zh-Hans", "ja")
    assert not missing
    assert [(c["sources"], c["orig"]["text"]) for c in cues] == [
        (["o939"], "父"), (["o940"], "啓全さん"), (["o1229", "o1230"], "清志さんの妻\n吉田 貴美子 さん"),
        (["o1347", "v158"], "ディレクター 佐野達也\n田才孝子")]


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


def test_info_position_ignores_far_stray_source():
    from nyasub.llm.translate import info_geometry

    # 画面右边缘一小块误认成同样文字的（o213）不能把位置拉到画面中间、把时间提前
    stray = {"id": "o213", "src": "info", "start": 675.474, "end": 678.14, "rect": [0.9699, 0.4229, 0.9992, 0.4486], "ocr_score": 0.703}
    title = {"id": "o217", "src": "info", "start": 679.14, "end": 686.474, "rect": [0.0152, 0.8389, 0.3352, 0.9986], "ocr_score": 0.958}
    assert info_geometry([stray, title]) == {"rect": title["rect"], "start": 679.14, "end": 686.474}
    # 竖着叠的三段台标、同一行左右两段：都并在一起
    logo = [{"id": "a", "start": 0, "end": 5, "rect": [0.075, 0.5375, 0.2117, 0.6375]},
            {"id": "b", "start": 0, "end": 5, "rect": [0.0516, 0.7458, 0.2359, 0.7833]},
            {"id": "c", "start": 0, "end": 5, "rect": [0.0844, 0.8882, 0.2129, 0.9819]}]
    assert info_geometry(logo)["rect"] == [0.0516, 0.5375, 0.2359, 0.9819]
    credit = [{"id": "a", "start": 0, "end": 5, "rect": [0.07, 0.83, 0.3, 0.9]}, {"id": "b", "start": 0, "end": 5, "rect": [0.78, 0.83, 0.98, 0.9]}]
    assert info_geometry(credit)["rect"] == [0.07, 0.83, 0.98, 0.9]


def test_refine_rejects_reading_copied_from_another_image():
    from nyasub.llm.refine import borrowed

    group = [{"id": "o213", "text": "的精切日手的梯切目"}, {"id": "o217", "text": "冬を食べるワニ"}, {"id": "o1010", "text": "近"}]
    got = [{"text": "冬を食べる ワニ"}, {"text": "冬を食べるワニ"}, {"text": "米沢市 / 近藤"}]
    assert borrowed(group, got) == [0]  # 只认出一个字的补全不算串图


def test_refine_asks_suspect_image_alone(tmp_path):
    from nyasub.llm import refine

    paths = []
    for i in range(3):
        f = tmp_path / f"{i}.jpg"
        f.write_bytes(b"x%d" % i)
        paths.append(str(f))
    group = [{"id": "o28", "text": "冬を食べる\nあの手この手の物語"}, {"id": "o29", "text": "冬生食饭冬里食品"},
             {"id": "o31", "text": "語り柴田祐規子"}]
    calls = []

    class Fake:
        def chat_json(self, system, user, images=None):
            calls.append([i["label"] for i in images])
            if len(images) == 1:  # 单独问：片名动画的另一帧，字确实一样
                return {"items": [{"no": 1, "text": "冬を食べる / あの手この手の物語", "kind": "caption"}]}
            # 一批里返回顺序打乱了，按编号对回去
            return {"items": [{"no": 3, "text": "語り 柴田祐規子"}, {"no": 1, "text": "冬を食べる / あの手この手の物語"},
                              {"no": 2, "text": "冬を食べる / あの手この手の物語"}]}

    got = refine._ask(Fake(), group, paths)
    assert calls == [["第 1 张", "第 2 张", "第 3 张"], ["第 1 张"]]
    assert [g["text"] for g in got] == ["冬を食べる / あの手この手の物語"] * 2 + ["語り 柴田祐規子"]


def test_subs_wording_kept_and_names_checked():
    from nyasub.llm.translate import keep_subs_wording, term_misses

    # 大模型把字幕里的“キミイ”改成了另一个人“一美”：改回字幕的写法，“｜”位置不变
    subs = [{"src": "subs", "start": 0, "text": "それでも　地元の人たちが\n亡きキミイさんの思いを受け継ぎ➡"},
            {"src": "subs", "start": 1, "text": "伝統の味を守ろうとしています。"}]
    assert keep_subs_wording(subs, "それでも　地元の人たちが｜亡き一美さんの思いを受け継ぎ｜伝統の味を守ろうとしています。") == \
        "それでも　地元の人たちが｜亡きキミイさんの思いを受け継ぎ｜伝統の味を守ろうとしています。"
    # 画面字那半句不动；只是全角半角不同的也不动
    mixed = [{"src": "telop", "start": 0, "text": "非常に歩留まりの悪い野菜です"}, {"src": "subs", "start": 1, "text": "７年前"}]
    assert keep_subs_wording(mixed, "非常に歩留まりの悪い野菜です｜7年前") == "非常に歩留まりの悪い野菜です｜7年前"
    # 译文没按术语表的名字翻：找出来；片假名名字不算更长单词的一部分
    cues = [{"orig": {"text": "これはキミイの写真"}, "tr": {"zh-Hans": {"text": "这是Kimi的照片"}}},
            {"orig": {"text": "キミイさんの夢"}, "tr": {"zh-Hans": {"text": "纪美的梦想"}}},
            {"orig": {"text": "蚕のさなぎに イナゴ"}, "tr": {"zh-Hans": {"text": "蚕蛹和蚱蜢"}}}]
    terms = [{"src": "キミイさん", "tr": "纪美", "type": "人名"}, {"src": "イナ", "tr": "伊那", "type": "地名"}]
    assert term_misses(cues, terms, "zh-Hans") == [(0, [("キミイ", "纪美")])]
    # 店名、食物等术语表里的每一条都要统一；用了术语表译法的不算
    cues = [{"orig": {"text": "今夜 どろぶねで"}, "tr": {"zh-Hans": {"text": "今夜 在泥舟"}}},
            {"orig": {"text": "ナマコを干す"}, "tr": {"zh-Hans": {"text": "晒干海参"}}},
            {"orig": {"text": "ナマコの酢の物"}, "tr": {"zh-Hans": {"text": "醋拌海鼠"}}}]
    terms = [{"src": "どろぶね", "tr": "泥船", "type": "场所"}, {"src": "ナマコ", "tr": "海参", "type": "其他"}]
    assert term_misses(cues, terms, "zh-Hans") == [(0, [("どろぶね", "泥船")]), (2, [("ナマコ", "海参")])]


def test_refine_moments_shared_when_on_screen():
    from nyasub.llm.refine import plan_moments

    def ev(i, start, end, snap, rect, crop=None, **kw):
        return {"id": i, "start": start, "end": end, "snap_t": snap, "rect": rect, "crop_rect": crop or rect, **kw}

    bottle = ev("o18", 64.0, 66.0, 64.4, [0.95, 0.6, 0.97, 0.7])
    one = ev("o19", 64.2, 65.6, 64.5, [0.98, 0.4, 0.99, 0.44])           # 同一时刻在画面上：共用 o18 那一帧
    pan = ev("o42", 63.0, 66.0, 64.6, [0.5, 0.1, 0.6, 0.2], [0.3, 0.1, 0.6, 0.2])  # 镜头摇移，别的时刻框不准
    later = ev("o50", 70.0, 72.0, 70.5, [0.1, 0.8, 0.9, 0.9])
    m = plan_moments([later, pan, one, bottle])
    assert {t: [e["id"] for e in es] for t, es in m.items()} == {64.4: ["o18", "o19"], 64.6: ["o42"], 70.5: ["o50"]}

    # 前一句还没消失的换字交界处（背景牌刚好在那 0.3s 截图）：下一句不能共用那张含上一句字的帧，要改用自己清晰的时刻
    prev_cap = ev("o1476", 1844.45, 1847.55, 1846.20, [0.17, 0.85, 0.83, 0.98])
    bg_sign = ev("o1482", 1847.35, 1851.55, 1847.50, [0.10, 0.20, 0.20, 0.30])
    next_cap = ev("o1480", 1847.25, 1851.55, 1849.30, [0.07, 0.84, 0.93, 0.98])
    m2 = plan_moments([prev_cap, bg_sign, next_cap])
    assert {t: [e["id"] for e in es] for t, es in m2.items()} == {
        1846.20: ["o1476"], 1847.50: ["o1482"], 1849.30: ["o1480"],
    }

    # 长时间背景大框和末尾才出现的短字幕不算一起出现：不能共用同一帧
    long_bg = ev("o280", 1323.82, 1333.52, 1332.57, [0.1375, 0.7306, 0.9148, 0.9861])
    short_cap = ev("o283", 1330.02, 1332.62, 1330.17, [0.2918, 0.9042, 0.7047, 0.9833])
    m3 = plan_moments([long_bg, short_cap])
    assert {t: [e["id"] for e in es] for t, es in m3.items()} == {1330.17: ["o283"], 1332.57: ["o280"]}


def test_refine_match_merges_split_title_and_finds_misses():
    from nyasub.llm.refine import match_moment, read_items

    def ev(i, rect, start=10.0, end=14.0):
        return {"id": i, "start": start, "end": end, "rect": rect, "text": i}

    # 标题「サヨナラまでの日々」被本地切成两块（一起出现）；下面一条字幕被标题的框盖住一部分；
    # 一条不在这一刻复核的字；一条模型没读到的字
    a, b = ev("o1", [0.35, 0.76, 0.6, 0.89]), ev("o2", [0.62, 0.75, 0.96, 0.91], start=10.2)
    sub = ev("o3", [0.2, 0.84, 0.8, 0.9], start=11.0, end=20.0)
    other = ev("o4", [0.1, 0.1, 0.3, 0.2])
    unseen = ev("o5", [0.5, 0.3, 0.6, 0.35])
    raw = {"items": [{"text": "サヨナラまでの日々", "kind": "caption", "bbox": [354, 764, 957, 892]},
                     {"text": "字幕です", "kind": "caption", "bbox": [200, 840, 800, 900]},
                     {"text": "せふり", "kind": "scene", "bbox": [160, 360, 300, 490]},       # 本地漏掉的
                     {"text": "看板", "kind": "scene", "bbox": [100, 100, 300, 200]},         # 归 o4（不在这一刻复核）
                     {"text": "私の居場所", "kind": "caption", "bbox": [850, 140, 945, 172]},  # 台标：排除区
                     {"text": "", "bbox": [0, 0, 10, 10]}]}
    regions = [{"rect": [0, 0, 1, 1], "exclude": False}, {"rect": [0.824, 0.025, 0.976, 0.181], "exclude": True}]
    items = read_items(raw, regions)
    assert [it["text"] for it in items] == ["サヨナラまでの日々", "字幕です", "せふり", "看板"]
    visible = [(e, e["rect"]) for e in (a, b, sub, other, unseen)]
    groups, misses = match_moment(items, visible, {"o1", "o2", "o3", "o5"})
    got = {tuple(e["id"] for e in evs): [it["text"] for it in its] for evs, its in groups}
    assert got == {("o1", "o2"): ["サヨナラまでの日々"], ("o3",): ["字幕です"], ("o5",): []}
    assert [it["text"] for it in misses] == ["せふり"]


def test_vlm_groups_atomic_caption_lines_and_keeps_adjacent_scene_separate(tmp_path, monkeypatch):
    """静态字不预合并：地图标号「24」和两行白色字幕作为独立原子轨迹送入整帧复核，
    由 VLM 分别归入 scene 与 caption，字幕自动合成一条且框紧贴字幕、不混入「24」。"""
    import cv2
    import numpy as np

    from nyasub.core.config import Config
    from nyasub.llm import client, refine
    from nyasub.sources import media

    frame = tmp_path / "frame.jpg"
    cv2.imwrite(str(frame), np.full((720, 1280, 3), 90, np.uint8))
    monkeypatch.setattr(media, "grab_frames", lambda video, times, out, **kw: {round(t, 3): frame for t in times})
    (tmp_path / "crops").mkdir()
    video = tmp_path / "v.ts"
    video.write_bytes(b"x")

    events = [
        {"id": "o1", "start": 10.0, "end": 13.7, "snap_t": 10.2, "rect": [0.168, 0.811, 0.220, 0.886],
         "text": "24", "vertical": False, "lines": 1, "score": 0.99, "pieces": [[0.168, 0.811, 0.220, 0.886, "24"]]},
        {"id": "o2", "start": 10.0, "end": 13.7, "snap_t": 10.2, "rect": [0.282, 0.826, 0.716, 0.915],
         "text": "ここだったら白浜中学校に", "vertical": False, "lines": 1, "score": 1.0,
         "pieces": [[0.282, 0.826, 0.716, 0.915, "ここだったら白浜中学校に"]]},
        {"id": "o3", "start": 10.0, "end": 13.7, "snap_t": 10.2, "rect": [0.270, 0.910, 0.710, 0.980],
         "text": "避難するんですけど", "vertical": False, "lines": 1, "score": 1.0,
         "pieces": [[0.270, 0.910, 0.710, 0.980, "避難するんですけど"]]},
    ]

    class Fake:
        def chat_json(self, system, user, images=None):
            if system == refine.READ_SYSTEM:
                return {"items": [
                    {"text": "24", "kind": "scene", "bbox": [163, 808, 224, 890]},
                    {"text": "ここだったら白浜中学校に / 避難するんですけど", "kind": "caption", "bbox": [270, 822, 718, 982]},
                ]}
            return {"text": "ここだったら白浜中学校に / 避難するんですけど", "kind": "caption"}

    monkeypatch.setattr(client, "LLM", lambda *a, **k: Fake())
    out = refine.refine_ocr(events, Config(), str(tmp_path), None, video=str(video), size=(1280, 720))
    caps = [e for e in out if e.get("kind") == "caption"]
    scenes = [e for e in out if e.get("kind") == "scene"]
    assert len(caps) == 1 and len(scenes) == 1
    assert caps[0]["text"] == "ここだったら白浜中学校に\n避難するんですけど"
    assert caps[0]["rect"] == [0.27, 0.826, 0.716, 0.98]
    assert scenes[0]["text"] == "24"


def test_refine_frames_end_to_end(tmp_path, monkeypatch):
    import cv2
    import numpy as np

    from nyasub.core.config import Config
    from nyasub.llm import refine
    from nyasub.sources import media, ocr

    frame = tmp_path / "frame.jpg"
    cv2.imwrite(str(frame), np.full((2160, 3840, 3), 90, np.uint8))
    monkeypatch.setattr(media, "grab_frames", lambda video, times, out, **kw: {round(t, 3): frame for t in times})
    monkeypatch.setattr(ocr, "track_region", lambda video, t, rect, aspect=16 / 9: (t - 2, t + 3))
    (tmp_path / "crops").mkdir()
    video = tmp_path / "v.ts"
    video.write_bytes(b"x")
    events = [{"id": "o1", "start": 10.0, "end": 14.0, "snap_t": 12.0, "rect": [0.62, 0.75, 0.96, 0.91], "text": "わでの曰々",
               "vertical": False, "lines": 1, "crop": "crops/o1.jpg", "pieces": [[0.62, 0.75, 0.96, 0.91, "わでの曰々"]]},
              {"id": "o2", "start": 10.2, "end": 14.0, "snap_t": 12.0, "rect": [0.35, 0.76, 0.6, 0.89], "text": "坂道の上の家",
               "vertical": False, "lines": 1, "crop": "crops/o2.jpg", "pieces": [[0.35, 0.76, 0.6, 0.89, "坂道の上の家"]]}]
    asked = []

    class Fake:
        def chat_json(self, system, user, images=None):
            asked.append(system)
            if system == refine.READ_SYSTEM:
                return {"items": [{"text": "サヨナラまでの日々", "kind": "caption", "bbox": [354, 764, 957, 892]},
                                  {"text": "せふり さしみコンニャク", "kind": "scene", "bbox": [160, 300, 700, 600]}]}
            return {"text": "サヨナラまでの日々", "kind": "caption"}

    import nyasub.llm.client as client
    monkeypatch.setattr(client, "LLM", lambda *a, **k: Fake())
    out = refine.refine_ocr(events, Config(), str(tmp_path), None, video=str(video), size=(3840, 2160))
    assert sorted(e["id"] for e in out) == ["o1", "v1"]  # o2 并进了 o1；补出一条本地漏掉的
    t, v = (next(e for e in out if e["id"] == i) for i in ("o1", "v1"))
    assert (t["text"], t["text_local"], t["kind"], t["start"], t["merged"]) == \
        ("サヨナラまでの日々", "坂道の上の家わでの曰々", "caption", 10.0, ["o2"])
    assert t["rect"] == [0.35, 0.75, 0.96, 0.91] and t["rect_local"] == [0.35, 0.75, 0.96, 0.91]
    assert len(t["pieces"]) == 2
    assert (v["text"], v["kind"], v["start"], v["end"], v["found_by"]) == ("せふり さしみコンニャク", "scene", 10.0, 15.0, "vlm")
    assert (tmp_path / v["crop"]).exists()
    # 本地读的和模型读的差很多：截特写再问了一次；临时截的画面删掉了
    assert asked.count(refine.RECHECK_SYSTEM) == 1
    assert not (tmp_path / "crops" / "read").exists()


def test_refine_frames_also_checks_events_without_a_saved_crop(tmp_path, monkeypatch):
    """识别时没存下截图的框（「加熱用のわに」读成乱码「加他用のbに」）：按画面复核时照样看，模型认出是实物上的字。"""
    import cv2
    import numpy as np

    from nyasub.core.config import Config
    from nyasub.llm import refine
    from nyasub.sources import media, ocr

    frame = tmp_path / "frame.jpg"
    cv2.imwrite(str(frame), np.full((2160, 3840, 3), 90, np.uint8))
    monkeypatch.setattr(media, "grab_frames", lambda video, times, out, **kw: {round(t, 3): frame for t in times})
    monkeypatch.setattr(ocr, "track_region", lambda video, t, rect, aspect=16 / 9: (t - 2, t + 3))
    (tmp_path / "crops").mkdir()
    video = tmp_path / "v.ts"
    video.write_bytes(b"x")
    events = [{"id": "o245", "start": 672.1, "end": 673.6, "snap_t": None, "rect": [0.8441, 0.7681, 0.8828, 0.7979],
               "text": "加他用のbに", "vertical": False, "lines": 1, "score": 0.709,
               "pieces": [[0.8441, 0.7681, 0.8828, 0.7979, "加他用のbに"]]}]

    class Fake:
        def chat_json(self, system, user, images=None):
            if system == refine.READ_SYSTEM:
                return {"items": [{"text": "加熱用のわに", "kind": "scene", "bbox": [844, 768, 883, 798]}]}
            return {"text": "加熱用のわに", "kind": "scene"}

    import nyasub.llm.client as client
    monkeypatch.setattr(client, "LLM", lambda *a, **k: Fake())
    out = refine.refine_ocr(events, Config(), str(tmp_path), None, video=str(video), size=(3840, 2160))
    e = next(e for e in out if e["id"] == "o245")
    assert (e["text"], e["text_local"], e["kind"]) == ("加熱用のわに", "加他用のbに", "scene")


def test_refine_rechecks_when_confident_local_reading_differs(tmp_path, monkeypatch):
    """本地读得很有把握，模型整帧读出来的只有结尾几个字一样（竖排海报「西和賀の良さを 全国に発信しよう」读成
    「冬の食と手に発信しよう」）：截特写再问，按特写读的来。"""
    import cv2
    import numpy as np

    from nyasub.core.config import Config
    from nyasub.llm import refine
    from nyasub.sources import media, ocr

    frame = tmp_path / "frame.jpg"
    cv2.imwrite(str(frame), np.full((2160, 3840, 3), 90, np.uint8))
    monkeypatch.setattr(media, "grab_frames", lambda video, times, out, **kw: {round(t, 3): frame for t in times})
    monkeypatch.setattr(ocr, "track_region", lambda video, t, rect, aspect=16 / 9: (t - 2, t + 3))
    (tmp_path / "crops").mkdir()
    video = tmp_path / "v.ts"
    video.write_bytes(b"x")
    events = [{"id": "o559", "start": 1165.0, "end": 1170.4, "snap_t": 1165.26, "rect": [0.5859, 0.0125, 0.6922, 0.8583],
               "text": "西和賀の良さを\n全国に発信しよう", "vertical": True, "lines": 2, "score": 0.981, "crop": "crops/o559.jpg",
               "pieces": [[0.6344, 0.0125, 0.6922, 0.5958, "西和賀の良さを"], [0.5859, 0.1903, 0.6492, 0.8583, "全国に発信しよう"]]}]
    asked = []

    class Fake:
        def chat_json(self, system, user, images=None):
            asked.append(system)
            if system == refine.READ_SYSTEM:
                return {"items": [{"text": "冬の食と手に発信しよう", "kind": "caption", "bbox": [590, 0, 685, 846]}]}
            return {"text": "西和賀の良さを\n全国に発信しよう", "kind": "scene"}

    import nyasub.llm.client as client
    monkeypatch.setattr(client, "LLM", lambda *a, **k: Fake())
    out = refine.refine_ocr(events, Config(), str(tmp_path), None, video=str(video), size=(3840, 2160))
    e = next(e for e in out if e["id"] == "o559")
    assert asked.count(refine.RECHECK_SYSTEM) == 1
    assert (e["text"], e["kind"]) == ("西和賀の良さを\n全国に発信しよう", "scene")


def test_refine_keeps_confident_local_text_when_model_calls_it_scene(tmp_path, monkeypatch):
    """模型把两行字幕的大块判成背景字（scene），两次都只读出「「失敗したかな」とか」，漏了本地读清楚的「言う時ある」：
    本地有把握的照留，模型补的那行（特写里也有）加上。"""
    import cv2
    import numpy as np

    from nyasub.core.config import Config
    from nyasub.llm import refine
    from nyasub.sources import media, ocr

    frame = tmp_path / "frame.jpg"
    cv2.imwrite(str(frame), np.full((2160, 3840, 3), 90, np.uint8))
    monkeypatch.setattr(media, "grab_frames", lambda video, times, out, **kw: {round(t, 3): frame for t in times})
    monkeypatch.setattr(ocr, "track_region", lambda video, t, rect, aspect=16 / 9: (t - 2, t + 3))
    (tmp_path / "crops").mkdir()
    video = tmp_path / "v.ts"
    video.write_bytes(b"x")
    events = [{"id": "o1215", "start": 3306.5, "end": 3310.5, "snap_t": 3308.5, "rect": [0.2359, 0.9042, 0.7664, 0.9833],
               "text": "「失敗したかな」とか言う時ある", "lines": 1, "score": 0.999, "crop": "crops/o1215.jpg",
               "pieces": [[0.2359, 0.9042, 0.7664, 0.9833, "「失敗したかな」とか言う時ある"]]}]
    two = "部長とか課長とかになったり\n「失敗したかな」とか"

    class Fake:
        def chat_json(self, system, user, images=None):
            if system == refine.READ_SYSTEM:
                return {"items": [{"text": two.replace("\n", " / "), "kind": "scene", "bbox": [236, 880, 766, 983]}]}
            return {"text": two, "kind": "scene"}

    import nyasub.llm.client as client
    monkeypatch.setattr(client, "LLM", lambda *a, **k: Fake())
    out = refine.refine_ocr(events, Config(), str(tmp_path), None, video=str(video), size=(3840, 2160))
    e = next(e for e in out if e["id"] == "o1215")
    assert e["text"] == "部長とか課長とかになったり\n「失敗したかな」とか言う時ある"


def test_refine_rechecks_small_changes_to_confident_caption_and_caps_retime(tmp_path, monkeypatch):
    """字幕本地读得很准（0.98 以上），模型只改了一个字（「鮭とば」→「鮭とは」）：截特写再问，按特写定。
    本地只认出一小块、按模型的框找时间的，最多往前后各放宽 RETIME_MAX 秒（雪景背景里找过头，标题早出来 7 秒）。"""
    import cv2
    import numpy as np

    from nyasub.core.config import Config
    from nyasub.llm import refine
    from nyasub.sources import media, ocr

    frame = tmp_path / "frame.jpg"
    cv2.imwrite(str(frame), np.full((2160, 3840, 3), 90, np.uint8))
    monkeypatch.setattr(media, "grab_frames", lambda video, times, out, **kw: {round(t, 3): frame for t in times})
    monkeypatch.setattr(ocr, "track_region", lambda video, t, rect, aspect=16 / 9: (t - 7, t + 3))
    (tmp_path / "crops").mkdir()
    video = tmp_path / "v.ts"
    video.write_bytes(b"x")
    events = [{"id": "o858", "start": 1973.9, "end": 1980.5, "snap_t": 1974.0, "rect": [0.06, 0.85, 0.27, 0.98], "text": "鮭とば",
               "vertical": False, "lines": 1, "score": 0.987, "crop": "crops/o858.jpg", "pieces": [[0.06, 0.85, 0.27, 0.98, "鮭とば"]]},
              {"id": "o1360", "start": 3538.4, "end": 3541.2, "snap_t": 3538.5, "rect": [0.53, 0.68, 0.92, 1.0], "text": "風土記",
               "vertical": False, "lines": 1, "score": 0.878, "crop": "crops/o1360.jpg",
               "pieces": [[0.53, 0.68, 0.92, 1.0, "風土記"]]}]
    asked = []

    class Fake:
        def chat_json(self, system, user, images=None):
            asked.append((system, user))
            if system == refine.READ_SYSTEM:
                if "1974" in str(images):
                    return {"items": [{"text": "鮭とは", "kind": "caption", "bbox": [60, 850, 270, 980]}]}
                return {"items": [{"text": "新日本風土記", "kind": "caption", "bbox": [300, 400, 950, 1000]}]}
            if "風土記" in user:
                return {"text": "新日本風土記", "kind": "caption"}
            return {"text": "鮭とば", "kind": "caption"}

    import nyasub.llm.client as client
    monkeypatch.setattr(client, "LLM", lambda *a, **k: Fake())
    out = {e["id"]: e for e in refine.refine_ocr(events, Config(), str(tmp_path), None, video=str(video), size=(3840, 2160))}
    assert out["o858"]["text"] == "鮭とば"
    assert sum(1 for s, u in asked if s == refine.RECHECK_SYSTEM and "鮭" in u) == 1
    assert out["o1360"]["start"] == round(3538.4 - refine.RETIME_MAX, 3)


def test_unread_boxes_tracked_into_hints():
    """书法片名：本地只认出「新」，下面那块找到了位置但没认出字。停留够久、没被别的字盖住的才交给复核。"""
    import numpy as np

    from nyasub.sources import ocr

    box = lambda x0, y0, x1, y1: np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], np.float32)
    raw = [(box(1009, 387, 1182, 472), "新", 1.0), (box(990, 480, 1230, 715), "", 0.0),
           (box(400, 300, 410, 309), "", 0.0)]  # 太小的（树叶）不算
    dets = [{"rect": (1009.0, 387.0, 1182.0, 472.0), "text": "新"}]
    assert ocr._unread(raw, dets, 1280, 720) == [(990.0, 480.0, 1230.0, 715.0)]

    regions = []
    for i in range(16):  # 1.5 秒，后面的帧框得更全
        t = 16.0 + i * 0.1
        ocr._track_unread(regions, [(990.0, 480.0, 1230.0, 715.0 + (5 if i > 8 else 0))], t, 0.75)
    ocr._track_unread(regions, [(100.0, 100.0, 300.0, 200.0)], 16.0, 0.75)  # 一闪而过
    hints = ocr._unread_hints(regions, [], 1280, 720, 1.0)
    assert len(hints) == 1 and hints[0]["start"] == 16.0 and hints[0]["t"] == 16.9
    # 本地已经有字盖住这块（会照常复核）：不另看
    ev = {"start": 15.0, "end": 20.0, "rect": [0.77, 0.66, 0.97, 1.0]}
    assert ocr._unread_hints(regions, [ev], 1280, 720, 1.0) == []


def test_refine_reads_unread_spot_only_there(tmp_path, monkeypatch):
    """本地认不出字的地方单独看一帧：只补那块地方读到的字；附近已有复核时刻的不另看。"""
    import cv2
    import numpy as np

    from nyasub.core.config import Config
    from nyasub.llm import refine
    from nyasub.sources import media, ocr

    frame = tmp_path / "frame.jpg"
    cv2.imwrite(str(frame), np.full((2160, 3840, 3), 90, np.uint8))
    grabbed = []
    monkeypatch.setattr(media, "grab_frames", lambda video, times, out, **kw: grabbed.extend(times) or {round(t, 3): frame for t in times})
    monkeypatch.setattr(ocr, "track_region", lambda video, t, rect, aspect=16 / 9: (round(t - 0.7, 3), round(t + 5.1, 3)))
    (tmp_path / "crops").mkdir()
    video = tmp_path / "v.ts"
    video.write_bytes(b"x")
    events = [{"id": "o1", "start": 1.0, "end": 4.0, "snap_t": 2.0, "rect": [0.79, 0.9, 0.98, 0.98], "text": "声 松たか子",
               "vertical": False, "lines": 1, "crop": "crops/o1.jpg", "pieces": [[0.79, 0.9, 0.98, 0.98, "声 松たか子"]]}]
    unread = [{"start": 16.0, "end": 21.5, "t": 16.9, "rect": [0.77, 0.66, 0.97, 1.0]},
              {"start": 1.5, "end": 3.0, "t": 2.5, "rect": [0.1, 0.1, 0.3, 0.2]}]  # 2 秒那一帧本来就要读

    class Fake:
        def chat_json(self, system, user, images=None):
            if system == refine.READ_SYSTEM:
                return {"items": [{"text": "新日本風土記", "kind": "caption", "bbox": [780, 670, 960, 990]},
                                  {"text": "声 松たか子", "kind": "caption", "bbox": [790, 900, 980, 980]},
                                  {"text": "電力", "kind": "scene", "bbox": [300, 200, 400, 260]}]}  # 背景里别处的字
            return {"text": "新日本風土記", "kind": "caption"}

    import nyasub.llm.client as client
    monkeypatch.setattr(client, "LLM", lambda *a, **k: Fake())
    out = refine.refine_ocr(events, Config(), str(tmp_path), None, video=str(video), size=(3840, 2160), unread=unread)
    assert sorted(grabbed) == [2.0, 16.9]
    added = [e for e in out if e.get("found_by") == "vlm"]
    # 2 秒那帧上的「新日本風土記」「電力」照常按漏检补；16.9 秒那帧只补认不出字那块的
    late = [e for e in added if e["start"] >= 16]
    assert [(e["text"], e["start"], e["end"]) for e in late] == [("新日本風土記", 16.2, 22.0)]


# ---- 本地截图方式（llm/refine_crop，0.3 以前的做法）----


def test_refine_context_frames_shared_by_same_moment():
    from nyasub.llm.refine_crop import plan_groups

    def ev(i, start, end, snap, rect, crop=None, **kw):
        return {"id": i, "start": start, "end": end, "snap_t": snap, "rect": rect, "crop_rect": crop or rect, **kw}

    bottle = ev("o18", 64.0, 66.0, 64.4, [0.95, 0.6, 0.97, 0.7])
    one = ev("o19", 64.2, 65.6, 64.5, [0.98, 0.4, 0.99, 0.44])           # 同一时刻在画面上：共用 o18 那一帧
    pan = ev("o42", 63.0, 66.0, 64.6, [0.5, 0.1, 0.6, 0.2], [0.3, 0.1, 0.6, 0.2])  # 镜头摇移，别的时刻框不准
    later = ev("o50", 70.0, 72.0, 70.5, [0.1, 0.8, 0.9, 0.9])
    groups = plan_groups([later, pan, one, bottle], 4, context=True)
    assert [[e["id"] for e in g] for g, _ in groups] == [["o18", "o19", "o42", "o50"]]
    assert groups[0][1] == [64.4, 64.4, 64.6, 70.5]
    # 不带画面：顺序、分批和以前一样（请求缓存照样命中）
    assert [[e["id"] for e in g] for g, at in plan_groups([later, pan, one, bottle], 3)] == [["o50", "o42", "o19"], ["o18"]]


def test_refine_context_frame_marked(tmp_path):
    import cv2
    import numpy as np

    from nyasub.llm.refine_crop import mark_frame

    f = tmp_path / "f.jpg"
    cv2.imwrite(str(f), np.full((720, 1280, 3), 128, np.uint8))
    e = {"id": "o19", "start": 64.2, "end": 65.6, "snap_t": 64.5, "rect": [0.98, 0.4, 0.99, 0.44]}
    out = mark_frame(str(f), [e], [1], 64.5, str(tmp_path / "m"))
    img = cv2.imread(out)
    assert img.shape[:2] == (360, 640)
    assert (np.abs(img.astype(int) - 128).sum(axis=2) > 60).sum() > 50  # 画上了框和编号


def test_refine_context_asks_with_frames(tmp_path):
    from nyasub.llm import refine_crop as refine

    paths = []
    for i in range(2):
        f = tmp_path / f"{i}.jpg"
        f.write_bytes(b"x%d" % i)
        paths.append(str(f))
    group = [{"id": "o18", "text": "辛口純米"}, {"id": "o19", "text": "生"}]
    calls = []

    class Fake:
        def chat_json(self, system, user, images=None):
            calls.append((system, [i["label"] for i in images]))
            return {"items": [{"no": 1, "text": "辛口純米", "kind": "scene"}, {"no": 2, "text": "生", "kind": "scene"}]}

    got = refine._ask(Fake(), group, paths, lambda idx: [(paths[0], list(range(len(idx))))])
    assert calls[0][0] == refine.VLM_CTX_SYSTEM
    assert calls[0][1] == ["画面 A（框出了编号 1、2）", "第 1 张（编号 1 的特写，在画面 A 上）", "第 2 张（编号 2 的特写，在画面 A 上）"]
    assert [g["kind"] for g in got] == ["scene", "scene"]


def test_refine_recrop_boxes_text_at_snapshot(tmp_path):
    import cv2
    import numpy as np

    from nyasub.llm import refine_crop as refine

    f = tmp_path / "f.jpg"
    cv2.imwrite(str(f), np.full((1080, 1920, 3), 128, np.uint8))
    # 镜头摇移：截图框是整段移动范围（会带进片名），截图那一刻的位置只在其中一小块
    pan = {"id": "o42", "start": 238.0, "end": 242.0, "snap_t": 238.9, "lines": 1, "vertical": False,
           "rect": [0.5, 0.1, 0.55, 0.2], "crop_rect": [0.4, 0.05, 0.9, 0.3], "snap_rect": [0.8, 0.12, 0.85, 0.22]}
    p = refine.recrop(str(f), pan, 238.9, str(tmp_path / "r"))
    img = cv2.imread(p)
    assert img.shape[1] < 0.2 * 1920  # 只截了那一刻字的位置（整段移动范围有 960 宽）
    red = (img[:, :, 2] > 200) & (img[:, :, 1] < 80)
    assert red.sum() > 50  # 框出了要转写的字
    # 旧项目没记下那一刻的位置、字又挪过：框不准，用原来的特写
    old = dict(pan)
    del old["snap_rect"]
    assert refine.recrop(str(f), old, 238.9, str(tmp_path / "r")) is None


def test_refine_boxed_crops_told_to_model(tmp_path):
    from nyasub.llm import refine_crop as refine

    paths = []
    for i in range(2):
        f = tmp_path / f"{i}.jpg"
        f.write_bytes(b"x%d" % i)
        paths.append(str(f))
    calls = []

    class Fake:
        def chat_json(self, system, user, images=None):
            calls.append((system, [i["label"] for i in images]))
            return {"items": [{"no": 1, "text": "ネプ山坂", "kind": "scene"}, {"no": 2, "text": "第十号", "kind": "scene"}]}

    refine._ask(Fake(), [{"id": "o42", "text": "ネプ山坊"}, {"id": "o60", "text": "第十号"}], paths, boxed=[True, False])
    assert calls[0][0] == refine.VLM_SYSTEM + refine.BOXED_NOTE
    assert calls[0][1] == ["第 1 张（红框里是要转写的字）", "第 2 张"]


def test_refine_keeps_kind_when_mixed_up_or_batch_fails(tmp_path):
    from nyasub.core.config import Config
    from nyasub.llm import refine

    paths = []
    for i in range(2):
        f = tmp_path / f"{i}.jpg"
        f.write_bytes(b"x%d" % i)
        paths.append(str(f))
    group = [{"id": "o232", "text": "净速上证"}, {"id": "o236", "text": "フジタフーズ"}]

    class Mixed:
        def chat_json(self, system, user, images=None):
            if len(images) == 1:  # 单独问：字看不清，但看得出是价签上的字
                return {"items": [{"no": 1, "text": "", "kind": "scene"}]}
            return {"items": [{"no": 1, "text": "フジタフーズ", "kind": "caption"}, {"no": 2, "text": "フジタフーズ", "kind": "scene"}]}

    got = refine._ask(Mixed(), group, paths)
    assert got[0] == {"kind": "scene"}  # 文字保留本地结果，类型用单独问的

    calls = []

    class Broken:
        def chat_json(self, system, user, images=None):
            calls.append(len(images))
            if len(images) > 1:
                return {"items": [{"no": 1, "text": "x"}]}  # 条数不对
            return {"items": [{"no": 1, "text": "ok", "kind": "scene"}]}

    import nyasub.llm.client as client
    events = [dict(e, crop=f"{i}.jpg", start=i, end=i + 2, rect=[0, 0, 0.1, 0.1]) for i, e in enumerate(group)]
    orig = client.LLM
    client.LLM = lambda *a, **k: Broken()
    try:
        refine.refine_ocr(events, Config(), str(tmp_path), None)
    finally:
        client.LLM = orig
    assert calls == [2, 1, 1]  # 一批失败后逐条单独问
    assert [(e["text"], e["kind"]) for e in events] == [("ok", "scene"), ("ok", "scene")]


def test_dialogue_slides_aside_from_name_card():
    """右下角的人名条只擦到台词右边一点：台词原高度往左挪开，不整块推到画面中间；整行的硬字幕照旧往上推。"""
    cfg = config.Config()
    prof = ProfileCfg(name="t", layers=[LayerCfg(role="translation")])
    cue = {"id": "q385", "start": 1688.4, "end": 1696.9, "type": "narration", "onscreen": False,
           "orig": {"lang": "ja", "text": "もともと虫を食べる文化は"}, "tr": {"zh-Hans": {"text": "长野县各地原本都有食虫文化"}}}
    items = layout.build([cue], prof, cfg, "ja")
    card = {"start": 1691.1, "end": 1697.4, "rect": [0.6242, 0.7097, 0.8227, 0.95]}  # 牧田 豊さん
    px, py, _, placed = ass.place(items, prof, cfg.render.styles, 16 / 9, [card])
    p = placed[0]
    assert p["margin_v"] == 46 and p["x"] < px / 2 and p["y"] == py - 46
    caption = {"start": 1688.0, "end": 1697.0, "rect": [0.1, 0.85, 0.9, 0.98]}
    _, _, _, placed = ass.place(items, prof, cfg.render.styles, 16 / 9, [caption])
    assert placed[0]["margin_v"] > 46 and placed[0].get("x") is None


def test_fuse_dup_needs_subs_on_screen_at_the_same_time():
    """“私 失敗しないので”消失后旁白才说“失敗しない清志さんも…”（只是靠时间余量带进来、共用一个短语）：
    不算重复，照常用；同时在屏幕上的真正重复照样去掉。"""
    subs = [{"id": "c498", "start": 3287.1, "end": 3289.3, "text": "（笑い声）"},
            {"id": "c499", "start": 3289.3, "end": 3293.6, "text": "失敗しない清志さんも　今年６２歳。"},
            {"id": "c470", "start": 3111.6, "end": 3115.0, "text": "（取材者）百姓たちが作り上げた野菜。\nそう　農家が。"}]
    ocr = [_ocr("o1115", 3286.1, 3289.0, "私 失敗しないので"),
           _ocr("o1041", 3109.9, 3116.5, "昔の百姓たちが作り上げた野菜\n農家が")]
    fuse(subs, ocr, [], FUSION, lang.get("ja"), 3600)
    assert "dup_of" not in ocr[0] and ocr[1]["dup_of"] == ["c470"]


def test_title_card_misjudged_as_scene_is_rescued():
    """同样的“冬を食べる”在别处判成了叠加字幕：这里判成实物上的字的那张标题卡（连同下面一行“ナマコ”）救回来。"""
    card = _ocr("o777", 1972.9, 1980.5, "冬を食べる", (0.0211, 0.7472, 0.1922, 0.8514))
    card["kind"] = "caption"
    a = dict(_ocr("o932", 2562.5, 2568.0, "冬を食べる", (0.0203, 0.7528, 0.1891, 0.8514)), kind="scene")
    b = dict(_ocr("o933", 2563.3, 2568.0, "ナマコ", (0.0523, 0.8361, 0.2703, 0.9917)), kind="scene")
    sign = dict(_ocr("o100", 2562.5, 2568.0, "冬を食べる", (0.7, 0.2, 0.9, 0.3)), kind="scene")  # 别的位置：不算
    fuse([], [card, a, b, sign], [], FUSION, lang.get("ja"), 3600)
    assert not a.get("dropped") and not b.get("dropped") and sign["dropped"] == "scene"


def test_caption_line_merged_with_scene_text_is_kept():
    """沿线地图的字和下面的硬字幕连成了一框，整框判成实物上的字：底部居中、和语音对得上的那一行留下来当画面字
    （位置是那一行的，时间收到那段语音），语音不再单独成条；地图的字照旧丢掉。"""
    box = dict(_ocr("o84", 128.6, 134.4, "岳南電車全駅から富士山が望める鉄道\n沿線MAP Gakunan Electric Train map\nこの“赤がえる”撮りたいの私",
                    (0.268, 0.7056, 0.7289, 0.9833)), kind="scene",
               pieces=[[0.4301, 0.7264, 0.566, 0.8333, "全駅から富士山が望める鉄道"], [0.4133, 0.7792, 0.5469, 0.9299, "沿線MAP"],
                       [0.268, 0.9, 0.7289, 0.9833, "この“赤がえる”撮りたいの私"]])
    sign = dict(_ocr("o85", 128.6, 134.4, "駅前商店街\nようこそ", (0.3, 0.75, 0.7, 0.98)), kind="scene",
                pieces=[[0.3, 0.75, 0.7, 0.85, "駅前商店街"], [0.3, 0.88, 0.7, 0.98, "ようこそ"]])   # 和语音对不上：照旧丢掉
    asr = [{"id": "a3-2", "start": 126.0, "end": 129.5, "text": "ここ11時29分？降りました。"},
           {"id": "a3-3", "start": 130.1, "end": 132.2, "text": "いや、この赤蛙を撮りたいの、私。"}]
    out = fuse([], [box, sign], asr, FUSION, lang.get("ja"), 3600)
    items = {it["id"]: it for it in out["items"]}
    o = items["o84"]
    assert o["src"] == "telop" and o["text"] == "この“赤がえる”撮りたいの私" and o["rect"] == [0.268, 0.9, 0.7289, 0.9833]
    assert 129.7 <= o["start"] <= 130.1 and o["end"] <= 132.8
    assert "a3-3" not in items and "o85" not in items and sign["dropped"] == "scene"
    assert box["caption"]["from"].startswith("岳南電車") and out["stats"]["ocr_caption"] == 1


def test_caption_line_from_scene_box_waits_for_other_caption():
    """旗子上的字和下面的硬字幕「決めたくないとか」连成一框（整框出现的时间更早）：同一位置先出的是另一条硬字幕
    「自分の性別がわからないとか」，救回来的那一行时间让开它，排在它后面。"""
    box = dict(_ocr("o644", 2427.3, 2431.0, "DONYOKU\n決めたくないとか", (0.3375, 0.5736, 0.6672, 0.9986)), kind="scene",
               pieces=[[0.5453, 0.5736, 0.6672, 0.7972, "DONYOKU"], [0.3375, 0.825, 0.6602, 0.9986, "決めたくないとか"]])
    first = dict(_ocr("o645", 2427.4, 2429.4, "自分の性別がわからないとか", (0.264, 0.906, 0.737, 0.974)), kind="caption")
    asr = [{"id": "a1", "start": 2413.3, "end": 2441.0,
            "text": "自分の性別がわからないとか、決めたくないとか、決めつけられたくないっていう人が"}]
    out = fuse([], [box, first], asr, FUSION, lang.get("ja"), 3600)
    items = sorted(out["items"], key=lambda it: it["start"])
    assert [it["id"] for it in items[:2]] == ["o645", "o644"]
    assert items[1]["start"] == 2429.4 and items[1]["text"] == "決めたくないとか"


def test_info_translation_beside_box_when_above_and_below_blocked():
    """标题卡第二行“鮭とば”：上面是第一行，下面到画面边了，译文放在右边，不跑到画面顶部。"""
    cfg = config.Config()
    prof = ProfileCfg(name="t", layers=[LayerCfg(role="translation")])
    cue = {"id": "q449", "start": 1973.9, "end": 1980.5, "type": "info", "onscreen": True, "rect": [0.0492, 0.8306, 0.2742, 0.9931],
           "orig": {"lang": "ja", "text": "鮭とば"}, "tr": {"zh-Hans": {"text": "鲑鱼干"}}, "sources": ["o778"]}
    first_line = {"start": 1972.9, "end": 1980.5, "rect": [0.0211, 0.7472, 0.1922, 0.8514]}
    px, py, _, placed = ass.place(layout.build([cue], prof, cfg, "ja"), prof, cfg.render.styles, 16 / 9, [first_line])
    p = placed[0]
    assert p["an"] == 4 and p["x"] > 0.2742 * px and 0.83 * py < p["y"] < 0.99 * py


def test_subs_line_also_printed_on_screen_hides_original(sample):
    """字幕的这句话画面上也打着（画面字当成重复去掉了）：原文不再显示；画面上只打了句子里提到的地名，照常显示。"""
    from nyasub.core import pipeline

    r = pipeline.Run(sample, config.Config(), pipeline.Options())
    r.results = {
        "fuse": {"items": [{"id": "c470", "src": "subs", "start": 3111.6, "end": 3115.0, "text": "（取材者）百姓たちが作り上げた野菜。そう　農家が。"},
                           {"id": "c33", "src": "subs", "start": 162.4, "end": 166.0, "text": "舞台は日本海に　ほど近い　片野鴨池。"}],
                 "ocr_marks": {"o1041": {"dup_of": ["c470"]}, "o50": {"dup_of": ["c33"]}}},
        "ocr_refine": [dict(_ocr("o1041", 3109.9, 3116.5, "昔の百姓たちが作り上げた野菜\n農家が")),
                       dict(_ocr("o50", 162.0, 167.0, "片野鴨池", (0.6, 0.5, 0.8, 0.58)))],
    }
    cue = lambda i, sid, s, e, t: {"id": i, "start": s, "end": e, "type": "dialogue", "sources": [sid], "onscreen": False,
                                   "orig": {"lang": "ja", "text": t}, "tr": {"zh-Hans": {"text": "译"}}}
    out = {c["id"]: c for c in r._tidy([cue("q653", "c470", 3111.6, 3115.0, "（取材者）百姓たちが作り上げた野菜。　そう　農家が。"),
                                        cue("q33", "c33", 162.4, 166.0, "舞台は日本海に　ほど近い　片野鴨池。")], {})}
    assert out["q653"]["onscreen"] and not out["q33"]["onscreen"]


def test_vertical_name_card_translation_goes_outward():
    """竖排人名条：人一般在条的里侧（靠画面中间），译文放外侧；外侧放不下才放里侧；不压在条上。"""
    from nyasub.render.ass import _info_spot

    px, py, w, h = 1920, 1080, 460, 52
    it = lambda r: {"start": 0, "end": 3, "rect": list(r)}
    # 截图那张：条在画面中间偏右，人在左边 → 放右边
    an, x, y, bx0, by0 = _info_spot(it((0.527, 0.25, 0.612, 0.71)), True, w, h, px, py, [])
    assert an == 7 and bx0 >= 0.612 * px
    # 条贴着右边：右边放不下 → 放左边（里侧），也不压在条上
    an, x, y, bx0, by0 = _info_spot(it((0.88, 0.2, 0.95, 0.7)), True, w, h, px, py, [])
    assert bx0 + w <= 0.88 * px
    # 条在左半：放左边（外侧）
    an, x, y, bx0, by0 = _info_spot(it((0.30, 0.2, 0.36, 0.7)), True, w, h, px, py, [])
    assert an == 9 and bx0 + w <= 0.30 * px


def test_vertical_name_card_translation_is_vertical(tmp_path):
    """竖排人名条的中文译文也竖排：按空格分列（头衔一列、名字一列，从右往左），贴在竖条外侧；
    导出的 ASS 一个字一个事件；关掉开关、或者译成英文的照旧横排。"""
    cfg = config.Config()
    prof = ProfileCfg(name="t", layers=[LayerCfg(role="translation")])
    rect = [0.527, 0.25, 0.612, 0.71]
    cue = {"id": "q1", "start": 10, "end": 14, "type": "info", "onscreen": True, "rect": rect,
           "orig": {"lang": "ja", "text": "富士製紙協同組合\n相原和生さん"}, "tr": {"zh-Hans": {"text": "富士造纸合作社 相原和生"}}, "sources": ["o1"]}
    items = layout.build([cue], prof, cfg, "ja")
    px, py, _, placed = ass.place(items, prof, cfg.render.styles, 16 / 9, [])
    p = placed[0]
    cols = {}
    for x, y, ch in p["vchars"]:
        cols.setdefault(x, []).append(ch)
    xs = sorted(cols, reverse=True)
    assert ["".join(cols[x]) for x in xs] == ["富士造纸合作社", "相原和生"]   # 第一列在右边
    assert p["vbox"][0] >= rect[2] * px                                       # 在竖条外侧（右边）
    out = tmp_path / "v.ass"
    ass.write(out, items, prof, cfg.render.styles, 16 / 9, [])
    ev = [l for l in out.read_text(encoding="utf-8-sig").splitlines() if l.startswith("Dialogue")]
    assert len(ev) == 11 and all("{\\an5\\pos(" in l for l in ev)
    # 关掉开关：横排
    prof2 = ProfileCfg(name="t", layers=[LayerCfg(role="translation")], info_vertical=False)
    assert "vchars" not in ass.place(layout.build([cue], prof2, cfg, "ja"), prof2, cfg.render.styles, 16 / 9, [])[3][0]
    # 译成英文：横排
    en = dict(cue, tr={"en": {"text": "Kazuo Aihara"}})
    prof3 = ProfileCfg(name="t", layers=[LayerCfg(role="translation", lang="en")])
    assert "vchars" not in ass.place(layout.build([en], prof3, cfg, "ja"), prof3, cfg.render.styles, 16 / 9, [])[3][0]


def test_dialogue_vertical_preset_and_manual_override(tmp_path):
    """台词跟着竖排画面字时自动竖排；台词和说明都可以手动强制横排 / 竖排，并正常导出 ASS。"""
    cfg = config.Config()
    prof = ProfileCfg(name="t", layers=[LayerCfg(role="translation")])
    vrect = [0.75, 0.2, 0.81, 0.72]   # 竖排框
    hrect = [0.2, 0.8, 0.8, 0.88]     # 横排框
    dlg = {"id": "q1", "start": 1, "end": 4, "type": "dialogue", "onscreen": True, "near": vrect,
           "orig": {"lang": "ja", "text": "私を見つける"}, "tr": {"zh-Hans": {"text": "发现自己"}}, "sources": ["o1"]}
    # 1. 台词跟随竖排画面字：自动预设竖排，贴在竖条外侧（右边）
    items = layout.build([dlg], prof, cfg, "ja")
    px, _, _, placed = ass.place(items, prof, cfg.render.styles, 16 / 9, [])
    assert placed[0]["kind"] == "main" and len(placed[0]["vchars"]) == 4
    assert placed[0]["vbox"][0] >= vrect[2] * px
    out = tmp_path / "dlg_v.ass"
    ass.write(out, items, prof, cfg.render.styles, 16 / 9, [])
    ev = [l for l in out.read_text(encoding="utf-8-sig").splitlines() if l.startswith("Dialogue")]
    assert len(ev) == 4 and all("{\\an5\\pos(" in l for l in ev)
    # 2. 手动改横排：覆盖自动竖排
    dlg_h = dict(dlg, vertical=False)
    assert "vchars" not in ass.place(layout.build([dlg_h], prof, cfg, "ja"), prof, cfg.render.styles, 16 / 9, [])[3][0]
    # 3. 底部台词（无 near）手动改竖排：也能竖排
    dlg_bottom_v = {"id": "q2", "start": 5, "end": 8, "type": "dialogue", "onscreen": False, "vertical": True,
                    "orig": {"lang": "ja", "text": "風の中に"}, "tr": {"zh-Hans": {"text": "在风中"}}, "sources": ["c1"]}
    p_bv = ass.place(layout.build([dlg_bottom_v], prof, cfg, "ja"), prof, cfg.render.styles, 16 / 9, [])[3][0]
    assert len(p_bv["vchars"]) == 3
    # 4. 说明类横排框手动改竖排、竖排框手动改横排
    info_h_to_v = {"id": "q3", "start": 9, "end": 12, "type": "info", "onscreen": True, "rect": hrect, "vertical": True,
                   "orig": {"lang": "ja", "text": "青森"}, "tr": {"zh-Hans": {"text": "青森县"}}, "sources": ["o2"]}
    assert len(ass.place(layout.build([info_h_to_v], prof, cfg, "ja"), prof, cfg.render.styles, 16 / 9, [])[3][0]["vchars"]) == 3
    info_v_to_h = dict(info_h_to_v, rect=vrect, vertical=False)
    assert "vchars" not in ass.place(layout.build([info_v_to_h], prof, cfg, "ja"), prof, cfg.render.styles, 16 / 9, [])[3][0]


def test_edited_translation_keeps_segments(sample):
    """只改了译文（把放错段的半句剪掉）：原文照没改过时一样整理（落在同一条字幕中间的“｜”合回去），
    和译文段数一样，照常分段——不会因为对不上整条变成一段、“｜”露在字里。"""
    from nyasub.core import pipeline, segments

    r = pipeline.Run(sample, config.Config(), pipeline.Options())
    r.results = {"fuse": {"items": [
        {"id": "c500", "src": "subs", "start": 2435.8, "end": 2438.5, "text": "ペーパーキットって富士市\n紙のまちなんで"},
        {"id": "c501", "src": "subs", "start": 2438.5, "end": 2443.0, "text": "富士市でやらない手はないな\n会社辞めたらやろうかなって思ってたんですね"}]}}
    orig = "ペーパーキットって富士市｜紙のまちなんで｜富士市でやらない手はないな 会社辞めたらやろうかなって思ってたんですね"
    tr = "纸模型套件嘛，富士市是纸之城｜在富士市做再合适不过了 我当时想，等辞职后就来做"
    cue = {"id": "q457", "start": 2435.8, "end": 2443.0, "type": "dialogue", "sources": ["c500", "c501"], "onscreen": False,
           "orig": {"lang": "ja", "text": orig}, "tr": {"zh-Hans": {"text": tr}}}
    out = r._tidy([cue], {"q457": {"tr": {"zh-Hans": tr}}})[0]
    assert segments.pieces(out["orig"]["text"]) == ["ペーパーキットって富士市　紙のまちなんで", "富士市でやらない手はないな 会社辞めたらやろうかなって思ってたんですね"]
    assert out["tr"]["zh-Hans"]["text"] == tr and segments.bars_ok(out) and len(out["parts"]) == 2


def test_track_region_follows_overlay_on_busy_background(tmp_path):
    """本地漏掉的字（补出来的）前后找时间：满是边缘、一直在变的背景上叠着一块字，2~5 秒在画面上。"""
    import cv2
    import numpy as np

    from nyasub.sources.ocr import track_region

    path = str(tmp_path / "v.mp4")
    out = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 10, (640, 360))
    rng = np.random.default_rng(0)
    for i in range(80):
        img = rng.integers(0, 120, (360, 640, 3), dtype=np.uint8)  # 碎石一样的背景，每帧都不一样
        if 20 <= i < 50:
            cv2.putText(img, "SAYONARA", (120, 300), cv2.FONT_HERSHEY_SIMPLEX, 2.2, (255, 255, 255), 3)
        out.write(img)
    out.release()
    start, end = track_region(path, 3.5, [0.17, 0.68, 0.83, 0.86], aspect=16 / 9, window=8)
    assert abs(start - 2.0) <= 0.4 and abs(end - 5.0) <= 0.4


def test_refine_folds_duplicate_pieces_of_same_text():
    """本地切出的一小块单独复核成了整块字、或者只剩标题的一个字：并进认全的那条；不相干的、杂讯里的不并。"""
    from nyasub.llm.refine import fold_dups

    full = {"id": "o146", "start": 1199.07, "end": 1207.77, "text": "森のなかのふたり", "rect": [0.458, 0.731, 0.956, 0.862]}
    dup = {"id": "o147", "start": 1198.92, "end": 1207.72, "text": "森のなかのふたり", "rect": [0.459, 0.733, 0.952, 0.863]}
    one = {"id": "o148", "start": 1203.17, "end": 1207.37, "text": "森", "rect": [0.459, 0.732, 0.538, 0.806]}
    other = {"id": "o149", "start": 1200.0, "end": 1206.0, "text": "二人", "rect": [0.5, 0.75, 0.6, 0.8]}
    junk = {"id": "o200", "start": 1380.4, "end": 1384.2, "text": "シ\n16\n1\nルネサ", "rect": [0.308, 0.588, 0.359, 0.999], "dropped": "junk"}
    good = {"id": "v10", "start": 1380.4, "end": 1384.2, "text": "シ\n16\n1", "rect": [0.31, 0.584, 0.343, 0.774]}
    ev = [full, dup, one, other, junk, good]
    assert fold_dups(ev) == 2
    assert [e["id"] for e in ev] == ["o146", "o149", "o200", "v10"]
    assert full["start"] == 1198.92 and set(full["merged"]) == {"o147", "o148"}

    # 已确认的短字幕不能被并进未确认的大框长事件里
    unverified_big = {"id": "o280", "start": 1323.82, "end": 1333.52, "text": "よろしくお願いいたします",
                      "rect": [0.1375, 0.7306, 0.9148, 0.9861], "unverified": True}
    verified_cap = {"id": "o283", "start": 1330.02, "end": 1332.62, "text": "よろしくお願いいたします",
                    "rect": [0.2918, 0.9042, 0.7047, 0.9833]}
    ev2 = [unverified_big, verified_cap]
    assert fold_dups(ev2) == 0
    assert [e["id"] for e in ev2] == ["o280", "o283"]


def test_music_mark_subtitle_does_not_stretch_cue():
    """「♬〜」字幕一挂十几秒，和片名并成一条时时间按片名的；只有它自己时照旧。"""
    from nyasub.llm.translate import make_cue

    music = {"id": "c133", "src": "subs", "start": 1193.334, "end": 1209.134, "text": "♬〜"}
    title = {"id": "o146", "src": "info", "start": 1199.065, "end": 1207.765, "text": "森のなかのふたり",
             "rect": [0.458, 0.731, 0.956, 0.862], "ocr_score": 0.985}
    c = make_cue([music, title], "森のなかのふたり", "森林中的两人", "zh-Hans", "info", "ja")
    assert (c["start"], c["end"]) == (1199.065, 1207.765) and c["sources"] == ["c133", "o146"]
    c = make_cue([music], "♬〜", "♬～", "zh-Hans", "song", "ja")
    assert (c["start"], c["end"]) == (1193.334, 1209.134)


def test_refine_miss_found_by_local_a_moment_later_not_added(tmp_path, monkeypatch):
    """模型在某一刻读到的字，本地隔一下（0.25 秒后）才认出来：不另补一条（找不到时间时按前后各 1 秒，译文会提前出来），
    本地那条的开始放宽到这一刻；同一位置换了一句的照样补。"""
    import cv2
    import numpy as np

    from nyasub.core.config import Config
    from nyasub.llm import refine
    from nyasub.sources import media, ocr

    frame = tmp_path / "frame.jpg"
    cv2.imwrite(str(frame), np.full((2160, 3840, 3), 90, np.uint8))
    monkeypatch.setattr(media, "grab_frames", lambda video, times, out, **kw: {round(t, 3): frame for t in times})
    monkeypatch.setattr(ocr, "track_region", lambda video, t, rect, aspect=16 / 9: (round(t - 1, 3), round(t + 1, 3)))
    (tmp_path / "crops").mkdir()
    video = tmp_path / "v.ts"
    video.write_bytes(b"x")
    bottle = {"id": "o619", "start": 2367.565, "end": 2368.165, "snap_t": 2367.715, "rect": [0.2852, 0.7722, 0.325, 0.8042],
              "text": "BACARDI", "vertical": False, "lines": 1, "crop": "crops/o619.jpg", "pieces": []}
    line = {"id": "o620", "start": 2367.965, "end": 2371.765, "snap_t": 2368.115, "rect": [0.364, 0.844, 0.631, 0.978],
            "text": "ここがないと多分\nだめになると思う", "vertical": False, "lines": 2, "pieces": [], "text_local": "x"}
    said = "ここがないと多分 / だめになると思う"

    class Fake:
        def chat_json(self, system, user, images=None):
            if system == refine.READ_SYSTEM:
                return {"items": [{"text": "BACARDI", "kind": "scene", "bbox": [285, 772, 325, 804]},
                                  {"text": said, "kind": "caption", "bbox": [363, 844, 635, 975]}]}
            if "ここがないと" in user:  # 补出的字幕要特写再读一遍作证
                return {"text": said, "kind": "caption"}
            return {"text": "BACARDI", "kind": "scene"}

    import nyasub.llm.client as client
    monkeypatch.setattr(client, "LLM", lambda *a, **k: Fake())
    out = refine.refine_ocr([bottle, line], Config(), str(tmp_path), None, video=str(video), size=(3840, 2160))
    assert not [e for e in out if e.get("found_by") == "vlm"]
    assert line["start"] == 2367.715 and line["end"] == 2371.765

    other = dict(line, id="o621", text="居場所がないと思う", start=2367.965)
    bottle.pop("text_local", None)
    out = refine.refine_ocr([dict(bottle), other], Config(), str(tmp_path), None, video=str(video), size=(3840, 2160))
    assert [e["text"] for e in out if e.get("found_by") == "vlm"] == ["ここがないと多分\nだめになると思う"]


def test_dialogue_can_follow_its_screen_text():
    """画面上打出来的台词默认在底部；选了跟着那块画面字，就贴在那块字旁边，和底部同时说的另一句各放各的。"""
    cfg = config.Config()
    prof = ProfileCfg(name="t", layers=[LayerCfg(role="translation")])

    def cue(i, text, near=None):
        return {"id": i, "start": 10.0, "end": 14.0, "type": "dialogue", "onscreen": True, "sources": [],
                "orig": {"lang": "ja", "text": "あ"}, "tr": {"zh-Hans": {"text": text}}, **({"near": near} if near else {})}

    left = [0.05, 0.2, 0.3, 0.3]  # 左边那个人的话
    items = layout.build([cue("q1", "左边的人说的话", left), cue("q2", "底部的一句")], prof, cfg, "ja")
    px, py, _, placed = ass.place(items, prof, cfg.render.styles, 16 / 9, [])
    p = {x["item"]["cue"]: x for x in placed}
    assert p["q1"]["x"] < 0.5 * px and 0.3 * py < p["q1"]["y"] < 0.5 * py  # 贴在左上那块字下面
    assert p["q2"].get("x") is None and p["q2"]["item"]["end"] == 14.0     # 底部那句照常，时间不被截
    assert layout.build([cue("q3", "x", [0.5, 0.5, 0.4, 0.6])], prof, cfg, "ja")[0]["near"] is None  # 不像框的不算


def test_vlm_duplicate_of_local_caption_dropped():
    """视觉复核补出的字和本地认出的同一句（框不对、时间早了 1.4 秒）：丢掉补出的，以本地为准；别的补出来的照旧留着。"""
    local = dict(_ocr("o839", 3254.0, 3258.7, "命日にお経さ読んで\n参っとったんやけど", (0.3336, 0.8347, 0.6648, 0.9819)), kind="caption")
    dup = dict(_ocr("v207", 3252.6, 3254.4, "命日にお経さ読んで\n参っとったんやけど", (0.337, 0.605, 0.851, 0.707)),
               kind="caption", found_by="vlm")
    new = dict(_ocr("v208", 3260.0, 3262.0, "仏壇も変になっとる", (0.3, 0.85, 0.7, 0.97)), kind="caption", found_by="vlm")
    out = fuse([], [local, dup, new], [], FUSION, lang.get("ja"), 3600)
    assert [it["id"] for it in out["items"]] == ["o839", "v208"] and dup["dropped"] == "dup_vlm"


def test_glossary_enforced_for_every_type_and_short_names():
    from nyasub.llm.translate import restore_labels, term_misses

    terms = [{"src": "ミーちゃん", "tr": "牛奶", "type": "人名"}, {"src": "みーちゃん", "tr": "牛奶", "type": "人名"},
             {"src": "さしみこんにゃく", "tr": "刺身魔芋", "type": "物品"}, {"src": "中村", "tr": "中村", "type": "人名"}]
    cues = [{"orig": {"text": "ミーちゃん！"}, "tr": {"zh-Hans": {"text": "咪酱！"}}},
            {"orig": {"text": "もちろん　出来たて　さしみこんにゃく！"}, "tr": {"zh-Hans": {"text": "当然是刚做好的生鱼片魔芋！"}}},
            {"orig": {"text": "まーみー 豚肉ちょうだい"}, "tr": {"zh-Hans": {"text": "真美子 给我猪肉"}}},   # “みー”只是半截
            {"orig": {"text": "ばーちゃんと みーちゃんの家"}, "tr": {"zh-Hans": {"text": "奶奶和牛奶的家"}}},
            {"orig": {"text": "（中村）これがね…"}, "tr": {"zh-Hans": {"text": "这个啊……"}}},
            {"orig": {"text": "（妻は）図書館で働いてた"}, "tr": {"zh-Hans": {"text": "原本在图书馆工作"}}}]
    assert term_misses(cues, terms, "zh-Hans") == [(0, [("ミー", "牛奶")]), (1, [("さしみこんにゃく", "刺身魔芋")]),
                                                   (4, [("中村", "中村")])]
    restore_labels(cues, terms, "zh-Hans")
    assert cues[4]["tr"]["zh-Hans"]["text"] == "（中村）这个啊……"
    assert cues[5]["tr"]["zh-Hans"]["text"] == "原本在图书馆工作"   # 不是名字的标签不补


def test_vlm_text_read_slightly_outside_excluded_region_still_excluded():
    """台标下面的「私の居場所」实际在排除区里，模型给的框往下偏了一点、中心落到排除区外：大半在外扩一圈里，照样排除；
    底部的字幕、旁边的竖排字不受影响。"""
    from nyasub.llm.refine import read_items

    regions = [{"rect": [0.824, 0.025, 0.976, 0.181], "exclude": True}]
    raw = {"items": [{"text": "私の居場所", "kind": "caption", "bbox": [850, 188, 947, 227]},
                     {"text": "新日本風土記", "kind": "caption", "bbox": [824, 49, 968, 138]},
                     {"text": "居場所がないと思う", "kind": "caption", "bbox": [340, 914, 661, 980]},
                     {"text": "弱者の居場所がない社会", "kind": "scene", "bbox": [808, 353, 836, 891]}]}
    assert [it["text"] for it in read_items(raw, regions)] == ["居場所がないと思う", "弱者の居場所がない社会"]


def test_model_box_too_flat_for_its_text_is_clipped():
    """模型的框只框了字的下面一条（「回せ回せ回せ」）：扁得装不下这几个字，用本地的框；
    模型的框比本地的小但装得下（本地的框连着树叶，「とうとう」）：照用模型的框。"""
    from nyasub.llm.refine import _clipped

    asp = 16 / 9
    assert _clipped([0.389, 0.96, 0.61, 0.979], "回せ回せ回せ", False, asp)
    assert not _clipped([0.3836, 0.9069, 0.6133, 0.9792], "回せ回せ回せ", False, asp)
    assert not _clipped([0.444, 0.914, 0.556, 0.975], "とうとう", False, asp)
    assert not _clipped([0.45, 0.916, 0.551, 0.968], "この戸", False, asp)
    assert _clipped([0.90, 0.20, 0.905, 0.60], "縦書きの字幕です", True, asp)


def test_close_up_blacks_out_excluded_regions(tmp_path):
    """截大框的特写时四周多留的一圈会截到台标：排除区（外扩一圈）涂黑，模型就读不到台标、不会当成这块的字。"""
    import cv2
    import numpy as np

    from nyasub.llm import refine

    img = np.full((200, 400, 3), 255, np.uint8)
    path = tmp_path / "f.png"
    cv2.imwrite(str(path), img)
    regions = [{"rect": [0.8, 0.0, 1.0, 0.2], "exclude": True}, {"rect": [0.0, 0.0, 0.1, 0.1], "exclude": False}]
    plain = cv2.imread(refine._Frame(path, str(tmp_path), 400).crop([0.5, 0.15, 0.95, 0.6], "a"))
    masked = cv2.imread(refine._Frame(path, str(tmp_path), 400, mask=regions).crop([0.5, 0.15, 0.95, 0.6], "b"))
    assert plain.min() > 200
    assert masked[0, -1].max() < 30 and masked[-1, 0].min() > 200  # 右上角（台标）涂黑，别处不动


def test_logo_text_is_not_taken_for_a_caption():
    """排除区（台标）里读到「新日本風土記 / 富士南麓 鉄道の旅」；人名条那一刻字已经没了，特写里模型照着片名写了「富士南麓」：不能用。
    本地这块字里本来就有的（真的写着片名的标题）照常用。"""
    from nyasub.llm.refine import logo_text, read_items

    regions = [{"rect": [0.824, 0.025, 0.976, 0.181], "exclude": True}]
    raw = {"items": [{"text": "新日本風土記 / 富士南麓 鉄道の旅", "kind": "caption", "bbox": [827, 30, 975, 176]},
                     {"text": "住職 / 遠藤文祥上人", "kind": "caption", "bbox": [772, 272, 878, 741]}]}
    logo = set()
    assert [it["text"] for it in read_items(raw, regions, logo)] == ["住職\n遠藤文祥上人"]
    assert logo_text("富士南麓", "富市柏果町木林满代\n住職\n遠藤文祥上人", logo)
    assert not logo_text("住職 / 遠藤文祥上人", "住職\n遠藤文祥上人", logo)
    assert not logo_text("富士南麓 鉄道の旅", "富士南麓鉄道の旅", logo)


def test_screen_only_narration_without_voice_is_info():
    """配乐时打在画面上的叙述（只有画面字、语音没念）是说明，不是旁白；画面字和语音念的一样的（烧进画面的旁白字幕）照旧是旁白。"""
    from nyasub.llm.translate import fix_type

    caption = {"src": "telop", "text": "今も子どもたちや鉄道ファンを笑顔に", "asr_hint": "おーい、お母さん。"}
    assert fix_type("narration", [caption]) == "info"
    assert fix_type("narration", [dict(caption, asr_hint="")]) == "info"
    voiced = dict(caption, asr_hint="今も子どもたちや鉄道ファンを笑顔にしています")
    assert fix_type("narration", [voiced]) == "narration"
    assert fix_type("narration", [{"src": "subs", "text": "午前６時。"}]) == "narration"
    assert fix_type("dialogue", [caption]) == "dialogue"


def test_terms_and_translations_with_kana_are_fixed_into_chinese():
    """术语表的译法夹着假名（「ヘビトンボ幼虫（孙太郎）」）：先改成中文；改不好的不再拿来统一译名（照它改只会把假名搬进译文）。
    最后译文里还有假名的，请模型只改那几处，分段 / 换行对不上、还留着假名的不收。"""
    from types import SimpleNamespace

    from nyasub.llm.translate import fix_leftover, fix_terms, term_misses

    class LLM:
        def __init__(self, answer):
            self.answer, self.calls = answer, []

        def chat_json(self, system, user):
            self.calls.append((system, user))
            return self.answer

    ja, zh = lang.get("ja"), lang.get("zh-Hans")
    terms = [{"src": "孫太郎", "reading": "まごたろう", "tr": "ヘビトンボ幼虫（孙太郎）", "type": "物品"},
             {"src": "ざざ虫", "tr": "ざざ虫（扎扎虫）", "type": "物品"},
             {"src": "慎也", "tr": "慎也さん", "type": "人名"},
             {"src": "天竜川", "tr": "天龙川", "type": "地名"},
             {"src": "孫太郎（ヘビトンボ）", "tr": "孙太郎（dobsonfly幼虫）", "type": "人名"},  # 没译出来的英文普通名词也算
             {"src": "ジャトコ前", "tr": "JATCO前", "type": "场所"}]                       # 大写的英文写法照常用
    llm = LLM({"terms": [{"id": "t1", "tr": "齿蛉幼虫（孙太郎）"}, {"id": "t2", "tr": "ざざ虫"}, {"id": "t4", "tr": "孙太郎（dobsonfly幼虫）"}]})
    fix_terms(llm, terms, ja, zh, "专有名词用俗名、学名")
    assert "专有名词用俗名、学名" in llm.calls[0][0] and "天竜川" not in llm.calls[0][1] and "JATCO" not in llm.calls[0][1]
    assert [t["tr"] for t in terms] == ["齿蛉幼虫（孙太郎）", "ざざ虫（扎扎虫）", "慎也さん", "天龙川",
                                        "孙太郎（dobsonfly幼虫）", "JATCO前"]  # 模型还写假名 / 英文的不收
    cues = [{"id": "q1", "orig": {"text": "ざざ虫を食べる　慎也さん"}, "tr": {"zh-Hans": {"text": "吃扎扎虫的慎也"}}},
            {"id": "q2", "orig": {"text": "これ 孫太郎"}, "tr": {"zh-Hans": {"text": "这是孙太郎"}}}]
    assert term_misses(cues, terms, "zh-Hans") == [(1, [("孫太郎", "齿蛉幼虫")])]  # 没改好的两条不拿来统一

    cues = [{"id": "q1", "orig": {"text": "ざざ虫で乾杯やわ"}, "tr": {"zh-Hans": {"text": "为ざざ虫干杯！"}}},
            {"id": "q2", "orig": {"text": "天竜川の｜ざざ虫"}, "tr": {"zh-Hans": {"text": "天龙川的｜ざざ虫"}}},
            {"id": "q3", "orig": {"text": "どうですか"}, "tr": {"zh-Hans": {"text": "怎么样"}}}]
    llm = LLM({"items": [{"id": "q1", "tr": "为扎扎虫干杯！"}, {"id": "q2", "tr": "天龙川的扎扎虫"}]})
    fix_leftover(llm, cues, terms, ja, zh, "zh-Hans", SimpleNamespace(project=SimpleNamespace(context="")))
    assert "q3" not in llm.calls[0][1] and "天竜川 → 天龙川" in llm.calls[0][1]
    assert [c["tr"]["zh-Hans"]["text"] for c in cues] == ["为扎扎虫干杯！", "天龙川的｜ざざ虫", "怎么样"]  # 分段对不上的不收


def test_settle_keeps_confident_local_text():
    """有证据才用：本地有把握的字幕，本地读出的字照留（同一个模型整帧、特写两次都读错，「冬を食べる」里的真实例子）；
    模型只能补本地没读到的整行、行头行尾，或改本地常认混的形近字，而且特写里也得有。
    本地没把握（乱码）时，两次读法整句很像就用模型的；都对不上标为没证据。"""
    from nyasub.llm.refine import _settle

    # 模型两次都改了本地读清楚的字：用本地的
    assert _settle("鮭とは", "鮭とは", "鮭とば", 0.987) == ("鮭とば", True)
    assert _settle("蓬間 正樹さん", "蓬間 正樹さん", "差間正樹さん", 0.998) == ("差間正樹さん", True)
    two = "中嶋さんは凍みこんにゃくを作り続ける\n数少ない農家"
    assert _settle(two, two, "中嶋さんは凍みこんにゃく作りを守る\n数少ない農家", 0.997) == \
        ("中嶋さんは凍みこんにゃく作りを守る\n数少ない農家", True)
    assert _settle("よう泳いできてくれたと思いました", "よう泳いできてくれたと思いました",
                   "よう泳いできてくれた思いました", 0.999) == ("よう泳いできてくれた思いました", True)
    assert _settle("ひとり", "ひとり", "ひとり独身", 0.998) == ("ひとり独身", True)
    assert _settle("ナマコ作り", "クチコの作り", "クチコ作り", 0.985) == ("クチコ作り", True)
    # 模型补本地没读到的整行、行尾：特写里也有才补；本地那行照留
    m = "こんなんありますよ\n世の中に知らせたい"
    assert _settle(m, m, "世の中に知らしめたい", 1.0) == ("こんなんありますよ\n世の中に知らしめたい", True)
    assert _settle(m, "世の中に知らせたい", "世の中に知らしめたい", 1.0) == ("世の中に知らしめたい", True)  # 特写里没有那行
    m = "部長とか課長とかになったり\n「失敗したかな」とか"
    assert _settle(m, m, "「失敗したかな」とか言う時ある", 0.999) == ("部長とか課長とかになったり\n「失敗したかな」とか言う時ある", True)
    assert _settle("これ以上のことはないべ", "これ以上のことはないべ", "これ以上のことはない", 0.992) == ("これ以上のことはないべ", True)
    assert _settle("これ以上のことはないべ", "", "これ以上のことはない", 0.992) == ("これ以上のことはない", True)
    # 形近字：本地认混的用模型的
    assert _settle("カーブを曲がった", "カーブを曲がった", "カ一ブを曲がった", 0.984) == ("カーブを曲がった", True)
    assert _settle("2019年 アイヌ施策推進法", "2019年 アイヌ施策推進法", "2019年アイ又施策推進法", 0.966) == ("2019年 アイヌ施策推進法", True)
    assert _settle("NHKニュース 九州で", "", "ER", 0.959) == ("ER", True)
    # 滚动的片尾字幕：本地是各帧拼的，两次读法一致就用模型的
    m = "映像技術　西田 彩華\n音響効果　河原 久美子"
    assert _settle(m, m, "西田彩華映像技術\n音響効果　河原 久美子", 0.997, moving=True) == (m, True)
    assert _settle(m, "映像技術", "西田彩華映像技術", 0.997, moving=True) == ("西田彩華映像技術", True)
    # 本地没把握
    assert _settle("サヨナラまでの日々", "サヨナラまでの日々", "サヨナラ", 0.7) == ("サヨナラまでの日々", True)
    assert _settle("三が日用の分に", "加熱用のわに", "加他用のbに", 0.709) == ("加熱用のわに", False)


def test_organize_restores_subtitle_lines_the_model_left_out():
    """整理时大模型整行漏抄了字幕（城崎温泉 q41：两行的「水鳥が快適に生息できる環境が／守られていると➡」只抄了第一行）：
    补回同一条字幕前一行的后面；括号里的说明、已经抄了的不动。"""
    from nyasub.llm.translate import apply_result

    chunk = [{"id": "c27", "src": "subs", "start": 189.62, "end": 193.52, "text": "水鳥が快適に生息できる環境が\n守られていると➡"},
             {"id": "c28", "src": "subs", "start": 193.52, "end": 196.0, "text": "下流の水田や湿地とともに➡"},
             {"id": "c29", "src": "subs", "start": 196.0, "end": 200.43, "text": "ラムサール条約に 登録されています｡"},
             {"id": "c30", "src": "subs", "start": 201.0, "end": 203.0, "text": "(げたの音)\nいい音ですね"}]
    result = {"cues": [{"src": ["c27", "c28", "c29"], "type": "narration",
                        "orig": "水鳥が快適に生息できる環境が｜下流の水田や湿地とともに｜ラムサール条約に 登録されています｡"},
                       {"src": ["c30"], "type": "dialogue", "orig": "いい音ですね"}]}
    cues, missing = apply_result(chunk, result, "zh-Hans", "ja")
    assert not missing
    assert cues[0]["orig"]["text"] == "水鳥が快適に生息できる環境が\n守られていると｜下流の水田や湿地とともに｜ラムサール条約に 登録されています｡"
    assert cues[1]["orig"]["text"] == "いい音ですね"   # 只是括号里的说明，整理有意拿掉的不补


def test_organize_drops_lines_its_sources_do_not_support():
    """整理把乱码“纠正”成一句台词（「加他用のbに」→「三が日用の分に」）：画面字里一半都对不上，不要；只是改了错字的照旧。"""
    items = [{"id": "o245", "src": "telop", "start": 672.0, "end": 673.6, "text": "加他用のbに"},
             {"id": "o244", "src": "telop", "start": 671.9, "end": 675.4, "text": "お正月用と年末用"},
             {"id": "o559", "src": "info", "start": 1165.0, "end": 1170.4, "text": "西和箕町の良さを\n全国に発信しよう"}]
    res = {"cues": [{"src": ["o245"], "type": "dialogue", "orig": "三が日用の分に"},
                    {"src": ["o244"], "type": "dialogue", "orig": "お正月用と年末用"},
                    {"src": ["o559"], "type": "info", "orig": "西和賀の良さを\n全国に発信しよう"}]}
    cues, missing = apply_result(items, res, "zh-Hans", "ja")
    assert not missing and [c["sources"] for c in cues] == [["o244"], ["o559"]]


def test_refine_frames_concurrency_preserves_exact_results(tmp_path, monkeypatch):
    """frame 模式并发复核：OpenAI 接口确实并发请求，且结果与串行完全一致。"""
    import copy
    import threading
    import time

    import cv2
    import numpy as np

    import nyasub.llm.client as client
    from nyasub.core.config import Config
    from nyasub.llm import refine
    from nyasub.sources import media, ocr

    frame = tmp_path / "frame.jpg"
    cv2.imwrite(str(frame), np.full((1080, 1920, 3), 90, np.uint8))
    monkeypatch.setattr(media, "grab_frames", lambda video, times, out, **kw: {round(t, 3): frame for t in times})
    monkeypatch.setattr(ocr, "track_region", lambda video, t, rect, aspect=16 / 9: (round(t - 1, 3), round(t + 1, 3)))
    (tmp_path / "crops").mkdir()
    video = tmp_path / "v.ts"
    video.write_bytes(b"x")

    # 3 个时刻（其中第 1 帧放 2 条与本地差距大的字触发同帧并发特写），加 1 条本地漏检
    base_events = [
        {"id": "o1", "start": 10.0, "end": 12.0, "snap_t": 11.0, "rect": [0.1, 0.8, 0.4, 0.9], "text": "误认甲",
         "vertical": False, "lines": 1, "crop": "crops/o1.jpg", "pieces": [[0.1, 0.8, 0.4, 0.9, "误认甲"]]},
        {"id": "o2", "start": 10.0, "end": 12.0, "snap_t": 11.0, "rect": [0.5, 0.8, 0.8, 0.9], "text": "误认乙",
         "vertical": False, "lines": 1, "crop": "crops/o2.jpg", "pieces": [[0.5, 0.8, 0.8, 0.9, "误认乙"]]},
        {"id": "o3", "start": 20.0, "end": 22.0, "snap_t": 21.0, "rect": [0.1, 0.8, 0.5, 0.9], "text": "正字丙",
         "vertical": False, "lines": 1, "crop": "crops/o3.jpg", "pieces": [[0.1, 0.8, 0.5, 0.9, "正字丙"]]},
        {"id": "o4", "start": 30.0, "end": 32.0, "snap_t": 31.0, "rect": [0.1, 0.8, 0.5, 0.9], "text": "误认丁",
         "vertical": False, "lines": 1, "crop": "crops/o4.jpg", "pieces": [[0.1, 0.8, 0.5, 0.9, "误认丁"]]},
    ]

    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    class Fake:
        kind = "openai"

        def chat_json(self, system, user, images=None):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            try:
                time.sleep(0.05)
                if system == refine.READ_SYSTEM:
                    name = Path_name(images[0]["path"])
                    if name.startswith("000011000"):
                        return {"items": [
                            {"text": "正解甲", "kind": "caption", "bbox": [100, 800, 400, 900]},
                            {"text": "正解乙", "kind": "caption", "bbox": [500, 800, 800, 900]},
                            {"text": "招牌丁", "kind": "scene", "bbox": [150, 200, 450, 350]},
                        ]}
                    if name.startswith("000021000"):
                        return {"items": [{"text": "正字丙", "kind": "caption", "bbox": [100, 800, 500, 900]}]}
                    return {"items": [{"text": "正解丁", "kind": "caption", "bbox": [100, 800, 500, 900]}]}
                if "误认甲" in user:
                    return {"text": "正解甲", "kind": "caption"}
                if "误认乙" in user:
                    return {"text": "正解乙", "kind": "caption"}
                if "误认丁" in user:
                    return {"text": "正解丁", "kind": "caption"}
                return {"text": "招牌丁", "kind": "scene"}
            finally:
                with lock:
                    state["active"] -= 1

    def Path_name(p):
        import os
        return os.path.basename(p)

    monkeypatch.setattr(client, "LLM", lambda *a, **k: Fake())
    cfg1 = Config()
    cfg1.ocr.refine_concurrency = 1
    out1 = refine.refine_ocr(copy.deepcopy(base_events), cfg1, str(tmp_path), None, video=str(video), size=(1920, 1080))
    assert state["peak"] == 1

    state["active"] = state["peak"] = 0
    cfg4 = Config()
    cfg4.ocr.refine_concurrency = 4
    out4 = refine.refine_ocr(copy.deepcopy(base_events), cfg4, str(tmp_path), None, video=str(video), size=(1920, 1080))
    assert state["peak"] >= 2
    assert out4 == out1


def test_refine_cross_frame_recheck_concurrency(tmp_path, monkeypatch):
    """跨帧特写并发：即使每帧只有 1 处特写，整批 12 帧里的特写也能跨帧并发且结果完全一致。"""
    import copy
    import os
    import threading
    import time

    import cv2
    import numpy as np

    import nyasub.llm.client as client
    from nyasub.core.config import Config
    from nyasub.llm import refine
    from nyasub.sources import media, ocr

    frame = tmp_path / "frame.jpg"
    cv2.imwrite(str(frame), np.full((1080, 1920, 3), 90, np.uint8))
    monkeypatch.setattr(media, "grab_frames", lambda video, times, out, **kw: {round(t, 3): frame for t in times})
    monkeypatch.setattr(ocr, "track_region", lambda video, t, rect, aspect=16 / 9: (round(t - 1, 3), round(t + 1, 3)))
    (tmp_path / "crops").mkdir()
    video = tmp_path / "v.ts"
    video.write_bytes(b"x")

    # 4 个不同时刻，每帧各自只有 1 处差异很大的字（单帧内永远只能达到 1 并发）
    base_events = [
        {"id": f"e{i}", "start": i * 10.0, "end": i * 10.0 + 2.0, "snap_t": i * 10.0 + 1.0,
         "rect": [0.1, 0.8, 0.5, 0.9], "text": f"误认{i}", "vertical": False, "lines": 1,
         "crop": f"crops/e{i}.jpg", "pieces": [[0.1, 0.8, 0.5, 0.9, f"误认{i}"]]}
        for i in range(4)
    ]

    lock = threading.Lock()
    state = {"rc_active": 0, "rc_peak": 0}

    class FakeCross:
        kind = "openai"

        def chat_json(self, system, user, images=None):
            if system == refine.READ_SYSTEM:
                idx = int(os.path.basename(images[0]["path"]).split(".")[0]) // 10000
                return {"items": [{"text": f"正解{idx}", "kind": "caption", "bbox": [100, 800, 500, 900]}]}
            with lock:
                state["rc_active"] += 1
                state["rc_peak"] = max(state["rc_peak"], state["rc_active"])
            try:
                time.sleep(0.05)
                for i in range(4):
                    if f"误认{i}" in user:
                        return {"text": f"正解{i}", "kind": "caption"}
                return {"text": "", "kind": "caption"}
            finally:
                with lock:
                    state["rc_active"] -= 1

    monkeypatch.setattr(client, "LLM", lambda *a, **k: FakeCross())
    cfg1 = Config()
    cfg1.ocr.refine_concurrency = 1
    out1 = refine.refine_ocr(copy.deepcopy(base_events), cfg1, str(tmp_path), None, video=str(video), size=(1920, 1080))
    assert state["rc_peak"] == 1

    state["rc_active"] = state["rc_peak"] = 0
    cfg4 = Config()
    cfg4.ocr.refine_concurrency = 4
    out4 = refine.refine_ocr(copy.deepcopy(base_events), cfg4, str(tmp_path), None, video=str(video), size=(1920, 1080))
    assert state["rc_peak"] >= 2
    assert out4 == out1


def test_refine_crop_concurrency_and_cancel_and_cli_serial(tmp_path, monkeypatch):
    """crop 模式并发一致、取消事件及时中断排队任务，以及本机 CLI 保持串行。"""
    import copy
    import threading
    import time

    import cv2
    import numpy as np
    import pytest

    import nyasub.llm.client as client
    from nyasub import env
    from nyasub.core.config import Config
    from nyasub.llm import refine, refine_crop
    from nyasub.sources import media

    frame = tmp_path / "frame.jpg"
    cv2.imwrite(str(frame), np.full((720, 1280, 3), 120, np.uint8))
    monkeypatch.setattr(media, "grab_frames", lambda video, times, out, **kw: {round(t, 3): frame for t in times})
    crops = tmp_path / "crops"
    crops.mkdir()
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    base_events = []
    for i in range(4):
        (crops / f"e{i}.jpg").write_bytes(b"img%d" % i)
        base_events.append({
            "id": f"e{i}", "start": i * 10.0, "end": i * 10.0 + 2.0, "snap_t": i * 10.0 + 1.0,
            "rect": [0.1, 0.8, 0.4, 0.9], "snap_rect": [0.1, 0.8, 0.4, 0.9],
            "text": f"错字{i}", "vertical": False, "lines": 1, "crop": f"crops/e{i}.jpg",
        })

    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    class FakeCrop:
        kind = "openai"

        def chat_json(self, system, user, images=None):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            try:
                time.sleep(0.05)
                # 根据 hint 里的 "错字N" 精准返回
                for i in range(4):
                    if f"错字{i}" in user:
                        return {"items": [{"no": 1, "text": f"正字{i}", "kind": "scene"}]}
                return {"items": [{"no": 1, "text": "未知", "kind": "scene"}]}
            finally:
                with lock:
                    state["active"] -= 1

    monkeypatch.setattr(client, "LLM", lambda *a, **k: FakeCrop())
    cfg1 = Config()
    cfg1.ocr.refine_concurrency = 1
    out1 = refine_crop.refine_ocr(copy.deepcopy(base_events), cfg1, str(tmp_path), None, batch=1,
                                  video=str(video), context=True, boxed=True)
    assert state["peak"] == 1

    state["active"] = state["peak"] = 0
    cfg4 = Config()
    cfg4.ocr.refine_concurrency = 4
    out4 = refine_crop.refine_ocr(copy.deepcopy(base_events), cfg4, str(tmp_path), None, batch=1,
                                  video=str(video), context=True, boxed=True)
    assert state["peak"] >= 2
    assert out4 == out1

    # 本机 CLI（agy / codex）自动保持串行
    class FakeCli:
        kind = "agy"

    assert refine.refine_workers(cfg4, FakeCli()) == 1

    # 取消信号可及时中断 parallel_map
    cancel_ev = threading.Event()
    started = threading.Event()

    def slow_job(x):
        started.set()
        for _ in range(40):
            env.check_cancel()
            time.sleep(0.02)
        return x

    env.set_cancel(cancel_ev)
    try:
        threading.Timer(0.05, cancel_ev.set).start()
        with pytest.raises(env.Cancelled):
            refine.parallel_map(list(range(8)), slow_job, workers=4)
        assert started.is_set()
    finally:
        env.set_cancel(None)


def test_refine_skip_small_scene_option(tmp_path, monkeypatch):
    """开启 refine_skip_small_scene 后：整帧已判为 scene 且字很小的条不再发特写请求，小字 caption 照常特写复核。"""
    import copy

    import cv2
    import numpy as np

    import nyasub.llm.client as client
    from nyasub.core.config import Config
    from nyasub.llm import refine
    from nyasub.sources import media, ocr

    frame = tmp_path / "frame.jpg"
    cv2.imwrite(str(frame), np.full((1080, 1920, 3), 90, np.uint8))
    monkeypatch.setattr(media, "grab_frames", lambda video, times, out, **kw: {round(t, 3): frame for t in times})
    monkeypatch.setattr(ocr, "track_region", lambda video, t, rect, aspect=16 / 9: (round(t - 1, 3), round(t + 1, 3)))
    (tmp_path / "crops").mkdir()
    video = tmp_path / "v.ts"
    video.write_bytes(b"x")

    # 本地已有 1 条与整帧一致的小字 scene；同一帧模型另外新读出 1 条小字 scene 和 1 条小字 caption
    base_events = [
        {"id": "o1", "start": 10.0, "end": 12.0, "snap_t": 11.0, "rect": [0.1, 0.2, 0.25, 0.22],
         "text": "神社絵馬", "vertical": False, "lines": 1, "crop": "crops/o1.jpg",
         "pieces": [[0.1, 0.2, 0.25, 0.22, "神社絵馬"]]},
    ]
    recheck_calls = []

    class Fake:
        kind = "openai"

        def chat_json(self, system, user, images=None):
            if system == refine.READ_SYSTEM:
                return {"items": [
                    {"text": "神社絵馬", "kind": "scene", "bbox": [100, 200, 250, 220]},
                    {"text": "遠景看板", "kind": "scene", "bbox": [600, 200, 750, 220]},
                    {"text": "小字旁白", "kind": "caption", "bbox": [200, 880, 500, 905]},
                ]}
            recheck_calls.append(user)
            if "小字旁白" in user:
                return {"text": "小字旁白", "kind": "caption"}
            if "遠景看板" in user:
                return {"text": "遠景看板", "kind": "scene"}
            return {"text": "神社絵馬", "kind": "scene"}

    monkeypatch.setattr(client, "LLM", lambda *a, **k: Fake())

    # 默认关闭：3 处小字均触发特写
    cfg_off = Config()
    assert cfg_off.ocr.refine_skip_small_scene is False
    out_off = refine.refine_ocr(copy.deepcopy(base_events), cfg_off, str(tmp_path), None, video=str(video), size=(1920, 1080))
    assert len(recheck_calls) == 3

    # 开启后：小字 scene（o1 与漏检的「遠景看板」）跳过特写但仍保留条目，只有 caption 触发特写
    recheck_calls.clear()
    cfg_on = Config()
    cfg_on.ocr.refine_skip_small_scene = True
    out_on = refine.refine_ocr(copy.deepcopy(base_events), cfg_on, str(tmp_path), None, video=str(video), size=(1920, 1080))
    assert len(recheck_calls) == 1 and "小字旁白" in recheck_calls[0]
    assert sorted((e["text"], e.get("kind")) for e in out_on) == [
        ("小字旁白", "caption"), ("神社絵馬", "scene"), ("遠景看板", "scene"),
    ]
    assert len(out_on) == len(out_off)


def test_vlm_bbox_drift_and_bloat_do_not_override_accurate_local_box(tmp_path, monkeypatch):
    """本地已认全文字时，以 DBNet 本地框为几何真相：
    - VLM 框向右虚大（o18 模型）不撑大框，也不误触发 track_region 拉长结束时间；
    - VLM 框整帧上偏（o378 模型）不把框往上拉偏；
    - 花字标题本地只认出一小截、VLM 补全整句时，仍采用 VLM 框并触发 track_region。"""
    import cv2
    import numpy as np

    from nyasub.core.config import Config
    from nyasub.llm import client, refine
    from nyasub.sources import media, ocr

    frame = tmp_path / "frame.jpg"
    cv2.imwrite(str(frame), np.full((720, 1280, 3), 90, np.uint8))
    monkeypatch.setattr(media, "grab_frames", lambda video, times, out, **kw: {round(t, 3): frame for t in times})
    retimed = []
    monkeypatch.setattr(ocr, "track_region", lambda video, t, rect, aspect=16 / 9: (retimed.append(round(t, 1)) or (t - 1.5, t + 1.5)))
    (tmp_path / "crops").mkdir()
    video = tmp_path / "v.ts"
    video.write_bytes(b"x")

    events = [
        {"id": "o18", "start": 84.038, "end": 88.338, "snap_t": 84.188,
         "rect": [0.3148, 0.8986, 0.6828, 0.9889], "text": "清めたまえ祓いたまえ",
         "vertical": False, "lines": 1, "score": 0.988,
         "pieces": [[0.3148, 0.8986, 0.6828, 0.9889, "清めたまえ祓いたまえ"]]},
        {"id": "o378", "start": 2168.038, "end": 2171.238, "snap_t": 2168.188,
         "rect": [0.2477, 0.9014, 0.7539, 0.9833], "text": "平穏な日々台風などの来ないこと",
         "vertical": False, "lines": 1, "score": 1.0,
         "pieces": [[0.2477, 0.9014, 0.7539, 0.9833, "平穏な日々台風などの来ないこと"]]},
        {"id": "o99", "start": 500.0, "end": 502.0, "snap_t": 501.0,
         "rect": [0.55, 0.75, 0.65, 0.88], "text": "たり",
         "vertical": False, "lines": 1, "score": 0.85,
         "pieces": [[0.55, 0.75, 0.65, 0.88, "たり"]]},
    ]

    class Fake:
        def chat_json(self, system, user, images=None):
            if system == refine.READ_SYSTEM:
                s = str(images)
                if "000084188" in s:
                    return {"items": [{"text": "清めたまえ、祓いたまえ", "kind": "caption", "bbox": [321, 879, 827, 987]}]}
                if "002168188" in s:
                    return {"items": [{"text": "平穏な日々 台風などの来ないこと", "kind": "caption", "bbox": [251, 852, 739, 928]}]}
                return {"items": [{"text": "森のなかのふたり", "kind": "caption", "bbox": [200, 700, 700, 900]}]}
            return {"text": "森のなかのふたり", "kind": "caption"}

    monkeypatch.setattr(client, "LLM", lambda *a, **k: Fake())
    out = {e["id"]: e for e in refine.refine_ocr(events, Config(), str(tmp_path), None, video=str(video), size=(1280, 720))}
    assert out["o18"]["rect"] == [0.3148, 0.8986, 0.6828, 0.9889]
    assert out["o18"]["end"] == 88.338
    assert out["o378"]["rect"] == [0.2477, 0.9014, 0.7539, 0.9833]
    assert out["o378"]["end"] == 2171.238
    assert out["o99"]["rect"] == [0.2, 0.7, 0.7, 0.9]
    assert retimed == [501.0]
    # 单字漏检补成两字大标题（「り」→「祟り」）：同样判定为 VLM 补字，采用 VLM 框
    assert refine._vlm_expanded("祟り", "り")
    assert not refine._vlm_expanded("鮭とば", "鮭とは")


def test_prefiltered_ruby_does_not_steal_vlm_box_and_merge_maintains_rect():
    """多行标题（o29 + o31）伴随完全落在 VLM 大框内的振假名（o30, prefiltered）：
    1. match_moment 中 o30 不以 overlap=1.0 截胡 VLM 框；
    2. _merge_into 合并后同步更新 rect、crop_rect、zone，维持数据结构不变量。"""
    from nyasub.llm.refine import _merge_into, match_moment

    o29 = {"id": "o29", "start": 122.338, "end": 129.438, "rect": [0.1812, 0.6528, 0.4742, 0.8333],
           "text": "入らずの森", "vertical": False, "lines": 1, "pieces": [[0.1812, 0.6528, 0.4742, 0.8333, "入らずの森"]]}
    o30 = {"id": "o30", "start": 122.338, "end": 129.438, "rect": [0.5172, 0.7583, 0.5617, 0.8097],
           "text": "モリ", "vertical": False, "lines": 1, "prefiltered": True}
    o31 = {"id": "o31", "start": 122.338, "end": 129.438, "rect": [0.3273, 0.7917, 0.5875, 0.9528],
           "text": "畏れの杜", "vertical": False, "lines": 1, "pieces": [[0.3273, 0.7917, 0.5875, 0.9528, "畏れの杜"]]}
    items = [{"text": "入らずの\n眠れの杜", "kind": "caption", "box": [0.205, 0.697, 0.579, 0.942]}]
    visible = [(e, e["rect"]) for e in (o29, o30, o31)]
    groups, misses = match_moment(items, visible, {"o29", "o31"})
    assert misses == []
    assert len(groups) == 1
    evs, its = groups[0]
    assert [e["id"] for e in evs] == ["o29", "o31"]
    assert len(its) == 1 and its[0]["text"] == "入らずの\n眠れの杜"

    _merge_into(o29, [o31])
    assert o29["rect"] == [0.1812, 0.6528, 0.5875, 0.9528]
    assert o29["crop_rect"] == [0.1812, 0.6528, 0.5875, 0.9528]
    assert len(o29["pieces"]) == 2



