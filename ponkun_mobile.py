import base64
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import secrets
from datetime import datetime, timezone
from pathlib import Path
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from faster_whisper import WhisperModel
from pydantic import BaseModel
from ponkun_tts import synthesize

# ぽんくんの「魂」から必要な最小限だけをインポート
MOBILE_HUB_URL = os.environ.get("PONKUN_MOBILE_HUB_URL", "http://127.0.0.1:8788").strip().rstrip("/")
os.environ["PONKUN_HUB_URL"] = MOBILE_HUB_URL
os.environ["PONKUN_MOBILE_HUB_ONLY"] = "1"

from ponkun_core import (
    DB_PATH,
    MODEL_LUNA,
    ask_ponkun,
    client,
    save_history,
)
from ponkun_web_search import WebSearchError
from ponkun_inference import InferenceUnavailable
from ponkun_openai_vision import OpenAIVisionError, analyze_image

app = FastAPI(title="Ponkun Mobile API", version="1.0")
VISION_BACKEND = os.environ.get("PONKUN_VISION_BACKEND", "disabled").strip().lower()
MAX_VISION_BYTES = 10 * 1024 * 1024
MAX_VISION_PIXELS = 40_000_000
VISION_FORMATS = {"png", "jpeg", "webp"}

CHAT_DIAGNOSTIC_LOG = Path(os.environ.get(
    "PONKUN_MOBILE_DIAGNOSTIC_LOG",
    str(Path(__file__).resolve().parent / "ponkun_hub" / "runtime_logs" / "mobile_chat_errors.log"),
))


def _chat_route_hint(user_text: str) -> str:
    """Return a coarse route label without recording the user text."""
    lowered = user_text.casefold()
    web_terms = ("weather", "news", "latest", "price", "天気", "ニュース", "最新", "現在")
    return "web" if any(term in lowered for term in web_terms) else "qwen"


def _classify_chat_exception(exc: Exception) -> tuple[str, int, str, str]:
    """Map known failures to safe public categories without exposing details."""
    text = str(exc).casefold()
    adapter_code = str(getattr(exc, "code", ""))
    adapter_status = getattr(exc, "http_status", None)
    adapter_stage = str(getattr(exc, "stage", "inference"))
    if adapter_code == "hub_timeout":
        return "qwen_timeout", 504, "Qwenの応答がタイムアウトしました。", adapter_stage
    if adapter_code.startswith("hub_http_"):
        status = int(adapter_status) if isinstance(adapter_status, int) else 502
        if status == 503:
            return "inference_unavailable", 503, "推論サービスが利用できません。", adapter_stage
        return "hub_bad_gateway", 502, "Hubで推論に失敗しました。", adapter_stage
    if adapter_code in {"hub_connection_error", "hub_invalid_response", "hub_empty_response"}:
        return "hub_unavailable", 502, "Hubに接続できません。", adapter_stage
    if adapter_code.startswith("strata_http_"):
        status = int(adapter_status) if isinstance(adapter_status, int) else 502
        if status in {401, 403}:
            return "strata_authentication_failed", 502, "Qwenの認証に失敗しました。", adapter_stage
        if status == 503:
            return "strata_model_not_ready", 503, "モデルが準備できていません。", adapter_stage
        return "hub_bad_gateway", 502, "Hubで推論に失敗しました。", adapter_stage
    if adapter_code in {"strata_timeout"}:
        return "qwen_timeout", 504, "Qwenの応答がタイムアウトしました。", adapter_stage
    if adapter_code in {"strata_invalid_response", "strata_empty_response"}:
        return "inference_invalid_response", 502, "推論応答の形式を確認できませんでした。", adapter_stage
    if isinstance(exc, WebSearchError):
        return "web_search_unavailable", 503, "Web検索に接続できません。", "web_search"
    if "timed out" in text or "timeout" in text or isinstance(exc, TimeoutError):
        return "qwen_timeout", 504, "Qwenの応答がタイムアウトしました。", "inference"
    if "502" in text or "bad gateway" in text:
        return "hub_bad_gateway", 502, "Hubで推論に失敗しました。", "hub.inference"
    if "503" in text or "service unavailable" in text or "queue is full" in text:
        return "inference_unavailable", 503, "推論サービスが利用できません。", "inference"
    if any(term in text for term in ("api key", "authentication", "unauthorized", "invalid api")):
        return "strata_authentication_failed", 502, "Qwenの認証に失敗しました。", "strata.inference"
    if any(term in text for term in ("model", "not ready", "empty content")):
        return "strata_model_not_ready", 503, "モデルが準備できていません。", "strata.inference"
    if any(term in text for term in ("json", "compatible", "invalid response")):
        return "inference_invalid_response", 502, "推論応答の形式を確認できませんでした。", "inference.response"
    if isinstance(exc, InferenceUnavailable) or "hub inference" in text or "shared hub" in text:
        return "hub_unavailable", 502, "Hubに接続できません。", "hub.connect"
    return "internal_error", 500, "サーバー内部エラーが発生しました。", "core.ask_ponkun"


def _write_chat_diagnostic(request_id: str, category: str, exception_name: str, stage: str, status: int, route: str) -> None:
    """Write only a bounded, secret-free diagnostic record."""
    record = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id,
        "category": category,
        "exception_class": exception_name,
        "stage": stage,
        "http_status": status,
        "route": route,
    }
    try:
        CHAT_DIAGNOSTIC_LOG.parent.mkdir(parents=True, exist_ok=True)
        with CHAT_DIAGNOSTIC_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True) + "\n")
    except OSError:
        pass

MOBILE_AUTH_TOKEN = os.environ.get("PONKUN_MOBILE_AUTH_TOKEN", "")
if not MOBILE_AUTH_TOKEN:
    try:
        from ponkun_hub.secure_credentials import read_mobile_token
        MOBILE_AUTH_TOKEN = read_mobile_token() or ""
    except Exception:
        MOBILE_AUTH_TOKEN = ""
MOBILE_AUTH_REQUIRED = os.environ.get("PONKUN_MOBILE_AUTH_REQUIRED", "1").strip() != "0"
MOBILE_COOKIE_SECURE = os.environ.get("PONKUN_MOBILE_COOKIE_SECURE", "0").strip() == "1"
SESSION_COOKIE = "ponkun_mobile_session"
CSRF_COOKIE = "ponkun_mobile_csrf"
_sessions: dict[str, str] = {}
_sessions_lock = threading.RLock()


class AuthRequest(BaseModel):
    token: str


def require_auth(request: Request) -> str | None:
    if not MOBILE_AUTH_REQUIRED:
        return None
    if not MOBILE_AUTH_TOKEN:
        raise HTTPException(status_code=503, detail="Mobile authentication is not configured")
    supplied = request.headers.get("Authorization", "")
    if supplied.startswith("Bearer ") and secrets.compare_digest(supplied[7:], MOBILE_AUTH_TOKEN):
        return "bearer"
    session_id = request.cookies.get(SESSION_COOKIE, "")
    csrf_cookie = request.cookies.get(CSRF_COOKIE, "")
    with _sessions_lock:
        expected_csrf = _sessions.get(session_id)
    if session_id and expected_csrf and secrets.compare_digest(expected_csrf, csrf_cookie):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            csrf_header = request.headers.get("X-CSRF-Token", "")
            if not csrf_header or not secrets.compare_digest(csrf_header, csrf_cookie):
                raise HTTPException(status_code=403, detail="CSRF validation failed")
        return "session"
    raise HTTPException(status_code=401, detail="Mobile authentication required")


@app.post("/auth/login")
def login(auth: AuthRequest):
    if not MOBILE_AUTH_TOKEN:
        raise HTTPException(status_code=503, detail="Mobile authentication is not configured")
    if not secrets.compare_digest(auth.token, MOBILE_AUTH_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    session_id = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(24)
    with _sessions_lock:
        _sessions[session_id] = csrf_token
    response = Response(content=json.dumps({"authenticated": True}), media_type="application/json")
    response.set_cookie(SESSION_COOKIE, session_id, httponly=True, samesite="strict", secure=MOBILE_COOKIE_SECURE, max_age=3600)
    response.set_cookie(CSRF_COOKIE, csrf_token, httponly=False, samesite="strict", secure=MOBILE_COOKIE_SECURE, max_age=3600)
    return response


@app.post("/auth/logout")
def logout(request: Request, _auth=Depends(require_auth)):
    session_id = request.cookies.get(SESSION_COOKIE, "")
    with _sessions_lock:
        _sessions.pop(session_id, None)
    response = Response(content=json.dumps({"authenticated": False}), media_type="application/json")
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(CSRF_COOKIE)
    return response


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page():
    return HTMLResponse(
        """<!doctype html><html lang='ja'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
        <title>Ponkun Mobile ログイン</title><body style='font-family: sans-serif;max-width:32rem;margin:3rem auto;padding:1rem'>
        <h1>Ponkun Mobile</h1><p>ログイン情報は保存されません。Strata APIキーは入力しないでください。</p>
        <form id='login'><label>Mobile認証トークン<br><input id='token' type='password' autocomplete='off' required style='width:100%;padding:.6rem'></label>
        <button style='margin-top:1rem;padding:.6rem 1rem'>ログイン</button><p id='status' role='status'></p></form>
        <script>document.getElementById('login').addEventListener('submit',async(e)=>{e.preventDefault();const s=document.getElementById('status');const t=document.getElementById('token');const r=await fetch('/auth/login',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:t.value})});t.value='';if(r.ok){location.href='/';}else{s.textContent='ログインに失敗しました。';}});</script>
        </body></html>"""
    )

# 静的ファイル配信
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/manifest.json", include_in_schema=False)
def manifest():
    return FileResponse(
        "static/manifest.webmanifest", media_type="application/manifest+json"
    )


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest_webmanifest():
    return FileResponse(
        "static/manifest.webmanifest", media_type="application/manifest+json"
    )


@app.get("/service-worker.js", include_in_schema=False)
def service_worker():
    return FileResponse("static/service-worker.js", media_type="application/javascript")


# =========================
# Mobile TTS (Fish Audio)
# =========================
# =========================
# Whisper 音声認識モデル (Mobile側の耳)
# =========================
WHISPER_MODEL_NAME = os.environ.get("PONKUN_WHISPER_MODEL", "large-v3")
WHISPER_DEVICE = os.environ.get("PONKUN_WHISPER_DEVICE", "cuda")
WHISPER_COMPUTE_TYPE = os.environ.get("PONKUN_WHISPER_COMPUTE_TYPE", "float16")
whisper_model = None
whisper_state = "not_loaded"
whisper_error = None
whisper_lock = threading.Lock()


def get_whisper_model():
    global whisper_model, whisper_state, whisper_error
    if whisper_model is not None:
        return whisper_model
    with whisper_lock:
        if whisper_model is not None:
            return whisper_model
        whisper_state = "loading"
        whisper_error = None
        try:
            whisper_model = WhisperModel(
                WHISPER_MODEL_NAME,
                device=WHISPER_DEVICE,
                compute_type=WHISPER_COMPUTE_TYPE,
            )
            whisper_state = "ready"
            return whisper_model
        except Exception as exc:
            whisper_state = "error"
            whisper_error = f"{type(exc).__name__}: {exc}"
            raise


class ChatRequest(BaseModel):
    text: str
    session_id: str = "mobile-main"


class SpeakRequest(BaseModel):
    text: str


# =========================
# Mobile発話 API (スマホ本体で再生するMP3を生成)
# =========================
@app.post("/speak")
def speak_api(request: SpeakRequest, _auth=Depends(require_auth)):
    text = request.text.strip()
    if not text:
        return Response(status_code=400)
    started = time.perf_counter()
    try:
        print("Mobile Fish音声生成 >", text)
        result = synthesize(text)
        print(f"[ponkun-timing] mobile_fish_tts_ms={result.elapsed_ms} audio_bytes={len(result.audio)} backend={result.backend}")
        return Response(content=result.audio, media_type=result.content_type or "audio/mpeg")
    except Exception as e:
        print("Mobile TTSエラー >", e)
        return Response(status_code=503)


# =========================================================
# Mobile専用の身体機能: カメラ・画像解析 (Vision)
# =========================================================
def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if not data.startswith(b"\xff\xd8"):
        return None
    index = 2
    sof_markers = set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(range(0xC9, 0xCC)) | set(range(0xCD, 0xD0))
    while index + 4 <= len(data):
        while index < len(data) and data[index] != 0xFF:
            index += 1
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            return None
        marker = data[index]
        index += 1
        if marker in {0xD8, 0xD9}:
            continue
        if index + 2 > len(data):
            return None
        length = int.from_bytes(data[index:index + 2], "big")
        if length < 2 or index + length > len(data):
            return None
        if marker in sof_markers and length >= 7:
            height = int.from_bytes(data[index + 3:index + 5], "big")
            width = int.from_bytes(data[index + 5:index + 7], "big")
            return width, height
        index += length
    return None


def validate_vision_image(data: bytes, declared_type: str | None = None) -> tuple[str, int, int]:
    """Validate actual image signatures and dimensions, not only MIME metadata."""
    if not data or len(data) > MAX_VISION_BYTES:
        raise HTTPException(status_code=413, detail="画像サイズが上限を超えています。")
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24 and data[12:16] == b"IHDR":
        kind = "png"
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
    elif data.startswith(b"\xff\xd8"):
        dimensions = _jpeg_dimensions(data)
        if not dimensions:
            raise HTTPException(status_code=400, detail="有効なJPEG画像を確認できません。")
        kind = "jpeg"
        width, height = dimensions
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if len(data) < 30:
            raise HTTPException(status_code=400, detail="有効なWebP画像を確認できません。")
        chunk = data[12:16]
        if chunk == b"VP8X":
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
        elif chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
        elif chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
        else:
            raise HTTPException(status_code=400, detail="対応形式のWebP画像を確認できません。")
        kind = "webp"
    else:
        raise HTTPException(status_code=415, detail="PNG、JPEG、WebP以外の画像は対応していません。")
    if kind not in VISION_FORMATS or width <= 0 or height <= 0 or width * height > MAX_VISION_PIXELS:
        raise HTTPException(status_code=413, detail="画像の画素数が上限を超えています。")
    return kind, width, height


def detect_vision_mode(user_text: str) -> str:
    text = (user_text or "").lower()
    error_words = [
        "エラー",
        "error",
        "exception",
        "traceback",
        "動かない",
        "起動しない",
        "開かない",
        "接続できない",
        "どう直す",
        "直し方",
        "原因",
        "対処",
        "クラッシュ",
        "failed",
        "refused",
        "denied",
        "invalid",
    ]
    ocr_words = [
        "何て書いて",
        "なんて書いて",
        "何が書いて",
        "文字",
        "読んで",
        "読み取って",
        "ocr",
        "型番",
        "品番",
        "翻訳",
        "書類",
        "文章",
        "テキスト",
        "番号",
        "金額",
        "日付",
    ]
    if any(word in text for word in error_words):
        return "error"
    if any(word in text for word in ocr_words):
        return "ocr"
    return "general"


def is_ocr_request(text: str) -> bool:
    if not text:
        return False
    keywords = [
        "何て書いて",
        "なんて書いて",
        "書いてある",
        "文字",
        "読んで",
        "OCR",
        "エラー",
        "画面",
        "書類",
        "要約",
        "翻訳",
        "英語",
        "何が書いてある",
        "読める",
        "文章",
        "テキスト",
    ]
    return any(k in text for k in keywords)


def build_vision_instructions(user_text: str) -> str:
    mode = detect_vision_mode(user_text)

    if mode == "error":
        return """あなたはユーザー専用AIアシスタント「ぽんくん」です。
この画像について、PC・スマホ・アプリなどのエラー解析をしてください。
画像内のエラー文、エラーコード、例外名など重要な文字を優先して確認してください。
回答形式:
【エラー内容】
何が起きているか簡潔に説明
【原因】
考えられる原因を整理
【直し方】
1. 最初に試すこと
2. 次に試すこと
3. それでも直らない場合
必要なら【読めた重要文字】を追加してください。
画像から確認できないことは断定しないでください。
日本語で簡潔に回答してください。"""

    if mode == "ocr":
        return """あなたはユーザー専用AIアシスタント「ぽんくん」です。
ユーザーは画像内の文字情報を知りたがっています。
OCRを優先して、重要な文字を正確に読み取ってください。
回答形式:
【読めた文字】
・重要な文字を1項目1行で記載
・型番、番号、金額、日付、URLなどを優先
【要点】
何が書かれているか簡潔に説明
【説明】
必要な場合のみ補足
読めない箇所は推測せず「一部判読できません」としてください。
Markdownの太字記号は使用しないでください。"""

    return """あなたはユーザー専用AIアシスタント「ぽんくん」です。
画像を自然に理解して、ユーザーの質問に直接答えてください。
重要:
・ユーザーが文字の読み取りを求めていない場合、OCRを最優先にしないでください。
・画像内に文字があっても、質問への回答に重要でなければ読み上げる必要はありません。
・物、人形、食べ物、動物、風景、機械などについて尋ねられた場合は、まず対象物について答えてください。
・「読めた文字」という見出しを勝手に付けないでください。
・「文字は確認できません」のような不要な報告もしないでください。
・画像から確認できないことを断定しないでください。
・日本語で自然かつ簡潔に回答してください。"""


def build_vision_route(user_text: str) -> str:
    mode = detect_vision_mode(user_text)
    if mode == "error":
        return "VISION-GPT-ERROR"
    if mode == "ocr":
        return "VISION-GPT-OCR"
    return "VISION-GPT"


def make_vision_speech_text(answer: str) -> str:
    lines = [line.strip() for line in answer.splitlines() if line.strip()]
    important = []
    for line in lines:
        if (
            line.startswith("【要点】")
            or line.startswith("【説明】")
            or line.startswith("- ")
        ):
            important.append(line)
    short_text = " ".join(important[:5])
    return short_text if short_text else answer[:140]


def ask_cloud_vision(
    user_text: str, image_bytes: bytes, mime_type: str = "image/jpeg", *, external_consent: bool = False
):
    if VISION_BACKEND != "openai":
        raise OpenAIVisionError("vision_disabled", 503)
    if not external_consent:
        raise OpenAIVisionError("vision_consent_required", 403)
    return analyze_image(user_text, image_bytes, mime_type)[0]

    # Legacy implementation retained below for rollback/reference only.
    # The dedicated adapter above is the only reachable Vision path.
    base64_image = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:{mime_type};base64,{base64_image}"

    prompt = user_text.strip() if user_text else ""
    if not prompt:
        prompt = "この画像の文字を優先して読み取り、そのあと内容を説明してください。"
    elif is_ocr_request(prompt):
        prompt = f"""次の質問に答えてください。
まず画像内の文字を優先して読み取り、
その文字を踏まえて要点を整理して答えてください。

質問: {prompt}""".strip()

    response = client.responses.create(
        model=MODEL_LUNA,
        instructions=build_vision_instructions(prompt),
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": data_url},
                ],
            }
        ],
    )
    return response.output_text.strip()


# =========================
# UI 配信
# =========================
@app.get("/", response_class=HTMLResponse)
def home():
    return r"""<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <meta name="theme-color" content="#111827">
    <title>ぽんくん AI</title>
    <link rel="icon" href="/static/favicon.ico" type="image/x-icon">
    <link rel="shortcut icon" href="/static/favicon.ico" type="image/x-icon">
    <link rel="icon" type="image/png" sizes="192x192" href="/static/icon-192.png">
    <link rel="apple-touch-icon" href="/static/icon-192.png">
    <link rel="manifest" href="/manifest.webmanifest">
    <style>
        :root {
            --bg-color: #0b1220;
            --bg-elevated: #111b2c;
            --sidebar-bg: #121b2a;
            --panel: rgba(31, 43, 63, 0.82);
            --panel-strong: #202c40;
            --user-bubble: #24334b;
            --text-main: #f4f7fb;
            --text-sub: #a9b5c8;
            --text-muted: #71809a;
            --accent: #4f8cff;
            --accent-2: #8b5cf6;
            --accent-glow: rgba(79, 140, 255, 0.24);
            --success: #35d07f;
            --border: rgba(154, 172, 201, 0.16);
            --radius-sm: 12px;
            --radius-md: 18px;
            --radius-lg: 26px;
            --shadow-soft: 0 16px 44px rgba(0, 0, 0, 0.24);
            --safe-bottom: env(safe-area-inset-bottom, 0px);
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            -webkit-tap-highlight-color: transparent;
        }

        html, body {
            width: 100%;
            max-width: 100%;
            overflow-x: hidden;
        }

        body {
            font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans JP", sans-serif;
            background: radial-gradient(circle at 70% 4%, rgba(55, 114, 220, 0.18), transparent 30%), var(--bg-color);
            color: var(--text-main);
            display: flex;
            height: 100dvh;
            overflow: hidden;
        }

        #sidebar {
            width: 304px;
            flex: 0 0 304px;
            background: linear-gradient(180deg, #151f30 0%, var(--sidebar-bg) 100%);
            border-right: 1px solid var(--border);
            display: flex;
            flex-direction: column;
            padding: 22px 18px calc(18px + var(--safe-bottom));
            transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            z-index: 100;
        }

        .sidebar-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 20px;
        }

        .brand-title {
            font-size: 1.08rem;
            font-weight: 760;
            letter-spacing: 0.5px;
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .brand-mark {
            width: 42px;
            height: 42px;
            border-radius: 14px;
            display: grid;
            place-items: center;
            background: linear-gradient(145deg, #53c9ff, #4f7cff 58%, #9b64f6);
            box-shadow: 0 10px 28px rgba(63, 125, 255, 0.35);
            font-size: 1.25rem;
        }

        .brand-avatar,
        .welcome-avatar {
            width: 100%;
            height: 100%;
            display: block;
            object-fit: cover;
            border-radius: inherit;
        }

        .brand-copy { display: flex; flex-direction: column; gap: 2px; }
        .brand-line { display: flex; align-items: center; gap: 8px; }
        .brand-subtitle { color: var(--text-muted); font-size: 0.7rem; font-weight: 500; }

        .brand-badge {
            font-size: 0.7rem;
            background: linear-gradient(135deg, var(--accent), #6c72ff);
            color: white;
            padding: 2px 6px;
            border-radius: 999px;
            font-weight: bold;
        }

        .new-chat-btn {
            background: linear-gradient(135deg, rgba(50, 164, 255, 0.95), rgba(77, 93, 230, 0.95));
            border: 1px solid rgba(126, 205, 255, 0.48);
            color: var(--text-main);
            padding: 13px 15px;
            border-radius: 15px;
            cursor: pointer;
            width: 100%;
            text-align: left;
            margin-bottom: 18px;
            font-size: 0.92rem;
            font-weight: 650;
            box-shadow: 0 10px 25px rgba(46, 108, 232, 0.26);
            transition: all 0.2s ease;
        }

        .new-chat-btn:hover {
            transform: translateY(-1px);
            filter: brightness(1.08);
        }

        .section-label {
            font-size: 0.75rem;
            color: var(--text-sub);
            letter-spacing: 0.04em;
            margin: 12px 0 9px 4px;
        }

        .history-list {
            flex: 1;
            overflow-y: auto;
            list-style: none;
            display: flex;
            flex-direction: column;
            gap: 7px;
        }

        .history-item {
            position: relative;
            padding: 11px 34px 11px 38px;
            border-radius: 13px;
            font-size: 0.86rem;
            color: #c6d0df;
            cursor: pointer;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .history-item::before {
            content: "";
            position: absolute;
            left: 13px;
            top: 50%;
            width: 13px;
            height: 10px;
            transform: translateY(-50%);
            border: 1.5px solid var(--text-muted);
            border-radius: 5px;
        }

        .history-item::after {
            content: "›";
            position: absolute;
            right: 13px;
            color: var(--text-muted);
        }

        .history-item:hover {
            background: rgba(255, 255, 255, 0.055);
            color: var(--text-main);
        }

        .status-box {
            font-size: 0.78rem;
            color: #c8d3e3;
            background: rgba(255, 255, 255, 0.045);
            border: 1px solid var(--border);
            padding: 12px 13px;
            border-radius: 15px;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .status-dot {
            width: 9px;
            height: 9px;
            background: var(--success);
            border-radius: 50%;
            box-shadow: 0 0 0 5px rgba(53, 208, 127, 0.11), 0 0 10px rgba(53, 208, 127, 0.65);
        }

        .status-copy { display: flex; flex-direction: column; gap: 2px; }
        .status-copy small { color: var(--text-muted); font-size: 0.67rem; }

        #main-wrapper {
            flex: 1;
            width: 100%;
            max-width: 100%;
            display: flex;
            flex-direction: column;
            position: relative;
            background: radial-gradient(circle at 50% 0%, rgba(43, 102, 190, 0.16), transparent 34%), var(--bg-color);
            min-width: 0;
        }

        .app-bar {
            display: flex;
            align-items: center;
            min-height: 64px;
            padding: 10px 18px;
            border-bottom: 1px solid var(--border);
            justify-content: space-between;
            gap: 12px;
            background: rgba(11, 18, 32, 0.76);
            backdrop-filter: blur(18px);
            -webkit-backdrop-filter: blur(18px);
            z-index: 10;
        }

        .menu-trigger {
            background: rgba(255, 255, 255, 0.055);
            border: 1px solid var(--border);
            color: var(--text-main);
            font-size: 1.12rem;
            cursor: pointer;
            width: 40px;
            height: 40px;
            border-radius: 13px;
        }

        .current-model-info {
            font-size: 1rem;
            color: var(--text-main);
            font-weight: 740;
            letter-spacing: 0.01em;
        }

        .app-brand { display: flex; align-items: center; gap: 9px; }
        .app-brand .brand-badge { font-size: 0.62rem; }
        .header-status { display: flex; align-items: center; gap: 7px; color: var(--text-sub); font-size: 0.72rem; }
        .header-status .status-dot { width: 7px; height: 7px; box-shadow: 0 0 8px rgba(53, 208, 127, 0.65); }

        #chat-scroller {
            flex: 1;
            overflow-y: auto;
            padding: 24px 18px 12px;
            display: flex;
            flex-direction: column;
            gap: 18px;
            scroll-padding-bottom: 130px;
        }

        .welcome-panel {
            width: min(100%, 720px);
            margin: auto;
            padding: 4vh 0 12px;
            text-align: center;
        }

        .welcome-orb {
            width: 92px;
            height: 92px;
            margin: 0 auto 20px;
            display: grid;
            place-items: center;
            border-radius: 30px;
            background: radial-gradient(circle at 38% 30%, #dff9ff 0 12%, #6edcff 28%, #4e72ff 62%, #7555d9 100%);
            box-shadow: 0 20px 58px rgba(64, 127, 255, 0.38), inset 0 1px 10px rgba(255, 255, 255, 0.55);
            font-size: 2.8rem;
            transform: rotate(-3deg);
            overflow: hidden;
        }

        .welcome-panel h1 { font-size: clamp(1.65rem, 6vw, 2.3rem); letter-spacing: -0.035em; margin-bottom: 7px; }
        .welcome-panel > p { color: #a9bce0; font-size: clamp(0.98rem, 3.8vw, 1.12rem); line-height: 1.55; }
        .suggestion-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin-top: 28px; text-align: left; }
        .suggestion-card {
            border: 1px solid var(--border);
            border-radius: var(--radius-md);
            padding: 15px;
            min-height: 86px;
            color: var(--text-main);
            background: linear-gradient(145deg, rgba(39, 55, 82, 0.9), rgba(27, 39, 59, 0.88));
            display: flex;
            align-items: center;
            gap: 12px;
            cursor: pointer;
            text-align: left;
            font: inherit;
            line-height: 1.45;
            box-shadow: 0 12px 28px rgba(0, 0, 0, 0.15);
        }
        .suggestion-card:active { transform: scale(0.985); }
        .suggestion-icon { flex: 0 0 auto; width: 38px; height: 38px; border-radius: 13px; display: grid; place-items: center; background: rgba(79, 140, 255, 0.15); color: #83b8ff; font-size: 1.15rem; }
        .suggestion-card:nth-child(2) .suggestion-icon { color: #c99aff; background: rgba(139, 92, 246, 0.15); }
        .suggestion-card:nth-child(3) .suggestion-icon { color: #72dc8f; background: rgba(53, 208, 127, 0.14); }
        .suggestion-card:nth-child(4) .suggestion-icon { color: #ffbc64; background: rgba(245, 158, 11, 0.14); }

        .message-row {
            display: flex;
            width: 100%;
            max-width: 760px;
            margin: 0 auto;
            flex-direction: column;
        }

        .message-row.user {
            align-items: flex-end;
        }

        .message-row.ponkun {
            align-items: flex-start;
        }

        .bubble {
            padding: 12px 16px;
            border-radius: 18px;
            font-size: 0.95rem;
            line-height: 1.6;
            max-width: 85%;
            word-break: break-word;
            overflow-wrap: anywhere;
            white-space: pre-wrap;
        }

        .user .bubble {
            background: linear-gradient(145deg, #263750, #223047);
            border: 1px solid var(--border);
            border-bottom-right-radius: 4px;
        }

        .ponkun .bubble {
            background: rgba(28, 42, 63, 0.72);
            border: 1px solid var(--border);
            border-bottom-left-radius: 6px;
        }

        .meta-line {
            font-size: 0.72rem;
            color: var(--text-sub);
            margin-top: 4px;
            display: flex;
            align-items: center;
            flex-wrap: wrap;
            gap: 8px;
            max-width: 100%;
            min-width: 0;
        }

        .route-pill {
            background: var(--user-bubble);
            padding: 2px 6px;
            border-radius: 4px;
            border: 1px solid var(--border);
            font-size: 0.68rem;
            max-width: 100%;
            overflow-wrap: anywhere;
        }

        .source-link {
            display: inline-block;
            max-width: min(260px, 100%);
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: normal;
            overflow-wrap: anywhere;
            word-break: break-word;
            color: var(--accent);
            text-decoration: none;
        }

        .source-link:hover {
            text-decoration: underline;
        }

        .input-container {
            padding: 10px 16px calc(14px + var(--safe-bottom));
            max-width: 760px;
            width: 100%;
            margin: 0 auto;
            background: linear-gradient(180deg, rgba(11, 18, 32, 0), rgba(11, 18, 32, 0.96) 24%);
        }

        .input-card {
            display: flex;
            align-items: flex-end;
            background: rgba(32, 44, 64, 0.94);
            border: 1px solid var(--border);
            border-radius: 25px;
            padding: 7px 8px 7px 15px;
            box-shadow: var(--shadow-soft), inset 0 1px 0 rgba(255, 255, 255, 0.035);
            backdrop-filter: blur(18px);
            transition: border-color 0.2s ease, box-shadow 0.2s ease;
        }

        .input-card:focus-within {
            border-color: var(--accent);
            box-shadow: 0 10px 35px var(--accent-glow);
        }

        #message {
           flex: 1;
           background: transparent;
           border: none;
           color: var(--text-main);
           font-size: 0.98rem;
           outline: none;
           padding: 10px 0;
           resize: none;
           overflow-y: hidden;
           scrollbar-width: none;
           -ms-overflow-style: none;
           min-height: 42px;
           max-height: 160px;
           line-height: 1.5;
           font-family: inherit;
        }

        #message::-webkit-scrollbar { display: none; }

        #message::placeholder {
            color: #7f8ca2;
        }

        .btn-icon {
            background: rgba(255, 255, 255, 0.07);
            border: 1px solid rgba(255, 255, 255, 0.025);
            color: var(--text-sub);
            width: 40px;
            height: 40px;
            border-radius: 50%;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.02rem;
            flex: 0 0 auto;
            transition: all 0.2s ease;
        }

        .btn-icon:hover {
            color: var(--text-main);
            background: var(--user-bubble);
        }

        .btn-submit {
            color: white;
            background: linear-gradient(135deg, #4f9cff, #5268f5);
            box-shadow: 0 8px 18px rgba(64, 116, 255, 0.34);
        }

        .thinking-row .bubble {
            display: inline-flex;
            align-items: center;
            gap: 10px;
            color: var(--text-sub);
        }

        .thinking-spinner {
            width: 18px;
            height: 18px;
            flex: 0 0 18px;
            border: 2px solid rgba(169, 181, 200, 0.28);
            border-top-color: var(--accent);
            border-radius: 50%;
            animation: thinking-spin 0.8s linear infinite;
        }

        @keyframes thinking-spin {
            to { transform: rotate(360deg); }
        }

        .image-preview-card { display: none; align-items: center; gap: 10px; margin-bottom: 8px; background: var(--panel-strong); padding: 9px 11px; border-radius: 15px; border: 1px solid var(--border); box-shadow: var(--shadow-soft); }
        .image-preview-card img { width: 46px; height: 46px; border-radius: 10px; object-fit: cover; }
        .image-preview-name { font-size: 0.8rem; color: var(--text-sub); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .vision-destination { font-size: 0.69rem; color: var(--text-muted); max-width: 180px; overflow-wrap: anywhere; }
        .preview-cancel { background: rgba(255, 92, 117, 0.12); border: none; color: #ff8094; cursor: pointer; width: 34px; height: 34px; border-radius: 50%; font-size: 0.95rem; }

        .appearance-button { border: 1px solid var(--border); background: rgba(255, 255, 255, 0.055); color: var(--text-sub); border-radius: 12px; min-height: 38px; padding: 0 12px; cursor: pointer; font: inherit; font-size: 0.78rem; }
        .appearance-modal { position: fixed; inset: 0; z-index: 200; display: none; place-items: center; padding: 18px; background: rgba(3, 8, 18, 0.72); backdrop-filter: blur(12px); }
        .appearance-modal.open { display: grid; }
        .appearance-dialog { width: min(100%, 440px); max-height: min(88vh, 680px); overflow-y: auto; border: 1px solid var(--border); border-radius: 24px; padding: 20px; background: var(--panel-strong); box-shadow: 0 24px 70px rgba(0, 0, 0, 0.45); }
        .appearance-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
        .appearance-heading h2 { font-size: 1.08rem; }
        .appearance-close { border: 0; background: rgba(255,255,255,0.07); color: var(--text-main); width: 36px; height: 36px; border-radius: 50%; cursor: pointer; }
        .avatar-preview { width: 104px; height: 104px; object-fit: cover; display: block; margin: 18px auto; border-radius: 30px; box-shadow: 0 16px 42px var(--accent-glow); }
        .preset-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
        .preset-button { border: 2px solid transparent; background: rgba(255,255,255,0.05); border-radius: 16px; padding: 7px; cursor: pointer; }
        .preset-button.selected { border-color: var(--accent); }
        .preset-button img { width: 100%; aspect-ratio: 1; object-fit: cover; border-radius: 11px; display: block; }
        .appearance-actions { display: flex; flex-wrap: wrap; gap: 9px; margin-top: 16px; }
        .appearance-action { flex: 1 1 120px; min-height: 42px; border-radius: 13px; border: 1px solid var(--border); background: rgba(255,255,255,0.07); color: var(--text-main); cursor: pointer; font: inherit; }
        .appearance-help { color: var(--text-muted); font-size: 0.75rem; line-height: 1.5; margin-top: 12px; }

        @media (max-width: 768px) {
            body {
                display: block;
                width: 100vw;
                max-width: 100vw;
                overflow: hidden;
            }

            #main-wrapper {
                width: 100vw;
                max-width: 100vw;
                height: 100dvh;
                min-width: 0;
                overflow: hidden;
            }

            .message-row {
                max-width: 100%;
                min-width: 0;
            }

            .bubble {
                max-width: 100%;
            }

            .source-link {
                max-width: 100%;
            }

            #sidebar {
                position: fixed;
                top: 0;
                bottom: 0;
                left: 0;
                transform: translateX(-100%);
                width: min(88vw, 330px);
                max-width: calc(100vw - 44px);
                height: 100dvh;
                visibility: hidden;
                pointer-events: none;
                box-shadow: 18px 0 48px rgba(0, 0, 0, 0.42);
            }

            #sidebar.open {
                transform: translateX(0);
                visibility: visible;
                pointer-events: auto;
            }

            .sidebar-backdrop {
                position: fixed;
                inset: 0;
                background: rgba(2, 7, 16, 0.68);
                backdrop-filter: blur(3px);
                z-index: 90;
                display: block;
                opacity: 0;
                visibility: hidden;
                pointer-events: none;
                transition: opacity 0.25s ease, visibility 0.25s ease;
            }

            .sidebar-backdrop.active {
                opacity: 1;
                visibility: visible;
                pointer-events: auto;
            }

            .app-bar { min-height: 58px; padding: 8px 12px; }
            .header-status span:last-child { display: none; }
            #chat-scroller { width: 100%; min-width: 0; overflow-x: hidden; padding: 16px 14px 8px; }
            .welcome-panel { padding-top: min(7vh, 54px); }
            .suggestion-grid { width: 100%; min-width: 0; }
            .suggestion-card { min-width: 0; }
            .input-container { width: 100%; max-width: 100vw; min-width: 0; padding-left: 10px; padding-right: 10px; }
            .input-card { width: 100%; min-width: 0; padding-left: 13px; gap: 3px; }
            #message { min-width: 0; width: 100%; }
            .btn-icon { width: 39px; height: 39px; }
            .vision-destination { display: none; }
        }

        @media (max-width: 390px) {
            .suggestion-grid { grid-template-columns: 1fr; }
            .suggestion-card { min-height: 68px; }
            .welcome-orb { width: 78px; height: 78px; font-size: 2.25rem; margin-bottom: 16px; }
            .btn-icon { width: 37px; height: 37px; }
        }
    </style>
</head>
<body>

<div id="backdrop" class="sidebar-backdrop" onclick="closeSidebar()" aria-hidden="true"></div>

<aside id="sidebar">
    <div class="sidebar-header">
        <div class="brand-title">
            <span class="brand-mark" aria-hidden="true"><img id="brandAvatar" class="brand-avatar" alt=""></span>
            <span class="brand-copy">
                <span class="brand-line">ぽんくん <span class="brand-badge">PRO</span></span>
                <span class="brand-subtitle">あなたの毎日に、AIのやさしい相棒</span>
            </span>
        </div>
        <button class="appearance-button" type="button" onclick="openAppearanceModal()" aria-label="アバター変更">見た目設定</button>
    </div>

    <button class="new-chat-btn" onclick="clearChat()">＋ 新しいチャット</button>

    <div class="section-label">最近のチャット</div>
    <ul id="historyList" class="history-list">
        <li class="history-item">おかえりなさい</li>
    </ul>

    <div class="status-box">
        <span class="status-dot"></span>
        <span class="status-copy"><strong>Ponkun Core Online</strong><small>いつでもお話しできます</small></span>
    </div>
</aside>

<main id="main-wrapper">
    <header class="app-bar">
        <button id="menuTrigger" class="menu-trigger" type="button" onclick="toggleSidebar()" aria-label="メニューを開く" aria-controls="sidebar" aria-expanded="false">☰</button>
        <div class="app-brand"><div class="current-model-info">ぽんくん</div><span class="brand-badge">PRO</span></div>
        <div class="header-status"><span class="status-dot"></span><span>オンライン</span></div>
    </header>

    <div id="chat-scroller">
        <section id="welcomePanel" class="welcome-panel" aria-labelledby="welcomeTitle">
            <div class="welcome-orb" aria-hidden="true"><img id="welcomeAvatar" class="welcome-avatar" alt=""></div>
            <h1 id="welcomeTitle">おかえりなさい。</h1>
            <p>今日は、何を一緒に進めましょう？</p>
            <div class="suggestion-grid">
                <button class="suggestion-card" type="button" onclick="useSuggestion('調べものを手伝って')"><span class="suggestion-icon">⌕</span><span>調べものを<br>手伝ってほしい</span></button>
                <button class="suggestion-card" type="button" onclick="useSuggestion('文章を書くのを手伝って')"><span class="suggestion-icon">✎</span><span>文章を書くのを<br>手伝ってほしい</span></button>
                <button class="suggestion-card" type="button" onclick="document.getElementById('imageFile').click()"><span class="suggestion-icon">▧</span><span>画像について<br>相談したい</span></button>
                <button class="suggestion-card" type="button" onclick="useSuggestion('プログラミングについて質問したい')"><span class="suggestion-icon">&lt;/&gt;</span><span>プログラミングを<br>相談したい</span></button>
            </div>
        </section>
    </div>

    <footer class="input-container">
        <div id="imagePreviewContainer" class="image-preview-card">
            <img id="imagePreview" src="" alt="選択した画像のプレビュー">
            <span id="imageName" class="image-preview-name"></span>
            <span id="visionDestination" class="vision-destination">送信先：OpenAI（外部API）・送信時に確認</span>
            <button class="preview-cancel" type="button" onclick="cancelImage()" aria-label="画像を取り消す">✕</button>
        </div>

        <div class="input-card">
            <textarea id="message" aria-label="メッセージ" rows="1"></textarea>
            <button class="btn-icon" type="button" onclick="document.getElementById('cameraFile').click()" title="カメラで撮影" aria-label="カメラで撮影">⌾</button>
            <button class="btn-icon" type="button" onclick="document.getElementById('voiceFile').click()" title="音声入力" aria-label="音声入力">♪</button>
            <button class="btn-icon btn-submit" type="button" onclick="sendMessage()" title="送信" aria-label="送信">➤</button>
        </div>

        <input id="cameraFile" type="file" accept="image/*" capture="environment" style="display:none">
        <input id="voiceFile" type="file" accept="audio/*" capture style="display:none">
    </footer>
</main>

<div id="appearanceModal" class="appearance-modal" role="dialog" aria-modal="true" aria-labelledby="appearanceTitle" onclick="closeAppearanceModal(event)">
    <div class="appearance-dialog">
        <div class="appearance-heading">
            <h2 id="appearanceTitle">アバター変更</h2>
            <button class="appearance-close" type="button" onclick="closeAppearanceModal()" aria-label="閉じる">✕</button>
        </div>
        <img id="avatarPreview" class="avatar-preview" alt="選択中のアバター">
        <div id="avatarPresets" class="preset-grid" aria-label="プリセット画像"></div>
        <div class="appearance-actions">
            <button class="appearance-action" type="button" onclick="document.getElementById('avatarFile').click()">画像を選ぶ</button>
            <button class="appearance-action" type="button" onclick="resetAvatar()">既定に戻す</button>
        </div>
        <input id="avatarFile" type="file" accept="image/png,image/jpeg,image/webp" hidden>
        <p class="appearance-help">画像はこの端末のブラウザ内に保存され、サーバーへ送信されません。</p>
    </div>
</div>

<script>
    const AVATAR_STORAGE_KEY = "ponkun_mobile_avatar_v1";
    const avatarPresets = [
        { id: "ponkun", colors: ["#6edcff", "#4e72ff"], face: "ぽ" },
        { id: "moon", colors: ["#9f7aea", "#4f46e5"], face: "☾" },
        { id: "spark", colors: ["#45d6b5", "#2789d8"], face: "✦" }
    ];

    function avatarSvgData(preset) {
        const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop stop-color="${preset.colors[0]}"/><stop offset="1" stop-color="${preset.colors[1]}"/></linearGradient></defs><rect width="128" height="128" rx="32" fill="url(#g)"/><circle cx="64" cy="64" r="43" fill="#081426" fill-opacity=".88"/><text x="64" y="78" text-anchor="middle" font-size="48" font-family="sans-serif" font-weight="700" fill="white">${preset.face}</text></svg>`;
        return "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
    }

    const DEFAULT_AVATAR = avatarSvgData(avatarPresets[0]);

    function getStoredAvatar() {
        try { return localStorage.getItem(AVATAR_STORAGE_KEY) || DEFAULT_AVATAR; }
        catch (_error) { return DEFAULT_AVATAR; }
    }

    function applyAvatar(imageUrl) {
        const safeUrl = imageUrl || DEFAULT_AVATAR;
        for (const id of ["brandAvatar", "welcomeAvatar", "avatarPreview"]) {
            const image = document.getElementById(id);
            if (image) image.src = safeUrl;
        }
        document.querySelectorAll(".preset-button").forEach(button => {
            button.classList.toggle("selected", button.dataset.avatar === safeUrl);
        });
    }

    function saveAvatar(imageUrl) {
        try { localStorage.setItem(AVATAR_STORAGE_KEY, imageUrl); }
        catch (_error) {
            window.alert("画像を保存できませんでした。より小さい画像を選んでください。");
            return;
        }
        applyAvatar(imageUrl);
    }

    function renderAvatarPresets() {
        const container = document.getElementById("avatarPresets");
        for (const preset of avatarPresets) {
            const imageUrl = avatarSvgData(preset);
            const button = document.createElement("button");
            button.type = "button";
            button.className = "preset-button";
            button.dataset.avatar = imageUrl;
            button.setAttribute("aria-label", `プリセット ${preset.id}`);
            const image = document.createElement("img");
            image.src = imageUrl;
            image.alt = "";
            button.appendChild(image);
            button.addEventListener("click", () => saveAvatar(imageUrl));
            container.appendChild(button);
        }
    }

    function openAppearanceModal() {
        applyAvatar(getStoredAvatar());
        document.getElementById("appearanceModal").classList.add("open");
        document.body.style.overflow = "hidden";
    }

    function closeAppearanceModal(event) {
        if (event && event.target !== event.currentTarget) return;
        document.getElementById("appearanceModal").classList.remove("open");
        document.body.style.overflow = "";
    }

    function resetAvatar() {
        try { localStorage.removeItem(AVATAR_STORAGE_KEY); } catch (_error) {}
        applyAvatar(DEFAULT_AVATAR);
    }

    renderAvatarPresets();
    applyAvatar(getStoredAvatar());
    document.getElementById("avatarFile").addEventListener("change", function() {
        const file = this.files && this.files[0];
        if (!file) return;
        if (!/^image\/(png|jpeg|webp)$/.test(file.type) || file.size > 2 * 1024 * 1024) {
            window.alert("PNG、JPEG、WebP形式で2MB以下の画像を選んでください。");
            this.value = "";
            return;
        }
        const reader = new FileReader();
        reader.onload = () => saveAvatar(String(reader.result || ""));
        reader.readAsDataURL(file);
        this.value = "";
    });

let selectedImageFile = null; const savedImageInput = document.createElement("input"); savedImageInput.type = "file"; savedImageInput.accept = "image/png,image/jpeg,image/webp"; savedImageInput.style.display = "none"; savedImageInput.id = "imageFile"; document.body.appendChild(savedImageInput); const savedImageButton = document.createElement("button"); savedImageButton.type = "button"; savedImageButton.className = "btn-icon"; savedImageButton.title = "保存済み画像を選択"; savedImageButton.setAttribute("aria-label", "保存済み画像を選択"); savedImageButton.textContent = "▧"; savedImageButton.onclick = () => savedImageInput.click(); document.querySelector(".input-card").insertBefore(savedImageButton, document.querySelector(".btn-submit")); function selectSavedImage(file) { if (!file) return; selectedImageFile = file; const reader = new FileReader(); reader.onload = function(e) { document.getElementById("imagePreview").src = e.target.result; document.getElementById("imageName").textContent = file.name || "保存済み画像"; document.getElementById("imagePreviewContainer").style.display = "flex"; }; reader.readAsDataURL(file); } savedImageInput.addEventListener("change", function() { selectSavedImage(this.files[0]); });
    let mobileAuthTokenInMemory = null;

    async function authenticatedFetch(url, options = {}) {
        const requestOptions = { ...options, credentials: "same-origin" };
        requestOptions.headers = new Headers(options.headers || {});
        const csrf = document.cookie.split(";").map(v => v.trim()).find(v => v.startsWith("ponkun_mobile_csrf="));
        if (csrf && ["POST", "PUT", "PATCH", "DELETE"].includes((requestOptions.method || "GET").toUpperCase())) {
            requestOptions.headers.set("X-CSRF-Token", decodeURIComponent(csrf.split("=").slice(1).join("=")));
        }
        let response = await fetch(url, requestOptions);
        if (response.status !== 401 || mobileAuthTokenInMemory !== null) return response;
        const token = window.prompt("Ponkun Mobileの認証トークンを入力してください（保存されません）");
        if (!token) return response;
        mobileAuthTokenInMemory = token;
        const loginResponse = await fetch("/auth/login", {
            method: "POST", credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ token })
        });
        mobileAuthTokenInMemory = null;
        if (!loginResponse.ok) return response;
        const retryOptions = { ...options, credentials: "same-origin", headers: new Headers(options.headers || {}) };
        const retryCsrf = document.cookie.split(";").map(v => v.trim()).find(v => v.startsWith("ponkun_mobile_csrf="));
        if (retryCsrf && ["POST", "PUT", "PATCH", "DELETE"].includes((retryOptions.method || "GET").toUpperCase())) {
            retryOptions.headers.set("X-CSRF-Token", decodeURIComponent(retryCsrf.split("=").slice(1).join("=")));
        }
        return fetch(url, retryOptions);
    }
    let currentSessionId = "mobile-main";

    function setSidebarOpen(open) {
        const sidebar = document.getElementById("sidebar");
        const backdrop = document.getElementById("backdrop");
        const menuTrigger = document.getElementById("menuTrigger");
        const shouldOpen = Boolean(open) && window.matchMedia("(max-width: 768px)").matches;
        sidebar.classList.toggle("open", shouldOpen);
        backdrop.classList.toggle("active", shouldOpen);
        backdrop.setAttribute("aria-hidden", String(!shouldOpen));
        menuTrigger.setAttribute("aria-expanded", String(shouldOpen));
    }

    function toggleSidebar() {
        const sidebar = document.getElementById("sidebar");
        setSidebarOpen(!sidebar.classList.contains("open"));
    }

    function closeSidebar() {
        setSidebarOpen(false);
    }

    function prepareSources(sources) {
        const candidates = [];
        const seenUrls = new Set();
        for (const source of Array.isArray(sources) ? sources : []) {
            const original = String(source && source.url || "").trim();
            if (!original) continue;
            let parsed;
            try {
                parsed = new URL(original);
                if (parsed.protocol !== "https:" || !parsed.hostname || parsed.username || parsed.password) continue;
            } catch (_err) {
                continue;
            }
            const displayUrl = new URL(parsed.href);
            for (const key of Array.from(displayUrl.searchParams.keys())) {
                if (key.toLowerCase().startsWith("utm_") || ["gclid", "fbclid", "msclkid"].includes(key.toLowerCase())) {
                    displayUrl.searchParams.delete(key);
                }
            }
            const domain = parsed.hostname.toLowerCase().replace(/^www\./, "");
            const normalized = displayUrl.href;
            if (seenUrls.has(normalized)) continue;
            seenUrls.add(normalized);
            const title = String(source && source.title || "").replace(/\s+/g, " ").trim();
            const label = (title ? title + " · " : "") + domain;
            candidates.push({ original, domain, normalized, label: label.length > 72 ? label.slice(0, 69) + "…" : label });
        }
        const selected = [];
        const selectedDomains = new Set();
        for (const candidate of candidates) {
            if (selectedDomains.has(candidate.domain)) continue;
            selected.push(candidate);
            selectedDomains.add(candidate.domain);
            if (selected.length === 5) return selected;
        }
        for (const candidate of candidates) {
            if (selected.length === 5) break;
            if (!selected.some(item => item.normalized === candidate.normalized)) selected.push(candidate);
        }
        return selected;
    }

    function useSuggestion(text) {
        const input = document.getElementById("message");
        input.value = text;
        input.focus();
        input.dispatchEvent(new Event("input"));
    }

    function appendMessage(role, text, route = null, sources = [], modelName = null) {
        const scroller = document.getElementById("chat-scroller");
        const welcomePanel = document.getElementById("welcomePanel");
        if (welcomePanel) welcomePanel.remove();
        const row = document.createElement("div");
        row.className = `message-row ${role}`;

        const bubble = document.createElement("div");
        bubble.className = "bubble";
        const safeText = String(text || "")
            .replace(/\[([^\]]+)\]\(https?:\/\/[^)]+\)/g, "$1")
            .replace(/https?:\/\/\S+/g, "")
            .replace(/[ \t]{2,}/g, " ")
            .trim();
        bubble.textContent = safeText;
        row.appendChild(bubble);

        if (route) {
            const meta = document.createElement("div");
            meta.className = "meta-line";
            const pill = document.createElement("span");
            pill.className = "route-pill";
            const labels = {"qwen": "Qwen", "qwen-web": "OpenAI Web", "qwen-sol": "Qwen", "qwen-astra": "Qwen", "web": "OpenAI Web"};
            pill.textContent = (labels[route] || route) + (modelName ? " (" + modelName + ")" : "");
            meta.appendChild(pill);
            for (const source of prepareSources(sources)) {
                const link = document.createElement("a");
                link.href = source.original;
                link.textContent = source.label;
                link.target = "_blank";
                link.rel = "noopener noreferrer";
                link.className = "source-link";
                meta.appendChild(link);
            }
            row.appendChild(meta);
        }

        scroller.appendChild(row);
        scroller.scrollTo({ top: scroller.scrollHeight, behavior: "smooth" });
    }

    function showThinkingIndicator() {
        if (document.getElementById("thinkingIndicator")) return;
        const scroller = document.getElementById("chat-scroller");
        const welcomePanel = document.getElementById("welcomePanel");
        if (welcomePanel) welcomePanel.remove();
        const row = document.createElement("div");
        row.id = "thinkingIndicator";
        row.className = "message-row ponkun thinking-row";
        row.setAttribute("role", "status");
        row.setAttribute("aria-live", "polite");
        const bubble = document.createElement("div");
        bubble.className = "bubble";
        const spinner = document.createElement("span");
        spinner.className = "thinking-spinner";
        spinner.setAttribute("aria-hidden", "true");
        const label = document.createElement("span");
        label.textContent = "ぽんくんが考えています...";
        bubble.append(spinner, label);
        row.appendChild(bubble);
        scroller.appendChild(row);
        scroller.scrollTo({ top: scroller.scrollHeight, behavior: "smooth" });
    }

    function hideThinkingIndicator() {
        document.getElementById("thinkingIndicator")?.remove();
    }

    function clearChat() {
        const now = new Date();
        const pad = (n) => String(n).padStart(2, "0");
        currentSessionId = "chat-" + now.getFullYear() + pad(now.getMonth() + 1) + pad(now.getDate()) + "-" + pad(now.getHours()) + pad(now.getMinutes()) + pad(now.getSeconds());

        const scroller = document.getElementById("chat-scroller");
        scroller.innerHTML = "";
        appendMessage("ponkun", "新しいチャットを開始しました。何について話しますか？");

        closeSidebar();
    }

    function appendImageMessage(role, text, imageUrl) {
        const scroller = document.getElementById("chat-scroller");
        const row = document.createElement("div");
        row.className = `message-row ${role}`;

        const bubble = document.createElement("div");
        bubble.className = "bubble";

        const img = document.createElement("img");
        img.src = imageUrl;
        img.style.maxWidth = "200px";
        img.style.maxHeight = "200px";
        img.style.borderRadius = "10px";
        img.style.display = "block";
        img.style.marginBottom = "8px";
        bubble.appendChild(img);

        const span = document.createElement("span");
        span.textContent = text;
        bubble.appendChild(span);

        row.appendChild(bubble);
        scroller.appendChild(row);
        scroller.scrollTo({ top: scroller.scrollHeight, behavior: "smooth" });
    }

    // スマホ本体のスピーカーで Fish Audio MP3 を再生する
    async function streamSpeech(text) {
        if (!text) return;

        let speechText = text
            .replace(/【[^】]+】/g, "")
            .replace(/^[・●-]\s*/gm, "")
            .trim();

        const sentences = speechText.match(/[^。！？\n]+[。！？]?/g) || [speechText];

        for (const sentence of sentences) {
            const chunk = sentence.trim();
            if (!chunk) continue;

            try {
                const response = await authenticatedFetch("/speak", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ text: chunk })
                });

                if (!response.ok) {
                    throw new Error("TTS error: " + response.status);
                }

                const blob = await response.blob();
                const audioUrl = URL.createObjectURL(blob);
                const audio = new Audio(audioUrl);

                await new Promise((resolve, reject) => {
                    audio.onended = () => {
                        URL.revokeObjectURL(audioUrl);
                        resolve();
                    };
                    audio.onerror = () => {
                        URL.revokeObjectURL(audioUrl);
                        reject(new Error("Audio playback error"));
                    };
                    audio.play().catch(reject);
                });
            } catch (err) {
                console.error("Mobile発話エラー:", err);
            }
        }
    }

    let messageRequestInFlight = false;

    async function sendMessage() {
        const input = document.getElementById("message");
        const text = input.value.trim();

        if ((!text && !selectedImageFile) || messageRequestInFlight) return;

        if (selectedImageFile) {
            const approved = window.confirm("送信先：OpenAI（外部API）\\n画像が外部へ送信され、利用料金が発生する場合があります。送信しますか？");
            if (!approved) return;
            const consent = window.confirm("送信先：OpenAI（外部API）\n画像が外部へ送信され、料金が発生する場合があります。送信しますか？");
            if (!consent) return;
            const fileToSend = selectedImageFile;
            const promptText = text;
            input.value = "";
            input.style.height = "auto";
            cancelImage();

            const previewUrl = URL.createObjectURL(fileToSend);
            appendImageMessage("user", promptText || "この写真を見て", previewUrl);
            messageRequestInFlight = true;
            showThinkingIndicator();

            const formData = new FormData();
            formData.append("image", fileToSend);
            formData.append("text", promptText);
            formData.append("session_id", currentSessionId);
            formData.append("external_consent", "1");
            formData.append("external_consent", "1");

            try {
                const res = await authenticatedFetch("/vision", { method: "POST", body: formData });
                const data = await res.json();
                if (!res.ok) throw { response: data };
                if (!res.ok) throw { response: data };
                hideThinkingIndicator();
                appendMessage("ponkun", data.display_text, data.route);
                streamSpeech(data.speech_text);
                await loadSessions();
            } catch (err) {
                const detail = err && err.response && err.response.detail;
                if (detail && detail.diagnostic_id && detail.message) {
                    appendMessage("ponkun", detail.message + " 診断ID: " + detail.diagnostic_id);
                } else if (detail && detail.message) {
                    appendMessage("ponkun", detail.message);
                } else {
                const detail = err && err.response && err.response.detail;
                if (detail && detail.message) {
                    const diagnostic = detail.diagnostic_id ? " 診断ID: " + detail.diagnostic_id : "";
                    appendMessage("ponkun", detail.message + diagnostic);
                } else {
                    appendMessage("ponkun", "画像の解析中にエラーが発生しました。");
                }
                }
            } finally {
                hideThinkingIndicator();
                messageRequestInFlight = false;
            }
            return;
        }

        appendMessage("user", text);
        input.value = "";
        input.dispatchEvent(new Event("input"));
        messageRequestInFlight = true;
        showThinkingIndicator();

        try {
            const res = await authenticatedFetch("/chat", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ text: text, session_id: currentSessionId })
            });
            const data = await res.json();
            if (!res.ok) throw { response: data };
            hideThinkingIndicator();
            appendMessage("ponkun", data.display_text, data.route, data.sources || [], data.model_name || null);
            streamSpeech(data.speech_text);
            await loadSessions();
        } catch (err) {
            const detail = err && err.response && err.response.detail;
            if (detail && detail.diagnostic_id && detail.message && !(detail.code || "").startsWith("web_search_")) {
                appendMessage("ponkun", detail.message + " \u8a3a\u65adID: " + detail.diagnostic_id);
                return;
            }
            if (detail && detail.code && detail.code.startsWith("web_search_")) {
                appendMessage("ponkun", "\u691c\u7d22\u30b5\u30fc\u30d3\u30b9\u306b\u63a5\u7d9a\u3067\u304d\u306a\u3044\u305f\u3081\u3001\u6700\u65b0\u60c5\u5831\u3092\u78ba\u8a8d\u3067\u304d\u307e\u305b\u3093\u3067\u3057\u305f\u3002");
                return;
            }
            appendMessage("ponkun", "通信エラーが発生しました。");
        } finally {
            hideThinkingIndicator();
            messageRequestInFlight = false;
        }
    }

    const messageInput = document.getElementById("message");
    function resizeMessageInput() {
        messageInput.style.height = "auto";
        const nextHeight = Math.min(messageInput.scrollHeight, 160);
        messageInput.style.height = nextHeight + "px";
        messageInput.style.overflowY = messageInput.scrollHeight > 160 ? "auto" : "hidden";
    }
    messageInput.addEventListener("input", resizeMessageInput);
    resizeMessageInput();

    messageInput.addEventListener("keydown", (e) => {
        const isMobile = window.matchMedia("(pointer: coarse)").matches;
        if (e.key === "Enter" && !e.shiftKey && !isMobile) {
            e.preventDefault();
            sendMessage();
        }
    });

    document.getElementById("voiceFile").addEventListener("change", async function() {
        const file = this.files[0];
        if (!file) return;

        appendMessage("user", "🎤 音声を解析中...");
        const formData = new FormData();
        formData.append("audio", file);

        try {
            const res = await authenticatedFetch("/voice", { method: "POST", body: formData });
            const data = await res.json();
            if (data.transcription) {
                appendMessage("user", data.transcription);
            }
            appendMessage("ponkun", data.display_text, data.route);
            streamSpeech(data.speech_text);
            await loadSessions();
        } catch (err) {
            appendMessage("ponkun", "音声処理エラーが発生しました。");
        }
        this.value = "";
    });

    document.getElementById("cameraFile").addEventListener("change", function() {
        const file = this.files[0];
        if (!file) return;

        selectedImageFile = file;
        const reader = new FileReader();
        reader.onload = function(e) {
            document.getElementById("imagePreview").src = e.target.result;
            document.getElementById("imageName").textContent = file.name || "写真が撮影されました";
            document.getElementById("imagePreviewContainer").style.display = "flex";
        };
        reader.readAsDataURL(file);
    });

    function cancelImage() {
        selectedImageFile = null;
        document.getElementById("cameraFile").value = "";
        document.getElementById("imagePreviewContainer").style.display = "none";
    }

    async function loadSessions() {
        try {
        const res = await authenticatedFetch("/sessions");
            const data = await res.json();
            const historyList = document.getElementById("historyList");
            historyList.innerHTML = "";

            for (const session of data.sessions || []) {
                const li = document.createElement("li");
                li.className = "history-item";
                li.textContent = session.title;
                li.onclick = () => loadHistory(session.session_id);
                historyList.appendChild(li);
            }
        } catch (err) {
            console.error("セッション一覧取得エラー:", err);
        }
    }

    async function loadHistory(sessionId) {
        try {
            currentSessionId = sessionId;
            const res = await authenticatedFetch("/history/" + encodeURIComponent(sessionId));
            const data = await res.json();
            const scroller = document.getElementById("chat-scroller");
            scroller.innerHTML = "";

            for (const message of data.messages || []) {
                const role = message.role === "assistant" ? "ponkun" : "user";
                appendMessage(role, message.content);
            }
            scroller.scrollTop = scroller.scrollHeight;

            closeSidebar();
        } catch (err) {
            console.error("履歴取得エラー:", err);
        }
    }

    setSidebarOpen(false);
    window.addEventListener("resize", () => setSidebarOpen(false));
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") closeSidebar();
    });
    loadSessions();
    if ("serviceWorker" in navigator) {
        window.addEventListener("load", () => {
            navigator.serviceWorker.register("/service-worker.js", {scope: "/"}).catch(() => {
                // PWA registration is optional; chat functionality remains network-based.
            });
        });
    }
</script>
</body>
</html>
"""

# =========================
# API エンドポイント
# =========================
@app.get("/health")
def health():
    return {"status": "ok", "client": "mobile", "model": whisper_state}


@app.get("/readiness")
def readiness():
    return {
        "ready": whisper_state == "ready",
        "model": whisper_state,
        "error": whisper_error,
        "model_name": WHISPER_MODEL_NAME,
        "device": WHISPER_DEVICE,
    }


@app.get("/sessions")
def get_sessions(_auth=Depends(require_auth)):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT session_id, MIN(id) AS first_id, MAX(id) AS last_id, COUNT(*) AS message_count
        FROM conversation_history
        GROUP BY session_id
        ORDER BY last_id DESC
    """)
    session_rows = cur.fetchall()
    sessions = []

    for session_id, first_id, last_id, message_count in session_rows:
        cur.execute(
            """
            SELECT content FROM conversation_history
            WHERE session_id = ? AND role = 'user'
            ORDER BY id ASC LIMIT 1
        """,
            (session_id,),
        )
        first_user = cur.fetchone()
        title = (
            first_user[0][:40]
            if first_user and first_user[0]
            else session_id
        )
        sessions.append({
            "session_id": session_id,
            "title": title,
            "message_count": message_count,
        })
    conn.close()
    return {"sessions": sessions}


@app.get("/history/{session_id}")
def get_session_history(session_id: str, _auth=Depends(require_auth)):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT role, content, created_at
        FROM conversation_history
        WHERE session_id = ?
        ORDER BY id ASC
    """,
        (session_id,),
    )
    rows = cur.fetchall()
    conn.close()

    return {
        "session_id": session_id,
        "messages": [
            {"role": r, "content": c, "created_at": ca} for r, c, ca in rows
        ],
    }


@app.post("/chat")
def chat(req: ChatRequest, _auth=Depends(require_auth)):
    # 魂 (ponkun_core) を直接呼び出し
    started = time.perf_counter()
    request_id = secrets.token_hex(4)
    route_hint = _chat_route_hint(req.text)
    try:
        result = ask_ponkun(req.text, session_id=req.session_id)
    except Exception as exc:
        category, status, message, stage = _classify_chat_exception(exc)
        _write_chat_diagnostic(request_id, category, type(exc).__name__, stage, status, route_hint)
        detail = {"code": category, "message": message, "diagnostic_id": request_id}
        if isinstance(exc, WebSearchError):
            detail["code"] = "web_search_" + exc.code
        raise HTTPException(status_code=status, detail=detail) from exc
    print(f"[ponkun-timing] mobile_qwen_total_ms={round((time.perf_counter() - started) * 1000)}")
    return {
        "route": result["route"],
        "display_text": result["answer"],
        "speech_text": result["answer"],
        "sources": result.get("sources", []),
        "model_name": result.get("model_name"),
    }


@app.post("/voice")
async def voice(audio: UploadFile = File(...), _auth=Depends(require_auth)):
    session_id = "mobile-main"
    suffix = os.path.splitext(audio.filename or "voice.m4a")[1] or ".m4a"

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
        audio_data = await audio.read()
        temp_file.write(audio_data)
        temp_path = temp_file.name

    try:
        asr_started = time.perf_counter()
        segments, _ = get_whisper_model().transcribe(
            temp_path,
            language="ja",
            initial_prompt="量子コンピュータ, NVIDIA, Python, GPT, Qwen, Whisper, Fish Audio, Ollama, Docker, OpenAI, API, RTX, GPU, ローカルLLM",
        )
        user_text = "".join(s.text for s in segments).strip()
        print(f"[ponkun-timing] mobile_whisper_ms={round((time.perf_counter() - asr_started) * 1000)}")

        if not user_text:
            return {
                "route": "none",
                "transcription": "",
                "display_text": "音声を認識できませんでした。",
                "speech_text": "音声を認識できませんでした。",
            }

        # 魂 (ponkun_core) を直接呼び出し
        qwen_started = time.perf_counter()
        result = ask_ponkun(user_text, session_id=session_id)
        print(f"[ponkun-timing] mobile_voice_qwen_total_ms={round((time.perf_counter() - qwen_started) * 1000)}")
        return {
            "route": result["route"],
            "transcription": user_text,
            "display_text": result["answer"],
            "speech_text": result["answer"],
        }
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass


@app.post("/vision")
async def vision_api(
    image: UploadFile = File(...),
    text: str = Form(""),
    session_id: str = Form("mobile-main"),
    external_consent: str = Form(""),
    _auth=Depends(require_auth),
):
    if VISION_BACKEND != "openai":
        raise HTTPException(status_code=503, detail={"code": "vision_disabled", "message": "画像分析は現在無効です。"})
    if external_consent != "1":
        raise HTTPException(status_code=403, detail={"code": "vision_consent_required", "message": "OpenAIへの画像送信確認が必要です。"})
    image_bytes = await image.read()
    kind, _width, _height = validate_vision_image(image_bytes, image.content_type)
    mime_type = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}[kind]
    user_prompt = text.strip() or "この画像について説明して"

    try:
        answer = ask_cloud_vision(user_prompt, image_bytes, mime_type, external_consent=True)
    except OpenAIVisionError as exc:
        request_id = secrets.token_hex(4)
        status = exc.http_status if isinstance(exc.http_status, int) and 400 <= exc.http_status <= 599 else 502
        _write_chat_diagnostic(request_id, exc.code, type(exc).__name__, "vision.provider", status, "vision")
        raise HTTPException(status_code=status, detail={"code": exc.code, "message": "画像分析に失敗しました。", "diagnostic_id": request_id}) from exc
    except Exception as exc:
        request_id = secrets.token_hex(4)
        _write_chat_diagnostic(request_id, "vision_internal_error", type(exc).__name__, "vision.adapter", 500, "vision")
        raise HTTPException(status_code=500, detail={"code": "vision_internal_error", "message": "画像分析で内部エラーが発生しました。", "diagnostic_id": request_id}) from exc
    save_history(session_id, f"📷 [画像送信] {user_prompt}", answer)
    speech_text = make_vision_speech_text(answer)

    return {
        "route": build_vision_route(user_prompt),
        "display_text": answer,
        "speech_text": speech_text,
    }
