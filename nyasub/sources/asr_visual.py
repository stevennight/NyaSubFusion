"""多模态云端 ASR 画面参考：把每个音频块对应的视频画面按低帧率抽成 3×3 缩略网格拼图。

设计要点：
- 纯用 ffmpeg 的 fps + scale + tile 滤镜拼图，零额外 Python 图像库依赖；
- 3 分钟（180 秒）按 5 秒一帧 = 36 帧 → 仅 4 张 3×3 拼图，token 成本低且能看清人物形象、场景切换、人名条与 OP/ED；
- 每张拼图附带清晰的起止时间说明（相对本音频块开头），让模型准确对应音频与画面。
"""
import math
import subprocess
from pathlib import Path

from ..textutil import fmt_time

TILE_COLS = 3
TILE_ROWS = 3
CELL_WIDTH = 384
MAX_GRIDS_PER_CHUNK = 6


def extract_chunk_grids(video_path, start: float, end: float, out_dir, interval: float = 5.0) -> list[dict]:
    """为视频的 [start, end] 区间生成 3×3 缩略网格图列表：[{"path": str, "label": str}]。
    失败或非视频文件时静默返回空列表，不影响主语音识别流程。"""
    if not video_path or not Path(video_path).exists():
        return []
    duration = max(0.0, float(end) - float(start))
    if duration < 1.0:
        return []
    interval = max(2.0, float(interval or 5.0))
    per_grid = TILE_COLS * TILE_ROWS
    # 防止超长块生成太多拼图：自动放宽间隔，控制在 MAX_GRIDS_PER_CHUNK 张以内
    est_frames = math.ceil(duration / interval)
    if est_frames > per_grid * MAX_GRIDS_PER_CHUNK:
        interval = duration / (per_grid * MAX_GRIDS_PER_CHUNK)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = out_dir / "grid_%02d.jpg"
    vf = f"fps=1/{interval:.3f},scale={CELL_WIDTH}:-2:flags=lanczos,tile={TILE_COLS}x{TILE_ROWS}:padding=2:margin=2:color=black"
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-ss", f"{max(0.0, start):.3f}",
        "-t", f"{duration:.3f}",
        "-i", str(video_path),
        "-an", "-sn",
        "-vf", vf,
        "-q:v", "6",
        str(pattern),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60)
    except Exception:
        return []
    if r.returncode != 0:
        return []

    files = sorted(out_dir.glob("grid_*.jpg"))
    out = []
    grid_span = per_grid * interval
    for idx, f in enumerate(files):
        t0 = idx * grid_span
        t1 = min(duration, (idx + 1) * grid_span)
        label = (
            f"参考画面拼图 {idx + 1}/{len(files)}（本音频块 {fmt_time(t0)}–{fmt_time(t1)}，"
            f"{TILE_COLS}×{TILE_ROWS} 按从左到右、从上到下排列，每格约 {interval:.1f} 秒）"
        )
        out.append({"path": str(f), "label": label})
    return out
