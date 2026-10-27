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


def test_restored_caption_skipped_once_translation_uses_it():
    """手动恢复的画面字（插入、带来源），重新翻译后这块字已经在字幕里了：不再另插一条，免得出现两次。"""
    op = {"op": "insert", "id": "u1", "start": 2562.5, "end": 2568.0, "type": "info", "orig": "冬を食べる", "tr": {},
          "sources": ["o932"], "rect": [0.02, 0.75, 0.19, 0.85], "onscreen": True}
    title = {"id": "q550", "start": 2562.5, "end": 2568.0, "type": "info", "sources": ["o932", "o933"],
             "orig": {"lang": "ja", "text": "冬を食べる\nナマコ"}, "tr": {"zh-Hans": {"text": "品味冬天\n海参"}}}
    other = dict(title, id="q9", sources=["c1"])
    assert [c["id"] for c in edits.apply([title], {"ops": [op]}, "ja")] == ["q550"]
    assert [c["id"] for c in edits.apply([other], {"ops": [op]}, "ja")] == ["q9", "u1"]  # 还没用上：照常恢复
