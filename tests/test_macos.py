"""macOS 适配单元测试（在 Windows/Linux 上通过 monkeypatch 模拟 darwin 平台验证）。"""
import sys

from nyasub import components, env, gpu
from nyasub.core.config import VideoOutCfg
from nyasub.render import video
from nyasub.sources import asr, media


def test_vt_quality_mapping():
    assert video.vt_quality(0) == 100
    assert video.vt_quality(20) == 64
    assert video.vt_quality(28) == 50
    assert video.vt_quality(51) == 8


def test_pick_encoders_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert video.pick_encoders("auto", "h264") == ["h264_videotoolbox", "libx264"]
    assert video.pick_encoders("auto", "hevc") == ["hevc_videotoolbox", "libx265"]
    assert video.pick_encoders("auto", "h264", bit_depth="10") == ["hevc_videotoolbox", "libx265"]
    assert video.pick_encoders("hevc_videotoolbox", "h264") == ["hevc_videotoolbox"]


def test_resolve_bit_depth_videotoolbox():
    assert video._resolve_bit_depth("h264_videotoolbox", "10", False) == (False, "yuv420p", [], True)
    assert video._resolve_bit_depth("hevc_videotoolbox", "10", False) == (True, "p010le", ["-profile:v", "main10"], False)
    assert video._resolve_bit_depth("hevc_videotoolbox", "auto", True) == (True, "p010le", ["-profile:v", "main10"], False)
    assert video._resolve_bit_depth("hevc_videotoolbox", "8", False) == (False, "yuv420p", [], False)


def test_burn_vf_videotoolbox():
    vinfo = {"width": 1920, "height": 1080, "index": 0}
    pre, vf = video._burn_vf(vinfo, 1280, 720, inter=False, use_gpu_decode=True, do_709=False, use_10=False, extra_vf="", hw_backend="videotoolbox")
    assert pre == ["-hwaccel", "videotoolbox"]
    assert "scale=1280:720:flags=lanczos" in vf
    assert not any("cuda" in x for x in vf)

    pre709, vf709 = video._burn_vf(vinfo, 1920, 1080, inter=False, use_gpu_decode=True, do_709=True, use_10=True, extra_vf="", hw_backend="videotoolbox")
    assert pre709 == ["-hwaccel", "videotoolbox"]
    assert any("zscale=" in x for x in vf709)


def test_merge_ffmpeg_args_overrides_vt_qv():
    base = [*video.ENCODERS["hevc_videotoolbox"](20), "-pix_fmt", "yuv420p"]
    merged = video.merge_ffmpeg_args(base, "-b:v 4M")
    assert "-q:v" not in merged
    assert "-b:v" in merged and "4M" in merged


def test_media_darwin_frame_and_proxy(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(media, "stream_fps", lambda path, idx: (24.0, "24/1"))
    vinfo = {"index": 0, "width": 1920, "height": 1080, "aspect": 16 / 9, "interlaced": False}

    cmd = media._frame_cmd("a.mp4", vinfo, 3.0, 1280, 720, hw=True)
    assert "-hwaccel" in cmd and "videotoolbox" in cmd
    assert "cuda" not in cmd

    attempts = media.proxy_attempts("a.mp4", vinfo, height=540)
    assert attempts[0][1] == ["-hwaccel", "videotoolbox"]
    assert "h264_videotoolbox" in attempts[0][3]
    assert attempts[1][1] == [] and "h264_videotoolbox" in attempts[1][3]
    assert "libx264" in attempts[2][3]


def test_gpu_ort_providers_coreml(monkeypatch):
    monkeypatch.setattr(gpu, "ocr_ready", lambda: False)
    monkeypatch.setattr(gpu, "ort_coreml_ready", lambda: True)
    assert gpu.ort_providers(True) == ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    assert gpu.ort_providers(False) == ["CPUExecutionProvider"]
    assert gpu.vocals_ready() is True


def test_components_manifest_empty_on_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert components.manifest() == {}


def test_env_home_darwin(monkeypatch):
    monkeypatch.delenv("NYASUB_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    p = env.home()
    assert p.parts[-3:] == ("Library", "Application Support", "NyaSubFusion")


def test_config_accepts_videotoolbox_encoders():
    assert VideoOutCfg(encoder="h264_videotoolbox").encoder == "h264_videotoolbox"
    assert VideoOutCfg(encoder="hevc_videotoolbox").encoder == "hevc_videotoolbox"


def test_mlx_repo_mapping():
    assert asr._mlx_repo("large-v3") == "mlx-community/whisper-large-v3-mlx"
    assert asr._mlx_repo("large-v3-turbo") == "mlx-community/whisper-large-v3-turbo"
    assert asr._mlx_repo("custom/org-model") == "custom/org-model"
