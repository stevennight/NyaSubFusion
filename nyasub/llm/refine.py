"""视觉大模型复核画面字：纠正 OCR 错字、补全框不全的字、补出本地漏掉的字，并判断是不是实物上本来就有的字（招牌、包装…）。

做法：在画面字出现的时刻从原视频截一帧干净的画面（不画框、不附本地结果），让模型自己找出画面上所有的字，
框出位置、转写、判断类型；程序再按位置和本地 OCR 的结果对上——时间用本地的，字、类型、位置用模型的。
- 本地把一个标题切成几块的：几块对到模型的同一块字上，合成一条。
- 模型读到、本地完全没找到的（花字标题、手写招牌）：按模型的框补一条，回原视频前后找它什么时候出现、消失。
- 两边读得差很多的、字很小的、模型没读到的：从原视频按原分辨率截这一块的特写，再问一次。
- 本地找到了字的位置、但一个字也没认出来的地方（书法片名常这样）：附近没有别的要复核的画面时刻，就单独看那一帧，
  只补在那块地方读到的字。
不把本地结果给模型：给了它会顺着本地的框和错字走（框不全，补全时读错）。"""
import os
import re
import shutil
from difflib import SequenceMatcher

from ..env import Cancelled, check_cancel, log, progress
from ..textutil import full_sim, norm, sim

_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")

_KINDS = """   - "caption"：节目后期叠加在画面上的字，比如采访台词字幕、人名条、地名、说明文字、标题、资料来源、片尾字幕。
     特征：浮在画面最上层，字体规整清晰，通常有描边或阴影，横平竖直，不随镜头透视变形，不受画面光影影响。
   - "scene"：拍摄到的实物上本来就有的字，比如招牌、海报、地图、古文书、日历、商品包装和瓶子上的标签、手写的纸、车身文字和车牌、书脊。
     特征：印在、写在、挂在画面里的物体上，跟随物体有透视或弯曲，受光照、景深、反光影响，字体五花八门。"""

READ_SYSTEM = f"""你是影视节目的画面文字校对员。给你视频里的一帧画面，找出画面上所有能读出来的文字，每一块一条：
一行或上下连在一起的几行字幕、一列或几列竖排字、一个标题、招牌 / 包装上的一组字。
对每一块：
1. text：准确、完整地转写，每一行、每一个字都要转写，不能漏字、漏掉行尾。
   同一块里的几行用 " / " 分隔；竖排按从右到左的列顺序，每列用 " / " 分隔。读不出来的块不要输出。
   汉字上方（竖排是右边）标读音的小字（振假名）不要转写，也不单独算一块。
2. kind：这块字是哪一类——
{_KINDS}
3. bbox：这块字在画面上的范围 [x0, y0, x1, y1]，按画面宽高归一到 0~1000（左上角是 0,0，右下角是 1000,1000），把每个字的笔画完整包进去。
输出 JSON：{{"items": [{{"text": "原文", "kind": "caption", "bbox": [x0, y0, x1, y1]}}, ...]}}；画面上没有字就输出 {{"items": []}}。"""

RECHECK_SYSTEM = f"""你是影视节目的画面文字校对员。给你视频画面里一处文字的高清特写（四周多截了一圈背景）。
1. text：准确、完整地转写特写中间那一块文字（一行或几行字幕、一列竖排字、一个标题、招牌上的一组字），每一个字都要转写；
   边上露出来的别处的字不要写。几行用 " / " 分隔；竖排按从右到左的列顺序，每列用 " / " 分隔；振假名（标读音的小字）不要转写。
   看不清就输出空字符串。附带的候选是别的程序读出来的，可能都不对，一切以图为准。
2. kind：这块字是哪一类——
{_KINDS}
输出 JSON：{{"text": "原文", "kind": "caption"}}"""

# 没有原视频（只剩识别时截的特写）时用：一批几张特写，附本地 OCR 参考
VLM_SYSTEM = """你是影视节目的画面文字校对员。会给你若干张截图，每张前面标着「第 n 张」，每张是画面上一处文字及其周围一小圈背景。
各张图互不相关：每张只转写这一张图里的字，不要把别的图上的字写进来。
对每张图做两件事：
1. text：准确、完整地转写图中的原文，每一行、每一个字都要转写，不能漏行、漏掉行尾。
   横排多行用 " / " 分隔；竖排按从右到左的列顺序，每列用 " / " 分隔。看不清就输出空字符串。
   附带的“本地 OCR 参考”可能有错字、多出噪点字符，也可能少字；一切以图片为准。
   汉字上方（竖排是右边）标读音的小字（振假名）不是单独的一行，不要转写。
2. kind：判断这处文字是哪一类——
   - "caption"：节目后期叠加在画面上的字幕，比如采访台词字幕、人名条、地名、说明文字、标题、资料来源、片尾字幕。
     特征：字体规整清晰，通常有描边或阴影，横平竖直，不随镜头透视变形，不受画面光影影响。
   - "scene"：拍摄到的实物上本来就有的字，比如招牌、海报、地图、古文书、日历、商品包装和标签、手写字、车身文字、电视台台标。
     特征：跟随物体有透视或弯曲，受光照、景深、反光影响，字体五花八门。
输出 JSON：{"items": [{"no": 1, "text": "第1张原文", "kind": "caption"}, ...]}，no 是图前面标的编号，数量必须和图片数量一致。"""

MATCH = 0.3        # 两个框重叠部分占小的那个的比例，到这么多算同一处字
SMALL_LINE = 0.035  # 每行字高不到画面高的这么多（720p 约 25 像素）：整帧画面上看不清，截特写再问
TINY_MISS = 0.04   # 本地漏掉的字，框的长边不到画面的这么多（车牌、远处的小标签）：不补
NEAR_T = 1.0       # 模型在某一刻读到、本地这块字前后这么多秒内才有的：是同一块字（本地晚一点 / 早一点认出来）
CROP_MAX_W = 1600  # 特写太宽就缩到这么宽（再宽只是多花 token）


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


def _ask_once(llm, group, paths):
    hint = "\n".join(f"第{j + 1}张（本地 OCR 参考）：{e['text_local' if 'text_local' in e else 'text'].replace(chr(10), ' / ')}"
                     for j, e in enumerate(group))
    # 每张图前面标上编号：只靠顺序时模型容易把相邻两张图的字搞混
    images = [{"path": p, "label": f"第 {j + 1} 张"} for j, p in enumerate(paths)]
    res = llm.chat_json(VLM_SYSTEM, f"共 {len(group)} 张图。\n{hint}", images=images)
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


def _ask(llm, group, paths):
    got = _ask_once(llm, group, paths)
    for j in borrowed(group, got):
        # 像是串图了：这一张单独再问一次。只有一张图就不可能串，单独问的结果直接用
        # （也可能根本没串：几张图拍的是同一段字、本地识别又认错了，比如片名动画的几帧）
        try:
            again = _ask_once(llm, [group[j]], [paths[j]])[0] if len(group) > 1 else {}
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


def _ask_each(llm, group, paths):
    """一批复核失败（返回条数不对、不是 JSON…）时逐条单独问；单独问也失败的那条保留本地结果。"""
    out = []
    for j in range(len(group)):
        try:
            out.append(_ask_once(llm, [group[j]], [paths[j]])[0])
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


# ---------------- 整帧复核 ----------------


def plan_moments(todo):
    """给每处字定一个画面时刻：按截图时刻排，前面已有的时刻里它也在画面上（框对得上）就共用那一帧，否则用自己的截图时刻。
    返回 {时刻: [在这一刻复核的字]}。"""
    moments = {}
    for e in sorted(todo, key=lambda e: (_snap_t(e), e["start"])):
        t = next((m for m in moments if box_at(e, m) is not None), None)
        if t is None:
            t = _snap_t(e)
            moments[t] = []
        moments[t].append(e)
    return moments


def _box(b):
    """模型给的 0~1000 的框换成 0~1；不像框的返回 None。"""
    try:
        x0, y0, x1, y1 = (min(1.0, max(0.0, float(v) / 1000)) for v in b)
    except (TypeError, ValueError):
        return None
    if x1 - x0 < 0.003 or y1 - y0 < 0.003:
        return None
    return [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)]


def _area(r):
    return max(1e-9, (r[2] - r[0]) * (r[3] - r[1]))


def overlap(a, b):
    """两个框重叠部分占小的那个的比例。"""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return ix * iy / min(_area(a), _area(b))


def _covered(a, b):
    """框 b 有多少被框 a 盖住。"""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return ix * iy / _area(b)


def _union(rs):
    return [min(r[0] for r in rs), min(r[1] for r in rs), max(r[2] for r in rs), max(r[3] for r in rs)]


def _together(a, b):
    """两条字在时间上大半重叠（同一个标题被切成几块，几块一起出现、一起消失）。"""
    inter = min(a["end"], b["end"]) - max(a["start"], b["start"])
    return inter >= 0.5 * min(a["end"] - a["start"], b["end"] - b["start"])


def read_items(raw, regions):
    """模型读出的字：框换成 0~1，去掉中心落在排除区（台标、角标）里的、空的。"""
    out = []
    for x in raw.get("items") or []:
        if not isinstance(x, dict):
            continue
        b, text = _box(x.get("bbox")), (x.get("text") or "").strip()
        if not b or not norm(text):
            continue
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        if any(g.get("exclude") and g["rect"][0] <= cx <= g["rect"][2] and g["rect"][1] <= cy <= g["rect"][3]
               for g in regions):
            continue
        out.append({"text": text.replace(" / ", "\n"), "kind": x.get("kind"), "box": b})
    return out


def match_moment(items, visible, mine):
    """一帧上模型读出的字和本地的字按位置对上。
    visible：[(这一刻在画面上的本地字, 它这一刻的框)]（包括不在这一刻复核的，免得当成漏检）；mine：这一刻复核的字的 id。
    返回 (groups, misses)：groups = [(合成一条的本地字（第一条留下）, 对上的模型字)]（没对上模型字的组，模型字为空），
    misses = 和本地哪条都对不上的模型字。
    每块模型字归重叠最多的那条本地字；几条一起出现的本地字都是和同一块模型字重叠最多（被切开的标题），合成一条。
    标题的大框顺带盖住下面的字幕不算：字幕和它自己那块模型字重叠得更多。"""
    hits = [[(overlap(it["box"], b), e) for e, b in visible] for it in items]
    hits = [[(o, e) for o, e in h if o >= MATCH] for h in hits]
    best = {}  # 本地字 id → 和它重叠最多的模型字序号
    for k, h in enumerate(hits):
        for o, e in h:
            if e["id"] not in best or o > best[e["id"]][0]:
                best[e["id"]] = (o, k)
    by_id = {e["id"]: e for e, _ in visible}
    groups = []  # [[本地字...], [模型字序号...]]
    where = {}
    for i in (i for i in by_id if i in mine):
        e = by_id[i]
        k = best.get(i, (0, None))[1]
        g = next((g for g in groups if k is not None and k in g[2] and all(_together(e, x) for x in g[0])), None)
        if g is None:
            g = [[], [], set() if k is None else {k}]
            groups.append(g)
        g[0].append(e)
        where[i] = g
    misses = []
    for k, (it, h) in enumerate(zip(items, hits)):
        if not h:
            misses.append(it)
            continue
        o, e = max(h, key=lambda x: x[0])
        if e["id"] in where:
            where[e["id"]][1].append(it)
    return [(sorted(evs, key=lambda e: (e["start"], e["rect"][1])), its) for evs, its, _ in groups], misses


def _reading_order(items, vertical):
    if vertical:
        return sorted(items, key=lambda it: -it["box"][2])
    return sorted(items, key=lambda it: ((it["box"][1] + it["box"][3]) / 2, it["box"][0]))


def _line_px(rect, text, vertical, aspect):
    """每行（竖排是每列）字的大小，按画面高的比例算。"""
    n = max(1, len([ln for ln in text.split("\n") if ln.strip()]))
    return ((rect[2] - rect[0]) * aspect if vertical else rect[3] - rect[1]) / n


def _differs(model, local):
    """模型读的和本地读的差很多（本地认出的字够多、又不在模型的结果里）。"""
    a, b = norm(model), norm(local)
    return len(b) >= 3 and b not in a and sim(model, local) < 50


class _Frame:
    """一个画面时刻：从原视频按原分辨率截的整帧，缩小给模型看，按需截特写。"""

    def __init__(self, path, out_dir, width):
        from ..sources.ocr import _imread

        self.img = _imread(str(path)) if path else None
        self.path, self.dir, self.width = path, out_dir, width

    def scaled(self, name):
        import cv2

        from ..sources.ocr import _imwrite

        H, W = self.img.shape[:2]
        img = self.img if W <= self.width else cv2.resize(self.img, (self.width, round(H * self.width / W)),
                                                          interpolation=cv2.INTER_AREA)
        p = os.path.join(self.dir, f"{name}.jpg")
        return p if _imwrite(p, img) else None

    def crop(self, r, name):
        """这一块字的特写：原分辨率，四周多留一圈（左右留字宽的 15%，上下留字高的 30%），太宽的缩到 CROP_MAX_W。"""
        import cv2

        from ..sources.ocr import _imwrite

        H, W = self.img.shape[:2]
        mx, my = (r[2] - r[0]) * 0.15 + 0.01, (r[3] - r[1]) * 0.3 + 0.01
        X0, Y0 = max(0, int((r[0] - mx) * W)), max(0, int((r[1] - my) * H))
        X1, Y1 = min(W, int((r[2] + mx) * W)), min(H, int((r[3] + my) * H))
        if X1 - X0 < 4 or Y1 - Y0 < 4:
            return None
        c = self.img[Y0:Y1, X0:X1]
        if c.shape[1] > CROP_MAX_W:
            c = cv2.resize(c, (CROP_MAX_W, round(c.shape[0] * CROP_MAX_W / c.shape[1])), interpolation=cv2.INTER_AREA)
        p = os.path.join(self.dir, f"{name}.jpg")
        return p if _imwrite(p, c) else None


def _recheck(llm, frame, r, cands, name):
    """截特写再问一次。返回 {"text", "kind"}（问不出来返回 {}）。"""
    p = frame.crop(r, name) if frame.img is not None else None
    if not p:
        return {}
    cand = "\n".join(f"候选 {i + 1}：{c.replace(chr(10), ' / ')}" for i, c in enumerate(dict.fromkeys(c for c in cands if c)))
    from .client import FatalLLMError

    try:
        g = llm.chat_json(RECHECK_SYSTEM, cand or "（没有候选）", images=[{"path": p, "label": "特写", "detail": "high"}])
    except (Cancelled, FatalLLMError):
        raise
    except Exception as ex:
        log(f"  特写再问失败：{ex}")
        return {}
    return {"text": (g.get("text") or "").strip().replace(" / ", "\n"),
            **({"kind": g["kind"]} if g.get("kind") in ("caption", "scene") else {})}


def _merge_into(keep, others):
    """被切开的几块合成一条：时间取并集，每块字的框和文字都留着。"""
    keep["start"] = round(min(e["start"] for e in [keep, *others]), 3)
    keep["end"] = round(max(e["end"] for e in [keep, *others]), 3)
    keep["pieces"] = [p for e in [keep, *others] for p in e.get("pieces") or []]
    keep["merged"] = [*keep.get("merged", []), *[e["id"] for e in others]]


def fold_dups(events):
    """复核完同一块字还剩几条的，并成一条：本地切出的一小块在另一个画面时刻单独复核，模型读出整块、按整块重新找了时间，
    就和本地认全的那条重复了；或者模型那一刻只读出标题的一个字。条件：框大半落在另一条里、时间大半重叠、字是它的一部分。
    返回被并掉的条数。"""
    keep, gone = sorted(events, key=lambda e: -_area(e["rect"])), set()
    for i, big in enumerate(keep):
        if big["id"] in gone:
            continue
        for small in keep[i + 1:]:
            if (small["id"] in gone or big.get("moving") or small.get("moving") or not norm(small["text"])
                    or (big.get("dropped") and not small.get("dropped"))  # 不把有用的并进杂讯里
                    or _covered(big["rect"], small["rect"]) < 0.8 or not _together(big, small)
                    or norm(small["text"]) not in norm(big["text"])):
                continue
            _merge_into(big, [small])
            gone.add(small["id"])
            log(f"  {big['id']}：{small['id']}（{small['text'].replace(chr(10), ' / ')}）是同一块字，合成一条")
    events[:] = [e for e in events if e["id"] not in gone]
    return len(gone)


def refine_ocr(events, cfg, workdir, cache_dir, video=None, size=None, batch=4, unread=None):
    """size：原视频的 (宽, 高)，特写按这个分辨率截。没有原视频时退回只看识别时截的特写（一批 batch 张）。
    unread：本地找到位置但认不出字的地方（sources/ocr.scan），有原视频时也去看。"""
    from .client import LLM, FatalLLMError

    llm = LLM(cfg, vision=True, cache_dir=cache_dir)
    todo = [e for e in events if e.get("crop") and "text_local" not in e]
    frames = bool(video and os.path.exists(str(video)) and size)
    # 台标、振假名不用复核。按画面复核时，判成杂讯的（很短、一闪而过）也看：本地常常只认出花字标题的一小块
    # （「森のなかのふたり」只认出「たり」、只闪了半秒），模型看整帧能读出整个标题；看不出字的照旧当杂讯，不再追问
    todo = [e for e in todo if not e.get("prefiltered") or (frames and e.get("dropped") == "junk")]
    unread = (unread or []) if frames else []
    log(f"视觉大模型复核画面字：{len(todo)} 条" + (f"，另看本地认不出字的 {len(unread)} 处" if unread else ""))
    if not todo and not unread:
        return events
    try:
        if frames:
            return _refine_frames(llm, events, todo, cfg, workdir, video, size, unread)
        return _refine_crops(llm, events, todo, workdir, batch)
    except FatalLLMError as ex:
        _fatal(ex)
        return events
    finally:
        for d in ("frames", "read"):
            shutil.rmtree(os.path.join(workdir, "crops", d), ignore_errors=True)


def _fatal(ex):
    log(f"  {ex}")
    log("  视觉复核已跳过，继续使用本地 OCR 结果。请检查 [llm] vision_model / model")


def plan_unread(moments, unread):
    """本地认不出字的地方：它停留的时间里已经有要复核的画面时刻（那一帧整个都会读），就不另看；
    否则在它框得最全的那一刻加一个画面时刻。返回 {新加的时刻: [那块地方的框]}。"""
    hints = {}
    for u in sorted(unread, key=lambda u: u["t"]):
        if any(u["start"] - 0.01 <= m <= u["end"] + 0.01 for m in list(moments) + list(hints)):
            continue
        hints[u["t"]] = [u["rect"]]
    for t in hints:
        moments[t] = []
    return hints


def _refine_frames(llm, events, todo, cfg, workdir, video, size, unread=()):
    from .client import FatalLLMError

    W, H = size
    aspect = W / max(1, H)
    width = cfg.ocr.refine_frame_width if cfg.ocr.refine_frame_width >= 1024 else 1280
    regions = [g.model_dump() for g in cfg.ocr.regions]
    moments = plan_moments(todo)
    hints = plan_unread(moments, unread)
    times = sorted(moments)
    log(f"  按画面复核：{len(times)} 个画面时刻" + (f"（其中 {len(hints)} 个是专门看本地认不出字的地方）" if hints else "")
        + f"，整帧画面 {width} 宽，特写按原视频 {W}x{H} 截")
    frame_dir = os.path.join(workdir, "crops", "frames")
    read_dir = os.path.join(workdir, "crops", "read")
    os.makedirs(read_dir, exist_ok=True)
    gone, added, short = set(), [], []
    n = {"read": 0, "matched": 0, "merged": 0, "missed": 0, "recheck": 0, "unseen": 0, "retimed": 0}
    chunk = 12  # 原分辨率的整帧一张几 MB：一次截一小批，用完就删
    try:
        _frames_loop(llm, events, moments, times, chunk, video, W, H, aspect, width, regions, frame_dir, read_dir,
                     gone, added, short, n, workdir, hints)
    except FatalLLMError as ex:  # 已经复核的留着结果（合并、补出的照样生效），没复核的保持本地结果
        _fatal(ex)
        short = []
    progress(len(times), len(times), "复核")
    n["missed"] = len(added)
    log(f"  模型读到 {n['read']} 块字：对上本地 {n['matched']} 条（其中 {n['retimed']} 条本地只认出一小块、重新找了时间），"
        f"合并本地切开的 {n['merged']} 块，补出本地漏掉的 {n['missed']} 处；模型没读到的 {n['unseen']} 条；截特写再问 {n['recheck']} 次")
    if short:
        _retry_short(llm, short, workdir, video, 4, float(cfg.ocr.fps))
    events[:] = sorted([e for e in events if e["id"] not in gone] + added, key=lambda e: (e["start"], e["rect"][1]))
    fold_dups(events)
    return events


def _frames_loop(llm, events, moments, times, chunk, video, W, H, aspect, width, regions, frame_dir, read_dir,
                 gone, added, short, n, workdir, hints=None):
    from ..sources import media
    from ..sources.ocr import _zone, track_region
    from .client import FatalLLMError

    for ci in range(0, len(times), chunk):
        part = times[ci:ci + chunk]
        got = media.grab_frames(str(video), part, frame_dir, width=W, exact=True, quality=2)
        for ti, t in enumerate(part):
            check_cancel()
            progress(ci + ti, len(times), "复核")
            mine_events = [e for e in moments[t] if e["id"] not in gone]
            frame = _Frame(got.get(round(t, 3)), read_dir, width)
            key = f"{round(t * 1000):09d}"
            if frame.img is None:  # 截不出画面：这几条保持本地结果
                for e in mine_events:
                    e["text_local"] = e["text"]
                continue
            try:
                raw = llm.chat_json(READ_SYSTEM, "这是画面。", images=[{"path": frame.scaled(key), "label": "画面", "detail": "high"}])
                items = read_items(raw, regions)
            except (Cancelled, FatalLLMError):
                raise
            except Exception as ex:
                log(f"  {t:.1f}s 这一帧复核失败（{ex}），逐条截特写复核")
                items = None
            visible = [(e, b) for e in events if e["id"] not in gone and (b := box_at(e, t)) is not None]
            mine = {e["id"] for e in mine_events}
            if items is None:
                groups, misses = [([e], []) for e in mine_events], []
            else:
                n["read"] += len(items)
                groups, misses = match_moment(items, visible, mine)
            boxes = dict((e["id"], b) for e, b in visible)
            for evs, its in groups:
                keep = evs[0]
                local = "\n".join(e["text"] for e in evs)
                keep["text_local"] = local
                if len(evs) > 1:
                    _merge_into(keep, evs[1:])
                    gone.update(e["id"] for e in evs[1:])
                    n["merged"] += len(evs) - 1
                    log(f"  {keep['id']}：和 {'、'.join(e['id'] for e in evs[1:])} 是同一块字（本地切开了），合成一条")
                if not its and keep.get("dropped") == "junk":
                    continue  # 判成杂讯的、模型也没读到：照旧当杂讯
                if not its:  # 模型没读到这处字：截特写再问；还是读不出就保持本地结果
                    n["unseen"] += 1
                    g = _recheck(llm, frame, boxes[keep["id"]], [local], f"{key}_{keep['id']}")
                    n["recheck"] += bool(g)
                    if truncated(g.get("text") or "", local):
                        short.append(keep)
                        g = {"kind": g["kind"]} if g.get("kind") else {}
                    _apply(keep, g)
                    continue
                n["matched"] += 1
                its = _reading_order(its, keep.get("vertical"))
                text = "\n".join(it["text"] for it in its)
                kind = max(its, key=lambda it: _area(it["box"]))["kind"]
                box = _union([it["box"] for it in its])
                if _differs(text, local) or _line_px(box, text, keep.get("vertical"), aspect) < SMALL_LINE:
                    g = _recheck(llm, frame, box, [text, local], f"{key}_{keep['id']}")
                    n["recheck"] += bool(g)
                    if g.get("text"):
                        text = g["text"]
                    kind = g.get("kind", kind)
                if truncated(text, local):  # 这一帧字还没出全（逐字出现的字幕）：换一帧再问
                    short.append(keep)
                    if kind in ("caption", "scene"):
                        keep["kind"] = kind
                    continue
                _apply(keep, {"text": text, "kind": kind})
                keep["lines"] = len([ln for ln in keep["text"].split("\n") if ln.strip()]) or 1
                if not keep.get("moving"):  # 位置用模型框的（本地框常常不全）；滚动的字只有那一刻的框，不动
                    mine_box = _union([boxes[e["id"]] for e in evs])
                    if _covered(mine_box, box) < 0.6:
                        # 本地只认出这块字的一小部分（花字标题），认出的那一小块常常只闪了一下，时间也不全：
                        # 按模型的框回原视频前后找这块字待了多久，和本地的时间合起来
                        start, end = track_region(str(video), t, box, aspect)
                        keep["start"], keep["end"] = round(min(keep["start"], start), 3), round(max(keep["end"], end), 3)
                        n["retimed"] += 1
                    keep["rect_local"] = keep["rect"]
                    keep["rect"] = keep["snap_rect"] = box
                    keep["zone"] = _zone(box, keep.get("vertical"))
            only = (hints or {}).get(t)
            for it in misses:
                b = it["box"]
                if max(b[2] - b[0], b[3] - b[1]) < TINY_MISS:
                    continue
                if only and not any(overlap(b, r) >= MATCH for r in only):
                    continue  # 专门看本地认不出字的那块地方：别处的字（背景里的招牌…）本地都没找到过，不补
                if any(overlap(b, e["rect"]) >= MATCH and e["start"] <= t <= e["end"] for e in added):
                    continue  # 前面的画面时刻已经补过这块字
                if any(e["start"] <= t <= e["end"] and overlap(b, e.get("crop_rect") or e["rect"]) >= MATCH
                       for e in events if e["id"] not in gone):
                    continue  # 本地有（滚动的字这一刻的框对不上，按整段范围算）
                late = next((e for e in events if e["id"] not in gone and e["start"] - NEAR_T <= t <= e["end"] + NEAR_T
                             and overlap(b, e.get("crop_rect") or e["rect"]) >= MATCH and sim(it["text"], e["text"]) >= 80), None)
                if late:
                    # 本地也有这块字，只是隔一下才认出来（刚淡入、本地按间隔取帧）：不另补一条（另补的那条找不到时间时
                    # 按前后各 1 秒算，字没出来译文就先出来了），本地那条的时间放宽到这一刻
                    late["start"], late["end"] = round(min(late["start"], t), 3), round(max(late["end"], t), 3)
                    continue
                vertical = (b[3] - b[1]) * H > 1.5 * (b[2] - b[0]) * W and len(norm(it["text"])) > 1
                text, kind = it["text"], it["kind"]
                if _line_px(b, text, vertical, aspect) < SMALL_LINE:
                    g = _recheck(llm, frame, b, [text], f"{key}_m{len(added)}")
                    n["recheck"] += bool(g)
                    text, kind = g.get("text") or text, g.get("kind", kind)
                start, end = track_region(str(video), t, b, aspect)
                ev = {"id": f"v{len(added) + 1}", "start": start, "end": end, "text": text, "text_local": "",
                      "rect": b, "vertical": vertical, "lines": len([ln for ln in text.split("\n") if ln.strip()]) or 1,
                      "zone": _zone(b, vertical), "score": 1.0, "crop_rect": b, "snap_t": round(t, 3), "snap_rect": b,
                      "pieces": [[*b, text]], "found_by": "vlm",
                      **({"kind": kind} if kind in ("caption", "scene") else {})}
                p = frame.crop(b, f"{key}_v{len(added) + 1}")
                if p:
                    dst = os.path.join(workdir, "crops", f"{ev['id']}.jpg")
                    shutil.copyfile(p, dst)
                    ev["crop"] = os.path.relpath(dst, workdir).replace("\\", "/")
                added.append(ev)
                log(f"  补出本地漏掉的字（{start:.1f}–{end:.1f}s）：{text.replace(chr(10), ' / ')}")
        shutil.rmtree(frame_dir, ignore_errors=True)


def _refine_crops(llm, events, todo, workdir, batch):
    """没有原视频：一批几张识别时截的特写，附本地 OCR 参考。"""
    from .client import FatalLLMError

    batch = max(1, int(batch))
    for i in range(0, len(todo), batch):
        check_cancel()
        progress(i, len(todo), "复核", every=0)
        group = todo[i:i + batch]
        paths = [os.path.join(workdir, e["crop"]) for e in group]
        try:
            got = _ask(llm, group, paths)
        except (Cancelled, FatalLLMError):
            raise
        except Exception as ex:
            log(f"  第 {i // batch + 1} 批复核失败（{ex}），逐条单独复核")
            got = _ask_each(llm, group, paths)
        for e, g in zip(group, got):
            e["text_local"] = e["text"]
            _apply(e, g)
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
