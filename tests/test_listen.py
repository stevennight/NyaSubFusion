"""参考语音（听音标注）：分段、标注、写进翻译提示词；没开时提示词和指纹都和以前一样。"""
import copy
import re
import json
import wave

import pytest

from nyasub.core import config
from nyasub.llm import client, listen, prompts
from nyasub.llm.translate import listen_note


def cue(i, start, end, text, typ="dialogue", sources=("c1",)):
    return {"id": i, "start": start, "end": end, "type": typ, "sources": list(sources), "orig": {"text": text}, "tr": {}}


def test_windows_only_dialogue_split_by_gap_length_and_count():
    cues = [cue("q1", 0, 2, "a"), cue("q2", 3, 5, "b"), cue("q3", 4, 9, "旁白", typ="narration"),
            cue("q4", 20, 22, "c"),  # 隔了 15 秒：另起一段
            *[cue(f"r{k}", 30 + k * 2, 31 + k * 2, "x") for k in range(listen.MAX_ITEMS + 2)]]
    wins = listen.windows(cues)
    ids = [[c["id"] for c in w] for _, _, w in wins]
    assert ids[0] == ["q1", "q2"] and ids[1] == ["q4"]
    assert len(ids[2]) == listen.MAX_ITEMS and len(ids[3]) == 2
    assert all("q3" not in w for w in ids)
    a, b, _ = wins[0]
    assert a == 0.0 and b == pytest.approx(5 + listen.PAD)


def test_clean_note_keeps_heard_only_for_asr_lines():
    asr, sub = cue("q1", 0, 1, "歯ないよ。", sources=["a5"]), cue("q2", 0, 1, "あれなの", sources=["o3"])
    assert listen.clean_note({"kind": "陈述句", "tone": "随口回应", "heard": "は ないよ"}, asr) == \
        {"kind": "陈述", "tone": "随口回应", "heard": "は ないよ"}
    assert listen.clean_note({"kind": "陈述", "heard": "あれなん"}, sub) == {"kind": "陈述"}
    assert listen.clean_note({"kind": "", "heard": "歯ないよ"}, asr) == {}  # 听到的和原文一样


def test_prompt_unchanged_without_notes_and_shows_notes_when_present():
    c = cue("q92", 448.4, 451.6, "これを何回か濾したやつがあれなの")
    plain = prompts.cue_line(c)
    assert "〔" not in plain and listen_note([c]) == ""
    c["listen"] = {"kind": "陈述", "tone": "平静地讲解"}
    assert prompts.cue_line(c) == plain + "　〔听原声：陈述，平静地讲解〕"
    assert "听原声的标注" in listen_note([c])
    from nyasub.llm.fine import group_prompt
    text = group_prompt([c], 0, 0, "", "zh-Hans", "", [], [])
    assert "〔听原声：陈述，平静地讲解〕" in text and "听原声的标注" in text


def _cfg(listen_name="", listen_model=""):
    return config.Config.model_validate({"llm": {
        "translate": "relay", "listen": listen_name,
        "providers": [{"name": "relay", "base_url": "https://relay/v1", "model": "gpt-6-luna", "api_key": "k",
                       **({"listen_model": listen_model} if listen_model and not listen_name else {})},
                      {"name": "or", "base_url": "https://or/v1", "model": "x", "api_key": "k2",
                       **({"listen_model": listen_model} if listen_model and listen_name else {})}]}})


def test_listen_endpoint_needs_a_listen_model_or_a_chosen_provider():
    assert _cfg().endpoint("listen")["model"] == ""  # 翻译接口没填听音模型：没设
    assert _cfg(listen_model="gem").endpoint("listen")["model"] == "gem"
    ep = _cfg("or").endpoint("listen")  # 单独选了接口：没填听音模型就用它的模型
    assert (ep["base_url"], ep["model"], ep["key"]) == ("https://or/v1", "x", "k2")
    assert _cfg("or", "gem-pro").endpoint("listen")["model"] == "gem-pro"


@pytest.fixture
def wav(tmp_path):
    p = tmp_path / "a.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes(b"\0\0" * 16000 * 30)
    return p


def test_annotate_marks_cues_and_caches(tmp_path, wav, monkeypatch):
    sent = []

    def fake(self, system, user, images=None, audio=None):
        sent.append(audio)
        self.usage = {"prompt_tokens": 500, "prompt_tokens_details": {"audio_tokens": 300}}
        return {"notes": [{"id": "q1", "kind": "陈述", "tone": "平静地讲解"}, {"id": "q2", "kind": "问句"},
                          {"id": "zz", "kind": "问句"}]}
    monkeypatch.setattr(client.LLM, "_call", fake)
    cues = [cue("q1", 1, 3, "これを何回か濾したやつがあれなの"), cue("q2", 4, 5, "荒れてた？"), cue("q3", 5, 9, "旁白", typ="narration")]
    n = listen.annotate(cues, _cfg(listen_model="gem"), tmp_path / "cache", wav, tmp_path / "clips")
    assert n == 2 and cues[0]["listen"] == {"kind": "陈述", "tone": "平静地讲解"} and "listen" not in cues[2]
    assert len(sent) == 1 and sent[0][1] in ("mp3", "wav")
    for c in cues:
        c.pop("listen", None)
    assert listen.annotate(cues, _cfg(listen_model="gem"), tmp_path / "cache", wav, tmp_path / "clips") == 2
    assert len(sent) == 1  # 第二次读缓存


def test_annotate_discards_made_up_notes_when_audio_was_dropped(tmp_path, wav, monkeypatch):
    calls = []

    def fake(self, system, user, images=None, audio=None):
        calls.append(1)
        self.usage = {"prompt_tokens": 120, "prompt_tokens_details": {"audio_tokens": 0}}
        return {"notes": [{"id": "q1", "kind": "问句"}]}
    monkeypatch.setattr(client.LLM, "_call", fake)
    cues = [cue("q1", 1, 6, "これ"), cue("q2", 20, 25, "それ")]
    assert listen.annotate(cues, _cfg(listen_model="gem"), tmp_path / "cache", wav, tmp_path / "clips") == 0
    assert len(calls) == 1 and not any("listen" in c for c in cues)


def test_annotate_skips_without_listen_model(tmp_path, wav, monkeypatch):
    monkeypatch.setattr(client.LLM, "_call", lambda *a, **k: pytest.fail("不该发请求"))
    cues = [cue("q1", 1, 3, "これ")]
    assert listen.annotate(cues, _cfg(), tmp_path / "cache", wav, tmp_path / "clips") == 0


def test_translate_fingerprint_only_changes_when_listening_is_on(sample, monkeypatch):
    from nyasub.core import pipeline

    monkeypatch.setenv("NYASUB_TEST_KEY", "x")
    base = {"project": {"source": "ja"}, "llm": {"mode": "api", "model": "fake", "api_key_env": "NYASUB_TEST_KEY"}}
    run = lambda over: pipeline.Run(sample, config.load(None, None, config.deep_merge(copy.deepcopy(base), over))[0], pipeline.Options())
    r = run({})
    r.results["audio"] = {"wav": "media/audio.wav"}
    off = r._extra("translate")
    assert "listen" not in off  # 没开：指纹和以前一样，已有项目不会重新翻译
    r = run({"llm": {"mode": "api", "model": "fake", "api_key_env": "NYASUB_TEST_KEY", "listen_audio": True}})
    r.results["audio"] = {"wav": "media/audio.wav"}
    assert r._extra("translate") == off  # 开了但没设听音模型：不听，指纹也不变
    r = run({"llm": {"mode": "api", "listen_audio": True, "translate": "p", "providers": [
        {"name": "p", "base_url": "https://x/v1", "model": "fake", "listen_model": "gem", "api_key_env": "NYASUB_TEST_KEY"}]}})
    r.results["audio"] = {"wav": "media/audio.wav"}
    assert r._extra("translate")["listen"] == "gem"


def test_pipeline_listens_before_translating_and_notes_reach_prompt(sample, api_config, monkeypatch):
    """开了参考语音、设了听音模型：翻译前听对话原声，标注附在翻译提示词里，也存进结果。"""
    from nyasub import cli

    from . import fakellm

    fakellm.install(monkeypatch)
    heard, translated = [], []

    def call(self, system, user, images=None, audio=None):
        if audio:
            heard.append((self.model, user))
            self.usage = {"prompt_tokens_details": {"audio_tokens": 100}}
            return {"notes": [{"id": i, "kind": "陈述", "tone": "平静地讲解"} for i in re.findall(r"^\[(q\d+)\]", user, re.M)]}
        if '"items": [{"ids"' in system:
            translated.append(user)
        return fakellm.respond(system, user)
    monkeypatch.setattr(client.LLM, "_call", call)
    cfg = api_config.with_name("listen.toml")
    cfg.write_text(api_config.read_text(encoding="utf-8") + 'listen_audio = true\nlisten = "p"\n'
                   '[[llm.providers]]\nname = "p"\nbase_url = "https://x/v1"\nmodel = "fake"\nlisten_model = "gem"\n'
                   'api_key_env = "NYASUB_TEST_KEY"\n', encoding="utf-8")
    assert cli.main(["run", str(sample), "--no-asr", "--no-ocr", "--config", str(cfg)]) == 0
    data = json.loads((sample.with_name("sample.nyasub") / "stages" / "translate.json").read_text(encoding="utf-8"))
    talk = [c for c in data["cues"] if c["type"] == "dialogue"]
    assert heard and {m for m, _ in heard} == {"gem"}
    assert talk and all(c.get("listen") == {"kind": "陈述", "tone": "平静地讲解"} for c in talk)
    assert any("〔听原声：陈述，平静地讲解〕" in u and "听原声的标注" in u for u in translated)
