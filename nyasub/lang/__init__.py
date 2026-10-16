"""语言包注册表。"""
import re

from .base import LanguagePack
from .en import English, Korean
from .ja import Japanese
from .zh import Chinese, ChineseTraditional

_PACKS = {p.code: p() for p in (Japanese, Chinese, ChineseTraditional, English, Korean)}
_ALIASES = {"zh": "zh-Hans", "zh-cn": "zh-Hans", "zh-hans": "zh-Hans", "zh-sg": "zh-Hans",
            "zh-tw": "zh-Hant", "zh-hk": "zh-Hant", "zh-hant": "zh-Hant", "jp": "ja", "jpn": "ja",
            "eng": "en", "kor": "ko", "chi": "zh-Hans", "zho": "zh-Hans"}


class Generic(LanguagePack):
    """没有专门语言包的语言：按空格断行，不做特殊处理。"""
    word_based = True

    def __init__(self, code):
        self.code = code
        self.name = code
        self.whisper = code.split("-")[0].lower() or None


def canonical(code: str) -> str:
    c = (code or "").strip()
    return _ALIASES.get(c.lower(), c)


def get(code: str) -> LanguagePack:
    c = canonical(code)
    return _PACKS.get(c) or Generic(c)


def available() -> list[LanguagePack]:
    return list(_PACKS.values())


_KANA = re.compile(r"[぀-ヿ]")
_HANGUL = re.compile(r"[가-힣]")
_HAN = re.compile(r"[㐀-鿿]")
_LATIN = re.compile(r"[A-Za-z]")


def detect(text: str) -> str | None:
    """按文字粗略判断语言，只用来在没有语音识别时猜源语言。"""
    if not text:
        return None
    kana, hangul, han, latin = (len(r.findall(text)) for r in (_KANA, _HANGUL, _HAN, _LATIN))
    total = kana + hangul + han + latin
    if not total:
        return None
    if kana >= 0.05 * total:
        return "ja"
    if hangul >= 0.3 * total:
        return "ko"
    if han >= 0.3 * total:
        return "zh-Hans"
    return "en" if latin else None
