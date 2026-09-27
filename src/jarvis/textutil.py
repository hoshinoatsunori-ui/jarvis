"""LLM のトークンストリームを、TTS に渡せる文の単位に切り出す。

TTS の time-to-first-audio を縮めるには、応答が全部出揃うのを待たずに
1文できた時点で合成を始める必要がある。ここはその切り出しだけを担当する。
"""

from __future__ import annotations

from typing import Iterable, Iterator

#: 文末とみなす文字。日本語の句点・感嘆・疑問と、英語のピリオド類。
SENTENCE_ENDINGS = "。！？!?\n"

#: 文末が来なくてもこの長さを超えたら区切る（長い列挙などで発話が始まらないのを防ぐ）。
MAX_CHUNK_CHARS = 60

#: これより短い断片は次とまとめる（「はい。」だけで合成を呼ぶとかえって遅い）。
MIN_CHUNK_CHARS = 8


def iter_sentences(
    chunks: Iterable[str],
    *,
    max_chars: int = MAX_CHUNK_CHARS,
    min_chars: int = MIN_CHUNK_CHARS,
) -> Iterator[str]:
    """テキスト片のストリームを、発話に適した長さのまとまりに切って返す。"""
    buffer = ""
    for chunk in chunks:
        buffer += chunk
        while True:
            cut = _find_cut(buffer, max_chars, min_chars)
            if cut is None:
                break
            piece, buffer = buffer[:cut], buffer[cut:]
            piece = piece.strip()
            if piece:
                yield piece
    tail = buffer.strip()
    if tail:
        yield tail


def _find_cut(buffer: str, max_chars: int, min_chars: int) -> int | None:
    # 文末は max_chars の範囲内でだけ探す。遠くの句点まで待つと、その間 TTS が始まらない。
    for i, ch in enumerate(buffer[:max_chars]):
        if ch in SENTENCE_ENDINGS and i + 1 >= min_chars:
            return i + 1
    if len(buffer) >= max_chars:
        # 文末が来ない。読点か空白で切り、それも無ければ諦めて長さで切る。
        for sep in ("、", "，", ", ", " "):
            idx = buffer.rfind(sep, min_chars, max_chars)
            if idx != -1:
                return idx + len(sep)
        return max_chars
    return None
