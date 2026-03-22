from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import os, tempfile, threading, time, subprocess, json, glob

app = Flask(__name__)
CORS(app, origins="*")

DOWNLOAD_DIR = tempfile.mkdtemp()
COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies.txt')

FORMATS = {
    '1080': 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best',
    '1440': 'bestvideo[height<=1440][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1440]+bestaudio/best',
    '2160': 'bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=2160]+bestaudio/best',
    'best': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best',
    'mp3':  'bestaudio/best',
}

PLAYER_CLIENTS = [
    'android,web', 'ios,web', 'android_vr',
    'tv_embedded,web', 'mweb,android', 'web_creator,android',
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
    return "".join(c for c in t if c.isalnum() or c in " -_()[]").strip()[:n] or 'video'


@app.route('/health')
def health():
    # Also shows yt-dlp version to confirm it's installed
    try:
        v = subprocess.run(['yt-dlp', '--version'], capture_output=True, text=True, timeout=5)
        version = v.stdout.strip()
    except:
        version = 'not found'
    return jsonify({'status': 'ok', 'yt_dlp': version, 'cookies': os.path.exists(COOKIES_FILE)})


@app.route('/api/info', methods=['POST'])
def get_info():
    url = (request.json or {}).get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    last_error = 'Failed to fetch info'
    for attempt in range(6):
        try:
            result = subprocess.run(
                base_args(attempt) + ['--dump-json', '--skip-download', url],
                capture_output=True, text=True, timeout=40
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
            # capture actual error from stderr
            if result.stderr:
                last_error = result.stderr.strip().split('\n')[-1]
        except subprocess.TimeoutExpired:
            last_error = 'Timeout fetching info'
        except Exception as e:
            last_error = str(e)
        time.sleep(2)

    return jsonify({'error': last_error}), 400


@app.route('/api/download', methods=['POST'])
def download_video():
    url = (request.json or {}).get('url', '').strip()
    quality = (request.json or {}).get('quality', '1080')

    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    fmt = FORMATS.get(quality, FORMATS['1080'])
    is_mp3 = quality == 'mp3'
    # Use video title in filename directly via yt-dlp template
    out_tmpl = os.path.join(DOWNLOAD_DIR, '%(title)s_%(height)s.%(ext)s') if not is_mp3 else os.path.join(DOWNLOAD_DIR, '%(title)s.%(ext)s')

    last_error = 'Download failed'
    for attempt in range(6):
        try:
            args = base_args(attempt) + [
                '--format', fmt,
                '--output', out_tmpl,
                '--print', 'after_move:filepath',  # prints final filepath after download+merge
            ]

            if is_mp3:
                args += [
                    '--extract-audio',
                    '--audio-format', 'mp3',
                    '--audio-quality', '0',
                ]
            else:
                args += ['--merge-output-format', 'mp4']

            args.append(url)

            result = subprocess.run(args, capture_output=True, text=True, timeout=600)

            # --print filepath gives us the exact output file path
            filepath = None
            for line in result.stdout.strip().split('\n'):
                line = line.strip()
                if line and os.path.exists(line):
                    filepath = line
                    break

            # fallback: find newest file
            if not filepath:
                ext = '.mp3' if is_mp3 else '.mp4'
                matches = glob.glob(os.path.join(DOWNLOAD_DIR, f'*{ext}'))
                if matches:
                    filepath = max(matches, key=os.path.getmtime)

            if not filepath or not os.path.exists(filepath):
                last_error = result.stderr.strip().split('\n')[-1] if result.stderr else 'File not found'
                time.sleep(2)
                continue

            file_size = os.path.getsize(filepath)
            if file_size < 10000:
                last_error = 'Downloaded file too small — likely failed'
                os.remove(filepath)
                time.sleep(2)
                continue

            download_name = os.path.basename(filepath)
            mimetype = 'audio/mpeg' if is_mp3 else 'video/mp4'
            cleanup(filepath)

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
        time.sleep(2)

    return jsonify({'error': last_error}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
