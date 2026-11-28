"""v0.8.0 测试：本机 ONNX CTC 强制对齐、人声能量包络长音延伸/尾噪截断、歌曲局部连续性与 2 对 1 拆分对齐、排版层阅读缓冲。"""
import numpy as np

from nyasub.core import config, songs as songs_mod
from nyasub.render import layout
from nyasub.sources import forced_align
from nyasub.sources.asr_api import HOP


class _FakeWav:
    """用 50ms 分贝数组构造的轻量测试人声轨。"""
    def __init__(self, db_list, floor=-60.0):
        self.db = np.array(db_list, dtype=np.float32)
        self.floor = float(floor)
        self.duration = len(self.db) * HOP

    def _range(self, a, b):
        i = max(0, min(len(self.db), int(round(a / HOP))))
        j = max(i, min(len(self.db), int(round(b / HOP))))
        return i, j


def test_ctc_viterbi_align_basic():
    """标准 CTC Viterbi 能从逐帧声学概率中精确定位每个字符音素的帧区间。"""
    T, V = 40, 10
    lp = np.full((T, V), -10.0, dtype=np.float32)
    lp[:, 0] = -0.05  # blank 默认高概率
    # 音素 1 在帧 5..7，音素 2 在帧 12..15，音素 3 在帧 25..28
    lp[5:8, 1] = -0.02
    lp[5:8, 0] = -8.0
    lp[12:16, 2] = -0.02
    lp[12:16, 0] = -8.0
    lp[25:29, 3] = -0.02
    lp[25:29, 0] = -8.0

    spans = forced_align.ctc_viterbi_align(lp, [1, 2, 3], blank=0)
    assert len(spans) == 3
    assert spans[0][0] == 5 and spans[0][1] == 8
    assert spans[1][0] == 12 and spans[1][1] == 16
    assert spans[2][0] == 25 and spans[2][1] == 29

    # 软时间带测试：当同一音素在帧 10（0.2s）和帧 450（9.0s）同样清晰出现时，
    # 带粗估区间 (430, 470) 的软时间带能锁到真实位置 450 而不是被吸回 10
    T2 = 500
    lp2 = np.full((T2, V), -10.0, dtype=np.float32)
    lp2[:, 0] = -0.05
    lp2[10:14, 1] = -0.02
    lp2[10:14, 0] = -8.0
    lp2[450:454, 1] = -0.02
    lp2[450:454, 0] = -8.0
    sp_banded = forced_align.ctc_viterbi_align(lp2, [1], blank=0, token_bounds=[(430, 470)], frame_dur=0.02)
    assert 448 <= sp_banded[0][0] <= 454


def test_refine_with_vocal_envelope_sustained_and_noise():
    """人声包络精修：
    1) 唱歌长音（CTC 只打在开头，声音持续到 3.4s）→ 延伸到真正唱完；
    2) 句尾吸噪（1.8s 说完，中间静音 1.2s，3.0s 有孤立尾噪）→ 截断在 1.8s；
    3) 句首串尾（前 0.2s 杂音，静音 0.8s，6.0s 才开口）→ 起点推进到 6.0s。"""
    # 200 帧 = 10.0s（每帧 50ms）
    db = [-60.0] * 200
    # 场景 1: 0.5s..3.4s 持续歌唱（帧 10..68），CTC 过早截在 2.0s
    for f in range(10, 68):
        db[f] = -15.0
    # 场景 2: 4.0s..4.8s 主体语音（帧 80..96）+ 静音 + 5.8s..6.0s 孤立尾噪（帧 116..120）
    for f in range(80, 96):
        db[f] = -14.0
    for f in range(116, 120):
        db[f] = -18.0
    # 场景 3: 6.5s..6.7s 串入前句尾音（帧 130..134）+ 静音 0.8s + 7.5s..8.5s 真正开口（帧 150..170）
    for f in range(130, 134):
        db[f] = -18.0
    for f in range(150, 170):
        db[f] = -14.0

    w = _FakeWav(db)
    segs = [
        {"id": "a1", "start": 0.5, "end": 2.0, "text": "sustain", "timing": "aligned"},
        {"id": "a2", "start": 4.0, "end": 6.0, "text": "選べ", "timing": "aligned"},
        {"id": "a3", "start": 6.5, "end": 8.5, "text": "lead_bleed", "timing": "aligned"},
    ]
    out = forced_align.refine_with_vocal_envelope(segs, w)
    # 1) 长音延伸至 ~3.4s（不超过 a2.start - 0.05 = 3.95）
    assert 3.3 <= out[0]["end"] <= 3.5
    # 2) 句尾吸噪被截断在 ~4.8s
    assert 4.7 <= out[1]["end"] <= 4.95
    # 3) 句首串尾被裁到 ~7.5s
    assert 7.4 <= out[2]["start"] <= 7.6


def test_song_alignment_prevents_30_line_title_jump(tmp_path):
    """OP 结尾的短尾音「Fate」绝不能跨 30 行跳到完整版末尾的歌名行《God FATE》。"""
    filler = [{"orig": f"別の歌詞行その{k}", "tr": {}} for k in range(30)]
    god_fate = {
        "id": "god-fate",
        "title": "God FATE",
        "artist": "飛蘭",
        "role": "OP",
        "lines": [
            {"orig": "果てなき闇の彼方へ", "tr": {"zh-Hans": "前往无尽黑暗的彼岸"}},
            {"orig": "吠えて交わる FATE", "tr": {"zh-Hans": "咆哮着交织的命运"}},
            {"orig": "この世界を信じて", "tr": {"zh-Hans": "相信这个世界"}},
            *filler,
            {"orig": "《God FATE》", "tr": {"zh-Hans": "《God FATE》"}},
        ],
    }
    songs_mod.save_songs(tmp_path, [god_fate])
    cues = [
        {"id": "q0", "start": 142.0, "end": 145.5, "type": "song",
         "orig": {"lang": "ja", "text": "♪ 果てなき闇の彼方へ"}, "tr": {}},
        {"id": "q1", "start": 146.0, "end": 149.0, "type": "song",
         "orig": {"lang": "ja", "text": "♪ 愛で交わる Fate"}, "tr": {}},
        {"id": "q2", "start": 149.2, "end": 150.5, "type": "song",
         "orig": {"lang": "ja", "text": "♪ Fate"}, "tr": {}},
    ]
    songs_mod.align_and_fill_songs(cues, tmp_path, config.Config(), tgt_code="zh-Hans")
    assert cues[0]["orig"]["text"] == "♪ 果てなき闇の彼方へ"
    assert cues[1]["orig"]["text"] == "♪ 吠えて交わる FATE"
    # q2 绝不能跨 30 行错配成《God FATE》，且不静默删掉原句
    assert len(cues) == 3 and "God FATE" not in cues[2]["orig"]["text"]


def test_song_alignment_phonetic_kana_and_lossless_parens(tmp_path):
    """日语汉字/假名读音匹配与全语言括号内容兼容（匹配时兼容括号注音/和声，输出时不盲删官方歌词括号）。"""
    hanyuu = {
        "id": "hanyuu",
        "title": "埴生の宿",
        "artist": "",
        "role": "insert",
        "lines": [
            {"orig": "埴生(はにゅう)の宿も わが宿", "tr": {}},
            {"orig": "玉のよそおい うらやまじ", "tr": {}},
            {"orig": "のどかなりや 春の空", "tr": {}},
            {"orig": "花はあるじ 鳥は友", "tr": {}},
        ],
    }
    songs_mod.save_songs(tmp_path, [hanyuu])
    cues = [
        {"id": "q1", "start": 251.4, "end": 254.3, "type": "song", "orig": {"lang": "ja", "text": "♪ 埴生の宿も"}, "tr": {}},
        {"id": "q2", "start": 254.4, "end": 257.9, "type": "song", "orig": {"lang": "ja", "text": "♪ 我が宿"}, "tr": {}},
        {"id": "q3", "start": 258.2, "end": 263.9, "type": "song", "orig": {"lang": "ja", "text": "♪ 玉の装い"}, "tr": {}},
        {"id": "q4", "start": 264.5, "end": 268.5, "type": "song", "orig": {"lang": "ja", "text": "♪ 羨まじ"}, "tr": {}},
        {"id": "q5", "start": 269.2, "end": 273.3, "type": "song", "orig": {"lang": "ja", "text": "♪ のどかなりや"}, "tr": {}},
        {"id": "q6", "start": 273.5, "end": 276.8, "type": "song", "orig": {"lang": "ja", "text": "♪ 春の空"}, "tr": {}},
        {"id": "q7", "start": 277.7, "end": 281.9, "type": "song", "orig": {"lang": "ja", "text": "♪ 花は主"}, "tr": {}},
        {"id": "q8", "start": 283.3, "end": 286.3, "type": "song", "orig": {"lang": "ja", "text": "♪ 鳥は友"}, "tr": {}},
    ]
    n = songs_mod.align_and_fill_songs(cues, tmp_path, config.Config(), tgt_code="zh-Hans")
    assert n == 8
    assert [c["orig"]["text"] for c in cues] == [
        "♪ 埴生(はにゅう)の宿も", "♪ わが宿",
        "♪ 玉のよそおい", "♪ うらやまじ",
        "♪ のどかなりや", "♪ 春の空",
        "♪ 花はあるじ", "♪ 鳥は友",
    ]
    # 英文和声括号也能高分匹配且不被删除
    assert songs_mod._line_sim("I love you", "I love you (yeah, yeah)") == 100.0


def test_song_alignment_2_to_1_split_prevents_verse2_jump(tmp_path):
    """插曲相邻两条短句「花は主」「鳥は友」应 2 对 1 拆分对齐到「花はあるじ 鳥は友」，而不是把「鳥は友」跳到 2 番「月はあるじ むしは友」。"""
    hanyuu = {
        "id": "hanyuu-no-yado",
        "title": "埴生の宿",
        "artist": "",
        "role": "insert",
        "lines": [
            {"orig": "埴生の宿も 我が宿", "tr": {}},
            {"orig": "玉の装い 羨まじ", "tr": {}},
            {"orig": "のどかなりや 春の空", "tr": {}},
            {"orig": "花はあるじ 鳥は友", "tr": {}},
            {"orig": "おお我が宿よ 楽しとも", "tr": {}},
            {"orig": "楽しきかな 埴生の宿", "tr": {}},
            # 2 番
            {"orig": "書読む窓も 我が窓", "tr": {}},
            {"orig": "瑠璃の床も 羨まじ", "tr": {}},
            {"orig": "清らなりや 秋の夜半", "tr": {}},
            {"orig": "月はあるじ 虫は友", "tr": {}},
        ],
    }
    songs_mod.save_songs(tmp_path, [hanyuu])
    cues = [
        {"id": "q1", "start": 250.0, "end": 255.0, "type": "song",
         "orig": {"lang": "ja", "text": "♪ 埴生の宿も 我が宿"}, "tr": {}},
        {"id": "q2", "start": 256.0, "end": 261.0, "type": "song",
         "orig": {"lang": "ja", "text": "♪ 玉の装い 羨まじ"}, "tr": {}},
        {"id": "q3", "start": 262.0, "end": 266.0, "type": "song",
         "orig": {"lang": "ja", "text": "♪ のどかなりや 春の空"}, "tr": {}},
        {"id": "q4", "start": 267.0, "end": 269.5, "type": "song",
         "orig": {"lang": "ja", "text": "♪ 花は主"}, "tr": {}},
        {"id": "q5", "start": 270.0, "end": 272.5, "type": "song",
         "orig": {"lang": "ja", "text": "♪ 鳥は友"}, "tr": {}},
    ]
    n = songs_mod.align_and_fill_songs(cues, tmp_path, config.Config(), tgt_code="zh-Hans")
    assert n == 5
    assert cues[3]["orig"]["text"] == "♪ 花はあるじ"
    assert cues[4]["orig"]["text"] == "♪ 鳥は友"


def test_trim_outlier_spans_and_safe_overlap():
    """CTC 离群音素剔除（去掉跨越 >1.5s 静音的 1~2 个孤立边缘音素，保留正常多词停顿）+ 乱序条目不压扁前句。"""
    # 10 个音素，frame_dur=0.02s：前 9 个聚在 1.0s..1.8s，最后 1 个跳到 9.5s（如 では、選べ 尾音吸噪）
    spans = [(50 + i * 4, 53 + i * 4, -0.5) for i in range(9)] + [(475, 478, -2.0)]
    trimmed = forced_align._trim_outlier_spans(spans, frame_dur=0.02, max_gap_s=1.5)
    assert len(trimmed) == 9 and trimmed[-1][1] == 53 + 8 * 4

    # 乱序条目（后一条 start 比前一条 start 还早）绝不能把前一条压扁成 0.08s
    w = _FakeWav([-15.0] * 200)
    segs = [
        {"id": "a1", "start": 2.0, "end": 4.0, "text": "信乃、それ村雨の！", "timing": "aligned"},
        {"id": "a2", "start": 1.5, "end": 1.9, "text": "ん？", "timing": "segment"},
    ]
    out = forced_align.refine_with_vocal_envelope(segs, w)
    assert out[0]["end"] >= 3.9


def test_layout_reading_buffer_respects_gaps_and_onscreen():
    """阅读缓冲（在翻译落盘前一次性施加，审校页与导出所见即所得）：
    纯语音来源空档处提前 0.08s / 留白 0.35s；紧邻句保持 >=0.05s 间隙不重叠；硬字幕/广播字幕不拉动；
    且 layout.build 不再二次叠加缓冲。"""
    cfg = config.Config()
    prof = next(p for p in cfg.render.profiles if p.name == "bilingual")
    cues = [
        {"id": "q1", "start": 10.0, "end": 12.0, "type": "dialogue", "sources": ["a1"],
         "orig": {"lang": "ja", "text": "おはよう"}, "tr": {"zh-Hans": {"text": "早上好"}}},
        {"id": "q2", "start": 12.15, "end": 14.0, "type": "dialogue", "sources": ["a2"],
         "orig": {"lang": "ja", "text": "こんにちは"}, "tr": {"zh-Hans": {"text": "你好"}}},
        {"id": "q3", "start": 20.0, "end": 22.0, "type": "dialogue", "sources": ["a3"], "onscreen": True,
         "orig": {"lang": "ja", "text": "画面字幕"}, "tr": {"zh-Hans": {"text": "画面字幕"}}},
        {"id": "q4", "start": 30.0, "end": 32.0, "type": "dialogue", "sources": ["c1"],
         "orig": {"lang": "ja", "text": "广播字幕"}, "tr": {"zh-Hans": {"text": "广播字幕"}}},
    ]
    layout.apply_reading_buffer(cues)
    # q1 开头空档充裕 -> 提前 0.08s；与 q2 间距仅 0.15s -> 保留 >=0.05s 间隙不重叠
    assert cues[0]["start"] == 9.92
    assert cues[1]["start"] - cues[0]["end"] >= 0.049
    # q2 后面空档充裕 -> 延后 0.35s
    assert cues[1]["end"] == 14.35
    # q3 是 onscreen -> 不拉动；q4 是广播字幕 c1 -> 不拉动
    assert cues[2]["start"] == 20.0 and cues[2]["end"] == 22.0
    assert cues[3]["start"] == 30.0 and cues[3]["end"] == 32.0
    # 经过 layout.build 导出不再二次拉动（审校页手动修改完全保真）
    items = layout.build(cues, prof, cfg, "ja")
    assert items[0]["start"] == 9.92 and items[1]["end"] == 14.35
