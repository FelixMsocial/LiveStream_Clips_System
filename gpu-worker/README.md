# ClipFactory GPU worker

Runs on the local NVIDIA box under `systemd` (Linux) or NSSM (Windows). Pulls
jobs from the `clip-edit` Cloudflare Queue, runs the media pipeline (Gemini →
Deepgram → FFmpeg NVENC → Claude × 3), and patches D1 back through the
approval-worker's internal API.

## Install

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux:   source .venv/bin/activate
pip install -r requirements.txt
```

## Configure

Copy `../.env.example` to `gpu-worker/.env` and fill in the `CF_*`, `R2_*`,
`GEMINI_*`, `ANTHROPIC_*`, `DEEPGRAM_*`, and `GPU_*` values. Make sure
`cloudflared tunnel` is running to give the worker outbound-only connectivity
back to the approval-worker.

## Run

```bash
python -m clipfactory_gpu.main
```

## Run as a service

### Windows (NSSM)

```powershell
nssm install clipfactory-gpu "C:\path\to\.venv\Scripts\python.exe" "-m" "clipfactory_gpu.main"
nssm set clipfactory-gpu AppDirectory "D:\ClipFactory_Livestream_Clip_System\gpu-worker"
nssm start clipfactory-gpu
```

### Linux (systemd)

```ini
# /etc/systemd/system/clipfactory-gpu.service
[Unit]
Description=ClipFactory GPU pipeline worker
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/clipfactory/gpu-worker
EnvironmentFile=/opt/clipfactory/gpu-worker/.env
ExecStart=/opt/clipfactory/gpu-worker/.venv/bin/python -m clipfactory_gpu.main
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
