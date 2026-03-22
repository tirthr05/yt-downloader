from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import os, tempfile, threading, time, subprocess, json, glob, random

app = Flask(__name__)
CORS(app, origins="*")

DOWNLOAD_DIR = tempfile.mkdtemp()
COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies.txt')

CLIENTS = ['android', 'ios', 'web', 'android_vr', 'tv_embedded', 'mweb']

# Spoof X-Forwarded-For from different countries - free geo bypass
XFF_IPS = [
    '212.58.244.20',   # UK (BBC)
    '194.25.134.80',   # Germany
    '31.13.93.35',     # Netherlands
    '203.0.113.1',     # Japan
    '8.8.8.8',         # US
    '1.1.1.1',         # Australia
    '185.60.216.35',   # France
    '77.88.8.8',       # Russia
    '168.126.63.1',    # South Korea
    '203.112.2.2',     # Bangladesh
]

def base_args(client='android', xff=None):
    args = [
        'yt-dlp',
        '--no-warnings',
        '--no-playlist',
        '--no-check-certificate',
        '--concurrent-fragments', '8',
        '--retries', '3',
        '--fragment-retries', '3',
        '--extractor-args', f'youtube:player_client={client}',
        '--add-header', 'Accept-Language:en-US,en;q=0.9',
        '--add-header', 'Accept:text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        '--xff', xff or random.choice(XFF_IPS),
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

def find_newest(ext):
    files = glob.glob(os.path.join(DOWNLOAD_DIR, f'*{ext}'))
    return max(files, key=os.path.getmtime) if files else None


@app.route('/health')
def health():
    try:
        v = subprocess.run(['yt-dlp', '--version'], capture_output=True, text=True, timeout=5)
        return jsonify({'status': 'ok', 'yt_dlp': v.stdout.strip(), 'cookies': os.path.exists(COOKIES_FILE)})
    except:
        return jsonify({'status': 'error'})


@app.route('/api/info', methods=['POST'])
def get_info():
    url = (request.json or {}).get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    last_error = 'Failed'
    # Try every client + random xff combination
    for client in CLIENTS:
        for xff in random.sample(XFF_IPS, 3):
            try:
                r = subprocess.run(
                    base_args(client, xff) + [
                        '--skip-download',
                        '--print', 'title',
                        '--print', 'thumbnail',
                        '--print', 'duration',
                        '--print', 'uploader',
                        url
                    ],
                    capture_output=True, text=True, timeout=15
                )
                if r.returncode == 0 and r.stdout.strip():
                    lines = r.stdout.strip().split('\n')
                    return jsonify({
                        'title':     lines[0] if len(lines) > 0 else 'Unknown',
                        'thumbnail': lines[1] if len(lines) > 1 else '',
                        'duration':  int(float(lines[2])) if len(lines) > 2 and lines[2].replace('.','').isdigit() else 0,
                        'uploader':  lines[3] if len(lines) > 3 else 'Unknown',
                    })
                if r.stderr:
                    last_error = r.stderr.strip().split('\n')[-1]
            except subprocess.TimeoutExpired:
                last_error = 'Timeout'
            except Exception as e:
                last_error = str(e)

    return jsonify({'error': last_error}), 400


@app.route('/api/download', methods=['POST'])
def download_video():
    url     = (request.json or {}).get('url', '').strip()
    quality = (request.json or {}).get('quality', '1080')
    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    is_mp3   = quality == 'mp3'
    out_tmpl = os.path.join(DOWNLOAD_DIR, '%(title)s.%(ext)s')
    height   = {'1080': 1080, '1440': 1440, '2160': 2160}.get(quality, 1080)

    last_error = 'Download failed'

    for client in CLIENTS:
        for xff in random.sample(XFF_IPS, 2):
            try:
                args = base_args(client, xff) + [
                    '--output', out_tmpl,
                    '--print', 'after_move:filepath',
                ]

                if is_mp3:
                    args += [
                        '--format', 'bestaudio/best',
                        '--extract-audio',
                        '--audio-format', 'mp3',
                        '--audio-quality', '0',
                    ]
                else:
                    args += [
                        '--format',
                        f'bestvideo*[height<={height}]+bestaudio*/bestvideo[height<={height}]+bestaudio/bestvideo*+bestaudio*/best',
                        '--merge-output-format', 'mp4',
                    ]

                args.append(url)
                result = subprocess.run(args, capture_output=True, text=True, timeout=600)

                filepath = None
                for line in result.stdout.strip().split('\n'):
                    line = line.strip()
                    if line and os.path.exists(line):
                        filepath = line
                        break

                if not filepath:
                    filepath = find_newest('.mp3' if is_mp3 else '.mp4')

                if not filepath or not os.path.exists(filepath):
                    last_error = result.stderr.strip().split('\n')[-1] if result.stderr else 'File not found'
                    continue

                file_size = os.path.getsize(filepath)
                if file_size < 10000:
                    os.remove(filepath)
                    last_error = 'File too small'
                    continue

                mimetype      = 'audio/mpeg' if is_mp3 else 'video/mp4'
                download_name = os.path.basename(filepath)
                cleanup(filepath)

                def generate(path):
                    with open(path, 'rb') as f:
                        while True:
                            chunk = f.read(1024 * 1024)
                            if not chunk:
                                break
                            yield chunk

                return Response(
                    stream_with_context(generate(filepath)),
                    mimetype=mimetype,
                    headers={
                        'Content-Disposition': f'attachment; filename="{download_name}"',
                        'Content-Length':      str(file_size),
                        'X-Accel-Buffering':   'no',
                        'Cache-Control':       'no-cache',
                    }
                )

            except subprocess.TimeoutExpired:
                last_error = 'Timed out'
            except Exception as e:
                last_error = str(e)

    return jsonify({'error': last_error}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
