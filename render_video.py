import json
import os
import re
import subprocess
import textwrap
import urllib.request
from pathlib import Path

OUT = Path("output")
OUT.mkdir(exist_ok=True)

payload = json.loads(os.environ.get("PAYLOAD_JSON", "{}"))

title = payload.get("title", "Horror Story")
script = payload.get("script", "")
background_url = payload.get("background_url", "")
background_credit = payload.get("background_credit", "")
filename = payload.get("filename", "horror-video.mp4")
thumbnail_text = payload.get("thumbnail_text", "")
description = payload.get("description", "")
tags = payload.get("tags", [])
chapters = payload.get("chapters", [])

safe_filename = re.sub(r"[^A-Za-z0-9._ -]", "", filename).strip() or "horror-video.mp4"
if not safe_filename.lower().endswith(".mp4"):
    safe_filename += ".mp4"

(OUT / "title.txt").write_text(title, encoding="utf-8")
(OUT / "script.txt").write_text(script, encoding="utf-8")
(OUT / "description.txt").write_text(description + "\n\n" + background_credit, encoding="utf-8")
(OUT / "tags.txt").write_text(", ".join(map(str, tags)), encoding="utf-8")
(OUT / "chapters.json").write_text(json.dumps(chapters, ensure_ascii=False, indent=2), encoding="utf-8")

def run(cmd, check=True):
    print("+", " ".join(cmd))
    return subprocess.run(cmd, check=check)

# Download background image, with fallback.
bg_path = OUT / "background.jpg"
try:
    if background_url:
        urllib.request.urlretrieve(background_url, bg_path)
    else:
        raise RuntimeError("No background URL")
except Exception as e:
    print("Background download failed, using generated dark background:", e)
    run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=1920x1080:d=1",
        "-frames:v", "1", str(bg_path)
    ])

# Create narration.
# Fully free/offline fallback: espeak-ng. It is not as human as paid TTS,
# but costs nothing and works on GitHub-hosted runners.
wav_path = OUT / "narration.wav"
script_path = OUT / "script.txt"
run([
    "espeak-ng",
    "-v", "en-us",
    "-s", "132",
    "-p", "35",
    "-a", "150",
    "-f", str(script_path),
    "-w", str(wav_path)
])

# Optional title card image overlay is avoided to keep render robust.
# Audio bed: generated low-volume brown noise via ffmpeg, no copyright concerns.
video_path = OUT / safe_filename

filter_complex = (
    "[0:v]scale=1920:1080:force_original_aspect_ratio=increase,"
    "crop=1920:1080,format=yuv420p[v];"
    "[2:a]volume=0.025[a2];"
    "[1:a][a2]amix=inputs=2:duration=first:dropout_transition=2[a]"
)

run([
    "ffmpeg", "-y",
    "-loop", "1", "-i", str(bg_path),
    "-i", str(wav_path),
    "-f", "lavfi", "-i", "anoisesrc=color=brown:amplitude=0.12",
    "-filter_complex", filter_complex,
    "-map", "[v]",
    "-map", "[a]",
    "-shortest",
    "-r", "30",
    "-c:v", "libx264",
    "-preset", "veryfast",
    "-crf", "23",
    "-c:a", "aac",
    "-b:a", "160k",
    "-movflags", "+faststart",
    str(video_path)
])

print("DONE:", video_path)
