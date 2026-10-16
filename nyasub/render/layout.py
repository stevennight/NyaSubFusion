"""排版（与输出格式无关）：按输出方案取图层 → 规范化 → 断行 → 长句拆条 → 消除重叠。

产出的“排版条目”交给 ass.py / srt.py 写文件：
    {"start", "end", "kind": "main" | "info", "layers": [(LayerCfg, 语言, [行...])], "rect"?, "sources"}
"""
from .. import lang
from ..env import log

NEAR_TYPES = ("info", "name", "title")
_warned = set()


def resolve_layers(profile, src_code, targets):
    """方案里实际要输出的图层：[(LayerCfg, 语言代码)]。"""
    out = []
    for ly in profile.layers:
        if ly.show == "off":
            continue
        if ly.role == "original":
            out.append((ly, src_code))
        elif ly.role == "translation":
            out.append((ly, lang.canonical(ly.lang or targets[0])))
        elif "translit" not in _warned:
            _warned.add("translit")
            log("提示：转写图层（罗马字 / 整行拼音）将在 M2 支持，本次跳过")
    return out


def _text_of(cue, ly, code):
    if ly.role == "original":
        return cue["orig"]["text"]
    return (cue.get("tr", {}).get(code) or {}).get("text", "")


def _opts(cfg, code):
    return cfg.render.lang_options.get(code, {})


def _max_chars(ly, pack, opts):
    return ly.max_chars or int(opts.get("line_chars") or pack.line_chars)


def build(cues, profile, cfg, src_code):
    targets = [lang.canonical(t) for t in cfg.project.targets]
    layers = resolve_layers(profile, src_code, targets)
    if not layers:
        return []
    main, info = [], []
    for cue in cues:
        if cue.get("type") in NEAR_TYPES:
            if profile.info_mode != "off":
                item = _info_item(cue, layers, cfg)
                if item:
                    info.append(item)
            continue
        main += _expand(cue, layers, profile, cfg)
    main = _extend_short(_resolve_overlaps(main), profile.min_duration)
    return main + info


def _info_item(cue, layers, cfg):
    """说明类：用第一个有译文的译文层；没有译文层就用原文层。"""
    for ly, code in sorted(layers, key=lambda x: x[0].role != "translation"):
        text = _text_of(cue, ly, code).replace("｜", "")
        if text:
            pack = lang.get(code)
            opts = _opts(cfg, code)
            lines = pack.wrap(pack.display(text, opts), _max_chars(ly, pack, opts) + 4)
            return {"start": cue["start"], "end": cue["end"], "kind": "info", "layers": [(ly, code, lines)],
                    "rect": cue.get("rect"), "sources": cue.get("sources", [])}
    return None


def _expand(cue, layers, profile, cfg):
    """一条字幕 → 一条或几条（按“｜”或按长度拆），时间按主图层字数分配。"""
    has_orig_shown = any(ly.role == "original" and not (ly.show == "auto" and cue.get("onscreen")) for ly, _ in layers)
    any_tr = any(_text_of(cue, ly, code) for ly, code in layers if ly.role == "translation")
    fell_back = False
    texts = []  # [(ly, code, 原始文字)]
    for ly, code in layers:
        if ly.role == "original" and ly.show == "auto" and cue.get("onscreen"):
            continue  # 画面上已经有原文硬字幕
        t = _text_of(cue, ly, code)
        if not t and ly.role == "translation" and not any_tr and not has_orig_shown and not fell_back:
            t, code = cue["orig"]["text"], cue["orig"]["lang"]  # 一条译文都没有时显示原文（只显示一次）
            fell_back = True
        if t:
            texts.append((ly, code, t))
    if not texts:
        return []
    # 主图层：第一个译文层，决定怎么拆
    pi = next((i for i, (ly, _, _) in enumerate(texts) if ly.role == "translation"), 0)
    p_ly, p_code, p_text = texts[pi]
    p_pack, p_opts = lang.get(p_code), _opts(cfg, p_code)
    p_pieces = [p for p in p_text.split("｜") if p.strip()] or [p_text]
    groups = []  # [(piece_index, [主图层行])]
    for k, piece in enumerate(p_pieces):
        lines = p_pack.wrap(p_pack.display(piece, p_opts), _max_chars(p_ly, p_pack, p_opts))
        chunks = [lines[i:i + profile.max_lines] for i in range(0, len(lines), profile.max_lines)] or [[]]
        groups += [(k, g) for g in chunks]
    weights = [max(1, sum(len(l) for l in g)) for _, g in groups]

    # 其他图层按主图层的分段 / 字数比例切开
    per_layer = []
    for i, (ly, code, t) in enumerate(texts):
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
        per_layer.append([pack.wrap(p, _max_chars(ly, pack, opts))[: profile.max_lines] if p else [] for p in parts])

    out, t0 = [], cue["start"]
    dur = cue["end"] - cue["start"]
    for gi, w in enumerate(weights):
        d = dur * w / sum(weights)
        lys = [(ly, code, per_layer[i][gi]) for i, (ly, code, _) in enumerate(texts) if per_layer[i][gi]]
        out.append({"start": round(t0, 3), "end": round(t0 + d, 3), "kind": "main", "type": cue.get("type"),
                    "layers": lys, "sources": cue.get("sources", []), "onscreen": cue.get("onscreen", False)})
        t0 += d
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
