"""Jarvis 全体で使う例外。"""


class JarvisError(Exception):
    """Jarvis の基底例外。"""


class ConfigError(JarvisError):
    """設定ファイル・環境変数の不備。"""


class EngineUnavailable(JarvisError):
    """エンジンに必要な依存やサービスが揃っていない。

    `jarvis doctor` はこの例外を捕まえて「何を入れれば動くか」を表示する。
    """

    def __init__(self, engine: str, reason: str, remedy: str = "") -> None:
        self.engine = engine
        self.reason = reason
        self.remedy = remedy
        message = f"{engine}: {reason}"
        if remedy:
            message += f"\n  対処: {remedy}"
        super().__init__(message)


class TurnFailed(JarvisError):
    """このターンの応答は得られなかったが、会話は続けられる。

    混雑・一時的な障害・ネットワーク断など。常時待受のループを落とす理由に
    ならないものはこれにして、利用者には声で伝えたうえで待受に戻る。
    """
