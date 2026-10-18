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
