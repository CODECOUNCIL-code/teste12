import os
import re
import textwrap
import subprocess
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter


# =========================
# CONFIG
# =========================

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

SCRIPT_PATH = OUTPUT_DIR / "script.txt"
BACKGROUND_PATH = OUTPUT_DIR / "background.jpg"
FRAME_PATH = OUTPUT_DIR / "frame.jpg"
VOICE_PATH = OUTPUT_DIR / "voice.wav"
FINAL_VIDEO_PATH = OUTPUT_DIR / "final_video.mp4"

VIDEO_WIDTH = 1280
VIDEO_HEIGHT = 720

# Limite de segurança para impedir vídeos absurdos tipo 6h
MAX_DURATION_SECONDS = 45 * 60

# Para teste rápido, podes meter 10 * 60
# MAX_DURATION_SECONDS = 10 * 60

ESPEAK_VOICE = "en-us"
ESPEAK_SPEED = "145"  # 130-155 é bom para narração. Não metas muito baixo.

FPS = "24"


# =========================
# HELPERS
# =========================

def clean_text(text: str) -> str:
    text = text or ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def safe_title(text: str) -> str:
    text = text or "horror_video"
    text = re.sub(r"[^a-zA-Z0-9 _-]", "", text)
    text = text.strip().replace(" ", "_")
    return text[:80] or "horror_video"


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


def get_font(size: int):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]

    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size=size)

    return ImageFont.load_default()


def make_frame(background_path: Path, thumbnail_text: str, title: str, output_path: Path):
    print("Creating video frame...")

    img = Image.open(background_path).convert("RGB")

    # Crop/resize para 16:9
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

    # Escurecer e dar vibe horror
    img = ImageEnhance.Brightness(img).enhance(0.45)
    img = ImageEnhance.Contrast(img).enhance(1.15)
    img = img.filter(ImageFilter.GaussianBlur(radius=0.6))

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Vinheta simples
    for i in range(180):
        alpha = int((i / 180) * 120)
        draw.rectangle(
            [i, i, VIDEO_WIDTH - i, VIDEO_HEIGHT - i],
            outline=(0, 0, 0, alpha),
        )

    img = Image.alpha_composite(img.convert("RGBA"), overlay)

    draw = ImageDraw.Draw(img)

    text = (thumbnail_text or "DO NOT OPEN").strip().upper()
    text = text[:42]

    font_big = get_font(68)
    font_small = get_font(28)

    # Quebra texto principal
    wrapped = textwrap.wrap(text, width=16)
    wrapped = wrapped[:3]
    main_text = "\n".join(wrapped)

    bbox = draw.multiline_textbbox((0, 0), main_text, font=font_big, spacing=10)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    x = (VIDEO_WIDTH - text_w) // 2
    y = (VIDEO_HEIGHT - text_h) // 2

    pad = 28
    draw.rounded_rectangle(
        [x - pad, y - pad, x + text_w + pad, y + text_h + pad],
        radius=18,
        fill=(0, 0, 0, 150),
    )

    # sombra
    draw.multiline_text(
        (x + 3, y + 3),
        main_text,
        font=font_big,
        fill=(0, 0, 0, 220),
        spacing=10,
        align="center",
    )

    # texto
    draw.multiline_text(
        (x, y),
        main_text,
        font=font_big,
        fill=(245, 245, 245, 255),
        spacing=10,
        align="center",
    )

    # título pequeno em baixo
    small_title = (title or "").strip()
    small_title = small_title[:90]

    if small_title:
        small_bbox = draw.textbbox((0, 0), small_title, font=font_small)
        small_w = small_bbox[2] - small_bbox[0]
        small_x = (VIDEO_WIDTH - small_w) // 2
        small_y = VIDEO_HEIGHT - 70

        draw.rounded_rectangle(
            [small_x - 16, small_y - 10, small_x + small_w + 16, small_y + 42],
            radius=10,
            fill=(0, 0, 0, 130),
        )
        draw.text(
            (small_x, small_y),
            small_title,
            font=font_small,
            fill=(230, 230, 230, 255),
        )

    img.convert("RGB").save(output_path, quality=92)
    print(f"Frame saved: {output_path}")


def generate_voice(script: str, output_path: Path):
    print("Generating voice with espeak-ng...")

    SCRIPT_PATH.write_text(script, encoding="utf-8")

    # IMPORTANTE:
    # -s 145 evita voz absurdamente lenta.
    # -f lê o texto do ficheiro, evita problemas com texto gigante como argumento.
    run([
        "espeak-ng",
        "-v", ESPEAK_VOICE,
        "-s", ESPEAK_SPEED,
        "-w", str(output_path),
        "-f", str(SCRIPT_PATH),
    ])

    if not output_path.exists() or output_path.stat().st_size < 1000:
        raise RuntimeError("Voice generation failed or output file is too small.")

    print(f"Voice saved: {output_path}")


def get_media_duration(path: Path) -> float:
    output = run_capture([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])

    try:
        duration = float(output)
    except ValueError:
        raise RuntimeError(f"Could not read duration from ffprobe output: {output}")

    return duration


def render_video(frame_path: Path, voice_path: Path, output_path: Path):
    audio_duration = get_media_duration(voice_path)
    duration = min(audio_duration + 1.5, MAX_DURATION_SECONDS)

    print(f"Audio duration: {audio_duration:.2f}s")
    print(f"Video duration used: {duration:.2f}s")
    print(f"Max duration: {MAX_DURATION_SECONDS}s")

    # Se o áudio for maior que 45 min, o vídeo corta aos 45 min.
    # Isto impede render infinito/absurdo.
    run([
        "ffmpeg",
        "-y",

        # imagem fixa
        "-loop", "1",
        "-framerate", FPS,
        "-i", str(frame_path),

        # voz
        "-i", str(voice_path),

        # limite duro
        "-t", str(duration),

        # vídeo leve e rápido
        "-vf",
        (
            f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},"
            "format=yuv420p"
        ),

        "-r", FPS,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "stillimage",
        "-crf", "28",

        "-c:a", "aac",
        "-b:a", "128k",

        "-movflags", "+faststart",
        "-shortest",
        str(output_path),
    ])

    if not output_path.exists() or output_path.stat().st_size < 1000:
        raise RuntimeError("Final video was not created or is too small.")

    print(f"Final video saved: {output_path}")
    print(f"Final video size: {output_path.stat().st_size / 1024 / 1024:.2f} MB")


# =========================
# MAIN
# =========================

def main():
    title = os.environ.get("VIDEO_TITLE", "Horror Story")
    script = os.environ.get("VIDEO_SCRIPT", "")
    background_url = os.environ.get("BACKGROUND_URL", "")
    thumbnail_text = os.environ.get("THUMBNAIL_TEXT", "DO NOT OPEN")

    title = title.strip() or "Horror Story"
    script = clean_text(script)

    if not script:
        raise ValueError("VIDEO_SCRIPT is empty. The GitHub workflow did not receive the script.")

    print("========== INPUT ==========")
    print(f"Title: {title}")
    print(f"Thumbnail text: {thumbnail_text}")
    print(f"Script chars: {len(script)}")
    print(f"Script words: {len(script.split())}")
    print(f"Background URL: {background_url}")
    print("===========================")

    download_background(background_url, BACKGROUND_PATH)
    make_frame(BACKGROUND_PATH, thumbnail_text, title, FRAME_PATH)
    generate_voice(script, VOICE_PATH)

    # Debug importante para confirmares que já não está a gerar 6h
    voice_duration = get_media_duration(VOICE_PATH)
    print(f"Measured voice duration: {voice_duration:.2f}s / {voice_duration / 60:.2f} minutes")

    if voice_duration > MAX_DURATION_SECONDS:
        print(
            f"WARNING: voice is longer than max duration. "
            f"It will be cut to {MAX_DURATION_SECONDS / 60:.1f} minutes."
        )

    render_video(FRAME_PATH, VOICE_PATH, FINAL_VIDEO_PATH)

    print("DONE")


if __name__ == "__main__":
    main()
