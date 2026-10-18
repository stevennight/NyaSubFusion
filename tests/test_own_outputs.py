"""找“自带字幕”时不能把本工具自己以前输出的字幕算进去（否则改了配置再跑，会把译文当成原文读进来）。"""
from nyasub import cli
from nyasub.core import config, pipeline
from nyasub.sources import subtitle

from . import fakellm


def _run(*args):
    return cli.main(["run", *map(str, args)])


def test_changed_target_does_not_pick_old_output(sample, api_config, monkeypatch):
    fakellm.install(monkeypatch)
    sample.with_name("sample.srt").rename(sample.with_name("other.srt"))  # 这个视频没有自带字幕
    assert _run(sample, "--no-asr", "--no-ocr", "--config", api_config, "--target", "zh-Hans") == 0
    assert sample.with_name("sample.zh-Hans.srt").exists() and sample.with_name("sample.zh-Hans.ass").exists()
    # 改成翻译成英语：以前输出的 zh-Hans 字幕不再是“现在会写的文件名”，但仍然不能当成自带字幕
    cfg, _ = config.load(str(api_config), None, {"project": {"targets": ["en"]}})
    r = pipeline.Run(sample, cfg, pipeline.Options())
    assert r.external_subs() is None
    assert "sample.zh-Hans.srt" in r.written_names() and "sample.ja-merged.ass" in r.written_names()


def test_old_project_without_record_recognises_outputs_by_content(tmp_path):
    video = tmp_path / "ep1.mp4"
    video.write_bytes(b"")
    (tmp_path / "ep1.zh-Hans.ass").write_text("[Script Info]\n; 由 NyaSubFusion 生成\n", encoding="utf-8")
    (tmp_path / "ep1.zh-Hans.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\n译文\n", encoding="utf-8")
    assert subtitle.find_external(str(video)) is None          # ASS 有标记，旁边同名的 SRT 也是我们写的
    (tmp_path / "ep10.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n", encoding="utf-8")
    assert subtitle.find_external(str(video)) is None          # ep1 不能匹配到 ep10.srt
    (tmp_path / "ep1.ja.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\n本物\n", encoding="utf-8")
    assert subtitle.find_external(str(video)).endswith("ep1.ja.srt")
    (tmp_path / "ep1.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\n本物\n", encoding="utf-8")
    assert subtitle.find_external(str(video)).endswith("ep1.srt")   # 名字最短的最像原始那份
