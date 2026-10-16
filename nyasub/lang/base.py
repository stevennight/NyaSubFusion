"""语言包接口。

一门语言的分词、断行、标点、识别参数、给大模型的语言说明都放在这里；
原文和任何目标语言的译文都走同一套语言包。注音 / 转写（振假名、拼音…）在 M2 加入。
"""
import re


class LanguagePack:
    code = ""
    name = ""
    whisper = None            # faster-whisper 的 language 参数
    ocr_lang = "en"           # rapidocr 的 LangRec 值
    line_chars = 30           # 每行默认字数（按字计的语言）或字符数（按词计的语言）
    cps = 15                  # 每秒字数上限，超过就标记“语速过快”
    word_based = False        # 按空格分词断行（英语等）
    direction = "ltr"
    hallucinations: tuple[str, ...] = ()   # 语音识别常见的幻觉句
    name_super = None         # 人名条正则，例如「〇〇さん（82）」
    meaningful = re.compile(r"\w")         # 判断画面字是否像正经文字
    break_after = set(" ,.;:!?)]}\"'")
    no_line_start = set(",.;:!?)]}")

    # ---------- 给大模型的说明 ----------
    def prompt_source_notes(self) -> str:
        """作为源语言时，给大模型的额外说明（字幕习惯、方言等）。"""
        return ""

    def prompt_target_rules(self, opts: dict) -> str:
        """作为目标语言时的长度、标点要求。"""
        n = int(opts.get("line_chars") or self.line_chars)
        unit = "个字符" if self.word_based else "个字"
        return (f"字幕长度：每行不超过 {n} {unit}，每条最多 2 行（行之间用换行符，JSON 里写作 \\n）。"
                "一句话太长、两行放不下时，用“｜”把译文分成几段，每段不超过 2 行；原文也用“｜”分成同样多段、内容一一对应。")

    # ---------- 显示前的规范化 ----------
    def display(self, text: str, opts: dict) -> str:
        """去掉多余空白；按词计的语言把换行并成空格，之后统一重新断行。"""
        lines = [l.strip() for l in (text or "").replace("\\N", "\n").split("\n")]
        joiner = " " if self.word_based else ""
        return joiner.join(l for l in lines if l)

    # ---------- 断行 ----------
    def wrap(self, text: str, max_chars: int) -> list[str]:
        if self.word_based:
            return _wrap_words(text, max_chars)
        return _wrap_chars(text, max_chars, self.break_after, self.no_line_start)

    def split(self, text: str, ratios: list[float]) -> list[str]:
        """按比例把一段文字切成几份，切点吸附到附近的空格 / 标点。"""
        if len(ratios) <= 1:
            return [text]
        parts, start, acc = [], 0, 0.0
        n = len(text)
        for r in ratios[:-1]:
            acc += r
            target = int(round(n * acc))
            cut = target
            for d in range(0, 12 if self.word_based else 8):
                hit = [p for p in (target + d, target - d)
                       if start < p < n and (text[p - 1] in self.break_after or (self.word_based and text[p] == " "))]
                if hit:
                    cut = hit[0]
                    break
            parts.append(text[start:cut].strip("　 "))
            start = cut
        parts.append(text[start:].strip("　 "))
        return parts

    def length(self, text: str) -> int:
        """计算语速用的长度：不算空白和标点。"""
        return len(re.sub(r"[\s　\W]", "", text or ""))


def _wrap_chars(text, maxc, break_after, no_start):
    """按字数断行：优先在空格 / 标点后断，各行尽量等长，不让禁则字符出现在行首。"""
    out = []
    for line in text.split("\n"):
        while len(line) > maxc:
            n_lines = -(-len(line) // maxc)
            target = -(-len(line) // n_lines)
            cut = None
            for d in range(0, 6):
                for pos in (target + d, target - d):
                    if 0 < pos < len(line) and pos <= maxc and line[pos - 1] in break_after:
                        cut = pos
                        break
                if cut:
                    break
            if cut is None:
                cut = min(target, maxc)
                while 1 < cut < len(line) and line[cut] in no_start:
                    cut -= 1
            out.append(line[:cut].rstrip("　 "))
            line = line[cut:].lstrip("　 ")
        if line:
            out.append(line)
    return out


def _wrap_words(text, maxc):
    out = []
    for para in text.split("\n"):
        cur = ""
        for w in para.split():
            if cur and len(cur) + 1 + len(w) > maxc:
                out.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}" if cur else w
        if cur:
            out.append(cur)
    # 两行时尽量等长
    if len(out) == 2:
        words = text.split()
        best = None
        for i in range(1, len(words)):
            a, b = " ".join(words[:i]), " ".join(words[i:])
            if len(a) <= maxc and len(b) <= maxc:
                score = abs(len(a) - len(b))
                if best is None or score < best[0]:
                    best = (score, [a, b])
        if best:
            out = best[1]
    return out
