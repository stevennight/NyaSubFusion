"""参考语音（听音标注）：翻译前把对话的原声一段段发给能听音频的大模型（比如 Gemini），逐条标出
- 问句还是陈述（光看文字分不出来：「…あれなの」句尾下降是在讲解，上扬才是问）；
- 语气（生气地骂、开玩笑、自言自语……）；
- 只有语音识别听到的句子（没有字幕、画面字对照）实际听到的是什么（「歯ないよ」其实是「（足袋）は ないよ」）。
标注记在字幕的 "listen" 里，翻译时附在每条后面作参考。原文文字以字幕 / 画面字为准，模型只管听语调和语气。

翻译用的模型（比如 gpt-6-luna）多半听不了音频，所以听音单独用一个接口 / 模型（设置里的“听音模型”）。
中转站不转发音频时模型会凭空编：按用量里的音频 token 判断，收不到就这一集不再听。
"""
from pathlib import Path

from ..env import Cancelled, log, progress
from ..textutil import fmt_time

SPEECH = ("dialogue",)    # 只听对话；旁白是播音员念稿，没有歧义
MAX_SECONDS = 45          # 一段原声最长多少秒
MAX_ITEMS = 15            # 一段最多几条字幕
MAX_GAP = 6.0             # 前后两条对话隔得比这久就另起一段
PAD = 0.4                 # 前后多切一点，句尾的语调不被切掉
KINDS = ("问句", "陈述", "感叹", "呼唤", "命令")

SYSTEM = """你在帮字幕译者听原声。给你一段日语纪录片的原声，和这段时间里说的话的字幕原文（带相对这段原声的时间、来源）。
字幕原文来自电视字幕 / 画面字，文字一般可信；你的任务不是重新转写，而是听出光看文字分不出来的东西。
文字会误导你：「〜の」「〜か」「〜って」结尾的句子很多是在讲解、自言自语，不是在问。所以一定先按时间找到这句，听它的句尾，再下判断。逐条给出：
- end：这句最后几个音的音高：上扬 / 下降 / 平。只凭耳朵，不看文字。
- kind：问句 / 陈述 / 感叹 / 呼唤 / 命令。end 是上扬的才可能是问句；end 下降或平的，除非句子里有疑问词（いつ、何、どう、誰、どこ、どれ…）在问对方，否则不是问句（是讲解、确认、感叹）。
- tone：说话的语气、情绪，几个字（平静地讲解、生气地骂、心疼地责怪、开玩笑、自言自语、对着动物说……）；没有特别的语气就写空。
- heard：只看来源是“语音识别”的条目（没有字幕、画面字对照，可能听错）：你听到的和原文明显不同时，写你听到的日语；相同、或者听不清就写空。其他来源的条目 heard 一律写空。
听不到某句（没声音、被音乐或别人的话盖住）就 end、kind、tone 都写空，不要猜。
只输出 JSON：{"notes": [{"id": "条目 id", "end": "…", "kind": "…", "tone": "…", "heard": "…"}]}"""


def _origin(c):
    srcs = c.get("sources") or []
    if srcs and all(str(s).startswith("a") for s in srcs):
        return "语音识别"
    if any(str(s).startswith(("o", "v")) for s in srcs):
        return "画面字"
    return "字幕"


def windows(cues):
    """要听的对话按时间分成几段：[(开始, 结束, [字幕])]。"""
    out, cur = [], []
    for c in sorted((c for c in cues if c.get("type") in SPEECH), key=lambda c: c["start"]):
        if cur and (c["start"] - max(x["end"] for x in cur) > MAX_GAP or c["end"] - cur[0]["start"] > MAX_SECONDS
                    or len(cur) >= MAX_ITEMS):
            out.append(cur)
            cur = []
        cur.append(c)
    if cur:
        out.append(cur)
    return [(max(0.0, w[0]["start"] - PAD), max(c["end"] for c in w) + PAD, w) for w in out]


def window_prompt(a, b, cues):
    rel = lambda t: fmt_time(max(0.0, t - a))
    return (f"这段原声长 {b - a:.1f} 秒。字幕（时间相对这段原声的开头）：\n"
            + "\n".join(f"[{c['id']}] {rel(c['start'])}-{rel(c['end'])}（{_origin(c)}）{c['orig']['text'].replace(chr(10), ' / ')}"
                        for c in cues)
            + "\n\n请按要求输出 JSON。")


def _received(usage, seconds):
    """接口收到音频没有：用量里有音频 token 数就看它；没有这一项就当收到了（看不出来）。"""
    det = (usage or {}).get("prompt_tokens_details") or {}
    if "audio_tokens" in det and seconds >= 3:
        return bool(det.get("audio_tokens"))
    return True


def clean_note(n, c):
    kind = str(n.get("kind") or "").strip()
    kind = next((k for k in KINDS if k in kind), "")
    tone = str(n.get("tone") or "").strip()[:20]
    heard = str(n.get("heard") or "").strip() if _origin(c) == "语音识别" else ""
    if heard and heard.replace(" ", "").rstrip("。.!！?？") == c["orig"]["text"].replace(" ", "").rstrip("。.!！?？"):
        heard = ""
    return {k: v for k, v in (("kind", kind), ("tone", tone), ("heard", heard[:80])) if v}


def annotate(cues, cfg, cache_dir, wav, clip_dir):
    """给对话逐条标注（就地写进 cue["listen"]），返回标了几条。没设听音模型、接口收不到音频时跳过。"""
    from ..sources.asr_api import _encode
    from .client import LLM, FatalLLMError

    ep = cfg.endpoint("listen")
    if not ep["model"] or not ep["key"]:
        log("参考语音：没有设置听音模型（「设置」里给接口填“听音模型”，比如 Gemini），这次不听")
        return 0
    if not wav or not Path(wav).exists():
        log("参考语音：没有提取出音频，这次不听")
        return 0
    wins = windows(cues)
    if not wins:
        return 0
    llm = LLM(cfg, cache_dir=cache_dir, purpose="listen")
    clip_dir = Path(clip_dir)
    clip_dir.mkdir(parents=True, exist_ok=True)
    log(f"参考语音：{len(wins)} 段原声（{sum(b - a for a, b, _ in wins) / 60:.0f} 分钟）交给 {ep['model']} 听")
    marked = 0
    for i, (a, b, w) in enumerate(wins):
        progress(i, len(wins), f"听原声 {i + 1}/{len(wins)} 段", every=0)
        try:
            path, fmt = _encode(wav, a, b, clip_dir / f"{round(a * 100)}-{round(b * 100)}", fallback="wav")
            res = llm.chat_json(SYSTEM, window_prompt(a, b, w), audio=(path, fmt))
        except Cancelled:
            raise
        except FatalLLMError as e:
            if e.status in (401, 403):
                raise
            log(f"  听音接口报错，这一集不再听：{str(e)[:200]}")
            break
        except Exception as e:
            log(f"  第 {i + 1} 段（{fmt_time(a)} 起）没听成：{str(e)[:150]}")
            continue
        if llm.usage is not None and not _received(llm.usage, b - a):
            log(f"  接口没有收到音频（用量里音频 token 是 0）：{ep['model']} 不能听音频，或者中转站不转发音频。这一集不再听")
            for c in w:  # 这一段的结果是模型凭空编的，不用
                c.pop("listen", None)
            break
        by_id = {c["id"]: c for c in w}
        for n in (res or {}).get("notes") or []:
            c = by_id.get(str(n.get("id"))) if isinstance(n, dict) else None
            note = clean_note(n, c) if c else None
            if note:
                c["listen"] = note
                marked += 1
    progress(len(wins), len(wins), "听原声", every=0)
    log(f"参考语音：标注了 {marked} 条对话")
    return marked


def note_text(c):
    """附在字幕后面给译者看的标注：〔听：问句，生气地骂；语音识别可能听错，听到「…」〕"""
    n = c.get("listen") or {}
    parts = [x for x in (n.get("kind"), n.get("tone")) if x]
    if n.get("heard"):
        parts.append(f"这句只有语音识别、可能听错，原声听着像「{n['heard']}」")
    return f"　〔听原声：{'，'.join(parts)}〕" if parts else ""


NOTE = """

## 听原声的标注
有些对话后面附了〔听原声：…〕，是另一个模型听了原声标的：问句还是陈述、说话的语气，只有语音识别来源的句子还附了它听到的说法。
原文带「？」的照原文是问句；没有「？」、光看文字分不出问句和陈述、看不出语气时以标注为准
（比如句尾「〜の」标了陈述，就不要译成问句；标了生气地骂，就不要译成逗着玩）。
“原声听着像”只在原文读不通时参考。标注只帮你理解，不要把它写进译文。"""
