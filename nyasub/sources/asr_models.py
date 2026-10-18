"""语音识别模型：列表、下载（Hugging Face 不通时用 ModelScope）、加载前的修正。

实测（《東方八犬異聞》第 1 话，24 分钟，人声分离 + 补识别，以字幕组中文字幕的时间为参照）：

| 模型 | 完全漏掉 | 起点 1.5 秒内对上 | 用时（显卡） |
|---|---|---|---|
| large-v3 | 9 | 73% | 181 s |
| large-v3-turbo | 6 | 82% | 51 s |
| kotoba-whisper v2.0 | 8 | 70% | 28 s（不少句子听错、时间对错） |

anime-whisper（动画配音微调）不进列表：只有 Transformers 格式，要装 PyTorch 才能转换；
哭喊漏得最少，但普通台词错得多，整体不如 large-v3。
"""
import json
import os
import urllib.request
from pathlib import Path

from .. import env
from ..env import check_cancel, log

MODELS = {
    "large-v3": {
        "label": "large-v3", "desc": "最准的通用模型，速度一般（约 3 GB）",
        "hf": "Systran/faster-whisper-large-v3", "ms": "pengzhendong/faster-whisper-large-v3", "mb": 2944,
    },
    "large-v3-turbo": {
        "label": "large-v3-turbo", "desc": "快 3 倍；动画实测漏句更少、时间轴更准（约 1.6 GB）",
        "hf": "mobiuslabsgmbh/faster-whisper-large-v3-turbo", "ms": "pengzhendong/faster-whisper-large-v3-turbo", "mb": 1543,
    },
    "kotoba-tech/kotoba-whisper-v2.0-faster": {
        "label": "kotoba-whisper v2.0", "desc": "日语专用、很快；在动画上听错得多（约 1.4 GB）",
        "hf": "kotoba-tech/kotoba-whisper-v2.0-faster", "ms": "philpw99/kotoba-whisper-v2.0-faster", "mb": 1443,
        "decoder_layers": 2,  # 蒸馏模型：配置里的对齐头指向不存在的层，不修会在算逐字时间时崩溃
    },
    "medium": {
        "label": "medium", "desc": "小一些，准确度明显下降（约 1.5 GB）",
        "hf": "Systran/faster-whisper-medium", "ms": "pengzhendong/faster-whisper-medium", "mb": 1457,
    },
    "small": {
        "label": "small", "desc": "很小很快，只适合试跑（约 0.5 GB）",
        "hf": "Systran/faster-whisper-small", "ms": "Systran/faster-whisper-small", "mb": 464,
    },
}
FILES = ["config.json", "preprocessor_config.json", "tokenizer.json", "model.bin"]  # 另加 vocabulary.*


def catalog():
    return [{"id": k, "label": v["label"], "desc": v["desc"]} for k, v in MODELS.items()]


def _ms_dir(name):
    return env.home() / "models" / "asr" / name.replace("/", "__")


def _fix_alignment(path, layers):
    """对齐头指向超出解码器层数的层时，改成用最后一层的全部注意力头（CTranslate2 对未知模型的默认做法）。"""
    cfg_path = Path(path) / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return
    heads = cfg.get("alignment_heads") or []
    if heads and max(a for a, _ in heads) < layers:
        return
    n = max((b for _, b in heads), default=19) + 1
    cfg["alignment_heads"] = [[layers - 1, h] for h in range(max(n, 20))]
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    log("已修正模型的对齐头设置（否则算逐字时间时会崩溃）")


def _ms_files(repo):
    """ModelScope 上这个模型的文件（不同模型的词表文件名不一样：vocabulary.json / vocabulary.txt）。"""
    url = f"https://www.modelscope.cn/api/v1/models/{repo}/repo/files?Revision=master&Recursive=true"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "nyasub"}), timeout=30) as r:
        files = json.loads(r.read().decode("utf-8"))["Data"]["Files"]
    names = [f["Path"] for f in files if f.get("Type") != "tree"]
    return [n for n in names if n in FILES or n.startswith("vocabulary.")]


def _download_ms(repo, dest, mb):
    dest.mkdir(parents=True, exist_ok=True)
    files = _ms_files(repo)
    if "model.bin" not in files:
        raise RuntimeError(f"ModelScope 上的 {repo} 没有 model.bin")
    for f in files:
        target = dest / f
        if target.exists() and target.stat().st_size > 0:
            continue
        url = f"https://www.modelscope.cn/models/{repo}/resolve/master/{f}"
        tmp = target.with_suffix(target.suffix + ".part")
        req = urllib.request.Request(url, headers={"User-Agent": "nyasub"})
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as out:
            done, step = 0, 0
            while True:
                check_cancel()
                buf = r.read(1 << 20)
                if not buf:
                    break
                out.write(buf)
                done += len(buf)
                if f == "model.bin" and done // (200 << 20) > step:
                    step = done // (200 << 20)
                    log(f"  下载 {done >> 20} / {mb} MB")
        tmp.replace(target)


def resolve(name, download_root=None):
    """返回可以交给 WhisperModel 的本地目录（或原名，交给 faster-whisper 自己下载）。
    顺序：本地目录 → ModelScope 下载过的 → Hugging Face 缓存 → 从 Hugging Face 下载 → 从 ModelScope 下载。"""
    from faster_whisper.utils import download_model

    meta = MODELS.get(name, {})
    if os.path.isdir(name):
        path = name
    elif (_ms_dir(name) / "model.bin").exists():
        path = str(_ms_dir(name))
        log(f"加载语音模型 {meta.get('label', name)}（已在本地）")
    else:
        try:
            path = download_model(name, local_files_only=True, cache_dir=download_root)
            log(f"加载语音模型 {meta.get('label', name)}（已在本地缓存）")
        except Exception:
            log(f"首次使用，正在下载语音模型 {meta.get('label', name)}"
                + (f"（约 {meta['mb'] / 1024:.1f} GB，只需下载一次）" if meta.get("mb") else ""))
            try:
                path = download_model(name, cache_dir=download_root)
            except Exception as e:
                if not meta.get("ms"):
                    raise
                log(f"  Hugging Face 下载失败（{(str(e).splitlines() or [''])[0][:60]}），改从 ModelScope 下载")
                _download_ms(meta["ms"], _ms_dir(name), meta.get("mb", 0))
                path = str(_ms_dir(name))
    if meta.get("decoder_layers"):
        _fix_alignment(path, meta["decoder_layers"])
    return path
