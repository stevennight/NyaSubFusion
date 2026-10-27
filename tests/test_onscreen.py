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


def test_refine_context_frames_shared_by_same_moment():
    from nyasub.llm.refine import plan_groups

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

    from nyasub.llm.refine import mark_frame

    f = tmp_path / "f.jpg"
    cv2.imwrite(str(f), np.full((720, 1280, 3), 128, np.uint8))
    e = {"id": "o19", "start": 64.2, "end": 65.6, "snap_t": 64.5, "rect": [0.98, 0.4, 0.99, 0.44]}
    out = mark_frame(str(f), [e], [1], 64.5, str(tmp_path / "m"))
    img = cv2.imread(out)
    assert img.shape[:2] == (360, 640)
    assert (np.abs(img.astype(int) - 128).sum(axis=2) > 60).sum() > 50  # 画上了框和编号


def test_refine_context_asks_with_frames(tmp_path):
    from nyasub.llm import refine

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


def test_refine_recrop_boxes_text_at_snapshot(tmp_path):
    import cv2
    import numpy as np

    from nyasub.llm import refine

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
    from nyasub.llm import refine

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
