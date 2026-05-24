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

SCRIPT_PATH        = OUTPUT_DIR / "script.txt"
BACKGROUND_PATH    = OUTPUT_DIR / "background.jpg"
FRAME_PATH         = OUTPUT_DIR / "frame.jpg"
VOICE_MP3_PATH     = OUTPUT_DIR / "voice.mp3"
VOICE_WAV_PATH     = OUTPUT_DIR / "voice.wav"
SUBTITLE_PATH      = OUTPUT_DIR / "subtitles.srt"
FINAL_VIDEO_PATH   = OUTPUT_DIR / "final_video.mp4"

VIDEO_WIDTH  = 1280
VIDEO_HEIGHT = 720
FPS          = 24

MAX_DURATION_SECONDS = 45 * 60

# Voz PT-PT.
# Masculina: pt-PT-DuarteNeural
# Feminina:  pt-PT-RaquelNeural
EDGE_VOICE   = os.environ.get("EDGE_VOICE",  "pt-PT-DuarteNeural")
EDGE_RATE    = os.environ.get("EDGE_RATE",   "-12%")   # ligeiramente mais lento → mais humano
EDGE_VOLUME  = os.environ.get("EDGE_VOLUME", "+0%")


# =========================
# HELPERS — LIMPEZA DE TEXTO
# =========================

def clean_text(text: str) -> str:
    text = text or ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def inject_ssml_pauses(text: str) -> str:
    """
    Converte pontuação em tags SSML <break> para o edge-tts.
    Isto dá à voz respiração e pausas naturais — muito menos robótica.

    edge-tts aceita SSML inline desde que o texto comece por <speak>.
    Durações testadas para pt-PT-DuarteNeural:
      - vírgula      → 350 ms
      - ponto        → 600 ms
      - ponto e vírgula → 500 ms
      - dois pontos  → 500 ms
      - reticências  → 900 ms  (pausa dramática)
      - parágrafo    → 800 ms
      - travessão    → 450 ms
    """

    # Escapa caracteres XML antes de injetar tags
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")

    # Ordem importa: mais específico primeiro
    text = re.sub(r"…",  lambda m: m.group() + '<break time="900ms"/>', text)
    text = re.sub(r"\.\.\.", lambda m: m.group() + '<break time="900ms"/>', text)
    text = re.sub(r"—",  lambda m: m.group() + '<break time="450ms"/>', text)
    text = re.sub(r"–",  lambda m: m.group() + '<break time="450ms"/>', text)
    text = re.sub(r";",  lambda m: m.group() + '<break time="500ms"/>', text)
    text = re.sub(r":",  lambda m: m.group() + '<break time="500ms"/>', text)
    text = re.sub(r",",  lambda m: m.group() + '<break time="350ms"/>', text)

    # Pausa entre parágrafos
    text = re.sub(r"\n\n+", '\n<break time="800ms"/>\n', text)

    # Ponto final / exclamação / interrogação → pausa longa
    text = re.sub(r"([.!?])\s", lambda m: m.group(1) + '<break time="600ms"/> ', text)

    return f"<speak>{text}</speak>"


def normalize_portuguese_text(text: str) -> str:
    """
    Limpeza + substituições antes de gerar o SSML.
    """
    text = clean_text(text)

    replacements = {
        "AI":    "inteligência artificial",
        "IA":    "inteligência artificial",
        "CCTV":  "circuito fechado",
        "VHS":   "vê agá ésse",
        "24/7":  "vinte e quatro sobre sete",
        "24h":   "vinte e quatro horas",
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

    # Remove markdown acidental
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", text)

    # Remove caracteres invisíveis
    text = text.replace("\u200b", "")
    text = text.replace("\ufeff", "")

    return text.strip()


def split_sentences(text: str):
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def srt_timestamp(seconds: float) -> str:
    seconds = max(0, seconds)
    ms = int((seconds - int(seconds)) * 1000)
    s  = int(seconds) % 60
    m  = (int(seconds) // 60) % 60
    h  = int(seconds) // 3600
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def run(cmd, check=True):
    print("\nRUN:", " ".join(str(x) for x in cmd))
    return subprocess.run(cmd, check=check)


def run_capture(cmd):
    print("\nRUN:", " ".join(str(x) for x in cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def get_media_duration(path: Path) -> float:
    output = run_capture([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])
    try:
        return float(output)
    except ValueError:
        raise RuntimeError(f"Could not read duration from ffprobe output: {output}")


# =========================
# BACKGROUND & FRAME
# =========================

def download_background(url: str, output_path: Path):
    if not url:
        raise ValueError("BACKGROUND_URL is empty")
    print(f"Downloading background: {url}")
    response = requests.get(url, headers={"User-Agent": "Mozilla/5.0 n8n-renderer"}, timeout=60)
    response.raise_for_status()
    output_path.write_bytes(response.content)
    print(f"Background saved: {output_path}")


def make_base_frame(background_path: Path, output_path: Path):
    """
    Prepara o frame base com look CCTV/horror.
    Este frame é usado como INPUT para o ffmpeg gerar o vídeo animado.
    """
    print("Creating base horror frame...")

    img = Image.open(background_path).convert("RGB")
    src_w, src_h = img.size
    target_ratio = VIDEO_WIDTH / VIDEO_HEIGHT
    src_ratio    = src_w / src_h

    if src_ratio > target_ratio:
        new_w = int(src_h * target_ratio)
        left  = (src_w - new_w) // 2
        img   = img.crop((left, 0, left + new_w, src_h))
    else:
        new_h = int(src_w / target_ratio)
        top   = (src_h - new_h) // 2
        img   = img.crop((0, top, src_w, top + new_h))

    img = img.resize((VIDEO_WIDTH, VIDEO_HEIGHT), Image.LANCZOS)
    img = img.convert("L").convert("RGB")
    img = ImageEnhance.Brightness(img).enhance(0.52)
    img = ImageEnhance.Contrast(img).enhance(1.55)
    img = img.filter(ImageFilter.GaussianBlur(radius=1.15))
    img.save(output_path, quality=92)
    print(f"Frame saved: {output_path}")


# =========================
# VOICE (TTS com SSML)
# =========================

async def generate_voice_edge_tts(raw_script: str, output_path: Path):
    """
    Gera voz com edge-tts usando SSML para pausas naturais.
    """
    print("Generating voice with edge-tts (SSML pauses)...")
    print(f"Voice: {EDGE_VOICE}  Rate: {EDGE_RATE}  Volume: {EDGE_VOLUME}")

    ssml_script = inject_ssml_pauses(raw_script)
    print(f"SSML chars: {len(ssml_script)}")

    communicate = edge_tts.Communicate(
        text=ssml_script,
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
        "ffmpeg", "-y",
        "-i", str(mp3_path),
        "-ar", "44100", "-ac", "2",
        str(wav_path),
    ])


# =========================
# SUBTÍTULOS
# =========================

def create_subtitles(script: str, duration: float, output_path: Path):
    """
    Gera ficheiro SRT com grupos de palavras sincronizados.
    Usa o script LIMPO (sem SSML) para que as legendas sejam legíveis.
    """
    print("Creating subtitles...")

    sentences   = split_sentences(script)
    groups      = []
    current     = ""

    for sentence in sentences:
        if len(current) + len(sentence) < 90:
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
    cursor      = 0.0
    lines       = []

    for idx, group in enumerate(groups, start=1):
        words  = len(group.split())
        length = max(2.2, duration * (words / total_words))
        start  = cursor
        end    = min(duration, cursor + length)

        wrapped = "\n".join(textwrap.wrap(group, width=42))[:260]

        lines.append(str(idx))
        lines.append(f"{srt_timestamp(start)} --> {srt_timestamp(end)}")
        lines.append(wrapped)
        lines.append("")

        cursor = end
        if cursor >= duration:
            break

    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Subtitles saved: {output_path}")


# =========================
# RENDER — FUNDO ANIMADO EM LOOP
# =========================

def render_video(frame_path: Path, voice_path: Path, subtitle_path: Path, output_path: Path):
    """
    Renderiza o vídeo final com fundo animado (sem GIF externo).

    Em vez de imagem estática, o ffmpeg anima o frame com:
      - zoompan lento + drift sinusoidal (loop suave)
      - scanlines a piscar (efeito CRT/CCTV)
      - ruído de película
      - vinheta
      - legendas em baixo

    O loop é conseguido com -stream_loop -1 na imagem de entrada,
    que a repete indefinidamente, e o zoompan usa funções trigonométricas
    baseadas em `on` (número do frame), garantindo que o movimento é
    contínuo e sem saltos.
    """
    audio_duration = get_media_duration(voice_path)
    duration       = min(audio_duration + 1.5, MAX_DURATION_SECONDS)

    print(f"Audio duration: {audio_duration:.2f}s / {audio_duration / 60:.2f} min")
    print(f"Video duration: {duration:.2f}s / {duration / 60:.2f} min")

    subtitle_path_str = str(subtitle_path).replace("\\", "/").replace(":", "\\:")

    # ------------------------------------------------------------------
    # filtro de vídeo
    #
    # Animação procedural baseada em funções trigonométricas de `on`:
    #   - zoom oscila entre 1.0 e 1.08 a cada ~8 s
    #   - drift x e y com frequências ligeiramente diferentes → nunca repete exactamente
    #   - scanlines: drawgrid com alpha animado por mod(on,3) → pisca a cada 3 frames
    #   - noise: alls=14 (grão suave)
    #   - vignette fixa
    # ------------------------------------------------------------------
    vf = (
        # Dá margem extra para o zoompan não mostrar bordas pretas
        f"scale={VIDEO_WIDTH + 200}:{VIDEO_HEIGHT + 120}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH + 200}:{VIDEO_HEIGHT + 120},"

        # Movimento em loop suave — zoom oscila entre 1.00 e 1.08
        f"zoompan="
        f"z='1.00+0.08*sin(on/{FPS * 8}.0*PI)':"
        f"x='iw/2-(iw/zoom/2)+14*sin(on/{FPS * 11}.0)':"
        f"y='ih/2-(ih/zoom/2)+9*cos(on/{FPS * 13}.0)':"
        f"d=1:"
        f"s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:"
        f"fps={FPS},"

        # Preto e branco com parâmetros de contraste
        f"format=gray,"
        f"eq=contrast=1.40:brightness=-0.06:saturation=0,"

        # Ruído de película (grão suave, não agressivo)
        f"noise=alls=12:allf=t+u,"

        # Scanlines CRT — linhas horizontais finas a piscar levemente
        # drawgrid: linhas a cada 4px, espessura 1, alpha baixo
        # o alpha varia entre 0.08 e 0.18 consoante o frame (mod 6)
        # Nota: expr com mod em drawgrid não é suportado directamente,
        # então usamos um alpha fixo subtil — para piscar usamos geq abaixo
        f"drawgrid=width=0:height=4:thickness=1:color=black@0.13,"

        # Leve flicker de brilho global (simula CRT/CCTV real)
        # geq: multiplica cada pixel por um factor que oscila ±3% a 2Hz
        f"geq="
        f"lum='lum(X,Y)*(0.97+0.03*sin(2*PI*T*2))':"
        f"cb=128:cr=128,"

        # Vinheta
        f"vignette=PI/3.8,"

        # Overlay escuro nas bordas
        f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.10:t=fill,"

        # Legendas
        f"subtitles='{subtitle_path_str}':"
        f"force_style='"
        f"FontName=DejaVu Sans,"
        f"FontSize=23,"
        f"PrimaryColour=&H00FFFFFF,"
        f"OutlineColour=&H00000000,"
        f"BackColour=&H90000000,"
        f"BorderStyle=3,"
        f"Outline=1,"
        f"Shadow=0,"
        f"Alignment=2,"
        f"MarginV=38',"

        # Formato final compatível
        f"format=yuv420p,"
        f"setsar=1"
    )

    run([
        "ffmpeg", "-y",

        # Input: imagem em loop infinito
        "-loop", "1",
        "-framerate", str(FPS),
        "-i", str(frame_path),

        # Input: áudio
        "-i", str(voice_path),

        # Duração total
        "-t", str(duration),

        "-vf", vf,
        "-r", str(FPS),

        # Codec vídeo compatível
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-profile:v", "high",
        "-level", "4.0",
        "-preset", "veryfast",
        "-crf", "24",

        # Codec áudio
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

    print(f"Final video: {output_path}  ({output_path.stat().st_size / 1024 / 1024:.2f} MB)")


def verify_final_video(video_path: Path):
    print("Verifying final video...")
    try:
        video_info = run_capture([
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,profile,pix_fmt,width,height,r_frame_rate",
            "-of", "default=noprint_wrappers=1",
            str(video_path),
        ])
        audio_info = run_capture([
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name,sample_rate,channels",
            "-of", "default=noprint_wrappers=1",
            str(video_path),
        ])
        print("VIDEO INFO:\n", video_info)
        print("AUDIO INFO:\n", audio_info)
    except Exception as e:
        print(f"Could not verify final video: {e}")


# =========================
# MAIN
# =========================

def main():
    title          = os.environ.get("VIDEO_TITLE",      "História de Terror").strip() or "História de Terror"
    script         = os.environ.get("VIDEO_SCRIPT",     "")
    background_url = os.environ.get("BACKGROUND_URL",   "")
    thumbnail_text = os.environ.get("THUMBNAIL_TEXT",   "NÃO ABRAS ESTA PORTA")

    # Normaliza o texto ANTES de gerar SSML
    script_clean = normalize_portuguese_text(script)

    if not script_clean:
        raise ValueError("VIDEO_SCRIPT is empty. The GitHub workflow did not receive the script.")

    print("========== INPUT ==========")
    print(f"Title:          {title}")
    print(f"Thumbnail text: {thumbnail_text}")
    print(f"Script chars:   {len(script_clean)}")
    print(f"Script words:   {len(script_clean.split())}")
    print(f"Background URL: {background_url}")
    print(f"Voice:          {EDGE_VOICE}")
    print("===========================")

    SCRIPT_PATH.write_text(script_clean, encoding="utf-8")

    download_background(background_url, BACKGROUND_PATH)
    make_base_frame(BACKGROUND_PATH, FRAME_PATH)

    # TTS com SSML (pausas naturais)
    asyncio.run(generate_voice_edge_tts(script_clean, VOICE_MP3_PATH))
    convert_mp3_to_wav(VOICE_MP3_PATH, VOICE_WAV_PATH)

    voice_duration = get_media_duration(VOICE_WAV_PATH)
    print(f"Voice duration: {voice_duration:.2f}s / {voice_duration / 60:.2f} min")

    if voice_duration > MAX_DURATION_SECONDS:
        print(f"WARNING: voice will be cut to {MAX_DURATION_SECONDS / 60:.1f} min")

    final_duration = min(voice_duration + 1.5, MAX_DURATION_SECONDS)

    # Legendas usam o script LIMPO (sem tags SSML)
    create_subtitles(script_clean, final_duration, SUBTITLE_PATH)

    render_video(FRAME_PATH, VOICE_WAV_PATH, SUBTITLE_PATH, FINAL_VIDEO_PATH)
    verify_final_video(FINAL_VIDEO_PATH)

    print("DONE")


if __name__ == "__main__":
    main()
