"""打包后的内核入口：nyasub-core.exe = python -m nyasub。"""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from nyasub.cli import main

    sys.exit(main())
