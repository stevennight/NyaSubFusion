"""视觉大模型复核画面字：纠正 OCR 错字，并判断是不是实物上本来就有的字（招牌、包装…）。"""
import hashlib
import os
import re
import shutil
from difflib import SequenceMatcher

from ..env import Cancelled, check_cancel, log, progress
from ..textutil import full_sim, norm, sim

_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")

VLM_SYSTEM = """你是影视节目的画面文字校对员。会给你若干张截图，每张前面标着「第 n 张」，每张是画面上一处文字及其周围一小圈背景。
各张图互不相关：每张只转写这一张图里的字，不要把别的图上的字写进来。
对每张图做两件事：
1. text：准确、完整地转写图中的原文，每一行、每一个字都要转写，不能漏行、漏掉行尾。
   横排多行用 " / " 分隔；竖排按从右到左的列顺序，每列用 " / " 分隔。看不清就输出空字符串。
   附带的“本地 OCR 参考”可能有错字、多出噪点字符，也可能少字；一切以图片为准。
2. kind：判断这处文字是哪一类——
   - "caption"：节目后期叠加在画面上的字幕，比如采访台词字幕、人名条、地名、说明文字、标题、资料来源、片尾字幕。
     特征：字体规整清晰，通常有描边或阴影，横平竖直，不随镜头透视变形，不受画面光影影响。
   - "scene"：拍摄到的实物上本来就有的字，比如招牌、海报、地图、古文书、日历、商品包装和标签、手写字、车身文字、电视台台标。
     特征：跟随物体有透视或弯曲，受光照、景深、反光影响，字体五花八门。
输出 JSON：{"items": [{"no": 1, "text": "第1张原文", "kind": "caption"}, ...]}，no 是图前面标的编号，数量必须和图片数量一致。"""

# 带整帧画面的版本：每处文字都给一张它所在时刻的整帧画面（框出位置、标上编号；同一时刻的几处共用一张），再给每处的特写
VLM_CTX_SYSTEM = """你是影视节目的画面文字校对员。会给你一到几张整帧画面（画面 A、画面 B…）和若干张特写。
整帧画面上用彩色框框出了文字的位置，框旁标着编号；特写「第 n 张」是编号 n 那处文字（连同周围一小圈背景）放大的样子。
各处文字互不相关：每处只转写它自己的字，不要把别处的字写进来。
对每处文字做两件事：
1. text：看特写，准确、完整地转写原文，每一行、每一个字都要转写，不能漏行、漏掉行尾。
   横排多行用 " / " 分隔；竖排按从右到左的列顺序，每列用 " / " 分隔。看不清就输出空字符串。
   附带的“本地 OCR 参考”可能有错字、多出噪点字符，也可能少字；一切以图片为准。
2. kind：结合整帧画面判断这处文字是哪一类——
   - "caption"：节目后期叠加在画面上的字幕，比如采访台词字幕、人名条、地名、说明文字、标题、资料来源、片尾字幕。
     特征：浮在画面最上层，字体规整清晰，通常有描边或阴影，横平竖直，不随镜头透视变形，不受画面光影影响。
   - "scene"：拍摄到的实物上本来就有的字，比如招牌、海报、地图、古文书、日历、商品包装和瓶子上的标签、手写字、车身文字、电视台台标。
     特征：印在、写在、挂在画面里的物体上，跟随物体有透视或弯曲，受光照、景深、反光影响，字体五花八门。
     特写里看起来像字幕的单个字、短词，要看它在整帧画面里的位置：在某个物体（瓶子、包装、招牌…）上就是 scene。
   没有给整帧画面的那几处，只看特写判断。
输出 JSON：{"items": [{"no": 1, "text": "第1处原文", "kind": "caption"}, ...]}，no 是编号，数量必须和特写数量一致。"""

# 特写是从截图那一帧重新截的、上面框出了要转写的字时，加在提示词后面
BOXED_NOTE = """
有的特写上用红框框出了要转写的字（图前面的说明里会写）：只转写红框里的字。
红框外露出来的别的字（片名、台标、旁边别处的字幕或招牌…）都不是这一处的字，不要写进来，也不要据此判断 kind。"""

_COLORS = [(48, 59, 255), (89, 199, 52), (255, 132, 10), (0, 204, 255), (212, 45, 255), (255, 229, 0)]  # BGR


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


def _ask_once(llm, group, paths, frames=None, boxed=None):
    """frames：[(框好的整帧画面, 画面上框出的是这一批里的第几处)]。boxed：各张特写上有没有用红框框出要转写的字。"""
    boxed = boxed or [False] * len(paths)
    note = BOXED_NOTE if any(boxed) else ""
    red = lambda j: "，红框里是要转写的字" if boxed[j] else ""
    unit = "处" if frames else "张"
    hint = "\n".join(f"第{j + 1}{unit}（本地 OCR 参考）：{e['text_local' if 'text_local' in e else 'text'].replace(chr(10), ' / ')}"
                     for j, e in enumerate(group))
    # 每张图前面标上编号：只靠顺序时模型容易把相邻两张图的字搞混
    if frames:
        where = {j: chr(65 + k) for k, (_, idx) in enumerate(frames) for j in idx}
        images = [{"path": f, "label": f"画面 {chr(65 + k)}（框出了编号 {'、'.join(str(j + 1) for j in idx)}）"}
                  for k, (f, idx) in enumerate(frames)]
        images += [{"path": p, "label": f"第 {j + 1} 张（编号 {j + 1} 的特写" + (f"，在画面 {where[j]} 上" if j in where else "")
                    + red(j) + "）"} for j, p in enumerate(paths)]
        res = llm.chat_json(VLM_CTX_SYSTEM + note, f"共 {len(group)} 处文字。\n{hint}", images=images)
    else:
        images = [{"path": p, "label": f"第 {j + 1} 张" + (f"（{red(j)[1:]}）" if boxed[j] else "")} for j, p in enumerate(paths)]
        res = llm.chat_json(VLM_SYSTEM + note, f"共 {len(group)} 张图。\n{hint}", images=images)
    got = res.get("items")
    if got is None:
        got = [{"text": t} for t in res.get("texts", [])]
    if len(got) != len(group):
        raise ValueError(f"返回 {len(got)} 条，应为 {len(group)} 条")
    got = [g if isinstance(g, dict) else {"text": g} for g in got]
    nos = [g.get("no") for g in got]
    if sorted(n for n in nos if isinstance(n, int)) == list(range(1, len(group) + 1)):
        got = [dict(g) for g in sorted(got, key=lambda g: g["no"])]  # 按编号对回去（模型输出顺序可能乱）
    return got


def _ask(llm, group, paths, frames_for=None, boxed=None):
    """frames_for(这一批里第几处的列表) -> 给这几处的整帧画面（见 _ask_once）；不带画面时为 None。"""
    boxed = boxed or [False] * len(paths)
    got = _ask_once(llm, group, paths, frames_for(list(range(len(group)))) if frames_for else None, boxed)
    for j in borrowed(group, got):
        # 像是串图了：这一张单独再问一次。只有一张图就不可能串，单独问的结果直接用
        # （也可能根本没串：几张图拍的是同一段字、本地识别又认错了，比如片名动画的几帧）
        try:
            again = _ask_once(llm, [group[j]], [paths[j]], frames_for([j]) if frames_for else None,
                              [boxed[j]])[0] if len(group) > 1 else {}
        except Cancelled:  # 停止按钮：不能当成一次普通失败吞掉
            raise
        except Exception:
            again = {}
        if (again.get("text") or "").strip():
            got[j] = again
            log(f"  {group[j]['id']}：和同一批另一张图的字一样、和本地识别对不上，单独重新复核")
        else:
            # 单独问也没认出字：文字保留本地结果；单独问时给的类型没有串图的问题，照用（不然没有类型，招牌上的字会当成字幕去翻译）
            log(f"  {group[j]['id']}：复核结果和同一批另一张图的字一样、和本地识别对不上（串图了），保留本地结果")
            got[j] = {"kind": again["kind"]} if again.get("kind") in ("caption", "scene") else {}
    return got


def _ask_each(llm, group, paths, frames_for=None, boxed=None):
    """一批复核失败（返回条数不对、不是 JSON…）时逐条单独问；单独问也失败的那条保留本地结果。"""
    boxed = boxed or [False] * len(paths)
    out = []
    for j in range(len(group)):
        try:
            out.append(_ask_once(llm, [group[j]], [paths[j]], frames_for([j]) if frames_for else None, [boxed[j]])[0])
        except Cancelled:  # 停止按钮：不能当成一次普通失败吞掉
            raise
        except Exception as ex:
            log(f"  {group[j]['id']}：单独复核也失败，保留本地结果：{ex}")
            out.append({})
    return out


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


def _snap_t(e):
    return e["snap_t"] if e.get("snap_t") is not None else (e["start"] + e["end"]) / 2


def _moved(e):
    """截图框比字大一圈以上：字在画面上挪过（镜头摇移、逐字出现）。"""
    x0, y0, x1, y1 = e["rect"]
    X0, Y0, X1, Y1 = e.get("crop_rect") or e["rect"]
    return (X1 - X0) * (Y1 - Y0) > 1.5 * max(1e-6, (x1 - x0) * (y1 - y0))


def box_at(e, t):
    """这处字在 t 秒的画面上的框（0~1）。那一刻对不上（不在画面上、字或镜头在移动）返回 None。"""
    if abs(t - _snap_t(e)) < 0.01:  # 截图那一帧
        return e.get("snap_rect") or e.get("crop_rect") or e["rect"]
    if e.get("moving") or not (e["start"] <= t <= e["end"]) or _moved(e):
        return None  # 字挪过的话，别的时刻框不准
    return e["rect"]


def recrop(frame, e, t, out_dir):
    """从画面时刻那一帧重新截这处字的特写，用红框框出要转写的字。
    原来的特写是整段移动范围加一圈边（镜头摇移时会把片名、别处的字截进来，模型就转写了那些字）；
    这里只截字在那一刻的位置。旧项目没记下那一刻位置、字又挪过的，框不准，返回 None（用原来的特写）。"""
    import cv2

    from ..sources.ocr import _imread, _imwrite

    r = box_at(e, t)
    if r is None or (not e.get("snap_rect") and _moved(e)):
        return None
    path = os.path.join(out_dir, f"{e['id']}_{round(t * 1000)}.jpg")
    if os.path.exists(path):
        return path
    img = _imread(frame)
    if img is None:
        return None
    H, W = img.shape[:2]
    x0, y0, x1, y1 = round(r[0] * W), round(r[1] * H), round(r[2] * W), round(r[3] * H)
    lh = (x1 - x0 if e.get("vertical") else y1 - y0) / max(1, e.get("lines", 1))
    pad = max(16, int(lh * 0.8))
    X0, Y0, X1, Y1 = max(0, x0 - pad), max(0, y0 - pad), min(W, x1 + pad), min(H, y1 + pad)
    if X1 - X0 < 4 or Y1 - Y0 < 4:
        return None
    crop = img[Y0:Y1, X0:X1].copy()
    g = 4  # 框和字之间留几个像素，不压到字
    cv2.rectangle(crop, (max(0, x0 - X0 - g), max(0, y0 - Y0 - g)),
                  (min(X1 - X0 - 1, x1 - X0 + g), min(Y1 - Y0 - 1, y1 - Y0 + g)), (0, 0, 255), 2)
    os.makedirs(out_dir, exist_ok=True)
    return path if _imwrite(path, crop) else None


def plan_groups(todo, batch, context=False):
    """分批：每 batch 处一批。带整帧画面时按截图时刻排，再给每处定一个画面时刻（同一批里同一时刻都在画面上的共用一张画面）。
    返回 [(这一批, 各处的画面时刻 或 None)]。"""
    if context:
        todo = sorted(todo, key=lambda e: (_snap_t(e), e["start"]))
    out = []
    for i in range(0, len(todo), batch):
        group = todo[i:i + batch]
        if not context:
            out.append((group, None))
            continue
        moments, at = [], []
        for e in group:
            t = next((m for m in moments if box_at(e, m) is not None), None)
            if t is None:
                t = _snap_t(e)  # 自己的截图时刻：框一定准
                moments.append(t)
            at.append(t)
        out.append((group, at))
    return out


def mark_frame(frame, items, nos, t, out_dir, width=640):
    """整帧画面上框出这几处字、标上编号 nos（编号在框外，不挡字），缩到 width 宽存起来。
    640 宽一张约 300 token：认字靠特写，整帧画面只用来看字在什么东西上，够了。"""
    import cv2
    import numpy as np

    from ..sources.ocr import _imread, _imwrite

    name = hashlib.sha1(f"{frame}|{t}|{[e['id'] for e in items]}|{nos}|{width}".encode()).hexdigest()[:12]
    path = os.path.join(out_dir, f"{name}.jpg")
    if os.path.exists(path):
        return path
    img = _imread(frame)
    if img is None:
        return None
    H, W = img.shape[:2]
    if W > width:
        img = cv2.resize(img, (width, round(H * width / W)), interpolation=cv2.INTER_AREA)
        H, W = img.shape[:2]
    th = 2
    scale = max(0.55, H / 720 * 0.9)
    for e, no in zip(items, nos):
        r = box_at(e, t)
        if r is None:
            continue
        c = _COLORS[no % len(_COLORS)]
        p = th + 2
        x0, y0 = max(0, round(r[0] * W) - p), max(0, round(r[1] * H) - p)
        x1, y1 = min(W - 1, round(r[2] * W) + p), min(H - 1, round(r[3] * H) + p)
        cv2.rectangle(img, (x0, y0), (x1, y1), c, th)
        lab = str(no + 1)
        (tw, lh), base = cv2.getTextSize(lab, cv2.FONT_HERSHEY_SIMPLEX, scale, th)
        bw, bh = tw + 6, lh + base + 4
        # 标签放框外：上面放不下放下面，再不行放左边
        if y0 - bh >= 0:
            lx, ly = min(max(0, x0), W - bw), y0 - bh
        elif y1 + bh < H:
            lx, ly = min(max(0, x0), W - bw), y1 + 1
        else:
            lx, ly = max(0, x0 - bw), y0
        cv2.rectangle(img, (lx, ly), (lx + bw, ly + bh), c, -1)
        cv2.putText(img, lab, (lx + 3, ly + lh + 2), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), th, cv2.LINE_AA)
    os.makedirs(out_dir, exist_ok=True)
    return path if _imwrite(path, np.ascontiguousarray(img)) else None


def _grab(video, groups, out_dir, width=1920):
    """各处画面时刻的整帧画面。返回 {时刻: 路径}。
    截 1920 宽：重新截的特写比识别时（720 高）清楚一些，小字也看得清；发给模型的整帧画面另外缩小。"""
    import time

    from ..sources import media

    times = sorted({t for _, at in groups for t in at or []})
    t0 = time.time()
    got = media.grab_frames(str(video), times, out_dir, width=width, exact=True,
                            on_progress=lambda n, total: progress(n, total, "截取画面"))
    log(f"  截取画面 {len(got)} 张（用时 {time.time() - t0:.0f}s）")
    return {t: got.get(round(t, 3)) for t in times}


def refine_ocr(events, cfg, workdir, cache_dir, batch=4, video=None, context=False, frame_width=640, boxed=False):
    """context：每处字附上它所在时刻的整帧画面（框出位置、标上编号），判断是叠加字幕还是实物上的字准得多。
    boxed：特写从截图那一帧重新截（只截字那一刻的位置），并用红框框出要转写的字。
    batch：几处一批问。一批越多越省，但模型偶尔会把同一批另一张图上的字写过来（串图）；1 = 每处单独问。"""
    batch = max(1, int(batch))
    from .client import LLM, FatalLLMError

    llm = LLM(cfg, vision=True, cache_dir=cache_dir)
    todo = [e for e in events if e.get("crop") and "text_local" not in e and not e.get("prefiltered")]
    log(f"视觉大模型复核画面字：{len(todo)} 条")
    has_video = bool(todo and video and os.path.exists(str(video)))
    context, boxed = context and has_video, boxed and has_video
    groups = plan_groups(todo, batch, context or boxed)
    frames = _grab(video, groups, os.path.join(workdir, "crops", "frames")) if context or boxed else {}
    if context:
        log("  复核时附上整帧画面，看字是在实物上还是叠加的字幕")
    marked_dir = os.path.join(workdir, "crops", "marked")
    recrop_dir = os.path.join(workdir, "crops", "recrop")
    n_boxed = 0
    short = []  # 复核结果只有半截的：换一帧重新截图再问
    n_done = 0
    for gi, (group, at) in enumerate(groups):
        check_cancel()
        progress(n_done, len(todo), "复核", every=0)
        n_done += len(group)

        def frames_for(idx, group=group, at=at):
            """这几处（这一批里的序号）的整帧画面：同一时刻的共用一张，编号按 idx 里的先后。"""
            out = []
            for t in dict.fromkeys(at[j] for j in idx):
                mine = [k for k, j in enumerate(idx) if at[j] == t]
                f = frames.get(t) and mark_frame(frames[t], [group[idx[k]] for k in mine], mine, t, marked_dir,
                                                 width=frame_width)
                if f:
                    out.append((f, mine))
            return out

        paths, box = [], []
        for j, e in enumerate(group):
            p = recrop(frames[at[j]], e, at[j], recrop_dir) if boxed and frames.get(at[j]) else None
            paths.append(p or os.path.join(workdir, e["crop"]))
            box.append(bool(p))
        n_boxed += sum(box)
        try:
            got = _ask(llm, group, paths, frames_for if context else None, box)
        except FatalLLMError as ex:
            log(f"  {ex}")
            log("  视觉复核已跳过，继续使用本地 OCR 结果。请检查 [llm] vision_model / model")
            return events
        except Cancelled:  # 停止按钮：不能当成一次普通失败吞掉
            raise
        except Exception as ex:
            log(f"  第 {gi + 1} 批复核失败（{ex}），逐条单独复核")
            got = _ask_each(llm, group, paths, frames_for if context else None, box)
        for e, g in zip(group, got):
            e["text_local"] = e["text"]
            if truncated(g.get("text") or "", e["text"]):
                short.append(e)
                if g.get("kind") in ("caption", "scene"):
                    e["kind"] = g["kind"]
                continue
            _apply(e, g)
    if boxed:
        log(f"  {n_boxed} 条用截图那一帧重新截了特写、框出了要转写的字"
            + (f"（另外 {len(todo) - n_boxed} 条是旧的识别结果、字又挪过，用原来的特写）" if n_boxed < len(todo) else ""))
    if short:
        _retry_short(llm, short, workdir, video, batch, float(cfg.ocr.fps))
    # 整帧画面一批一张、占地方，复核完删掉（中途停止的留着，下次接着用；重新截出来的一样，请求缓存照样命中）
    for d in ("frames", "marked", "recrop"):
        shutil.rmtree(os.path.join(workdir, "crops", d), ignore_errors=True)
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
        except Cancelled:  # 停止按钮：不能当成一次普通失败吞掉
            raise
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
