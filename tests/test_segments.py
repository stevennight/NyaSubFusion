"""“｜”分段：什么时候算数、每段的时间、拆 / 合；画面字复核截成半句的判断。"""
from nyasub.core import config, segments
from nyasub.core.edits import apply, piece_join_fields, piece_split_ops
from nyasub.llm.refine import truncated
from nyasub.render import layout


def _cue(orig, tr, start=0.0, end=6.0, sources=(), **kw):
    return {"id": "q1", "start": start, "end": end, "type": "dialogue", "sources": list(sources), "onscreen": False,
            "orig": {"lang": "ja", "text": orig}, "tr": {"zh-Hans": {"text": tr}}, "flags": [], **kw}


def test_bars_only_count_when_orig_and_translation_match():
    assert segments.bars_ok(_cue("あ｜い", "甲｜乙"))
    assert not segments.bars_ok(_cue("でも一美さんは　西和賀で生きることにこだわり続けました。", "但一美始终坚持｜要在西和贺生活"))
    assert not segments.bars_ok(_cue("あ", "甲"))
    assert segments.unbar("但一美始终坚持｜要在西和贺生活", "zh-Hans") == "但一美始终坚持 要在西和贺生活"
    assert segments.unbar("その夢と｜共に", "ja") == "その夢と　共に"  # 日文：全角空格（停顿）


def test_part_times_snap_to_source_boundaries():
    # 四条画面字被大模型分成两段：第二段从第三条画面字开始
    items = {"o83": {"id": "o83", "src": "telop", "start": 448.057, "end": 450.057, "text": "エサで釣るって言い方は悪いですけど"},
             "o84": {"id": "o84", "src": "telop", "start": 450.057, "end": 453.057, "text": "なんとか飛来を増やすように"},
             "o85": {"id": "o85", "src": "telop", "start": 453.057, "end": 456.057, "text": "ご飯食べるところを作って\nカモを守りましょうということで"},
             "o86": {"id": "o86", "src": "telop", "start": 456.057, "end": 458.057, "text": "いろいろやってます"}}
    cue = _cue("エサで釣るって言い方は悪いですけど\nなんとか飛来を増やすように｜ご飯食べるところを作って　カモを守りましょうということで\nいろいろやってます",
               "甲｜乙", 448.057, 458.057, items)
    assert segments.part_times(cue, items) == [[448.057, 453.057], [453.057, 458.057]]


def test_part_times_ignore_over_speech_caption_and_fall_back():
    items = {"c1": {"id": "c1", "src": "subs", "start": 0.0, "end": 3.0, "text": "今日はいい天気ですね"},
             "c2": {"id": "c2", "src": "subs", "start": 3.0, "end": 6.0, "text": "散歩に行きましょう"},
             "o1": {"id": "o1", "src": "telop", "start": 1.0, "end": 6.0, "text": "まったく別の文字", "over_speech": True}}
    cue = _cue("今日はいい天気ですね｜散歩に行きましょう", "甲｜乙", 0.0, 6.0, items)
    assert segments.part_times(cue, items) == [[0.0, 3.0], [3.0, 6.0]]
    # 来源对不上：算不出来，按字数比例
    assert segments.part_times(_cue("ぜんぜん｜ちがう", "甲｜乙", 0.0, 6.0, items), items) is None
    assert segments.proportional(_cue("あ｜い", "甲甲｜乙", 0.0, 6.0)) == [[0.0, 4.0], [4.0, 6.0]]


def test_card_bars_find_splits_inside_one_caption():
    # 两条字幕用 ➡ 接成一句：第 1 个分界在两条之间（对的），第 2 个落在第二条字幕的换行上（大模型当成了分屏）
    items = {"c50": {"id": "c50", "src": "subs", "start": 380.767, "end": 385.6,
                     "text": "こうしてとられた鴨は\n江戸時代　幕府に献上され➡"},
             "c51": {"id": "c51", "src": "subs", "start": 385.6, "end": 390.767,
                     "text": "藩は\n餌場である周辺の田んぼを守りました。"}}
    cue = _cue("こうしてとられた鴨は　江戸時代　幕府に献上され｜藩は｜餌場である周辺の田んぼを守りました。",
               "这样捕获的鸭子 江户时代进贡给幕府｜藩主则｜保护周边作为觅食地的稻田", 380.767, 390.767, items)
    assert segments.card_bars(cue, items) == [2]
    assert segments.join_at(cue["tr"]["zh-Hans"]["text"], 2, "zh-Hans") == "这样捕获的鸭子 江户时代进贡给幕府｜藩主则 保护周边作为觅食地的稻田"
    # 语音里的分界不管（语音一段很长，中间本来就要分屏）
    asr = {"a1": {"id": "a1", "src": "asr", "start": 0.0, "end": 6.0, "text": "今日はいい天気ですね散歩に行きましょう"}}
    assert segments.card_bars(_cue("今日はいい天気ですね｜散歩に行きましょう", "甲｜乙", 0.0, 6.0, asr), asr) == []


def _layout(cue):
    cfg, _ = config.load(None, None, {"project": {"targets": ["zh-Hans"]}})
    prof = next(p for p in cfg.render.profiles if p.name == "bilingual")
    return layout.build([cue], prof, cfg, "ja")


def test_layout_uses_parts_and_ignores_mismatched_bars():
    out = _layout(_cue("今日は｜散歩", "今天｜散步", 0.0, 6.0, parts=[[0.0, 1.5], [1.5, 6.0]]))
    assert [(x["start"], x["end"]) for x in out] == [(0.0, 1.5), (1.5, 6.0)]
    # 只有译文里有“｜”：排版按译文的“｜”换屏，原文按比例跟着切（一屏放得下的，整理时已经去掉了“｜”）
    out = _layout(_cue("その夢と共に歩んだのが", "陪他一同追逐梦想的｜是"))
    assert len(out) == 2 and all(len(x["layers"]) == 2 for x in out)


def test_piece_split_and_join():
    cue = _cue("あ｜い｜う", "A｜B｜C", 0.0, 9.0, parts=[[0, 3], [3, 5], [5, 9]])
    ops, first = piece_split_ops(cue, [1, 2])
    got = apply([cue], {"ops": ops})
    assert first == "q1aa"
    assert [(c["start"], c["end"], c["orig"]["text"], c["tr"]["zh-Hans"]["text"]) for c in got] == [
        (0, 3, "あ", "A"), (3, 5, "い", "B"), (5, 9, "う", "C")]
    ops, _ = piece_split_ops(cue, [1])
    assert [c["orig"]["text"] for c in apply([cue], {"ops": ops})] == ["あ", "い｜う"]
    assert piece_join_fields(cue, 2) == {"orig": "あ｜い　う", "tr": {"zh-Hans": "A｜B C"}, "parts": [[0, 3], [3, 9]]}


def test_refine_truncated_reading():
    assert truncated("マタギが", "マタギが奥山に行った時に")
    assert truncated("初めて食べたときの食感が\n雪の上を歩いている", "初めて食べたときの食感が\n雪の上を歩いているようなキュッキュッ")
    assert not truncated("風がこっちから来とる", "TaJ\n風がこっちから来とる")  # 只是去掉了噪点
    assert not truncated("ご飯", "ごはん")
    assert not truncated("", "マタギが奥山に行った時に")


def test_piece_cut_and_manual_times():
    from nyasub.core.edits import piece_cut_fields

    cue = _cue("今日はいい天気ですね散歩に行きましょう", "今天天气真好 去散步吧", 0.0, 9.0)
    f = piece_cut_fields(cue, 4.5)
    assert f == {"orig": "今日はいい天気ですね｜散歩に行きましょう", "tr": {"zh-Hans": "今天天气真好｜去散步吧"},
                 "parts": [[0.0, 4.5], [4.5, 9.0]]}
    assert piece_cut_fields(cue, 0.1) is None  # 太靠边
    # 合并时时间也跟着合
    two = _cue(f["orig"], f["tr"]["zh-Hans"], 0.0, 9.0, parts=f["parts"])
    assert piece_join_fields(two, 1)["parts"] == [[0.0, 9.0]]
    # 手动调的每段时间：首尾对得上、一段接一段才用
    assert segments.valid_parts([[0.0, 3.0], [3.0, 9.0]], two)
    assert not segments.valid_parts([[0.0, 3.0], [3.5, 9.0]], two)
    assert not segments.valid_parts([[0.0, 3.0], [3.0, 8.0]], two)
    assert not segments.valid_parts([[0.0, 9.0]], two)


def test_original_hidden_only_for_onscreen_segment():
    # 第 1 段是画面上已有的字（不再显示原文），第 2 段来自字幕（原文照常显示）
    cue = _cue("大丈夫だ｜大豆を入れ", "没事｜放进大豆", 0.0, 6.0, parts=[[0.0, 1.5], [1.5, 6.0]], parts_onscreen=[True, False])
    out = _layout(cue)
    roles = [[ly.role for ly, _, _ in x["layers"]] for x in out]
    assert roles == [["translation"], ["translation", "original"]]


def test_show_orig_override():
    on = _cue("大丈夫だ", "没事", onscreen=True)
    assert [ly.role for ly, _, _ in _layout(on)[0]["layers"]] == ["translation"]  # 自动：画面上已有，不显示
    assert [ly.role for ly, _, _ in _layout(dict(on, show_orig="on"))[0]["layers"]] == ["translation", "original"]
    off = _cue("大丈夫だ", "没事")
    assert [ly.role for ly, _, _ in _layout(dict(off, show_orig="off"))[0]["layers"]] == ["translation"]


def test_duplicate_info_cues_merged():
    from nyasub.llm.translate import merge_dup_info

    a = _cue("ラポロアイヌネイション 会長｜差間 啓全さん", "会长｜差间", 1953.474, 1960.807, type="info", sources=["o658"])
    b = _cue("ラポロアイヌネイション 会長\n差間 啓全さん", "会长 差间", 1953.474, 1960.807, type="info", sources=["o659"])
    b["id"] = "q2"
    out = merge_dup_info([a, b])
    assert len(out) == 1 and out[0]["sources"] == ["o658", "o659"]
    assert len(merge_dup_info([a, b], keep={"q2"})) == 2  # 手动改过的不合并


def test_per_segment_type_and_show():
    from nyasub.core.edits import apply, piece_isolate_ops

    cue = _cue("今日は｜散歩｜歌", "今天｜散步｜歌", 0.0, 9.0, parts=[[0, 3], [3, 6], [6, 9]], onscreen=True,
               parts_onscreen=[True, True, True], parts_show=[None, "on", None], parts_type=[None, None, "song"])
    out = _layout(cue)
    assert [[ly.role for ly, _, _ in x["layers"]] for x in out] == [["translation"], ["translation", "original"], ["translation"]]
    assert [x["type"] for x in out] == ["dialogue", "dialogue", "song"]
    # 中间那段设成说明：单独拆成一条，前后两段的设置跟着各自的段走
    ops, nid = piece_isolate_ops(cue, 1)
    got = {c["id"]: c for c in apply([cue], {"ops": ops})}
    assert nid == "q1ab" and got[nid]["orig"]["text"] == "散歩" and got[nid]["show_orig"] == "on"
    assert got["q1b"]["type"] == "song" and got["q1aa"]["orig"]["text"] == "今日は"


def test_undo_redo_describes_what_changed():
    from nyasub.core.edits import describe_change

    base = {"cues": {"q3": {"orig": "あ"}}, "ops": []}
    timed = {"cues": {"q3": {"orig": "あ"}, "q7": {"start": 12.0, "end": 14.0}}, "ops": []}
    # 撤销改时间：跳到 q7，说明是“时间”
    assert describe_change(timed, base, undone=True) == {"id": "q7", "at": 12.0, "what": "时间"}
    # 撤销拆分：回到原来那一条；重做拆分：跳到拆出来的前一半
    split = {"cues": {}, "ops": [{"op": "split", "id": "q5", "at": 30.0}]}
    empty = {"cues": {}, "ops": []}
    assert describe_change(split, empty, undone=True)["id"] == "q5"
    assert describe_change(empty, split, undone=False) == {"id": "q5a", "at": 30.0, "what": "拆分"}
    # 重做删除：那一条没了，只给时间
    deleted = {"cues": {"q9": {"delete": True, "start": 50.0}}, "ops": []}
    assert describe_change({"cues": {"q9": {"start": 50.0}}, "ops": []}, deleted, undone=False)["id"] is None


def test_manual_position_everywhere():
    from nyasub.core import config as cfgmod
    from nyasub.render import ass

    cfg, _ = cfgmod.load(None, None, {"project": {"targets": ["zh-Hans"]}})
    prof = next(p for p in cfg.render.profiles if p.name == "bilingual")
    a = _cue("今日は", "今天", 0.0, 6.0)
    b = dict(_cue("散歩", "散步", 2.0, 8.0), id="q2", pos=[0.5, 0.2])  # 手动放到画面上方
    items = layout.build([a, b], prof, cfg, "ja")
    mains = sorted((x for x in items if x["kind"] == "main"), key=lambda x: x["start"])
    # 手动放过的不参与“后一句开始时前一句结束”：两句可以同时显示
    assert [(x["cue"], x["start"], x["end"]) for x in mains] == [("q1", 0.0, 6.0), ("q2", 2.0, 8.0)]
    px, py, _, placed = ass.place(items, prof, cfg.render.styles, 16 / 9, [])
    p2 = next(p for p in placed if p["item"]["cue"] == "q2")
    assert (p2["x"], p2["y"]) == (960, 216)
    # 说明类也能手动放
    info = dict(_cue("ワニ", "鲨鱼", 0.0, 3.0, type="info", rect=[0.0, 0.8, 0.3, 1.0]), id="q3", pos=[0.25, 0.5])
    _, _, _, placed = ass.place(layout.build([info], prof, cfg, "ja"), prof, cfg.render.styles, 16 / 9, [])
    assert (placed[0]["an"], placed[0]["x"], placed[0]["y"]) == (2, 480, 540)


def test_swap_segments():
    from nyasub.core.edits import piece_swap_fields

    cue = _cue("いやもう｜なんでも｜大変｜やっていかないと｜広報活動ですよ", "唉｜什么事｜很难｜必须做下去｜就是宣传", 0.0, 10.0,
               parts=[[0, 2], [2, 4], [4, 6], [6, 7], [7, 10]], parts_show=[None, None, None, "off", None])
    f = piece_swap_fields(cue, 3, 4)
    assert f["orig"] == "いやもう｜なんでも｜大変｜広報活動ですよ｜やっていかないと"
    assert f["tr"] == {"zh-Hans": "唉｜什么事｜很难｜就是宣传｜必须做下去"}
    assert f["parts_show"] == [None, None, None, None, "off"]   # 各段的设置跟着内容走
    assert "parts" not in f                                      # 每屏的时间位置不动
    assert piece_swap_fields(cue, 4, 5) is None


def test_fit_estimate_matches_layout_wrapping():
    """估算一屏放不放得下，和排版用同一套折行：在好的断点断开后数行数，不只数字数。"""
    cfg, _ = config.load(None, None, {"project": {"targets": ["zh-Hans"]}})
    assert segments.screen_lines("而且机会只有 鸭子飞向觅食地的黄昏时分那15分钟", "zh-Hans", cfg) == 2
    assert segments.screen_lines("这些家族会诞生被兽神选中的孩子 听说教会收养他们是为了得到那份力量", "zh-Hans", cfg) == 3
    assert segments.screen_lines("这是农民们培育出来的蔬菜", "zh-Hans", cfg) == 1


def test_wrap_never_splits_a_word():
    """折行整行一起选断点：不把一个词拆开、行首不是助词；为了不拆开小句可以多一行；不留一两个字的孤行。"""
    from nyasub import lang

    zh, ja = lang.get("zh-Hans"), lang.get("ja")
    assert zh.wrap("而且机会只有　鸭子飞向觅食地的黄昏时分那15分钟", 16) == ["而且机会只有　鸭子飞向", "觅食地的黄昏时分那15分钟"]
    assert zh.wrap("听说教会收养他们是为了得到那份力量", 16) == ["听说教会收养他们", "是为了得到那份力量"]
    assert zh.wrap("外表姑且不论 对喜欢的人来说是难以抗拒的美味", 16) == ["外表姑且不论", "对喜欢的人来说是难以抗拒的美味"]
    assert zh.wrap("嗯 不过能知道村雨在哪儿就算走运了", 16) == ["嗯 不过能知道村雨", "在哪儿就算走运了"]   # 不让“嗯”独占一行
    assert zh.wrap("可是 那个从刚才开始就一直盯着我的家伙", 16) == ["可是 那个从刚才开始", "就一直盯着我的家伙"]
    # 多一行换来每个小句完整
    assert zh.wrap("这么说 想召唤我们的 是信乃体内的村雨 或庄介体内的与四郎？", 16) ==         ["这么说 想召唤我们的", "是信乃体内的村雨", "或庄介体内的与四郎？"]
    # 术语表里的名字不拆开
    zh.learn_words(["加贺市"])
    assert zh.wrap("为了品尝只在加贺市内流通的鸭肉……", 16)[0].endswith("加贺市")
    # 日文：在文节的边界断，不断在词中间（刚好两整行长、卡在词中间时宁可多一行）
    lines = ja.wrap("もともと虫を食べる文化は長野県各地にありますが中でも伊那には独特の歴史があります", 20)
    assert lines == ["もともと虫を食べる文化は", "長野県各地にありますが中でも", "伊那には独特の歴史があります"]


def test_auto_split_screens_at_break_points():
    """输出时一屏放不下，先在空格 / 标点 / 原来的换行处分屏，不把一个词拆在两屏。"""
    from nyasub import lang

    zh = lang.get("zh-Hans")
    screens = layout._screens(zh, "这些家族会诞生被兽神选中的孩子　听说教会收养他们是为了得到那份力量", 16, 2)
    assert screens == [["这些家族会诞生被兽神选中的孩子"], ["听说教会收养他们", "是为了得到那份力量"]]
    # 原来的两行（标题 / 罗马字）各自成屏，罗马字那行折成两行也不拆开
    assert layout._screens(zh, "新日本风土记\nSHIN NIHON FUDOKI", 16, 2) == [["新日本风土记"], ["SHIN NIHON", "FUDOKI"]]
    assert layout._screens(zh, "放得下的一句", 16, 2) == [["放得下的一句"]]
