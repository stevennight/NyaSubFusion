"""校验仓库内所有版本号文件是否一致（可选校验 Git tag）。

    python packaging/check_version.py [--tag v0.5.30] [--print]
"""
import argparse
import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_versions() -> dict[str, str]:
    versions: dict[str, str] = {}

    with open(ROOT / "pyproject.toml", "rb") as f:
        versions["pyproject.toml"] = tomllib.load(f)["project"]["version"]

    with open(ROOT / "desktop/src-tauri/Cargo.toml", "rb") as f:
        versions["desktop/src-tauri/Cargo.toml"] = tomllib.load(f)["package"]["version"]

    for rel in ("web/package.json", "desktop/package.json", "desktop/src-tauri/tauri.conf.json"):
        with open(ROOT / rel, encoding="utf-8") as f:
            versions[rel] = json.load(f)["version"]

    return versions


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", help="Git tag（例如 v0.5.30），若提供则校验必须等于 v<version>")
    ap.add_argument("--print", dest="print_only", action="store_true", help="仅输出版本号（供 CI 捕获）")
    args = ap.parse_args()

    versions = read_versions()
    unique = set(versions.values())
    if len(unique) != 1:
        print("版本号不一致：", file=sys.stderr)
        for path, ver in versions.items():
            print(f"  {path}: {ver}", file=sys.stderr)
        return 1

    version = next(iter(unique))
    if args.tag:
        expected = f"v{version}"
        if args.tag != expected:
            print(f"Tag {args.tag} 与仓库版本号 {version}（期望 {expected}）不匹配", file=sys.stderr)
            return 1

    if args.print_only:
        print(version)
    else:
        print(f"版本一致：{version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
