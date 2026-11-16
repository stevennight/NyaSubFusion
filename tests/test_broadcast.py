"""广播字幕原样：ARIB-TTML 转成带位置、颜色、背景、振假名的原文 ASS；封装进 MKV；压制时选压哪个字幕。"""
import json
import subprocess

import pytest

from nyasub import cli
from nyasub.render import broadcast, video
from nyasub.sources import subtitle

from . import fakellm

HEAD = ('<?xml version="1.0" encoding="UTF-8"?><tt xmlns="http://www.w3.org/ns/ttml" '
        'xmlns:arib-tt="http://www.arib.or.jp/ns/arib-ttml/v1_0" xmlns:tts="http://www.w3.org/ns/ttml#styling" xml:lang="ja">'
        '<head><styling><style xml:id="normal" tts:fontSize="144px 144px" tts:lineHeight="240px" arib-tt:letter-spacing="16px" />'
        '<style xml:id="middle" tts:fontSize="72px 144px" tts:lineHeight="240px" arib-tt:letter-spacing="8px" />'
        '<style xml:id="small" tts:fontSize="72px 72px" tts:lineHeight="120px" arib-tt:letter-spacing="8px" />'
        '<style xml:id="yellow" tts:color="#FFFF00FF" /><style xml:id="back" tts:backgroundColor="#00000080" /></styling>')


def doc(begin, end, body, regions):
    rg = "".join(f'<region xml:id="{k}" tts:extent="{w}px {h}px" tts:origin="{x}px {y}px" />' for k, (x, y, w, h) in regions.items())
    return (f'{HEAD}<layout>{rg}</layout></head><body><div begin="{begin}" end="{end}">{body}</div></body></tt>').encode("utf-8")


def ttml_file(tmp_path):
    ruby = '<p region="r1"><span style="small yellow back">いな</span></p>'
    line = ('<p region="r2"><span style="normal yellow back">伊那の川</span>'
            '<span style="middle yellow back">。</span></p>')
    regions = {"r1": (1192, 1676, 160, 120), "r2": (872, 1796, 720, 240)}
    data = (doc("00:00:01.000", "00:00:03.000", ruby + line, regions)
            + doc("00:00:03.000", "00:00:04.000", ruby + line, regions)   # 重复发送：并成一条
            + doc("00:00:05.000", "00:00:06.000", '<p region="r2"><span style="normal">次の字幕</span></p>', regions))
    p = tmp_path / "arib_ttml_2.bin"
    p.write_bytes(b"\x00garbage" + data)
    return p


def test_ttml_layout_kept(tmp_path):
    docs, canvas = broadcast.parse_ttml(ttml_file(tmp_path))
    assert canvas == (1920, 1080) or canvas == (3840, 2160)
    assert [(d["start"], d["end"]) for d in docs] == [(1.0, 4.0), (5.0, 6.0)]
    ruby, line = docs[0]["boxes"]
    assert (ruby["x"], ruby["y"], ruby["w"], ruby["h"]) == (1192, 1676, 160, 120) and ruby["spans"][0]["fh"] == 72
    assert [(s["text"], s["fw"], s["fh"], s["sp"]) for s in line["spans"]] == [("伊那の川", 144, 144, 16), ("。", 72, 144, 8)]
    assert line["bg"] == "#00000080" and line["spans"][0]["color"] == "#FFFF00FF"


def test_ttml_to_ass(tmp_path):
    out = tmp_path / "v.ja-broadcast.ass"
    assert broadcast.ttml_to_ass(ttml_file(tmp_path), out, offset=1.2, font="MS Gothic") == 2
    text = out.read_text(encoding="utf-8-sig")
    ev = [l for l in text.splitlines() if l.startswith("Dialogue:")]
    # 振假名：底色框 + 字，各在自己的位置；时间挪了 1.2 秒
    assert ev[0].startswith("Dialogue: 0,0:00:02.20,0:00:05.20,Broadcast") and "\\pos(1192,1676)\\p1" in ev[0]
    assert "m 0 0 l 160 0 160 120 0 120" in ev[0] and "\\1a&H7F&" in ev[0]   # 半透明黑底
    assert "\\pos(1192,1736)" in ev[1] and "いな" in ev[1] and "\\1c&H00FFFF&" in ev[1]   # 黄字
    assert "\\fscx50\\fsp8" in ev[3] and "\\fscx100\\fsp16" in ev[3]   # 半角的“。”只占半个字宽
    assert "\\bord" in ev[4] and "\\p1" not in ev[4]                    # 没有底色的加描边
    assert subtitle.is_own_output(str(out))                             # 不会再被当成外挂字幕读进来


def test_shift_libaribcaption_output(tmp_path):
    src = tmp_path / "styled.ass"
    fmt = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
           "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding")
    src.write_text("[Script Info]\nPlayResX: 960\n\n[V4+ Styles]\n" + fmt + "\n"
                   "Style: Default,BIZ UDGothic,36,&Hffffff,&Hffffff,&H0,&H0,0,0,0,0,100,100,0,0,4,0,4,2,10,10,10,0\n\n"
                   "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
                   "MarginV, Effect, Text\nDialogue: 0,0:00:00.50,0:00:02.00,Default,,0,0,0,,{\\an7}{\\pos(10,20)}{\\1c&H00ffff&}{\\3a&Hff&}字\n"
                   "Dialogue: 0,0:00:10.00,0:00:12.00,Default,,0,0,0,,次\n", encoding="utf-8")
    out = tmp_path / "x.ass"
    assert broadcast.shift_ass(src, out, -1.0) == 2
    text = out.read_text(encoding="utf-8-sig")
    assert "0:00:00.00,0:00:01.00" in text and "0:00:09.00,0:00:11.00" in text and subtitle.OWN_MARK in text
    # 底框：BorderStyle 4 + 阴影（VSFilter 画成黑影、libass 画成实心黑框）→ 各播放器都一样的半透明黑框
    assert "Style: Default,BIZ UDGothic,36,&Hffffff,&Hffffff,&H7F000000,&H7F000000,0,0,0,0,100,100,0,0,3,7,0,2,10,10,10,0" in text
    assert "\\3a" not in text and "{\\an7}{\\pos(10,20)}{\\1c&H00ffff&}字" in text and "ScaledBorderAndShadow: yes" in text


def test_libaribcaption_boxes_tile_without_overlap(tmp_path):
    """同一屏的注音、正文、下一行各有一个底框，挨得近的叠在一起颜色深一道：按两行字中间的线分开，伸到中线、超出的裁掉。"""
    src = tmp_path / "styled.ass"
    fmt = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
           "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding")
    t = "Dialogue: 0,0:01:28.90,0:01:36.00,Default,,0,0,0,,"
    src.write_text("[Script Info]\nPlayResX: 960\nPlayResY: 540\n\n[V4+ Styles]\n" + fmt + "\n"
                   "Style: Default,BIZ UDGothic,36,&Hffffff,&Hffffff,&H0,&H0,0,0,0,0,100,100,0,0,4,0,4,2,10,10,10,0\n\n"
                   "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                   + t + "{\\an7}{\\pos(138,359)}{\\fs18}{\\fsp2}{\\3a&Hff&}まるやまがわ\n"
                   + t + "{\\an7}{\\pos(138,389)}{\\fsp4}{\\3a&Hff&}円山川の流れる 豊岡市は\n"
                   + t + "{\\an7}{\\pos(158,449)}{\\fsp4}{\\3a&Hff&}日本有数の コウノトリの生息地です｡\n"
                   "Dialogue: 0,0:00:42.70,0:00:47.00,Default,,0,0,0,,{\\an7}{\\pos(178,449)}{\\fsp4}{\\3a&Hff&}冒険の始まりは\n",
                   encoding="utf-8")
    out = tmp_path / "x.ass"
    broadcast.shift_ass(src, out)
    ev = [ln for ln in out.read_text(encoding="utf-8-sig").splitlines() if ln.startswith("Dialogue")]
    assert "\\clip(0,352,960,383)" in ev[0]                       # 注音：上面照旧大出 7，下面到和正文之间的中线
    assert "\\ybord12\\clip(0,383,960,437)" in ev[1]               # 正文：上接注音，下接下一行
    assert "\\clip(0,437,960,492)" in ev[2]                       # 下一行：上接正文，下面照旧大出 7
    assert "\\clip" not in ev[3] and "\\ybord" not in ev[3]        # 单独一行：照样式四周大出 7


@pytest.fixture
def with_broadcast(sample, api_config, monkeypatch, tmp_path):
    """sample 跑完一遍，再假装字幕来自 4K 广播（项目里放一份 ARIB-TTML）。"""
    from nyasub.core import pipeline

    fakellm.install(monkeypatch)
    assert cli.main(["run", str(sample), "--no-asr", "--no-ocr", "--config", str(api_config)]) == 0
    bin_path = ttml_file(tmp_path)
    monkeypatch.setattr(pipeline.Run, "_broadcast_source", lambda self, info=None, guess=False: ("ttml", bin_path))
    return sample


def _run(sample, api_config, **video):
    from nyasub.core import config, pipeline

    cfg, _ = config.load(str(api_config), None, {"render": {"video": {"encoder": "libx264", "quality": 40, "height": 180, **video}}})
    return pipeline.Run(sample, cfg, pipeline.Options())


def streams(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
                         capture_output=True, check=True).stdout
    return json.loads(out)["streams"]


def test_mux_adds_broadcast_track(with_broadcast, api_config):
    r = _run(with_broadcast, api_config)
    r.export_video("mux")
    assert with_broadcast.with_name("sample.ja-broadcast.ass").exists()
    subs = [s for s in streams(with_broadcast.with_name("sample" + video.MUX_SUFFIX)) if s["codec_type"] == "subtitle"]
    assert subs[-1]["tags"]["title"].endswith("（广播字幕原样）") and subs[-1]["tags"]["language"] == "jpn"
    assert subs[0]["disposition"]["default"] == 1 and subs[-1]["disposition"]["default"] == 0
    assert "sample.ja-broadcast.ass" in r.written_names()
    # 没单独选过时关掉就不放；单独选了封装哪些，就按选的放（"broadcast" 也是一项）
    r = _run(with_broadcast, api_config, broadcast=False)
    assert all(t["profile"] != "broadcast" for t in r._video_tracks("mux"))
    first = next(p.name for p in r.cfg.render.profiles if p.enabled)
    r = _run(with_broadcast, api_config, broadcast=False, profiles=[first, "broadcast"])
    assert [t["profile"] for t in r._video_tracks("mux")] == [first, "broadcast"]


def test_mux_default_track(with_broadcast, api_config):
    """默认字幕轨可以指定（广播字幕原样也行）；指定的不在封装的字幕里就是第一条。"""
    r = _run(with_broadcast, api_config, default="broadcast")
    r.export_video("mux")
    subs = [s for s in streams(with_broadcast.with_name("sample" + video.MUX_SUFFIX)) if s["codec_type"] == "subtitle"]
    assert [s["disposition"]["default"] for s in subs] == [0] * (len(subs) - 1) + [1]
    assert subs[-1]["tags"]["title"].endswith("（广播字幕原样）")
    tracks = _run(with_broadcast, api_config, default="nope")._video_tracks("mux")
    assert [bool(t.get("default")) for t in tracks] == [True] + [False] * (len(tracks) - 1)


def test_burn_picks_subtitle(with_broadcast, api_config):
    r = _run(with_broadcast, api_config, burn="broadcast")
    r.load_state()
    r.export_video("burn")
    assert r.results["burn"]["tracks"][0]["profile"] == "broadcast"
    # SRT 方案也能压：按这个方案第一个图层的样式转成 ASS 再压
    srt = next(p.name for p in r.cfg.render.profiles if p.format == "srt")
    r = _run(with_broadcast, api_config)
    r.load_state()
    r.export_video("burn", [srt])
    assert r.results["burn"]["tracks"][0]["profile"] == srt
    assert "Dialogue:" in r.proj.media("burn_srt.ass").read_text(encoding="utf-8-sig")
    # 默认还是第一个 ASS 方案
    first_ass = next(p.name for p in r.cfg.render.profiles if p.format == "ass" and p.enabled)
    assert [t["profile"] for t in _run(with_broadcast, api_config)._video_tracks("burn")] == [first_ass]


# ---------- 还没运行就预先判断有没有广播字幕 ----------
ARIB = {"codec": "arib_caption", "sub_index": 0, "lang": "jpn"}


def _fresh(sample, api_config):
    (sample.parent / "sample.srt").unlink()   # 有外挂字幕时用外挂的，就不是广播字幕了
    return _run(sample, api_config)


def test_guess_arib_track_before_run(sample, api_config):
    r = _fresh(sample, api_config)
    assert r._broadcast_source({"subtitles": [ARIB], "data_streams": []}, guess=True) == ("arib", ARIB)
    assert r._broadcast_source({"subtitles": [], "data_streams": []}, guess=True) is None


def test_ttml_in_arib_caption_stream_is_parsed(sample, api_config, monkeypatch, tmp_path):
    """4K XML may be reported as an arib_caption subtitle stream instead of data."""
    from nyasub.sources import media
    from pathlib import Path

    r = _fresh(sample, api_config)
    source = ttml_file(tmp_path).read_bytes()
    r._subs_file = lambda: None

    def dump(_path, _index, out_path, probe_bytes=0, max_seconds=900):
        Path(out_path).write_bytes(source)
        return len(source)

    monkeypatch.setattr(media, "dump_data_stream", dump)
    monkeypatch.setattr(media, "extract_sub_stream", lambda *_: pytest.fail("should parse XML directly"))
    info = {
        "video": {"height": 2160},
        "subtitles": [{"index": 2, "sub_index": 0, "codec": "unknown", "lang": "jpn"}],
        "data_streams": [],
    }
    cues = r._load_subs(info)
    assert len(cues) == 2 and cues[0]["text"] == "\u4f0a\u90a3\u306e\u5ddd\u3002"


def test_guess_ttml_needs_peek(sample, api_config, monkeypatch):
    from nyasub.sources import media

    r = _fresh(sample, api_config)
    info = {"subtitles": [], "data_streams": [1, 2]}
    assert r._broadcast_source(info, guess=True) == ("peek", None)
    assert r._broadcast_source(info) is None                 # 不是预先判断时照旧（运行时靠导出的数据）
    seen = []
    monkeypatch.setattr(media, "peek_data_stream", lambda path, idx, test: seen.append(idx) or test(b"x arib-ttml x" if idx == 2 else b"plain data"))
    assert r.peek_ttml(info) == 2 and seen == [1, 2]
    assert r._broadcast_source(info, guess=True) == ("ttml", None)
    monkeypatch.setattr(media, "peek_data_stream", lambda path, idx, test: False)
    assert r.peek_ttml(info) is None and r._broadcast_source(info, guess=True) is None


def test_peek_data_stream_stops_early(sample):
    from nyasub.sources import media

    sizes = []
    assert media.peek_data_stream(str(sample), 0, lambda d: sizes.append(len(d)) or len(d) > 0)
    assert len(sizes) == 1                                    # 一看到就停
    assert not media.peek_data_stream(str(sample), 0, lambda d: False, limit=200_000)


def test_summary_checks_broadcast_in_background(sample, monkeypatch):
    import time

    from fastapi.testclient import TestClient

    from nyasub.server.app import create_app
    from nyasub.sources import media

    (sample.parent / "sample.srt").unlink()
    probe = media.probe
    monkeypatch.setattr(media, "probe", lambda p: {**probe(p), "subtitles": [], "data_streams": [5]})
    monkeypatch.setattr(media, "peek_data_stream", lambda path, idx, test: time.sleep(0.3) or test(b"<tt xmlns='http://www.w3.org/ns/ttml'>"))
    with TestClient(create_app()) as c:
        pid = c.post("/api/projects", json={"path": str(sample)}).json()["id"]
        s = c.get(f"/api/projects/{pid}").json()
        assert s["broadcast_check"] == "checking" and s["broadcast"] is None
        for _ in range(50):
            s = c.get(f"/api/projects/{pid}").json()
            if s["broadcast_check"] == "done":
                break
            time.sleep(0.1)
        assert s["broadcast_check"] == "done" and s["broadcast"] == {"source": "ttml", "file": None}
