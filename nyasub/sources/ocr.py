"""画面字识别：全片按帧率扫描 → 逐帧 OCR → 跨帧跟踪 → 合并成多行字幕事件。

整片都扫，不只扫字幕的空档：一句话可能半句在字幕、半句在画面字上。
识别区域可以有多个识别区和排除区（台标、固定角标）。
"""
import os
import shutil
import statistics
import time
from collections import defaultdict

import cv2
import numpy as np
from rapidfuzz import fuzz

from ..env import check_cancel, log
from ..textutil import norm
from . import media


def _imwrite(path, img):
    """cv2.imwrite 在 Windows 上写不了含中日文的路径（而且不报错），先编码再自己写文件。"""
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if ok:
        buf.tofile(path)
    return ok


def _imread(path):
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)


def resolve(c):
    """确定实际用 GPU 还是 CPU、用哪种模型（det_model / rec_model 为 auto 时按有没有 GPU 选）。"""
    from .. import gpu

    cuda = bool(c.get("use_cuda")) and gpu.ocr_ready()
    size = "medium" if cuda else "small"
    return dict(c, use_cuda=cuda, det_model=size if c["det_model"] == "auto" else c["det_model"],
                rec_model=size if c["rec_model"] == "auto" else c["rec_model"])


def make_engine(c, ocr_lang="japan"):
    from rapidocr import LangRec, ModelType, RapidOCR

    if c.get("use_cuda") and not resolve(c)["use_cuda"]:
        log("没有可用的 CUDA（缺显卡或“画面字识别 GPU 加速”组件），画面字识别用 CPU（会慢很多，建议安装 GPU 加速组件）")
    c = resolve(c)
    params = {
        "Global.log_level": "error",
        "Global.text_score": float(c["min_score"]),
        "Det.model_type": ModelType(c["det_model"]),
        "Rec.model_type": ModelType(c["rec_model"]),
        "Rec.lang_type": LangRec(ocr_lang),
    }
    if c.get("use_cuda"):
        params["EngineConfig.onnxruntime.use_cuda"] = True
    return RapidOCR(params=params)


def _detect(eng, img, min_score):
    r = eng(img)
    if r is None or r.boxes is None or r.txts is None:
        return []
    out = []
    for box, txt, score in zip(r.boxes, r.txts, r.scores):
        txt = (txt or "").strip()
        score = float(score)
        n = norm(txt)
        if score < min_score or not n:
            continue
        xs, ys = box[:, 0], box[:, 1]
        rect = (float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max()))
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        if w < 4 or h < 4:
            continue
        # 单个字符太容易是噪点，要求高置信度
        if len(n) == 1 and score < 0.9:
            continue
        out.append({"rect": rect, "text": txt, "score": score, "vertical": h > w * 1.5 and len(txt) > 1})
    return out


def _iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua


class Track:
    __slots__ = ("id", "first", "last", "rects", "obs", "vertical", "snapshot", "rect")

    def __init__(self, tid, t, d):
        self.id = tid
        self.first = self.last = t
        self.rects = [d["rect"]]
        self.rect = d["rect"]
        self.obs = [(d["text"], d["score"])]
        self.vertical = d["vertical"]
        self.snapshot = None

    def add(self, t, d):
        self.last = t
        self.rects.append(d["rect"])
        self.rect = d["rect"]
        self.obs.append((d["text"], d["score"]))

    def best_text(self):
        # 同一文字出现越多、越长、置信度越高越优先（避免取到渐入时的半截文字）
        weight = defaultdict(float)
        for txt, s in self.obs:
            weight[txt] += s * max(1, len(norm(txt)))
        return max(weight.items(), key=lambda kv: kv[1])[0]

    def median_rect(self):
        return tuple(statistics.median(r[i] for r in self.rects) for i in range(4))

    def score(self):
        return sum(s for _, s in self.obs) / len(self.obs)


def _match(tracks, dets):
    """贪心匹配：位置重叠 + 文字相似。返回 [(det, track or None)]。"""
    pairs = []
    for di, d in enumerate(dets):
        nd = norm(d["text"])
        for ti, tr in enumerate(tracks):
            iou = _iou(d["rect"], tr.rect)
            if iou < 0.3:
                continue
            nt = norm(tr.obs[-1][0])
            s = fuzz.ratio(nd, nt)
            if s < 60 and not (nd in nt or nt in nd):
                continue
            pairs.append((iou + s / 100, di, ti))
    pairs.sort(reverse=True)
    used_d, used_t, res = set(), set(), {}
    for _, di, ti in pairs:
        if di in used_d or ti in used_t:
            continue
        used_d.add(di)
        used_t.add(ti)
        res[di] = tracks[ti]
    return [(d, res.get(i)) for i, d in enumerate(dets)]


def _in_regions(rect, W, H, include, exclude):
    """检测框中心落在某个识别区里、且不在任何排除区里。"""
    cx, cy = (rect[0] + rect[2]) / 2 / W, (rect[1] + rect[3]) / 2 / H
    inside = lambda r: r[0] <= cx <= r[2] and r[1] <= cy <= r[3]
    return any(inside(r) for r in include) and not any(inside(r) for r in exclude)


def scan(video, info, c, workdir, ocr_lang="japan"):
    """c 是 OcrCfg 的 dict。返回画面字事件列表。"""
    fps = float(c["fps"])
    vinfo = info["video"]
    W, H = media.frame_size(vinfo, c["height"])
    include = [tuple(g["rect"]) for g in c["regions"] if not g.get("exclude")] or [(0.0, 0.0, 1.0, 1.0)]
    exclude = [tuple(g["rect"]) for g in c["regions"] if g.get("exclude")]
    # 只把识别区的外接框送去 OCR，省时间
    bx0, by0 = min(r[0] for r in include), min(r[1] for r in include)
    bx1, by1 = max(r[2] for r in include), max(r[3] for r in include)
    roi = (int(bx0 * W), int(by0 * H), int(bx1 * W), int(by1 * H))
    full_roi = roi == (0, 0, W, H)
    filter_regions = len(include) > 1 or bool(exclude)

    frames_dir = os.path.join(workdir, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    eng = make_engine(c, ocr_lang)
    r = resolve(c)
    log(f"扫描画面字：{fps} 帧/秒，分辨率 {W}x{H}，识别模型 {ocr_lang} / {r['det_model']}，{'GPU' if r['use_cuda'] else 'CPU'}")

    active, closed = [], []
    next_id = 1
    prev_thumb, prev_dets = None, []
    n_frames = n_ocr = 0
    t0 = time.time()
    last_report = 0.0
    duration = info["duration"] or 1
    max_gap = float(c["max_gap"])

    for t, frame in media.iter_frames(video, vinfo, fps, c["height"], info["start_time"], c.get("hwaccel", False)):
        check_cancel()
        n_frames += 1
        img = frame if full_roi else frame[roi[1]:roi[3], roi[0]:roi[2]]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        thumb = cv2.resize(gray, (192, 108), interpolation=cv2.INTER_AREA).astype(np.int16)
        if c["skip_static"] and prev_thumb is not None and np.percentile(np.abs(thumb - prev_thumb), 99.5) < 8:
            dets = prev_dets
        else:
            dets = _detect(eng, np.ascontiguousarray(img), float(c["min_score"]))
            if not full_roi:
                for d in dets:
                    r = d["rect"]
                    d["rect"] = (r[0] + roi[0], r[1] + roi[1], r[2] + roi[0], r[3] + roi[1])
            if filter_regions:
                dets = [d for d in dets if _in_regions(d["rect"], W, H, include, exclude)]
            prev_thumb, prev_dets = thumb, dets
            n_ocr += 1

        snap_path = None
        for d, tr in _match(active, dets):
            if tr is None:
                tr = Track(next_id, t, d)
                next_id += 1
                active.append(tr)
            else:
                tr.add(t, d)
                if tr.snapshot is None:
                    if snap_path is None:
                        snap_path = os.path.join(frames_dir, f"f{n_frames:06d}.jpg")
                        _imwrite(snap_path, frame)
                    tr.snapshot = snap_path
        still = []
        for tr in active:
            (still if t - tr.last <= max_gap else closed).append(tr)
        active = still

        if t - last_report >= 120:
            last_report = t
            el = time.time() - t0
            log(f"  画面字 {t / duration * 100:5.1f}%  已用 {el:.0f}s，预计还要 {el / max(t, 1) * (duration - t):.0f}s")
    closed += active
    log(f"  共 {n_frames} 帧，实际 OCR {n_ocr} 帧，跟踪到 {len(closed)} 条文字轨迹")

    events = _build_events(closed, c, fps, W, H, duration)
    crops_dir = os.path.join(workdir, "crops")
    os.makedirs(crops_dir, exist_ok=True)
    for ev in events:
        snap = ev.pop("_snapshot")
        if not snap or not os.path.exists(snap):
            continue
        img = _imread(snap)
        x0, y0, x1, y1 = ev["rect"]
        lh = (y1 - y0) * H / max(1, ev["lines"]) if not ev["vertical"] else (x1 - x0) * W / max(1, ev["lines"])
        pad = max(10, int(lh * 0.8))  # 多留点背景，方便判断是叠加字幕还是实物上的字
        X0, Y0 = max(0, int(x0 * W) - pad), max(0, int(y0 * H) - pad)
        X1, Y1 = min(W, int(x1 * W) + pad), min(H, int(y1 * H) + pad)
        path = os.path.join(crops_dir, f"{ev['id']}.jpg")
        _imwrite(path, img[Y0:Y1, X0:X1])
        ev["crop"] = os.path.relpath(path, workdir).replace("\\", "/")
    if not c.get("keep_frames"):
        shutil.rmtree(frames_dir, ignore_errors=True)
    return events


def _zone(rect, vertical):
    x0, y0, x1, y1 = rect
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    if vertical:
        return "right" if cx > 0.5 else "left"
    if cy > 0.62:
        return "bottom"
    if cy < 0.25:
        return "top"
    return "middle"


def _build_events(tracks, c, fps, W, H, duration):
    half = 0.5 / fps
    items = []
    for tr in tracks:
        start, end = tr.first - half, tr.last + half
        if end - start < float(c["min_duration"]) or len(tr.obs) < 2:
            continue
        items.append({"tr": tr, "start": max(0.0, start), "end": end, "rect": tr.median_rect(),
                      "text": tr.best_text(), "vertical": tr.vertical})
    # 台标/角标：持续过长，或同一位置同一文字反复出现、累计时长占比很高
    acc = defaultdict(float)
    for it in items:
        r = it["rect"]
        key = (norm(it["text"]), round((r[0] + r[2]) / W * 10), round((r[1] + r[3]) / H * 10))
        it["_key"] = key
        acc[key] += it["end"] - it["start"]
    max_len = min(float(c["persistent_seconds"]), duration * 0.6)
    items = [it for it in items
             if it["end"] - it["start"] <= max_len
             and acc[it["_key"]] <= duration * 0.25 and acc[it["_key"]] <= max(60.0, duration * 0.1)]

    # 并查集：同一时间、空间相邻的行合成一条
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    order = sorted(range(len(items)), key=lambda i: items[i]["start"])
    for ai_pos, ai in enumerate(order):
        a = items[ai]
        for bi in order[ai_pos + 1:]:
            b = items[bi]
            if b["start"] >= a["end"]:
                break
            if _same_group(a, b):
                parent[find(ai)] = find(bi)
    groups = defaultdict(list)
    for i in range(len(items)):
        groups[find(i)].append(items[i])

    events = []
    for members in groups.values():
        events.append(_compose(members, W, H))
    events.sort(key=lambda e: (e["start"], e["rect"][1]))
    for i, ev in enumerate(events):
        ev["id"] = f"o{i + 1}"
    return events


def _same_group(a, b):
    ov = min(a["end"], b["end"]) - max(a["start"], b["start"])
    if ov < 0.5 * min(a["end"] - a["start"], b["end"] - b["start"]):
        return False
    if a["vertical"] != b["vertical"]:
        return False
    ra, rb = a["rect"], b["rect"]
    if not a["vertical"]:
        ha, hb = ra[3] - ra[1], rb[3] - rb[1]
        h = max(ha, hb)
        if abs(ha - hb) > 0.35 * h:
            return False
        v_ov = min(ra[3], rb[3]) - max(ra[1], rb[1])
        h_gap = max(ra[0], rb[0]) - min(ra[2], rb[2])
        if v_ov >= 0.5 * min(ha, hb) and h_gap <= 1.5 * h:
            return True  # 同一行被拆成几段
        h_ov = min(ra[2], rb[2]) - max(ra[0], rb[0])
        v_gap = max(ra[1], rb[1]) - min(ra[3], rb[3])
        return h_ov > 0.2 * min(ra[2] - ra[0], rb[2] - rb[0]) and -0.3 * h <= v_gap <= 0.8 * h
    wa, wb = ra[2] - ra[0], rb[2] - rb[0]
    w = max(wa, wb)
    if abs(wa - wb) > 0.4 * w:
        return False
    h_gap = max(ra[0], rb[0]) - min(ra[2], rb[2])
    v_ov = min(ra[3], rb[3]) - max(ra[1], rb[1])
    return h_gap <= 1.0 * w and v_ov >= 0.3 * min(ra[3] - ra[1], rb[3] - rb[1])


def _join(pieces):
    out = ""
    for p in pieces:
        if out and out[-1].isascii() and out[-1].isalnum() and p[:1].isascii() and p[:1].isalnum():
            out += " "
        out += p
    return out


def _dedupe_pieces(members):
    """同一行有时整行识别一次、又拆成半行再识别一次：去掉被包含的那段。"""
    def same_place(a, b):
        ra, rb = a["rect"], b["rect"]
        if a["vertical"]:
            return min(ra[2], rb[2]) - max(ra[0], rb[0]) > 0.5 * min(ra[2] - ra[0], rb[2] - rb[0])
        return min(ra[3], rb[3]) - max(ra[1], rb[1]) > 0.5 * min(ra[3] - ra[1], rb[3] - rb[1]) and \
            min(ra[2], rb[2]) - max(ra[0], rb[0]) > 0

    keep = []
    for m in sorted(members, key=lambda m: -len(norm(m["text"]))):
        nm = norm(m["text"])
        if any(same_place(m, k) and (nm in norm(k["text"]) or fuzz.partial_ratio(nm, norm(k["text"])) >= 90)
               for k in keep):
            continue
        keep.append(m)
    return keep


def _compose(members, W, H):
    members = _dedupe_pieces(members)
    vertical = members[0]["vertical"]
    if not vertical:
        members.sort(key=lambda m: (m["rect"][1] + m["rect"][3]) / 2)
        lines, cur = [], [members[0]]
        for m in members[1:]:
            prev = cur[-1]["rect"]
            h = prev[3] - prev[1]
            if abs((m["rect"][1] + m["rect"][3]) / 2 - (prev[1] + prev[3]) / 2) < 0.5 * h:
                cur.append(m)
            else:
                lines.append(cur)
                cur = [m]
        lines.append(cur)
        text = "\n".join(_join([m["text"] for m in sorted(line, key=lambda m: m["rect"][0])]) for line in lines)
    else:
        members.sort(key=lambda m: -m["rect"][0])  # 竖排：从右往左
        lines = [[m] for m in members]
        text = "\n".join(m["text"] for m in members)
    x0 = min(m["rect"][0] for m in members) / W
    y0 = min(m["rect"][1] for m in members) / H
    x1 = max(m["rect"][2] for m in members) / W
    y1 = max(m["rect"][3] for m in members) / H
    rect = [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)]
    latest = max(members, key=lambda m: m["start"])
    return {
        "start": round(min(m["start"] for m in members), 3),
        "end": round(max(m["end"] for m in members), 3),
        "text": text,
        "rect": rect,
        "vertical": vertical,
        "lines": len(lines),
        "zone": _zone(rect, vertical),
        "score": round(sum(m["tr"].score() for m in members) / len(members), 3),
        "_snapshot": latest["tr"].snapshot,
    }
