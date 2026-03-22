from flask import Flask, request, jsonify, send_file, after_this_request
from flask_cors import CORS
import yt_dlp
import os
import tempfile
import threading
import time

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = tempfile.mkdtemp()

def cleanup_file(path, delay=60):
    """Delete file after delay seconds"""
    def _delete():
        time.sleep(delay)
        try:
            os.remove(path)
        except:
            pass
    threading.Thread(target=_delete, daemon=True).start()


@app.route('/api/info', methods=['POST'])
def get_info():
    data = request.json
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    ydl_opts = {'quiet': True, 'no_warnings': True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return jsonify({
                'title': info.get('title', 'Unknown'),
                'thumbnail': info.get('thumbnail', ''),
                'duration': info.get('duration', 0),
                'uploader': info.get('uploader', 'Unknown'),
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/download', methods=['POST'])
def download_video():
    data = request.json
    url = data.get('url', '').strip()
    quality = data.get('quality', '1080')  # '1080', '1440', '2160', 'best', 'mp3'

    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    out_tmpl = os.path.join(DOWNLOAD_DIR, '%(title)s.%(ext)s')

    if quality == 'mp3':
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': out_tmpl,
            'quiet': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '320',
            }],
        }
        ext = 'mp3'
    else:
        if quality == 'best':
            fmt = 'bestvideo+bestaudio/best'
        else:
            fmt = f'bestvideo[height<={quality}]+bestaudio/best[height<={quality}]'

        ydl_opts = {
            'format': fmt,
            'outtmpl': out_tmpl,
            'quiet': True,
            'merge_output_format': 'mp4',
        }
        ext = 'mp4'

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get('title', 'video')

            # Find the downloaded file
            safe_title = ydl.prepare_filename(info)
            # For mp3, extension changes after postprocessing
            if quality == 'mp3':
                base = os.path.splitext(safe_title)[0]
                filepath = base + '.mp3'
            else:
                filepath = os.path.splitext(safe_title)[0] + '.mp4'
                if not os.path.exists(filepath):
                    filepath = safe_title  # fallback

            if not os.path.exists(filepath):
                return jsonify({'error': 'Download failed - file not found'}), 500

            cleanup_file(filepath, delay=120)

            return send_file(
                filepath,
                as_attachment=True,
                download_name=os.path.basename(filepath),
                mimetype='audio/mpeg' if quality == 'mp3' else 'video/mp4'
            )

    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, port=5000)
