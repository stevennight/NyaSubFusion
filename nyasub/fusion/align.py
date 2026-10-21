"""字幕与语音对时：用语音识别的逐词时间估计字幕整体偏移。"""
import bisect

from rapidfuzz import fuzz

from ..textutil import norm


def estimate_offset(subs, asr, max_shift=10.0):
    """返回 (offset 秒, 改善比例)；字幕时间要加上 offset。比对不出来时返回 (0, 0)。"""
    words = sorted(((w["s"] + w["e"]) / 2, norm(w["w"])) for a in asr for w in a.get("words", []))
    if len(words) < 50:  # 没有逐字时间（云端接口只给逐句时间）：把每句的字均匀摊在这句的时间里，估整体偏移够用
        words = sorted((a["start"] + (a["end"] - a["start"]) * (k + 0.5) / len(t), ch)
                       for a in asr if (t := norm(a.get("text", ""))) for k, ch in enumerate(t))
    if len(words) < 50:
        return 0.0, 0.0
    mids = [m for m, _ in words]
    cues = [(c["start"], c["end"], norm(c["text"])) for c in subs]
    cues = [c for c in cues if len(c[2]) >= 4]
    if len(cues) < 10:
        return 0.0, 0.0
    if len(cues) > 400:
        cues = cues[:: len(cues) // 400 + 1]

    def score(off):
        total = 0.0
        for s, e, n in cues:
            i, j = bisect.bisect_left(mids, s + off), bisect.bisect_right(mids, e + off)
            if j > i:
                total += fuzz.ratio(n, "".join(w for _, w in words[i:j])) * len(n)
        return total

    coarse = [x * 0.5 for x in range(int(-max_shift * 2), int(max_shift * 2) + 1)]
    best = max(coarse, key=score)
    fine = [round(best + x * 0.1, 2) for x in range(-6, 7)]
    best = max(fine, key=score)
    base, top = score(0.0), score(best)
    return best, (top - base) / max(base, 1.0)


def apply_offset(subs, off):
    return [dict(c, start=round(c["start"] + off, 3), end=round(c["end"] + off, 3)) for c in subs]
