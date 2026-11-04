"""翻译后统一译法：先找原文相同 / 相近的组（不用大模型），再由大模型逐组判断要不要统一。"""
from nyasub import lang
from nyasub.llm.consistency import similar_groups, unify


def _c(i, start, orig, tr):
    return {"id": f"q{i}", "start": start, "end": start + 2, "type": "info", "flags": [],
            "orig": {"lang": "ja", "text": orig}, "tr": {"zh-Hans": {"text": tr}}}


CUES = [_c(1, 120, "冬を食べる｜あの手この手の物語", "冬日滋味｜巧思百态"),
        _c(2, 680, "冬を食べるワニ", "品味冬天 鲨鱼"),
        _c(3, 1019, "冬を食べる\n雪納豆", "品味冬日\n雪纳豆"),
        _c(4, 1972, "冬を食べる", "品尝冬日"),
        _c(5, 158, "（鳴き声）", "（鸭鸣）"),
        _c(6, 353, "（鳴き声）", "（鸟鸣声）"),
        _c(7, 650, "（笑い声）", "（笑声）"),
        _c(8, 1451, "おぉ〜！　何十年ぶりだ\n（笑い声）", "哦！几十年没吃到了\n（笑声）"),
        _c(9, 1100, "でも一美さんは　西和賀で生きることにこだわり続けました。", "但一美始终坚持要在西和贺生活"),
        _c(10, 1040, "一美さん", "一美"),
        _c(11, 3000, "うん。", "嗯"), _c(12, 3100, "うん。", "嗯……")]


def test_groups_found_without_chaining():
    groups = [{CUES[i]["id"] for i in g} for g in similar_groups(CUES, "zh-Hans")]
    assert {"q1", "q2", "q3", "q4"} in groups              # 标题卡（包含关系、多行的一行也算）
    assert {"q5", "q6"} in groups                          # 同样的音效：交给大模型判断
    assert not any("q7" in g or "q8" in g for g in groups)  # 长的那条译文里包含同样的译法：算一致
    assert not any("q9" in g for g in groups)              # 只是提到同一个名字的长句：不算
    assert not any("q11" in g for g in groups)             # 太短的应答不找


class FakeLLM:
    def __init__(self, answer):
        self.answer, self.prompts = answer, []

    def chat_json(self, system, user):
        self.prompts.append(user)
        return self.answer


def test_unify_applies_only_groups_judged_same():
    cues = [dict(c, flags=[]) for c in CUES]
    order = [{c["id"] for c in [cues[i] for i in g]} for g in similar_groups(cues, "zh-Hans")]
    n_title, n_bird = order.index({"q1", "q2", "q3", "q4"}) + 1, order.index({"q5", "q6"}) + 1
    llm = FakeLLM({"groups": [
        {"g": n_title, "same": True, "reason": "同一个系列标题",
         "items": [{"id": "q1", "tr": "品尝冬日｜巧思百态"}, {"id": "q2", "tr": "品尝冬日 鲨鱼"},
                   {"id": "q3", "tr": "品尝冬日 / 雪纳豆"}, {"id": "q4", "tr": "品尝冬日｜x"}]},  # 换行写成“ / ”的换回来；分段数不对的不用
        {"g": n_bird, "same": False, "reason": "不同段落的不同鸟", "items": [{"id": "q6", "tr": "（鸭鸣）"}]}]})
    report = unify(llm, cues, lang.get("ja"), lang.get("zh-Hans"), "zh-Hans")
    by = {c["id"]: c for c in cues}
    assert by["q1"]["tr"]["zh-Hans"]["text"] == "品尝冬日｜巧思百态" and "unified" in by["q1"]["flags"]
    assert by["q2"]["tr"]["zh-Hans"]["text"] == "品尝冬日 鲨鱼"
    assert by["q3"]["tr"]["zh-Hans"]["text"] == "品尝冬日\n雪纳豆"
    assert by["q4"]["tr"]["zh-Hans"]["text"] == "品尝冬日"                # 段数不对：保持原样
    assert by["q6"]["tr"]["zh-Hans"]["text"] == "（鸟鸣声）" and not by["q6"]["flags"]  # 判断不统一：不动
    assert "前一句" in llm.prompts[0]
    assert sum(len(r["changed"]) for r in report) == 3


def test_group_order_same_in_every_process():
    """分组顺序不随进程变（集合的顺序跟着 PYTHONHASHSEED 变）：顺序一变，请求就和缓存对不上。"""
    import subprocess
    import sys

    code = ("from tests.test_consistency import _c; from nyasub.llm.consistency import similar_groups; "
            "cues = [_c(i * 2 + k, i * 100 + k * 10, w, w + str(k)) "
            "for i, w in enumerate(['（鳴き声）', '（笑い声）', '（拍手）ー', '（波の音）', '（風の音）', '（鈴の音）']) for k in (0, 1)]; "
            "print(similar_groups(cues, 'zh-Hans'))")
    outs = {subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                           env={**__import__("os").environ, "PYTHONHASHSEED": str(seed)}).stdout for seed in (1, 2, 3, 4)}
    assert len(outs) == 1
