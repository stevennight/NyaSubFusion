import re

from .base import LanguagePack

_MID = re.compile(r"[，。；]+")
_TAIL = re.compile(r"[，。；、\s　]+$")


class Chinese(LanguagePack):
    """简体中文。选项：comma_to_space（句中逗号句号换成空格，默认开）、line_chars。"""
    code = "zh-Hans"
    name = "简体中文"
    whisper = "zh"
    ocr_lang = "ch"
    line_chars = 16
    cps = 9
    hallucinations = ("请不吝点赞", "订阅", "字幕由", "谢谢观看")
    meaningful = re.compile(r"[㐀-鿿]")
    break_after = set("　 ，。、；：？！…」』）】》”")
    no_line_start = set("，。、；：？！…」』）】》”")

    def prompt_target_rules(self, opts):
        n = int(opts.get("line_chars") or self.line_chars)
        punct = ("句末不加句号、逗号；句中的逗号、句号用一个空格代替；问号、感叹号、省略号、引号、书名号照常保留。"
                 if opts.get("comma_to_space", True) else "句末不加句号、逗号；句中标点照常保留。")
        return (f"字幕长度：中文每行不超过 {n} 个字，每条最多 2 行（行之间用换行符，JSON 里写作 \\n）。"
                "一句话太长、两行放不下时，用“｜”把译文分成几段，每段不超过 2 行；原文也用“｜”分成同样多段、内容一一对应。"
                f"中文字幕标点规范：{punct}方言台词译成自然的口语，不要加注释；旁白译得书面、流畅一些。")

    def display(self, text, opts):
        comma_to_space = opts.get("comma_to_space", True)
        lines = []
        for line in (text or "").replace("\\N", "\n").split("\n"):
            line = _TAIL.sub("", line.strip())
            line = _MID.sub("　", line) if comma_to_space else re.sub(r"[。]+", "　", line)
            line = re.sub(r"[　 ]{2,}", "　", line).strip("　 ")
            if line:
                lines.append(line)
        return "\n".join(lines)


class ChineseTraditional(Chinese):
    code = "zh-Hant"
    name = "繁体中文"
    ocr_lang = "chinese_cht"
