"""排版（与输出格式无关）：按输出方案取图层 → 规范化 → 断行 → 长句拆条 → 注音 / 转写 → 消除重叠。

产出的“排版条目”交给 ass.py / srt.py 写文件：
    {"start", "end", "kind": "main" | "info", "layers": [(LayerCfg, 语言, 行列表)], "rect"?, "sources"}
每一行是若干块 [(文字, 注音或 None)]；没有注音的行就是一块。
"""
from .. import lang
from ..core import segments
from ..env import log
from ..lang import ruby

NEAR_TYPES = ("info", "name", "title")
_warned = set()


def _warn(key, msg):
    if key not in _warned:
        _warned.add(key)
        log(msg)


def plain(line):
    return "".join(t for t, _ in line)


def resolve_layers(profile, src_code, targets):
    """方案里实际要输出的图层：[(LayerCfg, 语言代码)]；转写层的语言是被转写那一层的语言。"""
    out = []
    for ly in profile.layers:
        if ly.show == "off":
            continue
        if ly.role == "original":
            out.append((ly, src_code))
        elif ly.role == "translation":
            out.append((ly, lang.canonical(ly.lang or targets[0])))
        else:
            code = src_code if ly.of == "original" else lang.canonical(ly.of)
            if not lang.get(code).translit_name:
                _warn(("translit", code), f"提示：{lang.get(code).name}没有整行转写，方案「{profile.name}」的转写层跳过")
                continue
            out.append((ly, code))
    for ly, code in out:
        k = ly.annotate.kind
        if k != "none" and k not in lang.get(code).annotations:
            _warn(("annot", code, k), f"提示：{lang.get(code).name}不支持「{k}」注音，方案「{profile.name}」的这一层不注音")
    return out


def _opts(cfg, code):
    return cfg.render.lang_options.get(code, {})


def _max_chars(ly, pack, opts):
    if ly.role == "transliteration":
        return ly.max_chars or 48
    return ly.max_chars or int(opts.get("line_chars") or pack.line_chars)


def build(cues, profile, cfg, src_code, tokens=None):
    """tokens：{cue_id: {"orig": 注音词, "tr": {语言: 注音词}}}，来自 annotate 步骤；没有就不注音。"""
    targets = [lang.canonical(t) for t in cfg.project.targets]
    layers = resolve_layers(profile, src_code, targets)
    if not layers:
        return []
    seen = [set() for _ in layers]  # scope = first：每层记录已经注过的词
    main, info = [], []
    for cue in sorted(cues, key=lambda c: c["start"]):
        if cue.get("type") in NEAR_TYPES:
            if profile.info_mode != "off":
                item = _info_item(cue, layers, cfg)
                if item:
                    info.append(item)
            continue
        main += _expand(cue, layers, profile, cfg, (tokens or {}).get(cue.get("id"), {}), seen)
    # 手动放过位置的字幕各放各的（常常就是为了和别的字幕同时显示），不参与“后一句开始时前一句结束”
    fixed = [m for m in main if m.get("pos")]
    main = _extend_short(_resolve_overlaps([m for m in main if not m.get("pos")]), profile.min_duration)
    return main + fixed + info


def _pos(cue):
    """手动拖过的位置：[x, y]（相对画面宽高 0~1，是字幕块底边中点）；没有或不合法返回 None。"""
    p = cue.get("pos")
    if isinstance(p, (list, tuple)) and len(p) == 2 and all(isinstance(v, (int, float)) for v in p):
        return [min(max(float(p[0]), 0.0), 1.0), min(max(float(p[1]), 0.0), 1.0)]
    return None


def _source_of(cue, ly, code):
    """(文字, 注音词的键)。转写层取被转写那一层的文字。"""
    if ly.role == "original" or (ly.role == "transliteration" and ly.of == "original"):
        return cue["orig"]["text"], "orig"
    return (cue.get("tr", {}).get(code) or {}).get("text", ""), code


def _info_item(cue, layers, cfg):
    """说明类：用第一个有译文的译文层；没有译文层就用原文层。"""
    for ly, code in sorted((x for x in layers if x[0].role != "transliteration"), key=lambda x: x[0].role != "translation"):
        text = _source_of(cue, ly, code)[0].replace("｜", "")
        if text:
            pack = lang.get(code)
            opts = _opts(cfg, code)
            lines = pack.wrap(pack.display(text, opts), _max_chars(ly, pack, opts) + 4)
            return {"start": cue["start"], "end": cue["end"], "kind": "info", "layers": [(ly, code, [[(l, None)] for l in lines])],
                    "rect": cue.get("rect"), "sources": cue.get("sources", []), "cue": cue.get("id"), "pos": _pos(cue)}
    return None


def _expand(cue, layers, profile, cfg, tok, seen):
    """一条字幕 → 一条或几条（按“｜”或按长度拆）。
    时间：有 parts（每段按来源字幕算好的时间）就按段用；段内再按长度拆的、或者没有 parts 的，按主图层字数分配。
    原文和译文的“｜”段数对不上时（大模型拿它当换行用）不按“｜”分，当成一整句按长度断。"""
    bars = segments.bars_ok(cue)
    n_seg = len(segments.pieces(cue["orig"]["text"])) if bars else 0
    per_seg = lambda key: cue.get(key) if n_seg and len(cue.get(key) or []) == n_seg else None
    seg_show, seg_type, pon = per_seg("parts_show"), per_seg("parts_type"), per_seg("parts_onscreen")
    force = cue.get("show_orig") if cue.get("show_orig") in ("on", "off") else None

    def orig_hidden(ly, k=None):
        """原文要不要显示（k = 第几段，None = 整条）：这一段手动指定的 > 整条手动指定的 > 画面上已有原文硬字幕就不显示。"""
        if ly.role != "original":
            return False
        f = (seg_show[k] if seg_show and k is not None else None) or force
        if f in ("on", "off"):
            return f == "off"
        if ly.show != "auto":
            return False
        return bool(pon[k]) if pon and k is not None else bool(cue.get("onscreen"))

    # 整条都不显示原文时这一层整个跳过；有一段要显示就保留，到每一屏再按段决定
    hidden = lambda ly: all(orig_hidden(ly, k) for k in range(n_seg)) if n_seg else orig_hidden(ly)
    has_orig_shown = any(ly.role == "original" and not hidden(ly) for ly, _ in layers)
    any_tr = any(_source_of(cue, ly, code)[0] for ly, code in layers if ly.role == "translation")
    fell_back = False
    texts = []  # [(图层序号, ly, 语言, 文字, 注音词的键)]
    for li, (ly, code) in enumerate(layers):
        if hidden(ly):
            continue
        t, key = _source_of(cue, ly, code)
        if not t and ly.role == "translation" and not any_tr and not has_orig_shown and not fell_back:
            t, key, code = cue["orig"]["text"], "orig", cue["orig"]["lang"]  # 一条译文都没有时显示原文（只显示一次）
            fell_back = True
        if t and not bars and ly.role != "translation":  # 段数对不上：原文不按“｜”分，跟着译文的分屏按比例切
            t = t.replace("｜", " " if lang.get(code).word_based else "")
        if t:
            texts.append((li, ly, code, t, key))
    if not texts or all(ly.role == "transliteration" for _, ly, _, _, _ in texts):
        return []
    # 主图层：第一个译文层（没有就第一个非转写层），决定怎么拆
    pi = next((i for i, x in enumerate(texts) if x[1].role == "translation"),
              next(i for i, x in enumerate(texts) if x[1].role != "transliteration"))
    _, p_ly, p_code, p_text, _ = texts[pi]
    p_pack, p_opts = lang.get(p_code), _opts(cfg, p_code)
    p_pieces = [p for p in p_text.split("｜") if p.strip()] or [p_text]
    groups = []  # [(piece_index, [主图层行])]
    for k, piece in enumerate(p_pieces):
        lines = p_pack.wrap(p_pack.display(piece, p_opts), _max_chars(p_ly, p_pack, p_opts))
        chunks = [lines[i:i + profile.max_lines] for i in range(0, len(lines), profile.max_lines)] or [[]]
        groups += [(k, g) for g in chunks]
    weights = [max(1, sum(len(l) for l in g)) for _, g in groups]

    per_layer = []  # 每层每组的行（文字）
    for i, (_, ly, code, t, _) in enumerate(texts):
        if i == pi:
            per_layer.append([g for _, g in groups])
            continue
        pack, opts = lang.get(code), _opts(cfg, code)
        pieces = [p for p in t.split("｜") if p.strip()]
        parts = []
        if len(pieces) == len(p_pieces):
            for k, piece in enumerate(pieces):
                sub = [w for (kk, _), w in zip(groups, weights) if kk == k]
                parts += pack.split(pack.display(piece, opts), [w / sum(sub) for w in sub])
        else:
            whole = pack.display(t.replace("｜", " " if pack.word_based else ""), opts)
            parts = pack.split(whole, [w / sum(weights) for w in weights])
        if ly.role == "transliteration":
            per_layer.append([[p] if p else [] for p in parts])  # 先保留原文，下面再转写
        else:
            per_layer.append([pack.wrap(p, _max_chars(ly, pack, opts))[: profile.max_lines] if p else [] for p in parts])

    parts = cue.get("parts") if bars else None
    # 分段里画面上已经有原文的那几段：这几屏不再显示原文（其他段照常显示）
    if parts and len(parts) == len(p_pieces):
        spans = []
        for k, (a, b) in enumerate(parts):
            ws = [w for (kk, _), w in zip(groups, weights) if kk == k]
            spans += [(b - a) * w / sum(ws) for w in ws]
    else:
        spans = [(cue["end"] - cue["start"]) * w / sum(weights) for w in weights]
    out, t0 = [], cue["start"]
    for gi, d in enumerate(spans):
        lys = []
        for i, (li, ly, code, _, key) in enumerate(texts):
            lines = per_layer[i][gi]
            if not lines or (n_seg and orig_hidden(ly, groups[gi][0])):
                continue
            words = tok.get("orig") if key == "orig" else tok.get("tr", {}).get(key)
            lys.append((ly, code, _decorate(lines, ly, code, words, seen[li], profile)))
        lys = [x for x in lys if x[2]]
        if lys:
            typ = (seg_type[groups[gi][0]] if seg_type and n_seg else None) or cue.get("type")  # 这一段单独设了类型就用它
            out.append({"start": round(t0, 3), "end": round(t0 + d, 3), "kind": "main", "type": typ, "cue": cue.get("id"), "pos": _pos(cue),
                        "layers": lys, "sources": cue.get("sources", []), "onscreen": cue.get("onscreen", False)})
        t0 += d
    return out


def _decorate(lines, ly, code, words, seen, profile):
    """文字行 → 块行：注音层加上注音，转写层换成转写文字。"""
    pack = lang.get(code)
    if ly.role == "transliteration":
        if not words:
            _warn(("nowords", code), "提示：没有注音数据（未安装注音依赖？），转写层跳过")
            return []
        covered = []
        for line in ruby.chunkify(lines, words):
            for ch in line:
                if ch["key"] and ch["key"][0] not in covered:
                    covered.append(ch["key"][0])
        text = pack.transliterate([words[i] for i in covered], ly.tone)
        return [[(l, None)] for l in lang.get("en").wrap(text, _max_chars(ly, pack, {}))[: profile.max_lines]]
    cfg = ly.annotate
    if cfg.kind == "none" or cfg.kind not in pack.annotations or not words:
        if cfg.kind != "none" and cfg.kind in pack.annotations and not words:
            _warn(("nowords", code), "提示：没有注音数据（未安装注音依赖？），本次不注音")
        return [[(l, None)] for l in lines]
    out = []
    done = set()  # 一段底字只注一次（跨行时注在第一次出现处）
    firsts = {}   # 词 → 本组里第一次出现的位置
    for line in ruby.chunkify(lines, words):
        cur = []
        for ch in line:
            rt = None
            if ch["key"] and ch["key"] not in done:
                wi, si = ch["key"]
                word, seg = words[wi], words[wi]["segs"][si]
                first = word["t"] not in seen and firsts.setdefault(word["t"], wi) == wi
                if len(seg) > 1 and pack.ruby_visible(word, cfg, first):
                    rt = pack.ruby_text(seg, cfg)
                done.add(ch["key"])
            if rt is None and cur and cur[-1][1] is None:
                cur[-1] = (cur[-1][0] + ch["text"], None)
            else:
                cur.append((ch["text"], rt))
        out.append(cur)
    for line in ruby.chunkify(lines, words):  # 记下这一组出现过的词（scope = first）
        for ch in line:
            if ch["key"]:
                seen.add(words[ch["key"][0]]["t"])
    return out


def _resolve_overlaps(items, min_dur=0.6):
    """台词不叠着显示：后一句开始时前一句结束；几乎同时开始的合并成一条。"""
    out = []
    for c in sorted(items, key=lambda c: (c["start"], c["end"])):
        if out and c["start"] < out[-1]["end"] - 0.05:
            prev = out[-1]
            if c["start"] - prev["start"] < min_dur:
                merged = []
                for ly, code, lines in prev["layers"]:
                    extra = next((l for l2, c2, l in c["layers"] if l2 is ly), [])
                    merged.append((ly, code, lines + extra))
                merged += [x for x in c["layers"] if not any(x[0] is m[0] for m in merged)]
                prev["layers"] = merged
                prev["end"] = max(prev["end"], c["end"])
                prev["sources"] = prev.get("sources", []) + c.get("sources", [])
                continue
            prev["end"] = c["start"]
        out.append(dict(c))
    return out


def _extend_short(items, min_dur):
    """太短的字幕往后延长，但不盖到下一句。"""
    for a, b in zip(items, items[1:] + [None]):
        want = a["start"] + min_dur
        if a["end"] < want:
            a["end"] = round(min(want, b["start"]) if b else want, 3)
    return items
