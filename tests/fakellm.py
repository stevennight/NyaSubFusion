"""模拟大模型：按提示词里的条目生成确定的回答，用来测试流水线（不联网、不花钱）。

- 术语表请求 → 固定的概要和一条术语
- 融合翻译请求 → 每个非语音条目单独成一条，译文 = "译:" + 原文；语音条目丢弃
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
    cues, drop = [], []
    for i, label, text in _ITEM.findall(user):
        if label == "语音识别":
            drop.append(i)
        else:
            text = text.replace(" / ", "\n")
            cues.append({"src": [i], "type": "info" if "像说明" in user.split(f"[{i}]")[1].split("\n")[0] else "dialogue",
                         "orig": text, "tr": "译:" + text})
    return {"cues": cues, "drop": drop}


def install(monkeypatch):
    from nyasub.llm import client

    CALLS.clear()
    monkeypatch.setattr(client.LLM, "_call", lambda self, system, user, images=None: respond(system, user))
    monkeypatch.setenv("NYASUB_TEST_KEY", "x")
