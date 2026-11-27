"""作品级歌曲库（<series>/songs.json）与内容驱动歌词对齐。

设计原则：
- 不按集数写死 OP/ED：作品级歌曲库存每首歌的完整歌词（lines: [{orig, tr: {lang: str}}]）；
- 任意一集出现连续的 type == "song" 区间（含 OP、ED、剧中插曲/哼唱）时：
  1. 先按内容模糊子序列匹配作品歌曲库 → 命中则直接替换为标准原文并锁定已有译文（保证十几集一字不差，
     且兼容单集剪辑、换 2 番、副歌切入、提前/延后换 OP/ED）；
  2. 未命中时，切**完整原声（带伴奏，非去伴奏人声轨）** + 作品名交由多模态模型辨认歌名与歌手，
     再优先通过 LRCLIB（免费高可信歌词库）获取官方完整歌词并做子序列模糊对齐；
  3. 匹配度不够高（如即兴哼唱、查不到词）时保留原听写，绝不强行乱替换；
- 翻译完成后将本集已对齐歌曲的译文沉淀回 series/songs.json，供后续集数复用。
"""
import re
import tempfile
from pathlib import Path

from rapidfuzz import fuzz

from ..env import log
from ..textutil import norm
from .store import load_json, save_json

SCHEMA_VERSION = 1
MIN_LINE_SIM = 48.0       # 单句模糊匹配最低相似度（兼容日式英语听错如 Shadow <-> Shall）
MIN_BLOCK_AVG_SIM = 52.0  # 整段平均相似度门槛（防止把即兴哼唱误套到别的歌）
MIN_MATCH_RATIO = 0.5     # 一段里至少这么多比例的句子对上才认定命中


def songs_path(series_dir: Path | None) -> Path | None:
    return (series_dir / "songs.json") if series_dir else None


def load_songs(series_dir: Path | None) -> list[dict]:
    p = songs_path(series_dir)
    if not p or not p.exists():
        return []
    return list(load_json(p, {"songs": []}).get("songs") or [])


def save_songs(series_dir: Path, songs: list[dict]) -> None:
    p = songs_path(series_dir)
    if p:
        save_json(p, {"version": SCHEMA_VERSION, "songs": songs})


def _song_id(title: str, artist: str = "") -> str:
    raw = f"{title}-{artist}".strip().lower()
    slug = re.sub(r"[^a-z0-9\u3040-\u30ff\u4e00-\u9fff]+", "-", raw).strip("-")
    return slug or "song"


def _strip_music(text: str) -> str:
    return re.sub(r"^[♪♬♫♩\s]+|[♪♬♫♩\s]+$", "", (text or "").strip())


MAX_SPAN_LINES = 4        # 单条 ASR 歌词最多合并对应歌词库中连续 4 行（防止截断丢半句）


def _line_sim(a: str, b: str) -> float:
    """整句覆盖相似度（用 fuzz.ratio 为主，不再用高分 partial_ratio 诱导只匹配半句）。"""
    na, nb = norm(_strip_music(a)), norm(_strip_music(b))
    if not na or not nb:
        return 0.0
    return float(fuzz.ratio(na, nb))


def _join_orig(lines: list[dict], j0: int, j1: int) -> str:
    parts = [ln.get("orig", "").strip() for ln in lines[j0:j1] if ln.get("orig", "").strip()]
    return " ".join(parts)


def find_song_blocks(cues: list[dict], max_gap: float = 12.0) -> list[list[int]]:
    """找出 cues 中连续的 type == 'song' 区间（返回索引列表的列表）。"""
    blocks: list[list[int]] = []
    cur: list[int] = []
    for i, c in enumerate(cues):
        if c.get("type") == "song":
            if cur and c["start"] - cues[cur[-1]]["end"] > max_gap:
                blocks.append(cur)
                cur = []
            cur.append(i)
        else:
            if cur and (i + 1 >= len(cues) or cues[i + 1].get("type") != "song"
                        or cues[i + 1]["start"] - cues[cur[-1]]["end"] > max_gap):
                blocks.append(cur)
                cur = []
    if cur:
        blocks.append(cur)
    return blocks


def align_block_to_lines(block_cues: list[dict], song_lines: list[dict]) -> tuple[dict[int, tuple[int, int]], float]:
    """将本集一段 song cues 按顺序做 **1 对多（1~MAX_SPAN_LINES 行）** 模糊子序列对齐到完整歌词 song_lines。
    返回 ({block内索引: (j0, j1) 半开区间}, 平均匹配相似度)。
    彻底避免 1 对 1 匹配把同一条语音里唱的两三行截断丢掉；同时支持 1 番、2 番、副歌切入与剧中哼唱。"""
    if not block_cues or not song_lines:
        return {}, 0.0
    n, m = len(block_cues), len(song_lines)

    # dp[i][j] = 前 i 条 cue 对齐到 song_lines[:j] 的最大总分
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    bt: list[list[tuple[int, int, int]]] = [[(0, 0, 0)] * (m + 1) for _ in range(n + 1)]  # (pi, pj, matched_k)
    for i in range(1, n + 1):
        cue_txt = block_cues[i - 1]["orig"]["text"]
        for j in range(1, m + 1):
            best, prev = dp[i - 1][j], (i - 1, j, 0)
            if dp[i][j - 1] > best:
                best, prev = dp[i][j - 1], (i, j - 1, 0)
            # 尝试将 cue[i-1] 匹配到 song_lines[j-k : j]（k = 1..MAX_SPAN_LINES）
            for k in range(1, min(MAX_SPAN_LINES, j) + 1):
                joined = _join_orig(song_lines, j - k, j)
                sc = _line_sim(cue_txt, joined)
                if sc >= MIN_LINE_SIM and dp[i - 1][j - k] + sc > best:
                    best = dp[i - 1][j - k] + sc
                    prev = (i - 1, j - k, k)
            dp[i][j], bt[i][j] = best, prev

    mapping: dict[int, tuple[int, int]] = {}
    i, j = n, m
    scores = []
    while i > 0 and j > 0:
        pi, pj, k = bt[i][j]
        if k > 0:
            mapping[i - 1] = (pj, j)
            scores.append(_line_sim(block_cues[i - 1]["orig"]["text"], _join_orig(song_lines, pj, j)))
        i, j = pi, pj
    if not mapping:
        return {}, 0.0
    avg = sum(scores) / len(scores)
    min_need = 1 if n == 1 else max(2, int(n * MIN_MATCH_RATIO + 0.5))
    if len(mapping) < min_need or avg < MIN_BLOCK_AVG_SIM or (n == 1 and avg < 70.0):
        return {}, 0.0
    return mapping, avg


def _calibrate_block_timing_with_lrc(block: list[dict], mapping: dict[int, tuple[int, int]], song_lines: list[dict]) -> None:
    """当歌曲带有精确 LRC 时间戳 `t` 时，用高置信锚点计算整首歌的全局时间偏移 offset，
    校准本段每条已对齐 cue 的起止时间（修正前奏后漂移 5~6 秒等大模型估时偏差）。"""
    anchors = []
    for bi, (j0, j1) in sorted(mapping.items()):
        t_lrc = song_lines[j0].get("t")
        if t_lrc is None:
            continue
        c = block[bi]
        sc = _line_sim(c["orig"]["text"], _join_orig(song_lines, j0, j1))
        if sc >= 65.0:
            anchors.append((bi, c["start"] - float(t_lrc)))
    if len(anchors) < 2:
        return
    offs = sorted(d for _, d in anchors)
    med = offs[len(offs) // 2]
    # 剔除偏离中位数 > 2.5s 的离群锚点再取中位数
    inliers = [d for d in offs if abs(d - med) <= 2.5]
    if len(inliers) < 2:
        return
    offset = inliers[len(inliers) // 2]

    ordered = sorted(mapping.items())
    for idx, (bi, (j0, j1)) in enumerate(ordered):
        t0 = song_lines[j0].get("t")
        if t0 is None:
            continue
        new_start = round(max(0.0, float(t0) + offset), 3)
        # 若与本条已对准的时间相差过大（>4.5s）且本条本身已是 aligned（如 TV Size 剪短了 Full Ver. 的前奏/间奏），不强行拉偏
        if abs(new_start - block[bi]["start"]) > 4.5 and block[bi].get("timing") == "aligned":
            continue
        next_t = song_lines[j1].get("t") if j1 < len(song_lines) else None
        if next_t is not None and float(next_t) > float(t0):
            new_end = round(float(next_t) + offset - 0.08, 3)
        else:
            dur = max(1.5, block[bi]["end"] - block[bi]["start"])
            new_end = round(new_start + dur, 3)
        if new_end <= new_start + 0.3:
            new_end = round(new_start + 1.5, 3)
        # 单句时长异常拉长（>10s，通常是 Full Ver. 间奏）时收紧为原时长
        if new_end - new_start > 10.0:
            new_end = round(new_start + max(2.0, min(8.0, block[bi]["end"] - block[bi]["start"])), 3)
        block[bi]["start"] = new_start
        block[bi]["end"] = new_end
        if idx > 0:
            prev_c = block[ordered[idx - 1][0]]
            if prev_c["end"] > new_start:
                prev_c["end"] = round(max(prev_c["start"] + 0.3, new_start - 0.05), 3)


def match_song_library(block_cues: list[dict], songs: list[dict]) -> tuple[dict | None, dict[int, int], float]:
    """在作品歌曲库中找最匹配的歌及其行映射。"""
    best_song, best_map, best_score = None, {}, 0.0
    for song in songs:
        lines = song.get("lines") or []
        mapping, avg = align_block_to_lines(block_cues, lines)
        if not mapping:
            continue
        score = avg * (len(mapping) / max(1, len(block_cues)))
        if score > best_score:
            best_song, best_map, best_score = song, mapping, score
    return best_song, best_map, best_score


IDENTIFY_SYSTEM = """你是影视音乐与动漫歌曲识别助手。
给你一段影视作品里的歌曲完整原声（含伴奏或角色哼唱），以及听写出来的几句参考歌词和作品信息。
请辨认这首歌的准确歌名、歌手和类型（OP / ED / insert 剧中插曲或童谣）。
拿不准或只是无名即兴哼唱时，title 留空字符串，绝不要瞎猜。
只输出 JSON：{"title": "歌名", "artist": "歌手（知道才填）", "role": "OP|ED|insert"}"""


def identify_song_from_audio(orig_wav, a: float, b: float, block_cues: list[dict], cfg, cache_dir=None) -> dict | None:
    """切完整原声（带伴奏）交由支持音频的多模态模型辨认歌名与歌手。"""
    if not orig_wav or not Path(orig_wav).exists():
        return None
    ep = cfg.endpoint("listen")
    if ep.get("kind", "openai") not in ("openai", "agy") or not ep.get("model"):
        ep = cfg.endpoint("asr")
        if ep.get("kind", "openai") not in ("openai", "agy") or not ep.get("model"):
            return None
        purpose = "asr"
    else:
        purpose = "listen"

    from ..sources.asr_api import _encode
    from ..llm.client import LLM

    # 取前 45 秒完整原声（含前奏与主歌/副歌，足够辨认歌名）
    clip_end = min(b, a + 45.0)
    tmp = tempfile.TemporaryDirectory(prefix="nyasub-song-", ignore_cleanup_errors=True)
    try:
        path, fmt = _encode(orig_wav, max(0.0, a - 1.0), clip_end, Path(tmp.name) / "song", fallback="wav")
        sample_lines = "\n".join(_strip_music(c["orig"]["text"]) for c in block_cues[:8])
        user = (f"所属作品：{cfg.project.series or '（未知）'}\n"
                f"节目说明：{(cfg.project.context or '')[:300]}\n"
                f"这段音频长 {clip_end - a:.1f} 秒，参考听写歌词：\n{sample_lines}\n\n请辨认歌名和歌手。")
        llm = LLM(cfg, cache_dir=cache_dir, purpose=purpose)
        res = llm.chat_json(IDENTIFY_SYSTEM, user, audio=(path, fmt))
        if isinstance(res, dict) and (res.get("title") or "").strip():
            return {
                "title": res["title"].strip(),
                "artist": (res.get("artist") or "").strip(),
                "role": (res.get("role") or "insert").strip(),
            }
    except Exception as e:
        log(f"  歌曲辨认跳过：{e}")
    finally:
        tmp.cleanup()
    return None


def align_and_fill_songs(cues: list[dict], series_dir: Path | None, cfg, cache_dir=None,
                         orig_wav=None, tgt_code: str = "zh-Hans") -> int:
    """在翻译前对 cues 中连续的 song 区间进行歌曲库匹配与官方歌词对齐。
    - 命中作品歌曲库且已有译文 → 替换 orig 并锁定 tr（cue["song_locked"] = True，翻译阶段跳过重翻）；
    - 未命中 → 用完整原声辨认歌名，查 LRCLIB 拿官方歌词对齐替换 orig，待翻译后入库。
    返回成功对齐的句子数。"""
    blocks = find_song_blocks(cues)
    if not blocks:
        return 0
    songs = load_songs(series_dir)
    aligned_total = 0

    for idxs in blocks:
        block = [cues[i] for i in idxs]
        song, mapping, _ = match_song_library(block, songs)
        if song:
            log(f"歌曲对齐：命中作品歌曲库《{song['title']}》（对齐 {len(mapping)}/{len(block)} 句）")
        else:
            # 未命中库：用完整原声辨认歌名 + 查 LRCLIB
            ident = identify_song_from_audio(orig_wav, block[0]["start"], block[-1]["end"], block, cfg, cache_dir)
            if not ident:
                continue
            from ..llm import research

            found = research.fetch_lrclib(ident["title"], ident.get("artist", ""))
            if not found and ident.get("artist"):
                found = research.fetch_lrclib(ident["title"])
            if not found:
                # 第二层兜底：LRCLIB 未收录时尝试用带联网能力的接口查官方歌词原文
                online_lines = research.fetch_lyrics_online(ident["title"], ident.get("artist", ""), cfg=cfg, cache_dir=cache_dir)
                if online_lines:
                    found = [{"title": ident["title"], "artist": ident.get("artist", ""), "lines": online_lines, "source": "online"}]
            for cand in found:
                timed = cand.get("timed_lines") or []
                if timed:
                    cand_lines = [{"orig": r["orig"], "t": r["t"], "tr": {}} for r in timed]
                else:
                    cand_lines = [{"orig": ln, "tr": {}} for ln in cand["lines"]]
                m2, _ = align_block_to_lines(block, cand_lines)
                if m2:
                    song = {
                        "id": _song_id(cand["title"], cand.get("artist", "")),
                        "title": cand["title"],
                        "artist": cand.get("artist", ""),
                        "role": ident.get("role") or "insert",
                        "lines": cand_lines,
                        "source": cand.get("source", "lrclib"),
                    }
                    mapping = m2
                    songs.append(song)
                    if series_dir:
                        save_songs(series_dir, songs)
                    log(f"歌曲对齐：识别为《{song['title']}》（{song.get('artist') or '未知歌手'}），已从 LRCLIB 对齐 {len(mapping)}/{len(block)} 句")
                    break

        if not song or not mapping:
            continue

        # 若歌曲带精确 LRC 时间戳，先用锚点全局偏移校准整首歌的时间轴
        _calibrate_block_timing_with_lrc(block, mapping, song["lines"])

        for bi, (j0, j1) in mapping.items():
            c = block[bi]
            std_orig = _join_orig(song["lines"], j0, j1)
            if not std_orig.startswith(("♪", "♬")):
                std_orig = "♪ " + std_orig
            c["orig"]["text"] = std_orig
            c["song_ref"] = {"id": song["id"], "title": song["title"], "lines": [j0, j1]}
            # 若区间内每一行都已有锁定译文，拼接后直接锁定
            tr_parts = [(song["lines"][k].get("tr") or {}).get(tgt_code, "").strip() for k in range(j0, j1)]
            if tr_parts and all(tr_parts):
                locked_tr = " ".join(tr_parts)
                if not locked_tr.startswith(("♪", "♬")):
                    locked_tr = "♪ " + locked_tr
                c.setdefault("tr", {})[tgt_code] = {"text": locked_tr}
                c["song_locked"] = True
            aligned_total += 1

    return aligned_total


def save_translated_songs(cues: list[dict], series_dir: Path | None, tgt_code: str) -> int:
    """翻译完成后，把本集已对齐歌曲的新译文沉淀回作品级 songs.json，供后续集数复用。"""
    if not series_dir:
        return 0
    songs = load_songs(series_dir)
    if not songs:
        return 0
    by_id = {s["id"]: s for s in songs}
    updated = 0
    for c in cues:
        ref = c.get("song_ref")
        if not ref or ref.get("id") not in by_id:
            continue
        tr_text = ((c.get("tr") or {}).get(tgt_code) or {}).get("text", "").strip()
        if not tr_text:
            continue
        song = by_id[ref["id"]]
        song_lines = song.get("lines") or []
        span = ref.get("lines")
        if isinstance(span, list) and len(span) == 2:
            j0, j1 = span
            # 1 对 1 精确沉淀到单行；1 对多时仅当单行无译文且恰好对应 1 行才逐行写，避免跨行错配
            if j1 - j0 == 1 and 0 <= j0 < len(song_lines):
                tr_map = song_lines[j0].setdefault("tr", {})
                if not tr_map.get(tgt_code):
                    tr_map[tgt_code] = _strip_music(tr_text)
                    updated += 1
        elif isinstance(ref.get("line"), int) and 0 <= ref["line"] < len(song_lines):
            tr_map = song_lines[ref["line"]].setdefault("tr", {})
            if not tr_map.get(tgt_code):
                tr_map[tgt_code] = _strip_music(tr_text)
                updated += 1
    if updated:
        save_songs(series_dir, songs)
        log(f"作品歌曲库已更新：沉淀 {updated} 句标准歌词译文")
    return updated
