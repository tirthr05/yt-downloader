from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import os, tempfile, threading, time, subprocess, json, glob

app = Flask(__name__)
CORS(app, origins="*")

DOWNLOAD_DIR = tempfile.mkdtemp()
COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies.txt')

def base_args():
    args = [
        'yt-dlp',
        '--no-warnings',
        '--no-playlist', 
        '--no-check-certificate',
        '--concurrent-fragments', '8',
        '--retries', '3',
        '--fragment-retries', '3',
        '--add-header', 'Accept-Language:en-US,en;q=0.9',
    ]
    if os.path.exists(COOKIES_FILE):
        args += ['--cookies', COOKIES_FILE]
    return args

CLIENTS = ['android', 'ios', 'web', 'android_vr', 'tv_embedded', 'mweb']

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

    for client in CLIENTS:
        try:
            r = subprocess.run(
                base_args() + [
                    '--extractor-args', f'youtube:player_client={client}',
                    '--skip-download',
                    '--print', 'title',
                    '--print', 'thumbnail',
                    '--print', 'duration',
                    '--print', 'uploader',
                    url
                ],
                capture_output=True, text=True, timeout=12
            )
            if r.returncode == 0 and r.stdout.strip():
                lines = r.stdout.strip().split('\n')
                return jsonify({
                    'title':     lines[0] if len(lines) > 0 else 'Unknown',
                    'thumbnail': lines[1] if len(lines) > 1 else '',
                    'duration':  int(float(lines[2])) if len(lines) > 2 and lines[2].replace('.','').isdigit() else 0,
                    'uploader':  lines[3] if len(lines) > 3 else 'Unknown',
                })
        except:
            pass
        time.sleep(0.5)

    return jsonify({'error': 'Could not fetch video info. Check the URL and try again.'}), 400


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
        try:
            args = base_args() + [
                '--extractor-args', f'youtube:player_client={client}',
                '--output', out_tmpl,
                '--print', 'after_move:filepath',
            ]

            if is_mp3:
                # Audio only - dead simple, always works
                args += [
                    '--format', 'bestaudio/best',
                    '--extract-audio',
                    '--audio-format', 'mp3',
                    '--audio-quality', '0',
                ]
            else:
                # bestvideo*+bestaudio* with wildcard covers ALL video types
                # Multiple fallbacks guarantee something always downloads
                args += [
                    '--format',
                    f'bestvideo*[height<={height}]+bestaudio*/bestvideo[height<={height}]+bestaudio/bestvideo*+bestaudio*/best[height<={height}]/best',
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

            # Fallback: find newest file
            if not filepath:
                filepath = find_newest('.mp3' if is_mp3 else '.mp4')

            if not filepath or not os.path.exists(filepath):
                last_error = result.stderr.strip().split('\n')[-1] if result.stderr else 'File not found'
                time.sleep(1)
                continue

            file_size = os.path.getsize(filepath)
            if file_size < 10000:
                os.remove(filepath)
                last_error = 'File too small, retrying...'
                time.sleep(1)
                continue

            # Success - stream file to browser
            mimetype      = 'audio/mpeg' if is_mp3 else 'video/mp4'
            download_name = os.path.basename(filepath)
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
                    'Content-Length':      str(file_size),
                    'X-Accel-Buffering':   'no',
                    'Cache-Control':       'no-cache',
                }
            )

        except subprocess.TimeoutExpired:
            last_error = 'Download timed out'
        except Exception as e:
            last_error = str(e)
        time.sleep(1)

    return jsonify({'error': last_error}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
