"""重新翻译后，手动修改要对到新的字幕条目上，不能按编号套到别的句子上。"""
from nyasub.core import rebase
from nyasub.core.edits import apply


def _cue(id, start, end, orig, sources, typ="narration", tr=""):
    return {"id": id, "start": start, "end": end, "type": typ, "sources": sources, "onscreen": False,
            "orig": {"lang": "ja", "text": orig}, "tr": {"zh-Hans": {"text": tr}} if tr else {}, "flags": []}


OLD = [_cue("q1", 0, 3, "今日は", ["c1"]), _cue("q2", 3, 6, "それじゃあ揚げますか", ["o5"], "dialogue"),
       _cue("q3", 6, 9, "この虫がふるさとの味", ["c2"])]
# 重新翻译：前两条合成了一句，后面的编号都往前挪了
NEW = [_cue("q1", 0, 3, "今日は全国各地の味を訪ねる旅", ["c1", "c1b"]), _cue("q2", 4, 6, "それじゃあ揚げますか", ["o5"], "dialogue"),
       _cue("q3", 6, 8, "けっこう入ってはいるようですね", ["o6"], "dialogue"), _cue("q4", 8, 11, "この虫がふるさとの味", ["c2"])]


def test_edits_follow_their_cue_by_anchor():
    e = {"cues": {"q2": {"tr": {"zh-Hans": "开始收网吧"}}, "q3": {"review": "ok"}}, "ops": [{"op": "split", "id": "q3", "at": 7.5}],
         "anchors": {b: rebase.anchor(next(c for c in OLD if c["id"] == b)) for b in ("q2", "q3")}, "base": rebase.base_of(OLD)}
    new, moved, lost = rebase.rebase(e, NEW)
    assert moved == 2 and not lost
    assert new["cues"] == {"q2": {"tr": {"zh-Hans": "开始收网吧"}}, "q4": {"review": "ok"}}  # q3 → 新的 q4
    assert new["ops"] == [{"op": "split", "id": "q4", "at": 7.5}]
    out = {c["id"]: c for c in apply(NEW, new)}
    assert out["q2"]["tr"]["zh-Hans"]["text"] == "开始收网吧"
    assert out["q3"]["orig"]["text"] == "けっこう入ってはいるようですね"  # 没被别的修改盖掉


def test_legacy_edits_matched_by_content_or_left_out():
    # 旧版本的修改没有锚点：按改的内容找；删除的那条短句在新结果里已经并进别的句子，对不上 → 不套用（不能把整句删掉）
    e = {"cues": {"q1": {"orig": "今日は", "delete": True},
                  "q8": {"orig": "それじゃあ揚げますか。", "tr": {"zh-Hans": "开始收网吧"}},
                  "q9": {"type": "dialogue"}}, "ops": []}
    new, moved, lost = rebase.rebase(e, NEW)
    assert new["cues"] == {"q2": {"orig": "それじゃあ揚げますか。", "tr": {"zh-Hans": "开始收网吧"}}}
    assert sorted(o["id"] for o in lost) == ["q1", "q9"] and len(new["orphans"]) == 2
    assert {c["id"] for c in apply(NEW, new)} == {"q1", "q2", "q3", "q4"}  # 一条都没被删


def test_time_edit_matches_same_kind_only():
    cues = [_cue("q1", 678.67, 690.0, "年の瀬に地元の人が", ["c9"]), _cue("q2", 679.14, 686.47, "冬を食べるワニ", ["o217"], "info")]
    e = {"cues": {"q7": {"type": "title", "rect": [0, 0.8, 0.3, 1], "start": 678.47, "end": 686.47}}, "ops": []}
    new, _, lost = rebase.rebase(e, cues)
    assert list(new["cues"]) == ["q2"] and not lost
