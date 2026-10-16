"""输出 SRT：图层按顺序一行行叠起来；注音写成 漢字(かんじ)；说明类加 {\\an8} 放顶部（多数播放器支持）。"""


def ts(t):
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write(path, items):
    out, n = [], 0
    for it in sorted(items, key=lambda i: i["start"]):
        lines = ["".join(t + (f"({rt})" if rt and ly.annotate.srt == "paren" else "") for t, rt in l)
                 for ly, _, ls in it["layers"] for l in ls]
        if not lines:
            continue
        n += 1
        text = "\n".join(lines)
        if it["kind"] == "info":
            text = "{\\an8}" + text
        out.append(f"{n}\n{ts(it['start'])} --> {ts(max(it['end'], it['start'] + 0.8))}\n{text}\n")
    with open(path, "w", encoding="utf-8-sig", newline="\n") as f:
        f.write("\n".join(out))
