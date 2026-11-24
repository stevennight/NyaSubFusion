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
    import sys

    from nyasub.sources import media

    monkeypatch.setattr(media, "stream_fps", lambda path, index: (59.94, "60000/1001"))
    v = {"index": 0, "width": 3840, "height": 2160, "aspect": 16 / 9, "interlaced": True}
    names = []
    for name, pre, vf, venc in media.proxy_attempts("x.ts", v):
        names.append(name)
        assert any("format=yuv420p" in f for f in vf)  # 10 位源也转成 8 位，浏览器才能放
        assert "fps=30000/1001" in vf and ("yadif_cuda" in vf[0] or "bwdif" in vf[0])
    if sys.platform == "darwin":
        assert names[0].startswith("VideoToolbox") and "x264" in names[-1]
    else:
        assert names[0].startswith("显卡") and "x264" in names[-1]


def test_hls_gpu_decode_failing_midway_falls_back_to_cpu(monkeypatch, tmp_path):
    """有的片源显卡解码转了几秒就出错：换 CPU 解码重转；都失败时不留带结束标记的半截播放列表。"""
    import sys

    from nyasub.sources import media

    monkeypatch.setattr(media, "_need", lambda *a: None)
    monkeypatch.setattr(media, "stream_fps", lambda path, index: (29.97, "30000/1001"))
    v = {"index": 0, "width": 3840, "height": 2160, "aspect": 16 / 9}
    out, tried = tmp_path / "preview", []
    hw_tag = "videotoolbox" if sys.platform == "darwin" else "cuda"
    expected_name = "CPU 解码 + VideoToolbox" if sys.platform == "darwin" else "CPU 解码 + NVENC"

    def run(cmd, duration, prog, cancel, cwd):
        tried.append("-hwaccel" in cmd and hw_tag in cmd)
        (tmp_path / "preview" / "index.m3u8").write_text("#EXTM3U\n#EXTINF:2.0,\nseg_00000.ts\n#EXT-X-ENDLIST\n")
        return (0, "", 3600.0) if len(tried) == 2 else (1, "Error reinitializing filters!", 2.0)

    monkeypatch.setattr(media, "run_ffmpeg", run)
    assert media.make_hls("x.ts", out, v, 3600.0) == expected_name and tried == [True, False]

    monkeypatch.setattr(media, "run_ffmpeg", lambda cmd, d, p, c, cwd: (1, "boom", 2.0))
    with pytest.raises(RuntimeError):
        media.make_hls("x.ts", out, v, 3600.0)
    assert not out.exists()


def test_hls_short_finished_playlist_not_complete(tmp_path):
    """旧版半路出错留下的：有结束标记但只有几秒，不算转好（打开时会重转）。"""
    from nyasub.sources import media

    (tmp_path / "index.m3u8").write_text("#EXTM3U\n#EXTINF:2.068733,\nseg_00000.ts\n#EXT-X-ENDLIST\n")
    assert media.hls_state(tmp_path)["complete"]
    assert not media.hls_state(tmp_path, 3541.1)["complete"]
    assert media.hls_state(tmp_path, 20.0)["complete"]


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


def test_auto_encoder_follows_source_codec_and_rate_is_capped():
    import sys

    from nyasub.render.video import pick_encoders, rate_cap

    hw_hevc = "hevc_videotoolbox" if sys.platform == "darwin" else "hevc_nvenc"
    hw_h264 = "h264_videotoolbox" if sys.platform == "darwin" else "h264_nvenc"
    assert pick_encoders("auto", "hevc") == [hw_hevc, "libx265"]
    assert pick_encoders("auto", "h264") == [hw_h264, "libx264"]
    assert pick_encoders("auto", "h264", bit_depth="10") == [hw_hevc, "libx265"]
    assert pick_encoders("libx264", "hevc") == ["libx264"]
    uhd, fhd = (3840, 2160), (1920, 1080)
    assert rate_cap(25_000_000, "hevc", uhd, uhd, hw_hevc) == 25_000_000
    assert rate_cap(25_000_000, "hevc", uhd, fhd, hw_hevc) == 6_250_000      # 缩到 1080p：按面积折算
    assert rate_cap(25_000_000, "hevc", uhd, uhd, hw_h264) == 37_500_000     # H.265 → H.264 放宽 1.5 倍
    assert rate_cap(8_000_000, "h264", fhd, fhd, "libx264") == 8_000_000
    assert rate_cap(0, "hevc", uhd, uhd, hw_hevc) is None


def test_detect_bt2020_sdr(monkeypatch):
    def fake(stream, trc=()):
        monkeypatch.setattr(video, "streams", lambda path: [stream])
        monkeypatch.setattr(video, "_body_transfer", lambda path, at: list(trc))
        return video.detect_bt2020_sdr("x.ts", {"index": 0}, duration=100.0)

    ok, _ = fake({"index": 0, "codec_type": "video", "codec_name": "h264", "color_primaries": "bt709"})
    assert ok is False

    ok, _ = fake({"index": 0, "codec_type": "video", "codec_name": "hevc", "color_primaries": "bt2020",
                  "color_transfer": "arib-std-b67"}, trc=[1, 1, 1])
    assert ok is True

    ok, _ = fake({"index": 0, "codec_type": "video", "codec_name": "hevc", "color_primaries": "bt2020",
                  "color_transfer": "arib-std-b67"}, trc=[18, 18, 1])
    assert ok is False

    ok, _ = fake({"index": 0, "codec_type": "video", "codec_name": "vp9", "color_primaries": "bt2020",
                  "color_transfer": "smpte2084"})
    assert ok is False


def test_merge_ffmpeg_args():
    base = ["-c:v", "hevc_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "20", "-b:v", "0",
            "-maxrate", "20000000", "-bufsize", "40000000", "-pix_fmt", "p010le", "-c:a", "copy"]
    merged = video.merge_ffmpeg_args(base, "-preset p6 -b:v 25M -spatial-aq 1")
    assert merged == ["-c:v", "hevc_nvenc", "-tune", "hq", "-rc", "vbr", "-pix_fmt", "p010le", "-c:a", "copy",
                      "-preset", "p6", "-b:v", "25M", "-spatial-aq", "1"]

    with pytest.raises(RuntimeError, match="附加视频滤镜"):
        video.merge_ffmpeg_args(base, "-vf scale=1280:720")
    with pytest.raises(RuntimeError, match="自定义 FFmpeg 参数格式有误"):
        video.merge_ffmpeg_args(base, '"unclosed')


def test_burn_commands_for_709_bit_depth_and_extra_args(monkeypatch, tmp_path):
    vid = tmp_path / "in.ts"
    vid.write_bytes(b"\x00" * 1024)
    ass = tmp_path / "s.ass"
    ass.write_text("[Script Info]\n[Events]\n", encoding="utf-8")
    vinfo = {"index": 0, "width": 3840, "height": 2160, "aspect": 16 / 9, "codec": "hevc", "interlaced": False}
    captured = []

    def fake_ffmpeg(cmd, duration, label, cancel=None, cwd=None, part=None, video=None, vinfo=None):
        captured.append(list(cmd))
        part.write_bytes(b"ok")
        return 0, "", 10.0

    monkeypatch.setattr(video, "streams", lambda path: [
        {"index": 0, "codec_type": "video", "codec_name": "hevc", "color_primaries": "bt2020"},
        {"index": 1, "codec_type": "audio", "codec_name": "aac"},
    ])
    monkeypatch.setattr(video, "_body_transfer", lambda path, at: [1, 1])
    monkeypatch.setattr(video, "_ffmpeg", fake_ffmpeg)

    # 1. BT.2020 SDR 自动转 709 + hevc_nvenc + auto 色深 -> 10-bit p010le + zscale + hevc_metadata
    out1 = tmp_path / "out1.mp4"
    video.burn(str(vid), str(ass), [], out1, vinfo, encoder="hevc_nvenc", duration=10.0,
               work_dir=tmp_path / "w1", bit_depth="auto", color_709="auto")
    c1 = captured[-1]
    assert "-hwaccel" in c1 and "-hwaccel_output_format" not in c1
    vf1 = c1[c1.index("-vf") + 1]
    assert "zscale=pin=2020:tin=709:min=2020_ncl:rin=limited:p=2020:t=linear:m=gbr:r=full:npl=100,format=gbrpf32le," in vf1
    assert "format=yuv420p10le,subtitles=sub.ass:fontsdir=fonts,setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709" in vf1
    assert c1[c1.index("-profile:v") + 1] == "main10"
    assert c1[c1.index("-pix_fmt") + 1] == "p010le"
    assert "hevc_metadata=colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1" in c1[c1.index("-bsf:v") + 1]

    # 2. libx265 指定 10-bit（不开 709） -> main10 + yuv420p10le
    out2 = tmp_path / "out2.mp4"
    video.burn(str(vid), str(ass), [], out2, vinfo, encoder="libx265", duration=10.0,
               work_dir=tmp_path / "w2", bit_depth="10", color_709="off")
    c2 = captured[-1]
    assert c2[c2.index("-profile:v") + 1] == "main10"
    assert c2[c2.index("-pix_fmt") + 1] == "yuv420p10le"
    assert "-bsf:v" not in c2

    # 3. 附加滤镜拼在字幕滤镜前 + 自定义参数替换默认 -preset / -crf
    out3 = tmp_path / "out3.mp4"
    video.burn(str(vid), str(ass), [], out3, vinfo, encoder="libx264", duration=10.0,
               work_dir=tmp_path / "w3", extra_vf="hqdn3d", extra_args="-preset slow -crf 18")
    c3 = captured[-1]
    vf3 = c3[c3.index("-vf") + 1]
    assert vf3.index("hqdn3d") < vf3.index("subtitles=sub.ass:fontsdir=fonts")
    assert c3[c3.index("-preset") + 1] == "slow" and c3.count("-preset") == 1
    assert c3[c3.index("-crf") + 1] == "18" and c3.count("-crf") == 1


def test_burn_force_709_real_ffmpeg(sample, api_config, fake):
    cfg = api_config.with_name("burn_709.toml")
    cfg.write_text(
        api_config.read_text(encoding="utf-8")
        + '[render.video]\nencoder = "libx264"\nquality = 30\ncolor_709 = "force"\nextra_args = "-preset ultrafast"\n',
        encoding="utf-8",
    )
    assert run(sample, "--no-asr", "--no-ocr", "--config", cfg, "--video", "burn") == 0
    out = sample.with_name("sample" + video.BURN_SUFFIX)
    v = [s for s in probe_streams(out) if s["codec_type"] == "video"][0]
    assert (v["codec_name"], v["color_primaries"], v["color_transfer"], v["color_space"]) == (
        "h264", "bt709", "bt709", "bt709"
    )


def test_burn_resumable_chunks_and_resume(tmp_path, monkeypatch):
    import json
    import subprocess
    import threading
    from nyasub.sources import media

    # 造一个 6 秒、每 2 秒一个关键帧的测试视频（含音轨）
    vid = tmp_path / "clip.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-c:v", "libx264", "-g", "50", "-keyint_min", "50", "-sc_threshold", "0", "-preset", "ultrafast",
        "-c:a", "aac", str(vid),
    ], check=True)
    ass = tmp_path / "sub.ass"
    ass.write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 320\nPlayResY: 180\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,18,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:00.50,0:00:03.50,Default,,0,0,0,,Hello\n"
        "Dialogue: 0,0:00:04.00,0:00:05.50,Default,,0,0,0,,World\n",
        encoding="utf-8",
    )
    info = media.probe(str(vid))
    work = tmp_path / "work"
    out = tmp_path / "out.mp4"

    # 第一轮：压完第 1 段后模拟中断取消
    cancel = threading.Event()
    orig_run = media.run_ffmpeg
    enc_calls = []

    def cancel_after_first_chunk(cmd, *args, **kwargs):
        res = orig_run(cmd, *args, **kwargs)
        if any("enc_0000.part.mp4" in str(x) for x in cmd):
            cancel.set()
        return res

    monkeypatch.setattr(media, "run_ffmpeg", cancel_after_first_chunk)
    with pytest.raises(video.Stopped):
        video.burn(str(vid), str(ass), [], out, info["video"], encoder="libx264", quality=30,
                   duration=info["duration"], cancel=cancel, work_dir=work,
                   extra_args="-preset ultrafast", resumable=True, chunk_seconds=2)

    chunks_dir = work / "chunks"
    manifest = json.loads((chunks_dir / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["chunks"]) >= 2
    assert manifest["done"] == [0]
    seg0 = chunks_dir / "enc_0000.mp4"
    assert seg0.exists()
    seg0_mtime = seg0.stat().st_mtime_ns

    # 第二轮：不取消，从断点续压——第 0 段应被跳过，最终拼接出完整含音轨的 MP4 并清理缓存
    def record_run(cmd, *args, **kwargs):
        enc_calls.append(list(cmd))
        return orig_run(cmd, *args, **kwargs)

    monkeypatch.setattr(media, "run_ffmpeg", record_run)
    video.burn(str(vid), str(ass), [], out, info["video"], encoder="libx264", quality=30,
               duration=info["duration"], work_dir=work,
               extra_args="-preset ultrafast", resumable=True, chunk_seconds=2)

    assert not any("enc_0000.part.mp4" in str(x) for cmd in enc_calls for x in cmd)
    assert out.exists() and not chunks_dir.exists()
    out_streams = probe_streams(out)
    assert any(s["codec_type"] == "video" for s in out_streams)
    assert any(s["codec_type"] == "audio" for s in out_streams)
    assert float(out_streams[0]["duration"]) >= 5.5

