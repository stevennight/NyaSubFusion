"""语音识别：云端接口（可选后端，代替本地 faster-whisper；按音频时长付费）。

两种接口（asr.api_kind）：
- transcribe：OpenAI 兼容的 /v1/audio/transcriptions（gpt-transcribe、whisper-1 和各家兼容实现）。
  先要 verbose_json + 逐词 / 逐句时间；接口只肯给文字时，改成在安静处切成 ≤25 秒的小段逐段识别，
  这时一段的时间就是那一小段里有声音的部分。
- gemini：多模态大模型（Gemini 等）听音频，走 OpenAI 兼容的 chat/completions + input_audio，让它按句给时间。
  有的中转会悄悄丢掉音频、让模型凭空编一段（只计了几十个输入 token），按输入 token 数检查出来就报错。

产出的段和本地 Whisper 一样：{"id","start","end","text","words":[{"w","s","e"}]}，后处理（asr.repair 等）照常用。
接口不给逐词时间时 words 为空，并在段上记 timing："word"（有逐词时间）/ "segment"（接口给的句子时间）/
"clip"（只知道在哪一小段里）。不编造逐词时间。

音频按 10 分钟左右切块（在切点附近找最安静的地方），块之间重叠 2 秒；拼接时以切点为界，
每段 / 每个字按中点归到前后某一块，再把切点附近文字几乎一样的重复段去掉。
每块的结果按内容缓存在项目的 llm_cache/ 里：中途失败重跑时，已识别的块不再花钱。
"""
import base64
import hashlib
import json
import re
import subprocess
import tempfile
import threading
import time
import wave
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import numpy as np

from .. import env
from ..env import Cancelled, check_cancel, log, progress

CHUNK = 600.0       # 每块大约这么长（接口有文件大小上限：OpenAI 25 MB，10 分钟 64 kbps 的 mp3 不到 5 MB）
CHUNK_SHORT = 180.0  # 处理时间有上限的接口（OpenRouter：上游 60 秒超时）默认切短一些
CHUNK_RANGE = (30.0, 1500.0)
OVERLAP = 2.0       # 相邻两块重叠这么长
SEARCH = 30.0       # 切点在 [目标 - SEARCH, 目标] 里找最安静的地方，免得切在一个字中间
CLIP = 25.0         # 只给文字的接口：切成不超过这么长的小段
PARALLEL = 3        # 同时上传几块
RETRIES = 4
TIMEOUT = 600.0
HOP = 0.05          # 音量按 50 毫秒一帧算
PAUSE = 0.8         # 只有逐词时间时：字和字之间停顿超过这么久就断句
DEFAULT_MODEL = {"transcribe": "gpt-transcribe", "gemini": "gemini-2.5-flash"}
# 多模态接口：每秒音频至少该计这么多输入 token（Gemini 32 / GPT 约 10）。输入 token 只比提示词本身（按每字 1.5 token 估）
# 多出这么一点，说明音频被中转丢了（实测：40 秒音频 + 350 字提示只计了 409 个）。太短的块不查，免得误报
MIN_AUDIO_TOKENS = 2.0
GUARD_SECONDS = 30.0
# 多模态大模型的输出上限（max_tokens）：基数 + 每秒音频这么多。不给上限时 OpenRouter 按模型的最大输出预扣费用
# （gemini-4-argon 约 94 万 token ≈ 9 美元一次），几块同时传就报 402 in_flight_budget_exhausted。
# 实测对话最密的 3 分钟输出 2532 token（每秒 14 个），这个上限留了 5 倍以上（会先思考的模型，思考也算在里面）；
# 只按实际输出收费，上限只影响预扣。万一还是写满被截断，这一块对半切开重新识别
OUT_BASE = 8000
OUT_PER_SEC = 80
SPLIT_MIN = 20.0    # 被截断的块短于这么长就不再切，报错
LANG_NAMES = {"japanese": "ja", "chinese": "zh", "english": "en", "korean": "ko", "french": "fr", "german": "de",
              "spanish": "es", "russian": "ru", "italian": "it", "portuguese": "pt", "thai": "th", "vietnamese": "vi",
              "cantonese": "yue", "indonesian": "id"}
PROMPT_V = 1


class AsrApiError(RuntimeError):
    """接口配置 / 调用错误（不重试）。"""


class _NoTimestamps(Exception):
    """接口不支持 verbose_json / 时间戳，只肯给文字。"""


class _Truncated(Exception):
    """多模态大模型写到输出上限被截断（finish_reason = length）：这一块切短重新识别。"""


class _Retry(Exception):
    """临时错误（限流、服务端错误、网络），等一会儿重试。"""


class _Rejected(AsrApiError):
    """400 但没说原因（OpenRouter 转发上游错误时只有“Provider returned 400”）：可能是某个参数上游不支持，换一种请求再试。"""


# 逐字时间不是每家都支持（Mistral Voxtral 只给逐句时间，要 word 就 400）：按顺序试，试出能用的记住，后面的块直接用
GRANULARITIES = (["word", "segment"], ["segment"])
_gran_ok: dict = {}


# ---------------- 音量包络：找切点、找小段里有声音的部分 ----------------
class Wav:
    def __init__(self, path):
        with wave.open(str(path)) as w:
            if w.getsampwidth() != 2:
                raise AsrApiError(f"只支持 16 位 WAV：{path}")
            self.sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
            hop = int(self.sr * HOP)
            parts = []
            while True:
                buf = w.readframes(hop * 400)
                if not buf:
                    break
                x = np.frombuffer(buf, np.int16).astype(np.float32) / 32768
                if ch > 1:
                    x = x[: len(x) // ch * ch].reshape(-1, ch).mean(axis=1)
                m = len(x) // hop
                if m:
                    parts.append(np.sqrt(np.mean(x[: m * hop].reshape(m, hop) ** 2, axis=1) + 1e-12))
        self.duration = n / self.sr
        self.db = 20 * np.log10(np.concatenate(parts)) if parts else np.full(1, -120.0)
        self.floor = float(np.percentile(self.db, 20))

    def _range(self, lo, hi):
        return max(0, int(lo / HOP)), min(len(self.db), max(int(hi / HOP), int(lo / HOP) + 1))

    def quiet(self, lo, hi):
        """[lo, hi] 里最安静的时间点（按 0.3 秒平滑，找的是一小段安静，不是一帧）。"""
        i, j = self._range(lo, hi)
        if j - i < 2:
            return hi
        seg = self.db[i:j]
        k = min(6, len(seg))
        smooth = np.convolve(seg, np.ones(k) / k, mode="same")
        return round((i + int(np.argmin(smooth)) + 0.5) * HOP, 3)

    def active(self, lo, hi):
        """[lo, hi] 里有声音的部分 (开始, 结束)；几乎没声音时返回 None（这一小段不用花钱识别）。"""
        i, j = self._range(lo, hi)
        loud = np.nonzero(self.db[i:j] > max(-50.0, self.floor + 12))[0]
        if len(loud) * HOP < 0.3:
            return None
        return round((i + loud[0]) * HOP, 3), round((i + loud[-1] + 1) * HOP, 3)


def plan_chunks(duration, quiet, chunk=None, overlap=None, search=None):
    """切块：[(上传的开始, 上传的结束, 归这块的开始, 归这块的结束)]。切点选在安静处，块之间重叠 overlap 秒。"""
    chunk, overlap, search = chunk or CHUNK, OVERLAP if overlap is None else overlap, search or SEARCH
    cuts = [0.0]
    while duration - cuts[-1] > chunk:
        target = cuts[-1] + chunk
        c = quiet(max(cuts[-1] + chunk / 2, target - search), target)
        cuts.append(c if cuts[-1] < c < duration else target)
    cuts.append(duration)
    h = overlap / 2
    return [(round(max(0.0, a - h), 3), round(min(duration, b + h), 3), a, b) for a, b in zip(cuts, cuts[1:])]


# ---------------- 切音频 ----------------
def _encode(wav, a, b, out, fallback="flac"):
    """把 [a, b] 这段编码成 mp3（小）；没有 mp3 编码器时用 fallback（flac / wav）。返回 (文件, 格式)。"""
    for f, args in (("mp3", ["-c:a", "libmp3lame", "-b:a", "64k"]), (fallback, [])):
        path = Path(out).with_suffix("." + f)
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(wav),
                            "-ac", "1", "-ar", "16000", *args, str(path)], capture_output=True)
        if r.returncode == 0:
            return path, f
    raise AsrApiError(f"切音频失败：{r.stderr.decode(errors='replace')[-300:]}")


# ---------------- 接口调用 ----------------
def _client():
    import httpx

    return httpx.Client(timeout=TIMEOUT)


def _url(ep, path):
    return ep["base_url"].rstrip("/") + path


def _check(r):
    """按状态码分类：可重试 / 不支持时间戳 / 致命错误。"""
    if r.status_code < 400:
        return
    msg = r.text[:400].replace("\n", " ")
    if r.status_code in (408, 409, 429) or r.status_code >= 500:
        raise _Retry(f"{r.status_code} {msg}")
    if r.status_code == 402 and "in_flight" in msg:
        # OpenRouter：同时在处理的几个请求预扣的费用加起来超过余额（不是余额用完了），等前面的处理完再试
        raise _Retry(f"{r.status_code} {msg}")
    if r.status_code == 400 and re.search(r"response_format|timestamp|verbose_json|granularit", msg, re.I):
        raise _NoTimestamps(msg)
    hint = {401: "API Key 不对", 403: "没有权限", 404: "接口地址或模型不存在（这个接口可能不支持音频转写）",
            413: "音频太大"}.get(r.status_code, "请求被拒绝")
    err = _Rejected if r.status_code == 400 else AsrApiError
    raise err(f"语音识别接口报错（{r.status_code}，{hint}）：{msg}")


def _with_retry(fn):
    import httpx

    for attempt in range(RETRIES + 1):
        check_cancel()
        try:
            return fn()
        except (_Retry, httpx.TransportError) as e:
            if attempt == RETRIES:
                raise AsrApiError(f"语音识别接口多次失败：{e}") from e
            for _ in range(int(min(60, 3 * 2 ** attempt) * 4)):  # 等的时候也能取消
                check_cancel()
                time.sleep(0.25)


def _transcribe_file(ep, path, fmt, language, timed):
    """/audio/transcriptions：返回接口的 JSON。timed=False 时只要文字。
    要时间时先要逐字 + 逐句，被拒（400）就只要逐句；都被拒才当成不给时间（_NoTimestamps，改成切小段只要文字）。"""
    mime = {"mp3": "audio/mpeg", "flac": "audio/flac", "wav": "audio/wav"}[fmt]

    def call(gran):
        data = {"model": ep["model"], "response_format": "verbose_json" if timed else "json", "temperature": "0"}
        if gran:
            data["timestamp_granularities[]"] = gran
        if language:
            data["language"] = language
        with _client() as c, open(path, "rb") as f:
            r = c.post(_url(ep, "/audio/transcriptions"), headers={"Authorization": f"Bearer {ep['key']}"},
                       data=data, files={"file": (Path(path).name, f, mime)})
        _check(r)
        return r.json() if r.content.strip()[:1] in (b"{", b"[") else {"text": r.text}

    if not timed:
        return _with_retry(lambda: call(None))
    who = (ep["base_url"], ep["model"])
    tries = [_gran_ok[who]] if who in _gran_ok else list(GRANULARITIES)
    last = None
    for gran in tries:
        try:
            resp = _with_retry(lambda g=gran: call(g))
        except _Rejected as e:
            if who in _gran_ok:  # 已经试出能用的请求方式了，这次被拒是别的原因（比如这段音频有问题）：照实报错
                raise
            last = e
            continue
        if who not in _gran_ok:
            _gran_ok[who] = gran
            if gran != list(GRANULARITIES[0]):
                log(f"  {ep['model']} 不支持逐字时间，改用逐句时间")
        return resp
    raise _NoTimestamps(str(last))


SYSTEM = """你是逐字转写员。把音频里说出来的每一句话按原语言逐字写下来：不翻译、不总结、不润色，口语、语气词、方言照原样写；
不写音乐、音效、笑声之类的说明；听不清的地方按听到的写，不要编。日语、中文照常写，词和词之间不加空格，句末照常加标点。
按句子分段（一句话一段，长句在停顿处断开，每段不超过 8 秒），每段给出开始和结束时间（相对这段音频开头，格式 MM:SS.ss）。
只输出 JSON：{"language": "ISO 639-1 语言代码", "segments": [{"start": "MM:SS.ss", "end": "MM:SS.ss", "text": "..."}]}
没有人说话就输出 {"language": "", "segments": []}。"""


CONTEXT_NOTE = ("\n\n节目信息（只用来写对人名、地名、专有名词的写法：听到的名字照这里的写法写；"
                "这里有、但音频里没说的名字不要加进去，听不清的照听到的写）：\n{context}")


def names_text(terms):
    """术语表里人工给的名字（手动加的、导入的）→ 给多模态大模型的名单：一行一个「原文（读音）」。
    只给原文和读音：译法、类型、备注是翻译用的，识别用不上，不占提示词。"""
    rows = []
    for t in terms or []:
        src = (t.get("src") or "").strip()
        if src:
            rows.append(src + (f"（{t['reading'].strip()}）" if (t.get("reading") or "").strip() else ""))
    return "\n".join(rows)


def _chat_audio(ep, path, fmt, language, seconds, context=""):
    """多模态大模型（chat/completions + input_audio）：返回 {"language", "segments": [...]}。
    context：节目说明（「资料」里填的，比如从网上贴的角色表），让模型写对人名（「犬川」不写成同一部剧里另一个人的「犬飼」）。"""
    from ..llm.client import parse_json

    b64 = base64.b64encode(Path(path).read_bytes()).decode()
    user = f"这段音频长 {seconds:.1f} 秒（{int(seconds // 60):02d}:{seconds % 60:05.2f}）。语言：{language or '请自动判断'}。"
    if context.strip():
        user += CONTEXT_NOTE.format(context=context.strip())
    body = {"model": ep["model"], "temperature": 0, "response_format": {"type": "json_object"},
            "max_tokens": max_out(seconds),
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": [{"type": "text", "text": user},
                                                      {"type": "input_audio", "input_audio": {"data": b64, "format": fmt}}]}]}

    def call():
        with _client() as c:
            r = c.post(_url(ep, "/chat/completions"), headers={"Authorization": f"Bearer {ep['key']}"}, json=body)
        if r.status_code == 400 and "response_format" in r.text and "response_format" in body:
            body.pop("response_format")  # 有的接口不支持 JSON 模式
            raise _Retry("不支持 JSON 模式，去掉再试")
        if r.status_code == 400 and "max_tokens" in r.text and "max_tokens" in body:
            body.pop("max_tokens")
            raise _Retry("不支持 max_tokens，去掉再试")
        _check(r)
        return r.json()
    try:
        resp = _with_retry(call)
    except _NoTimestamps as e:
        raise AsrApiError(f"语音识别接口报错：{e}") from e
    used = (resp.get("usage") or {}).get("prompt_tokens")
    text_tokens = (len(SYSTEM) + len(user)) * 1.5
    if used is not None and seconds >= GUARD_SECONDS and used < text_tokens + seconds * MIN_AUDIO_TOKENS:
        raise AsrApiError(f"接口没有收到音频（{seconds:.0f} 秒音频 + 提示词只计了 {used} 个输入 token）：这个接口 / 中转不支持音频输入，"
                          "模型会凭空编一段话或者什么也不给。请换支持音频的接口（例如 Gemini 官方接口）")
    choice = (resp.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    if choice.get("finish_reason") == "length":
        raise _Truncated(f"{seconds:.0f} 秒的音频写到输出上限（{body.get('max_tokens')} token）被截断")
    try:
        return parse_json(text)
    except Exception as e:
        raise AsrApiError(f"模型返回的不是 JSON：{text[:200]}") from e


def max_out(seconds):
    """多模态大模型识别这么长的音频，输出上限（max_tokens）设多少。"""
    return int(OUT_BASE + seconds * OUT_PER_SEC)


def _ts(v):
    """时间：数字（秒）或 “MM:SS.ss” / “HH:MM:SS.ss”。"""
    if isinstance(v, (int, float)):
        return float(v)
    parts = str(v).strip().replace("：", ":").split(":")
    try:
        sec = 0.0
        for p in parts:
            sec = sec * 60 + float(p)
        return sec
    except ValueError:
        return None


# ---------------- 一块的结果 → 段（绝对时间） ----------------
def _lang_code(name):
    if not name:
        return None
    name = str(name).strip().lower()
    return LANG_NAMES.get(name, name if len(name) <= 3 else None)


def _from_verbose(resp, t0):
    """verbose_json：逐句时间 + （有的话）逐词时间。词按中点分到句子里。"""
    segs = []
    for s in resp.get("segments") or []:
        text = (s.get("text") or "").strip()
        if text:
            segs.append({"start": round(float(s["start"]) + t0, 3), "end": round(float(s["end"]) + t0, 3),
                         "text": text, "words": [], "timing": "segment"})
    words = [{"w": w.get("word", ""), "s": round(float(w["start"]) + t0, 3), "e": round(float(w["end"]) + t0, 3)}
             for w in resp.get("words") or [] if w.get("word", "").strip()]
    if not segs and words:  # 只有逐词时间：在字和字之间的停顿处（或句末标点后）断句
        groups = [[words[0]]]
        for prev, w in zip(words, words[1:]):
            if w["s"] - prev["e"] > PAUSE or prev["w"].strip()[-1:] in "。！？!?.":
                groups.append([])
            groups[-1].append(w)
        return [{"start": g[0]["s"], "end": g[-1]["e"], "text": _join([x["w"] for x in g]), "words": g, "timing": "word"}
                for g in groups]
    for w in words:
        m = (w["s"] + w["e"]) / 2
        best = min(segs, key=lambda s: 0 if s["start"] <= m <= s["end"] else min(abs(m - s["start"]), abs(m - s["end"])))
        best["words"].append(w)
    for s in segs:
        if s["words"]:
            s["timing"] = "word"
            if not s["text"]:
                s["text"] = _join([w["w"] for w in s["words"]])
    return segs


def _join(ws):
    """字连成句：拉丁文字之间加空格，日文 / 中文直接连。"""
    out = ""
    for w in ws:
        w = w.strip() if not w.startswith(" ") else w
        if out and re.match(r"[A-Za-z0-9]", w[:1] or "") and re.search(r"[A-Za-z0-9,.!?]$", out):
            out += " "
        out += w
    return out.strip()


_CJK = "々぀-ヿ㐀-鿿！-～、。"
_CJK_SPACE = re.compile(f"(?<=[{_CJK}]) +(?=[{_CJK}])")


def _from_chat(resp, t0, seconds):
    segs = []
    for s in resp.get("segments") or []:
        a, b, text = _ts(s.get("start")), _ts(s.get("end")), (s.get("text") or "").strip()
        text = _CJK_SPACE.sub("", text)  # 模型偶尔把日语按词分开写（「見 た とこ 普通 の ガキ」）：字之间的空格去掉，英文单词之间的照留
        if a is None or not text or a > seconds + 2:
            continue
        b = b if b is not None and b > a else a + max(0.5, len(text) * 0.12)
        segs.append({"start": round(t0 + max(0.0, a), 3), "end": round(t0 + min(seconds, b), 3), "text": text,
                     "words": [], "timing": "segment"})
    return segs


def _clip_to(seg, lo, hi):
    """只留归这块的部分：有逐词时间时按每个字的中点切开；只有整句时间时，和这块沾边就留下
    （两块对同一句的时间估计会有出入，按中点分可能两边都不要它；两边都留下的，由 stitch 按文字去重）。"""
    if seg["words"]:
        ws = [w for w in seg["words"] if lo <= (w["s"] + w["e"]) / 2 < hi]
        if not ws:
            return None
        if len(ws) == len(seg["words"]):
            return seg
        return dict(seg, start=ws[0]["s"], end=ws[-1]["e"], text=_join([w["w"] for w in ws]), words=ws)
    return seg if seg["start"] < hi and seg["end"] > lo else None


def stitch(parts):
    """parts：[(归这块的开始, 归这块的结束, 段列表)]，按时间拼起来，去掉切点附近重复的段。"""
    from rapidfuzz import fuzz

    from ..textutil import norm

    cuts = [lo for lo, _, _ in parts[1:]]
    out = []
    for k, (lo, hi, segs) in enumerate(parts):
        lo = -1e9 if k == 0 else lo
        hi = 1e9 if k == len(parts) - 1 else hi
        out += [s for s in (_clip_to(s, lo, hi) for s in segs) if s]
    out.sort(key=lambda s: (s["start"], s["end"]))
    kept = []
    for s in out:
        p = kept[-1] if kept else None
        near = any(abs(s["start"] - c) < OVERLAP + 3 for c in cuts)
        a, b = norm(p["text"]) if p else "", norm(s["text"])
        same = a and b and (fuzz.ratio(a, b) >= 80 or (min(len(a), len(b)) >= 2 and fuzz.partial_ratio(a, b) >= 90))
        if p and near and s["start"] < p["end"] + 0.5 and same:
            if len(norm(s["text"])) > len(norm(p["text"])):
                kept[-1] = s
            continue
        kept.append(s)
    return kept


# ---------------- 入口 ----------------
def _cached(cache_dir, key, fn):
    path = Path(cache_dir) / "asr_api" / f"{key}.json" if cache_dir else None
    if path and path.exists() and not env.fresh():
        return json.loads(path.read_text(encoding="utf-8")), True
    out = fn()
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out, False


def _run_parallel(jobs, fn, on_done):
    """并发跑 jobs（每个 fn(job) 在工作线程里），主线程每 0.25 秒检查一次取消；on_done(job, 结果) 在主线程里调用。
    取消或出错时，工作线程在下一次检查时停下（正在传的那一块传完为止），排队的不再开始。"""
    user = env.cancel_event()
    stop = threading.Event()
    fresh = env.fresh()

    def work(job):
        env.set_cancel(stop)
        env.set_fresh(fresh)
        return fn(job)

    pool = ThreadPoolExecutor(max_workers=PARALLEL)
    futs = {pool.submit(work, j): j for j in jobs}
    try:
        pending = set(futs)
        while pending:
            if user is not None and user.is_set():
                raise Cancelled()
            done, pending = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
            for f in done:
                on_done(futs[f], f.result())
    except BaseException:
        stop.set()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=True)


def chunk_seconds(c: dict, ep: dict) -> float:
    """每块上传多长：设置里填了就用（限制在 30 秒 ~ 25 分钟）；没填（0）按接口自动选。"""
    v = float(c.get("api_chunk_seconds") or 0)
    if v > 0:
        return min(max(v, CHUNK_RANGE[0]), CHUNK_RANGE[1])
    return CHUNK_SHORT if "openrouter.ai" in (ep.get("base_url") or "") else CHUNK


def transcribe(wav, c: dict, ep: dict, language=None, offset=0.0, duration=0.0, junk=(), cache_dir=None):
    """c 是 AsrCfg 的 dict，ep 是 Config.endpoint("asr")。返回 (语音段列表, 检测到的语言)，和 asr.transcribe 一样。
    duration 只为和本地后端同样的签名（进度按 wav 实际长度算）。"""
    kind = c.get("api_kind", "transcribe")
    context = (c.get("context") or "").strip() if kind == "gemini" else ""  # 节目说明 + 术语表里的名字：只给多模态大模型
    names = names_text(c.get("names")) if kind == "gemini" else ""
    if names:
        context = (context + "\n\n" if context else "") + "人名、地名、专有名词（原文（读音））：\n" + names
    if not ep.get("key") or not ep.get("model"):
        raise AsrApiError("云端语音识别没有可用的接口：请在「设置」里添加接口并设置 API Key，再在流程页选它")
    _gran_ok.pop((ep["base_url"], ep["model"]), None)  # 每次运行重新试一下支持哪种时间（接口可能升级了）
    w = Wav(wav)
    total = w.duration
    size = chunk_seconds(c, ep)
    chunks = plan_chunks(total, w.quiet, size, None, min(SEARCH, size / 4))
    log(f"云端语音识别：{ep.get('name') or ep['base_url']} · {ep['model']}（{'转写接口' if kind == 'transcribe' else '多模态大模型'}），"
        f"{total / 60:.1f} 分钟切成 {len(chunks)} 块上传（每块约 {size / 60:g} 分钟）")
    # 取消时还在上传的工作线程可能开着块文件（Windows 删不掉）：清理失败不报错，留给系统临时目录
    tmp = tempfile.TemporaryDirectory(prefix="nyasub-asr-", ignore_cleanup_errors=True)
    state = {"timed": True, "lang": None, "done": 0.0, "hits": 0, "calls": 0}
    lock = threading.Lock()

    def key_of(path, extra):
        h = hashlib.sha1(Path(path).read_bytes()).hexdigest()
        return hashlib.sha1(json.dumps([PROMPT_V, kind, ep["base_url"], ep["model"], language, extra, h]).encode()).hexdigest()

    def one(a, b, timed):
        """识别 [a, b] 这一段（相对 wav 开头），返回 (段列表（相对 wav 开头）, 语言)。"""
        path, f = _encode(wav, a, b, Path(tmp.name) / f"{a:09.3f}", "wav" if kind == "gemini" else "flac")
        try:
            if kind == "gemini":
                try:
                    resp, hit = _cached(cache_dir, key_of(path, ["chat", hashlib.sha1(context.encode()).hexdigest()] if context else "chat"),
                                        lambda: _chat_audio(ep, path, f, language, b - a, context))
                except _Truncated as e:
                    if b - a < SPLIT_MIN * 2:
                        raise AsrApiError(f"语音识别：{e}，这一块已经很短，没法再切") from e
                    m = w.quiet(a + (b - a) * 0.4, a + (b - a) * 0.6)  # 切在中间附近最安静的地方
                    log(f"{a:.0f}–{b:.0f} 秒：{e}，切成两块重新识别")
                    s1, l1 = one(a, m, timed)
                    s2, l2 = one(m, b, timed)
                    return s1 + s2, l1 or l2
                segs, lang = _from_chat(resp, a, b - a), resp.get("language")
            else:
                resp, hit = _cached(cache_dir, key_of(path, timed),
                                    lambda: _transcribe_file(ep, path, f, language, timed))
                segs, lang = (_from_verbose(resp, a) if timed else []), resp.get("language")
                if not timed and (resp.get("text") or "").strip():
                    segs = [{"start": a, "end": b, "text": resp["text"].strip(), "words": [], "timing": "clip"}]
        finally:
            Path(path).unlink(missing_ok=True)
        with lock:
            state["hits" if hit else "calls"] += 1
        return segs, _lang_code(lang)

    def clips_of(a, b):
        """只给文字的接口：一块再切成 ≤ CLIP 秒的小段，只识别有声音的部分。"""
        out = []
        for _, _, x, y in plan_chunks(b - a, lambda lo, hi: w.quiet(lo + a, hi + a) - a, CLIP, 0.0, CLIP * 0.4):
            act = w.active(a + x, a + y)
            if act:
                out.append((max(a + x, act[0] - 0.2), min(a + y, act[1] + 0.2)))
        return out

    def chunk_job(ch):
        a, b, lo, hi = ch
        if state["timed"]:
            try:
                segs, lang = one(a, b, True)
                return segs, lang, "timed"
            except _NoTimestamps:
                state["timed"] = False
        segs, lang = [], None
        for x, y in clips_of(a, b):
            s, l2 = one(x, y, False)
            segs += s
            lang = lang or l2
        return segs, lang, "clip"

    results = {}

    def done(ch, res):
        results[ch] = res
        state["lang"] = state["lang"] or res[1]
        state["done"] += ch[3] - ch[2]
        progress(state["done"], total, "识别")

    progress(0, total, "识别")
    try:
        # 先单独跑第一块：Key 不对、接口不支持时间戳这些情况第一块就知道，不会同时白传好几块
        first = chunks[0]
        done(first, chunk_job(first))
        if results[first][2] == "clip":
            log("这个接口只给文字、不给时间：改成切成小段逐段识别（段的时间精确到小段，没有逐字时间）")
        _run_parallel(chunks[1:], chunk_job, done)
    finally:
        tmp.cleanup()
    parts = [(lo, hi, results[(a, b, lo, hi)][0]) for a, b, lo, hi in chunks]
    segs = stitch(parts)
    junk = tuple(junk)
    out = []
    for s in segs:
        if junk and any(j in s["text"] for j in junk):
            continue
        s = dict(s, start=round(s["start"] + offset, 3), end=round(s["end"] + offset, 3),
                 words=[dict(x, s=round(x["s"] + offset, 3), e=round(x["e"] + offset, 3)) for x in s["words"]])
        out.append({"id": f"a{len(out) + 1}", "start": s["start"], "end": s["end"], "text": s["text"],
                    "words": s["words"], "timing": s["timing"]})
    timing_model = (c.get("api_timing_model") or "").strip() if kind == "gemini" else ""
    if timing_model:
        log(f"对时：再用转写模型 {timing_model} 识别一遍，句子的起止对到它的时间上")
        ref, _ = transcribe(wav, dict(c, api_kind="transcribe", api_model=timing_model, api_timing_model="", context="", names=[]),
                            dict(ep, model=timing_model), language, offset, duration, junk, cache_dir)
        out = align_to_reference(out, ref)
    timing = {t: sum(1 for s in out if s["timing"] == t) for t in ("word", "aligned", "segment", "clip")}
    log(f"云端识别：{len(out)} 段（时间精度：" + "，".join(f"{n} {k}" for k, n in
        (("逐字", timing["word"]), ("逐句、借转写模型的时间", timing["aligned"]), ("逐句", timing["segment"]),
         ("小段", timing["clip"])) if n) + f"），请求 {state['calls']} 次，复用缓存 {state['hits']} 次")
    if timing["segment"] + timing["clip"] and not timing["word"] and not timing["aligned"]:
        log("接口没给逐字时间：字幕自动对时改按逐句时间估计（误差大约 0.2 秒）")
    return out, language or state["lang"]


ALIGN_V = 2          # 对时的做法改了就加一（记进语音识别的指纹，填了“对时用”的工程重新对；两个模型的结果都有缓存）
DRIFT = 3.0         # 大模型估的时间最多偏这么多：对上的字在这个范围外，当成对错了位置
MIN_HIT = 0.4       # 一句里至少这么多字（而且至少 2 个）对上了转写模型的字，才用它的时间
PUNCT_GAP = 0.5     # 标点离前一个字不超过这么远，才算这个字的一部分
CHAR_SEC = 0.12     # 句首 / 句尾几个字没对上（名字写法不同）时，按每个字这么长往外推
SHORT = 4           # 大模型漏掉、转写模型听到的这么短的话（「え？」「うわあ！」）补进来
SHORT_GAP = 0.6     # 补的短话离大模型的句子（原来的时间、对过的时间都算）至少这么远


def align_to_reference(segs, ref):
    """大模型的句子（字准，时间是估的）按文字对到转写模型上（逐字时间准，字常常错）：两边的字大多一样，
    整集按字对齐，每句的起止用对上的第一个 / 最后一个字在转写模型里的时间。按文字对，大模型的时间整段偏了一两秒、
    转写模型把两句并成一句（「犬塚志乃、犬川荘助。」）都不影响；对上的字太少的句子（转写模型没听到）保留原来的时间。
    转写模型听到、大模型那个时间什么也没有的短话补进来。"""
    from difflib import SequenceMatcher

    from ..textutil import norm
    from .asr import repair

    ref = repair(ref)
    chars = []  # 转写模型的每个字：(字, 开始, 结束)；字后面紧跟的标点（「。」也有时间）算进结束，句尾才不会收得太早
    spans = []  # 转写模型每句的字在 chars 里的范围
    for r in ref:
        first = len(chars)
        for w in r.get("words") or []:
            t = norm(w["w"])
            if not t:
                if chars and w["s"] - chars[-1][2] < PUNCT_GAP:
                    chars[-1] = (chars[-1][0], chars[-1][1], max(chars[-1][2], w["e"]))
                continue
            for k, ch in enumerate(t):
                d = (w["e"] - w["s"]) / len(t)
                chars.append((ch, w["s"] + k * d, w["s"] + (k + 1) * d))
        spans.append(range(first, len(chars)))
    if not chars:
        return segs
    texts = [norm(s["text"]) for s in segs]
    owner = [i for i, t in enumerate(texts) for _ in t]
    hit = {}  # 大模型第几个字 → 转写模型第几个字
    for a, b, n in SequenceMatcher(None, "".join(texts), "".join(c[0] for c in chars), autojunk=False).get_matching_blocks():
        for k in range(n):
            hit[a + k] = b + k
    out, pos = [], 0
    for i, s in enumerate(segs):
        n = len(texts[i])
        got = [(k, hit[pos + k]) for k in range(n) if pos + k in hit
               and s["start"] - DRIFT <= chars[hit[pos + k]][1] <= s["end"] + DRIFT]
        pos += n
        if n == 0 or len(got) < max(2, MIN_HIT * n):
            out.append(s)
            continue
        (k0, m0), (k1, m1) = got[0], got[-1]
        a = chars[m0][1] - k0 * CHAR_SEC
        b = chars[m1][2] + (n - 1 - k1) * CHAR_SEC
        out.append(dict(s, start=round(a, 3), end=round(b, 3), timing="aligned") if b > a else s)
    used = set(hit.values())
    near = [(s["start"], s["end"]) for s in segs] + [(s["start"], s["end"]) for s in out]
    for r, span in zip(ref, spans):
        # 大模型漏掉的短话：字一个也没对上，附近大模型也什么都没写（「ケンタ」旁边有「健太！」、「ソニー」是听错的「荘兄」，不补）
        if (len(norm(r["text"])) <= SHORT and not any(m in used for m in span)
                and not any(min(r["end"] + SHORT_GAP, b) - max(r["start"] - SHORT_GAP, a) > 0 for a, b in near)
                and not (out[-1]["timing"] == "word" and norm(out[-1]["text"]) == norm(r["text"])
                         and r["start"] - out[-1]["end"] < SHORT_GAP)):  # 转写模型把一声「ん？」写成了挨着的两条
            out.append(dict(r, timing="word"))
    out.sort(key=lambda s: s["start"])
    return [dict(s, id=f"a{k + 1}") for k, s in enumerate(out)]

