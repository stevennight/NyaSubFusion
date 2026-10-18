"""人声分离（MDX-Net，UVR 的 Kim_Vocal_2 模型）：把背景音乐、音效去掉，只留人声，再交给语音识别。

动画、综艺这类背景音乐很响的节目，直接识别时人声检测会把音乐当成说话、或者漏掉压在音乐下的台词，
时间也会被拉到音乐开头；片头片尾曲基本识别不出来。分离后这些都会好很多。

- 模型约 64 MB，第一次用时下载到 <用户数据目录>/models/；有 CUDA（画面字识别 GPU 组件）就用显卡，否则用 CPU。
- 边解码边分离边写出（ffmpeg 管道进、管道出），一小时的节目内存也不会涨。
- 每块约 5.9 秒，两边各多算 n_fft/2 再裁掉，块与块之间没有接缝。
"""
import subprocess
import time
import urllib.request
from pathlib import Path

import numpy as np

from .. import env
from ..env import check_cancel, log

MODEL = {
    "name": "Kim_Vocal_2", "size": 66759214,
    "urls": ["https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/Kim_Vocal_2.onnx",
             "https://hf-mirror.com/seanghay/uvr_models/resolve/main/Kim_Vocal_2.onnx",
             "https://huggingface.co/seanghay/uvr_models/resolve/main/Kim_Vocal_2.onnx"],
    "n_fft": 7680, "hop": 1024, "dim_f": 3072, "dim_t": 256, "compensate": 1.009,
}
RATE = 44100


def model_path():
    return env.home() / "models" / f"{MODEL['name']}.onnx"


def ensure_model():
    """模型不在就下载（依次试 GitHub 和 Hugging Face 镜像）。"""
    path = model_path()
    if path.exists() and path.stat().st_size == MODEL["size"]:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    err = None
    for url in MODEL["urls"]:
        try:
            log(f"首次使用人声分离，正在下载模型（约 64 MB）：{url.split('/')[2]}")
            req = urllib.request.Request(url, headers={"User-Agent": "nyasub"})
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
                while True:
                    check_cancel()
                    buf = r.read(1 << 20)
                    if not buf:
                        break
                    f.write(buf)
            if tmp.stat().st_size != MODEL["size"]:
                raise RuntimeError(f"文件大小不对（{tmp.stat().st_size}）")
            tmp.replace(path)
            return path
        except env.Cancelled:
            tmp.unlink(missing_ok=True)
            raise
        except Exception as e:
            err = e
            log(f"  下载失败（{str(e)[:80]}），换下一个地址")
    tmp.unlink(missing_ok=True)
    raise RuntimeError(f"下载人声分离模型失败：{err}")


class _Mdx:
    def __init__(self, path, gpu):
        import onnxruntime as ort

        providers = (["CUDAExecutionProvider"] if gpu else []) + ["CPUExecutionProvider"]
        self.sess = ort.InferenceSession(str(path), providers=providers)
        self.on_gpu = "CUDAExecutionProvider" in self.sess.get_providers()
        m = MODEL
        self.n_fft, self.hop, self.dim_f, self.dim_t = m["n_fft"], m["hop"], m["dim_f"], m["dim_t"]
        self.chunk = self.hop * (self.dim_t - 1)          # 每块输入的采样数
        self.trim = self.n_fft // 2                        # 两边各裁掉这么多
        self.gen = self.chunk - 2 * self.trim              # 每块实际产出的采样数
        n = np.arange(self.n_fft)
        self.window = (0.5 - 0.5 * np.cos(2 * np.pi * n / self.n_fft)).astype(np.float32)  # periodic hann
        self.idx = np.arange(self.n_fft)[None, :] + self.hop * np.arange(self.dim_t)[:, None]
        wsq = np.zeros(self.n_fft + self.hop * (self.dim_t - 1), np.float32)
        for t in range(self.dim_t):
            wsq[t * self.hop: t * self.hop + self.n_fft] += self.window ** 2
        self.wsq = np.maximum(wsq, 1e-8)

    def _stft(self, x):  # [2, chunk] → [2, F, T] complex（与 torch.stft center=True 一致）
        p = self.n_fft // 2
        xp = np.pad(x, ((0, 0), (p, p)), mode="reflect")
        frames = xp[:, self.idx] * self.window              # [2, T, n_fft]
        return np.fft.rfft(frames, axis=-1).transpose(0, 2, 1)

    def _istft(self, spec):  # [2, F, T] → [2, chunk]
        frames = np.fft.irfft(spec.transpose(0, 2, 1), n=self.n_fft, axis=-1) * self.window  # [2, T, n_fft]
        out = np.zeros((2, self.n_fft + self.hop * (self.dim_t - 1)), np.float32)
        for t in range(self.dim_t):
            out[:, t * self.hop: t * self.hop + self.n_fft] += frames[:, t]
        out /= self.wsq
        p = self.n_fft // 2
        return out[:, p: p + self.chunk]

    def run(self, x):
        """x：[2, chunk] 混音 → [2, gen] 人声（已裁掉两边）。"""
        spec = self._stft(x)[:, :self.dim_f]                                     # [2, dim_f, T]
        inp = np.stack([spec.real, spec.imag], axis=1).reshape(1, 4, self.dim_f, self.dim_t).astype(np.float32)
        out = self.sess.run(None, {"input": inp})[0].reshape(2, 2, self.dim_f, self.dim_t)
        full = np.zeros((2, self.n_fft // 2 + 1, self.dim_t), np.complex64)
        full[:, :self.dim_f] = out[:, 0] + 1j * out[:, 1]
        return self._istft(full)[:, self.trim: self.trim + self.gen] * MODEL["compensate"]


def separate(video, out_wav, duration=0.0, gpu=False):
    """从视频里分离出人声，写成 16 kHz 单声道 wav（语音识别用）。返回 (是否用了显卡, 用时秒)。"""
    path = ensure_model()
    mdx = _Mdx(path, gpu)
    log(f"人声分离（{MODEL['name']}，{'显卡' if mdx.on_gpu else 'CPU'}）"
        + ("" if mdx.on_gpu else "：CPU 上大约要节目时长的 1/3，装了画面字识别 GPU 组件会快很多"))
    t0 = time.time()
    src = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(video), "-map", "0:a:0", "-vn", "-ac", "2", "-ar", str(RATE),
                            "-f", "f32le", "-"], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
    tmp = Path(out_wav).with_suffix(".part.wav")
    dst = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(RATE), "-ac", "2", "-i", "-",
                            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(tmp)], stdin=subprocess.PIPE)
    try:
        buf = np.zeros((2, mdx.trim), np.float32)  # 开头补 trim 个 0
        done, total, last, eof = 0, int(duration * RATE), 0.0, False
        while True:
            check_cancel()
            while buf.shape[1] < mdx.chunk and not eof:
                raw = src.stdout.read(mdx.gen * 8)
                if not raw:
                    eof = True
                    break
                a = np.frombuffer(raw[: len(raw) // 8 * 8], np.float32).reshape(-1, 2).T
                buf = np.concatenate([buf, a], axis=1)
            if eof and buf.shape[1] <= mdx.trim:
                break
            real = buf.shape[1] - mdx.trim                 # 这块里真实的采样数（不含开头补的 / 上一块留下的 trim）
            block = buf[:, :mdx.chunk]
            if block.shape[1] < mdx.chunk:                 # 结尾不够一块：补 0
                block = np.pad(block, ((0, 0), (0, mdx.chunk - block.shape[1])))
            out = mdx.run(block)
            n = min(mdx.gen, real if eof else mdx.gen)
            dst.stdin.write(np.ascontiguousarray(out[:, :n].T).astype(np.float32).tobytes())
            done += n
            buf = buf[:, mdx.gen:]
            if total and time.time() - last > 10:
                last = time.time()
                log(f"  人声分离 {min(100, done / total * 100):5.1f}%")
            if eof and buf.shape[1] <= mdx.trim:
                break
        dst.stdin.close()
        if dst.wait() != 0:
            raise RuntimeError("写出人声音频失败")
        tmp.replace(out_wav)
    finally:
        for p in (src, dst):
            if p.poll() is None:
                p.kill()
                p.wait()
        tmp.unlink(missing_ok=True)
    return mdx.on_gpu, time.time() - t0
