"""语音识别（faster-whisper）。用途：定时间轴、发现没有字幕覆盖的语音、给大模型做纠错参考。"""
import os
import time

from ..env import check_cancel, log


def transcribe(wav, c: dict, language=None, offset=0.0, duration=0.0):
    """c 是 AsrCfg 的 dict；language 为 None 时自动检测。返回 (语音段列表, 检测到的语言)。"""
    if c.get("hf_endpoint"):
        os.environ["HF_ENDPOINT"] = c["hf_endpoint"]
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")  # 镜像站不支持 xet 协议
    from faster_whisper import WhisperModel
    from faster_whisper.utils import download_model

    root = c.get("download_root") or None
    from .. import gpu

    if c["device"] == "cuda" and not gpu.asr_ready():
        log("没有可用的 CUDA（缺显卡或“语音识别 GPU 加速”组件），语音识别用 CPU，会慢一些")
        c = dict(c, device="cpu", compute_type="int8")
    try:
        download_model(c["model"], local_files_only=True, cache_dir=root)
        log(f"加载语音模型 {c['model']}（已在本地缓存）")
    except Exception:
        log(f"首次使用，正在下载语音模型 {c['model']}（large 模型约 3GB，只需下载一次）")
    try:
        model = WhisperModel(c["model"], device=c["device"], compute_type=c["compute_type"], download_root=root)
    except Exception as e:
        if c["device"] != "cuda":
            raise
        log(f"GPU 加载失败（{e}），改用 CPU，会慢很多")
        model = WhisperModel(c["model"], device="cpu", compute_type="int8", download_root=root)
    segments, info = model.transcribe(
        wav,
        language=language or None,
        beam_size=c["beam_size"],
        vad_filter=c["vad"],
        vad_parameters={"min_silence_duration_ms": 400, "threshold": float(c.get("vad_threshold", 0.25))},
        word_timestamps=True,
        condition_on_previous_text=False,
        no_speech_threshold=0.6,
    )
    check_cancel()
    if not language:
        log(f"语音语言检测：{info.language}（{info.language_probability:.0%}）")
    out = []
    t0 = time.time()
    last_report = 0
    for seg in segments:
        check_cancel()  # 每识别出一段检查一次：一小时的节目也能随时停下
        text = seg.text.strip()
        if not text:
            continue
        words = [{"s": round(w.start + offset, 3), "e": round(w.end + offset, 3), "w": w.word}
                 for w in (seg.words or [])]
        out.append({
            "id": f"a{len(out) + 1}",
            "start": round(seg.start + offset, 3),
            "end": round(seg.end + offset, 3),
            "text": text,
            "no_speech": round(seg.no_speech_prob, 3),
            "logprob": round(seg.avg_logprob, 3),
            "words": words,
        })
        if duration and seg.end - last_report > 120:
            last_report = seg.end
            log(f"  语音识别 {seg.end / duration * 100:5.1f}%  ({time.time() - t0:.0f}s)")
    del model
    return out, info.language
