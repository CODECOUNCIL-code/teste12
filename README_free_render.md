# Free renderer for the n8n horror workflow

This replaces Shotstack with GitHub Actions.

## Files to put in your GitHub repo

Create a GitHub repository and add:

```text
.github/workflows/render.yml
render_video.py
```

Use the two files included in this package.

## GitHub token

Create a GitHub fine-grained token with permission to trigger Actions in that repo.

In n8n, paste it in every GitHub HTTP node header:

```text
Authorization = Bearer YOUR_GITHUB_TOKEN
```

## n8n CONFIG values

Set these in the CONFIG node:

```text
github_owner = your GitHub username or org
github_repo = your repo name
github_branch = main
github_workflow_file = render.yml
```

## What happens

n8n generates the story and image, then triggers GitHub Actions. GitHub uses:

- `espeak-ng` for free/offline TTS
- `ffmpeg` for video rendering
- generated brown-noise ambience, so no copyrighted music

The final video is uploaded as a GitHub Actions artifact.

## Important

Free TTS will not sound as human as ElevenLabs or paid TTS. This is a zero-credit workaround for testing the channel pipeline before spending money.
