"""精翻：整段翻完后，人物表 → 分组 → 一组一组按顺序重翻；思考程度只用在精翻那一轮。"""
import json
import re

from nyasub import cli
from nyasub.llm import client, fine

from . import fakellm

_LINE = re.compile(r"^\[(q\d+)\] \d\d:\d\d \S+?：(.*)$", re.M)


def _cues(n):
    return [{"id": f"q{i}", "start": i * 2.0, "end": i * 2 + 1.5, "type": "dialogue",
             "orig": {"lang": "ja", "text": f"原文{i}"}, "tr": {"zh-Hans": {"text": f"旧{i}"}}} for i in range(1, n + 1)]


def test_groups_fill_gaps_trim_overlap_and_split_big_ones():
    cues = _cues(30)
    res = {"groups": [{"from": "q1", "to": "q3", "topic": "开头"},
                      {"from": "q6", "to": "q8"},                   # q4–q5 漏了：补成一组
                      {"from": "q7", "to": "q9"},                   # 和上一组重叠：从 q9 开始
                      {"from": "q10", "to": "q27", "topic": "长"},  # 18 条：平均拆成两份 9 条
                      {"from": "q99", "to": "q100"}]}               # 对不上的忽略；q28–q30 补成一组
    got = [(cues[a]["id"], cues[b]["id"], t) for a, b, t in fine.make_groups(res, cues)]
    assert got == [("q1", "q3", "开头"), ("q4", "q5", ""), ("q6", "q8", ""), ("q9", "q9", ""),
                   ("q10", "q18", "长"), ("q19", "q27", "长"), ("q28", "q30", "")]


def test_cast_terms_join_all_names_and_skip_one_letter_names():
    cast = [{"names": [{"src": "ミルク", "tr": "牛奶"}, {"src": "ミーちゃん", "tr": "牛奶"}, {"src": "ミ", "tr": "牛"}],
             "kind": "动物", "who": "敏子的猫"}]
    terms = fine.cast_terms(cast)
    assert [(t["src"], t["tr"]) for t in terms] == [("ミルク", "牛奶"), ("ミーちゃん", "牛奶")]
    assert "也叫 ミーちゃん" in terms[0]["note"] and terms[0]["type"] == "人名"


def test_prompt_has_context_memory_and_only_relevant_cast():
    cues = _cues(12)
    cues[2]["orig"]["text"] = "ミルクー"
    cues[9]["orig"]["text"] = "原文1"  # 和前面的 q1 一样：给前面的译法
    cast = [{"names": [{"src": "ミルク", "tr": "牛奶"}], "kind": "动物", "who": "猫"},
            {"names": [{"src": "真兵", "tr": "真兵"}], "kind": "人", "who": "图书馆主人"}]
    p = fine.group_prompt(cues, 8, 10, "找猫", "zh-Hans", "概要", [], cast)
    assert "## 这一组（找猫）" in p and "[q9] 00:18 对话：原文9" in p and "[q12]" not in p
    assert "原文8 → 旧8" in p              # 前文带已经翻好的译文
    assert "00:24 原文12" in p             # 后文只有原文
    assert "前面 00:02：原文1 → 旧1" in p   # 译文记忆
    assert "ミルク→牛奶" in p and "真兵" not in p


def _responder(seen):
    def call(self, system, user, images=None):
        seen.append((system, self.effort, bool(images)))
        if "整理出登场的人和动物" in system:
            return {"characters": [{"names": [{"src": "深浦町", "tr": "深浦镇"}, {"src": "ミルク", "tr": "牛奶"}],
                                    "kind": "人", "who": "测试"}]}
        if "分组，翻译时一组一起翻" in system:
            ids = [m[0] for m in _LINE.findall(user)]
            return {"groups": [{"from": ids[0], "to": ids[-1], "topic": "全部"}]}
        if "每次给你一组连续的字幕" in system:
            items = _LINE.findall(user)
            # 第一条故意多分一屏：段数和原文对不上，保留原来的译文
            return {"items": [{"id": i, "tr": ("精:" + t + ("｜多" if k == 0 else "")).replace(" / ", "\n")}
                              for k, (i, t) in enumerate(items)]}
        return fakellm.respond(system, user)
    return call


def test_pipeline_fine_retranslates_with_effort_only_there(sample, api_config, monkeypatch):
    fakellm.install(monkeypatch)
    seen = []
    monkeypatch.setattr(client.LLM, "_call", _responder(seen))
    cfg = api_config.with_name("fine.toml")
    cfg.write_text(api_config.read_text(encoding="utf-8") + 'grouped = true\nreasoning_effort = "high"\n', encoding="utf-8")
    assert cli.main(["run", str(sample), "--no-asr", "--no-ocr", "--config", str(cfg)]) == 0

    stages = sample.with_name("sample.nyasub") / "stages"
    data = json.loads((stages / "translate.json").read_text(encoding="utf-8"))
    texts = [c["tr"]["zh-Hans"]["text"] for c in data["cues"]]
    assert texts[0].startswith("译:") and all(t.startswith("精:") for t in texts[1:])
    # 整理的结果另存着：同样的字幕、整理时的第一版译文
    organized = json.loads((stages / "organize.json").read_text(encoding="utf-8"))
    assert [c["id"] for c in organized["cues"]] == [c["id"] for c in data["cues"]]
    assert all(c["tr"]["zh-Hans"]["text"].startswith("译:") for c in organized["cues"])
    # 人物表的译名排在这一集自动收的术语前面
    assert next(t for t in data["glossary"]["terms"] if t["src"] == "深浦町")["tr"] == "深浦镇"
    assert data["glossary"]["cast"][0]["who"] == "测试"
    # 思考程度只用在精翻那几次请求；整段翻译、人物表、分组都不设
    assert {e for s, e, _ in seen if "每次给你一组连续的字幕" in s} == {"high"}
    assert {e for s, e, _ in seen if "每次给你一组连续的字幕" not in s} == {""}
    assert any(img for s, _, img in seen if "每次给你一组连续的字幕" in s)

    # 关掉分组翻：整理要重做（思考程度改用在整理的分段请求上），翻译跟着重做
    seen.clear()
    cfg.write_text(api_config.read_text(encoding="utf-8") + 'reasoning_effort = "high"\n', encoding="utf-8")
    assert cli.main(["run", str(sample), "--no-asr", "--no-ocr", "--config", str(cfg)]) == 0
    assert not any("每次给你一组连续的字幕" in s for s, _, _ in seen)
    assert any(e == "high" and "术语整理" not in s for s, e, _ in seen)


def test_effort_dropped_when_api_rejects_it(monkeypatch):
    from nyasub.core import config as config_mod

    monkeypatch.setenv("NYASUB_TEST_KEY", "x")
    cfg = config_mod.Config.model_validate({"llm": {"model": "m", "api_key_env": "NYASUB_TEST_KEY", "max_retries": 2}})
    llm = client.LLM(cfg, effort="xhigh")
    sent = []

    class Err(Exception):
        status_code = 400

    class Resp:
        choices = [type("C", (), {"message": type("M", (), {"content": '{"ok": 1}'})()})()]

    def create(**kw):
        sent.append(kw.get("reasoning_effort"))
        if kw.get("reasoning_effort"):
            raise Err("Unsupported value: 'reasoning_effort' does not support 'xhigh'")
        return Resp()

    monkeypatch.setattr(llm.client.chat.completions, "create", create)
    assert llm._call("s", "u", None) == {"ok": 1}
    assert sent == ["xhigh", None] and llm.effort == ""


def test_turning_fine_on_keeps_organize_and_redoes_translate(sample, api_config, monkeypatch):
    """精翻（分组翻 + 思考 high）开 / 关只影响翻译：整理不用重做（以前翻过的片子开精翻，只花分组翻那部分）。"""
    fakellm.install(monkeypatch)
    seen = []
    monkeypatch.setattr(client.LLM, "_call", _responder(seen))
    stages = sample.with_name("sample.nyasub") / "stages"
    assert cli.main(["run", str(sample), "--no-asr", "--no-ocr", "--config", str(api_config)]) == 0
    org = (stages / "organize.fp").read_text(encoding="utf-8")
    tr = (stages / "translate.fp").read_text(encoding="utf-8")
    fine_cfg = api_config.with_name("fine.toml")
    fine_cfg.write_text(api_config.read_text(encoding="utf-8") + 'grouped = true\nreasoning_effort = "high"\n', encoding="utf-8")
    seen.clear()
    assert cli.main(["run", str(sample), "--no-asr", "--no-ocr", "--config", str(fine_cfg)]) == 0
    assert (stages / "organize.fp").read_text(encoding="utf-8") == org
    assert (stages / "translate.fp").read_text(encoding="utf-8") != tr
    assert seen and all("术语整理" not in s and "融合" not in s[:40] for s, _, _ in seen)
    # 关掉：整理照旧，翻译回到原来的结果
    assert cli.main(["run", str(sample), "--no-asr", "--no-ocr", "--config", str(api_config)]) == 0
    assert (stages / "organize.fp").read_text(encoding="utf-8") == org
    assert json.loads((stages / "translate.fp").read_text(encoding="utf-8"))["out"] == json.loads(tr)["out"]
