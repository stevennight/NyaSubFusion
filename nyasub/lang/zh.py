import re

from . import ruby
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


    # ---------- 注音 ----------
    annotations = ("pinyin", "zhuyin")
    translit_name = "整行拼音"

    def annotate(self, text, readings=()):
        """逐字给拼音（pypinyin 按词组判断多音字）。readings：[{"src": "重庆", "reading": "chóng qìng"}]。"""
        from pypinyin import Style, load_phrases_dict, pinyin

        custom = {r["src"]: [[s] for s in r["reading"].split()] for r in readings
                  if r.get("src") and r.get("reading") and len(r["reading"].split()) == len(r["src"])}
        if custom:
            load_phrases_dict(custom)
        hans = [c for c in text if ruby.is_kanji(c)]
        toned = [x[0] for x in pinyin(text, style=Style.TONE, errors="ignore")]
        bopo = [x[0] for x in pinyin(text, style=Style.BOPOMOFO, errors="ignore")]
        if len(toned) != len(hans) or len(bopo) != len(hans):  # 对不齐时逐字查
            toned = [pinyin(c, style=Style.TONE)[0][0] for c in hans]
            bopo = [pinyin(c, style=Style.BOPOMOFO)[0][0] for c in hans]
        guessed = {c for r in readings for c in r.get("src", "")}
        words, k, run = [], 0, ""
        for c in text:
            if ruby.is_kanji(c):
                if run:
                    words.append(_plain(run))
                    run = ""
                words.append({"t": c, "segs": [[c, toned[k], bopo[k]]], "pos": "han", "p": False, "num": False,
                              "src": "dict" if c in guessed else "auto", "guess": False})
                k += 1
            else:
                run += c
        if run:
            words.append(_plain(run))
        return words

    def ruby_visible(self, word, cfg, first):
        return len(word["segs"][0]) > 1

    def ruby_text(self, seg, cfg):
        if cfg.kind == "zhuyin":
            z = seg[2]
            return "˙" + z[:-1] if z.endswith("˙") else z  # 轻声点写在前面
        return ruby.tone_style(seg[1], cfg.tone)

    def transliterate(self, words, tone="mark"):
        out = ""
        for w in words:
            seg = w["segs"][0]
            if len(seg) > 1:
                out += ("" if not out or out.endswith((" ", "(", "“")) else " ") + ruby.tone_style(seg[1], tone)
            else:
                t = "".join(_PUN.get(c, c) for c in w["t"])
                out = out.rstrip() + t if t.strip()[:1] in ",.?!;:)”" else out + (" " if out and t.strip() else "") + t
        return re.sub(r"\s+", " ", out).strip()


_PUN = {"，": ",", "。": ".", "、": ",", "？": "?", "！": "!", "；": ";", "：": ":", "（": "(", "）": ")", "　": " ",
        "…": "..."}


def _plain(text):
    return {"t": text, "segs": [[text]], "pos": "other", "p": False, "num": text.isdigit(), "src": "auto", "guess": False}


class ChineseTraditional(Chinese):
    code = "zh-Hant"
    name = "繁体中文"
    ocr_lang = "chinese_cht"
