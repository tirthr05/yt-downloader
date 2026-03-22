from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import os, tempfile, threading, time, subprocess, json, glob

app = Flask(__name__)
CORS(app, origins="*")

DOWNLOAD_DIR = tempfile.mkdtemp()
COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cookies.txt')
YT_EMAIL    = os.environ.get('YT_EMAIL', '')
YT_PASSWORD = os.environ.get('YT_PASSWORD', '')

FORMATS = {
    # Sort by fps DESC so 60fps is preferred over 30fps at same resolution
    # bestvideo[height=X] picks highest fps automatically when sorted
    '1080': (
        # Best fps at exactly 1080p mp4
        'bestvideo[height=1080][fps>=60][ext=mp4]+bestaudio[ext=m4a]/'
        'bestvideo[height=1080][fps>=50][ext=mp4]+bestaudio[ext=m4a]/'
        'bestvideo[height=1080][ext=mp4]+bestaudio[ext=m4a]/'
        'bestvideo[height=1080][fps>=60]+bestaudio/'
        'bestvideo[height=1080]+bestaudio/'
        # Fallback: best up to 1080p
        'bestvideo[height<=1080][fps>=60][ext=mp4]+bestaudio[ext=m4a]/'
        'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/'
        'bestvideo[height<=1080]+bestaudio/'
        # Last resort: absolute best
        'bestvideo[ext=mp4]+bestaudio[ext=m4a]/'
        'bestvideo+bestaudio/best'
    ),
    '1440': (
        'bestvideo[height=1440][fps>=60][ext=mp4]+bestaudio[ext=m4a]/'
        'bestvideo[height=1440][fps>=50][ext=mp4]+bestaudio[ext=m4a]/'
        'bestvideo[height=1440][ext=mp4]+bestaudio[ext=m4a]/'
        'bestvideo[height=1440][fps>=60]+bestaudio/'
        'bestvideo[height=1440]+bestaudio/'
        'bestvideo[height<=1440][fps>=60][ext=mp4]+bestaudio[ext=m4a]/'
        'bestvideo[height<=1440][ext=mp4]+bestaudio[ext=m4a]/'
        'bestvideo[height<=1440]+bestaudio/'
        'bestvideo[ext=mp4]+bestaudio[ext=m4a]/'
        'bestvideo+bestaudio/best'
    ),
    '2160': (
        'bestvideo[height=2160][fps>=60][ext=mp4]+bestaudio[ext=m4a]/'
        'bestvideo[height=2160][fps>=50][ext=mp4]+bestaudio[ext=m4a]/'
        'bestvideo[height=2160][ext=mp4]+bestaudio[ext=m4a]/'
        'bestvideo[height=2160][fps>=60]+bestaudio/'
        'bestvideo[height=2160]+bestaudio/'
        'bestvideo[height<=2160][fps>=60][ext=mp4]+bestaudio[ext=m4a]/'
        'bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/'
        'bestvideo[height<=2160]+bestaudio/'
        'bestvideo[ext=mp4]+bestaudio[ext=m4a]/'
        'bestvideo+bestaudio/best'
    ),
    'best': (
        'bestvideo[fps>=60][ext=mp4]+bestaudio[ext=m4a]/'
        'bestvideo[ext=mp4]+bestaudio[ext=m4a]/'
        'bestvideo[fps>=60]+bestaudio/'
        'bestvideo+bestaudio/best'
    ),
    'mp3': 'bestaudio/best',
}

PLAYER_CLIENTS = [
    'android,web', 'ios,web', 'android_vr',
    'tv_embedded,web', 'mweb,android', 'web_creator,android',
]

# ─── AUTO COOKIE REFRESH ──────────────────────────────────────────────────────

def refresh_cookies():
    """Use Playwright to log into YouTube and export fresh cookies"""
    if not YT_EMAIL or not YT_PASSWORD:
        print('[cookies] No YT_EMAIL/YT_PASSWORD set — skipping auto refresh')
        return False
    try:
        print('[cookies] Starting auto cookie refresh...')
        script = f"""
import asyncio
from playwright.async_api import async_playwright

async def get_cookies():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            '--no-sandbox', '--disable-setuid-sandbox',
            '--disable-dev-shm-usage', '--disable-gpu'
        ])
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'
        )
        page = await context.new_page()

        # Go to YouTube login
        await page.goto('https://accounts.google.com/signin/v2/identifier?service=youtube', timeout=30000)
        await page.wait_for_timeout(2000)

        # Enter email
        await page.fill('input[type="email"]', '{YT_EMAIL}')
        await page.click('#identifierNext')
        await page.wait_for_timeout(3000)

        # Enter password
        await page.fill('input[type="password"]', '{YT_PASSWORD}')
        await page.click('#passwordNext')
        await page.wait_for_timeout(5000)

        # Navigate to YouTube
        await page.goto('https://www.youtube.com', timeout=30000)
        await page.wait_for_timeout(3000)

        # Export cookies in Netscape format
        cookies = await context.cookies(['https://www.youtube.com'])
        lines = ['# Netscape HTTP Cookie File']
        for c in cookies:
            domain = c['domain']
            flag = 'TRUE' if domain.startswith('.') else 'FALSE'
            secure = 'TRUE' if c.get('secure') else 'FALSE'
            expiry = int(c.get('expires', 0)) if c.get('expires') else 0
            name = c['name']
            value = c['value']
            path = c.get('path', '/')
            lines.append(f'{{domain}}\\t{{flag}}\\t{{path}}\\t{{secure}}\\t{{expiry}}\\t{{name}}\\t{{value}}')

        with open('{COOKIES_FILE}', 'w') as f:
            f.write('\\n'.join(lines))

        await browser.close()
        print('[cookies] Cookie refresh successful!')

asyncio.run(get_cookies())
"""
        result = subprocess.run(['python3', '-c', script],
                                capture_output=True, text=True, timeout=120)
        if result.returncode == 0 and os.path.exists(COOKIES_FILE):
            size = os.path.getsize(COOKIES_FILE)
            print(f'[cookies] Saved {size} bytes to cookies.txt')
            return True
        else:
            print(f'[cookies] Refresh failed: {result.stderr[-300:]}')
            return False
    except Exception as e:
        print(f'[cookies] Exception: {e}')
        return False


def cookie_scheduler():
    """Run cookie refresh every 12 hours using plain threading"""
    # Refresh immediately on startup if no cookies
    if not os.path.exists(COOKIES_FILE):
        refresh_cookies()
    # Then sleep 12 hours and repeat
    while True:
        time.sleep(12 * 60 * 60)
        refresh_cookies()


# Start cookie refresh thread on startup
threading.Thread(target=cookie_scheduler, daemon=True).start()

# ─── YT-DLP HELPERS ──────────────────────────────────────────────────────────

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
        '--extractor-args', 'youtube:player_skip=webpage,configs',
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


# ─── ROUTES ──────────────────────────────────────────────────────────────────

@app.route('/health')
def health():
    try:
        v = subprocess.run(['yt-dlp', '--version'], capture_output=True, text=True, timeout=5)
        version = v.stdout.strip()
    except:
        version = 'not found'
    return jsonify({
        'status': 'ok',
        'yt_dlp': version,
        'cookies': os.path.exists(COOKIES_FILE),
        'auto_refresh': bool(YT_EMAIL),
    })


@app.route('/api/refresh-cookies', methods=['POST'])
def manual_refresh():
    """Manual trigger to refresh cookies"""
    success = refresh_cookies()
    return jsonify({'success': success})


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
            if result.stderr:
                last_error = result.stderr.strip().split('\n')[-1]
                # If bot error, try refreshing cookies immediately
                if 'Sign in' in last_error or 'bot' in last_error.lower():
                    threading.Thread(target=refresh_cookies, daemon=True).start()
        except subprocess.TimeoutExpired:
            last_error = 'Timeout - try again'
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

    fmt = FORMATS.get(quality, FORMATS['1080'])
    is_mp3 = quality == 'mp3'
    out_tmpl = os.path.join(DOWNLOAD_DIR, '%(title)s.%(ext)s')

    last_error = 'Download failed'
    for attempt in range(6):
        try:
            args = base_args(attempt) + [
                '--format', fmt,
                '--output', out_tmpl,
                '--print', 'after_move:filepath',
            ]
            if is_mp3:
                args += ['--extract-audio', '--audio-format', 'mp3', '--audio-quality', '0']
            else:
                args += ['--merge-output-format', 'mp4']

            args.append(url)
            result = subprocess.run(args, capture_output=True, text=True, timeout=600)

            # Auto refresh cookies if bot detected
            if result.returncode != 0 and result.stderr:
                err = result.stderr.strip().split('\n')[-1]
                if 'Sign in' in err or 'bot' in err.lower():
                    print('[cookies] Bot detected — refreshing cookies...')
                    refresh_cookies()
                last_error = err
                time.sleep(2)
                continue

            # Get filepath from --print output
            filepath = None
            for line in result.stdout.strip().split('\n'):
                line = line.strip()
                if line and os.path.exists(line):
                    filepath = line
                    break

            # Fallback: newest file
            if not filepath:
                ext = '.mp3' if is_mp3 else '.mp4'
                matches = glob.glob(os.path.join(DOWNLOAD_DIR, f'*{ext}'))
                if matches:
                    filepath = max(matches, key=os.path.getmtime)

            if not filepath or not os.path.exists(filepath):
                last_error = 'File not found after download'
                time.sleep(2)
                continue

            file_size = os.path.getsize(filepath)
            if file_size < 10000:
                last_error = 'File too small'
                os.remove(filepath)
                time.sleep(2)
                continue

            download_name = os.path.basename(filepath)
            mimetype = 'audio/mpeg' if is_mp3 else 'video/mp4'
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
                    'Content-Length': str(file_size),
                    'X-Accel-Buffering': 'no',
                    'Cache-Control': 'no-cache',
                }
            )

        except subprocess.TimeoutExpired:
            last_error = 'Timed out'
        except Exception as e:
            last_error = str(e)
        time.sleep(1.5)

    return jsonify({'error': last_error}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
