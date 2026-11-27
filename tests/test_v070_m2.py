"""v0.7.0 M2 测试：多模态画面网格拼图、原声/人声自适应双轨选择、统一说话人信号与防跨人合并。"""
import subprocess
import wave

import numpy as np

from nyasub.fusion.fuse import fuse
from nyasub.lang import get as get_lang
from nyasub.llm import prompts, speakers as llm_speakers, translate
from nyasub.sources import asr_api, asr_visual


def _write_wav(path, samples: np.ndarray, sr: int = 16000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.clip(samples * 32767.0, -32768, 32767).astype(np.int16).tobytes())


def test_choose_hybrid_track_clean_vs_bgm(tmp_path):
    """人声清晰选 orig，BGM 干扰强（原声远大于人声轨）选 vocals。"""
    sr = 16000
    t = np.linspace(0, 4.0, sr * 4, endpoint=False)
    voice = 0.25 * np.sin(2 * np.pi * 440 * t)
    # 场景 1：原声与人声几乎一样（无 BGM）→ 选 orig
    p_orig_clean = tmp_path / "orig_clean.wav"
    p_voc_clean = tmp_path / "voc_clean.wav"
    _write_wav(p_orig_clean, voice)
    _write_wav(p_voc_clean, voice * 0.95)
    assert asr_api.choose_hybrid_track(asr_api.Wav(p_orig_clean), asr_api.Wav(p_voc_clean), 0.0, 4.0) == "orig"

    # 场景 2：原声混有很强的 BGM（能量比人声轨高 > 6 dB）→ 选 vocals
    p_orig_bgm = tmp_path / "orig_bgm.wav"
    p_voc_bgm = tmp_path / "voc_bgm.wav"
    bgm = 0.7 * np.sin(2 * np.pi * 120 * t)
    _write_wav(p_orig_bgm, voice * 0.3 + bgm)
    _write_wav(p_voc_bgm, voice * 0.3)
    assert asr_api.choose_hybrid_track(asr_api.Wav(p_orig_bgm), asr_api.Wav(p_voc_bgm), 0.0, 4.0) == "vocals"


def test_extract_chunk_grids_with_ffmpeg(tmp_path):
    """用 ffmpeg 生成 10 秒测试视频，验证能拼出带时间范围标签的网格图。"""
    vid = tmp_path / "clip.mp4"
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=10:size=320x180:rate=5",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(vid)],
        capture_output=True,
    )
    assert r.returncode == 0
    grids = asr_visual.extract_chunk_grids(vid, 0.0, 10.0, tmp_path / "grids", interval=2.0)
    assert len(grids) >= 1
    assert "参考画面拼图 1/" in grids[0]["label"]
    assert (tmp_path / "grids" / "grid_01.jpg").exists()


def test_speaker_flows_from_asr_to_fusion_prompts_and_blocks_cross_speaker_merge():
    """ASR speaker 贯穿 _from_chat → fuse → make_cue → prompts，且 can_merge 禁止合并不同说话人。"""
    segs = asr_api._from_chat({
        "language": "ja",
        "segments": [
            {"start": "00:01.00", "end": "00:02.50", "text": "いや！信乃姉様！", "speaker": "浜路"},
            {"start": "00:02.80", "end": "00:04.00", "text": "大丈夫だ、浜路。", "speaker": "信乃"},
        ],
    }, t0=0.0, seconds=10.0)
    assert [s["speaker"] for s in segs] == ["浜路", "信乃"]

    # 融合后 speaker 透传到 asr items
    asr_in = [dict(s, id=f"a{i + 1}") for i, s in enumerate(segs)]
    fused = fuse([], [], asr_in, {"auto_sync": False, "dup_similarity": 70, "asr_match_similarity": 60,
                                  "asr_uncovered_ratio": 0.3, "snap_seconds": 0.7}, get_lang("ja"), 10.0)
    assert [it.get("speaker") for it in fused["items"]] == ["浜路", "信乃"]

    # prompts.item_line 和 cue_line 均带说话人信息
    assert "说话人：浜路" in prompts.item_line(fused["items"][0], asr_only=True)
    c1 = translate.make_cue([fused["items"][0]], "いや！信乃姉様！", "", "zh-Hans", "dialogue", "ja")
    c2 = translate.make_cue([fused["items"][1]], "大丈夫だ、浜路。", "", "zh-Hans", "dialogue", "ja")
    c1["id"], c2["id"] = "q1", "q2"
    assert c1["speaker"] == "浜路" and c2["speaker"] == "信乃"
    assert "说话人：浜路" in prompts.cue_line(c1)

    # 不同说话人严禁合并；同说话人允许合并
    assert translate.can_merge([c1, c2]) is False
    c2_same = dict(c2, speaker="浜路")
    assert translate.can_merge([c1, c2_same]) is True


def test_broadcast_speaker_judge_includes_asr_speaker_hint():
    """广播字幕双人同屏判定（llm/speakers.py）附带 ASR 说话人标签作为线索。"""
    sub = {
        "id": "c1", "start": 1.0, "end": 4.0, "text": "いや！\n大丈夫だ。",
        "speaker_check": [{"pos": "居中", "text": "いや！"}, {"pos": "居中", "text": "大丈夫だ。"}],
    }
    segs = [
        {"start": 1.0, "end": 2.0, "text": "いや！", "speaker": "浜路"},
        {"start": 2.5, "end": 3.8, "text": "大丈夫だ。", "speaker": "信乃"},
    ]
    txt = llm_speakers._item_text(sub, None, None, segs)
    assert "[浜路]" in txt and "[信乃]" in txt
