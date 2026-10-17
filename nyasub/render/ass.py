"""输出 ASS。

- 图层按方案顺序从上到下叠在一条字幕里（整块底边对齐），每层用自己的样式。
- 带注音的字幕逐块定位（libass 不支持 ruby）：底字和注音用同一个中心 x，注音一定在底字正上方。
- 台词只在真的会撞上原画面硬字幕（或已放好的说明译文）时才往上让。
- 说明类画面字的译文贴在原文旁边。
"""
from . import fonts


def ts(t):
    cs = int(round(max(0.0, t) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def esc(text):
    return (text or "").replace("{", "｛").replace("}", "｝").replace("\r", "").replace("\\N", "\n").replace("\n", "\\N")


def color(hex_rgb, alpha=0):
    """#RRGGBB → ASS 的 &HAABBGGRR。"""
    h = hex_rgb.lstrip("#")
    if len(h) != 6:
        return hex_rgb  # 已经是 ASS 格式
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def style_line(name, st, align=2, margin_v=46):
    return (f"Style: {name},{st.font},{st.size:g},{color(st.color)},&H000000FF,{color(st.outline_color)},&H80000000,"
            f"{-1 if st.bold else 0},{-1 if st.italic else 0},0,0,100,100,0,0,1,{st.outline:g},{st.shadow:g},"
            f"{align},40,40,{margin_v},1")


def header(play_x, play_y, styles: dict, info_styles=()):
    lines = ["[Script Info]", "; 由 NyaSubFusion 生成", "ScriptType: v4.00+", f"PlayResX: {play_x}",
             f"PlayResY: {play_y}", "WrapStyle: 2", "ScaledBorderAndShadow: yes", "YCbCr Matrix: TV.709", "",
             "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
             "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
             "Alignment, MarginL, MarginR, MarginV, Encoding"]
    for name, st in styles.items():
        lines.append(style_line(name, st, align=8 if name in info_styles else 2))
    lines += ["", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    return "\n".join(lines)


def is_vertical(rect, aspect):
    return (rect[3] - rect[1]) > (rect[2] - rect[0]) * aspect * 1.5


def info_pos(rect, vertical, px, py):
    """说明文字的译文放在原文旁边，不盖住原文。返回 (an, x, y)。"""
    x0, y0, x1, y1 = rect
    gap = 10
    if vertical:
        if (x0 + x1) / 2 > 0.5:
            return 9, int(x0 * px) - gap, int(y0 * py)
        return 7, int(x1 * px) + gap, int(y0 * py)
    cx = (x0 + x1) / 2
    below = (y0 + y1) / 2 < 0.5
    if cx < 0.33:
        an, x = (7 if below else 1), int(x0 * px)
    elif cx > 0.67:
        an, x = (9 if below else 3), int(x1 * px)
    else:
        an, x = (8 if below else 2), int(cx * px)
    y = int(y1 * py) + gap if below else int(y0 * py) - gap
    return an, x, y


def _info_spot(it, vertical, w, h, px, py, boxes):
    """说明译文放哪：先试贴着原文（near），再试原文的另一侧，最后放画面顶部 / 底部正中。
    选第一个不出画面、也不压住同一时间其他画面字（含已经放好的说明译文）的位置。
    标题卡这种大字常常只识别出一部分框，只看自己的框会压到没识别全的字上。返回 (an, x, y, 左, 上)。"""
    x0, y0, x1, y1 = it["rect"]
    first = info_pos(it["rect"], vertical, px, py)
    cands = [first]
    if not vertical:  # 另一侧：原文在下半就放到原文下面，反之放上面
        an, x, _ = first
        flip = {1: 7, 2: 8, 3: 9, 7: 1, 8: 2, 9: 3}[an]
        cands.append((flip, x, int(y1 * py) + 10 if flip in (7, 8, 9) else int(y0 * py) - 10))
    cands += [(8, px // 2, 24), (2, px // 2, py - 24)]

    def rect_of(an, x, y):
        bx0 = x if an in (1, 7) else x - w / 2 if an in (2, 8) else x - w
        bx0 = max(8, min(px - w - 8, bx0))  # 不出画面左右
        by0 = y - h if an in (1, 2, 3) else y
        return bx0, by0

    def clear(bx0, by0):
        if by0 < 0 or by0 + h > py:
            return False
        for b in boxes:
            if min(it["end"], b["end"]) - max(it["start"], b["start"]) <= 0.2:
                continue
            if b["x1"] <= bx0 or b["x0"] >= bx0 + w or b["bottom"] <= by0 or b["top"] >= by0 + h:
                continue
            return False
        return True

    for an, x, y in cands:
        bx0, by0 = rect_of(an, x, y)
        if clear(bx0, by0):
            # 左右被挪进画面时，锚点跟着换算
            x = bx0 if an in (1, 7) else bx0 + w / 2 if an in (2, 8) else bx0 + w
            return an, int(x), y, bx0, by0
    an, x, y = first
    bx0, by0 = rect_of(an, x, y)
    return an, int(bx0 if an in (1, 7) else bx0 + w / 2 if an in (2, 8) else bx0 + w), y, bx0, by0


def _box(start, end, x0, x1, top, bottom):
    return {"start": start, "end": end, "x0": x0, "x1": x1, "top": top, "bottom": bottom}


def avoid_margin(item, boxes, base, x0, x1, height, py):
    """只在真的会撞上时才把台词往上推：时间重叠、左右重叠、上下也重叠。"""
    m = base
    for _ in range(3):  # 推上去之后可能又碰到上面的框
        top, bottom = py - m - height, py - m
        moved = False
        for b in boxes:
            if min(item["end"], b["end"]) - max(item["start"], b["start"]) <= 0.2:
                continue
            if b["x1"] < x0 - 20 or b["x0"] > x1 + 20:
                continue
            if b["top"] >= bottom or b["bottom"] <= top:
                continue
            m = max(m, int(py - b["top"]) + 12)
            moved = True
        if not moved:
            break
    return m


def place(items, profile, styles: dict, aspect, hardsub_boxes, margin_v=46):
    """算出每条字幕在 1080p 画布上的位置（导出 ASS 和界面预览共用）。

    返回 (px, py, 用到的样式, 位置列表)：
      说明类 {"kind": "info", "item", "an", "x", "y"} 或 {"kind": "info", "item", "top": True}
      台词   {"kind": "main", "item", "margin_v", "rows", "ruby"}（margin_v 是底边到画面底部的距离）
    """
    py = 1080
    px = int(round(py * aspect))
    used = {}
    for it in items:
        for ly, _, _ in it["layers"]:
            name = profile.info_style if it["kind"] == "info" else ly.style
            used[name] = styles.get(name) or styles["main"]

    boxes = []
    if profile.avoid_hardsub:
        for b in hardsub_boxes:
            r = b["rect"]
            if not b.get("vertical"):
                boxes.append(_box(b["start"], b["end"], r[0] * px, r[2] * px, r[1] * py, r[3] * py))

    placed = []
    info_st = used.get(profile.info_style)
    for it in (i for i in items if i["kind"] == "info"):
        lines = [plain(l) for l in it["layers"][0][2]]
        if profile.info_mode == "near" and it.get("rect"):
            m = fonts.metrics(info_st.font)
            w = max(m.width(l, info_st.size) for l in lines)
            h = len(lines) * info_st.size * 1.25
            an, x, y, bx0, by0 = _info_spot(it, is_vertical(it["rect"], aspect), w, h, px, py, boxes)
            if profile.avoid_hardsub:
                boxes.append(_box(it["start"], it["end"], bx0, bx0 + w, by0, by0 + h))
            placed.append({"kind": "info", "item": it, "an": an, "x": x, "y": y})
        else:
            placed.append({"kind": "info", "item": it, "top": True})

    for it in sorted((i for i in items if i["kind"] == "main"), key=lambda i: i["start"]):
        rows = _rows(it, used)
        w = max((r["width"] for r in rows), default=0) + 12
        h = sum(r["height"] for r in rows)
        mv = avoid_margin(it, boxes, margin_v, px / 2 - w / 2, px / 2 + w / 2, h, py)
        placed.append({"kind": "main", "item": it, "margin_v": mv, "rows": rows, "ruby": any(r["ruby"] for r in rows)})
    return px, py, used, placed


def write(path, items, profile, styles: dict, aspect, hardsub_boxes, margin_v=46):
    """items 来自 layout.build。hardsub_boxes：[{start, end, rect, vertical}]，原画面硬字幕的位置。"""
    px, py, used, placed = place(items, profile, styles, aspect, hardsub_boxes, margin_v)
    out = [header(px, py, used, info_styles={profile.info_style})]
    for p in placed:
        it = p["item"]
        if p["kind"] == "info":
            text = esc("\n".join(plain(l) for l in it["layers"][0][2]))
            tag = "{\\an8}" if p.get("top") else f"{{\\an{p['an']}\\pos({p['x']},{p['y']})}}"
            out.append(f"Dialogue: 1,{ts(it['start'])},{ts(it['end'])},{profile.info_style},,0,0,0,,{tag}{text}")
            continue
        if p["ruby"]:
            out += _positioned(it, p["rows"], px, py - p["margin_v"])
            continue
        first = it["layers"][0][0].style
        parts = []
        for k, (ly, _, lines) in enumerate(it["layers"]):
            seg = esc("\n".join(plain(l) for l in lines))
            parts.append(seg if k == 0 else f"{{\\r{ly.style}}}{seg}")
        body = "\\N".join(parts)
        out.append(f"Dialogue: 0,{ts(it['start'])},{ts(it['end'])},{first},,0,0,{p['margin_v']},,{body}")
    _save(path, out)


def plain(line):
    return "".join(t for t, _ in line)


RUBY_GAP = 0.06  # 注音与底字的间距（底字字号的比例）


def _rows(it, used):
    """每一行的尺寸。带注音的行高 = 底字字号 + 注音字号 + 间距；块宽取底字和注音中较宽的。"""
    rows = []
    for ly, _, lines in it["layers"]:
        st = used[ly.style]
        m = fonts.metrics(st.font)
        rsize = st.size * ly.annotate.size
        for line in lines:
            chunks = []
            for i, (text, rt) in enumerate(line):
                bw = m.width(text, st.size)
                cw = bw
                if rt:
                    # 注音比底字宽时，可以向两侧的普通文字（假名等）各探出一个注音字宽，探不下的部分才撑宽底字；
                    # 两边挨着别的注音时不能探出，免得注音叠在一起
                    extra = m.width(rt, rsize) - bw
                    if extra > 0:
                        left_ok = i > 0 and not line[i - 1][1]
                        right_ok = i + 1 < len(line) and not line[i + 1][1]
                        allow = rsize if (left_ok and right_ok) else 0
                        cw = bw + max(0.0, extra - 2 * allow)
                chunks.append((text, rt, bw, cw))
            has_rt = any(rt for _, rt in line)
            rows.append({"ly": ly, "st": st, "chunks": chunks, "ruby": has_rt, "rsize": rsize,
                         "width": sum(c[3] for c in chunks),
                         "height": st.size + (rsize + st.size * RUBY_GAP if has_rt else 0)})
    return rows


def _positioned(it, rows, px, bottom):
    """逐块定位：底字和注音都用 \\an2 以同一个中心 x 定位，注音一定正好在底字上方；
    宽度只影响块与块之间的间距。没有注音的块用 \\an1 从左边定位。"""
    out = []
    t0, t1 = ts(it["start"]), ts(it["end"])
    y = bottom
    for r in reversed(rows):
        ly, st = r["ly"], r["st"]
        x = px / 2 - r["width"] / 2
        ruby_y = y - st.size - st.size * RUBY_GAP
        col = _inline_color(ly.annotate.color) if ly.annotate.color else ""
        bord = st.outline * ly.annotate.size
        for text, rt, bw, cw in r["chunks"]:
            body = esc(text).replace(" ", "\\h")
            if rt:
                cx = round(x + cw / 2)
                out.append(f"Dialogue: 0,{t0},{t1},{ly.style},ruby,0,0,0,,{{\\an2\\pos({cx},{round(y)})}}{body}")
                out.append(f"Dialogue: 0,{t0},{t1},{ly.style},ruby,0,0,0,,{{\\an2\\pos({cx},{round(ruby_y)})"
                           f"\\fs{r['rsize']:.0f}\\bord{bord:.1f}\\shad{st.shadow * ly.annotate.size:.1f}{col}}}{esc(rt)}")
            elif text:
                out.append(f"Dialogue: 0,{t0},{t1},{ly.style},ruby,0,0,0,,{{\\an1\\pos({round(x + (cw - bw) / 2)},{round(y)})}}{body}")
            x += cw
        y -= r["height"]
    return out


def _inline_color(hex_rgb):
    h = hex_rgb.lstrip("#")
    return f"\\c&H{h[4:6]}{h[2:4]}{h[0:2]}&".upper() if len(h) == 6 else ""


MERGED_COLORS = {"subs": "#FFFFFF", "telop": "#C8F0FF", "info": "#FFE680", "asr": "#B4B4B4"}


def write_merged(path, cues, styles: dict, aspect, hardsub_boxes):
    """原文合并字幕（不经过大模型）：白=字幕，浅蓝=画面台词，黄=画面说明，灰斜体=只有语音。"""
    py = 1080
    px = int(round(py * aspect))
    base = styles.get("orig_main") or styles["main"]
    used = {}
    for kind, col in MERGED_COLORS.items():
        used[kind] = base.model_copy(update={"color": col, "italic": kind == "asr",
                                             "size": base.size * (0.8 if kind == "info" else 1)})
    out = [header(px, py, used)]
    for c in sorted(cues, key=lambda c: c["start"]):
        kind = c.get("kind", "subs")
        text = esc(c["orig"]["text"])
        end = max(c["end"], c["start"] + 0.8)
        if kind == "info" and c.get("rect"):
            an, x, y = info_pos(c["rect"], is_vertical(c["rect"], aspect), px, py)
            text = f"{{\\an{an}\\pos({x},{y})}}" + text
        out.append(f"Dialogue: 0,{ts(c['start'])},{ts(end)},{kind},,0,0,0,,{text}")
    _save(path, out)


def _save(path, lines):
    with open(path, "w", encoding="utf-8-sig", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
