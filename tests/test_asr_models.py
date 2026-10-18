"""语音识别模型：Hugging Face 不通时从 ModelScope 下载；蒸馏模型的对齐头修正（不修会崩溃）。"""
import json

from nyasub.sources import asr_models


def test_fix_alignment_heads_for_distilled_model(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"alignment_heads": [[25, 18], [20, 1]], "x": 1}), encoding="utf-8")
    asr_models._fix_alignment(tmp_path, 2)
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert cfg["x"] == 1 and {a for a, _ in cfg["alignment_heads"]} == {1} and len(cfg["alignment_heads"]) == 20
    before = (tmp_path / "config.json").read_text(encoding="utf-8")
    asr_models._fix_alignment(tmp_path, 2)  # 已经对了：不动
    assert (tmp_path / "config.json").read_text(encoding="utf-8") == before


def test_resolve_falls_back_to_modelscope(monkeypatch, isolated_home):
    import faster_whisper.utils as fwu

    def no_hf(name, local_files_only=False, cache_dir=None):
        raise OSError("Hugging Face 连不上\n第二行")
    monkeypatch.setattr(fwu, "download_model", no_hf)
    got = []

    def fake_ms(repo, dest, mb):
        got.append(repo)
        dest.mkdir(parents=True)
        (dest / "model.bin").write_bytes(b"x")
        (dest / "config.json").write_text(json.dumps({"alignment_heads": [[25, 18]]}), encoding="utf-8")
    monkeypatch.setattr(asr_models, "_download_ms", fake_ms)
    path = asr_models.resolve("kotoba-tech/kotoba-whisper-v2.0-faster")
    assert got == ["philpw99/kotoba-whisper-v2.0-faster"] and path.endswith("kotoba-tech__kotoba-whisper-v2.0-faster")
    assert json.loads(open(path + "/config.json", encoding="utf-8").read())["alignment_heads"][0][0] == 1
    # 第二次直接用下载好的，不再访问网络
    got.clear()
    assert asr_models.resolve("kotoba-tech/kotoba-whisper-v2.0-faster") == path and got == []


def test_catalog_lists_models_with_descriptions():
    ids = [m["id"] for m in asr_models.catalog()]
    assert ids[:2] == ["large-v3", "large-v3-turbo"] and all(m["desc"] for m in asr_models.catalog())
