"""语音识别（faster-whisper）。用途：定时间轴、发现没有字幕覆盖的语音、给大模型做纠错参考。"""
import os
import time

from ..env import check_cancel, log, progress


def transcribe(wav, c: dict, language=None, offset=0.0, duration=0.0, fill_gaps=False, junk=(), detect_wav=None):
    """c 是 AsrCfg 的 dict；language 为 None 时自动检测。返回 (语音段列表, 检测到的语言)。
    fill_gaps：wav 是分离出的人声时，再补识别一遍“有声音、但没识别出字”的地方（见 fill_gaps）。
    junk：语言包的幻觉句（补识别时整段等于这些的丢掉）。
    detect_wav：自动判断语言时用这个文件来判断（分离出的人声里大段安静，会被判断成别的语言，要用原音频）。"""
    if c.get("hf_endpoint"):
        os.environ["HF_ENDPOINT"] = c["hf_endpoint"]
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")  # 镜像站不支持 xet 协议
    from faster_whisper import WhisperModel

    from .. import gpu
    from .asr_models import resolve

    root = c.get("download_root") or None
    if c["device"] == "cuda" and not gpu.asr_ready():
        log("没有可用的 CUDA（缺显卡或“语音识别 GPU 加速”组件），语音识别用 CPU，会慢一些")
        c = dict(c, device="cpu", compute_type="int8")
    path = resolve(c["model"], root)
    try:
        model = WhisperModel(path, device=c["device"], compute_type=c["compute_type"], download_root=root)
    except Exception as e:
        if c["device"] != "cuda":
            raise
        log(f"GPU 加载失败（{e}），改用 CPU，会慢很多")
        model = WhisperModel(path, device="cpu", compute_type="int8", download_root=root)
    if not language and detect_wav:
        from faster_whisper.audio import decode_audio

        language, prob, _ = model.detect_language(decode_audio(str(detect_wav)), vad_filter=True,
                                                  language_detection_segments=4)
        log(f"语音语言检测（按原音频）：{language}（{prob:.0%}）")
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
    language = language or info.language
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
        if duration:
            progress(min(seg.end - offset, duration), duration, "识别")
        if duration and seg.end - last_report > 600:  # 日志里每 10 分钟记一笔就够了，细的进度在步骤图上
            last_report = seg.end
            log(f"  语音识别到 {seg.end / 60:.0f} 分钟（{time.time() - t0:.0f}s）")
    if fill_gaps:
        t1 = time.time()
        extra = _fill_gaps(model, wav, out, language or info.language, offset, junk)
        if extra:
            log(f"补识别：人声轨上有声音、但没识别出字的地方，关掉人声检测再识别一遍，补出 {len(extra)} 段"
                f"（哭喊、歌声等），用时 {time.time() - t1:.0f}s")
            out = sorted(out + extra, key=lambda s: (s["start"], s["end"]))
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
    return _split_sentences(out)


LONG = 6.0      # 超过这么长的段，按句末标点再拆（有的模型不分段，一大段十几秒，字幕时间就不准了）
SENT_END = set("。！？!?…")


def _split_sentences(segments):
    out = []
    for seg in segments:
        words = seg.get("words") or []
        if seg["end"] - seg["start"] <= LONG or len(words) < 2:
            out.append(seg)
            continue
        parts, cur = [], []
        for w in words:
            cur.append(w)
            if w["w"].strip() and w["w"].strip()[-1] in SENT_END and cur[-1]["e"] - cur[0]["s"] >= 0.8:
                parts.append(cur)
                cur = []
        if cur:
            if parts and len("".join(x["w"] for x in cur).strip()) <= 2:
                parts[-1] += cur  # 句末剩下一两个字（“…ッ”），并到前一句
            else:
                parts.append(cur)
        if len(parts) == 1:
            out.append(seg)
            continue
        base = seg["id"]
        for k, g in enumerate(parts):
            out.append(dict(seg, id=base if k == 0 else f"{base}.{k + 1}", start=g[0]["s"], end=g[-1]["e"],
                            text="".join(x["w"] for x in g).strip(), words=g))
    return out


# ---------------- 补识别 ----------------
# 人声检测（Silero VAD）不把哭喊、尖叫、歌声当成说话，这些地方整段被跳过；
# 其实关掉人声检测、切成短片段单独识别，large-v3 是能听出来的（“いやー!…兄さま!”“もう、死なないでー!”）。
# 只在分离出的人声上做：背景音乐去掉了，按音量就能找到“有人声”的地方。
GAP_HOP = 0.05          # 音量按 50 毫秒一帧算
GAP_MIN = 0.6           # 至少这么长的有声片段才补
GAP_CLIP = 20.0         # 合并成不超过这么长的片段送去识别
JUNK_EXACT = {"お疲れ様でした", "それではまた", "お待ちしております", "エンディング", "おわり", "次回もお楽しみに",
              "ご視聴ありがとうございました", "おやすみなさい"}  # 关掉人声检测后常见的幻觉（整段只有这句才丢）


def _voiced_gaps(x, sr, segments):
    """人声轨上有声音、但没被任何语音段覆盖的片段：[(开始, 结束)]，已合并成 ≤ GAP_CLIP 秒。"""
    import numpy as np

    hop = int(sr * GAP_HOP)
    n = len(x) // hop
    if n == 0:
        return []
    rms = np.sqrt(np.mean(x[: n * hop].reshape(n, hop) ** 2, axis=1) + 1e-12)
    db = 20 * np.log10(rms)
    voiced = db > max(-45.0, float(np.percentile(db, 20)) + 18)  # 比底噪高 18 dB，且不低于 -45 dB
    covered = np.zeros(n, bool)
    for s in segments:
        covered[max(0, int((s["start"] - 0.3) / GAP_HOP)): int((s["end"] + 0.3) / GAP_HOP)] = True
    cand = voiced & ~covered
    regions, i = [], 0
    while i < n:
        if not cand[i]:
            i += 1
            continue
        j = i
        while j < n and (cand[j] or (j + 8 < n and cand[j: j + 8].any() and not covered[j])):  # 中间短暂停顿不断开
            j += 1
        if (j - i) * GAP_HOP >= GAP_MIN:
            regions.append((i * GAP_HOP, j * GAP_HOP))
        i = j
    clips = []
    for a, b in regions:
        if clips and a - clips[-1][1] < 1.5 and b - clips[-1][0] <= GAP_CLIP:
            clips[-1] = (clips[-1][0], b)
        else:
            clips.append((a, b))
    return clips


def _keep(seg, text, junk):
    """补识别出的段：去掉幻觉句、极短的碎片、置信度很低的。剩下的交给大模型判断（它会丢掉没意义的）。"""
    import re

    core = re.sub(r"[\s。、！？!?…ー～〜・.,]", "", text)
    if not core or core in JUNK_EXACT or any(h in text for h in junk):
        return False
    if seg.end - seg.start < 0.4 and len(core) <= 3:
        return False
    return seg.avg_logprob >= -0.8


def _fill_gaps(model, wav, segments, language, offset, junk):
    import wave

    import numpy as np

    with wave.open(str(wav)) as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    # 用修好时间的段算覆盖：没修的话，停在音乐开头的那个字会把中间一大段都“覆盖”掉
    base = [dict(s, start=s["start"] - offset, end=s["end"] - offset) for s in repair(segments)]
    out = []
    gaps = _voiced_gaps(x, sr, base)
    for k, (a, b) in enumerate(gaps):
        check_cancel()
        progress(k, len(gaps), "补识别")
        a0, b0 = max(0.0, a - 0.4), min(len(x) / sr, b + 0.4)
        res, _ = model.transcribe(x[int(a0 * sr): int(b0 * sr)], language=language, beam_size=5,
                                  condition_on_previous_text=False, vad_filter=False, word_timestamps=True,
                                  no_speech_threshold=0.6)
        for seg in res:
            text = seg.text.strip()
            if not _keep(seg, text, junk):
                continue
            t = a0 + offset
            out.append({
                "id": f"b{len(out) + 1}", "start": round(seg.start + t, 3), "end": round(seg.end + t, 3), "text": text,
                "no_speech": round(seg.no_speech_prob, 3), "logprob": round(seg.avg_logprob, 3), "second_pass": True,
                "words": [{"s": round(w.start + t, 3), "e": round(w.end + t, 3), "w": w.word} for w in (seg.words or [])],
            })
    return out
