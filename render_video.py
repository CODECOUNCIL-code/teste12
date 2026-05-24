import asyncio
import os
import re
import textwrap
import subprocess
from pathlib import Path

import edge_tts
import requests
from PIL import Image, ImageEnhance, ImageFilter


# =========================
# CONFIG
# =========================

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

SCRIPT_PATH = OUTPUT_DIR / "script.txt"
BACKGROUND_PATH = OUTPUT_DIR / "background.jpg"
FRAME_PATH = OUTPUT_DIR / "frame.jpg"
VOICE_MP3_PATH = OUTPUT_DIR / "voice.mp3"
VOICE_WAV_PATH = OUTPUT_DIR / "voice.wav"
SUBTITLE_PATH = OUTPUT_DIR / "subtitles.srt"
FINAL_VIDEO_PATH = OUTPUT_DIR / "final_video.mp4"

VIDEO_WIDTH = 1280
VIDEO_HEIGHT = 720
FPS = 24

# Segurança: nunca deixa gerar vídeo gigante.
MAX_DURATION_SECONDS = 45 * 60

# Para teste rápido, podes mudar temporariamente para 10 minutos:
# MAX_DURATION_SECONDS = 10 * 60

# Voz PT-PT.
# Masculina: pt-PT-DuarteNeural
# Feminina: pt-PT-RaquelNeural
EDGE_VOICE = os.environ.get("EDGE_VOICE", "pt-PT-DuarteNeural")
EDGE_RATE = os.environ.get("EDGE_RATE", "-8%")
EDGE_VOLUME = os.environ.get("EDGE_VOLUME", "+0%")


# =========================
# HELPERS
# =========================

def clean_text(text: str) -> str:
    text = text or ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_portuguese_text(text: str) -> str:
    """
    Pequena limpeza para TTS.
    Ajuda a voz pt-PT a ler melhor.
    """
    text = clean_text(text)

    replacements = {
        "AI": "inteligência artificial",
        "IA": "inteligência artificial",
        "CCTV": "circuito fechado",
        "VHS": "vê agá ésse",
        "24/7": "vinte e quatro sobre sete",
        "24h": "vinte e quatro horas",
        "00:00": "meia-noite",
        "01:00": "uma da manhã",
        "02:00": "duas da manhã",
        "03:00": "três da manhã",
        "04:00": "quatro da manhã",
        "05:00": "cinco da manhã",
        "06:00": "seis da manhã",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    # Remove markdown se o modelo meter sem querer.
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", text)

    # Remove caracteres invisíveis/problemáticos.
    text = text.replace("\u200b", "")
    text = text.replace("\ufeff", "")

    return text.strip()


def split_sentences(text: str):
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def srt_timestamp(seconds: float) -> str:
    seconds = max(0, seconds)
    ms = int((seconds - int(seconds)) * 1000)
    s = int(seconds) % 60
    m = (int(seconds) // 60) % 60
    h = int(seconds) // 3600
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def run(cmd, check=True):
    print("\nRUN:", " ".join(str(x) for x in cmd))
    return subprocess.run(cmd, check=check)


def run_capture(cmd):
    print("\nRUN:", " ".join(str(x) for x in cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def get_media_duration(path: Path) -> float:
    output = run_capture([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])

    try:
        return float(output)
    except ValueError:
        raise RuntimeError(f"Could not read duration from ffprobe output: {output}")


def download_background(url: str, output_path: Path):
    if not url:
        raise ValueError("BACKGROUND_URL is empty")

    print(f"Downloading background: {url}")

    headers = {
        "User-Agent": "Mozilla/5.0 n8n-renderer"
    }

    response = requests.get(url, headers=headers, timeout=60)
    response.raise_for_status()

    output_path.write_bytes(response.content)
    print(f"Background saved: {output_path}")


def make_base_frame(background_path: Path, output_path: Path):
    print("Creating base horror frame...")

    img = Image.open(background_path).convert("RGB")

    src_w, src_h = img.size
    target_ratio = VIDEO_WIDTH / VIDEO_HEIGHT
    src_ratio = src_w / src_h

    if src_ratio > target_ratio:
        new_w = int(src_h * target_ratio)
        left = (src_w - new_w) // 2
        img = img.crop((left, 0, left + new_w, src_h))
    else:
        new_h = int(src_w / target_ratio)
        top = (src_h - new_h) // 2
        img = img.crop((0, top, src_w, top + new_h))

    img = img.resize((VIDEO_WIDTH, VIDEO_HEIGHT), Image.LANCZOS)

    # Look tipo CCTV / horror: preto e branco, escuro, desfocado.
    img = img.convert("L").convert("RGB")
    img = ImageEnhance.Brightness(img).enhance(0.52)
    img = ImageEnhance.Contrast(img).enhance(1.55)
    img = img.filter(ImageFilter.GaussianBlur(radius=1.15))

    img.save(output_path, quality=92)
    print(f"Frame saved: {output_path}")


async def generate_voice_edge_tts(script: str, output_path: Path):
    print("Generating voice with edge-tts...")
    print(f"Voice: {EDGE_VOICE}")
    print(f"Rate: {EDGE_RATE}")
    print(f"Volume: {EDGE_VOLUME}")

    communicate = edge_tts.Communicate(
        text=script,
        voice=EDGE_VOICE,
        rate=EDGE_RATE,
        volume=EDGE_VOLUME,
    )

    await communicate.save(str(output_path))

    if not output_path.exists() or output_path.stat().st_size < 1000:
        raise RuntimeError("Edge TTS failed or output file is too small.")

    print(f"Voice saved: {output_path}")


def convert_mp3_to_wav(mp3_path: Path, wav_path: Path):
    run([
        "ffmpeg",
        "-y",
        "-i", str(mp3_path),
        "-ar", "44100",
        "-ac", "2",
        str(wav_path),
    ])


def create_subtitles(script: str, duration: float, output_path: Path):
    print("Creating subtitles...")

    sentences = split_sentences(script)

    groups = []
    current = ""

    for sentence in sentences:
        if len(current) + len(sentence) < 105:
            current = (current + " " + sentence).strip()
        else:
            if current:
                groups.append(current)
            current = sentence

    if current:
        groups.append(current)

    if not groups:
        groups = [script[:120]]

    total_words = sum(len(g.split()) for g in groups) or 1
    cursor = 0.0
    lines = []

    for idx, group in enumerate(groups, start=1):
        words = len(group.split())
        length = max(2.2, duration * (words / total_words))
        start = cursor
        end = min(duration, cursor + length)

        wrapped = "\n".join(textwrap.wrap(group, width=48))[:260]

        lines.append(str(idx))
        lines.append(f"{srt_timestamp(start)} --> {srt_timestamp(end)}")
        lines.append(wrapped)
        lines.append("")

        cursor = end

        if cursor >= duration:
            break

    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Subtitles saved: {output_path}")


def render_video(frame_path: Path, voice_path: Path, subtitle_path: Path, output_path: Path):
    audio_duration = get_media_duration(voice_path)
    duration = min(audio_duration + 1.5, MAX_DURATION_SECONDS)

    print(f"Audio duration: {audio_duration:.2f}s / {audio_duration / 60:.2f} min")
    print(f"Video duration used: {duration:.2f}s / {duration / 60:.2f} min")
    print(f"Max duration: {MAX_DURATION_SECONDS}s")

    subtitle_path_str = str(subtitle_path).replace("\\", "/").replace(":", "\\:")

    # Efeito estilo GIF/CCTV:
    # - zoompan lento
    # - deslocamento suave
    # - preto e branco
    # - ruído
    # - vinheta
    # - legendas em baixo
    #
    # IMPORTANTE:
    # format=yuv420p no fim para compatibilidade com Windows/YouTube/telemóveis.
    vf = (
        f"scale={VIDEO_WIDTH + 180}:{VIDEO_HEIGHT + 100}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH + 180}:{VIDEO_HEIGHT + 100},"
        f"zoompan="
        f"z='min(zoom+0.00020,1.10)':"
        f"x='iw/2-(iw/zoom/2)+8*sin(on/37)':"
        f"y='ih/2-(ih/zoom/2)+5*cos(on/43)':"
        f"d=1:"
        f"s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:"
        f"fps={FPS},"
        f"format=gray,"
        f"eq=contrast=1.38:brightness=-0.055:saturation=0,"
        f"noise=alls=18:allf=t+u,"
        f"vignette=PI/4,"
        f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.12:t=fill,"
        f"subtitles='{subtitle_path_str}':"
        f"force_style='FontName=DejaVu Sans,"
        f"FontSize=24,"
        f"PrimaryColour=&H00FFFFFF,"
        f"OutlineColour=&H00000000,"
        f"BackColour=&H80000000,"
        f"BorderStyle=3,"
        f"Outline=1,"
        f"Shadow=0,"
        f"Alignment=2,"
        f"MarginV=35',"
        f"format=yuv420p,"
        f"setsar=1"
    )

    run([
        "ffmpeg",
        "-y",

        "-loop", "1",
        "-framerate", str(FPS),
        "-i", str(frame_path),

        "-i", str(voice_path),

        "-t", str(duration),

        "-vf", vf,

        "-r", str(FPS),

        # Compatível com Windows/YouTube.
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-profile:v", "high",
        "-level", "4.0",
        "-preset", "veryfast",
        "-crf", "24",

        # Áudio compatível.
        "-c:a", "aac",
        "-b:a", "160k",
        "-ar", "44100",
        "-ac", "2",

        "-movflags", "+faststart",
        "-shortest",
        str(output_path),
    ])

    if not output_path.exists() or output_path.stat().st_size < 1000:
        raise RuntimeError("Final video was not created or is too small.")

    print(f"Final video saved: {output_path}")
    print(f"Final video size: {output_path.stat().st_size / 1024 / 1024:.2f} MB")


def verify_final_video(video_path: Path):
    """
    Mostra no log se o ficheiro ficou compatível.
    O objetivo é ver:
    codec_name=h264
    pix_fmt=yuv420p
    """
    print("Verifying final video...")

    try:
        video_info = run_capture([
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,profile,pix_fmt,width,height,r_frame_rate",
            "-of", "default=noprint_wrappers=1",
            str(video_path),
        ])

        audio_info = run_capture([
            "ffprobe",
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name,sample_rate,channels",
            "-of", "default=noprint_wrappers=1",
            str(video_path),
        ])

        print("VIDEO INFO:")
        print(video_info)
        print("AUDIO INFO:")
        print(audio_info)

    except Exception as e:
        print(f"Could not verify final video: {e}")


# =========================
# MAIN
# =========================

def main():
    title = os.environ.get("VIDEO_TITLE", "História de Terror").strip() or "História de Terror"
    script = os.environ.get("VIDEO_SCRIPT", "")
    background_url = os.environ.get("BACKGROUND_URL", "")
    thumbnail_text = os.environ.get("THUMBNAIL_TEXT", "NÃO ABRAS ESTA PORTA")

    script = normalize_portuguese_text(script)

    if not script:
        raise ValueError("VIDEO_SCRIPT is empty. The GitHub workflow did not receive the script.")

    print("========== INPUT ==========")
    print(f"Title: {title}")
    print(f"Thumbnail text: {thumbnail_text}")
    print(f"Script chars: {len(script)}")
    print(f"Script words: {len(script.split())}")
    print(f"Background URL: {background_url}")
    print(f"Voice: {EDGE_VOICE}")
    print("===========================")

    SCRIPT_PATH.write_text(script, encoding="utf-8")

    download_background(background_url, BACKGROUND_PATH)
    make_base_frame(BACKGROUND_PATH, FRAME_PATH)

    asyncio.run(generate_voice_edge_tts(script, VOICE_MP3_PATH))
    convert_mp3_to_wav(VOICE_MP3_PATH, VOICE_WAV_PATH)

    voice_duration = get_media_duration(VOICE_WAV_PATH)
    print(f"Measured voice duration: {voice_duration:.2f}s / {voice_duration / 60:.2f} minutes")

    if voice_duration > MAX_DURATION_SECONDS:
        print(
            f"WARNING: voice is longer than max duration. "
            f"It will be cut to {MAX_DURATION_SECONDS / 60:.1f} minutes."
        )

    final_duration = min(voice_duration + 1.5, MAX_DURATION_SECONDS)

    create_subtitles(script, final_duration, SUBTITLE_PATH)
    render_video(FRAME_PATH, VOICE_WAV_PATH, SUBTITLE_PATH, FINAL_VIDEO_PATH)
    verify_final_video(FINAL_VIDEO_PATH)

    print("DONE")


if __name__ == "__main__":
    main()
