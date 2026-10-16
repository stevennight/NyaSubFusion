"""离线翻译：把任务导出成文件交给外部助手（如 Claude Code），完成后导回。"""
import os
import shutil
import sys
from pathlib import Path

from .. import lang
from ..core.store import load_json, save_json, write_text
from ..env import log
from . import prompts
from .client import parse_json
from .translate import apply_result, chunk_items, fallback, finalize, merge_terms

TASK_MD = """# 字幕融合 + 翻译任务

这个目录是 NyaSubFusion 导出的翻译任务（{src} → {tgt}）。请按下面步骤完成，**只写结果文件，不要修改输入文件**。

## 第 1 步：术语表
读 `glossary.input.txt`，按下面「术语表要求」整理，写入 `glossary.result.json`。

## 第 2 步：逐段处理
按编号顺序处理 `job_001.input.txt`、`job_002.input.txt` …，每个写出对应的 `job_001.result.json` …
- 每段开头的术语表、概要以第 1 步的结果为准。
- 处理某段前，可以看上一段的 result，保证衔接和用词统一。
- 画面字条目对应的截图在 `../crops/<id>.jpg`，OCR 结果可疑时可以直接看图核对。
- 每段处理完都要自检：输入里的每个 id 都必须出现在 cues[].src 或 drop 里，且只出现一次。

## 第 3 步：生成最终字幕（你自己执行，不要让用户操作）
全部段落完成后，运行下面这条命令，它会校验并导入结果、在视频旁边生成字幕文件：

```
{finish}
```

如果输出里有「漏了 …」或「翻译任务还没完成」，补好对应的 result 文件后再运行一次，直到出现「已输出」。
最后告诉用户生成的字幕文件路径。

---

## 术语表要求
{glossary}

---

## 逐段处理要求
{chunk}
"""


def finish_command(video, config_path=None) -> str:
    cmd = f'"{sys.executable}" -m nyasub run "{video}" --translate export'
    if config_path:
        cmd += f' --config "{os.path.abspath(config_path)}"'
    return cmd


def export_jobs(items, cfg, job_dir: Path, video, src_code, known_terms, fingerprint, config_path=None):
    """导出任务。之前的任务对应的融合结果变了（指纹不同）时，旧任务移到 jobs.old。"""
    stamp = job_dir / "source.fp"
    if job_dir.is_dir() and any(job_dir.iterdir()):
        if stamp.exists() and stamp.read_text(encoding="utf-8").strip() == fingerprint:
            return job_dir, len(list(job_dir.glob("job_*.items.json")))
        bak = job_dir.with_name(job_dir.name + ".old")
        shutil.rmtree(bak, ignore_errors=True)
        shutil.copytree(job_dir, bak)
        shutil.rmtree(job_dir, ignore_errors=True)
        log(f"融合结果有变化，旧的翻译任务已移到 {bak.name}")
    job_dir.mkdir(parents=True, exist_ok=True)
    src = lang.get(src_code)
    tgt_code = lang.canonical(cfg.project.targets[0])
    tgt = lang.get(tgt_code)
    system = prompts.chunk_system(src, tgt, cfg.project.context, cfg.render.lang_options.get(tgt_code, {}))
    finish = finish_command(video, config_path)
    write_text(job_dir / "TASK.md", TASK_MD.format(src=src.name, tgt=tgt.name, finish=finish,
                                                   glossary=prompts.glossary_system(src, tgt), chunk=system))
    write_text(job_dir / "glossary.input.txt", prompts.glossary_input(items, known_terms))
    chunks = chunk_items(items, cfg.llm.chunk_seconds, cfg.llm.chunk_max_items)
    for i, chunk in enumerate(chunks, 1):
        prompt = prompts.chunk_prompt(chunk, "（见 glossary.result.json）", "（见 glossary.result.json 的 summary）",
                                      None, tgt_code)
        write_text(job_dir / f"job_{i:03d}.input.txt", prompt)
        save_json(job_dir / f"job_{i:03d}.items.json", chunk)
    write_text(stamp, fingerprint)
    return job_dir, len(chunks)


def import_jobs(job_dir: Path, cfg, src_code, known_terms):
    """读取外部助手写好的结果。返回 (cues 或 None, 缺失的任务, glossary)。"""
    names = sorted(job_dir.glob("job_*.items.json")) if job_dir.is_dir() else []
    if not names:
        return None, ["(未导出)"], None
    tgt_code = lang.canonical(cfg.project.targets[0])
    all_cues, missing_jobs = [], []
    for n in names:
        base = n.name[: -len(".items.json")]
        res_path = job_dir / f"{base}.result.json"
        if not res_path.exists():
            missing_jobs.append(base)
            continue
        chunk = load_json(n)
        cues, missing = apply_result(chunk, parse_json(res_path.read_text(encoding="utf-8")), tgt_code, src_code)
        if missing:
            log(f"  警告：{base} 漏了 {', '.join(missing)}")
            cues += fallback(chunk, missing, tgt_code, src_code)
        all_cues += sorted(cues, key=lambda x: x["start"])
    gl = {}
    gp = job_dir / "glossary.result.json"
    if gp.exists():
        gl = parse_json(gp.read_text(encoding="utf-8"))
    terms = merge_terms(known_terms, gl.get("terms"))
    glossary = {"summary": gl.get("summary", ""), "terms": terms, "new": len(terms) - len(known_terms or [])}
    return finalize(all_cues), missing_jobs, glossary
