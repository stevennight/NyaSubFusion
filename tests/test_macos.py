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


def test_mlx_transcribe_detect_wav_and_fill_gaps(monkeypatch, tmp_path):
    import types
    import wave

    import numpy as np

    wav = tmp_path / "vocals.wav"
    sr = 16000
    # 0~1s 静音，1~3s 有声（将被第一次识别漏掉，由 fill_gaps 补回）
    t = np.linspace(0, 2.0, sr * 2, endpoint=False)
    voiced = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16)
    pcm = np.concatenate([np.zeros(sr, dtype=np.int16), voiced])
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())

    calls = []

    def fake_transcribe(audio, **kwargs):
        calls.append((isinstance(audio, str), kwargs))
        if not isinstance(audio, str) and kwargs.get("word_timestamps") is False:
            return {"language": "ja", "segments": []}
        if isinstance(audio, str):
            return {"language": "en", "segments": []}
        return {
            "language": "ja",
            "segments": [{
                "start": 0.1,
                "end": 1.2,
                "text": "助けて！",
                "no_speech_prob": 0.05,
                "avg_logprob": -0.2,
                "words": [{"start": 0.1, "end": 1.2, "word": "助けて！"}],
            }],
        }

    fake_mod = types.SimpleNamespace(transcribe=fake_transcribe)
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake_mod)
    segs, lang = asr._transcribe_mlx(
        str(wav),
        {"model": "large-v3-turbo", "device": "cuda"},
        language=None,
        offset=10.0,
        duration=3.0,
        fill_gaps=True,
        junk=(),
        detect_wav=str(wav),
    )
    assert lang == "ja"
    assert len(segs) == 1
    assert segs[0]["second_pass"] is True
    assert segs[0]["text"] == "助けて！"
    assert any(is_str and kw.get("hallucination_silence_threshold") == 2.0 for is_str, kw in calls)


def test_mlx_skips_ctranslate2_only_model(monkeypatch):
    import types

    monkeypatch.setitem(sys.modules, "mlx_whisper", types.SimpleNamespace(transcribe=lambda *a, **k: {}))
    assert asr._transcribe_mlx("a.wav", {"model": "kotoba-tech/kotoba-whisper-v2.0-faster"}) is None


def test_vocals_coreml_provider_cache(monkeypatch, tmp_path):
    import onnxruntime

    from nyasub.sources import vocals

    seen = []

    class _FakeSess:
        def __init__(self, path, providers=None):
            seen.append(providers)

        def get_providers(self):
            return ["CoreMLExecutionProvider", "CPUExecutionProvider"]

    monkeypatch.setenv("NYASUB_HOME", str(tmp_path))
    monkeypatch.setattr(gpu, "ocr_ready", lambda: False)
    monkeypatch.setattr(gpu, "ort_coreml_ready", lambda: True)
    monkeypatch.setattr(onnxruntime, "InferenceSession", _FakeSess)
    mdx = vocals._Mdx("m.onnx", gpu=True)
    assert mdx.backend == "CoreML" and mdx.on_gpu is True
    assert isinstance(seen[0][0], tuple) and seen[0][0][0] == "CoreMLExecutionProvider"
    assert "ModelCacheDirectory" in seen[0][0][1]


def test_cli_backend_fresh_path_darwin(monkeypatch):
    from nyasub.llm import cli_backend

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    p = cli_backend._fresh_path()
    assert "/opt/homebrew/bin" in p
    assert ".gemini/antigravity/bin" in p


def test_broadcast_fonts_include_macos_hiragino():
    from nyasub.render import broadcast

    assert "Hiragino Maru Gothic ProN" in broadcast.FONTS
    assert "Hiragino Sans" in broadcast.FONTS


def test_fonts_pingfang_found_on_darwin():
    from nyasub.render import fonts

    if sys.platform != "darwin":
        return
    fonts._index.cache_clear()
    # macOS 系统自带 Hiragino Sans 与 PingFang SC（AssetsV2）
    assert fonts.find("Hiragino Sans") is not None
    assert fonts.find("PingFang SC") is not None


def test_asr_local_engine_config_and_override(monkeypatch):
    from nyasub.core.config import AsrCfg

    assert AsrCfg().local_engine == "auto"
    assert AsrCfg(local_engine="mlx").local_engine == "mlx"
    assert AsrCfg(local_engine="faster-whisper").local_engine == "faster-whisper"

    mlx_called = []
    monkeypatch.setattr(gpu, "mlx_whisper_ready", lambda: True)
    monkeypatch.setattr(asr, "_transcribe_mlx", lambda *a, **k: (mlx_called.append(True), ([], "ja"))[1])

    # auto / mlx 走 MLX
    assert asr.transcribe("a.wav", {"model": "tiny", "device": "cuda", "local_engine": "auto"}) == ([], "ja")
    assert asr.transcribe("a.wav", {"model": "tiny", "device": "cpu", "local_engine": "mlx"}) == ([], "ja")
    assert len(mlx_called) == 2

    # 显式指定 faster-whisper 时跳过 MLX
    import faster_whisper

    class _Boom(Exception):
        pass

    monkeypatch.setattr(faster_whisper, "WhisperModel", lambda *a, **k: (_ for _ in ()).throw(_Boom()))
    monkeypatch.setattr("nyasub.sources.asr_models.resolve", lambda name, root=None: name)
    try:
        asr.transcribe("a.wav", {"model": "tiny", "device": "cuda", "compute_type": "float16", "local_engine": "faster-whisper"})
    except _Boom:
        pass
    assert len(mlx_called) == 2


def test_codex_found_in_chatgpt_app_on_darwin():
    from pathlib import Path

    from nyasub.llm import cli_backend

    if sys.platform != "darwin":
        return
    bundled = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex")
    if bundled.is_file():
        hit = cli_backend.find_executable("codex")
        assert hit is not None


