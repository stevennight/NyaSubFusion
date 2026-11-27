"""v0.7.0 M4 测试：作品级歌曲库与内容驱动子序列对齐、剧中插曲统一、三层联网建档与歌曲库 API。"""
from nyasub.core import songs as songs_mod
from nyasub.core.config import Config
from nyasub.llm import research


def test_song_alignment_fixes_misheard_lyrics_and_handles_verse2_and_improvisation(tmp_path):
    """内容驱动对齐：修正日式英语听错（Shadow -> Shall）、兼容换 2 番/删减、拒绝即兴哼唱误替换。"""
    god_fate = {
        "id": "god-fate",
        "title": "God FATE",
        "artist": "飛蘭",
        "role": "OP",
        "lines": [
            {"orig": "Shall wish come true, it's been the start of beginning", "tr": {"zh-Hans": "愿心愿成真 一切由此开始"}},
            {"orig": "You can choose my fate", "tr": {"zh-Hans": "你可以决定我的命运"}},
            {"orig": "乾いた風に抱かれて", "tr": {"zh-Hans": "被干燥的风拥抱"}},
            {"orig": "生命への神秘を覚え", "tr": {"zh-Hans": "领悟生命的神秘"}},
            # 2 番歌词
            {"orig": "凍えた夜を越えて", "tr": {"zh-Hans": "越过冰冷的夜晚"}},
            {"orig": "約束の場所へ向かう", "tr": {"zh-Hans": "前往约定的地方"}},
        ],
    }
    songs_mod.save_songs(tmp_path, [god_fate])

    # 第 1 集：OP 1 番（含听错 Shadow）
    ep1_cues = [
        {"id": "q1", "start": 68.9, "end": 74.4, "type": "song",
         "orig": {"lang": "ja", "text": "♪ Shadow wish come true, it's been in the start of beginning."}, "tr": {}},
        {"id": "q2", "start": 74.5, "end": 78.6, "type": "song",
         "orig": {"lang": "ja", "text": "♪ You can choose my fate"}, "tr": {}},
        {"id": "q3", "start": 81.8, "end": 84.6, "type": "song",
         "orig": {"lang": "ja", "text": "♪ 乾いた風に抱かれて"}, "tr": {}},
    ]
    n = songs_mod.align_and_fill_songs(ep1_cues, tmp_path, Config(), tgt_code="zh-Hans")
    assert n == 3
    assert ep1_cues[0]["orig"]["text"] == "♪ Shall wish come true, it's been the start of beginning"
    assert ep1_cues[0]["tr"]["zh-Hans"]["text"] == "♪ 愿心愿成真 一切由此开始"
    assert ep1_cues[0]["song_locked"] is True

    # 某集特殊剪辑：直接唱 2 番
    ep12_cues = [
        {"id": "q1", "start": 10.0, "end": 14.0, "type": "song",
         "orig": {"lang": "ja", "text": "♪ 凍えた夜を越えて"}, "tr": {}},
        {"id": "q2", "start": 14.5, "end": 18.0, "type": "song",
         "orig": {"lang": "ja", "text": "♪ 約束の場所へ向かう"}, "tr": {}},
    ]
    n2 = songs_mod.align_and_fill_songs(ep12_cues, tmp_path, Config(), tgt_code="zh-Hans")
    assert n2 == 2
    assert ep12_cues[0]["tr"]["zh-Hans"]["text"] == "♪ 越过冰冷的夜晚"
    assert ep12_cues[1]["tr"]["zh-Hans"]["text"] == "♪ 前往约定的地方"

    # 即兴乱哼：不匹配任何歌曲，保留原样不乱替换
    improv = [
        {"id": "q9", "start": 200.0, "end": 203.0, "type": "song",
         "orig": {"lang": "ja", "text": "♪ ららら ふんふん"}, "tr": {}},
        {"id": "q10", "start": 203.5, "end": 206.0, "type": "song",
         "orig": {"lang": "ja", "text": "♪ きょうもいいてんき"}, "tr": {}},
    ]
    assert songs_mod.align_and_fill_songs(improv, tmp_path, Config(), tgt_code="zh-Hans") == 0
    assert improv[0]["orig"]["text"] == "♪ ららら ふんふん"
    assert not improv[0].get("song_locked")


def test_insert_song_save_and_reuse_across_episodes(tmp_path):
    """剧中插曲（如滨路哼唱《埴生の宿》）翻译后自动沉淀到作品歌曲库，后续集数自动复用。"""
    insert_song = {
        "id": "hanyuu-no-yado",
        "title": "埴生の宿",
        "artist": "",
        "role": "insert",
        "lines": [
            {"orig": "埴生の宿も我が宿", "tr": {}},
            {"orig": "玉の装い羨まじ", "tr": {}},
        ],
    }
    songs_mod.save_songs(tmp_path, [insert_song])

    # 第 1 集对齐原文（此时库里尚无译文），翻译后沉淀
    ep1 = [
        {"id": "q42", "start": 250.8, "end": 257.0, "type": "song",
         "orig": {"lang": "ja", "text": "♪ 埴生の宿も我が宿"}, "tr": {}},
        {"id": "q43", "start": 259.8, "end": 268.0, "type": "song",
         "orig": {"lang": "ja", "text": "♪ 玉の装い羨まじ"}, "tr": {}},
    ]
    assert songs_mod.align_and_fill_songs(ep1, tmp_path, Config(), tgt_code="zh-Hans") == 2
    assert not ep1[0].get("song_locked")
    ep1[0]["tr"]["zh-Hans"] = {"text": "♪ 茅屋虽简 也是我家"}
    ep1[1]["tr"]["zh-Hans"] = {"text": "♪ 不羡华堂美饰"}
    assert songs_mod.save_translated_songs(ep1, tmp_path, "zh-Hans") == 2

    # 第 5 集再次哼唱同一首歌：自动锁定标准译文
    ep5 = [
        {"id": "q10", "start": 100.0, "end": 105.0, "type": "song",
         "orig": {"lang": "ja", "text": "♪ 埴生の宿も我が宿"}, "tr": {}},
        {"id": "q11", "start": 106.0, "end": 112.0, "type": "song",
         "orig": {"lang": "ja", "text": "♪ 玉の装い羨まじ"}, "tr": {}},
    ]
    assert songs_mod.align_and_fill_songs(ep5, tmp_path, Config(), tgt_code="zh-Hans") == 2
    assert ep5[0]["song_locked"] is True
    assert ep5[0]["tr"]["zh-Hans"]["text"] == "♪ 茅屋虽简 也是我家"


def test_research_work_uses_tiered_apis_without_network(monkeypatch):
    """三层检索优先调用 Bangumi + Wikipedia 结构化 API，在无 LLM 模式下也能直接返回可用资料。"""
    monkeypatch.setattr(research, "fetch_bangumi", lambda q: {
        "source": "bangumi",
        "title": "八犬伝―東方八犬異聞―",
        "title_cn": "八犬传—东方八犬异闻—",
        "summary": "大冢村火灾幸存者犬冢信乃与犬川庄介的故事。",
        "terms": [{"src": "犬塚信乃", "tr": "犬冢信乃", "type": "人名", "note": "主角 / CV 柿原徹也", "from": "import"}],
    })
    monkeypatch.setattr(research, "fetch_wikipedia", lambda q, langs=("ja", "zh"): [
        {"source": "wikipedia:ja", "title": "八犬伝―東方八犬異聞―", "text": "あべ美幸による漫画作品。"},
    ])
    res = research.research_work("八犬伝", content_kind="anime", cfg=None)
    assert "bangumi" in res["sources"] and "wikipedia:ja" in res["sources"]
    assert "犬冢信乃" in res["context"]
    assert res["terms"][0]["src"] == "犬塚信乃"

    # 手工粘贴文本兜底：不触发联网 API
    pasted = research.research_work("冷门作品", content_kind="anime", cfg=None, pasted_text="这是一段手工粘贴的角色简介。")
    assert pasted["sources"] == ["pasted"] and "手工粘贴" in pasted["context"]


def test_series_songs_api(isolated_home):
    from fastapi.testclient import TestClient

    from nyasub.server.app import create_app

    client = TestClient(create_app())
    r = client.put("/api/series/八犬传/songs", json={"songs": [
        {"id": "god-fate", "title": "God FATE", "artist": "飛蘭", "role": "OP",
         "lines": [{"orig": "You can choose my fate", "tr": {"zh-Hans": "你可以决定我的命运"}}]},
    ]})
    assert r.status_code == 200
    got = client.get("/api/series/八犬传/songs").json()
    assert len(got["songs"]) == 1 and got["songs"][0]["title"] == "God FATE"
