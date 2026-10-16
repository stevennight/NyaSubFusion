import re

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

_tokenizer = None


def _tok():
    global _tokenizer
    if _tokenizer is None:
        from sudachipy import Dictionary

        _tokenizer = Dictionary(dict="core").tokenizer()
    return _tokenizer


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
    annotations = ("furigana",)
    translit_name = "罗马字"

    def prompt_source_notes(self) -> str:
        return ("日文字幕习惯：外挂字幕里的（　）通常是说话人或音效，≪≫ 表示画外音，♪ 表示歌曲；"
                "方言台词按意思理解。")

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
