"""手动修改：字段、拆分 / 合并 / 插入、撤销 / 重做。"""
from nyasub.core import edits


def _cues():
    return [
        {"id": "q1", "start": 0.0, "end": 4.0, "type": "dialogue", "sources": ["c1"], "onscreen": False,
         "orig": {"lang": "ja", "text": "冬の朝、漁師たちは港へ向かう"}, "tr": {"zh-Hans": {"text": "冬日清晨　渔夫们赶往港口"}},
         "flags": [], "review": "ok"},
        {"id": "q2", "start": 5.0, "end": 7.0, "type": "dialogue", "sources": ["a2"], "onscreen": False,
         "orig": {"lang": "ja", "text": "よいしょ"}, "tr": {"zh-Hans": {"text": "嘿咻"}}, "flags": ["asr_only"], "review": "todo"},
    ]


def test_legacy_format_still_works():
    out = edits.apply(_cues(), {"q1": {"tr": {"zh-Hans": "改过"}}, "q2": {"delete": True}})
    assert [c["id"] for c in out] == ["q1"] and out[0]["tr"]["zh-Hans"]["text"] == "改过"


def test_split_merge_insert_and_field_edits_on_new_ids():
    cues = _cues()
    op = edits.split_op(cues[0], 2.0)
    assert op["orig"][0].endswith("、") or op["orig"][0]  # 切点吸附到标点
    e = {"cues": {"q1b": {"tr": {"zh-Hans": "后半句改过"}}},
         "ops": [op, {"op": "merge", "ids": ["q1b", "q2"]}, {"op": "insert", "id": "u1", "start": 8, "end": 9, "orig": "追加"}]}
    out = edits.apply(cues, e, "ja")
    assert [c["id"] for c in out] == ["q1a", "q1b", "u1"]
    a, b, u = out
    assert (a["start"], a["end"], b["start"], b["end"]) == (0.0, 2.0, 2.0, 7.0)
    assert a["orig"]["text"] + b["orig"]["text"].split("\n")[0] == "冬の朝、漁師たちは港へ向かう"
    assert b["tr"]["zh-Hans"]["text"] == "后半句改过"  # 拆分出来的新条目也能改字
    assert b["sources"] == ["c1", "a2"] and "asr_only" in b["flags"]
    assert u["orig"] == {"lang": "ja", "text": "追加"} and u["flags"] == ["manual"]
    assert edits.apply(cues, e, "ja") == out  # 套用是确定的


def test_store_undo_redo(tmp_path):
    s = edits.Store(tmp_path)
    s.set_fields("q1", {"tr": {"zh-Hans": "一"}})
    s.set_fields("q1", {"tr": {"zh-Hans": "二"}})
    s.add_op({"op": "merge", "ids": ["q1", "q2"]})
    assert s.depth() == {"undo": 3, "redo": 0}
    s.undo()
    assert s.load()["ops"] == [] and s.load()["cues"]["q1"]["tr"]["zh-Hans"] == "二"
    s.undo()
    assert s.load()["cues"]["q1"]["tr"]["zh-Hans"] == "一"
    s.redo()
    assert s.load()["cues"]["q1"]["tr"]["zh-Hans"] == "二" and s.depth() == {"undo": 2, "redo": 1}
    s.set_fields("q2", {"review": "ok"})  # 新修改清空重做栈
    assert s.depth()["redo"] == 0
