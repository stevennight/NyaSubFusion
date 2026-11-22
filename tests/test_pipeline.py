"""流水线端到端：只用外挂字幕（不跑语音 / 画面字识别，几秒钟完成）+ 模拟大模型。"""
import json

import pysubs2
import pytest

from nyasub import cli

from . import fakellm


def run(*args):
    return cli.main(["run", *map(str, args)])


def fp(video, stage):
    return json.loads((video.with_name("sample.nyasub") / "stages" / f"{stage}.fp").read_text(encoding="utf-8"))


@pytest.fixture
def fake(monkeypatch):
    fakellm.install(monkeypatch)
    return fakellm


def test_full_run_two_targets(sample, api_config, fake):
    assert run(sample, "--no-asr", "--no-ocr", "--config", api_config, "--target", "zh-Hans,en") == 0
    out = sample.with_name("sample.zh-Hans.ass")
    srt = sample.with_name("sample.zh-Hans.srt")
    assert out.exists() and srt.exists()
    assert sample.with_name("sample.ja-merged.ass").exists()

    data = json.loads((sample.with_name("sample.nyasub") / "stages" / "translate.json").read_text(encoding="utf-8"))
    cues = data["cues"]
    assert [c["id"] for c in cues] == ["q1", "q2", "q3", "q4"]
    assert cues[0]["tr"]["zh-Hans"]["text"].startswith("译:青森県")
    assert cues[0]["tr"]["en"]["text"].startswith("EN:")
    assert data["glossary"]["terms"][0]["src"] == "深浦町"

    subs = pysubs2.load(str(out))
    first = subs.events[0]
    # 译文在上、原文（自动：画面上没有原文硬字幕时显示）在下
    assert first.text.startswith("译:青森県")
    assert "\\N{\\rsub}" in first.text
    assert "main" in subs.styles and "sub" in subs.styles


def test_skip_sound_annotations_judged_in_organize(sample, api_config, fake, monkeypatch):
    from nyasub.llm import client

    subtitle = sample.with_suffix(".srt")
    subtitle.write_text(
        "1\n00:00:00,200 --> 00:00:01,800\n♪（祭り囃子）\n\n"
        "2\n00:00:02,000 --> 00:00:03,800\n（田中）こんにちは（笑）\n（拍手）\n\n"
        "3\n00:00:04,000 --> 00:00:05,800\n♪ 春が来た（伴奏）\n\n"
        "4\n00:00:06,000 --> 00:00:07,800\n[波の音]\n\n"
        "5\n00:00:08,000 --> 00:00:09,800\n（競りの声）\n\n"
        "6\n00:00:10,000 --> 00:00:11,800\n(一同)フフフフフッ｡\n\n"
        "7\n00:00:12,000 --> 00:00:13,800\n（男性の声）聞こえますか？\n",
        encoding="utf-8",
    )
    assert run(sample, "--no-asr", "--no-ocr", "--config", api_config) == 0
    stages = sample.with_name("sample.nyasub") / "stages"
    before_subs = (stages / "subs.json").read_bytes()
    before_align = fp(sample, "align")
    before_fuse = fp(sample, "fuse")
    assert "祭り囃子" in (stages / "translate.json").read_text(encoding="utf-8")

    # 开启后由整理阶段的大模型结合上下文决定删留，不再在 align 硬删
    def judge_sound(self, system, user, images=None):
        res = fake.respond(system, user)
        if "不翻译" in system and "跳过声音说明" in system:
            cues, drop = [], list(res["drop"])
            for cue in res["cues"]:
                text = cue["orig"]
                if text in ("♪（祭り囃子）", "（競りの声）", "(一同)フフフフフッ｡"):
                    drop.extend(cue["src"])
                elif text == "[波の音]":
                    # 即使模型仍标成 sfx，apply_result 也会兜底丢掉
                    cues.append({**cue, "type": "sfx"})
                elif "こんにちは" in text:
                    cues.append({**cue, "orig": "（田中）こんにちは"})
                elif "春が来た" in text:
                    cues.append({**cue, "orig": "♪ 春が来た"})
                else:
                    cues.append(cue)
            return {"cues": cues, "drop": drop}
        return res

    monkeypatch.setattr(client.LLM, "_call", judge_sound)
    cfg = api_config.with_name("skip_sound.toml")
    cfg.write_text(api_config.read_text(encoding="utf-8") + "\n[subs]\nskip_sound_annotations = true\n", encoding="utf-8")
    fake.CALLS.clear()
    assert run(sample, "--no-asr", "--no-ocr", "--config", cfg) == 0

    # subs / align / fuse 全部复用，只有整理及后续步骤重跑
    assert (stages / "subs.json").read_bytes() == before_subs
    assert fp(sample, "align") == before_align
    assert fp(sample, "fuse") == before_fuse

    org_calls = [(s, u) for s, u in fake.CALLS if "不翻译" in s]
    tr_calls = [(s, u) for s, u in fake.CALLS if '"items": [{"ids"' in s]
    assert len(org_calls) == 1 and len(tr_calls) == 1
    org_sys, org_user = org_calls[0]
    assert "跳过声音说明" in org_sys and "|sfx" not in org_sys
    # 整理阶段收到了完整原文，交由模型判断
    assert all(note in org_user for note in ("祭り囃子", "（笑）", "拍手", "伴奏", "波の音", "競りの声", "フフフフフッ"))
    # 翻译阶段只收到模型保留并清理后的台词 / 歌词
    assert all(not any(note in u for note in ("祭り囃子", "（笑）", "拍手", "伴奏", "波の音", "競りの声", "フフフフフッ")) for _, u in tr_calls)

    data = json.loads((stages / "translate.json").read_text(encoding="utf-8"))
    assert [c["orig"]["text"] for c in data["cues"]] == ["（田中）こんにちは", "♪ 春が来た", "（男性の声）聞こえますか？"]
    assert [(c["start"], c["end"]) for c in data["cues"]] == [(2.0, 3.8), (4.0, 5.8), (12.0, 13.8)]


def test_skip_sound_annotations_postprocess_and_export(sample, tmp_path, fake):
    from nyasub import lang
    from nyasub.llm import prompts
    from nyasub.llm.translate import apply_result, keep_subs_lines, keep_subs_wording

    ja, zh = lang.get("ja"), lang.get("zh-Hans")
    assert prompts.organize_system(ja, "") == prompts.organize_system(ja, "", skip_sound_annotations=False)
    assert "|sfx" in prompts.organize_system(ja, "")
    assert "|sfx" not in prompts.organize_system(ja, "", skip_sound_annotations=True)
    assert "跳过声音说明" in prompts.organize_system(ja, "", skip_sound_annotations=True)
    assert prompts.chunk_system(ja, zh, "", {}) == prompts.chunk_system(ja, zh, "", {}, skip_sound_annotations=False)
    assert "跳过声音说明" in prompts.chunk_system(ja, zh, "", {}, skip_sound_annotations=True)

    # 删掉句中声音说明留下空格/标点时，keep_subs_wording 不能把它补回去；真正写错的名字照旧改回
    mid = [{"src": "subs", "start": 0.0, "end": 2.0, "text": "こんにちは（笑）また明日"}]
    assert keep_subs_wording(mid, "こんにちは また明日") == "こんにちは また明日"
    assert keep_subs_wording(mid, "こんにちは、また明日") == "こんにちは、また明日"
    typo = [{"src": "subs", "start": 0.0, "end": 2.0, "text": "亡きキミイさんが笑う"}]
    assert keep_subs_wording(typo, "亡き一美さんが笑う") == "亡きキミイさんが笑う"

    # 多行字幕删掉长括号说明或方括号/【】声音行时，keep_subs_lines 不能补回
    short_plus_note = [{"src": "subs", "start": 0.0, "end": 2.0, "text": "みんな集まって\nおはよう（盛大な拍手と歓声）"}]
    assert keep_subs_lines(short_plus_note, "みんな集まって\nおはよう") == "みんな集まって\nおはよう"
    bracket_line = [{"src": "subs", "start": 0.0, "end": 2.0, "text": "（田中）こんにちは\n【拍手喝采】"}]
    assert keep_subs_lines(bracket_line, "（田中）こんにちは") == "（田中）こんにちは"

    # apply_result：开启跳过声音说明时兜底丢掉 sfx 和空 orig；关闭时照常保留 sfx
    chunk = [
        {"id": "c1", "src": "subs", "start": 0.0, "end": 1.0, "text": "（拍手）"},
        {"id": "c2", "src": "subs", "start": 1.0, "end": 2.0, "text": "（波の音）"},
        {"id": "c3", "src": "subs", "start": 2.0, "end": 3.0, "text": "こんにちは"},
    ]
    res = {"cues": [
        {"src": ["c1"], "type": "sfx", "orig": "（拍手）"},
        {"src": ["c2"], "type": "dialogue", "orig": ""},
        {"src": ["c3"], "type": "dialogue", "orig": "こんにちは"},
    ], "drop": []}
    cues_keep, missing_keep = apply_result(chunk, res, "zh-Hans", "ja", skip_sound_annotations=False)
    assert missing_keep == [] and [c["orig"]["text"] for c in cues_keep] == ["（拍手）", "（波の音）", "こんにちは"]
    cues_skip, missing_skip = apply_result(chunk, res, "zh-Hans", "ja", skip_sound_annotations=True)
    assert missing_skip == [] and [c["orig"]["text"] for c in cues_skip] == ["こんにちは"]

    # 导出任务模式：开启开关会重新导出带规则的 TASK.md，且导回时同样兜底丢掉 sfx
    assert run(sample, "--no-asr", "--no-ocr", "--translate", "export") == 0
    jobs = sample.with_name("sample.nyasub") / "jobs"
    assert "跳过声音说明" not in (jobs / "TASK.md").read_text(encoding="utf-8")

    cfg = tmp_path / "export_skip_sound.toml"
    cfg.write_text("[subs]\nskip_sound_annotations = true\n", encoding="utf-8")
    assert run(sample, "--no-asr", "--no-ocr", "--translate", "export", "--config", cfg) == 0
    assert "跳过声音说明" in (jobs / "TASK.md").read_text(encoding="utf-8")
    prompt = (jobs / "job_001.input.txt").read_text(encoding="utf-8")
    exported = fake.respond("融合", prompt)
    exported["cues"][0]["type"] = "sfx"
    (jobs / "job_001.result.json").write_text(json.dumps(exported, ensure_ascii=False), encoding="utf-8")
    (jobs / "glossary.result.json").write_text('{"summary": "s", "terms": []}', encoding="utf-8")
    assert run(sample, "--no-asr", "--no-ocr", "--translate", "export", "--config", cfg) == 0
    tr = json.loads((sample.with_name("sample.nyasub") / "stages" / "translate.json").read_text(encoding="utf-8"))
    assert len(tr["cues"]) == len(exported["cues"]) - 1 and all(c["type"] != "sfx" for c in tr["cues"])


def test_cache_reuse_and_partial_redo(sample, api_config, fake):
    assert run(sample, "--no-asr", "--no-ocr", "--config", api_config) == 0
    calls = len(fake.CALLS)
    before = fp(sample, "translate")

    # 再跑一次：全部复用，不调大模型
    assert run(sample, "--no-asr", "--no-ocr", "--config", api_config) == 0
    assert len(fake.CALLS) == calls
    assert fp(sample, "translate") == before

    # 只改样式：只影响输出，不重新翻译
    style = api_config.with_name("style.toml")
    style.write_text(api_config.read_text(encoding="utf-8") + '[render.styles.main]\nsize = 72\n', encoding="utf-8")
    assert run(sample, "--no-asr", "--no-ocr", "--config", style) == 0
    assert len(fake.CALLS) == calls
    subs = pysubs2.load(str(sample.with_name("sample.zh-Hans.ass")))
    assert subs.styles["main"].fontsize == 72

    # 改节目说明：重新翻译（大模型请求有缓存键，内容变了就会重新请求）
    ctx = api_config.with_name("ctx.toml")
    ctx.write_text(api_config.read_text(encoding="utf-8") + '[project]\ncontext = "另一个节目"\n', encoding="utf-8")
    assert run(sample, "--no-asr", "--no-ocr", "--config", ctx) == 0
    assert len(fake.CALLS) > calls
    assert fp(sample, "translate")["in"] != before["in"]

    # 改本集简介：重新生成概要并翻译
    synopsis = api_config.with_name("synopsis.toml")
    synopsis.write_text(api_config.read_text(encoding="utf-8") + '[project]\nsynopsis = "这一集介绍青森县的食物。"\n', encoding="utf-8")
    before_synopsis = fp(sample, "translate")
    assert run(sample, "--no-asr", "--no-ocr", "--config", synopsis) == 0
    assert len(fake.CALLS) > calls and fp(sample, "translate")["in"] != before_synopsis["in"]


def test_export_mode_roundtrip(sample, tmp_path, fake):
    # 第一次：导出任务，不出最终字幕
    assert run(sample, "--no-asr", "--no-ocr", "--translate", "export") == 0
    jobs = sample.with_name("sample.nyasub") / "jobs"
    assert (jobs / "TASK.md").exists() and (jobs / "job_001.input.txt").exists()
    assert not sample.with_name("sample.zh-Hans.ass").exists()

    # 模拟外部助手写结果
    prompt = (jobs / "job_001.input.txt").read_text(encoding="utf-8")
    result = fake.respond("融合", prompt)
    (jobs / "job_001.result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    (jobs / "glossary.result.json").write_text('{"summary": "s", "terms": []}', encoding="utf-8")

    assert run(sample, "--no-asr", "--no-ocr", "--translate", "export") == 0
    out = sample.with_name("sample.zh-Hans.ass")
    assert out.exists()
    assert "译:青森県" in out.read_text(encoding="utf-8-sig")


def test_series_glossary_shared(sample, api_config, fake, isolated_home):
    assert run(sample, "--no-asr", "--no-ocr", "--config", api_config, "--series", "海辺") == 0
    gl = isolated_home / "series" / "海辺" / "glossary.json"
    terms = json.loads(gl.read_text(encoding="utf-8"))["terms"]
    assert terms[0]["src"] == "深浦町"


def test_edits_applied_on_render(sample, api_config, fake):
    assert run(sample, "--no-asr", "--no-ocr", "--config", api_config) == 0
    edits = sample.with_name("sample.nyasub") / "edits.json"
    edits.write_text(json.dumps({"q1": {"tr": {"zh-Hans": "手改的译文"}}, "q4": {"delete": True}},
                                ensure_ascii=False), encoding="utf-8")
    assert run(sample, "--no-asr", "--no-ocr", "--config", api_config) == 0
    srt = sample.with_name("sample.zh-Hans.srt").read_text(encoding="utf-8-sig")
    assert "手改的译文" in srt
    assert "いただきます" not in srt


@pytest.mark.slow
def test_real_asr_ocr(sample, api_config, fake):
    """真实的语音识别 + 画面字识别（需要模型，约 1 分钟）。"""
    pytest.importorskip("faster_whisper")
    pytest.importorskip("rapidocr")
    assert run(sample, "--config", api_config, "--until", "fuse") == 0
    fused = json.loads((sample.with_name("sample.nyasub") / "stages" / "fuse.json").read_text(encoding="utf-8"))
    s = fused["stats"]
    assert s["subs"] == 4 and s["telop"] >= 2 and s["asr_uncovered"] >= 1


def test_saving_new_terms_does_not_retrigger_translation(sample, api_config, fake, isolated_home):
    """这一集自己存进作品术语表的术语，不应该让这一集下次运行时重新翻译。"""
    args = (sample, "--no-asr", "--no-ocr", "--config", api_config, "--series", "海辺")
    assert run(*args) == 0
    calls = len(fake.CALLS)
    terms = json.loads((isolated_home / "series" / "海辺" / "glossary.json").read_text(encoding="utf-8"))["terms"]
    assert terms[0]["from"] == "sample"
    assert run(*args) == 0
    assert len(fake.CALLS) == calls


def test_edited_own_terms_are_kept_and_used(sample, api_config, fake, isolated_home):
    """这一集自己整理的术语，人改过（edited）就留着：重新翻译时照它翻，存术语表时不被新整理的换掉。"""
    args = (sample, "--no-asr", "--no-ocr", "--config", api_config, "--series", "海辺")
    assert run(*args) == 0
    gl = isolated_home / "series" / "海辺" / "glossary.json"
    data = json.loads(gl.read_text(encoding="utf-8"))
    assert data["terms"][0]["src"] == "深浦町" and data["terms"][0]["from"] == "sample"
    data["terms"][0].update(tr="深浦镇（改过）", edited=True)
    gl.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    calls = len(fake.CALLS)
    assert run(*args) == 0
    assert len(fake.CALLS) > calls  # 改了就重新翻译
    assert any("深浦镇（改过）" in user for _, user in fake.CALLS[calls:])
    terms = json.loads(gl.read_text(encoding="utf-8"))["terms"]
    mine = [t for t in terms if t["src"] == "深浦町"]
    assert len(mine) == 1 and mine[0]["tr"] == "深浦镇（改过）" and mine[0]["edited"]


def test_project_glossary_without_series(sample, api_config, fake):
    assert run(sample, "--no-asr", "--no-ocr", "--config", api_config) == 0
    gl = sample.with_name("sample.nyasub") / "glossary.json"
    assert json.loads(gl.read_text(encoding="utf-8"))["terms"][0]["src"] == "深浦町"
    # 手动加一条术语：下次运行要重新翻译，并且提示词里带上它
    data = json.loads(gl.read_text(encoding="utf-8"))
    data["terms"].append({"src": "青森県", "tr": "青森县", "from": "manual"})
    gl.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    calls = len(fake.CALLS)
    assert run(sample, "--no-asr", "--no-ocr", "--config", api_config) == 0
    assert len(fake.CALLS) > calls
    assert any("青森県" in user and "青森县" in user for _, user in fake.CALLS[calls:])


def test_plan_marks_disabled_steps_and_output_files(sample, api_config, fake):
    """关着的步骤显示“未启用”（不是“可复用”）；输出字幕文件按文件在不在、新不新算。"""
    import os
    import time

    from nyasub.core import config, pipeline

    assert run(sample, "--no-asr", "--no-ocr", "--config", api_config) == 0
    cfg, _ = config.load(str(api_config), None, {"asr": {"enabled": False}, "ocr": {"enabled": False}})
    r = pipeline.Run(sample, cfg, pipeline.Options())
    plan = {p["stage"]: p for p in r.plan()}
    assert {k: plan[k]["status"] for k in ("asr", "ocr", "ocr_refine")} == {"asr": "off", "ocr": "off", "ocr_refine": "off"}
    assert plan["asr"]["note"] == "未启用" and plan["subs"]["status"] == "done"
    st = r.render_status()
    assert st["status"] == "done" and "sample.zh-Hans.ass" in st["files"]
    ann = sample.with_name("sample.nyasub") / "stages" / "annotate.json"
    later = time.time() + 30
    os.utime(ann, (later, later))  # 改过字幕（注音结果比输出文件新）→ 要重新输出
    assert r.render_status()["status"] == "stale"
    sample.with_name("sample.zh-Hans.ass").unlink()
    sample.with_name("sample.zh-Hans.srt").unlink()
    assert r.render_status()["status"] == "missing"


def test_redo_all_without_cache_and_edits(sample, api_config, fake):
    assert run(sample, "--no-asr", "--no-ocr", "--config", api_config) == 0
    proj = sample.with_name("sample.nyasub")
    (proj / "edits.json").write_text(json.dumps({"q1": {"tr": {"zh-Hans": "手改的译文"}}}, ensure_ascii=False),
                                     encoding="utf-8")
    assert run(sample, "--no-asr", "--no-ocr", "--config", api_config) == 0  # 套用修改、记下锚点（和审校页改完一样）
    calls = len(fake.CALLS)

    # 只全部重做：请求有缓存，不再调大模型；手动修改还在
    assert run(sample, "--no-asr", "--no-ocr", "--config", api_config, "--redo", "all") == 0
    assert len(fake.CALLS) == calls
    assert "手改的译文" in sample.with_name("sample.zh-Hans.srt").read_text(encoding="utf-8-sig")

    # 不用缓存 + 清掉手动修改：重新请求，手动修改备份后清掉
    assert run(sample, "--no-asr", "--no-ocr", "--config", api_config, "--redo", "all,no_cache,clear_edits") == 0
    assert len(fake.CALLS) > calls
    left = json.loads((proj / "edits.json").read_text(encoding="utf-8"))
    assert not left.get("cues") and not left.get("ops")
    assert list((proj / "backup").glob("edits-*.json"))
    assert "手改的译文" not in sample.with_name("sample.zh-Hans.srt").read_text(encoding="utf-8-sig")


def test_translate_sends_frames_and_falls_back_without_images(sample, api_config, fake, monkeypatch):
    from nyasub.llm import client

    seen = []

    def with_images(self, system, user, images=None):
        seen.append(len(images or []))
        return fake.respond(system, user)

    monkeypatch.setattr(client.LLM, "_call", with_images)
    assert run(sample, "--no-asr", "--no-ocr", "--config", api_config) == 0
    assert max(seen) > 0  # 融合翻译那一步每条都附了截图
    assert list((sample.with_name("sample.nyasub") / "media" / "frames").glob("*.jpg"))

    # 接口不收图片：报 400 后这一集都不带图，翻译照样完成
    seen.clear()

    def no_images(self, system, user, images=None):
        seen.append(len(images or []))
        if images:
            err = client.FatalLLMError("大模型接口报错（400）：image input is not supported")
            err.status = 400
            raise err
        return fake.respond(system, user)

    monkeypatch.setattr(client.LLM, "_call", no_images)
    assert run(sample, "--no-asr", "--no-ocr", "--config", api_config, "--redo", "all,no_cache") == 0
    assert seen.count(0) >= 2 and sum(1 for n in seen if n) == 1  # 只试了一次带图
    assert "译:" in sample.with_name("sample.zh-Hans.srt").read_text(encoding="utf-8-sig")


def test_stage_stopped_midway_is_not_saved_as_done(sample, api_config):
    """步骤里把取消当成一次普通失败吞掉、照样返回了结果（比如复核某一批失败就保留本地结果）：不能存成完成。"""
    import threading

    from nyasub.core import config, pipeline

    cfg, _ = config.load(str(api_config), None, {"asr": {"enabled": False}, "ocr": {"enabled": False}})
    r = pipeline.Run(sample, cfg, pipeline.Options())
    r.cancel = threading.Event()

    def swallow():
        r.cancel.set()
        try:
            raise pipeline.Cancelled()
        except Exception:
            return {"partial": True}

    with pytest.raises(pipeline.Cancelled):
        r.stage("probe", swallow, force=True)
    assert not sample.with_name("sample.nyasub").joinpath("stages", "probe.fp").exists()


def test_redo_request_survives_stop_and_can_pick_steps(sample, api_config, fake):
    from nyasub.core import config, pipeline

    assert run(sample, "--no-asr", "--no-ocr", "--config", api_config) == 0
    cfg, _ = config.load(str(api_config), None, {"asr": {"enabled": False}, "ocr": {"enabled": False}})

    # 要求全部重做、刚开始就停了：没做的步骤都显示“将重做”，下次普通运行接着重做
    r = pipeline.Run(sample, cfg, pipeline.Options(redo={"all"}))
    r._fresh_start()
    plan = {p["stage"]: p for p in pipeline.Run(sample, cfg, pipeline.Options()).plan()}
    assert plan["fuse"]["status"] == "stale" and plan["fuse"]["note"] == "要求重做"
    assert plan["translate"]["status"] == "stale"
    calls = len(fake.CALLS)
    assert run(sample, "--no-asr", "--no-ocr", "--config", api_config) == 0
    assert len(fake.CALLS) == calls  # 重做了，但请求有缓存，不花钱
    plan = {p["stage"]: p["status"] for p in pipeline.Run(sample, cfg, pipeline.Options()).plan()}
    assert plan["translate"] == "done" and plan["fuse"] == "done"

    # 只重做整理、不用缓存：整理重新请求（翻译跟着重做），前面的步骤复用
    before = fp(sample, "fuse")
    assert run(sample, "--no-asr", "--no-ocr", "--config", api_config, "--redo", "organize,no_cache") == 0
    assert len(fake.CALLS) > calls
    assert fp(sample, "fuse") == before
    assert not pipeline.Run(sample, cfg, pipeline.Options())._pending()


def test_redo_no_cache_reuses_cache_written_after_restart(sample, api_config, fake, monkeypatch):
    """勾选不用缓存后中途重启：只作废重做开始前的旧缓存，本次已跑出并写入的新缓存可以接着用。"""
    import time

    from nyasub import env
    from nyasub.core import config, pipeline
    from nyasub.llm.client import LLM
    from nyasub.sources import asr_api

    assert run(sample, "--no-asr", "--no-ocr", "--config", api_config) == 0
    cache_dir = sample.with_name("sample.nyasub") / "llm_cache"
    old_caches = list(cache_dir.glob("*.json"))
    assert old_caches

    cfg, _ = config.load(str(api_config), None, {"asr": {"enabled": False}, "ocr": {"enabled": False}})
    time.sleep(0.03)
    r = pipeline.Run(sample, cfg, pipeline.Options(redo={"translate", "no_cache"}))
    r._fresh_start()
    since = r._pending()["translate"]["fresh"]
    assert isinstance(since, float) and since > 0

    calls = 0
    llm = LLM(cfg, cache_dir=cache_dir)

    def fake_call(s, u, i, audio=None):
        nonlocal calls
        calls += 1
        return {"ok": u}

    monkeypatch.setattr(llm, "_call", fake_call)
    try:
        env.set_fresh(since)
        assert not env.cache_valid(old_caches[0])
        time.sleep(0.03)
        # 第一次请求：写入新缓存；模拟中断重启后再请求同一条（仍带着 since），命中新缓存不重复调用
        assert llm.chat_json("sys", "new-item") == {"ok": "new-item"}
        assert calls == 1
        assert llm.chat_json("sys", "new-item") == {"ok": "new-item"}
        assert calls == 1

        # 云端 ASR 切片缓存同样按时间戳续跑：重做前旧切片不复用，重做后写入的新切片可复用
        asr_old = cache_dir / "asr_api" / "old.json"
        asr_old.parent.mkdir(parents=True, exist_ok=True)
        asr_old.write_text("[]", encoding="utf-8")
        asr_old.touch()
        import os
        os.utime(asr_old, (since - 10, since - 10))
        _, hit_old = asr_api._cached(cache_dir, "old", lambda: [{"text": "fresh"}])
        assert hit_old is False
        _, hit_new = asr_api._cached(cache_dir, "old", lambda: [{"text": "should-not-run"}])
        assert hit_new is True
    finally:
        env.set_fresh(False)


def test_cues_typed_drop_never_reach_subtitles(sample, api_config, fake, monkeypatch):
    """模型把要丢掉的条目写成 type 为 drop 的字幕：新翻译直接丢掉；已经翻好的旧结果在注音那一步过滤掉。"""
    import json as _json

    from nyasub.llm import client

    def drops_first(self, system, user, images=None):
        res = fake.respond(system, user)
        if res.get("cues"):
            res["cues"][0]["type"] = "drop"
        return res

    monkeypatch.setattr(client.LLM, "_call", drops_first)
    assert run(sample, "--no-asr", "--no-ocr", "--config", api_config) == 0
    proj = sample.with_name("sample.nyasub")
    tr = _json.loads((proj / "stages" / "translate.json").read_text(encoding="utf-8"))
    assert all(c["type"] != "drop" for c in tr["cues"])

    # 旧版本的翻译结果里留着 drop 条目：不重新翻译，只在注音 / 输出时去掉
    victim = tr["cues"][0]
    victim["type"] = "drop"
    (proj / "stages" / "translate.json").write_text(_json.dumps(tr, ensure_ascii=False), encoding="utf-8")
    (proj / "stages" / "annotate.fp").unlink()  # 注音升了版本：要重做（翻译不重做）
    from nyasub.core import config, pipeline

    cfg, _ = config.load(str(api_config), None, {"asr": {"enabled": False}, "ocr": {"enabled": False}})
    r = pipeline.Run(sample, cfg, pipeline.Options())
    r.load_state()
    r.reannotate()
    r.load_state()
    assert victim["id"] not in [c["id"] for c in r.render_context()[0]]


def test_normal_run_clears_stale_fresh_flag(sample, api_config, fake):
    """普通运行或未勾选“不用缓存”的重做，会覆盖清理残留的 fresh 标记，不再无视已有缓存。"""
    from nyasub.core import config, pipeline
    from nyasub.core.store import save_json

    assert run(sample, "--no-asr", "--no-ocr", "--config", api_config) == 0
    cfg, _ = config.load(str(api_config), None, {"asr": {"enabled": False}, "ocr": {"enabled": False}})
    r = pipeline.Run(sample, cfg, pipeline.Options())
    save_json(r._pending_path(), {"ocr_refine": {"fresh": True}, "translate": {"fresh": 1700000000.0}})

    pipeline.Run(sample, cfg, pipeline.Options(redo=set()))._fresh_start()
    p1 = pipeline.Run(sample, cfg, pipeline.Options())._pending()
    assert p1["ocr_refine"]["fresh"] is False and p1["translate"]["fresh"] is False

    save_json(r._pending_path(), {"ocr_refine": {"fresh": True}})
    pipeline.Run(sample, cfg, pipeline.Options(redo={"ocr_refine"}))._fresh_start()
    p2 = pipeline.Run(sample, cfg, pipeline.Options())._pending()
    assert p2["ocr_refine"]["fresh"] is False
