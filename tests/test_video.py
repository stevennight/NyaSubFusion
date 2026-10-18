"""输出视频：字体子集、封装 MKV（字幕轨 + 字体）、压制硬字幕、取消。"""
import json
import subprocess
import threading

import numpy as np
import pytest

from nyasub import cli, env
from nyasub.render import fonts, video

from . import fakellm


@pytest.fixture
def fake(monkeypatch):
    fakellm.install(monkeypatch)


def run(*args):
    return cli.main(["run", *map(str, args)])


def probe_streams(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
                         capture_output=True, check=True).stdout
    return json.loads(out)["streams"]


def frame(path, t):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1", "-vf", "scale=320:180",
                          "-pix_fmt", "gray", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(180, 320).astype(int)


def test_ass_fonts_follow_styles_and_overrides(tmp_path):
    p = tmp_path / "a.ass"
    p.write_text("\n".join([
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: main,Microsoft YaHei,60,&H00FFFFFF,&H000000FF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,3,1,2,40,40,46,1",
        "Style: sub,Yu Gothic,38,&H00FFFFFF,&H000000FF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,2,40,40,46,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        "Dialogue: 0,0:00:01.00,0:00:02.00,main,,0,0,0,,你好,世界\\N{\\rsub}日本{\\fnSegoe UI\\b0}ab",
    ]), encoding="utf-8")
    got = video.ass_fonts(p)
    assert got[("Microsoft YaHei", False)] == set("你好,世界")
    assert got[("Yu Gothic", True)] == set("日本")
    assert got[("Segoe UI", False)] == set("ab")


@pytest.mark.skipif(not fonts.find("Microsoft YaHei"), reason="需要系统字体 Microsoft YaHei")
def test_font_subset_is_small_and_keeps_name(tmp_path):
    from fontTools.ttLib import TTFont

    p = tmp_path / "a.ass"
    p.write_text("[V4+ Styles]\nFormat: Name, Fontname, Bold\nStyle: main,Microsoft YaHei,0\n\n[Events]\n"
                 "Format: Layer, Start, End, Style, Text\nDialogue: 0,0:00:01.00,0:00:02.00,main,字幕测试\n", encoding="utf-8")
    files, notes = video.collect_fonts([p], tmp_path / "fonts", "subset")
    assert not notes and len(files) == 1
    assert files[0].stat().st_size < 200_000  # 完整字体十几 MB
    f = TTFont(files[0])
    assert f["name"].getDebugName(1) == "Microsoft YaHei"
    cmap = f.getBestCmap()
    assert all(ord(c) in cmap for c in "字幕测试") and ord("龍") not in cmap


def test_mux_mkv_with_tracks_and_fonts(sample, api_config, fake):
    assert run(sample, "--no-asr", "--no-ocr", "--config", api_config, "--video", "mux") == 0
    out = sample.with_name("sample" + video.MUX_SUFFIX)
    assert out.exists()
    st = probe_streams(out)
    subs = [s for s in st if s["codec_type"] == "subtitle"]
    assert [s["codec_name"] for s in subs] == ["ass", "subrip"]  # 默认两个方案：双语 ASS + 译文 SRT
    assert subs[0]["tags"]["language"] == "chi" and subs[0]["disposition"]["default"] == 1
    assert subs[1]["disposition"]["default"] == 0
    assert any(s["codec_type"] == "video" for s in st) and any(s["codec_type"] == "audio" for s in st)
    if fonts.find("Microsoft YaHei"):
        att = [s for s in st if s["codec_type"] == "attachment"]
        assert att and all(s["tags"]["mimetype"].startswith("application/") for s in att)

    # 再跑一次：字幕没变，不重新封装；加文件夹时跳过输出的视频
    mtime = out.stat().st_mtime_ns
    assert run(sample, "--no-asr", "--no-ocr", "--config", api_config, "--video", "mux") == 0
    assert out.stat().st_mtime_ns == mtime
    from nyasub.core import pipeline

    assert pipeline.collect_inputs([str(sample.parent)]) == [str(sample)]


def test_burn_draws_subtitles(sample, api_config, fake):
    cfg = api_config.with_name("burn.toml")
    cfg.write_text(api_config.read_text(encoding="utf-8") + '[render.video]\nencoder = "libx264"\nquality = 30\nheight = 360\n',
                   encoding="utf-8")
    assert run(sample, "--no-asr", "--no-ocr", "--config", cfg, "--video", "burn") == 0
    out = sample.with_name("sample" + video.BURN_SUFFIX)
    v = [s for s in probe_streams(out) if s["codec_type"] == "video"][0]
    assert (v["codec_name"], v["height"]) == ("h264", 360)
    cue = json.loads((sample.with_name("sample.nyasub") / "stages" / "annotate.json").read_text(encoding="utf-8"))["cues"][0]
    t = (cue["start"] + cue["end"]) / 2
    diff = np.abs(frame(out, t) - frame(sample, t))
    assert diff[120:, :].mean() > diff[:60, :].mean() + 2  # 字幕画在画面下方


def test_cancel_stops_ffmpeg(sample, api_config, fake):
    from nyasub.core import config, pipeline

    assert run(sample, "--no-asr", "--no-ocr", "--config", api_config) == 0
    cfg, _ = config.load(str(api_config), None, {"render": {"video": {"encoder": "libx264", "quality": 40}}})
    r = pipeline.Run(sample, cfg, pipeline.Options())
    r.cancel = threading.Event()

    def on(ev):
        if ev.get("type") == "progress":
            r.cancel.set()
    env.on_event(on)
    try:
        with pytest.raises(pipeline.Cancelled):
            r.export_video("burn")
    finally:
        env.off_event(on)
    out = sample.with_name("sample" + video.BURN_SUFFIX)
    assert not out.exists() and not video._part(out).exists()


def test_server_export_video_plan_and_cancel(monkeypatch, sample):
    from fastapi.testclient import TestClient

    from nyasub.server.app import create_app

    from .test_server import PROJECT, wait_job

    fakellm.install(monkeypatch)
    with TestClient(create_app()) as c:
        pid = c.post("/api/projects", json={"path": str(sample)}).json()["id"]
        cfg = {**PROJECT, "render": {**PROJECT["render"], "video": {"mode": "mux"}}}
        plan = c.put(f"/api/projects/{pid}/config", json={"project": cfg}).json()["plan"]
        assert [p["stage"] for p in plan][-1] == "mux" and "burn" not in [p["stage"] for p in plan]
        assert c.post(f"/api/projects/{pid}/cancel").status_code == 409  # 没在运行
        c.post(f"/api/projects/{pid}/run", json={})
        assert wait_job(c, pid)["status"] == "done"
        s = c.get(f"/api/projects/{pid}").json()
        assert s["videos"] == {"mux": "sample.sub.mkv"} and all(p["status"] in ("done", "off", "empty") for p in s["plan"])
        subs = [x for x in probe_streams(sample.with_name("sample.sub.mkv")) if x["codec_type"] == "subtitle"]
        assert len(subs) == 2  # 两个启用的方案

        # 单独导出：只封装一个方案
        assert c.post(f"/api/projects/{pid}/export_video", json={"mode": "nope"}).status_code == 400
        r = c.post(f"/api/projects/{pid}/export_video", json={"mode": "mux", "profiles": ["learn"]}).json()
        assert r["file"] == "sample.sub.mkv"
        assert wait_job(c, pid)["status"] == "done"
        subs = [x for x in probe_streams(sample.with_name("sample.sub.mkv")) if x["codec_type"] == "subtitle"]
        assert len(subs) == 1 and subs[0]["tags"]["title"] == "日语"


def test_queue_video_option(monkeypatch, sample):
    from fastapi.testclient import TestClient

    from nyasub.server.app import create_app

    from .test_queue import _wait
    from .test_server import PROJECT

    fakellm.install(monkeypatch)
    with TestClient(create_app()) as c:
        pid = c.post("/api/projects", json={"path": str(sample)}).json()["id"]
        c.put(f"/api/projects/{pid}/config", json={"project": PROJECT})
        with c.websocket_connect("/ws/projects/_queue") as ws:
            ws.receive_json()
            c.post("/api/queue", json={"paths": [str(sample)], "options": {"video": "mux"}})
            kinds = set()
            while True:
                ev = ws.receive_json()
                kinds.add(ev["type"])
                if ev["type"] == "queue" and ev["items"][0]["status"] not in ("pending", "running"):
                    break
        st = _wait(c, lambda s: s["items"][0]["status"] == "done")
        assert st["items"][0]["status"] == "done" and "queue_progress" in kinds
        assert sample.with_name("sample.sub.mkv").exists()


def test_proxy_chains_are_8bit_and_halve_high_fps(monkeypatch):
    from nyasub.sources import media

    monkeypatch.setattr(media, "stream_fps", lambda path, index: (59.94, "60000/1001"))
    v = {"index": 0, "width": 3840, "height": 2160, "aspect": 16 / 9, "interlaced": True}
    names = []
    for name, pre, vf, venc in media.proxy_attempts("x.ts", v):
        names.append(name)
        assert any("format=yuv420p" in f for f in vf)  # 10 位源也转成 8 位，浏览器才能放
        assert "fps=30000/1001" in vf and ("yadif_cuda" in vf[0] or "bwdif" in vf[0])
    assert names[0].startswith("显卡") and "x264" in names[-1]


def test_server_builds_hls_preview(monkeypatch, sample):
    """浏览器不能直接放的格式：转成 HLS 分段，转出第一段就能播；转完播放列表写上结束标记。"""
    import time

    from fastapi.testclient import TestClient

    from nyasub.server.app import create_app

    mkv = sample.with_suffix(".mkv")
    sample.rename(mkv)  # MKV 浏览器不能直接放 → 要转预览视频
    with TestClient(create_app()) as c:
        pid = c.post("/api/projects", json={"path": str(mkv)}).json()["id"]
        old = mkv.with_name("sample.nyasub") / "media" / "proxy.mp4"
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_bytes(b"old")
        r = c.get(f"/api/projects/{pid}/media/source").json()
        assert r["kind"] == "hls" and r["status"] == "building" and r["total"] == pytest.approx(40, abs=1)
        assert c.get(f"/api/projects/{pid}/media/video").status_code == 404
        t0 = time.time()
        while r["status"] == "building" and time.time() - t0 < 60:
            time.sleep(0.3)
            r = c.get(f"/api/projects/{pid}/media/source").json()
        assert r["status"] == "ready" and r["complete"] and r["segments"] >= 9 and not old.exists()
        assert c.get(f"/api/projects/{pid}").json()["proxy"] == "ready"
        pl = c.get(f"/api/projects/{pid}/media/hls/index.m3u8")
        assert pl.headers["cache-control"] == "no-store" and "#EXT-X-ENDLIST" in pl.text
        seg = c.get(f"/api/projects/{pid}/media/hls/seg_00000.ts")
        assert seg.status_code == 200 and seg.headers["content-type"] == "video/mp2t"
        assert c.get(f"/api/projects/{pid}/media/hls/..%2Fproject.toml").status_code == 404
        assert c.get(f"/api/projects/{pid}/media/hls/other.txt").status_code == 404
    segs = mkv.with_name("sample.nyasub") / "media" / "preview"
    v = [s for s in probe_streams(segs / "seg_00000.ts") if s["codec_type"] == "video"][0]
    assert (v["codec_name"], v["pix_fmt"], v["height"]) == ("h264", "yuv420p", 540)


def test_direct_playable_needs_no_preview(sample):
    from fastapi.testclient import TestClient

    from nyasub.server.app import create_app

    with TestClient(create_app()) as c:
        pid = c.post("/api/projects", json={"path": str(sample)}).json()["id"]
        assert c.get(f"/api/projects/{pid}/media/source").json() == {"kind": "direct"}
        assert c.get(f"/api/projects/{pid}/media/video").status_code == 200
