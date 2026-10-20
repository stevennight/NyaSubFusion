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
    assert segments.unbar("その夢と｜共に", "ja") == "その夢と共に"


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


def _layout(cue):
    cfg, _ = config.load(None, None, {"project": {"targets": ["zh-Hans"]}})
    prof = next(p for p in cfg.render.profiles if p.name == "bilingual")
    return layout.build([cue], prof, cfg, "ja")


def test_layout_uses_parts_and_ignores_mismatched_bars():
    out = _layout(_cue("今日は｜散歩", "今天｜散步", 0.0, 6.0, parts=[[0.0, 1.5], [1.5, 6.0]]))
    assert [(x["start"], x["end"]) for x in out] == [(0.0, 1.5), (1.5, 6.0)]
    # 只有译文里有“｜”：不分屏
    out = _layout(_cue("その夢と共に歩んだのが", "陪他一同追逐梦想的｜是"))
    assert len(out) == 1


def test_piece_split_and_join():
    cue = _cue("あ｜い｜う", "A｜B｜C", 0.0, 9.0, parts=[[0, 3], [3, 5], [5, 9]])
    ops, first = piece_split_ops(cue, [1, 2])
    got = apply([cue], {"ops": ops})
    assert first == "q1aa"
    assert [(c["start"], c["end"], c["orig"]["text"], c["tr"]["zh-Hans"]["text"]) for c in got] == [
        (0, 3, "あ", "A"), (3, 5, "い", "B"), (5, 9, "う", "C")]
    ops, _ = piece_split_ops(cue, [1])
    assert [c["orig"]["text"] for c in apply([cue], {"ops": ops})] == ["あ", "い｜う"]
    assert piece_join_fields(cue, 2) == {"orig": "あ｜いう", "tr": {"zh-Hans": "A｜B C"}, "parts": [[0, 3], [3, 9]]}


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
