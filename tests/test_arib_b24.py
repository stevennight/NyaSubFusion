"""2K 广播字幕（ARIB STD-B24）：自己解码、按 NHK 的样子整理（符号、居中、振假名）。"""
import re

from nyasub.render import broadcast
from nyasub.sources import arib_b24, subtitle

SETUP = bytes([0x0C]) + b"\x9b960;540 V" + b"\x9b0;0 _" + b"\x9b36;36 W" + b"\x9b4 X" + b"\x9b24 Y"


def _aps(row, col):
    return bytes([0x1C, 0x40 + row, 0x40 + col])


def _kanji(text):
    """汉字 / 全角符号（G0 默认是汉字集，GL 调用）：JIS 码两个字节。"""
    return b"".join(bytes([b & 0x7F for b in ch.encode("euc_jp")]) for ch in text)


def _hira(text):
    """平假名（G2 默认是平假名集，GR 调用）：一个字节。"""
    return bytes(0xA1 + ord(ch) - 0x3041 for ch in text)


def _packet(body):
    unit = bytes([0x1F, 0x20]) + len(body).to_bytes(3, "big") + body
    data = bytes([0x00]) + len(unit).to_bytes(3, "big") + unit                # TMD + 数据单元
    group = bytes([0x04, 0x00, 0x00]) + len(data).to_bytes(2, "big") + data   # 字幕正文（A 组语言 1）
    return bytes([0x80, 0xFF, 0xF0]) + group + b"\x00\x00"


def test_decode_positions_and_geta():
    body = SETUP + _aps(7, 0) + _kanji("風") + bytes([0x0F]) + b"\x1b\x6e" + bytes(0x21 + ord(c) - 0x3041 for c in "のなかに") \
        + bytes([0x0F]) + _kanji("〓")
    st = arib_b24.decode([(9.8, _packet(body)), (12.5, _packet(bytes([0x0C])))])
    assert [s["t"] for s in st] == [9.8, 12.5]
    text = "".join(c["c"] for c in st[0]["chars"])
    assert text == "風のなかに〓"
    assert st[0]["chars"][0]["x"] == 0 and st[0]["chars"][0]["y"] == 7 * 60 and st[0]["chars"][1]["x"] == 40
    assert st[1]["chars"] == []


def test_only_degraded_data_is_rebuilt():
    # 从 4K 字幕转出来的：每行都从最左边写起、没有小字
    assert arib_b24.degraded(_screen([(6, "みこし"), (7, "総勢が神輿を担ぐ")]))
    # 原生的 2K 字幕：每行有自己的位置，振假名是小字（交给 libaribcaption）
    body = SETUP + _aps(7, 5) + _kanji("日本を代表する景勝地") + _aps(8, 3) + _kanji("栃木県日光です")
    assert not arib_b24.degraded(arib_b24.decode([(1.0, _packet(body))]))
    small = SETUP + bytes([0x88]) + _aps(13, 0) + _hira("にっこう") + bytes([0x8A]) + _aps(7, 0) + _kanji("日光")
    assert not arib_b24.degraded(arib_b24.decode([(1.0, _packet(small))]))


def test_fix_geta_by_position():
    assert arib_b24.fix_geta("風の中に〓") == "風の中に➡"
    assert arib_b24.fix_geta("〓〜") == "♬〜"
    assert arib_b24.fix_geta("〓\u3000遊ぼ\u3000遊ぼ") == "♬\u3000遊ぼ\u3000遊ぼ"
    assert arib_b24.fix_geta("〓おはようございま〜す。") == "⚟おはようございま〜す。"
    assert arib_b24.fix_geta("（掛け声）") == "（掛け声）"


def _screen(rows, t=50.0):
    """rows：[(行号, 文字)] 都从最左边写起（降过级的 2K 数据就是这样）。"""
    body = SETUP
    for row, text in rows:
        body += _aps(row, 0) + _kanji(text)
    return arib_b24.decode([(t, _packet(body))])


def test_reading_rows_become_furigana_real_lines_stay():
    st = _screen([(5, "すみだがわ"), (6, "せんじゅ"), (7, "隅田川と荒川に挟まれた　千住。")])
    sc = arib_b24.screens(st, 60.0)
    assert [ln["text"] for ln in sc[0]["lines"]] == ["隅田川と荒川に挟まれた　千住。"]
    line = sc[0]["lines"][0]["text"]
    assert {(r["text"], line[r["span"][0]:r["span"][1]]) for r in sc[0]["rubies"]} == {("すみだがわ", "隅田川"), ("せんじゅ", "千住")}
    # 对不上读音的假名行是真的台词：保留
    st = _screen([(6, "しかし"), (7, "バブル崩壊などで　飲食業が冷え込み〓")])
    sc = arib_b24.screens(st, 60.0)
    assert [ln["text"] for ln in sc[0]["lines"]] == ["しかし", "バブル崩壊などで　飲食業が冷え込み➡"] and not sc[0]["rubies"]
    # 一个词开头几个字的读音
    sc = arib_b24.screens(_screen([(6, "にっこう"), (7, "日光街道　最初の宿場町")]), 60.0)
    r = sc[0]["rubies"][0]
    assert sc[0]["lines"][0]["text"][r["span"][0]:r["span"][1]] == "日光"


def test_cues_and_broadcast_ass(tmp_path):
    st = _screen([(6, "みこし"), (7, "総勢２００人が神輿を担ぐ　秋祭りです。")]) + _screen([], t=55.8)
    sc = arib_b24.screens(st, 60.0)
    cues = subtitle.cues_from_screens(sc)
    assert [(c["start"], c["end"], c["text"]) for c in cues] == [(50.0, 55.8, "総勢２００人が神輿を担ぐ　秋祭りです。")]
    out = tmp_path / "b.ass"
    assert broadcast.screens_to_ass(sc, str(out)) == 1
    ev = [ln for ln in out.read_text(encoding="utf-8-sig").splitlines() if ln.startswith("Dialogue")]
    assert len(ev) == 2
    # 正文居中：19 个字、每格 40（放大一倍 80），左边 = (1920 - 19*80) / 2
    x = float(re.search(r"\\pos\(([\d.]+),", ev[0]).group(1))
    assert x == (1920 - 19 * 80) / 2
    # 振假名小一号（字号是正文的一半），中心在“神輿”（第 7、8 个字）上面
    fs = lambda e: float(re.search(r"\\fs([\d.]+)", e).group(1))
    assert "みこし" in ev[1] and fs(ev[1]) == fs(ev[0]) / 2
    rx = float(re.search(r"\\pos\(([\d.]+),", ev[1]).group(1))
    centre = rx + 3 * 40 / 2  # 三个小字，每个 20（放大一倍 40）
    assert x + 7 * 80 < centre < x + 9 * 80
