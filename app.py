from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import os, tempfile, threading, time, subprocess, json, glob

app = Flask(__name__)
CORS(app, origins="*")

DOWNLOAD_DIR = tempfile.mkdtemp()
COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies.txt')

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
        '--retries', '5',
        '--fragment-retries', '5',
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

def find_newest(ext):
    matches = glob.glob(os.path.join(DOWNLOAD_DIR, f'*{ext}'))
    return max(matches, key=os.path.getmtime) if matches else None


@app.route('/health')
def health():
    try:
        v = subprocess.run(['yt-dlp', '--version'], capture_output=True, text=True, timeout=5)
        ver = v.stdout.strip()
    except:
        ver = 'not found'
    return jsonify({'status': 'ok', 'yt_dlp': ver, 'cookies': os.path.exists(COOKIES_FILE)})


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
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and result.stdout.strip():
                info = json.loads(result.stdout.strip().split('\n')[0])
                return jsonify({
                    'title': info.get('title', 'Unknown'),
                    'thumbnail': info.get('thumbnail', ''),
                    'duration': info.get('duration', 0),
                    'uploader': info.get('uploader', 'Unknown'),
                })
            if result.stderr:
                last_error = result.stderr.strip().split('\n')[-1]
        except subprocess.TimeoutExpired:
            last_error = 'Timeout'
        except Exception as e:
            last_error = str(e)
        time.sleep(1)
    return jsonify({'error': last_error}), 400


@app.route('/api/download', methods=['POST'])
def download_video():
    url = (request.json or {}).get('url', '').strip()
    quality = (request.json or {}).get('quality', '1080')
    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    is_mp3 = quality == 'mp3'
    out_tmpl = os.path.join(DOWNLOAD_DIR, '%(title)s.%(ext)s')

    # Height targets
    height_map = {'1080': 1080, '1440': 1440, '2160': 2160}

    last_error = 'Download failed'
    for attempt in range(6):
        try:
            args = base_args(attempt) + [
                '--output', out_tmpl,
                '--print', 'after_move:filepath',
            ]

            if is_mp3:
                # Audio only - simple, always works
                args += [
                    '--format', 'bestaudio',
                    '--extract-audio',
                    '--audio-format', 'mp3',
                    '--audio-quality', '0',
                ]
            elif quality == 'best':
                # Absolute best quality no restrictions
                args += [
                    '--format-sort', 'res,fps,codec,br',
                    '--format', 'bestvideo+bestaudio/best',
                    '--merge-output-format', 'mp4',
                ]
            else:
                h = height_map.get(quality, 1080)
                # Use -S (sort) flag - most reliable way in yt-dlp
                # Sorts by: height (capped), then fps, then codec, then bitrate
                # Never throws format not available
                args += [
                    '--format-sort', f'res:{h},fps,codec,br',
                    '--format', 'bestvideo+bestaudio/best',
                    '--merge-output-format', 'mp4',
                ]

            args.append(url)
            result = subprocess.run(args, capture_output=True, text=True, timeout=600)

            # Get filepath from --print output
            filepath = None
            for line in result.stdout.strip().split('\n'):
                line = line.strip()
                if line and os.path.exists(line):
                    filepath = line
                    break

            # Fallback: newest file
            if not filepath:
                filepath = find_newest('.mp3' if is_mp3 else '.mp4')

            if not filepath or not os.path.exists(filepath):
                last_error = result.stderr.strip().split('\n')[-1] if result.stderr else 'File not found'
                time.sleep(1)
                continue

            file_size = os.path.getsize(filepath)
            if file_size < 10000:
                last_error = 'File too small'
                os.remove(filepath)
                time.sleep(1)
                continue

            download_name = os.path.basename(filepath)
            mimetype = 'audio/mpeg' if is_mp3 else 'video/mp4'
            cleanup(filepath)

            def generate(path):
                with open(path, 'rb') as f:
                    while True:
                        chunk = f.read(1024 * 1024)  # 1MB chunks
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
            last_error = 'Timed out'
        except Exception as e:
            last_error = str(e)
        time.sleep(1)

    return jsonify({'error': last_error}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
