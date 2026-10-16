import re

from .base import LanguagePack


class English(LanguagePack):
    code = "en"
    name = "英语"
    whisper = "en"
    ocr_lang = "en"
    line_chars = 42
    cps = 17
    word_based = True
    hallucinations = ("Thanks for watching", "Please subscribe", "Subtitles by")
    meaningful = re.compile(r"[A-Za-z]{2,}")


class Korean(LanguagePack):
    code = "ko"
    name = "韩语"
    whisper = "ko"
    ocr_lang = "korean"
    line_chars = 20
    cps = 10
    word_based = True
    meaningful = re.compile(r"[가-힣]")
