"""本机轻量 ONNX 强制对齐引擎（MMS-300M 1130 语种 CTC Forced Aligner）。

定位：
- 解决多模态大模型（如 Gemini）听字极准、但时间轴是估算（常整体漂移或差 0.5~5 秒）的问题；
- 直接拿大模型转写出的精准文字（或官方歌曲歌词）转为音素/拉丁字母序列，在 16kHz 人声轨上做
  CTC Viterbi 强制对齐，给出 ±30ms 级别的真实物理开口时间；
- 结合人声 50ms 音量包络修正 CTC 的两类物理特性：
  1) 句尾长音/拖音（如唱歌长音）：CTC 倾向只在元音开头打峰值导致收尾过早 → 沿连续人声能量延伸到真正唱完；
  2) 句首串尾 / 句尾吸噪（如窗口末尾吸到 1 秒后的底噪）：切掉跨越长静音的孤立杂音峰。
- 严格只输出真实物理发声起止时间，不在此层人为加阅读缓冲（阅读缓冲统一由 render/layout.py 负责）。
"""
import json
import re
import unicodedata
import wave
from pathlib import Path

import numpy as np

from .. import lang
from ..env import check_cancel, log, progress

REPO = "onnx-community/mms-300m-1130-forced-aligner-ONNX"
MODEL_FILE = "onnx/model_int8.onnx"
VOCAB_FILE = "vocab.json"
TARGET_SR = 16000
CHUNK_SECONDS = 30
FORCED_ALIGN_V = 4  # v4：按上传音频块真实边界整块联合 CTC 对齐（彻底摆脱大模型估时漂移）

_SESSION_CACHE: dict[tuple[str, str], tuple] = {}
_MUSIC_RE = re.compile(r"^[♪♬♫♩\s]+|[♪♬♫♩\s]+$")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _load_model(c: dict | None = None):
    """下载并缓存 MMS-300M INT8 ONNX 模型与词表，返回 (InferenceSession, vocab, blank_id)。"""
    c = c or {}
    endpoint = (c.get("hf_endpoint") or "").strip() or None
    download_root = (c.get("download_root") or "").strip() or None
    key = (endpoint or "", download_root or "")
    if key in _SESSION_CACHE:
        return _SESSION_CACHE[key]

    import onnxruntime as ort
    from huggingface_hub import hf_hub_download

    kw = {}
    if endpoint:
        kw["endpoint"] = endpoint
    if download_root:
        kw["cache_dir"] = download_root

    log(f"强制对齐：加载本机 ONNX 模型（{REPO}）…")
    vocab_path = hf_hub_download(REPO, VOCAB_FILE, **kw)
    model_path = hf_hub_download(REPO, MODEL_FILE, **kw)

    vocab = json.loads(Path(vocab_path).read_text(encoding="utf-8"))
    blank_id = int(vocab.get("<blank>", vocab.get("<pad>", 0)))

    avail = set(ort.get_available_providers())
    providers = [p for p in ("CUDAExecutionProvider", "CoreMLExecutionProvider", "CPUExecutionProvider") if p in avail]
    if not providers:
        providers = ["CPUExecutionProvider"]
    opts = ort.SessionOptions()
    opts.log_severity_level = 3  # 仅输出错误，避免无 CUDA DLL 时刷屏警告
    sess = ort.InferenceSession(model_path, sess_options=opts, providers=providers)
    used = sess.get_providers()[0] if sess.get_providers() else "CPUExecutionProvider"
    log(f"强制对齐：模型就绪（{used}）")
    _SESSION_CACHE[key] = (sess, vocab, blank_id)
    return sess, vocab, blank_id


def text_to_tokens(text: str, language: str, vocab: dict[str, int]) -> list[int]:
    """将任意语言的句子转为 MMS 词表对应的字符 token id 序列（拉丁字母表示）。"""
    clean = _MUSIC_RE.sub("", (text or "").strip())
    if not clean:
        return []
    # 去掉常见括注（如 [笑い] / （笑））但保留正文
    clean = re.sub(r"[（(【\[][^）)】\]]{1,12}[）)】\]]", " ", clean).strip()
    if not clean:
        return []

    code = (language or "").lower().split("-")[0]
    rom = ""
    if code == "ja" or _KANA_RE.search(clean):
        try:
            ja = lang.get("ja")
            words = ja.annotate(clean)
            rom = ja.transliterate(words)
        except Exception:
            rom = ""
    elif code == "zh" or (_CJK_RE.search(clean) and not _KANA_RE.search(clean)):
        try:
            from pypinyin import lazy_pinyin

            rom = " ".join(lazy_pinyin(clean, errors="default"))
        except Exception:
            rom = ""

    if not rom:
        rom = clean

    # NFKD 分解并去除重音变音符（兼容法/德/西/葡等拉丁语系）
    normed = "".join(
        ch for ch in unicodedata.normalize("NFKD", rom.lower())
        if not unicodedata.combining(ch)
    )
    ids: list[int] = []
    for ch in normed:
        if ch.isspace():
            continue
        if ch in vocab:
            ids.append(vocab[ch])
    # 日语单字感叹（如「ん？」「あっ」）若转写后无有效拉丁字符，兜底映射基本假名音素
    if not ids and code == "ja":
        if "ん" in clean or "ン" in clean:
            if "n" in vocab:
                ids.append(vocab["n"])
    return ids


def ctc_viterbi_align(
    log_probs: np.ndarray,
    tokens: list[int],
    blank: int = 0,
    token_bounds: list[tuple[int, int]] | None = None,
    frame_dur: float = 0.02,
    band_margin_s: float = 3.0,
    band_slope: float = 0.4,
    band_cap: float = 2.5,
) -> list[tuple[int, int, float]]:
    """标准 / 软时间带 CTC Viterbi 强制对齐。

    输入：
    - log_probs: shape [T, V] 的逐帧 log-softmax 声学概率；
    - tokens: 长度为 L 的字符 token id 序列；
    - blank: CTC blank token id；
    - token_bounds（可选）: 长度为 L 的列表，每项为该字符所属句子的粗估帧区间 (lo_frame, hi_frame)；
      在 [lo - band_margin, hi + band_margin] 软时间带内 0 惩罚（纯声学精卡），
      超出时间带按超出秒数施加温和线性先验惩罚（仅作用于非 blank 字符态），防止多句多米诺串位。

    返回长度为 L 的列表，每项为 (start_frame, end_frame_exclusive, avg_log_prob)。
    若帧数不足或输入为空返回空列表。
    """
    T = int(log_probs.shape[0]) if log_probs.ndim == 2 else 0
    L = len(tokens)
    if L == 0 or T == 0 or T < L:
        return []

    S = 2 * L + 1
    ext = [blank if i % 2 == 0 else tokens[i // 2] for i in range(S)]
    ext_arr = np.array(ext, dtype=np.int32)

    # 预计算软时间带边界（仅非 blank 态 1, 3, 5... 生效）
    use_band = token_bounds is not None and len(token_bounds) == L and band_slope > 0
    if use_band:
        margin_f = band_margin_s / max(1e-4, frame_dur)
        lo_arr = np.full(S, -1e9, dtype=np.float32)
        hi_arr = np.full(S, 1e9, dtype=np.float32)
        for k, (lf, hf) in enumerate(token_bounds):
            st = 2 * k + 1
            lo_arr[st] = lf - margin_f
            hi_arr[st] = hf + margin_f
        scale = np.float32(band_slope * frame_dur)
        cap = np.float32(band_cap)

    neg_inf = np.float32(-1e9)
    dp = np.full((T, S), neg_inf, dtype=np.float32)
    bp = np.zeros((T, S), dtype=np.int32)

    dp[0, 0] = log_probs[0, blank]
    if S > 1:
        e0 = log_probs[0, ext[1]]
        if use_band:
            over0 = max(0.0, float(lo_arr[1])) + max(0.0, float(-hi_arr[1]))
            e0 = e0 - min(float(cap), float(scale) * over0)
        dp[0, 1] = e0

    can_skip = np.zeros(S, dtype=bool)
    for s in range(2, S):
        if ext[s] != blank and ext[s] != ext[s - 2]:
            can_skip[s] = True

    idx_arr = np.arange(S, dtype=np.int32)
    for t in range(1, T):
        emit = log_probs[t, ext_arr]
        if use_band:
            tf = np.float32(t)
            over = np.maximum(np.float32(0.0), lo_arr - tf) + np.maximum(np.float32(0.0), tf - hi_arr)
            emit = emit - np.minimum(cap, scale * over)
        prev = dp[t - 1]

        best = prev.copy()
        src = idx_arr.copy()

        cand1 = np.empty_like(prev)
        cand1[0] = neg_inf
        cand1[1:] = prev[:-1]
        m1 = cand1 > best
        best[m1] = cand1[m1]
        src[m1] = idx_arr[m1] - 1

        cand2 = np.full_like(prev, neg_inf)
        cand2[2:] = np.where(can_skip[2:], prev[:-2], neg_inf)
        m2 = cand2 > best
        best[m2] = cand2[m2]
        src[m2] = idx_arr[m2] - 2

        dp[t] = best + emit
        bp[t] = src

    s = S - 1 if dp[T - 1, S - 1] >= dp[T - 1, S - 2] else S - 2
    path = [0] * T
    for t in range(T - 1, -1, -1):
        path[t] = s
        s = int(bp[t, s])

    spans: list[tuple[int, int, float]] = []
    # 单遍扫描 path 收集每个 token 对应的帧范围（O(T + L)）
    first_f = [-1] * L
    last_f = [-1] * L
    sum_lp = [0.0] * L
    cnt_f = [0] * L
    for t, ps in enumerate(path):
        if ps % 2 == 1:
            k = ps // 2
            if first_f[k] < 0:
                first_f[k] = t
            last_f[k] = t + 1
            sum_lp[k] += float(log_probs[t, tokens[k]])
            cnt_f[k] += 1

    for k in range(L):
        if cnt_f[k] > 0:
            spans.append((first_f[k], last_f[k], sum_lp[k] / cnt_f[k]))
        else:
            spans.append((-1, -1, -99.0))
    return spans


def _read_wav_slice(wav_path: str | Path, start_s: float = 0.0, end_s: float | None = None) -> tuple[np.ndarray, int, float]:
    """读取单声道 16-bit WAV 的 [start_s, end_s] 区间并转为 float32 [-1, 1]。"""
    with wave.open(str(wav_path), "rb") as w:
        sr = w.getframerate()
        n_channels = w.getnchannels()
        n_frames = w.getnframes()
        total_s = n_frames / max(1, sr)
        f0 = max(0, min(n_frames, int(round(start_s * sr))))
        f1 = n_frames if end_s is None else max(f0, min(n_frames, int(round(end_s * sr))))
        w.setpos(f0)
        raw = np.frombuffer(w.readframes(f1 - f0), dtype=np.int16).astype(np.float32) / 32768.0
        if n_channels > 1 and len(raw) >= n_channels:
            raw = raw.reshape(-1, n_channels).mean(axis=1)
        if sr != TARGET_SR and len(raw) > 1:
            new_len = int(round(len(raw) * TARGET_SR / sr))
            raw = np.interp(np.linspace(0, len(raw) - 1, new_len), np.arange(len(raw)), raw).astype(np.float32)
            sr = TARGET_SR
        return raw, sr, f0 / max(1, w.getframerate()) if n_frames else 0.0


def compute_emissions(
    wav_path: str | Path,
    c: dict | None = None,
    start_s: float = 0.0,
    end_s: float | None = None,
    report_progress: bool = True,
) -> tuple[np.ndarray, float, float, float, dict[str, int], int]:
    """对 wav_path 的 [start_s, end_s] 做 30s 分块 ONNX 推理，返回 (log_probs, frame_dur, actual_start, dur, vocab, blank_id)。"""
    sess, vocab, blank_id = _load_model(c)
    inp_name = sess.get_inputs()[0].name
    samples, sr, actual_start = _read_wav_slice(wav_path, start_s, end_s)
    total_samples = len(samples)
    if total_samples < sr // 4:
        return np.zeros((0, len(vocab)), dtype=np.float32), 0.02, actual_start, 0.0, vocab, blank_id

    chunk_samples = sr * CHUNK_SECONDS
    emits = []
    done = 0
    if report_progress:
        progress(0, total_samples / sr, "强制对齐")
    for s0 in range(0, total_samples, chunk_samples):
        check_cancel()
        seg = samples[s0: s0 + chunk_samples]
        if len(seg) < sr // 4:
            # 末尾不足 0.25s 时右补零，避免 Conv1D 报错
            seg = np.pad(seg, (0, sr // 4 - len(seg)))
        seg = (seg - seg.mean()) / (seg.std() + np.float32(1e-7))
        logits = sess.run(None, {inp_name: seg[None, :].astype(np.float32)})[0][0]  # [T, V]
        mx = logits.max(axis=-1, keepdims=True)
        lsm = logits - mx - np.log(np.exp(logits - mx).sum(axis=-1, keepdims=True))
        emits.append(lsm.astype(np.float32))
        done = min(total_samples, s0 + chunk_samples)
        if report_progress:
            progress(done / sr, total_samples / sr, "强制对齐")

    log_probs = np.concatenate(emits, axis=0)
    dur = total_samples / sr
    frame_dur = dur / max(1, len(log_probs))
    return log_probs, frame_dur, actual_start, dur, vocab, blank_id


def refine_with_vocal_envelope(segs: list[dict], w, offset: float = 0.0) -> list[dict]:
    """用人声轨 50ms 能量包络修正 CTC 的三类物理边界问题（100% 忠实于真实声音，不加阅读缓冲）：
    1. 句首串尾裁剪：若句内前 <0.5s 有一小段声音，紧接着 >=0.55s 纯静音，后面才是主体声音 → 把起点移到静音后真正开口处；
    2. 句尾吸噪截断：若主体声音（>=0.25s）之后出现 >=0.65s 纯静音，末尾仅剩 <0.45s 孤立杂音峰 → 截断在静音前真正说完处；
    3. 长音/拖音延伸：若 CTC 给出的句尾时刻人声能量仍在持续（常见于唱歌长音/拖尾元音），沿连续人声能量向后延伸到声音真正落下（不越过下一句 - 0.05s）。
    """
    from .asr_api import HOP

    if not segs or len(w.db) < 4:
        return segs

    base_thr = max(-45.0, w.floor + 11.0)
    n_frames = len(w.db)
    out = [dict(s) for s in segs]

    for idx, s in enumerate(out):
        a_rel = max(0.0, s["start"] - offset)
        b_rel = max(a_rel, s["end"] - offset)
        if b_rel - a_rel < 0.08:
            continue
        i0, j0 = w._range(a_rel, b_rel)
        if j0 <= i0:
            continue
        seg_db = w.db[i0:j0]
        peak_db = float(np.percentile(seg_db, 90))
        seg_thr = max(base_thr, peak_db - 18.0)
        # 长音延伸阈值取 peak - 17dB（兼顾唱歌长音尾音衰减与屏蔽 -22dB 级背景混响）
        tail_thr = max(max(-42.0, w.floor + 13.0), peak_db - 17.0)

        # 找出段内连续的活跃人声区间 [(f_start, f_end)]（相对 i0）
        active = seg_db > seg_thr
        runs: list[tuple[int, int]] = []
        r_start = None
        for k, flag in enumerate(active):
            if flag and r_start is None:
                r_start = k
            elif not flag and r_start is not None:
                runs.append((r_start, k))
                r_start = None
        if r_start is not None:
            runs.append((r_start, len(active)))

        na, nb = a_rel, b_rel
        if len(runs) >= 2:
            # 1) 句首串尾裁剪：第一段很短（<0.5s），紧跟着 >=0.55s 静音，且后面主体 >=0.25s
            first_dur = (runs[0][1] - runs[0][0]) * HOP
            gap_after_first = (runs[1][0] - runs[0][1]) * HOP
            rest_active = sum((r[1] - r[0]) * HOP for r in runs[1:])
            if first_dur < 0.5 and gap_after_first >= 0.55 and rest_active >= 0.25:
                na = round((i0 + runs[1][0]) * HOP, 3)
                runs = runs[1:]

        if len(runs) >= 2:
            # 2) 句尾吸噪截断：仅截断“极短（<0.25s）且响度明显弱于主声（< peak - 12dB）”的真正尾噪；
            # 避免误伤停顿后清晰说出的短词（如「浜路…荘介…よしろ」里停顿 1.5s 后清晰说出的 0.4s「よしろ」）
            last_dur = (runs[-1][1] - runs[-1][0]) * HOP
            gap_before_last = (runs[-1][0] - runs[-2][1]) * HOP
            prev_active = sum((r[1] - r[0]) * HOP for r in runs[:-1])
            last_peak = float(np.max(seg_db[runs[-1][0]: runs[-1][1]]))
            if last_dur < 0.25 and last_peak < peak_db - 12.0 and gap_before_last >= 0.65 and prev_active >= 0.25:
                nb = round((i0 + runs[-2][1]) * HOP, 3)
                runs = runs[:-1]

        # 3) 句尾长音/拖音延伸：仅当没有刚截断过句尾噪点，且当前 nb 处人声能量仍在持续
        if nb == b_rel and runs:
            end_f = min(n_frames - 1, int(round(nb / HOP)))
            # 检查 nb 附近（前后 1 帧）是否仍有高于 tail_thr 的人声能量
            local_end_db = float(np.max(w.db[max(0, end_f - 1): min(n_frames, end_f + 2)]))
            if local_end_db > tail_thr:
                next_start_rel = (out[idx + 1]["start"] - offset - 0.05) if idx + 1 < len(out) else (n_frames * HOP)
                max_end_f = min(n_frames, int(round(min(next_start_rel, nb + 3.5) / HOP)))
                quiet_streak = 0
                last_loud_f = end_f
                for f in range(end_f, max_end_f):
                    if w.db[f] > tail_thr:
                        last_loud_f = f + 1
                        quiet_streak = 0
                    else:
                        quiet_streak += 1
                        if quiet_streak >= 2:  # 连续 >= 100ms 低于主声阈值则认为真正唱/说完
                            break
                ext_b = round(last_loud_f * HOP, 3)
                if ext_b > nb + 0.05:
                    nb = min(ext_b, round(next_start_rel, 3)) if idx + 1 < len(out) else ext_b

        if nb > na + 0.08:
            s["start"] = round(na + offset, 3)
            s["end"] = round(nb + offset, 3)

    # 保证单调不重叠：仅当后一句确实在当前句之后开始（> start + 0.15s）才裁剪，防止乱序/同时说话条目把前一句压扁成 0.08s
    for i in range(len(out) - 1):
        if out[i + 1]["start"] > out[i]["start"] + 0.15 and out[i]["end"] > out[i + 1]["start"]:
            mid = round(max(out[i]["start"] + 0.15, out[i + 1]["start"] - 0.02), 3)
            out[i]["end"] = mid
    return out


def _trim_outlier_spans(valid: list[tuple[int, int, float]], frame_dur: float, max_gap_s: float = 1.5) -> list[tuple[int, int, float]]:
    """剔除单句内跨越超长静音的孤立句首/句尾离群音素（常为边缘少量音素吸到几秒外的底噪/前奏/水声）。
    - 1.5~2.5s 空档：仅当边缘离群音素 <= 2 个且占比 <= 20% 时裁剪；
    - > 2.5s 巨大空档：允许裁剪占比 <= 25% 的边缘离群音素簇（如句首 3~5 字符吸到 8 秒前环境声）。
    这样既彻底消灭跨静音吸噪，又绝不误伤句中正常多词停顿（每段占比通常 > 30%）。"""
    if len(valid) < 4:
        return valid
    lo, hi = 0, len(valid)
    max_small = min(2, max(1, len(valid) // 5))
    max_large = max(max_small, len(valid) // 4)
    # 句首离群音素检查
    for k in range(1, max_large + 1):
        if lo + k < hi:
            gap_s = (valid[lo + k][0] - valid[lo + k - 1][1]) * frame_dur
            if (k <= max_small and gap_s > max_gap_s) or gap_s > 2.5:
                lo = lo + k
                break
    # 句尾离群音素检查
    for k in range(1, max_large + 1):
        if hi - k > lo:
            gap_s = (valid[hi - k][0] - valid[hi - k - 1][1]) * frame_dur
            if (k <= max_small and gap_s > max_gap_s) or gap_s > 2.5:
                hi = hi - k
                break
    return valid[lo:hi] if hi > lo else valid


def _group_windows(segs: list[dict], max_gap: float = 6.0, max_span: float = 120.0, max_count: int = 35) -> list[list[int]]:
    """把语音段按自然停顿与时长上限切分为对齐窗口（返回索引列表）。"""
    groups: list[list[int]] = []
    cur: list[int] = []
    for i, s in enumerate(segs):
        if cur:
            prev = segs[cur[-1]]
            first = segs[cur[0]]
            if (s["start"] - prev["end"] > max_gap
                    or s["end"] - first["start"] > max_span
                    or len(cur) >= max_count):
                groups.append(cur)
                cur = []
        cur.append(i)
    if cur:
        groups.append(cur)
    return groups


def _get_text(item: dict) -> str:
    if "text" in item:
        return item["text"] or ""
    return ((item.get("orig") or {}).get("text")) or ""


def align_segments(
    segs: list[dict],
    wav_path: str | Path,
    language: str = "",
    c: dict | None = None,
    offset: float = 0.0,
    chunk_spans: list[tuple[float, float]] | None = None,
) -> list[dict]:
    """对整集 ASR 段落做本机 ONNX CTC 强制对齐 + 离群音素剔除 + 人声能量包络精修。
    - 若传入 chunk_spans（每块上传给大模型的真实物理区间 [(a, b), ...]），直接按物理块做整块联合 CTC 对齐，
      彻底不依赖大模型自身可能漂移 10~30 秒的估时；
    - 未传 chunk_spans 时，按自然停顿分组并允许在相邻组可用空档内对齐。"""
    if not segs:
        return segs

    from .asr_api import Wav

    log_probs, frame_dur, actual_start, total_dur, vocab, blank_id = compute_emissions(
        wav_path, c=c, report_progress=True
    )
    if len(log_probs) == 0:
        return segs

    out = [dict(s) for s in segs]
    aligned_n = 0

    if chunk_spans:
        # 按上传给大模型的真实音频块 [a, b] 分组（每条 seg 带 _chunk 索引，或按中点落入对应块）
        by_chunk: dict[int, list[int]] = {}
        for i, s in enumerate(out):
            ci = s.get("_chunk")
            if ci is None or not (0 <= ci < len(chunk_spans)):
                mid = (s["start"] + s["end"]) / 2.0 - offset
                ci = min(range(len(chunk_spans)), key=lambda k: 0.0 if chunk_spans[k][0] <= mid <= chunk_spans[k][1]
                         else min(abs(mid - chunk_spans[k][0]), abs(mid - chunk_spans[k][1])))
            by_chunk.setdefault(ci, []).append(i)
        windows = [(chunk_spans[ci][0], chunk_spans[ci][1], by_chunk[ci]) for ci in sorted(by_chunk)]
    else:
        groups = _group_windows(out)
        windows = []
        for gi, g in enumerate(groups):
            min_s = min(out[i]["start"] for i in g) - offset
            max_e = max(out[i]["end"] for i in g) - offset
            lo = max(actual_start, min_s - 16.0)
            hi = min(actual_start + total_dur, max(max_e + 16.0, lo + 1.0))
            if gi + 1 < len(groups):
                next_min_s = min(out[i]["start"] for i in groups[gi + 1]) - offset
                if next_min_s > max_e + 1.0:
                    hi = min(hi, next_min_s)
            windows.append((lo, hi, g))

    prev_win_end = actual_start
    for lo_raw, hi_raw, g in windows:
        check_cancel()
        lo = max(actual_start, prev_win_end, lo_raw)
        hi = min(actual_start + total_dur, max(hi_raw, lo + 1.0))
        f0 = max(0, int((lo - actual_start) / frame_dur))
        f1 = min(len(log_probs), int((hi - actual_start) / frame_dur))
        if f1 <= f0:
            continue

        flat_tokens: list[int] = []
        ranges: list[tuple[int, int, int]] = []
        for idx in g:
            toks = text_to_tokens(_get_text(out[idx]), language, vocab)
            a_i = len(flat_tokens)
            flat_tokens.extend(toks)
            ranges.append((idx, a_i, len(flat_tokens)))

        if not flat_tokens or f1 - f0 < len(flat_tokens):
            continue

        spans = ctc_viterbi_align(log_probs[f0:f1], flat_tokens, blank=blank_id)
        if not spans:
            continue

        win_last_end = prev_win_end
        for idx, a_i, b_i in ranges:
            if b_i <= a_i:
                continue
            valid = [sp for sp in spans[a_i:b_i] if sp[0] >= 0]
            if not valid:
                continue
            valid = _trim_outlier_spans(valid, frame_dur)
            ns_rel = actual_start + (f0 + valid[0][0]) * frame_dur
            ne_rel = actual_start + (f0 + valid[-1][1]) * frame_dur
            if ne_rel > ns_rel + 0.05:
                out[idx]["start"] = round(ns_rel + offset, 3)
                out[idx]["end"] = round(ne_rel + offset, 3)
                out[idx]["timing"] = "aligned"
                aligned_n += 1
                win_last_end = max(win_last_end, ne_rel)
        prev_win_end = win_last_end

    for s in out:
        s.pop("_chunk", None)
    w = Wav(wav_path)
    out = refine_with_vocal_envelope(out, w, offset=offset)
    log(f"强制对齐完成：{aligned_n}/{len(out)} 段已卡准物理声学时间")
    return out


def align_subset(
    items: list[dict],
    wav_path: str | Path,
    language: str = "",
    c: dict | None = None,
    offset: float = 0.0,
    min_start_s: float | None = None,
    max_end_s: float | None = None,
) -> int:
    """对一小段（如一首歌的官方标准歌词 cues）就地做二次声学强制对齐。
    只切取该段前后安全区间跑推理（约 1~2 秒），不跑整集。
    min_start_s / max_end_s（可选）：音频绝对时间上下界（前后相邻非歌曲句边界），防止侵入前后对话。"""
    if not items or not wav_path or not Path(wav_path).exists():
        return 0

    from .asr_api import Wav

    min_s = max(0.0, min(it["start"] for it in items) - offset - 4.0)
    if min_start_s is not None:
        min_s = max(min_s, min_start_s - offset)
    max_e = max(it["end"] for it in items) - offset + 4.0
    if max_end_s is not None:
        max_e = min(max_e, max(min_s + 0.5, max_end_s - offset))
    log_probs, frame_dur, actual_start, total_dur, vocab, blank_id = compute_emissions(
        wav_path, c=c, start_s=min_s, end_s=max_e, report_progress=False
    )
    if len(log_probs) == 0:
        return 0

    flat_tokens: list[int] = []
    ranges: list[tuple[int, int, int]] = []
    for idx, it in enumerate(items):
        toks = text_to_tokens(_get_text(it), language, vocab)
        a_i = len(flat_tokens)
        flat_tokens.extend(toks)
        ranges.append((idx, a_i, len(flat_tokens)))

    if not flat_tokens or len(log_probs) < len(flat_tokens):
        return 0

    spans = ctc_viterbi_align(log_probs, flat_tokens, blank=blank_id)
    if not spans:
        return 0

    aligned_n = 0
    for idx, a_i, b_i in ranges:
        if b_i <= a_i:
            continue
        valid = [sp for sp in spans[a_i:b_i] if sp[0] >= 0]
        if not valid:
            continue
        valid = _trim_outlier_spans(valid, frame_dur)
        ns = actual_start + valid[0][0] * frame_dur + offset
        ne = actual_start + valid[-1][1] * frame_dur + offset
        if ne > ns + 0.05:
            items[idx]["start"] = round(ns, 3)
            items[idx]["end"] = round(ne, 3)
            items[idx]["timing"] = "aligned"
            aligned_n += 1

    if aligned_n:
        w = Wav(wav_path)
        refined = refine_with_vocal_envelope(items, w, offset=offset)
        for it, ref in zip(items, refined):
            it["start"] = ref["start"]
            it["end"] = ref["end"]
        if max_end_s is not None and items[-1]["end"] > max_end_s:
            items[-1]["end"] = round(max(items[-1]["start"] + 0.2, max_end_s), 3)
    return aligned_n
