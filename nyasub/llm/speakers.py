"""字幕的线索看不出一条字幕里是不是换了人说话（位置只错开一点、纯文字字幕没有任何标记）：请大模型判断。
给它看各行的文字、在画面上的位置和颜色、前后各一条字幕、这段时间语音识别听到了什么（带时间，看得出停顿和对答）。"""
from ..env import Cancelled, log
from ..textutil import fmt_time

SYSTEM = (
    "你是影视字幕整理助手。下面每条是一条字幕（同一时间显示在画面上的几行），要判断这几行是一个人说的，"
    "还是几个人各说各的（对话、一问一答、旁白和采访对象等）。\n"
    "线索：各行在画面上的位置（横向占画面的百分比）和颜色——同一个人的话折行一般左对齐或右对齐、颜色相同，"
    "不同的人常放在画面不同位置或用不同颜色，但不绝对；行首的（名字）、⚟、“- ”是说话人标记；"
    "内容是否接得上（一句话折行 vs 一问一答、附和、回答）；语音识别里中间有没有停顿、像不像换了一个人在说。\n"
    '输出 JSON：{"items": [{"id": "c12", "voices": [0, 0, 1], "reason": "一句话理由"}]}。'
    "voices 每行一个数字，同一个人用同一个数字，从 0 开始按出场先后编号；一个人说的就全是 0。拿不准就当一个人（全 0）。")


def _item_text(c, prev, nxt, segs):
    lines = []
    for k, ln in enumerate(c["speaker_check"], 1):
        where = f"位置 {ln['pos']}" if ln.get("pos") else "位置不明"
        color = f"，颜色 {ln['color']}" if ln.get("color") else ""
        lines.append(f"　第{k}行（{where}{color}）：{ln['text']}")
    heard = [f"{fmt_time(s['start'])}-{fmt_time(s['end'])} {s['text']}" for s in segs
             if s["end"] > c["start"] - 1 and s["start"] < c["end"] + 1]
    return (f"[{c['id']}] {fmt_time(c['start'])}-{fmt_time(c['end'])}\n" + "\n".join(lines)
            + f"\n　前一条字幕：{(prev or {}).get('text', '（无）').replace(chr(10), ' / ')}"
            + f"\n　后一条字幕：{(nxt or {}).get('text', '（无）').replace(chr(10), ' / ')}"
            + "\n　这段时间语音识别听到的：" + ("；".join(heard) if heard else "（没有）"))


def judge(llm, subs, segs, batch=30):
    """字幕里带 speaker_check（线索看不出来）的条目，请大模型判断各行是谁说的，结果写进 voices。返回判断了几条、几条有两个人。"""
    idx = [i for i, c in enumerate(subs) if c.get("speaker_check")]
    if not idx:
        return 0, 0
    log(f"说话人：{len(idx)} 条字幕看不出是不是两个人说的，请大模型判断")
    multi = 0
    for k in range(0, len(idx), batch):
        part = idx[k:k + batch]
        prompt = "\n\n".join(_item_text(subs[i], subs[i - 1] if i else None, subs[i + 1] if i + 1 < len(subs) else None, segs)
                             for i in part)
        try:
            res = llm.chat_json(SYSTEM, prompt)
        except Cancelled:
            raise
        except Exception as e:
            log(f"  说话人判断失败，这几条当成一个人：{e}")
            continue
        got = {str(x.get("id")): x.get("voices") for x in res.get("items", []) or [] if isinstance(x, dict)}
        for i in part:
            c, vs = subs[i], got.get(subs[i]["id"])
            n = len(c["text"].split("\n"))
            if not isinstance(vs, list) or len(vs) != n or not all(isinstance(v, int) for v in vs):
                continue
            order = {}  # 按出场先后重新编号：[1, 1, 0] → [0, 0, 1]
            vs = [order.setdefault(v, len(order)) for v in vs]
            if len(set(vs)) > 1:
                c["voices"] = vs
                multi += 1
            else:
                c.pop("voices", None)
    log(f"说话人：{multi} 条判断为几个人各说各的，会拆成独立的字幕")
    return len(idx), multi
