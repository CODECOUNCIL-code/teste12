import asyncio
import os
import re
import textwrap
import subprocess
import tempfile
from pathlib import Path

import edge_tts
import requests
from PIL import Image, ImageEnhance, ImageFilter


# =========================
# CONFIG
# =========================

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

SCRIPT_PATH      = OUTPUT_DIR / "script.txt"
BACKGROUND_PATH  = OUTPUT_DIR / "background.jpg"
FRAME_PATH       = OUTPUT_DIR / "frame.jpg"
VOICE_MP3_PATH   = OUTPUT_DIR / "voice.mp3"
VOICE_WAV_PATH   = OUTPUT_DIR / "voice.wav"
SUBTITLE_PATH    = OUTPUT_DIR / "subtitles.srt"
FINAL_VIDEO_PATH = OUTPUT_DIR / "final_video.mp4"

VIDEO_WIDTH  = 1280
VIDEO_HEIGHT = 720
FPS          = 24

MAX_DURATION_SECONDS = 45 * 60

# Voz PT-PT.
# Masculina: pt-PT-DuarteNeural
# Feminina:  pt-PT-RaquelNeural
EDGE_VOICE   = os.environ.get("EDGE_VOICE",  "pt-PT-DuarteNeural")
EDGE_RATE    = os.environ.get("EDGE_RATE",   "-15%")   # mais lento = mais humano
EDGE_VOLUME  = os.environ.get("EDGE_VOLUME", "+0%")


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
    """Limpeza + substituições para melhor pronúncia."""
    text = clean_text(text)

    replacements = {
        "AI":    "inteligência artificial",
        "IA":    "inteligência artificial",
        "CCTV":  "circuito fechado de televisão",
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
    text = text.replace("\u200b", "").replace("\ufeff", "")

    return text.strip()


def split_into_tts_chunks(text: str):
    """
    Divide o texto em (chunk, pausa_ms) para TTS frase-a-frase.

    NOTA CRÍTICA: NÃO usamos SSML nem <break> tags.
    O edge-tts v7+ escapa automaticamente < e > antes de enviar,
    por isso SSML é lido literalmente como texto (a voz dizia
    "break time trezentos e cinquenta milissegundos").

    A solução correcta: gerar um MP3 por chunk + silêncio real
    via ffmpeg e concatenar. Assim as pausas são áudio real,
    não dependem do TTS.

    Pausas (ms):
      vírgula          →  320
      dois pontos      →  420
      ponto e vírgula  →  500
      travessão        →  480
      ponto/!/? frase  →  650
      reticências      →  900  (pausa dramática)
      parágrafo        → 1000
    """
    # Normaliza espaços
    text = re.sub(r"[ \t]+", " ", text).strip()

    # Padrões de split: (regex, pausa_ms)
    # Ordem: do mais específico para o mais genérico
    PAUSE_RULES = [
        (re.compile(r"…"),        900),
        (re.compile(r"\.\.\."),   900),
        (re.compile(r"[.!?]+"),   650),
        (re.compile(r"[;]"),      500),
        (re.compile(r"[—–]"),     480),
        (re.compile(r":"),        420),
        (re.compile(r","),        320),
    ]

    result   = []
    current  = ""
    i        = 0

    while i < len(text):
        # Parágrafo
        if text[i:i+2] in ("\n\n", "\r\n"):
            if current.strip():
                result.append((current.strip(), 1000))
            current = ""
            i += 2
            continue
        if text[i] == "\n":
            # Newline simples = pequena pausa
            if current.strip():
                result.append((current.strip(), 500))
            current = ""
            i += 1
            continue

        matched = False
        for pattern, pause_ms in PAUSE_RULES:
            m = pattern.match(text, i)
            if m:
                current += m.group()
                chunk = current.strip()
                if chunk:
                    result.append((chunk, pause_ms))
                current = ""
                i = m.end()
                matched = True
                break

        if not matched:
            current += text[i]
            i += 1

    # Resto sem pontuação no fim
    if current.strip():
        result.append((current.strip(), 400))

    # Filtra chunks vazios ou só pontuação
    result = [(c, p) for c, p in result if re.search(r"\w", c)]

    return result


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
        raise RuntimeError(f"ffprobe output inválido: {output}")


# =========================
# BACKGROUND & FRAME
# =========================

def download_background(url: str, output_path: Path):
    if not url:
        raise ValueError("BACKGROUND_URL está vazio")
    print(f"A descarregar background: {url}")
    response = requests.get(
        url, headers={"User-Agent": "Mozilla/5.0 n8n-renderer"}, timeout=60
    )
    response.raise_for_status()
    output_path.write_bytes(response.content)
    print(f"Background guardado: {output_path}")


def make_base_frame(background_path: Path, output_path: Path):
    """Frame base com look CCTV/horror (P&B, escuro, ligeiro blur)."""
    print("A criar frame base horror...")

    img      = Image.open(background_path).convert("RGB")
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
    print(f"Frame guardado: {output_path}")


# =========================
# VOICE — TTS CHUNK A CHUNK + SILÊNCIOS REAIS
# =========================

def make_silence_mp3(duration_ms: int, output_path: Path):
    """Gera um ficheiro MP3 de silêncio com a duração exacta em ms."""
    duration_s = duration_ms / 1000.0
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", "anullsrc=channel_layout=mono:sample_rate=24000",
        "-t", str(duration_s),
        "-acodec", "libmp3lame",
        "-b:a", "48k",
        str(output_path),
    ], check=True, capture_output=True)


async def generate_chunk_tts(text: str, voice: str, rate: str, volume: str, output_path: Path):
    """Gera áudio para um único chunk de texto."""
    communicate = edge_tts.Communicate(
        text=text,
        voice=voice,
        rate=rate,
        volume=volume,
    )
    await communicate.save(str(output_path))


async def generate_voice_with_real_pauses(script: str, output_path: Path):
    """
    Gera a narração completa com pausas REAIS entre frases.

    Fluxo:
      1. Divide o script em (chunk, pausa_ms)
      2. Para cada chunk: gera MP3 via edge-tts
      3. Após cada chunk: gera MP3 de silêncio com a pausa correcta
      4. Concatena tudo com ffmpeg
    """
    print("A gerar voz com pausas reais (chunk a chunk)...")
    print(f"Voz: {EDGE_VOICE}  Rate: {EDGE_RATE}")

    chunks = split_into_tts_chunks(script)
    print(f"Total de chunks: {len(chunks)}")

    tmp_dir    = Path(tempfile.mkdtemp(prefix="tts_chunks_"))
    file_list  = []

    for idx, (chunk_text, pause_ms) in enumerate(chunks):
        print(f"  Chunk {idx+1}/{len(chunks)} [{pause_ms}ms] {chunk_text[:60]}...")

        chunk_mp3   = tmp_dir / f"chunk_{idx:04d}.mp3"
        silence_mp3 = tmp_dir / f"silence_{idx:04d}.mp3"

        # Gera áudio do chunk
        await generate_chunk_tts(chunk_text, EDGE_VOICE, EDGE_RATE, EDGE_VOLUME, chunk_mp3)

        if not chunk_mp3.exists() or chunk_mp3.stat().st_size < 100:
            print(f"  AVISO: chunk {idx} falhou ou está vazio, a ignorar.")
            continue

        file_list.append(chunk_mp3)

        # Gera silêncio após o chunk (excepto talvez no último, mas não faz mal)
        make_silence_mp3(pause_ms, silence_mp3)
        file_list.append(silence_mp3)

    if not file_list:
        raise RuntimeError("Nenhum chunk de áudio foi gerado.")

    # Cria lista de ficheiros para o ffmpeg concat
    concat_list = tmp_dir / "concat.txt"
    with open(concat_list, "w", encoding="utf-8") as f:
        for fp in file_list:
            # ffmpeg concat demuxer precisa de caminhos absolutos ou relativos correctos
            f.write(f"file '{fp.resolve()}'\n")

    print(f"A concatenar {len(file_list)} ficheiros de áudio...")

    subprocess.run([
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        str(output_path),
    ], check=True)

    if not output_path.exists() or output_path.stat().st_size < 1000:
        raise RuntimeError("Ficheiro de voz final não foi criado ou está vazio.")

    print(f"Voz guardada: {output_path}  ({output_path.stat().st_size / 1024:.0f} KB)")


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
    Gera SRT sincronizado.
    Usa o script LIMPO (sem qualquer markup) para legendas legíveis.
    """
    print("A criar legendas...")

    # Divide em frases para as legendas
    sentences = re.split(r"(?<=[.!?…])\s+", script.strip())
    sentences = [s.strip() for s in sentences if s.strip()]

    # Agrupa frases curtas para evitar legendas de 2 palavras
    groups  = []
    current = ""

    for sentence in sentences:
        if len(current) + len(sentence) < 85:
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
        length = max(2.0, duration * (words / total_words))
        start  = cursor
        end    = min(duration, cursor + length)

        wrapped = "\n".join(textwrap.wrap(group, width=40))[:260]

        lines.append(str(idx))
        lines.append(f"{srt_timestamp(start)} --> {srt_timestamp(end)}")
        lines.append(wrapped)
        lines.append("")

        cursor = end
        if cursor >= duration:
            break

    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Legendas guardadas: {output_path}")


# =========================
# RENDER — FUNDO ANIMADO
# =========================

def render_video(frame_path: Path, voice_path: Path, subtitle_path: Path, output_path: Path):
    """
    Renderiza o vídeo final com fundo animado proceduralmente.

    O fundo anima com:
      - zoompan lento com drift sinusoidal → nunca salta, loop suave
      - flicker de brilho (geq) → simula CRT/CCTV real
      - scanlines horizontais finas (drawgrid)
      - ruído de película (noise)
      - vinheta
      - legendas em baixo
    """
    audio_duration = get_media_duration(voice_path)
    duration       = min(audio_duration + 1.5, MAX_DURATION_SECONDS)

    print(f"Duração do áudio: {audio_duration:.2f}s ({audio_duration / 60:.2f} min)")
    print(f"Duração do vídeo: {duration:.2f}s ({duration / 60:.2f} min)")

    subtitle_path_str = str(subtitle_path).replace("\\", "/").replace(":", "\\:")

    vf = (
        f"scale={VIDEO_WIDTH + 200}:{VIDEO_HEIGHT + 120}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH + 200}:{VIDEO_HEIGHT + 120},"

        # Zoom oscila entre 1.00 e 1.08, drift em x e y com frequências diferentes
        f"zoompan="
        f"z='1.00+0.08*sin(on/{FPS * 8}.0*PI)':"
        f"x='iw/2-(iw/zoom/2)+14*sin(on/{FPS * 11}.0)':"
        f"y='ih/2-(ih/zoom/2)+9*cos(on/{FPS * 13}.0)':"
        f"d=1:"
        f"s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:"
        f"fps={FPS},"

        f"format=gray,"
        f"eq=contrast=1.40:brightness=-0.06:saturation=0,"

        # Ruído de película
        f"noise=alls=12:allf=t+u,"

        # Scanlines CRT
        f"drawgrid=width=0:height=4:thickness=1:color=black@0.13,"

        # Flicker de brilho global (±3% a 2Hz)
        f"geq="
        f"lum='lum(X,Y)*(0.97+0.03*sin(2*PI*T*2))':"
        f"cb=128:cr=128,"

        f"vignette=PI/3.8,"
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

        f"format=yuv420p,"
        f"setsar=1"
    )

    run([
        "ffmpeg", "-y",
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

    if not output_path.exists() or output_path.stat().st_size < 1000:
        raise RuntimeError("Vídeo final não foi criado ou está vazio.")

    print(f"Vídeo final: {output_path}  ({output_path.stat().st_size / 1024 / 1024:.2f} MB)")


def verify_final_video(video_path: Path):
    print("A verificar vídeo final...")
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
        print(f"Não foi possível verificar o vídeo: {e}")


# =========================
# MAIN
# =========================

def main():
    title          = os.environ.get("VIDEO_TITLE",    "História de Terror").strip() or "História de Terror"
    script         = os.environ.get("VIDEO_SCRIPT",   "")
    background_url = os.environ.get("BACKGROUND_URL", "")
    thumbnail_text = os.environ.get("THUMBNAIL_TEXT", "NÃO ABRAS ESTA PORTA")

    script_clean = normalize_portuguese_text(script)

    if not script_clean:
        raise ValueError("VIDEO_SCRIPT está vazio. O workflow não recebeu o script.")

    print("========== INPUT ==========")
    print(f"Título:         {title}")
    print(f"Thumbnail:      {thumbnail_text}")
    print(f"Script chars:   {len(script_clean)}")
    print(f"Script words:   {len(script_clean.split())}")
    print(f"Background URL: {background_url}")
    print(f"Voz:            {EDGE_VOICE}  Rate: {EDGE_RATE}")
    print("===========================")

    SCRIPT_PATH.write_text(script_clean, encoding="utf-8")

    download_background(background_url, BACKGROUND_PATH)
    make_base_frame(BACKGROUND_PATH, FRAME_PATH)

    # TTS com pausas reais (chunk a chunk + silêncios ffmpeg)
    asyncio.run(generate_voice_with_real_pauses(script_clean, VOICE_MP3_PATH))
    convert_mp3_to_wav(VOICE_MP3_PATH, VOICE_WAV_PATH)

    voice_duration = get_media_duration(VOICE_WAV_PATH)
    print(f"Duração da voz: {voice_duration:.2f}s ({voice_duration / 60:.2f} min)")

    if voice_duration > MAX_DURATION_SECONDS:
        print(f"AVISO: voz será cortada aos {MAX_DURATION_SECONDS / 60:.1f} min")

    final_duration = min(voice_duration + 1.5, MAX_DURATION_SECONDS)

    create_subtitles(script_clean, final_duration, SUBTITLE_PATH)
    render_video(FRAME_PATH, VOICE_WAV_PATH, SUBTITLE_PATH, FINAL_VIDEO_PATH)
    verify_final_video(FINAL_VIDEO_PATH)

    print("DONE")


if __name__ == "__main__":
    main()
