"""字体度量：按字体名找到系统里的字体文件，计算一段文字在 libass 里的宽度。

ASS 注音要逐块定位，宽度算准了，注音才能正好在底字上方。libass（与 VSFilter 兼容）把字号当作
字体的 usWinAscent + usWinDescent 高度，所以 1 个字体单位 = 字号 / (winAscent + winDescent) 像素。
找不到字体时退回估算：全角字 1 个字号，半角 0.55 个字号。
"""
import os
from functools import lru_cache
from pathlib import Path

from .. import env
from ..core.store import digest, load_json, save_json

FONT_EXTS = (".ttf", ".otf", ".ttc", ".otc")


def _font_dirs():
    dirs = []
    if os.name == "nt":
        dirs.append(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts")
        dirs.append(Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts")
    else:
        dirs += [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".fonts",
                 Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path.home() / "Library" / "Fonts"]
    return [d for d in dirs if d.is_dir()]


def _scan_files():
    files = []
    for d in _font_dirs():
        for root, _, names in os.walk(d):
            files += [os.path.join(root, n) for n in names if n.lower().endswith(FONT_EXTS)]
    return sorted(files)


@lru_cache(maxsize=1)
def _index():
    """{字体名（小写，含本地化名）: [路径, 序号, 是否粗体]}；缓存在用户数据目录。"""
    files = _scan_files()
    key = digest([(f, os.path.getsize(f)) for f in files])
    cache_path = env.home() / "cache" / "fonts.json"
    cache = load_json(cache_path, {})
    if cache.get("key") == key:
        return cache["fonts"]
    from fontTools.ttLib import TTCollection, TTFont

    fonts = {}
    for f in files:
        try:
            faces = TTCollection(f, lazy=True).fonts if f.lower().endswith((".ttc", ".otc")) else [TTFont(f, lazy=True)]
        except Exception:
            continue
        for i, face in enumerate(faces):
            try:
                names = face["name"]
                bold = bool(face["OS/2"].fsSelection & 0x20) if "OS/2" in face else False
                for rec in names.names:
                    if rec.nameID in (1, 4, 16):
                        try:
                            n = rec.toUnicode().strip().lower()
                        except Exception:
                            continue
                        # 同名时优先常规体
                        if n and (n not in fonts or (fonts[n][2] and not bold)):
                            fonts[n] = [f, i, bold]
            except Exception:
                continue
    save_json(cache_path, {"key": key, "fonts": fonts})
    return fonts


class Metrics:
    def __init__(self, family):
        self.family = family
        self.cmap = None
        try:
            hit = _index().get(family.strip().lower())
        except Exception:
            hit = None
        if not hit:
            return
        from fontTools.ttLib import TTFont

        try:
            face = TTFont(hit[0], fontNumber=hit[1], lazy=True)
            os2 = face["OS/2"]
            self.units = (os2.usWinAscent + os2.usWinDescent) or face["head"].unitsPerEm
            self.em = face["head"].unitsPerEm
            self.cmap = face.getBestCmap()
            self.hmtx = face["hmtx"].metrics
        except Exception:
            self.cmap = None

    @property
    def found(self):
        return self.cmap is not None

    def width(self, text, size):
        if not self.cmap:
            return sum(size if ord(c) > 0x2E7F else size * 0.55 for c in text)
        total = 0.0
        for c in text:
            g = self.cmap.get(ord(c))
            if g is None:  # 这个字体没有这个字，libass 会换字体，按一个字号估算
                total += (self.em if ord(c) > 0x2E7F else self.em * 0.5) * size / self.units
            else:
                total += self.hmtx[g][0] * size / self.units
        return total


@lru_cache(maxsize=32)
def metrics(family) -> Metrics:
    return Metrics(family)
