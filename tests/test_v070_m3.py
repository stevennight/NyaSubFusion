"""v0.7.0 M3 测试：语音辅助多音字注音（listen 提取 readings，annotate 按 cue 粒度覆盖且人工词典优先）。"""
from nyasub.core.config import Config
from nyasub.core.pipeline import Options, Run
from nyasub.llm import listen


def test_listen_clean_note_extracts_valid_readings_and_covers_monologue_and_song():
    c_dlg = {"id": "q1", "start": 1.0, "end": 3.0, "type": "dialogue", "sources": ["c1"],
             "orig": {"lang": "ja", "text": "明日の希望を歌うよ"}}
    c_mono = {"id": "q2", "start": 4.0, "end": 6.0, "type": "monologue", "sources": ["a2"],
              "orig": {"lang": "ja", "text": "あの時、俺たちは"}}
    c_song = {"id": "q3", "start": 7.0, "end": 10.0, "type": "song", "sources": ["a3"],
              "orig": {"lang": "ja", "text": "生命への神秘を覚え"}}

    wins = listen.windows([c_dlg, c_mono, c_song])
    assert len(wins) == 1 and [c["id"] for c in wins[0][2]] == ["q1", "q2", "q3"]

    note = listen.clean_note({
        "kind": "陈述",
        "tone": "激昂",
        "readings": [
            {"src": "明日", "reading": "あす"},
            {"src": "不存在的词", "reading": "なに"},
            {"src": "歌う", "reading": "歌う"},
        ],
    }, c_dlg)
    assert note["readings"] == [{"src": "明日", "reading": "あす"}]
    # note_text 给翻译看时不混入 readings 字段
    assert "あす" not in listen.note_text({"listen": note})


def test_annotate_uses_per_cue_audio_readings_and_respects_user_dict_priority(tmp_path):
    """同一集里不同 cue 对同一多音字「明日」可按各自语音分别注音（あした vs あす），且人工词典优先级最高。"""
    vid = tmp_path / "ep1.mp4"
    vid.write_bytes(b"")
    cfg = Config()
    cfg.project.source = "ja"
    run = Run(vid, cfg, Options())
    run.src = "ja"
    run.results = {"ocr_refine": []}

    translated = {
        "glossary": {"terms": [{"src": "生命", "reading": "せいめい", "tr": "生命"}]},
        "cues": [
            {"id": "q1", "start": 0.0, "end": 2.0, "type": "dialogue", "sources": ["a1"], "onscreen": False,
             "orig": {"lang": "ja", "text": "明日また来ます"}, "tr": {"zh-Hans": {"text": "明天再来"}},
             "flags": [], "review": "ok",
             "listen": {"readings": [{"src": "明日", "reading": "あした"}]}},
            {"id": "q2", "start": 3.0, "end": 6.0, "type": "song", "sources": ["a2"], "onscreen": False,
             "orig": {"lang": "ja", "text": "明日の希望と生命"}, "tr": {"zh-Hans": {"text": "明日的希望与生命"}},
             "flags": [], "review": "ok",
             "listen": {"readings": [{"src": "明日", "reading": "あす"}, {"src": "生命", "reading": "いのち"}]}},
        ],
    }

    res = run._annotate(translated)
    w1 = {w["t"]: w for w in res["tokens"]["q1"]["orig"]}
    w2 = {w["t"]: w for w in res["tokens"]["q2"]["orig"]}
    assert w1["明日"]["segs"] == [["明日", "あした"]] and w1["明日"]["src"] == "audio"
    assert w2["明日"]["segs"] == [["明日", "あす"]] and w2["明日"]["src"] == "audio"
    # 语音听出的「いのち」优先级高于术语表默认「せいめい」
    assert w2["生命"]["segs"] == [["生命", "いのち"]] and w2["生命"]["src"] == "audio"


def test_arib_ttml_recovers_broadcast_furigana_hints(tmp_path):
    """4K 广播 ARIB-TTML 字幕里的振假名小字不再白白丢弃，能按坐标投影回收为官方读音提示。"""
    from nyasub.sources.subtitle import load_arib_ttml

    ttml = """<?xml version="1.0" encoding="UTF-8"?>
<tt xmlns="http://www.w3.org/ns/ttml" xmlns:tts="http://www.w3.org/ns/ttml#styling" xml:lang="ja">
  <head>
    <styling>
      <style xml:id="body" tts:fontSize="100px" tts:color="#FFFFFFFF"/>
      <style xml:id="ruby" tts:fontSize="50px" tts:color="#FFFFFFFF"/>
    </styling>
    <layout>
      <region xml:id="r_body" tts:origin="200px 800px" tts:extent="600px 120px"/>
      <region xml:id="r_ruby" tts:origin="200px 740px" tts:extent="200px 60px"/>
    </layout>
  </head>
  <body>
    <div begin="00:00:01.000" end="00:00:04.000">
      <p region="r_ruby"><span style="ruby">そら</span></p>
      <p region="r_body"><span style="body">宇宙の果て</span></p>
    </div>
  </body>
</tt>"""
    p = tmp_path / "arib.ttml"
    p.write_text(ttml, encoding="utf-8")
    cues = load_arib_ttml(str(p))
    assert len(cues) == 1
    assert cues[0]["text"] == "宇宙の果て"
    assert cues[0].get("ruby_hints") == [{"src": "宇宙", "reading": "そら"}]
