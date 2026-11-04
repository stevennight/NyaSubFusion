"""模拟大模型：按提示词里的条目生成确定的回答，用来测试流水线（不联网、不花钱）。

- 术语表请求 → 固定的概要和一条术语
- 整理请求 → 每个非语音条目单独成一条（只有原文）；语音条目丢弃
- 翻译请求（普通翻译、导出任务里两件事一起做的旧提示词）→ 译文 = "译:" + 原文
- 其余目标语言请求 → 译文 = "EN:" + 原文
"""
import re

CALLS = []
_ITEM = re.compile(r"^\[(\w+)\] [\d:.]+-[\d:.]+ (\S+?)(?:（[^）]*）)?：(.*?)(?:　〔.*〕)?$", re.M)
_EXTRA = re.compile(r"^\[(q\d+)\] (.*?)　〔参考", re.M)


def respond(system, user):
    CALLS.append((system, user))
    if "术语整理" in system:
        return {"summary": "测试概要", "terms": [{"src": "深浦町", "reading": "ふかうらまち", "tr": "深浦町", "type": "地名"}]}
    if '"items": [{"id"' in system:
        return {"items": [{"id": i, "tr": "EN:" + t} for i, t in _EXTRA.findall(user)]}
    if '"items": [{"ids"' in system:  # 翻译
        body = user.split("## 本段字幕")[-1] if "## 本段字幕" in user else user.split("## 这一组")[-1].split("## 后文")[0]
        return {"items": [{"ids": [i], "tr": "译:" + t.replace(" / ", "\n")} for i, _, t in _ITEM.findall(body)]}
    organize = "不翻译" in system
    cues, drop = [], []
    for i, label, text in _ITEM.findall(user):
        if label == "语音识别":
            drop.append(i)
        else:
            text = text.replace(" / ", "\n")
            cue = {"src": [i], "type": "info" if "像说明" in user.split(f"[{i}]")[1].split("\n")[0] else "dialogue", "orig": text}
            if not organize:
                cue["tr"] = "译:" + text
            cues.append(cue)
    return {"cues": cues, "drop": drop}


def install(monkeypatch):
    from nyasub.llm import client

    CALLS.clear()
    monkeypatch.setattr(client.LLM, "_call", lambda self, system, user, images=None: respond(system, user))
    monkeypatch.setenv("NYASUB_TEST_KEY", "x")
