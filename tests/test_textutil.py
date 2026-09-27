from jarvis.textutil import iter_sentences


def test_splits_on_japanese_sentence_end():
    chunks = ["今日は", "晴れです。", "明日は雨みたいですね。"]
    assert list(iter_sentences(chunks)) == ["今日は晴れです。", "明日は雨みたいですね。"]


def test_short_fragments_are_merged_with_the_next_sentence():
    # 「はい。」だけで TTS を呼ぶと、かえって発話が細切れになる。
    assert list(iter_sentences(["はい。", "承知しました。"])) == ["はい。承知しました。"]


def test_long_run_without_punctuation_is_cut_at_a_comma():
    text = "あ" * 30 + "、" + "い" * 40 + "。"
    out = list(iter_sentences([text], max_chars=50, min_chars=5))
    assert out[0].endswith("、")
    assert "".join(out) == text


def test_trailing_text_without_punctuation_is_emitted():
    assert list(iter_sentences(["最後は句点なし"])) == ["最後は句点なし"]


def test_empty_stream_yields_nothing():
    assert list(iter_sentences([])) == []
    assert list(iter_sentences(["   ", "\n"])) == []
