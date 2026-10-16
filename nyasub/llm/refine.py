"""视觉大模型复核画面字：纠正 OCR 错字，并判断是不是实物上本来就有的字（招牌、包装…）。"""
import os

from ..env import log

VLM_SYSTEM = """你是影视节目的画面文字校对员。会给你若干张截图（按顺序编号），每张是画面上一处文字及其周围一小圈背景。
对每张图做两件事：
1. text：准确转写图中的原文。横排多行用 " / " 分隔；竖排按从右到左的列顺序，每列用 " / " 分隔。看不清就输出空字符串。
2. kind：判断这处文字是哪一类——
   - "caption"：节目后期叠加在画面上的字幕，比如采访台词字幕、人名条、地名、说明文字、标题、资料来源、片尾字幕。
     特征：字体规整清晰，通常有描边或阴影，横平竖直，不随镜头透视变形，不受画面光影影响。
   - "scene"：拍摄到的实物上本来就有的字，比如招牌、海报、地图、古文书、日历、商品包装和标签、手写字、车身文字、电视台台标。
     特征：跟随物体有透视或弯曲，受光照、景深、反光影响，字体五花八门。
输出 JSON：{"items": [{"text": "第1张原文", "kind": "caption"}, ...]}，数量必须和图片数量一致。"""


def refine_ocr(events, cfg, workdir, cache_dir, batch=6):
    from .client import LLM, FatalLLMError

    llm = LLM(cfg, vision=True, cache_dir=cache_dir)
    todo = [e for e in events if e.get("crop") and "text_local" not in e and not e.get("prefiltered")]
    log(f"视觉大模型复核画面字：{len(todo)} 条")
    for i in range(0, len(todo), batch):
        group = todo[i:i + batch]
        paths = [os.path.join(workdir, e["crop"]) for e in group]
        hint = "\n".join(f"第{j + 1}张（本地 OCR 参考）：{e['text'].replace(chr(10), ' / ')}" for j, e in enumerate(group))
        try:
            res = llm.chat_json(VLM_SYSTEM, f"共 {len(group)} 张图。\n{hint}", images=paths)
            got = res.get("items")
            if got is None:
                got = [{"text": t} for t in res.get("texts", [])]
        except FatalLLMError as ex:
            log(f"  {ex}")
            log("  视觉复核已跳过，继续使用本地 OCR 结果。请检查 [llm] vision_model / model")
            return events
        except Exception as ex:
            log(f"  第 {i // batch + 1} 批复核失败，保留本地结果：{ex}")
            continue
        if len(got) != len(group):
            continue
        for e, g in zip(group, got):
            g = g if isinstance(g, dict) else {"text": g}
            t = (g.get("text") or "").strip()
            e["text_local"] = e["text"]
            if t:
                e["text"] = t.replace(" / ", "\n")
            if g.get("kind") in ("caption", "scene"):
                e["kind"] = g["kind"]
    return events
