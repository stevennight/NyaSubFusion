"""项目目录读写。所有 JSON 原子写入，中断不会留下半个文件。"""
import hashlib
import json
import os
from pathlib import Path


def save_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_text(path, text, encoding="utf-8"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding=encoding, newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def digest(obj) -> str:
    """任意可 JSON 化对象的稳定哈希。"""
    data = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha1(data).hexdigest()


def file_stamp(path) -> dict:
    """文件指纹：路径 + 大小 + 修改时间（不读内容，几 GB 的视频也很快）。"""
    st = os.stat(path)
    return {"path": os.path.abspath(path), "size": st.st_size, "mtime": int(st.st_mtime)}


class ProjectDir:
    """视频旁边的 `<stem>.nyasub/` 目录。"""

    def __init__(self, video, workdir=None):
        self.video = Path(video).resolve()
        self.stem = self.video.stem
        self.root = Path(workdir).resolve() if workdir else self.video.with_name(self.stem + ".nyasub")
        self.out_base = self.video.with_name(self.stem)
        for sub in ("media", "stages", "crops", "llm_cache"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def media(self, name):
        return self.root / "media" / name

    def stage(self, name):
        return self.root / "stages" / name

    def __truediv__(self, name):
        return self.root / name
