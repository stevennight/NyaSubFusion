"""云端语音识别（sources/asr_api.py）：切块、拼接去重、两种接口、只给文字时退回小段、出错 / 取消 / 缓存。

HTTP 全部用 httpx.MockTransport 模拟；切音频（ffmpeg）换成写一个记着“这是哪一段”的小文件，接口按它返回对应的台词。
"""
import base64
import json
import re
import threading
import wave

import httpx
import numpy as np
import pytest

from nyasub import env, secrets
from nyasub.core import config, pipeline
from nyasub.sources import asr, asr_api

SR = 16000
# 真实台词（绝对时间）：每句由几个字组成，字均匀分布在句子里
TRUTH = [(1.0, 3.0, "おはよう"), (5.0, 7.6, "今日は寒い"), (9.2, 11.0, "雪です"), (13.0, 16.0, "鍋を食べる"),
         (18.5, 20.5, "あったかい"), (22.0, 25.0, "冬の味"), (27.5, 29.0, "ほら"), (31.0, 34.5, "大根を干す"),
         (36.5, 38.0, "いいね"), (40.5, 43.0, "また来年"), (45.0, 47.5, "おわり")]
DURATION = 50.0


def _words(a, b, text):
    step = (b - a) / len(text)
    return [(ch, round(a + i * step, 3), round(a + (i + 1) * step, 3)) for i, ch in enumerate(text)]


@pytest.fixture
def wav(tmp_path):
    """台词的地方是噪声（有声音），其余安静。"""
    rng = np.random.default_rng(0)
    x = rng.normal(0, 0.002, int(SR * DURATION))
    for a, b, _ in TRUTH:
        x[int(a * SR): int(b * SR)] += rng.normal(0, 0.3, int(b * SR) - int(a * SR))
    path = tmp_path / "audio.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())
    return path


@pytest.fixture
def small(monkeypatch):
    """块切小：20 秒一块，在 [14, 20] 里找安静处切。"""
    monkeypatch.setattr(asr_api, "CHUNK", 20.0)
    monkeypatch.setattr(asr_api, "SEARCH", 6.0)
    monkeypatch.setattr(asr_api, "CLIP", 8.0)
    monkeypatch.setattr(asr_api, "PARALLEL", 2)
    monkeypatch.setattr(asr_api.time, "sleep", lambda s: None)

    def fake_encode(wav, a, b, out, fallback="flac"):
        path = out.with_suffix(".mp3")
        path.write_bytes(json.dumps({"a": a, "b": b}).encode())
        return path, "mp3"
    monkeypatch.setattr(asr_api, "_encode", fake_encode)


class Api:
    """模拟接口：按上传的“音频”（a, b）返回这段里的台词。"""

    def __init__(self, mode="verbose"):
        self.mode = mode          # verbose / words_only / text_only / segment_only / chat
        self.requests = []
        self.fail = []            # 依次返回的错误状态码
        self.prompt_tokens = None
        self.lock = threading.Lock()

    def handler(self, req: httpx.Request):
        with self.lock:
            self.requests.append(req)
            if self.fail:
                code = self.fail.pop(0)
                return httpx.Response(code, json={"error": {"message": f"error {code}"}})
        if req.url.path.endswith("/audio/transcriptions"):
            body = req.content.decode("utf-8", "replace")
            span = json.loads(re.search(r'\{"a": [^}]*\}', body).group(0))
            a, b = span["a"], span["b"]
            # Mistral Voxtral（经 OpenRouter）：要逐字时间就 400，而且不说原因
            if self.mode == "segment_only" and re.search(r'name="timestamp_granularities\[\]"\r\n\r\nword\r\n', body):
                return httpx.Response(400, json={"error": {"message": "Provider returned 400", "code": 400}})
            if self.mode == "text_only" and "verbose_json" in body:
                return httpx.Response(400, json={"error": {"message": "response_format 'verbose_json' is not compatible with model"}})
            inside = [t for t in TRUTH if a <= (t[0] + t[1]) / 2 < b]
            if self.mode == "text_only":
                return httpx.Response(200, json={"text": "".join(t[2] for t in inside)})
            words = [{"word": ch, "start": s - a, "end": e - a} for t in TRUTH for ch, s, e in _words(*t) if s >= a and e <= b]
            segs = [{"start": max(0, t[0] - a), "end": min(b, t[1]) - a, "text": t[2] + "。"} for t in TRUTH
                    if t[1] > a and t[0] < b]
            out = {"language": "japanese", "text": "", "words": words}
            if self.mode == "verbose":
                out["segments"] = segs
            if self.mode == "segment_only":
                out = {"language": "ja", "text": "", "segments": segs}
            return httpx.Response(200, json=out)
        assert req.url.path.endswith("/chat/completions")
        body = json.loads(req.content)
        audio = body["messages"][1]["content"][1]["input_audio"]
        span = json.loads(base64.b64decode(audio["data"]))
        a, b = span["a"], span["b"]
        mmss = lambda v: f"{int(v // 60):02d}:{v % 60:05.2f}"  # noqa: E731
        segs = [{"start": mmss(t[0] - a), "end": mmss(t[1] - a), "text": t[2]} for t in TRUTH if a <= (t[0] + t[1]) / 2 < b]
        tokens = self.prompt_tokens if self.prompt_tokens is not None else int((b - a) * 32) + 300
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"language": "ja", "segments": segs}, ensure_ascii=False)}}], "usage": {"prompt_tokens": tokens}})


@pytest.fixture
def api(monkeypatch, small):
    holder = {"api": Api()}
    monkeypatch.setattr(asr_api, "_client", lambda: httpx.Client(transport=httpx.MockTransport(
        lambda r: holder["api"].handler(r))))
    return holder


EP = {"name": "test", "base_url": "https://api.example.com/v1", "model": "gpt-transcribe", "key": "sk-test"}


def _run(wav, kind="transcribe", **kw):
    return asr_api.transcribe(str(wav), {"api_kind": kind}, dict(EP, model="m-" + kind), "ja", **kw)


# ---------------- 切块 ----------------
def test_plan_chunks_cuts_at_quiet_points_with_overlap():
    chunks = asr_api.plan_chunks(1500.0, lambda lo, hi: hi - 3)
    assert chunks == [(0.0, 598.0, 0.0, 597.0), (596.0, 1195.0, 597.0, 1194.0), (1193.0, 1500.0, 1194.0, 1500.0)]
    assert asr_api.plan_chunks(300.0, lambda lo, hi: hi) == [(0.0, 300.0, 0.0, 300.0)]  # 不到 10 分钟：一块


def test_wav_envelope_finds_silence(wav):
    w = asr_api.Wav(wav)
    assert w.duration == pytest.approx(DURATION)
    assert 16.0 < w.quiet(14.0, 18.0) < 18.0       # 鍋を食べる（13~16）之后的安静处
    assert w.active(3.5, 4.8) is None              # 句子之间：没声音，不用识别
    a, b = w.active(4.0, 8.5)
    assert a == pytest.approx(5.0, abs=0.06) and b == pytest.approx(7.6, abs=0.06)


# ---------------- 转写接口：逐词时间 ----------------
def test_verbose_json_words_stitched_without_duplicates(wav, api):
    events = []
    env.on_event(events.append)
    try:
        segs, lang = _run(wav, offset=0.5)
    finally:
        env.off_event(events.append)
    assert lang == "ja"
    assert [s["text"].rstrip("。") for s in segs] == [t[2] for t in TRUTH]  # 每句一次，不重复、不丢
    assert [s["id"] for s in segs] == [f"a{i + 1}" for i in range(len(TRUTH))]
    for s, (a, b, text) in zip(segs, TRUTH):
        assert s["timing"] == "word" and set(s) >= {"id", "start", "end", "text", "words"}
        assert s["start"] == pytest.approx(a + 0.5, abs=0.01) and s["end"] == pytest.approx(b + 0.5, abs=0.01)
        assert "".join(w["w"] for w in s["words"]) == text and s["words"][0]["s"] == pytest.approx(a + 0.5, abs=0.01)
    reqs = api["api"].requests
    assert len(reqs) == 3 and all(r.headers["authorization"] == "Bearer sk-test" for r in reqs)
    body = reqs[0].content.decode("utf-8", "replace")
    for field in ("m-transcribe", "verbose_json", "word", "segment"):
        assert field in body
    assert re.search(r'name="language"\r\n\r\nja', body)
    prog = [e for e in events if e["type"] == "progress"]
    assert prog[0]["done"] == 0 and prog[-1]["done"] == prog[-1]["total"] == DURATION
    assert asr.repair(segs) and asr._split_sentences(segs)  # 后处理照常能用


def test_words_only_response_is_split_by_repair(wav, api):
    api["api"].mode = "words_only"  # 只有逐词时间、没有句子：整块一段，靠 repair 按停顿拆开
    segs, _ = _run(wav)
    assert all(s["timing"] == "word" and s["words"] for s in segs)
    fixed = asr.repair(segs)
    assert ["".join(w["w"] for w in s["words"]) for s in fixed] == [t[2] for t in TRUTH]


def test_stitch_splits_words_at_cut_and_drops_duplicate_segments():
    w = lambda ch, s, e: {"w": ch, "s": s, "e": e}  # noqa: E731
    # 一句话跨过切点 10.0：前一块有它的前半，后一块（从 9.0 开始）有完整的一句
    left = [{"start": 8.0, "end": 10.6, "text": "あいうえ", "timing": "word",
             "words": [w("あ", 8.0, 8.6), w("い", 8.6, 9.2), w("う", 9.2, 9.9), w("え", 9.9, 10.6)]}]
    right = [{"start": 9.2, "end": 11.0, "text": "うえお", "timing": "word",
              "words": [w("う", 9.2, 9.9), w("え", 9.9, 10.6), w("お", 10.6, 11.0)]}]
    out = asr_api.stitch([(0.0, 10.0, left), (10.0, 20.0, right)])
    assert [(s["text"], s["start"], s["end"]) for s in out] == [("あいう", 8.0, 9.9), ("えお", 9.9, 11.0)]
    # 只有句子时间：两块都识别出同一句，时间稍有出入 → 只留一句（留字多的）
    a = [{"start": 9.4, "end": 10.7, "text": "雪です", "words": [], "timing": "segment"}]
    b = [{"start": 9.6, "end": 10.2, "text": "雪で", "words": [], "timing": "segment"},
         {"start": 12.0, "end": 13.0, "text": "はい", "words": [], "timing": "segment"}]
    out = asr_api.stitch([(0.0, 10.0, a), (10.0, 20.0, b)])
    assert [s["text"] for s in out] == ["雪です", "はい"]


# ---------------- 只给文字的接口：退回小段 ----------------
def test_text_only_api_falls_back_to_short_clips(wav, api):
    api["api"].mode = "text_only"
    segs, _ = _run(wav)
    assert "".join(s["text"] for s in segs) == "".join(t[2] for t in TRUTH)
    assert all(s["timing"] == "clip" and s["words"] == [] for s in segs)
    assert all(s["end"] - s["start"] <= asr_api.CLIP + 0.5 for s in segs)
    for a, b, text in TRUTH:  # 每句落在某一小段里，小段的时间就在这句附近
        s = next(s for s in segs if text in s["text"])
        assert s["start"] <= a + 0.1 and s["end"] >= b - 0.1
    bodies = [r.content.decode("utf-8", "replace") for r in api["api"].requests]
    assert sum("verbose_json" in b for b in bodies) == 1  # 第一块试过一次就不再要时间戳


# ---------------- 多模态大模型 ----------------
def test_chat_audio_segments_with_mmss_times(wav, api):
    api["api"].mode = "chat"
    segs, lang = _run(wav, kind="gemini")
    assert lang == "ja"
    assert [s["text"] for s in segs] == [t[2] for t in TRUTH]
    for s, (a, b, _) in zip(segs, TRUTH):
        assert s["timing"] == "segment" and s["words"] == []  # 不编造逐词时间
        assert s["start"] == pytest.approx(a, abs=0.01) and s["end"] == pytest.approx(b, abs=0.01)
    body = json.loads(api["api"].requests[0].content)
    assert body["model"] == "m-gemini" and body["messages"][1]["content"][1]["input_audio"]["format"] == "mp3"


def test_chat_audio_dropped_by_relay_is_an_error(wav, api, monkeypatch):
    monkeypatch.setattr(asr_api, "GUARD_SECONDS", 10.0)
    api["api"].mode = "chat"
    api["api"].prompt_tokens = 409  # 实测的中转：音频被丢了，只计了提示词的 token，模型什么也不给 / 在编
    with pytest.raises(asr_api.AsrApiError, match="没有收到音频"):
        _run(wav, kind="gemini")


# ---------------- 出错、重试、取消、缓存 ----------------
def test_bad_key_is_fatal_and_stops_after_first_chunk(wav, api):
    api["api"].fail = [401]
    with pytest.raises(asr_api.AsrApiError, match="API Key"):
        _run(wav)
    assert len(api["api"].requests) == 1


def test_transient_errors_are_retried(wav, api):
    api["api"].fail = [503, 429]
    segs, _ = _run(wav)
    assert len(segs) == len(TRUTH) and len(api["api"].requests) == 5


def test_cancel_stops_transcription(wav, api):
    ev = threading.Event()
    env.set_cancel(ev)
    orig = api["api"].handler

    def handler(req):
        ev.set()  # 第一次请求时按了停止
        return orig(req)
    api["api"].handler = handler
    try:
        with pytest.raises(env.Cancelled):
            _run(wav)
    finally:
        env.set_cancel(None)
    assert len(api["api"].requests) <= 2


def test_results_are_cached_per_chunk(wav, api, tmp_path):
    first, _ = _run(wav, cache_dir=tmp_path / "cache")
    n = len(api["api"].requests)
    again, _ = _run(wav, cache_dir=tmp_path / "cache")
    assert again == first and len(api["api"].requests) == n


def test_missing_key_is_reported(wav):
    with pytest.raises(asr_api.AsrApiError, match="API Key"):
        asr_api.transcribe(str(wav), {"api_kind": "transcribe"}, dict(EP, key=""), "ja")


# ---------------- 配置 / 流水线 ----------------
def test_endpoint_for_asr_reuses_llm_providers(isolated_home):
    secrets.set("oai", "sk-oai")
    secrets.set("gem", "sk-gem")
    llm = {"providers": [{"name": "oai", "base_url": "https://api.openai.com/v1", "model": "gpt-x"},
                         {"name": "gem", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "model": "g"}],
           "translate": "oai"}
    cfg = config.Config.model_validate({"llm": llm, "asr": {"backend": "api"}})
    assert cfg.endpoint("asr") == {"name": "oai", "base_url": "https://api.openai.com/v1", "model": "gpt-transcribe", "key": "sk-oai"}
    cfg = config.Config.model_validate({"llm": llm, "asr": {"backend": "api", "api_kind": "gemini", "api_provider": "gem", "api_model": ""}})
    ep = cfg.endpoint("asr")
    assert (ep["name"], ep["model"], ep["key"]) == ("gem", "gemini-2.5-flash", "sk-gem")
    assert cfg.endpoint()["model"] == "gpt-x"  # 翻译不受影响


def test_pipeline_uses_api_backend_and_keeps_local_fingerprint(sample, monkeypatch):
    calls = []

    def fake(wav, c, ep, language=None, offset=0.0, duration=0.0, junk=(), cache_dir=None):
        calls.append((c["backend"], ep["model"], language, str(cache_dir)))
        return [{"id": "a1", "start": 1.0, "end": 2.0, "text": "テスト", "words": [], "timing": "segment"}], "ja"
    monkeypatch.setattr(asr_api, "transcribe", fake)
    local, _ = config.load(None, None, {"project": {"source": "ja"}, "asr": {"separate": "off"}})
    r = pipeline.Run(sample, local, pipeline.Options())
    assert r._extra("asr") is None  # 本机识别：指纹和以前一样，已有项目不会重新识别
    cfg, _ = config.load(None, None, {"project": {"source": "ja"}, "ocr": {"enabled": False},
                                      "asr": {"backend": "api", "separate": "off", "api_model": "whisper-1"}})
    r = pipeline.Run(sample, cfg, pipeline.Options(until="asr"))
    assert r._extra("asr") == {"api": {"kind": "transcribe", "base_url": "https://api.openai.com/v1", "model": "whisper-1",
                                       "chunk": 600.0}}  # 每块时长也算进指纹：改了要重新识别
    r.run()
    out = json.loads((sample.with_name("sample.nyasub") / "stages" / "asr.json").read_text(encoding="utf-8"))
    assert out["segments"][0]["text"] == "テスト" and out["language"] == "ja"
    assert calls and calls[0][:3] == ("api", "whisper-1", "ja") and calls[0][3].endswith("llm_cache")


def test_chunk_length_setting():
    from nyasub.sources.asr_api import chunk_seconds, plan_chunks

    orouter = {"base_url": "https://openrouter.ai/api/v1"}
    openai = {"base_url": "https://api.openai.com/v1"}
    assert chunk_seconds({}, orouter) == 180.0       # 自动：OpenRouter 上游 60 秒超时，切短
    assert chunk_seconds({}, openai) == 600.0
    assert chunk_seconds({"api_chunk_seconds": 120}, openai) == 120.0   # 手动设置优先
    assert chunk_seconds({"api_chunk_seconds": 5}, openai) == 30.0      # 太短 / 太长都夹到范围里
    assert chunk_seconds({"api_chunk_seconds": 99999}, openai) == 1500.0
    # 30 分钟的音频按 3 分钟切：10 块左右，每块不超过 3 分钟（加重叠）
    chunks = plan_chunks(1800.0, lambda lo, hi: hi, 180.0, None, 45.0)
    assert 10 <= len(chunks) <= 11 and all(b - a <= 182.0 for a, b, _, _ in chunks)


def test_word_timing_rejected_falls_back_to_segment_timing(wav, api):
    # OpenRouter + Voxtral：要逐字时间就“Provider returned 400”；改要逐句时间后正常，而且后面的块不再先失败一次
    api["api"].mode = "segment_only"
    segs, _ = _run(wav)
    assert "".join(s["text"] for s in segs).replace("。", "") == "".join(t[2] for t in TRUTH)
    assert all(s["words"] == [] for s in segs)  # 不编造逐字时间
    bodies = [r.content.decode("utf-8", "replace") for r in api["api"].requests]
    with_word = [b for b in bodies if re.search(r'name="timestamp_granularities\[\]"\r\n\r\nword\r\n', b)]
    spans = {re.search(r'\{"a": [^}]*\}', x).group(0) for x in bodies}
    assert len(with_word) == 1 and len(bodies) == len(spans) + 1  # 只有第一次试逐字被拒，之后每块直接要逐句


def test_subtitle_offset_estimated_from_sentence_timing():
    from nyasub.fusion.align import estimate_offset

    # 字幕整体比语音早 1 秒；语音只有逐句时间（没有逐字）时也能估出来
    lines = ["今日はいい天気ですね", "散歩に行きましょうか", "川の水が冷たいです", "鍋を食べるのが楽しみ", "雪がたくさん降りました",
             "子供たちが遊んでいる", "夕方には帰りましょう", "お茶をいれてください", "山の上は寒いですね", "明日も晴れるといいな"] * 2
    speech = [{"start": 5.0 * i + 1.0, "end": 5.0 * i + 4.0, "text": t, "words": []} for i, t in enumerate(lines)]
    subs = [{"start": 5.0 * i, "end": 5.0 * i + 3.0, "text": t} for i, t in enumerate(lines)]
    off, gain = estimate_offset(subs, speech)
    assert abs(off - 1.0) <= 0.3 and gain > 0.05
