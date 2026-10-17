"""命令行：nyasub run / serve / doctor / langs / components。"""
import argparse
import os
import shutil
import sys
import traceback

from . import __version__, env


def _split(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _overrides(args) -> dict:
    """命令行参数 → 配置覆盖（最高优先级）。"""
    o: dict = {}

    def put(path, value):
        cur = o
        *head, last = path.split(".")
        for k in head:
            cur = cur.setdefault(k, {})
        cur[last] = value

    if args.source:
        put("project.source", args.source)
    if args.target:
        put("project.targets", _split(args.target))
    if args.series:
        put("project.series", args.series)
    if args.sub_track is not None:
        put("subs.track", args.sub_track)
    if args.no_asr:
        put("asr.enabled", False)
    if args.no_ocr:
        put("ocr.enabled", False)
    if args.ocr_refine:
        put("ocr.refine", args.ocr_refine)
    if args.video:
        put("render.video.mode", args.video)
    return o


def cmd_run(args):
    from .core import config, pipeline

    until = pipeline.ALIASES.get(args.until, args.until) if args.until else "render"
    if until not in pipeline.STAGES:
        env.log(f"--until 只能是：{', '.join(pipeline.STAGES)}")
        return 2
    redo = {pipeline.ALIASES.get(x, x) for x in _split(args.redo)}
    videos = pipeline.collect_inputs(args.inputs)
    if not videos:
        env.log("没有找到要处理的文件")
        return 1
    if args.workdir and len(videos) > 1:
        env.log("--workdir 只能用于单个文件")
        return 1
    failed = 0
    for v in videos:
        opts = pipeline.Options(until=until, redo=set(redo), subs_path=args.subs, translate=args.translate,
                                config_path=args.config, workdir=args.workdir)
        try:
            from .core.store import ProjectDir

            project_file = ProjectDir(v, args.workdir) / "project.toml"
            cfg, files = config.load(args.config, project_file, _overrides(args))
            if files:
                env.log("配置：" + " < ".join(files))
            pipeline.process(v, cfg, opts, files)
        except KeyboardInterrupt:
            env.log("已中断。下次运行会从断点继续。")
            return 130
        except Exception as e:
            failed += 1
            env.log(f"处理失败：{v}\n{e}")
            traceback.print_exc()
    return 1 if failed else 0


def cmd_doctor(args):
    ok = True

    def line(name, good, detail):
        nonlocal ok
        ok &= bool(good) or name.startswith("（可选）")
        print(f"  {'✓' if good else '✗'} {name}：{detail}")

    print(f"NyaSubFusion {__version__} · Python {sys.version.split()[0]}")
    print(f"  用户数据目录：{env.home()}")
    for tool in ("ffmpeg", "ffprobe"):
        line(tool, shutil.which(tool), shutil.which(tool) or "未找到（winget install Gyan.FFmpeg）")
    try:
        import onnxruntime as ort

        from . import gpu

        line("画面字识别 GPU", True, f"onnxruntime {ort.__version__}，{'CUDA 可用' if gpu.ocr_ready() else '只用 CPU（可安装 cuda-ocr 组件）'}")
    except Exception as e:
        line("onnxruntime", False, f"未安装（{e}）")
    try:
        import ctranslate2

        from . import gpu

        n = ctranslate2.get_cuda_device_count()
        line("语音识别（faster-whisper）", True, f"CTranslate2 {ctranslate2.__version__}，CUDA 设备 {n} 个，"
             f"{'GPU 可用' if gpu.asr_ready() else '只用 CPU（可安装 cuda-asr 组件）'}")
    except Exception as e:
        line("（可选）语音识别", False, f"未安装（{e}）")
    try:
        import rapidocr  # noqa: F401

        line("画面字识别（RapidOCR）", True, "已安装")
    except Exception as e:
        line("（可选）画面字识别", False, f"未安装（{e}）")
    from .core import config

    cfg, files = config.load(args.config)
    mode = cfg.llm_mode()
    line("大模型接口", mode == "api", f"{cfg.llm.model or '未配置模型'} @ {cfg.llm.base_url}"
         if mode == "api" else "未配置 → 翻译会导出任务文件（export 模式）")
    print("  配置文件：" + (" < ".join(files) if files else "（只用内置默认值）"))
    return 0 if ok else 1


def cmd_serve(args):
    try:
        import uvicorn

        from .server.app import create_app
    except ImportError as e:
        env.log(f"本地服务需要额外依赖（{e.name}），请安装 nyasub[server]")
        return 1
    import json
    import socket

    port = args.port
    if port == 0:  # 桌面壳用：让系统分配空闲端口
        with socket.socket() as sk:
            sk.bind((args.host, 0))
            port = sk.getsockname()[1]
    app = create_app(args.token)
    # 关闭时最多等 1 秒：界面的 WebSocket 不会自己断开，不设超时会一直等
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=port, log_level="warning", timeout_graceful_shutdown=1))
    app.state.server = server
    if args.parent_pid:
        _watch_parent(args.parent_pid, server)

    async def ready():
        # 等端口真的开始监听再报告，桌面壳读到这一行就去连
        import asyncio

        while not server.started:
            await asyncio.sleep(0.05)
        print("NYASUB_READY " + json.dumps({"port": port, "pid": os.getpid()}), flush=True)

    env.log(f"本地服务：http://{args.host}:{port}" + ("（需要 token）" if args.token else ""))
    import asyncio

    async def main():
        await asyncio.gather(server.serve(), ready())

    asyncio.run(main())
    return 0


def _watch_parent(pid, server):
    """父进程（桌面壳）退出时内核也退出，避免留下孤儿进程。"""
    import threading
    import time

    def alive():
        if os.name == "nt":
            import ctypes

            h = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            if not h:
                return False
            r = ctypes.windll.kernel32.WaitForSingleObject(h, 0)
            ctypes.windll.kernel32.CloseHandle(h)
            return r == 0x102  # WAIT_TIMEOUT：还在运行
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def loop():
        while alive():
            time.sleep(1)
        server.should_exit = True  # 先设退出标志：桌面壳没了，标准输出管道也断了，打日志可能失败
        import sys

        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")  # 管道已断，退出时不再刷输出
        env.log("桌面壳已退出，内核随之退出")

    threading.Thread(target=loop, daemon=True).start()


def cmd_components(args):
    from . import components

    if args.action == "install":
        components.install(args.name, args.index)
    elif args.action == "remove":
        components.remove(args.name)
    st = components.status()
    g = st["gpu"]
    print(f"  显卡：{g['name'] or '未检测到 NVIDIA 显卡'}；CUDA 设备 {g['cuda_devices']} 个；画面字识别 {'可用 CUDA' if g['ort_cuda'] else '只用 CPU'}")
    for c in st["components"]:
        print(f"  {'✓' if c['installed'] else '·'} {c['name']:9s} {c['label']}：{'已安装 ' + str(c['size_mb']) + ' MB' if c['installed'] else c['detail']}")
    return 0


def cmd_langs(args):
    from . import lang

    for p in lang.available():
        print(f"  {p.code:8s} {p.name:6s} 每行 {p.line_chars} {'字符' if p.word_based else '字'} · 语速上限 {p.cps}/秒"
              f" · OCR 模型 {p.ocr_lang}")
    return 0


def main(argv=None):
    env.setup()
    ap = argparse.ArgumentParser(prog="nyasub", description="多源（字幕 / 画面字 / 语音）融合的字幕生成与翻译工具")
    ap.add_argument("--version", action="version", version=f"NyaSubFusion {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="处理视频 / 音频")
    r.add_argument("inputs", nargs="+", help="视频、音频文件或文件夹")
    r.add_argument("--config", help="配置文件（TOML）")
    r.add_argument("--source", help="源语言，例如 ja / en / zh-Hans；默认自动判断")
    r.add_argument("--target", help="目标语言，逗号分隔，例如 zh-Hans,en")
    r.add_argument("--series", help="所属作品（共享节目说明、术语表）")
    r.add_argument("--subs", help="字幕文件路径；none 表示不用字幕。默认自动找同名字幕或内封字幕")
    r.add_argument("--sub-track", type=int, help="使用第几条内封字幕轨（从 0 开始）")
    r.add_argument("--no-asr", action="store_true", help="不做语音识别")
    r.add_argument("--no-ocr", action="store_true", help="不做画面字识别")
    r.add_argument("--ocr-refine", choices=["none", "vlm"], help="是否用视觉大模型复核画面字")
    r.add_argument("--video", choices=["off", "mux", "burn"],
                   help="输出视频：mux 封装 MKV（字幕轨 + 字体）/ burn 压制硬字幕 MP4")
    r.add_argument("--translate", choices=["api", "export", "off"], help="翻译方式，覆盖配置")
    r.add_argument("--until", help="做到哪一步为止：probe / subs / asr / ocr / fuse / translate / render")
    r.add_argument("--redo", default="", help="强制重做的步骤，逗号分隔，例如 ocr,fuse；all = 全部")
    r.add_argument("--workdir", help="工作目录（只处理单个文件时可用）")
    r.set_defaults(fn=cmd_run)

    d = sub.add_parser("doctor", help="检查运行环境")
    d.add_argument("--config")
    d.set_defaults(fn=cmd_doctor)

    sub.add_parser("langs", help="列出语言包").set_defaults(fn=cmd_langs)

    cp = sub.add_parser("components", help="查看 / 安装可选组件（GPU 加速）")
    cp.add_argument("action", nargs="?", choices=["list", "install", "remove"], default="list")
    cp.add_argument("name", nargs="?", help="cuda-asr / cuda-ocr")
    cp.add_argument("--index", help="PyPI 索引地址，默认依次试官方源和国内镜像")
    cp.set_defaults(fn=cmd_components)

    sv = sub.add_parser("serve", help="启动本地服务（界面用）")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--token", help="设置后所有接口都要带这个 token（桌面壳启动时用）")
    sv.add_argument("--parent-pid", type=int, help="这个进程退出时服务也退出（桌面壳启动时用）")
    sv.set_defaults(fn=cmd_serve)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
