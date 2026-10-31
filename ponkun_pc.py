import io
import os
import time
import wave
import tempfile
import threading
from ponkun_core import ask_ponkun
import numpy as np
import pygame
import sounddevice as sd
from faster_whisper import WhisperModel
from ponkun_tts import synthesize
from ponkun_hub.pc_streaming_voice import (
    answer_with_optional_streaming,
    stream_hub_deltas,
    streaming_voice_enabled,
)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# =========================================================
# 設定
# =========================================================

SAMPLE_RATE = 16000
BLOCK_SIZE = 1024

CLAP_THRESHOLD = 0.18

DOUBLE_CLAP_MIN_INTERVAL = 0.20
DOUBLE_CLAP_MAX_INTERVAL = 1.50

# 同じ拍手を何度も検出しないため
CLAP_DEBOUNCE = 0.15

WAKE_COOLDOWN = 3.0


# =========================================================
# 起動音
# =========================================================

WAKE_SOUND_FILE = os.path.join(BASE_DIR, "wake.wav")

WAKE_SOUND_START = 9.0
WAKE_SOUND_END = 20.0


# =========================================================
# Fish Audio
# =========================================================

# =========================================================
# Whisper
# =========================================================

print("Whisper読み込み中...")

WHISPER_MODEL_NAME = os.environ.get("PONKUN_WHISPER_MODEL", "large-v3")
WHISPER_DEVICE = os.environ.get("PONKUN_WHISPER_DEVICE", "cuda")
WHISPER_COMPUTE_TYPE = os.environ.get("PONKUN_WHISPER_COMPUTE_TYPE", "float16")

whisper_model = None
whisper_state = "not_loaded"
whisper_error = None
whisper_lock = threading.Lock()


def get_whisper_model():
    """Load Whisper only when speech recognition is actually requested."""

    global whisper_model, whisper_state, whisper_error

    if whisper_model is not None:
        return whisper_model

    with whisper_lock:
        if whisper_model is not None:
            return whisper_model

        whisper_state = "loading"
        whisper_error = None
        try:
            print("Whisper読み込み中...")
            whisper_model = WhisperModel(
                WHISPER_MODEL_NAME,
                device=WHISPER_DEVICE,
                compute_type=WHISPER_COMPUTE_TYPE,
            )
            whisper_state = "ready"
            print("Whisper準備OK")
            return whisper_model
        except Exception as exc:
            whisper_state = "error"
            whisper_error = f"{type(exc).__name__}: {exc}"
            print("Whisper読み込みエラー >", whisper_error)
            raise

print("Whisper準備OK")


# =========================================================
# 状態
# =========================================================

last_clap_time = 0.0
last_peak_time = 0.0
last_wake_time = 0.0

busy = False

state_lock = threading.Lock()


# =========================================================
# 音声再生
# =========================================================

audio_init_lock = threading.Lock()
audio_initialized = False


def ensure_audio_initialized():
    """Initialize pygame audio only when playback is actually requested."""

    global audio_initialized
    if audio_initialized:
        return
    with audio_init_lock:
        if not audio_initialized:
            pygame.mixer.init()
            audio_initialized = True


def play_wake_segment():
    """
    wake.wav の9秒～18秒だけ再生
    """

    try:

        ensure_audio_initialized()
        with wave.open(WAKE_SOUND_FILE, "rb") as wf:

            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            frame_rate = wf.getframerate()

            start_frame = int(
                WAKE_SOUND_START * frame_rate
            )

            end_frame = int(
                WAKE_SOUND_END * frame_rate
            )

            wf.setpos(start_frame)

            frames = wf.readframes(
                end_frame - start_frame
            )

        # 一時WAVファイルを作成
        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=".wav"
        ) as temp:

            temp_path = temp.name

        with wave.open(
            temp_path,
            "wb"
        ) as out:

            out.setnchannels(channels)
            out.setsampwidth(sample_width)
            out.setframerate(frame_rate)
            out.writeframes(frames)

        pygame.mixer.music.load(temp_path)

        pygame.mixer.music.play()

        while pygame.mixer.music.get_busy():
            time.sleep(0.02)

    except Exception as e:
        print("起動音エラー >", e)


    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass


# =========================================================
# Fish
# =========================================================

def speak(text: str):

    started = time.perf_counter()
    temp_path = None
    try:
        ensure_audio_initialized()

        print("ぽんくん >", text)

        result = synthesize(text)
        fish_ms = result.elapsed_ms

        audio_data = io.BytesIO(result.audio)

        pygame.mixer.music.load(
            audio_data,
            "mp3"
        )

        pygame.mixer.music.play()
        playback_start_ms = round((time.perf_counter() - started) * 1000)

        while pygame.mixer.music.get_busy():
            time.sleep(0.02)
        print(f"[ponkun-timing] pc_fish_tts_ms={fish_ms} playback_start_ms={playback_start_ms} playback_total_ms={round((time.perf_counter() - started) * 1000)}")

    except Exception as e:
        print("Fish Audioエラー >", e)


def play_streaming_audio(result):
    """Play one already-generated TTS result; used only by opt-in streaming."""
    ensure_audio_initialized()
    audio_data = io.BytesIO(result.audio)
    pygame.mixer.music.load(audio_data, "mp3")
    pygame.mixer.music.play()
    while pygame.mixer.music.get_busy():
        time.sleep(0.02)


# =========================================================
# wakeup 音声認識
# =========================================================

def listen_for_wakeup():

    print()
    print(">>> 「wakeup」と言ってください")

    # 3秒録音
    duration = 3.0

    audio = sd.rec(
        int(duration * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32"
    )

    sd.wait()

    # Whisper用にwavへ保存
    pcm = np.int16(
        np.clip(audio, -1, 1) * 32767
    )

    with tempfile.NamedTemporaryFile(
        delete=False,
        suffix=".wav"
    ) as temp:

        temp_path = temp.name

    with wave.open(
        temp_path,
        "wb"
    ) as wf:

        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)

        wf.writeframes(
            pcm.tobytes()
        )

    try:
        segments, _ = get_whisper_model().transcribe(
            temp_path,
            language="en"
        )
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass

    text = "".join(
        segment.text
        for segment in segments
    ).strip().lower()

    print("認識 >", text)

    text = (
        text
        .replace(" ", "")
        .replace(".", "")
        .replace(",", "")
        .replace("!", "")
        .replace("?", "")
    )

    return (
        "wakeup" in text
        or "wake up" in text
        or "ウェイクアップ" in text
    )

def listen_user_command():

    print()
    print(">>> 話し始めを待っています")

    threshold = 0.02
    silence_limit = 1.0
    max_duration = 15.0

    recorded_chunks = []

    started = False
    silence_started = None
    start_time = time.time()

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        blocksize=BLOCK_SIZE
    ) as stream:

        while True:

            data, _ = stream.read(BLOCK_SIZE)

            rms = float(
                np.sqrt(
                    np.mean(
                        np.square(data)
                    )
                )
            )

            now = time.time()

            # まだ話し始めていない
            if not started:

                if rms >= threshold:
                    print(">>> 発話開始")
                    started = True
                    recorded_chunks.append(data.copy())

                if now - start_time >= max_duration:
                    return ""

                continue

            # 発話中
            recorded_chunks.append(data.copy())

            if rms < threshold:

                if silence_started is None:
                    silence_started = now

                elif now - silence_started >= silence_limit:
                    print(">>> 発話終了")
                    break

            else:
                silence_started = None

            if now - start_time >= max_duration:
                break

    if not recorded_chunks:
        return ""

    audio = np.concatenate(
        recorded_chunks,
        axis=0
    )

    pcm = np.int16(
        np.clip(audio, -1, 1) * 32767
    )

    with tempfile.NamedTemporaryFile(
        delete=False,
        suffix=".wav"
    ) as temp:
        temp_path = temp.name

    with wave.open(temp_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())

    asr_started = time.perf_counter()
    try:
        segments, _ = get_whisper_model().transcribe(
            temp_path,
            language="ja"
        )
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass

    text = "".join(
        segment.text
        for segment in segments
    ).strip()

    print("あなた >", text)

    print(f"[ponkun-timing] pc_whisper_ms={round((time.perf_counter() - asr_started) * 1000)}")
    return text

# =========================================================
# 起動処理
# =========================================================

def wake_sequence():

    global busy
    global last_wake_time

    with state_lock:
        if busy:
            return
        busy = True

    try:

        print()
        print("========================")
        print("👏👏 DOUBLE CLAP")
        print("========================")

        if not listen_for_wakeup():
            print("wakeup を確認できませんでした")
            return

        print()
        print("WAKEUP確認")

        last_wake_time = time.time()

        print(">>> 起動音を再生します")
        play_wake_segment()
        print(">>> 起動音の再生終了")

        print(">>> Fishを再生します")
        speak("なんだ？")
        print(">>> Fish再生終了")
        while True:

            user_text = listen_user_command()

            if not user_text:
                print("音声を認識できませんでした")
                break

            # 終了ワード
            if user_text in [
                "終わり",
                "終了",
                "じゃあな",
                "またな",
                "おやすみ"
            ]:
                speak("じゃあな。")
                break

            qwen_started = time.perf_counter()
            if streaming_voice_enabled():
                stream_result = answer_with_optional_streaming(
                    enabled=True,
                    deltas=stream_hub_deltas(user_text),
                    tts_generate=synthesize,
                    play_audio=play_streaming_audio,
                    legacy_answer=lambda: None,
                )
                answer = stream_result.text
                print(f"[ponkun-timing] pc_stream_total_ms={stream_result.metrics.total_turn_ms}")
                print(f"[ponkun-timing] pc_stream_first_audio_ms={stream_result.metrics.time_to_first_audio_ms}")
            else:
                result = ask_ponkun(
                    user_text,
                    session_id="pc-main"
                )

                answer = result["answer"]
                print(f"[ponkun-timing] pc_qwen_total_ms={round((time.perf_counter() - qwen_started) * 1000)}")

                print(
                    "ルート >",
                    result["route"]
                )

                speak(answer)

            print()
            print(">>> 次の発話を待っています")

    finally:

        last_wake_time = time.time()

        time.sleep(0.5)

        with state_lock:
            busy = False

# =========================================================
# 拍手検出
# =========================================================

def audio_callback(
    indata,
    frames,
    time_info,
    status
):

    global last_clap_time
    global last_peak_time

    if busy:
        return

    now = time.time()

    if (
        now - last_wake_time
        < WAKE_COOLDOWN
    ):
        return

    rms = float(
        np.sqrt(
            np.mean(
                np.square(indata)
            )
        )
    )

    if rms < CLAP_THRESHOLD:
        return

    # 同じ拍手の余韻を無視
    if (
        now - last_peak_time
        < CLAP_DEBOUNCE
    ):
        return

    last_peak_time = now

    print(
        f"👏 音量ピーク RMS={rms:.3f}"
    )

    interval = (
        now - last_clap_time
    )

    if (
        DOUBLE_CLAP_MIN_INTERVAL
        <= interval
        <= DOUBLE_CLAP_MAX_INTERVAL
    ):

        last_clap_time = 0.0

        threading.Thread(
            target=wake_sequence,
            daemon=True
        ).start()

        return

    last_clap_time = now


# =========================================================
# Main
# =========================================================

def main():

    print()
    print("==============================")
    print(" Ponkun PC")
    print("==============================")
    print()
    print("👏👏 → wakeup → 起動音 → なんだ？")
    print()
    print(
        "Clap threshold =",
        CLAP_THRESHOLD
    )

    try:

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=BLOCK_SIZE,
            callback=audio_callback
        ):

            while True:
                time.sleep(1)

    except KeyboardInterrupt:

        print()
        print("Ponkun PC 終了")


if __name__ == "__main__":
    main()
