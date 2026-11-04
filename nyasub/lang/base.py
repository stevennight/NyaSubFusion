"""语言包接口。

一门语言的分词、断行、标点、识别参数、给大模型的语言说明都放在这里；
原文和任何目标语言的译文都走同一套语言包。注音 / 转写（振假名、拼音…）在 M2 加入。
"""
import re


# 分屏、换行的要求（各语言共用）
LINE_RULES = (
    "换行用换行符，不要用“｜”换行。“｜”只用来分屏：一条 cue 合并了几条前后相接的来源（比如一句话被拆在两条字幕里）时，"
    "“｜”放在来源和来源的交界处；一条字幕 / 画面字原来就是一屏，它里面的换行（“ / ”）只是换行，不能换成“｜”"
    "（「ペーパーキットって富士市 / 紙のまちなんで」是一条字幕，就是一屏）。没有字幕的语音一句话太长、两行放不下时，"
    "才在它中间用“｜”分成几屏。每屏不超过 2 行；原文和译文必须分成同样多段、内容一一对应。"
    "每段译文只译原文同一段里的内容，不要把前一段的意思挪到后一段（反过来也一样）：例如原文「明治時代　豊富な湧き水を利用した紙作りが｜一挙に広まり…」，"
    "译文应是「明治时代 利用丰富泉水造纸｜迅速普及…」，不能是「明治时代｜利用丰富泉水造纸 迅速普及…」。"
    "只有两种语言语序必须颠倒、实在拆不开时才可以跨段，这时也让每段的长度和原文那段差不多。一屏放得下的短句不要用“｜”。"
    "一屏里换行时不要让第二行只剩一个名字或两三个字（“她嫁给了在这水边长大的丈夫\\n武光”这样不好）；放得下就写成一行；"
    "一般第一行短、第二行长。两个人各说一句时才按说话人换行。"
)


class LanguagePack:
    code = ""
    name = ""
    whisper = None            # faster-whisper 的 language 参数
    ocr_lang = "en"           # rapidocr 的 LangRec 值
    line_chars = 30           # 每行默认字数（按字计的语言）或字符数（按词计的语言）
    cps = 15                  # 每秒字数上限，超过就标记“语速过快”
    generation = 0            # 分词学了新词（learn_words）就加一：之前按文字记住的注音结果作废
    word_based = False        # 按空格分词断行（英语等）
    direction = "ltr"
    hallucinations: tuple[str, ...] = ()   # 语音识别常见的幻觉句
    name_super = None         # 人名条正则，例如「〇〇さん（82）」
    meaningful = re.compile(r"\w")         # 判断画面字是否像正经文字
    break_after = set(" ,.;:!?)]}\"'")
    no_line_start = set(",.;:!?)]}")
    no_line_end = set("([{")              # 不放在行末的字（开括号、开引号）

    # ---------- 给大模型的说明 ----------
    def prompt_source_notes(self, with_tr=True) -> str:
        """作为源语言时，给大模型的额外说明（字幕习惯、方言等）。"""
        return ""

    def prompt_target_rules(self, opts: dict) -> str:
        """作为目标语言时的长度、标点要求。"""
        n = int(opts.get("line_chars") or self.line_chars)
        unit = "个字符" if self.word_based else "个字"
        return f"字幕长度：每行不超过 {n} {unit}，每条最多 2 行（行之间用换行符，JSON 里写作 \\n）。" + LINE_RULES

    # ---------- 显示前的规范化 ----------
    def display(self, text: str, opts: dict) -> str:
        """去掉多余空白；按词计的语言把换行并成空格，之后统一重新断行。"""
        lines = [l.strip() for l in (text or "").replace("\\N", "\n").split("\n")]
        joiner = " " if self.word_based else ""
        return joiner.join(l for l in lines if l)

    # ---------- 断行 ----------
    def break_levels(self, text: str) -> dict[int, int]:
        """text 里能断开的位置（断在第 p 个字之前）有多好：3 空格 / 标点后（小句的边界），2 小句里的自然停顿，
        1 词与词之间。不在里面的位置在一个词中间，尽量不断。默认只认空格 / 标点；中文、日文按分词补上词边界。"""
        out = {p: 3 for p in range(1, len(text)) if text[p - 1] in self.break_after}
        if self.word_based:
            out.update({p: 3 for p in range(1, len(text)) if text[p] == " "})
        return out

    def _break_points(self, text: str) -> dict[int, int]:
        """break_levels 再加上：开引号 / 开括号不放在行末（“名为“｜茶叶…”），它前面是个好断点（引用、括注的开头）。"""
        out = self.break_levels(text)
        for p in range(1, len(text)):
            if text[p - 1] in self.no_line_end:
                out.pop(p, None)
            elif text[p] in self.no_line_end:
                out[p] = max(out.get(p, 0), 2)
        return out

    def learn_words(self, words) -> None:
        """告诉分词这些是一个词（术语表里的人名、地名），折行时不拆开。没有分词的语言不用管。"""

    def wrap(self, text: str, max_chars: int) -> list[str]:
        if self.word_based:
            return _wrap_words(text, max_chars)
        return _wrap_chars(text, max_chars, self._break_points, self.no_line_start, self.no_line_end)

    def split(self, text: str, ratios: list[float]) -> list[str]:
        """按比例把一段文字切成几份（分屏用），切点选在附近好的断点上（空格 / 标点 > 小句停顿 > 词边界），不切在词中间。"""
        if len(ratios) <= 1:
            return [text]
        parts, start, acc = [], 0, 0.0
        n = len(text)
        lv = self._break_points(text)
        for r in ratios[:-1]:
            acc += r
            target = int(round(n * acc))
            cands = [p for p in range(max(start + 1, target - 12), min(n, target + 13)) if lv.get(p)]
            cut = max(cands, key=lambda p: (_score(lv[p], p, target), -abs(p - target))) if cands else target
            parts.append(text[start:cut].strip("　 "))
            start = cut
        parts.append(text[start:].strip("　 "))
        return parts

    # ---------- 画面上的注音小字（日语的振假名） ----------
    def is_furigana(self, text: str) -> bool:
        """这段字像不像标在别的字上的注音（日语：全是假名）。没有这种习惯的语言一律 False。"""
        return False

    def furigana_hint(self, line: str, i: int, j: int, reading: str):
        """画面上 line[i:j] 标着读音 reading：分词读得不一样时返回要告诉注音的读音 {"src", "reading"}，否则 None。"""
        return None

    # ---------- 注音 / 转写（有的语言包才实现） ----------
    annotations: tuple[str, ...] = ()   # 支持的注音：furigana / pinyin / zhuyin
    translit_name = None                 # 整行转写的名字，例如“罗马字”；None = 不支持

    def annotate(self, text, readings=()):
        """返回注音词列表（见 ruby.py）；不支持注音的语言返回 None。"""
        return None

    def ruby_visible(self, word, cfg, first) -> bool:
        return False

    def ruby_text(self, seg, cfg) -> str:
        return seg[1]

    def transliterate(self, words, tone="mark") -> str:
        return ""

    def length(self, text: str) -> int:
        """计算语速用的长度：不算空白和标点。"""
        return len(re.sub(r"[\s　\W]", "", text or ""))


def _score(level, pos, target):
    """断点好不好：断点本身的好坏（标点 / 空格 > 小句停顿 > 词边界）为主，离理想位置（各行等长）越近越好。"""
    return level * 4 - abs(pos - target)


# 折行的代价（整行一起选断点，总代价最小）：断点越差越贵，在词中间断几乎不可接受；
# 多一行要一点代价（为了不拆开小句可以多一行，只为凑满不行）；一两个字孤零零占一行（“嗯 / 不过……”）要罚
_BREAK_COST = {3: 0, 2: 4, 1: 8}
_MID_WORD = 40
_PER_LINE = 5
_SHORT_LINE = 12
_SHORT3 = 3        # 一行只有三个字（“今天呢｜要去买点东西…”）：比第一行稍长更难看
_ORPHAN = 6        # 最后一行只有三四个字
_TOP_HEAVY = 0.25  # 上一行比最后一行每长一个字


def _wrap_chars(text, maxc, levels, no_start, no_end=frozenset()):
    """按字数断行：整行一起选断点，使总代价最小——断在好的断点上（空格 / 标点 > 小句里的停顿 > 词与词之间），
    不断在一个词中间；为了不拆开小句可以多一行；不留孤零零的一两个字；同样好时各行尽量等长；禁则字符不放行首、开括号不放行末。
    levels(line) → {位置: 好坏}（见 LanguagePack.break_levels）。"""
    out = []
    for line in text.split("\n"):
        if len(line) > maxc:
            out += _best_breaks(line, maxc, levels(line), no_start, no_end)
        elif line:
            out.append(line)
    return out


def _best_breaks(line, maxc, lv, no_start, no_end=frozenset()):
    n = len(line)

    def brk(p):  # 在 p 处断开的代价；None = 不能断（下一行会以禁则字符开头、这一行以开括号结尾）
        if line[p] in no_start or line[p - 1] in no_end:
            return None
        return _BREAK_COST.get(lv.get(p), _MID_WORD)

    def seg_cost(q, p):  # line[q:p] 这一行本身的代价
        k = len(line[q:p].strip("　 "))
        c = _PER_LINE + (_SHORT_LINE if k < 3 else _SHORT3 if k == 3 else 0) + 0.005 * k * k  # 平方项：同样好时让各行等长
        if p == n and q > 0:
            if k <= 4:
                c += _ORPHAN  # 最后一行只剩一个名字、一个词（“…丈夫｜武光”）
            prev = len(line[back[q]:q].strip("　 "))
            c += _TOP_HEAVY * max(0, prev - k - 2)  # 习惯上第一行短、第二行长：不强制，差一两个字不管
        return c

    best = [None] * (n + 1)
    back = [0] * (n + 1)
    best[0] = 0.0
    for p in range(1, n + 1):
        b = 0 if p == n else brk(p)
        if b is None:
            continue
        for q in range(max(0, p - maxc), p):
            if best[q] is None:
                continue
            c = best[q] + seg_cost(q, p) + b
            if best[p] is None or c < best[p]:
                best[p], back[p] = c, q
    if best[n] is None:  # 怎么断都会把禁则字符放到行首：只好按字数切
        return [line[i:i + maxc] for i in range(0, n, maxc)]
    cuts, p = [], n
    while p > 0:
        cuts.append(p)
        p = back[p]
    cuts.reverse()
    return [x for x in (line[a:b].strip("　 ") for a, b in zip([0] + cuts, cuts)) if x]


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
    # 两行时尽量等长；差不多时第一行短一点
    if len(out) == 2:
        words = text.split()
        best = None
        for i in range(1, len(words)):
            a, b = " ".join(words[:i]), " ".join(words[i:])
            if len(a) <= maxc and len(b) <= maxc:
                score = abs(len(a) - len(b)) + _TOP_HEAVY * max(0, len(a) - len(b) - 2)
                if best is None or score < best[0]:
                    best = (score, [a, b])
        if best:
            out = best[1]
    return out
