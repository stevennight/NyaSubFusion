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
        hallucination_silence_threshold=2.0,  # 有逐字时间时：长时间没声音的地方如果出了字，多半是幻觉，跳过
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


GAP = 2.0       # 同一段里相邻两个字隔了这么久，就不是一口气说的
FRAGMENT = 2    # 这么短（字数）的一小块挂在大空档另一边，多半是时间对错了的碎片（“気|……13 秒……|持ちいい”）


def repair(segments):
    """修语音段的时间：人声检测把一大段音乐和台词连在一起时，第一个字的时间会停在音乐开头，
    整段跟着提前十几秒（“気”161.5 → “持ちいい”175.2）；中间隔了很久的，其实是两句话。

    - 段内相邻两字间隔超过 GAP 秒就拆开；
    - 拆出来很短的一小块（≤ FRAGMENT 个字）并到空档另一边那块上，时间用那块的；
    - 没有逐字时间的段保持原样。返回新的段列表（id 不变，拆开的加 -2、-3）。"""
    out = []
    for seg in segments:
        words = seg.get("words") or []
        if len(words) < 2:
            out.append(seg)
            continue
        groups = [[words[0]]]
        for w in words[1:]:
            if w["s"] - groups[-1][-1]["e"] > GAP:
                groups.append([w])
            else:
                groups[-1].append(w)
        if len(groups) == 1:
            out.append(seg)
            continue
        size = lambda g: len("".join(x["w"] for x in g).strip())  # noqa: E731
        merged = []
        for g in groups:  # 开头 / 中间的碎片：并到后面那块
            if merged and size(merged[-1]) <= FRAGMENT:
                frag = merged.pop()
                g = [dict(x, s=g[0]["s"], e=g[0]["s"]) for x in frag] + g
            merged.append(g)
        if len(merged) > 1 and size(merged[-1]) <= FRAGMENT:  # 结尾的碎片：并到前面那块
            frag = merged.pop()
            merged[-1] = merged[-1] + [dict(x, s=merged[-1][-1]["e"], e=merged[-1][-1]["e"]) for x in frag]
        for k, g in enumerate(merged):
            text = "".join(x["w"] for x in g).strip()
            out.append(dict(seg, id=seg["id"] if k == 0 else f"{seg['id']}-{k + 1}", start=g[0]["s"], end=g[-1]["e"],
                            text=text, words=g))
    return out
