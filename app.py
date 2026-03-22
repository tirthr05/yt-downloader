from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp
import os
import tempfile
import threading
import time
import random

app = Flask(__name__)
CORS(app, origins="*")

DOWNLOAD_DIR = tempfile.mkdtemp()

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15',
    'com.google.android.youtube/19.09.37 (Linux; U; Android 11) gzip',
    'com.google.android.youtube/17.36.4 (Linux; U; Android 12) gzip',
]

# Each attempt uses a different player client strategy
PLAYER_STRATEGIES = [
    ['android', 'web'],
    ['ios', 'web'],
    ['android_vr'],
    ['tv_embedded', 'web'],
    ['mweb', 'android'],
    ['web_creator', 'android'],
]

def get_opts(attempt=0):
    strategy = PLAYER_STRATEGIES[attempt % len(PLAYER_STRATEGIES)]
    return {
        'quiet': True,
        'no_warnings': True,
        'user_agent': random.choice(USER_AGENTS),
        'extractor_args': {
            'youtube': {
                'player_client': strategy,
            }
        },
        'http_headers': {
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Sec-Fetch-Mode': 'navigate',
        },
        'retries': 10,
        'fragment_retries': 10,
        'sleep_interval': 1,
        'max_sleep_interval': 4,
        'ignoreerrors': False,
        'geo_bypass': True,
        'geo_bypass_country': 'US',
    }

def cleanup_file(path, delay=120):
    def _delete():
        time.sleep(delay)
        try:
            os.remove(path)
        except:
            pass
    threading.Thread(target=_delete, daemon=True).start()


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})


@app.route('/api/info', methods=['POST'])
def get_info():
    data = request.json
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    last_error = None
    for attempt in range(6):
        try:
            ydl_opts = get_opts(attempt)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                return jsonify({
                    'title': info.get('title', 'Unknown'),
                    'thumbnail': info.get('thumbnail', ''),
                    'duration': info.get('duration', 0),
                    'uploader': info.get('uploader', 'Unknown'),
                })
        except Exception as e:
            last_error = str(e)
            time.sleep(2)

    return jsonify({'error': last_error}), 400


@app.route('/api/download', methods=['POST'])
def download_video():
    data = request.json
    url = data.get('url', '').strip()
    quality = data.get('quality', '1080')

    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    out_tmpl = os.path.join(DOWNLOAD_DIR, '%(title)s.%(ext)s')

    if quality == 'mp3':
        extra = {
            'format': 'bestaudio/best',
            'outtmpl': out_tmpl,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '320',
            }],
        }
    else:
        if quality == 'best':
            fmt = 'bestvideo+bestaudio/best'
        else:
            fmt = (
                f'bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/'
                f'bestvideo[height<={quality}]+bestaudio/'
                f'best[height<={quality}]/'
                f'best'
            )
        extra = {
            'format': fmt,
            'outtmpl': out_tmpl,
            'merge_output_format': 'mp4',
        }

    last_error = None
    for attempt in range(6):
        try:
            ydl_opts = {**get_opts(attempt), **extra}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)

                safe_title = ydl.prepare_filename(info)
                if quality == 'mp3':
                    filepath = os.path.splitext(safe_title)[0] + '.mp3'
                else:
                    filepath = os.path.splitext(safe_title)[0] + '.mp4'
                    if not os.path.exists(filepath):
                        filepath = safe_title

                if not os.path.exists(filepath):
                    raise Exception('File not found after download')

                cleanup_file(filepath, delay=180)

                return send_file(
                    filepath,
                    as_attachment=True,
                    download_name=os.path.basename(filepath),
                    mimetype='audio/mpeg' if quality == 'mp3' else 'video/mp4'
                )

        except Exception as e:
            last_error = str(e)
            time.sleep(2)

    return jsonify({'error': last_error}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
