"""長期記憶と会話ログ。

記事の「長期記憶／パーソナライズ」の最小実装。ベクトルDB は使わず、
覚えた事実を JSON に置いてシステムプロンプトへ注入する。
件数が増えて効かなくなったら、ここを Qdrant などの RAG に差し替える。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Fact:
    text: str
    created_at: float

    @classmethod
    def new(cls, text: str) -> "Fact":
        return cls(text=text, created_at=time.time())


class Memory:
    """覚えた事実の保管庫と、会話ログの追記。"""

    def __init__(self, path: str | Path, log_path: str | Path | None = None, max_facts: int = 200) -> None:
        self.path = Path(path)
        self.log_path = Path(log_path) if log_path else None
        self.max_facts = max_facts
        self.facts: list[Fact] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # 壊れた記憶ファイルで起動が止まるより、空で立ち上げて動き続ける方がよい。
            return
        self.facts = [Fact(**f) for f in raw.get("facts", []) if isinstance(f, dict) and "text" in f]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"facts": [asdict(f) for f in self.facts]}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def remember(self, text: str) -> Fact:
        text = text.strip()
        if not text:
            raise ValueError("空の内容は覚えられない")
        fact = Fact.new(text)
        self.facts.append(fact)
        del self.facts[: max(0, len(self.facts) - self.max_facts)]
        self._save()
        return fact

    def recall(self, query: str | None = None, limit: int = 20) -> list[Fact]:
        if query:
            needle = query.lower()
            hits = [f for f in self.facts if needle in f.text.lower()]
        else:
            hits = list(self.facts)
        return hits[-limit:]

    def forget(self, text: str) -> int:
        """内容が一致する事実を消し、消した件数を返す。"""
        before = len(self.facts)
        needle = text.strip().lower()
        self.facts = [f for f in self.facts if f.text.strip().lower() != needle]
        removed = before - len(self.facts)
        if removed:
            self._save()
        return removed

    def as_prompt_block(self) -> str:
        """システムプロンプトに差し込む形に整える。空なら空文字。"""
        if not self.facts:
            return ""
        lines = "\n".join(f"- {f.text}" for f in self.facts)
        return f"ユーザーについて覚えていること:\n{lines}"

    def log_turn(self, role: str, text: str) -> None:
        if self.log_path is None:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": time.time(), "role": role, "text": text}
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
