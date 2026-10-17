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


def _uni(rec):
    try:
        return rec.toUnicode().strip().lower()
    except Exception:
        return ""


REGULAR = {"regular", "normal", "standard", "roman", "book", "標準", "常规", "常規", "보통"}


@lru_cache(maxsize=1)
def _index():
    """{字体名（小写，含本地化名）: [路径, 序号, 是否粗体, 优先级]}；缓存在用户数据目录。

    同一个名字对应多个字形时（"Yu Gothic" 既是常规体的家族名，也是细体 / 粗体的“排版家族名”），
    按 libass 的选法优先：家族名相同的常规体 > 家族名 > 全名 > 排版家族名；同级里非粗体优先。
    """
    files = _scan_files()
    key = digest(["v2", [(f, os.path.getsize(f)) for f in files]])
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
                regular = any(_uni(r) in REGULAR for r in names.names if r.nameID == 2)
                for rec in names.names:
                    if rec.nameID not in (1, 4, 16):
                        continue
                    n = _uni(rec)
                    prio = {1: 0 if regular else 1, 4: 2, 16: 3}[rec.nameID] * 2 + bold
                    if n and (n not in fonts or prio < fonts[n][3]):
                        fonts[n] = [f, i, bold, prio]
            except Exception:
                continue
    save_json(cache_path, {"key": key, "fonts": fonts})
    return fonts


def find(family, bold=False):
    """按名字找字体文件：[路径, 序号, 是否粗体, 优先级] 或 None。bold=True 时先找同家族的粗体。"""
    try:
        idx = _index()
    except Exception:
        return None
    name = family.strip().lower()
    if bold:
        for k in (name + " bold", name + " bd", name + "-bold"):
            if k in idx and idx[k][2]:
                return idx[k]
    return idx.get(name)


class Metrics:
    def __init__(self, family):
        self.family = family
        self.cmap = None
        hit = find(family)
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
