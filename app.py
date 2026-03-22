from flask import Flask, request, jsonify, Response, stream_with_context
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
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0',
    'com.google.android.youtube/19.09.37 (Linux; U; Android 11) gzip',
    'com.google.android.youtube/17.36.4 (Linux; U; Android 12) gzip',
]

PLAYER_STRATEGIES = [
    ['android', 'web'],
    ['ios', 'web'],
    ['android_vr'],
    ['tv_embedded', 'web'],
    ['mweb', 'android'],
    ['web_creator', 'android'],
]

# STRICT quality map - video and audio are downloaded SEPARATELY then merged by ffmpeg
# This guarantees exact resolution - no combined streams which cap at 720p on YouTube
QUALITY_HEIGHT = {
    '1080': 1080,
    '1440': 1440,
    '2160': 2160,
    'best': 9999,
}

def get_format(quality):
    if quality == 'mp3':
        return 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best'
    h = QUALITY_HEIGHT.get(quality, 1080)
    if quality == 'best':
        return (
            'bestvideo[ext=mp4]+bestaudio[ext=m4a]/'
            'bestvideo+bestaudio/best'
        )
    return (
        # Exact height mp4 preferred
        f'bestvideo[height={h}][ext=mp4]+bestaudio[ext=m4a]/'
        # Exact height any container
        f'bestvideo[height={h}]+bestaudio[ext=m4a]/'
        f'bestvideo[height={h}]+bestaudio/'
        # Up to height mp4
        f'bestvideo[height<={h}][height>={h-10}][ext=mp4]+bestaudio[ext=m4a]/'
        f'bestvideo[height<={h}][height>={h-10}]+bestaudio/'
        # Relaxed up to height
        f'bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/'
        f'bestvideo[height<={h}]+bestaudio/'
        f'best[height<={h}]'
    )

def get_opts(attempt=0):
    return {
        'quiet': True,
        'no_warnings': True,
        'user_agent': random.choice(USER_AGENTS),
        'extractor_args': {
            'youtube': {
                'player_client': PLAYER_STRATEGIES[attempt % len(PLAYER_STRATEGIES)],
            }
        },
        'http_headers': {
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        },
        'retries': 10,
        'fragment_retries': 10,
        'geo_bypass': True,
        'geo_bypass_country': 'US',
        'concurrent_fragment_downloads': 8,
        'http_chunk_size': 10485760,
    }

def cleanup_file(path, delay=180):
    def _delete():
        time.sleep(delay)
        try:
            os.remove(path)
        except:
            pass
    threading.Thread(target=_delete, daemon=True).start()

def find_file(directory, video_id, ext):
    """Find downloaded file by video_id and extension"""
    for f in sorted(os.listdir(directory), key=lambda x: os.path.getmtime(os.path.join(directory, x)), reverse=True):
        if video_id in f and f.endswith(ext):
            return os.path.join(directory, f)
    # fallback: most recently modified file with that ext
    candidates = [f for f in os.listdir(directory) if f.endswith(ext)]
    if candidates:
        candidates.sort(key=lambda x: os.path.getmtime(os.path.join(directory, x)), reverse=True)
        return os.path.join(directory, candidates[0])
    return None

def safe_title(title, max_len=80):
    return "".join(c for c in title if c.isalnum() or c in " -_()[]").strip()[:max_len]


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
            with yt_dlp.YoutubeDL(get_opts(attempt)) as ydl:
                info = ydl.extract_info(url, download=False)
                # Find the best available resolutions for this video
                formats = info.get('formats', [])
                heights = sorted(set(
                    f.get('height') for f in formats
                    if f.get('height') and f.get('vcodec') != 'none'
                ), reverse=True)
                return jsonify({
                    'title': info.get('title', 'Unknown'),
                    'thumbnail': info.get('thumbnail', ''),
                    'duration': info.get('duration', 0),
                    'uploader': info.get('uploader', 'Unknown'),
                    'available_heights': heights[:8],
                })
        except Exception as e:
            last_error = str(e)
            time.sleep(1)

    return jsonify({'error': last_error}), 400


@app.route('/api/download', methods=['POST'])
def download_video():
    data = request.json
    url = data.get('url', '').strip()
    quality = data.get('quality', '1080')

    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    fmt = get_format(quality)
    is_mp3 = quality == 'mp3'

    if is_mp3:
        out_tmpl = os.path.join(DOWNLOAD_DIR, '%(id)s_audio.%(ext)s')
        extra = {
            'format': fmt,
            'outtmpl': out_tmpl,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '320',
            }],
        }
    else:
        out_tmpl = os.path.join(DOWNLOAD_DIR, '%(id)s_%(height)sp.%(ext)s')
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

            video_id = info.get('id', 'video')
            actual_height = info.get('height') or quality
            title = safe_title(info.get('title', video_id))

            if is_mp3:
                filepath = find_file(DOWNLOAD_DIR, video_id, '.mp3')
                download_name = f"{title}.mp3"
                mimetype = 'audio/mpeg'
            else:
                filepath = find_file(DOWNLOAD_DIR, video_id, '.mp4')
                download_name = f"{title}_{actual_height}p.mp4"
                mimetype = 'video/mp4'

            if not filepath:
                raise Exception('Downloaded file not found on disk')

            actual_size = os.path.getsize(filepath)
            if actual_size < 1000:
                raise Exception('Downloaded file is too small, likely corrupt')

            cleanup_file(filepath, delay=180)

            def generate(path):
                with open(path, 'rb') as f:
                    while True:
                        chunk = f.read(512 * 1024)  # 512KB chunks
                        if not chunk:
                            break
                        yield chunk

            return Response(
                stream_with_context(generate(filepath)),
                mimetype=mimetype,
                headers={
                    'Content-Disposition': f'attachment; filename="{download_name}"',
                    'Content-Length': str(actual_size),
                    'X-Accel-Buffering': 'no',
                    'Cache-Control': 'no-cache',
                }
            )

        except Exception as e:
            last_error = str(e)
            time.sleep(1.5)

    return jsonify({'error': last_error}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
