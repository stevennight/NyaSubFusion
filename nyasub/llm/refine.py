"""视觉大模型复核画面字：纠正 OCR 错字，并判断是不是实物上本来就有的字（招牌、包装…）。"""
import os
import re
from difflib import SequenceMatcher

from ..env import check_cancel, log, progress
from ..textutil import full_sim, norm, sim

_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")

VLM_SYSTEM = """你是影视节目的画面文字校对员。会给你若干张截图（按顺序编号），每张是画面上一处文字及其周围一小圈背景。
对每张图做两件事：
1. text：准确、完整地转写图中的原文，每一行、每一个字都要转写，不能漏行、漏掉行尾。
   横排多行用 " / " 分隔；竖排按从右到左的列顺序，每列用 " / " 分隔。看不清就输出空字符串。
   附带的“本地 OCR 参考”可能有错字、多出噪点字符，也可能少字；一切以图片为准。
2. kind：判断这处文字是哪一类——
   - "caption"：节目后期叠加在画面上的字幕，比如采访台词字幕、人名条、地名、说明文字、标题、资料来源、片尾字幕。
     特征：字体规整清晰，通常有描边或阴影，横平竖直，不随镜头透视变形，不受画面光影影响。
   - "scene"：拍摄到的实物上本来就有的字，比如招牌、海报、地图、古文书、日历、商品包装和标签、手写字、车身文字、电视台台标。
     特征：跟随物体有透视或弯曲，受光照、景深、反光影响，字体五花八门。
输出 JSON：{"items": [{"text": "第1张原文", "kind": "caption"}, ...]}，数量必须和图片数量一致。"""


def truncated(vlm, local):
    """复核结果只是本地 OCR 文字的一部分、明显短一截：多半是截图那一帧字还没出全（逐字出现的动画）。"""
    a, b = norm(vlm), norm(local)
    if not (a and len(a) < 0.85 * len(b) and sim(a, b) >= 90):
        return False
    # 少掉的部分只是英文字母、数字这类噪点（“TaJ”）：是本地识别多认了，不是复核截短了
    blocks = SequenceMatcher(None, b, a, autojunk=False).get_matching_blocks()
    kept = {i for x in blocks for i in range(x.a, x.a + x.size)}
    missing = "".join(ch for i, ch in enumerate(b) if i not in kept)
    return len(_CJK.findall(missing)) >= 3  # “一”被认成“―”这种一两个字的差别不算


def _ask(llm, group, paths):
    hint = "\n".join(f"第{j + 1}张（本地 OCR 参考）：{e['text_local' if 'text_local' in e else 'text'].replace(chr(10), ' / ')}"
                     for j, e in enumerate(group))
    res = llm.chat_json(VLM_SYSTEM, f"共 {len(group)} 张图。\n{hint}", images=paths)
    got = res.get("items")
    if got is None:
        got = [{"text": t} for t in res.get("texts", [])]
    if len(got) != len(group):
        raise ValueError(f"返回 {len(got)} 条，应为 {len(group)} 条")
    got = [g if isinstance(g, dict) else {"text": g} for g in got]
    for j in borrowed(group, got):
        log(f"  {group[j]['id']}：复核结果和同一批另一张图的字一样、和本地识别对不上（串图了），保留本地结果")
        got[j] = {}
    return got


def borrowed(group, got):
    """同一批里，某张图的复核结果其实是另一张图上的字（模型串图了）：返回这些图的序号。
    判断：结果和别的图的字（本地识别或复核结果）几乎一样，和自己的本地识别却对不上，而两张图本地识别的字本来也不一样。"""
    local = [e.get("text_local", e["text"]) for e in group]
    texts = [(g.get("text") or "").replace(" / ", "\n") for g in got]
    out = []
    for j, t in enumerate(texts):
        mine = norm(local[j])
        # 本地只认出一两个字（或者复核结果里包含本地认出的字）时，复核补全是正常的，不算串图
        if not norm(t) or len(mine) < 3 or mine in norm(t) or sim(t, local[j]) >= 50:
            continue
        for k in range(len(group)):
            if k != j and full_sim(local[j], local[k]) < 50 and max(full_sim(t, local[k]), full_sim(t, texts[k])) >= 85:
                out.append(j)
                break
    return out


def _apply(e, g):
    t = (g.get("text") or "").strip()
    if t:
        e["text"] = t.replace(" / ", "\n")
    if g.get("kind") in ("caption", "scene"):
        e["kind"] = g["kind"]


def refine_ocr(events, cfg, workdir, cache_dir, batch=4, video=None):
    from .client import LLM, FatalLLMError

    llm = LLM(cfg, vision=True, cache_dir=cache_dir)
    todo = [e for e in events if e.get("crop") and "text_local" not in e and not e.get("prefiltered")]
    log(f"视觉大模型复核画面字：{len(todo)} 条")
    short = []  # 复核结果只有半截的：换一帧重新截图再问
    for i in range(0, len(todo), batch):
        check_cancel()
        progress(i, len(todo), "复核", every=0)
        group = todo[i:i + batch]
        try:
            got = _ask(llm, group, [os.path.join(workdir, e["crop"]) for e in group])
        except FatalLLMError as ex:
            log(f"  {ex}")
            log("  视觉复核已跳过，继续使用本地 OCR 结果。请检查 [llm] vision_model / model")
            return events
        except Exception as ex:
            log(f"  第 {i // batch + 1} 批复核失败，保留本地结果：{ex}")
            continue
        for e, g in zip(group, got):
            e["text_local"] = e["text"]
            if truncated(g.get("text") or "", e["text"]):
                short.append(e)
                if g.get("kind") in ("caption", "scene"):
                    e["kind"] = g["kind"]
                continue
            _apply(e, g)
    if short:
        _retry_short(llm, short, workdir, video, batch, float(cfg.ocr.fps))
    return events


def _retry_short(llm, short, workdir, video, batch, fps):
    """截图那一帧字还没出全：换字最完整的那一帧、截宽一点重新截图，再复核一次。
    重截不了 / 再问失败时保留本地 OCR 的完整文字（宁可有错字也不能少半句，翻译时大模型还会结合上下文纠正）。"""
    from ..sources.ocr import crop_at

    log(f"  {len(short)} 条截图里的字没出全（逐字出现的字幕），换一帧重新截图复核")
    ready = []
    for e in short:
        check_cancel()
        path = os.path.join(workdir, "crops", f"{e['id']}_full.jpg")
        # 跟着说话逐字出现的字幕最后一帧最完整（end 是最后一次识别到再加半帧）；
        # 旧项目没记下最完整时的框，截整条横带（竖排截整列），文字往哪边长都截得到
        t = e["snap_t"] if e.get("snap_t") is not None else e["end"] - 0.5 / max(0.1, fps)
        box = dict(e)
        if not e.get("crop_rect"):
            x0, y0, x1, y1 = e["rect"]
            box["crop_rect"] = [x0, 0.0, x1, 1.0] if e.get("vertical") else [0.0, y0, 1.0, y1]
        if video and crop_at(video, box, t, path):
            e["crop"] = os.path.relpath(path, workdir).replace("\\", "/")
            ready.append(e)
    for i in range(0, len(ready), batch):
        group = ready[i:i + batch]
        try:
            got = _ask(llm, group, [os.path.join(workdir, e["crop"]) for e in group])
        except Exception as ex:
            log(f"  重新复核失败：{ex}")
            continue
        for e, g in zip(group, got):
            # 还是半截：几行字幕先后出现、没有哪一帧同时有全部的字。本地识别把各帧的字都记下了，置信度高就信本地的
            if truncated(g.get("text") or "", e["text_local"]) and e.get("score", 0) >= 0.95:
                if g.get("kind") in ("caption", "scene"):
                    e["kind"] = g["kind"]
                continue
            _apply(e, g)
