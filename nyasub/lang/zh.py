import re
from functools import lru_cache

from . import ruby
from .base import LINE_RULES, LanguagePack

_MID = re.compile(r"[，。；]+")
_TAIL = re.compile(r"[，。；、\s　]+$")
# 小句里的自然停顿：这些词之后（“鸭子飞向觅食地的 / 黄昏时分”）、这些词之前（“… / 但是 …”）断开读起来最顺。
# _ATTACHED 里的助词粘着前面的词，它们前面不断（不让一行以“的”“了”开头）
_PAUSE_AFTER = {"吗", "呢", "吧", "啊", "呀", "嘛"}
# 粘着前面的词、前面不断，但后面常接着名词 / 宾语，断开会拆开一个短语（“喜欢的 / 人”“盯着 / 我的家伙”），只算普通的词边界
_ATTACHED = _PAUSE_AFTER | {"的", "地", "得", "了", "着", "过"}
# 只收明确的连词：单字的介词 / “是”常常和前面的字连成一个词（“飞向”“只在”“这就是”），不能当停顿
_PAUSE_BEFORE = {"但", "但是", "而", "而且", "可是", "却", "所以", "因为", "如果", "虽然", "然后", "并且", "或者", "还是"}


_LEARNED = set()  # 已经告诉过 jieba 的词


@lru_cache(maxsize=4096)
def _words(text):
    """中文分词（jieba）；没装 jieba 就返回 None（只在空格 / 标点处断行）。"""
    import warnings

    try:
        with warnings.catch_warnings():  # jieba 源码里有老式正则转义，导入时会报 SyntaxWarning
            warnings.simplefilter("ignore")
            import jieba
    except ImportError:
        return None
    jieba.setLogLevel(60)  # 第一次加载词典时不往日志里打进度
    return tuple(jieba.lcut(text, HMM=True))


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
    no_line_end = set("“‘（「『《【〈〔")

    def learn_words(self, words):
        """术语表里的译名告诉 jieba（“加贺市”“差间正树”），折行时不拆开；加了新词就清掉分词缓存。"""
        new = {w.strip() for w in words if w and len(w.strip()) >= 2} - _LEARNED
        if not new or _words("") is None:
            return
        import jieba

        for w in new:
            jieba.add_word(w)
        _LEARNED.update(new)
        _words.cache_clear()
        self.generation += 1  # 分词变了：按文字记住的注音结果作废（core/pipeline._ANN_CACHE）

    def break_levels(self, text):
        """空格 / 标点后（3）之外，按分词补上词与词之间（1），助词之后、连词之前算小句里的停顿（2）。"""
        out = super().break_levels(text)
        words = _words(text)
        if not words:
            return out
        pos = 0
        for i, w in enumerate(words[:-1]):
            pos += len(w)
            if words[i + 1] in _ATTACHED:  # 助词前面不断
                if out.get(pos, 0) < 3:
                    out.pop(pos, None)
                continue
            pause = w in _PAUSE_AFTER or words[i + 1] in _PAUSE_BEFORE
            out[pos] = max(out.get(pos, 0), 2 if pause else 1)
        return out

    def prompt_target_rules(self, opts):
        n = int(opts.get("line_chars") or self.line_chars)
        punct = ("句末不加句号、逗号；句中的逗号、句号用一个空格代替；问号、感叹号、省略号、引号、书名号照常保留。"
                 if opts.get("comma_to_space", True) else "句末不加句号、逗号；句中标点照常保留。")
        return (f"字幕长度：中文每行不超过 {n} 个字，每条最多 2 行（行之间用换行符，JSON 里写作 \\n）。"
                + LINE_RULES +
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
