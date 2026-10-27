"""与语言无关的文本归一化、相似度、时间工具。"""
import re
import unicodedata
from difflib import SequenceMatcher

from rapidfuzz import fuzz

_PUNCT = re.compile(r"[\s　、。，．,.!！?？・･「」『』（）()\[\]【】〈〉《》≪≫<>\"'“”‘’…‥~〜ー―\-:：;；/／♪♬→←↑↓＞＜]+")


def _kata_to_hira(s):
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def norm(text):
    """比较用：NFKC、去标点空白、片假名转平假名、小写。"""
    s = unicodedata.normalize("NFKC", text or "")
    s = _PUNCT.sub("", s)
    return _kata_to_hira(s).lower()


def sim(a, b):
    """0~100。用 partial_ratio：一方是另一方的一部分时也算相似（半句字幕、半句画面字）。"""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if len(na) < 2 or len(nb) < 2:
        return 100.0 if na == nb else 0.0
    return fuzz.partial_ratio(na, nb)


def full_sim(a, b):
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    return fuzz.ratio(na, nb)


def covered(text, by):
    """text 有多少比例的字出现在 by 里（按至少两个字连续相同的片段算）。"""
    a, b = norm(text), norm(by)
    if not a:
        return 1.0
    if len(a) <= 2:  # 一两个字（“桶”）：直接看在不在里面
        return 1.0 if a in b else 0.0
    blocks = SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks()
    return sum(x.size for x in blocks if x.size >= 2) / len(a)


def overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def fmt_time(t):
    t = max(0.0, t)
    m, s = divmod(t, 60)
    return f"{int(m):02d}:{s:05.2f}"
