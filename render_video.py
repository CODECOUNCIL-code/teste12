import asyncio
import os
import re
import textwrap
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import edge_tts
import requests
from PIL import Image, ImageEnhance, ImageFilter


# =========================
# CONFIG
# =========================

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

SCRIPT_PATH = OUTPUT_DIR / "script.txt"
BACKGROUND_PATH = OUTPUT_DIR / "background"
FRAME_PATH = OUTPUT_DIR / "frame.jpg"
VOICE_FINAL_WAV_PATH = OUTPUT_DIR / "voice_final.wav"
SUBTITLE_PATH = OUTPUT_DIR / "subtitles.srt"
FINAL_VIDEO_PATH = OUTPUT_DIR / "final_video.mp4"
SEGMENTS_DIR = OUTPUT_DIR / "segments"
SEGMENTS_DIR.mkdir(exist_ok=True)

VIDEO_WIDTH = 1280
VIDEO_HEIGHT = 720
FPS = 24

# Segurança para nunca gerar vídeo absurdo.
MAX_DURATION_SECONDS = 45 * 60

# Para teste rápido:
# MAX_DURATION_SECONDS = 10 * 60

# Voz PT-PT.
EDGE_VOICE = os.environ.get("EDGE_VOICE", "pt-PT-DuarteNeural")
EDGE_RATE = os.environ.get("EDGE_RATE", "-14%")
EDGE_VOLUME = os.environ.get("EDGE_VOLUME", "+0%")
EDGE_PITCH = os.environ.get("EDGE_PITCH", "-2Hz")


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
    Limpa e prepara o texto para TTS em português de Portugal.
    Não reescreve a história; só remove lixo e ajuda a leitura.
    """
    text = clean_text(text)

    # Remove markdown/lixo comum.
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", text)
    text = text.replace("\u200b", "").replace("\ufeff", "")

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

    # Evita títulos/labels que estragam narração.
    text = re.sub(r"(?im)^\s*(scene|cena|capítulo|chapter)\s*\d+\s*[:\-].*$", "", text)
    text = re.sub(r"(?im)^\s*(intro|outro|conclusão)\s*[:\-].*$", "", text)

    # Pausas naturais em reticências e travessões.
    text = text.replace("...", "…")
    text = re.sub(r"\s+—\s+", " — ", text)

    return clean_text(text)


def split_sentences(text: str):
    """
    Divide por frases mantendo pontuação.
    """
    text = clean_text(text)
    parts = re.split(r"(?<=[.!?…])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def split_script_into_voice_segments(script: str, max_chars: int = 280):
    """
    Divide o guião em blocos pequenos para TTS.
    Isto melhora MUITO a respiração e a fluidez.
    Cada segmento recebe uma pausa real depois.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", script) if p.strip()]
    segments = []

    for paragraph in paragraphs:
        sentences = split_sentences(paragraph)
        current = ""

        for sentence in sentences:
            sentence = sentence.strip()

            if not current:
                current = sentence
            elif len(current) + 1 + len(sentence) <= max_chars:
                current += " " + sentence
            else:
                segments.append({
                    "text": current.strip(),
                    "pause": 0.45,
                })
                current = sentence

        if current:
            # Pausa maior no fim de parágrafo.
            segments.append({
                "text": current.strip(),
                "pause": 0.85,
            })

    # Remove pausa grande no último segmento.
    if segments:
        segments[-1]["pause"] = 0.2

    return segments


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


def guess_extension_from_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.lower()

    for ext in [".gif", ".mp4", ".webm", ".mov", ".jpg", ".jpeg", ".png"]:
        if path.endswith(ext):
            return ext

    return ".jpg"


def download_background(url: str) -> Path:
    if not url:
        raise ValueError("BACKGROUND_URL is empty")

    ext = guess_extension_from_url(url)
    output_path = BACKGROUND_PATH.with_suffix(ext)

    print(f"Downloading background: {url}")
    print(f"Background ext: {ext}")

    headers = {
        "User-Agent": "Mozilla/5.0 n8n-renderer"
    }

    response = requests.get(url, headers=headers, timeout=90)
    response.raise_for_status()

    output_path.write_bytes(response.content)
    print(f"Background saved: {output_path}")

    return output_path


def is_video_or_gif(path: Path) -> bool:
    return path.suffix.lower() in [".gif", ".mp4", ".webm", ".mov"]


def make_base_frame(background_path: Path, output_path: Path):
    print("Creating base horror frame from image...")

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

    # Base CCTV: preto e branco, escuro, desfocado.
    img = img.convert("L").convert("RGB")
    img = ImageEnhance.Brightness(img).enhance(0.52)
    img = ImageEnhance.Contrast(img).enhance(1.55)
    img = img.filter(ImageFilter.GaussianBlur(radius=1.05))

    img.save(output_path, quality=92)
    print(f"Frame saved: {output_path}")


async def synthesize_segment(text: str, mp3_path: Path):
    communicate = edge_tts.Communicate(
        text=text,
        voice=EDGE_VOICE,
        rate=EDGE_RATE,
        volume=EDGE_VOLUME,
        pitch=EDGE_PITCH,
    )
    await communicate.save(str(mp3_path))


async def generate_segmented_voice(script: str):
    """
    Gera voz por segmentos pequenos + silêncios reais.
    Resultado: voz menos corrida, mais respirada, mais humana.
    """
    print("Generating segmented voice with edge-tts...")
    print(f"Voice: {EDGE_VOICE}")
    print(f"Rate: {EDGE_RATE}")
    print(f"Volume: {EDGE_VOLUME}")
    print(f"Pitch: {EDGE_PITCH}")

    segments = split_script_into_voice_segments(script, max_chars=280)
    print(f"Voice segments: {len(segments)}")

    if not segments:
        raise RuntimeError("No voice segments found.")

    timeline = []
    concat_files = []
    cursor = 0.0

    for i, segment in enumerate(segments, start=1):
        text = segment["text"]
        pause = float(segment["pause"])

        base = SEGMENTS_DIR / f"seg_{i:04d}"
        mp3_path = base.with_suffix(".mp3")
        wav_path = base.with_suffix(".wav")
        silence_path = SEGMENTS_DIR / f"silence_{i:04d}.wav"

        print(f"Synth segment {i}/{len(segments)} chars={len(text)} pause={pause}")

        await synthesize_segment(text, mp3_path)

        if not mp3_path.exists() or mp3_path.stat().st_size < 500:
            raise RuntimeError(f"TTS failed for segment {i}")

        # Converte cada segmento para WAV compatível.
        run([
            "ffmpeg",
            "-y",
            "-i", str(mp3_path),
            "-ar", "44100",
            "-ac", "2",
            "-sample_fmt", "s16",
            str(wav_path),
        ])

        duration = get_media_duration(wav_path)

        timeline.append({
            "index": i,
            "text": text,
            "start": cursor,
            "end": cursor + duration,
            "duration": duration,
        })

        cursor += duration

        concat_files.append(wav_path)

        # Gera silêncio depois do segmento.
        if pause > 0:
            run([
                "ffmpeg",
                "-y",
                "-f", "lavfi",
                "-i", "anullsrc=r=44100:cl=stereo",
                "-t", str(pause),
                "-sample_fmt", "s16",
                str(silence_path),
            ])
            concat_files.append(silence_path)
            cursor += pause

    # Cria lista concat.
    concat_list_path = SEGMENTS_DIR / "concat_list.txt"
    lines = []
    for path in concat_files:
        safe = str(path).replace("'", "'\\''")
        lines.append(f"file '{safe}'")

    concat_list_path.write_text("\n".join(lines), encoding="utf-8")

    # Concatena tudo num WAV final.
    run([
        "ffmpeg",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_list_path),
        "-c", "copy",
        str(VOICE_FINAL_WAV_PATH),
    ])

    if not VOICE_FINAL_WAV_PATH.exists() or VOICE_FINAL_WAV_PATH.stat().st_size < 1000:
        raise RuntimeError("Final voice file was not created.")

    print(f"Final voice saved: {VOICE_FINAL_WAV_PATH}")
    print(f"Final voice duration: {get_media_duration(VOICE_FINAL_WAV_PATH):.2f}s")

    return timeline


def create_subtitles_from_timeline(timeline, output_path: Path):
    """
    Cria legendas alinhadas com os segmentos reais da voz.
    Muito melhor do que tentar estimar pela contagem de palavras.
    """
    print("Creating subtitles from real voice timeline...")

    lines = []

    for item in timeline:
        idx = item["index"]
        start = item["start"]
        end = item["end"]
        text = item["text"]

        # Legenda curta, 2 linhas no máximo sempre que possível.
        wrapped_lines = textwrap.wrap(text, width=46)
        if len(wrapped_lines) > 2:
            wrapped = "\n".join(wrapped_lines[:2])
        else:
            wrapped = "\n".join(wrapped_lines)

        # Segurança para não meter blocos gigantes.
        wrapped = wrapped[:180]

        lines.append(str(idx))
        lines.append(f"{srt_timestamp(start)} --> {srt_timestamp(end)}")
        lines.append(wrapped)
        lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Subtitles saved: {output_path}")


def build_common_filter(subtitle_path: Path) -> str:
    subtitle_path_str = str(subtitle_path).replace("\\", "/").replace(":", "\\:")

    # Filtro estável e compatível.
    # Sem drawbox com mod(), porque isso partiu no GitHub.
    return (
        f"format=gray,"
        f"eq=contrast=1.35:brightness=-0.055:saturation=0,"
        f"noise=alls=14:allf=t+u,"
        f"vignette=PI/4,"
        f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.10:t=fill,"
        f"subtitles='{subtitle_path_str}':"
        f"force_style='FontName=DejaVu Sans,"
        f"FontSize=23,"
        f"PrimaryColour=&H00FFFFFF,"
        f"OutlineColour=&H00000000,"
        f"BackColour=&H80000000,"
        f"BorderStyle=3,"
        f"Outline=1,"
        f"Shadow=0,"
        f"Alignment=2,"
        f"MarginV=34',"
        f"format=yuv420p,"
        f"setsar=1"
    )


def render_video_from_image(frame_path: Path, voice_path: Path, subtitle_path: Path, output_path: Path):
    audio_duration = get_media_duration(voice_path)
    duration = min(audio_duration + 0.5, MAX_DURATION_SECONDS)

    print(f"Audio duration: {audio_duration:.2f}s / {audio_duration / 60:.2f} min")
    print(f"Video duration used: {duration:.2f}s / {duration / 60:.2f} min")

    common = build_common_filter(subtitle_path)

    # Movimento tipo GIF: zoom/pan lento na imagem.
    vf = (
        f"scale={VIDEO_WIDTH + 180}:{VIDEO_HEIGHT + 100}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH + 180}:{VIDEO_HEIGHT + 100},"
        f"zoompan="
        f"z='min(zoom+0.00018,1.10)':"
        f"x='iw/2-(iw/zoom/2)+7*sin(on/41)':"
        f"y='ih/2-(ih/zoom/2)+4*cos(on/47)':"
        f"d=1:"
        f"s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:"
        f"fps={FPS},"
        f"{common}"
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
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-profile:v", "high",
        "-level", "4.0",
        "-preset", "veryfast",
        "-crf", "24",

        "-c:a", "aac",
        "-b:a", "160k",
        "-ar", "44100",
        "-ac", "2",

        "-movflags", "+faststart",
        "-shortest",
        str(output_path),
    ])


def render_video_from_loop(background_path: Path, voice_path: Path, subtitle_path: Path, output_path: Path):
    audio_duration = get_media_duration(voice_path)
    duration = min(audio_duration + 0.5, MAX_DURATION_SECONDS)

    print(f"Audio duration: {audio_duration:.2f}s / {audio_duration / 60:.2f} min")
    print(f"Video duration used: {duration:.2f}s / {duration / 60:.2f} min")
    print(f"Using animated background loop: {background_path}")

    common = build_common_filter(subtitle_path)

    # Se vier GIF/MP4/WebM, usamos em loop.
    vf = (
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},"
        f"fps={FPS},"
        f"{common}"
    )

    run([
        "ffmpeg",
        "-y",

        "-stream_loop", "-1",
        "-i", str(background_path),

        "-i", str(voice_path),

        "-t", str(duration),

        "-vf", vf,

        "-r", str(FPS),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-profile:v", "high",
        "-level", "4.0",
        "-preset", "veryfast",
        "-crf", "24",

        "-c:a", "aac",
        "-b:a", "160k",
        "-ar", "44100",
        "-ac", "2",

        "-movflags", "+faststart",
        "-shortest",
        str(output_path),
    ])


def verify_final_video(video_path: Path):
    print("Verifying final video...")

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

    if "pix_fmt=yuv420p" not in video_info:
        raise RuntimeError("Video is not yuv420p. Windows compatibility may fail.")

    if "codec_name=h264" not in video_info:
        raise RuntimeError("Video is not H.264.")

    if "codec_name=aac" not in audio_info:
        raise RuntimeError("Audio is not AAC.")


# =========================
# MAIN
# =========================

def main():
    title = os.environ.get("VIDEO_TITLE", "História de Terror").strip() or "História de Terror"
    script = os.environ.get("VIDEO_SCRIPT", "")
    background_url = os.environ.get("BACKGROUND_URL", "")
    thumbnail_text = os.environ.get("THUMBNAIL_TEXT", "NÃO LEIAS AS REGRAS")

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
    print(f"Rate: {EDGE_RATE}")
    print(f"Pitch: {EDGE_PITCH}")
    print("===========================")

    SCRIPT_PATH.write_text(script, encoding="utf-8")

    background_path = download_background(background_url)

    timeline = asyncio.run(generate_segmented_voice(script))
    create_subtitles_from_timeline(timeline, SUBTITLE_PATH)

    voice_duration = get_media_duration(VOICE_FINAL_WAV_PATH)
    print(f"Measured voice duration: {voice_duration:.2f}s / {voice_duration / 60:.2f} minutes")

    if voice_duration > MAX_DURATION_SECONDS:
        print(
            f"WARNING: voice is longer than max duration. "
            f"It will be cut to {MAX_DURATION_SECONDS / 60:.1f} minutes."
        )

    if is_video_or_gif(background_path):
        render_video_from_loop(background_path, VOICE_FINAL_WAV_PATH, SUBTITLE_PATH, FINAL_VIDEO_PATH)
    else:
        make_base_frame(background_path, FRAME_PATH)
        render_video_from_image(FRAME_PATH, VOICE_FINAL_WAV_PATH, SUBTITLE_PATH, FINAL_VIDEO_PATH)

    if not FINAL_VIDEO_PATH.exists() or FINAL_VIDEO_PATH.stat().st_size < 1000:
        raise RuntimeError("Final video was not created or is too small.")

    print(f"Final video saved: {FINAL_VIDEO_PATH}")
    print(f"Final video size: {FINAL_VIDEO_PATH.stat().st_size / 1024 / 1024:.2f} MB")

    verify_final_video(FINAL_VIDEO_PATH)

    print("DONE")


if __name__ == "__main__":
    main()
