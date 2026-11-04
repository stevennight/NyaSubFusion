import re
import threading
from functools import lru_cache

from . import ruby
from .base import LanguagePack

# 小学一、二年级汉字（教育漢字）：difficult 模式下只由这些字组成的词不注音
GRADE1 = set("一右雨円王音下火花貝学気九休玉金空月犬見五口校左三山子四糸字耳七車手十出女小上森人水正生青夕石赤千川先早草"
             "足村大男竹中虫町天田土二日入年白八百文木本名目立力林六")
GRADE2 = GRADE1 | set("引羽雲園遠何科夏家歌画回会海絵外角楽活間丸岩顔汽記帰弓牛魚京強教近兄形計元言原戸古午後語工公広交光考"
                      "行高黄合谷国黒今才細作算止市矢姉思紙寺自時室社弱首秋週春書少場色食心新親図数西声星晴切雪船線前組走"
                      "多太体台地池知茶昼長鳥朝直通弟店点電刀冬当東答頭同道読内南肉馬売買麦半番父風分聞米歩母方北毎妹万明"
                      "鳴毛門夜野友用曜来里理話")
EASY = {"grade1": GRADE1, "grade2": GRADE2}
HONORIFICS = {"さん", "様", "さま", "君", "くん", "ちゃん", "氏", "殿", "先生", "せんせい"}

_POS = {"接続助詞": "conj_particle", "助詞": "particle", "助動詞": "aux", "動詞": "verb", "形容詞": "adj", "形状詞": "adj", "副詞": "adv",
        "名詞": "noun", "代名詞": "noun", "接尾辞": "suffix", "接頭辞": "prefix", "補助記号": "punct", "記号": "punct",
        "感動詞": "interj", "連体詞": "adnominal", "接続詞": "conj", "空白": "space"}

_dictionary = None
_dict_lock = threading.Lock()
_local = threading.local()


def _tok():
    """sudachi 的分词器：词典大家共用（加载一次），分词器每个线程一个——同一个分词器不能两个线程同时用
    （界面同时发来几个请求时会报 “Tokenizer is already in use”）。"""
    global _dictionary
    tok = getattr(_local, "tok", None)
    if tok is None:
        with _dict_lock:
            if _dictionary is None:
                from sudachipy import Dictionary

                _dictionary = Dictionary(dict="core")
        tok = _local.tok = _dictionary.tokenizer()
    return tok


@lru_cache(maxsize=1024)
def _furigana_hint(line, i, j, reading):
    """画面上的振假名：line[i:j] 这几个汉字读 reading。找分词里包含它们的那个词（汉字前后只能是送假名），
    分词给的读音和“送假名 + 振假名”对得上就不用管；对不上（藁苞 わらづと）返回整个词的读音。"""
    reading = ruby.to_hira(reading)
    if not reading or not 0 <= i < j <= len(line):
        return None
    try:
        from sudachipy import SplitMode

        toks = _tok().tokenize(line, SplitMode.C)
    except ImportError:
        return None
    for m in toks:
        s, e = m.begin(), m.end()
        if not (s <= i and j <= e):
            continue
        surface = m.surface()
        head, tail = line[s:i], line[j:e]
        if not all(ruby.is_kana(c) for c in head + tail):  # 词里还有别的汉字：读音分不开，不管
            return None
        tail_h = ruby.to_hira(tail)
        # 识别出的注音有时把送假名也带上了（瞬 + き 读成「まばたき」）
        want = ruby.to_hira(head) + (reading if tail_h and reading.endswith(tail_h) else reading + tail_h)
        got = ruby.to_hira(m.reading_form())
        if got == want:
            return None
        return {"src": surface, "reading": want}
    return None


@lru_cache(maxsize=4096)
def _phrases(text):
    """日文分词（sudachi）：[(词, 词性大类)]；没装 sudachi 返回 None（只在空格 / 标点处断行）。"""
    try:
        from sudachipy import SplitMode

        return tuple((m.surface(), m.part_of_speech()[0]) for m in _tok().tokenize(text, SplitMode.C))
    except Exception:
        return None


# 粘着前面那个词的：它们前面不断（一行不以助词、助动词、接尾辞开头）
_ATTACHED = {"助詞", "助動詞", "接尾辞", "補助記号"}


class Japanese(LanguagePack):
    code = "ja"
    name = "日语"
    whisper = "ja"
    ocr_lang = "japan"
    line_chars = 30
    cps = 8
    hallucinations = ("ご視聴ありがとうございました", "チャンネル登録", "最後までご覧いただき", "字幕は")
    name_super = re.compile(r"[（(]\s*\d{1,3}\s*(?:歳)?\s*[)）]\s*$|(?:さん|氏|くん|ちゃん)\s*$")
    meaningful = re.compile(r"[぀-ヿ㐀-鿿]")
    break_after = set("　 、。，；：？！…」』）】》”")
    no_line_start = set("、。，．・：；？！ー」』）】》ぁぃぅぇぉっゃゅょァィゥェォッャュョ")
    no_line_end = set("「『（【《〈〔“‘")

    def break_levels(self, text):
        """空格 / 标点后（3）之外，按分词补上：助词、助动词之后（文节的边界，2）、其他词与词之间（1）；
        助词、助动词、接尾辞前面不断（“鴨が / 餌場に”可以，“鴨 / が”不行）。"""
        out = super().break_levels(text)
        toks = _phrases(text)
        if not toks:
            return out
        pos = 0
        for i, (w, p) in enumerate(toks[:-1]):
            pos += len(w)
            if toks[i + 1][1] in _ATTACHED:
                if out.get(pos, 0) < 3:
                    out.pop(pos, None)
                continue
            out[pos] = max(out.get(pos, 0), 2 if p in ("助詞", "助動詞") else 1)
        return out
    annotations = ("furigana",)
    translit_name = "罗马字"

    def prompt_source_notes(self, with_tr=True) -> str:
        both, where = ("原文、译文都", "原文和译文里都") if with_tr else ("原文", "原文里")
        return ("日文字幕习惯：外挂字幕里的（　）通常是说话人或音效，≪≫ 表示画外音，♪ 表示歌曲；"
                "方言台词按意思理解。\n"
                f"字幕行尾的“➡”表示这句话没说完、接着下一条字幕：把这几条合成一条 cue，{both}按原来的字幕条用“｜”分段；"
                f"同一条字幕里的换行（“ / ”）只是排版，不要变成“｜”。{where}不要保留“➡”。")

    def display(self, text, opts):
        lines = [re.sub(r"[。、\s　]+$", "", l.strip()) for l in (text or "").replace("\\N", "\n").split("\n")]
        return "　".join(l for l in lines if l)

    # ---------- 注音 ----------
    def annotate(self, text, readings=()):
        """readings：[{"src": 写法, "reading": 读音, "proper": bool, "from": "dict"|"glossary"}]，按长度优先匹配。"""
        words = []
        pos = 0
        entries = sorted((r for r in readings if r.get("src") and r.get("reading")), key=lambda r: -len(r["src"]))
        while pos < len(text):
            hit = next((r for r in entries if text.startswith(r["src"], pos)), None)
            if hit:
                words.append({"t": hit["src"], "segs": ruby.align_okurigana(hit["src"], hit["reading"]), "pos": "noun",
                              "p": hit.get("proper", True), "num": False, "src": hit.get("from", "dict"), "guess": False})
                pos += len(hit["src"])
                continue
            nxt = min((i for i in (text.find(r["src"], pos) for r in entries) if i >= 0), default=len(text))
            words += self._sudachi(text[pos:nxt])
            pos = nxt
        return words

    def _sudachi(self, text):
        from sudachipy import SplitMode

        out = []
        for m in _tok().tokenize(text, SplitMode.C) if text else []:
            surface = m.surface()
            p = m.part_of_speech()
            reading = ruby.to_hira(m.reading_form())
            has_kanji = any(ruby.is_kanji(c) for c in surface)
            known = all(ruby.is_kana(c) or c == "ー" for c in reading) and reading
            segs = ruby.align_okurigana(surface, reading) if has_kanji and known else [[surface]]
            proper = p[1] == "固有名詞"
            pos = _POS.get(p[1]) if p[1] == "接続助詞" else _POS.get(p[0], "other")
            out.append({"t": surface, "segs": segs, "pos": pos, "p": proper,
                        "num": p[1] == "数詞" or surface.isdigit(), "src": "auto",
                        "guess": has_kanji and (proper or not known)})
        return out

    def is_furigana(self, text):
        t = re.sub(r"[\s　]", "", text or "")
        return 0 < len(t) <= 12 and all(ruby.is_kana(c) for c in t)

    def furigana_hint(self, line, i, j, reading):
        hint = _furigana_hint(line, i, j, reading)
        return dict(hint) if hint else None


    def ruby_visible(self, word, cfg, first):
        """cfg 是 AnnotateCfg；first 表示这个词是不是第一次出现。"""
        if word["num"] and not cfg.numbers:
            return False
        if cfg.scope == "all":
            return True
        if cfg.scope == "proper":
            return word["p"]
        if cfg.scope == "first":
            return first
        if word["t"] in cfg.known_words:
            return False
        easy = EASY[cfg.easy_kanji]
        return word["p"] or any(ruby.is_kanji(c) and c not in easy for c in word["t"])

    def ruby_text(self, seg, cfg):
        r = seg[1]
        return ruby.to_kata(r) if cfg.script == "katakana" else ruby.romaji(r) if cfg.script == "romaji" else r

    # ---------- 整行转写 ----------
    def transliterate(self, words, tone="mark"):
        parts = []
        for w in words:
            if w["pos"] == "punct" or all(not (ruby.is_kana(c) or ruby.is_kanji(c)) for c in w["t"]):
                parts.append(("p", ruby.romaji(w["t"])))
                continue
            kana = "".join(s[1] if len(s) > 1 else s[0] for s in w["segs"])
            r = ruby.romaji(kana, keep_sokuon=True)
            if w["pos"] == "particle":
                r = {"は": "wa", "へ": "e", "を": "o"}.get(w["t"], r)
            if w["p"] and r:
                r = r[0].upper() + r[1:]
            # 后缀、接在用言后的助动词并到前一个词：漁師たち → ryoushitachi，出た → deta（もの だ 仍分开）
            prev = next((x["pos"] for x in reversed(words[:words.index(w)]) if x["pos"] != "space"), None)
            attach = w["pos"] == "suffix" or (prev in ("verb", "adj", "aux") and (
                w["pos"] == "aux" or (w["pos"] == "conj_particle" and w["t"] in ("て", "で", "ば", "ちゃ", "じゃ"))))
            if w["pos"] == "suffix" and w["t"] in HONORIFICS:
                r = "-" + r  # 敬称：Satou-san
            parts.append(("a" if attach else "w", r))
        out = ""
        for kind, r in parts:
            r = r.strip() if kind != "p" else r
            if not r:
                continue
            if out.endswith(ruby.SOKUON):  # 上一个词以「っ」结尾：双写这个词的第一个辅音
                c = "t" if r.startswith("ch") else (r[0] if r[0].isalpha() and r[0] not in "aiueon" else "")
                out = out[:-1] + c
                if kind == "w":
                    kind = "a"
            if kind == "p":
                out = out.rstrip() + r if r.strip() in ",.?!)\"…:" or r.strip() == "..." else out + r
            elif kind == "a" and out and not out.endswith(" "):
                out += r
            else:
                out += ("" if not out or out.endswith((" ", "(", '"')) else " ") + r
        return re.sub(r"\s+", " ", out.replace(ruby.SOKUON, "")).strip()
