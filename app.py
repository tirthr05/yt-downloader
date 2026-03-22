from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import os, tempfile, threading, time, subprocess, json, glob, random

app = Flask(__name__)
CORS(app, origins="*")

DOWNLOAD_DIR = tempfile.mkdtemp()
COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies.txt')

# STRICT format map — separate video+audio streams, exact resolution
# YouTube ONLY serves 1080p/2K/4K as DASH streams (separate video+audio)
# yt-dlp merges them with ffmpeg into a single mp4
FORMATS = {
    '1080': 'bestvideo[height=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height=1080]+bestaudio[ext=m4a]/bestvideo[height=1080]+bestaudio',
    '1440': 'bestvideo[height=1440][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height=1440]+bestaudio[ext=m4a]/bestvideo[height=1440]+bestaudio',
    '2160': 'bestvideo[height=2160][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height=2160]+bestaudio[ext=m4a]/bestvideo[height=2160]+bestaudio',
    'best': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio',
    'mp3':  'bestaudio[ext=m4a]/bestaudio/best',
}

PLAYER_CLIENTS = [
    'android,web',
    'ios,web',
    'android_vr',
    'tv_embedded,web',
    'mweb,android',
    'web_creator,android',
]

def base_args(attempt=0):
    args = [
        'yt-dlp',
        '--no-warnings',
        '--no-playlist',
        '--geo-bypass',
        '--geo-bypass-country', 'US',
        '--retries', '10',
        '--fragment-retries', '10',
        '--concurrent-fragments', '8',
        '--no-check-certificate',
        '--extractor-args', f'youtube:player_client={PLAYER_CLIENTS[attempt % len(PLAYER_CLIENTS)]}',
        '--add-header', 'Accept-Language:en-US,en;q=0.9',
    ]
    if os.path.exists(COOKIES_FILE):
        args += ['--cookies', COOKIES_FILE]
    return args

def cleanup(path, delay=180):
    def _del():
        time.sleep(delay)
        try: os.remove(path)
        except: pass
    threading.Thread(target=_del, daemon=True).start()

def safe_name(t, n=80):
    return "".join(c for c in t if c.isalnum() or c in " -_()[]").strip()[:n]

def find_file(vid_id, ext):
    # find most recently modified file matching video id and extension
    matches = glob.glob(os.path.join(DOWNLOAD_DIR, f'*{vid_id}*{ext}'))
    if matches:
        return max(matches, key=os.path.getmtime)
    # fallback: any recent file with that ext
    matches = glob.glob(os.path.join(DOWNLOAD_DIR, f'*{ext}'))
    if matches:
        return max(matches, key=os.path.getmtime)
    return None


@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'cookies': os.path.exists(COOKIES_FILE)})


@app.route('/api/info', methods=['POST'])
def get_info():
    url = (request.json or {}).get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    for attempt in range(6):
        try:
            result = subprocess.run(
                base_args(attempt) + ['--dump-json', '--skip-download', url],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and result.stdout.strip():
                info = json.loads(result.stdout.strip().split('\n')[0])
                formats = info.get('formats', [])
                heights = sorted(set(
                    f.get('height') for f in formats
                    if f.get('height') and f.get('vcodec') not in (None, 'none')
                ), reverse=True)
                return jsonify({
                    'title': info.get('title', 'Unknown'),
                    'thumbnail': info.get('thumbnail', ''),
                    'duration': info.get('duration', 0),
                    'uploader': info.get('uploader', 'Unknown'),
                    'available_heights': heights[:8],
                })
        except Exception as e:
            pass
        time.sleep(1.5)

    return jsonify({'error': 'Could not fetch video info. Try again.'}), 400


@app.route('/api/download', methods=['POST'])
def download_video():
    url = (request.json or {}).get('url', '').strip()
    quality = (request.json or {}).get('quality', '1080')

    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    fmt = FORMATS.get(quality, FORMATS['1080'])
    is_mp3 = quality == 'mp3'
    out_tmpl = os.path.join(DOWNLOAD_DIR, '%(id)s_%(height)s.%(ext)s')

    last_error = 'Unknown error'
    for attempt in range(6):
        try:
            args = base_args(attempt) + [
                '--format', fmt,
                '--merge-output-format', 'mp4' if not is_mp3 else 'mp4',
                '--output', out_tmpl,
            ]

            if is_mp3:
                args += [
                    '--extract-audio',
                    '--audio-format', 'mp3',
                    '--audio-quality', '0',  # 0 = best quality (VBR ~320kbps)
                ]

            args.append(url)

            result = subprocess.run(args, capture_output=True, text=True, timeout=600)

            if result.returncode != 0:
                last_error = result.stderr.strip().split('\n')[-1] if result.stderr else 'yt-dlp failed'
                time.sleep(2)
                continue

            # Get video ID from yt-dlp output
            vid_id = None
            for line in result.stderr.split('\n') + result.stdout.split('\n'):
                if 'Destination:' in line or '[download]' in line:
                    # extract id from filename
                    pass

            # Find the output file
            ext = '.mp3' if is_mp3 else '.mp4'

            # Most reliable: get the newest file in DOWNLOAD_DIR
            all_files = glob.glob(os.path.join(DOWNLOAD_DIR, f'*{ext}'))
            if not all_files:
                last_error = 'File not found after download'
                time.sleep(2)
                continue

            filepath = max(all_files, key=os.path.getmtime)
            file_size = os.path.getsize(filepath)

            if file_size < 10000:
                last_error = 'Downloaded file too small'
                os.remove(filepath)
                time.sleep(2)
                continue

            # Get actual resolution from filename
            basename = os.path.basename(filepath)
            # Try to extract height from filename like id_1080.mp4
            actual_height = quality
            parts = basename.replace('.mp4','').replace('.mp3','').split('_')
            for p in parts:
                if p.isdigit() and int(p) > 100:
                    actual_height = p
                    break

            # Build clean download filename
            # Re-fetch title quickly
            try:
                info_result = subprocess.run(
                    base_args(0) + ['--dump-json', '--skip-download', url],
                    capture_output=True, text=True, timeout=15
                )
                info = json.loads(info_result.stdout.strip().split('\n')[0])
                title = safe_name(info.get('title', 'video'))
            except:
                title = 'video'

            if is_mp3:
                download_name = f"{title}.mp3"
                mimetype = 'audio/mpeg'
            else:
                download_name = f"{title}_{actual_height}p.mp4"
                mimetype = 'video/mp4'

            cleanup(filepath, delay=180)

            def generate(path):
                with open(path, 'rb') as f:
                    while True:
                        chunk = f.read(512 * 1024)
                        if not chunk:
                            break
                        yield chunk

            return Response(
                stream_with_context(generate(filepath)),
                mimetype=mimetype,
                headers={
                    'Content-Disposition': f'attachment; filename="{download_name}"',
                    'Content-Length': str(file_size),
                    'X-Accel-Buffering': 'no',
                    'Cache-Control': 'no-cache',
                }
            )

        except subprocess.TimeoutExpired:
            last_error = 'Download timed out'
        except Exception as e:
            last_error = str(e)
        time.sleep(1.5)

    return jsonify({'error': last_error}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
