"""一条字幕里明确是两个人说的话（字幕的颜色 / 位置 / 说话人标记看得出来，或者大模型判断出来的）：
拆成各自独立的字幕，按说话的先后各占一段时间。分界用语音识别的逐字时间找第二个人开口的时刻，对不上才按字数估。"""
from difflib import SequenceMatcher

from ..textutil import norm

MIN_PART = 0.5  # 拆出来的每条至少显示这么久


def _heard(segs, t0, t1):
    """这段时间里语音识别听到的字：[(规范化后的字, 开始时间)]。"""
    out = []
    for s in segs:
        if s["end"] < t0 or s["start"] > t1:
            continue
        for w in s.get("words") or []:
            if t0 <= w["s"] <= t1:
                out += [(ch, w["s"]) for ch in norm(w["w"])]
    return out


def speech_starts(parts, start, end, segs):
    """每一部分从什么时候开始（第一部分就是整条的开始）：[开始时间...]、有几个分界对上了语音。"""
    heard = _heard(segs, start - 1.0, end + 1.0)
    text = "".join(ch for ch, _ in heard)
    total = sum(len(norm(p)) for p in parts) or 1

    def find(n, pos):
        """n 在听到的字里（pos 以后）第一次连着对上两个字以上的地方：(在 text 里的位置, 对上的长度)；没有返回 None。
        只在 pos 以后找：两个人说一样的话（“生きててよかった”各说一遍）时，后一个人的不能对到前一个人说的上。"""
        if not text or not n or pos >= len(text):
            return None
        sm = SequenceMatcher(None, text[pos:], n, autojunk=False)
        b = next((b for b in sm.get_matching_blocks() if b.size >= min(2, len(n))), None)
        return (pos + b.a, b.size) if b else None

    first = find(norm(parts[0]), 0)
    out, done, pos, matched = [start], len(norm(parts[0])), (first[0] + first[1]) if first else 0, 0
    for p in parts[1:]:
        t, n = None, norm(p)
        hit = find(n, pos)
        if hit:
            t, pos = heard[hit[0]][1], hit[0] + hit[1]
        if t is None or not (out[-1] + MIN_PART <= t <= end - MIN_PART):  # 对不上：按字数估
            t = start + (end - start) * done / total
        else:
            matched += 1
        out.append(round(t, 3))
        done += len(n)
    return out, matched


def split_speakers(subs, segs):
    """带 voices（各行是谁说的）且不止一个人的字幕，拆成各自独立的字幕：c470 → c470、c470b。
    拆不开（时间太短，每条不够 MIN_PART 秒）的保持原样。"""
    out = []
    for c in subs:
        vs, lines = c.get("voices"), c["text"].split("\n")
        if not vs or len(vs) != len(lines) or len(set(vs)) < 2:
            out.append({k: v for k, v in c.items() if k not in ("voices", "speaker_check")})
            continue
        parts, cur = [], None
        for v, line in zip(vs, lines):
            if parts and v == cur:
                parts[-1] += "\n" + line
            else:
                parts.append(line)
            cur = v
        starts, _ = speech_starts(parts, c["start"], c["end"], segs or [])
        ends = starts[1:] + [c["end"]]
        if any(b - a < MIN_PART for a, b in zip(starts, ends)):
            out.append({k: v for k, v in c.items() if k not in ("voices", "speaker_check")})
            continue
        base = {k: v for k, v in c.items() if k not in ("voices", "speaker_check", "id", "start", "end", "text")}
        for k, (p, a, b) in enumerate(zip(parts, starts, ends)):
            out.append({**base, "id": c["id"] + ("" if k == 0 else chr(ord("a") + k)), "start": a, "end": b,
                        "text": p, "split_from": c["id"]})
    return out
