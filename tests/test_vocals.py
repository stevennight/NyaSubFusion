"""人声分离（MDX 的分块 STFT / 拼接）、语音段时间修复、什么时候自动做人声分离。"""
import subprocess
import wave

import numpy as np
import pytest

from nyasub.core import config, pipeline
from nyasub.sources import asr, vocals


# ---------------- 时间修复 ----------------
def _seg(id, words, text=None):
    return {"id": id, "start": words[0][1], "end": words[-1][2], "text": text or "".join(w for w, _, _ in words),
            "words": [{"w": w, "s": s, "e": e} for w, s, e in words]}


def test_repair_moves_stray_first_char_to_real_speech():
    # 真实例子：“気”停在音乐开头，后面 13 秒才是台词
    segs = [_seg("a5", [("気", 161.55, 161.91), ("持", 175.16, 175.28), ("ち", 175.28, 175.62), ("いい", 175.62, 175.68)]),
            _seg("a16", [("入", 243.22, 243.62), ("りました", 291.33, 291.73), ("よ", 291.73, 292.03)])]
    out = asr.repair(segs)
    assert [(s["id"], s["start"], s["end"], s["text"]) for s in out] == [
        ("a5", 175.16, 175.68, "気持ちいい"), ("a16", 291.33, 292.03, "入りましたよ")]


def test_repair_splits_long_gaps_and_keeps_normal_segments():
    normal = _seg("a1", [("あの", 15.5, 15.9), ("時", 15.9, 16.2), ("俺", 16.3, 16.6)])
    gap = _seg("a10", [("それ", 212.75, 213.41), ("返して", 214.35, 215.37), ("この", 225.95, 226.97), ("村", 226.97, 227.35)])
    tail = _seg("a20", [("ダメ", 300.0, 300.5), ("かも", 300.5, 300.9), ("ね", 320.0, 320.2)])
    out = asr.repair([normal, gap, tail])
    assert out[0] is normal
    assert [(s["id"], s["start"], s["text"]) for s in out[1:3]] == [("a10", 212.75, "それ返して"), ("a10-2", 225.95, "この村")]
    assert (out[3]["start"], out[3]["end"], out[3]["text"]) == (300.0, 300.9, "ダメかもね")  # 结尾的碎片并到前面


# ---------------- 分块分离：用“原样输出”的假模型检查 STFT / ISTFT 和拼接 ----------------
class _Identity:
    def __init__(self, *a, **k):
        pass

    def get_providers(self):
        return ["CPUExecutionProvider"]

    def run(self, _, feeds):
        return [feeds["input"]]


@pytest.fixture
def fake_model(monkeypatch, tmp_path):
    import onnxruntime

    monkeypatch.setattr(onnxruntime, "InferenceSession", _Identity)
    path = tmp_path / "m.onnx"
    path.write_bytes(b"x")
    monkeypatch.setattr(vocals, "ensure_model", lambda: path)
    monkeypatch.setitem(vocals.MODEL, "compensate", 1.0)


def test_mdx_chunk_roundtrip(fake_model):
    mdx = vocals._Mdx("x", gpu=False)
    t = np.arange(mdx.chunk) / vocals.RATE
    x = np.stack([np.sin(2 * np.pi * 440 * t), 0.5 * np.sin(2 * np.pi * 1000 * t)]).astype(np.float32)
    y = mdx.run(x)
    assert y.shape == (2, mdx.gen)
    ref = x[:, mdx.trim: mdx.trim + mdx.gen]
    # 高于 dim_f 的频率被模型丢掉（≈17.6 kHz 以上），440 / 1000 Hz 应该几乎原样
    assert np.max(np.abs(y - ref)) < 1e-3


def test_separate_streams_whole_file_without_seams(fake_model, tmp_path):
    src = tmp_path / "in.wav"
    # 13 秒（两块多一点，最后一块不满）：1 kHz 正弦，左右声道一样
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=1000:duration=13:sample_rate=44100",
                    "-ac", "2", str(src)], check=True)
    out = tmp_path / "vocals.wav"
    on_gpu, _ = vocals.separate(src, out, 13.0)
    assert not on_gpu
    with wave.open(str(out)) as w:
        assert (w.getframerate(), w.getnchannels()) == (16000, 1)
        data = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    assert abs(len(data) / 16000 - 13.0) < 0.05
    # 每 0.25 秒的音量都差不多：块与块的拼接处没有断掉 / 跳变
    rms = [np.sqrt(np.mean(data[i:i + 4000] ** 2)) for i in range(4000, len(data) - 8000, 4000)]
    assert min(rms) > 0.8 * max(rms) and max(rms) > 0.05


# ---------------- 什么时候做人声分离 ----------------
def _run(sample, separate, subs):
    cfg, _ = config.load(None, None, {"asr": {"separate": separate}})
    r = pipeline.Run(sample, cfg, pipeline.Options())
    r.results["subs"] = subs
    return r


def test_separation_decision_and_fingerprint(sample):
    assert _run(sample, "auto", [])._separate() is True  # 没有字幕：语音是主要来源 → 分离
    assert _run(sample, "auto", [{"text": "x"}])._separate() is False
    assert _run(sample, "on", [{"text": "x"}])._separate() is True
    assert _run(sample, "off", [])._separate() is False
    # 不分离时语音识别的指纹和以前一样（已有项目不会因为升级重新识别）
    assert _run(sample, "auto", [{"text": "x"}])._extra("asr") is None
    assert _run(sample, "auto", [])._extra("asr") == {"vocals": "Kim_Vocal_2"}
