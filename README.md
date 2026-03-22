# YouTube Ultra HD Downloader

A clean YouTube downloader using yt-dlp with a premium dark UI.

---

## Quick Setup (Local)

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

> Also requires **ffmpeg** installed on your system:
> - **Windows**: Download from https://ffmpeg.org/download.html and add to PATH
> - **Mac**: `brew install ffmpeg`
> - **Linux**: `sudo apt install ffmpeg`

### 2. Run the backend
```bash
python app.py
```
The server starts at `http://localhost:5000`

### 3. Open the frontend
Open `index.html` in your browser (double-click it).

### 4. Connect backend in the UI
Click **"Backend URL (not set)"** in the top right and enter:
```
http://localhost:5000
```

---

## Hosting Options

### Option A — Local Network (easiest, free)
Run `python app.py` on your PC. Access from any device on your WiFi using your PC's local IP:
```
http://192.168.x.x:5000
```
Find your IP: `ipconfig` (Windows) or `ifconfig` (Mac/Linux).

### Option B — Railway (free tier, easiest cloud)
1. Push this folder to a GitHub repo
2. Go to https://railway.app → New Project → Deploy from GitHub
3. It auto-detects Python and deploys `app.py`
4. Set start command: `python app.py`
5. Get your public URL and use it as the backend URL

### Option C — Render (free tier)
1. Push to GitHub
2. Go to https://render.com → New Web Service
3. Connect your repo, set build command: `pip install -r requirements.txt`
4. Start command: `python app.py`
5. Free tier spins down after inactivity (30s cold start)

### Option C — VPS (DigitalOcean / Hetzner — ~$4–6/mo)
Best for permanent use:
```bash
# On your VPS:
git clone <your-repo>
cd yt-downloader
pip install -r requirements.txt
# Run with gunicorn for production:
pip install gunicorn
gunicorn -w 2 -b 0.0.0.0:5000 app:app
```
Use nginx as a reverse proxy + get a free domain from freenom.com.

---

## Notes
- Downloads are stored in a temp folder and auto-deleted after 2 minutes
- ffmpeg is required for merging video+audio (needed for 1080p+)
- MP3 extraction uses ffmpeg's audio postprocessor at 320kbps
