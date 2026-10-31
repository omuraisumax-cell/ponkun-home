"""安全なHubクライアント認証の設計用アダプター。

本モジュールはCredential Managerへ直接アクセスせず、秘密値を返す
providerを注入して利用する。実サービスへの組み込みは別フェーズで行う。
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Callable, Mapping


HUB_CLIENT_TOKEN_TARGET = "PonkunHome/HubClientToken"
TOKEN_HEADER = "X-Ponkun-Client-Token"
HUB_INFERENCE_PATHS = frozenset({"/api/inference", "/api/inference/stream"})


class HubClientAuthError(RuntimeError):
    """認証設定または認証値が利用できない。"""


class HubClientTokenMissing(HubClientAuthError):
    """必須モードでトークンが取得できない。"""


class HubClientTokenMismatch(HubClientAuthError):
    """送信されたトークンが期待値と一致しない。"""


SecretProvider = Callable[[str], str | None]


def load_hub_client_token(
    provider: SecretProvider,
    *,
    required: bool,
) -> str | None:
    """専用ターゲットをメモリ上へ読み込む。

    providerは将来Credential Managerの既存ラッパーを注入する。
    必須モードで未設定なら、認証なしで継続せず例外にする。
    """

    token = provider(HUB_CLIENT_TOKEN_TARGET)
    if token is not None:
        token = token.strip()
    if required and not token:
        raise HubClientTokenMissing("Hub client token is required")
    return token or None


def build_client_headers(
    token: str | None,
    *,
    required: bool,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """認証ヘッダーを構成する。秘密値はログへ出力しない。"""

    headers = dict(base or {})
    if not token:
        if required:
            raise HubClientTokenMissing("Hub client token is required")
        return headers
    headers[TOKEN_HEADER] = token
    return headers


def validate_client_token(
    supplied: str | None,
    expected: str | None,
    *,
    required: bool,
) -> bool:
    """Hub側の認証判定。

    値の比較は定数時間で行い、値そのものを例外やログに含めない。
    """

    expected = expected.strip() if expected else ""
    supplied = supplied or ""
    if not expected:
        if required:
            raise HubClientTokenMissing("Hub client token is required")
        return True
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HubClientTokenMismatch("Hub client token mismatch")
    return True


def build_component_headers(
    provider: SecretProvider,
    *,
    component: str,
    path: str,
    required: bool,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Hub/Mobile/PC Voice共通の認証供給経路をモック可能にする。

    componentは監査用の呼び出し元識別子であり、ログ出力は行わない。
    同期・ストリーミングのHub推論パス以外へ誤って適用しない。
    """

    if path not in HUB_INFERENCE_PATHS:
        raise ValueError("unsupported Hub inference path")
    if not component:
        raise ValueError("component is required")
    token = load_hub_client_token(provider, required=required)
    return build_client_headers(token, required=required, base=base)


@dataclass(frozen=True)
class HubClientAuthPlan:
    """実装フェーズへ渡す認証方針。"""

    required: bool = False
    target: str = HUB_CLIENT_TOKEN_TARGET

