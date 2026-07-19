#!/usr/bin/env python3
import re
import time
import requests
from flask import Flask, request, Response, render_template_string, jsonify
from urllib.parse import urljoin, quote_plus, unquote_plus, urlparse, urlunparse, parse_qs, urlencode
from playwright.sync_api import sync_playwright
from rapidfuzz import process, fuzz
import os
import datetime
import subprocess
import json
import undetected_chromedriver as uc
import threading
import tempfile
import shutil
import traceback


app = Flask(__name__)

TMDB_API_KEY = "123240ec331a97bb476ad9a05f86c3bf"

_current_origin = {"value": None}

def set_origin_from_url(url: str):
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    _current_origin["value"] = origin
    debug(f"Origin/Referer locked to: {origin}")

def get_headers() -> dict:
    origin = _current_origin["value"] or "https://cloudorchestranova.com"
    return {
        "User-Agent": "Mozilla/5.0",
        "Origin": origin,
        "Referer": origin + "/",
    }

_stream_origins = {}
_stream_origins_lock = threading.Lock()

def remember_stream_origin(url_for_host: str, origin: str):
    """Associates the Origin/Referer to send with a CDN *host* (not the
    exact URL) — variant playlists and segments share the master's host but
    aren't the same URL, so keying by host lets /proxy_playlist and
    /segment look up the right headers for any request belonging to that
    stream. This also means a background prescrape of a different episode
    (which briefly repoints the global "current" origin while it works)
    can't corrupt headers for a host the live stream has already recorded.
    """
    host = urlparse(url_for_host).netloc
    if not host or not origin:
        return
    with _stream_origins_lock:
        _stream_origins[host] = origin
        if len(_stream_origins) > 200:
            oldest_key = next(iter(_stream_origins))
            del _stream_origins[oldest_key]

def get_headers_for_stream(url: str) -> dict:
    host = urlparse(url).netloc
    with _stream_origins_lock:
        origin = _stream_origins.get(host)
    if not origin:
        origin = _current_origin["value"] or "https://cloudorchestranova.com"
    return {
        "User-Agent": "Mozilla/5.0",
        "Origin": origin,
        "Referer": origin + "/",
    }

REQUEST_TIMEOUT = 15
CACHE_TTL = 5
_playlist_cache = {}

def debug(msg):
    print(f"[DEBUG] {msg}")

def get_chrome_major_version():
    candidates = ["chromium-browser","chromium","google-chrome","google-chrome-stable"]
    for binary in candidates:
        try:
            result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=5)
            match = re.search(r"(\d+)\.\d+\.\d+", result.stdout.strip())
            if match:
                major = int(match.group(1))
                debug(f"Detected Chrome/Chromium version {major} via '{binary}'")
                return major
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
            continue
    for binary in ["chromedriver", "chromedriver-browser"]:
        try:
            result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=5)
            match = re.search(r"(\d+)\.\d+\.\d+", result.stdout.strip())
            if match:
                major = int(match.group(1))
                debug(f"Detected version {major} via '{binary} --version'")
                return major
        except Exception:
            continue
    debug("Could not detect Chrome/Chromium version; passing None to uc.Chrome")
    return None

def get_chromium_binary():
    candidates = ["/usr/bin/chromium-browser","/usr/bin/chromium","/usr/bin/google-chrome","/usr/bin/google-chrome-stable"]
    for path in candidates:
        if os.path.isfile(path):
            debug(f"Using Chromium binary: {path}")
            return path
    debug("No Chromium binary found in standard paths; letting uc find it automatically")
    return None

def search_tmdb(query: str):
    debug(f"Searching TMDb for title: '{query}'")
    r = requests.get(
        f"https://api.themoviedb.org/3/search/multi?api_key={TMDB_API_KEY}&query={quote_plus(query)}",
        timeout=REQUEST_TIMEOUT
    )
    data = r.json()
    results = data.get("results", [])
    debug(f"TMDb returned {len(results)} results")
    return results

def get_best_match(title: str, results: list):
    names = [r["title"] if r["media_type"] == "movie" else r["name"] for r in results]
    if not names:
        debug("No names to match")
        return None
    best_name, score, idx = process.extractOne(title, names, scorer=fuzz.token_sort_ratio)
    debug(f"Best TMDb match: '{best_name}' (score: {score})")
    return results[idx] if score and score > 60 else None

def get_seasons(tmdb_id: int):
    r = requests.get(f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={TMDB_API_KEY}", timeout=REQUEST_TIMEOUT)
    data = r.json()
    return [{"season_number": s["season_number"], "name": s["name"]} for s in data.get("seasons", [])]

def get_released_episodes(tmdb_id, season_number):
    url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_number}?api_key={TMDB_API_KEY}"
    resp = requests.get(url, timeout=REQUEST_TIMEOUT).json()
    episodes = resp.get("episodes", [])
    today = datetime.date.today()
    return [
        {"episode_number": ep["episode_number"], "name": ep.get("name", f"Episode {ep['episode_number']}"), "air_date": ep.get("air_date")}
        for ep in episodes
        if ep.get("air_date") and datetime.datetime.strptime(ep["air_date"], "%Y-%m-%d").date() <= today
    ]

def is_released(item):
    today = datetime.date.today()
    if item['type'] == 'movie':
        return datetime.datetime.strptime(item['release_date'], "%Y-%m-%d").date() <= today
    elif item['type'] == 'tv':
        for season in get_seasons(item['tmdb_id']):
            if get_released_episodes(item['tmdb_id'], season['season_number']):
                return True
        return False

@app.route("/get_episodes")
def get_episodes():
    tmdb_id = request.args.get("tmdb_id")
    season = request.args.get("season")
    if not tmdb_id or not season:
        return jsonify([])
    return jsonify(get_released_episodes(tmdb_id, int(season)))

def get_player_iframe_src(vsrc_url: str) -> str:
    debug(f"Fetching vsrc page: {vsrc_url}")
    r = requests.get(vsrc_url, headers=get_headers(), timeout=10)
    m = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', r.text)
    if not m:
        debug("No iframe src found in vsrc page")
        return None
    src = m.group(1)
    if src.startswith("//"):
        src = "https:" + src
    elif src.startswith("/"):
        src = urljoin(vsrc_url, src)
    debug(f"Resolved iframe URL: {src}")
    set_origin_from_url(src)
    return src

def capture_first_m3u8(page_url: str, retries=3) -> str:
    def attempt() -> str:
        result = {"url": None}
        lock = threading.Lock()
        done = threading.Event()
        chrome_failed = threading.Event()

        def chrome_worker():
            chrome_version = get_chrome_major_version()
            chromium_binary = get_chromium_binary()
            options = uc.ChromeOptions()
            if chromium_binary:
                options.binary_location = chromium_binary
            options.add_argument("--no-sandbox")
            options.add_argument("--window-size=1280,720")
            options.add_argument("--remote-debugging-port=0")
            options.add_argument("--disable-setuid-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--autoplay-policy=no-user-gesture-required")
            options.add_argument("--disable-software-rasterizer")
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_argument("--disable-extensions")
            options.add_argument("--disable-background-networking")
            options.add_argument("--disable-default-apps")
            options.add_argument("--mute-audio")
            options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
            options.page_load_strategy = "eager"

            import shutil as _shutil
            home_chromedriver = os.path.expanduser("~/chromedriver")
            system_chromedriver = None
            if not os.path.isfile(home_chromedriver):
                for src in ["/usr/bin/chromedriver","/usr/lib/chromium-browser/chromedriver","/usr/lib/chromium/chromedriver"]:
                    if os.path.isfile(src):
                        try:
                            _shutil.copy2(src, home_chromedriver)
                            os.chmod(home_chromedriver, 0o755)
                            debug(f"[Chrome] Copied chromedriver from {src} to {home_chromedriver}")
                        except Exception as copy_err:
                            debug(f"[Chrome] Could not copy chromedriver: {copy_err}")
                        break
            if os.path.isfile(home_chromedriver) and os.access(home_chromedriver, os.X_OK):
                system_chromedriver = home_chromedriver
                debug(f"[Chrome] Using writable chromedriver: {home_chromedriver}")
            else:
                for candidate in ["/usr/bin/chromedriver","/usr/lib/chromium-browser/chromedriver","/usr/lib/chromium/chromedriver"]:
                    if os.path.isfile(candidate):
                        system_chromedriver = candidate
                        debug(f"[Chrome] Using system chromedriver: {candidate}")
                        break

            kwargs = {"options": options, "use_subprocess": True}
            if chrome_version:
                kwargs["version_main"] = chrome_version
            if system_chromedriver:
                kwargs["driver_executable_path"] = system_chromedriver
            os.environ.setdefault("DISPLAY", ":0")
            driver = None
            try:
                driver = uc.Chrome(**kwargs)
                debug("[Chrome] Browser launched")
                driver.get(page_url)
                try:
                    driver.get_log("performance")
                except Exception:
                    pass
                for sel in ["button.vjs-play-control", ".jw-icon-play", ".play-btn", "video"]:
                    try:
                        el = driver.find_element("css selector", sel)
                        driver.execute_script("arguments[0].click();", el)
                        break
                    except Exception:
                        continue
                else:
                    try:
                        driver.execute_script("document.elementFromPoint(640, 360)?.click();")
                    except Exception:
                        pass
                deadline = time.time() + 120
                while time.time() < deadline:
                    if done.is_set():
                        break
                    try:
                        logs = driver.get_log("performance")
                    except Exception:
                        break
                    for entry in logs:
                        try:
                            msg = json.loads(entry["message"])["message"]
                            if msg.get("method") == "Network.requestWillBeSent":
                                url = msg["params"]["request"]["url"]
                                if ".m3u8" in url:
                                    with lock:
                                        if not result["url"]:
                                            result["url"] = url
                                            debug(f"[Chrome] Captured m3u8: {url}")
                                    done.set()
                                    return
                        except Exception:
                            continue
                    time.sleep(0.3)
            except Exception as e:
                debug(f"[Chrome] Error: {e}")
            finally:
                if driver:
                    try:
                        driver.quit()
                    except Exception:
                        pass
                if not done.is_set():
                    debug("[Chrome] Exited without capturing m3u8")
                    chrome_failed.set()
                    done.set()

        chrome_thread = threading.Thread(target=chrome_worker, daemon=True)
        chrome_thread.start()
        debug("[Playwright] Starting WebKit...")

        try:
            with sync_playwright() as p:
                browser = p.webkit.launch(headless=True)
                context = browser.new_context()
                page = context.new_page()

                def on_request(req):
                    url = req.url
                    if ".m3u8" in url:
                        with lock:
                            if not result["url"]:
                                result["url"] = url
                                debug(f"[Playwright] Captured m3u8: {url}")
                        done.set()

                page.on("request", on_request)
                try:
                    page.goto(page_url, wait_until="load", timeout=30000)
                except Exception as e:
                    debug(f"[Playwright] Page load error: {e}")

                selectors = ["button.vjs-play-control", ".jw-icon-play", ".play-btn", "video"]
                clicked = False
                for sel in selectors:
                    try:
                        el = page.query_selector(sel)
                        if el:
                            el.click()
                            clicked = True
                            break
                    except Exception:
                        continue
                if not clicked:
                    try:
                        page.mouse.click(640, 360)
                    except Exception:
                        pass

                for _ in range(60):
                    if done.is_set():
                        break
                    page.wait_for_timeout(500)

                context.close()
                browser.close()

        except Exception as e:
            debug(f"[Playwright] Fatal error: {e}")

        if not done.is_set():
            debug("Waiting for Chrome thread to finish...")
            done.wait(timeout=120)

        chrome_thread.join(timeout=5)

        if chrome_failed.is_set() and not result["url"]:
            return None

        return result["url"]

    for attempt_num in range(1, retries + 1):
        debug(f"[capture] Attempt {attempt_num}/{retries}")
        url = attempt()
        if url:
            debug(f"Final m3u8 (attempt {attempt_num}): {url}")
            return url
        debug(f"[capture] Attempt {attempt_num} failed, retrying...")

    debug("All capture attempts failed")
    return None

def get_best_variant(m3u8_url: str) -> str:
    debug(f"Fetching m3u8 to find best variant: {m3u8_url}")
    r = requests.get(m3u8_url, headers=get_headers(), timeout=REQUEST_TIMEOUT)
    variants = re.findall(r'(#EXT-X-STREAM-INF:[^\n]+\n)([^\n]+\.m3u8)', r.text)
    if not variants:
        return m3u8_url
    best_variant = max(
        variants,
        key=lambda v: int(re.search(r"RESOLUTION=(\d+)x(\d+)", v[0]).group(2)
                          if re.search(r"RESOLUTION=(\d+)x(\d+)", v[0]) else 0)
    )[1]
    final_url = urljoin(m3u8_url, best_variant)
    master_token = parse_qs(urlparse(m3u8_url).query).get("token", [None])[0]
    if master_token:
        variant_parts = urlparse(final_url)
        variant_qs = parse_qs(variant_parts.query)
        if "token" not in variant_qs:
            variant_qs["token"] = [master_token]
            new_query = urlencode(variant_qs, doseq=True)
            final_url = urlunparse(variant_parts._replace(query=new_query))
            debug(f"Appended token from master m3u8 to variant URL: {final_url}")
    return final_url

def find_mpeg_ts_start(data: bytes, min_consecutive_packets_check: int = 5):
    n = len(data)
    max_scan = min(4096, n)
    for idx in range(max_scan):
        if data[idx] != 0x47:
            continue
        good = True
        for k in range(1, min_consecutive_packets_check + 1):
            pos = idx + k * 188
            if pos >= n or data[pos] != 0x47:
                good = False
                break
        if good:
            return idx
    return None

def extract_ts_packets(data: bytes) -> bytes:
    start = find_mpeg_ts_start(data)
    if start is None:
        try:
            fallback = data.index(b'\x47')
            out = bytearray()
            i = fallback
            while i + 188 <= len(data):
                out.extend(data[i:i + 188])
                i += 188
            return bytes(out)
        except ValueError:
            return b''
    out = bytearray()
    i = start
    n = len(data)
    while i + 188 <= n:
        packet = data[i:i + 188]
        if packet[0] != 0x47:
            break
        out.extend(packet)
        i += 188
    return bytes(out)

def fetch_bytes(url):
    r = requests.get(url, headers=get_headers(), timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.content

def fetch_bytes_retry(url, max_retries=6, base_delay=1.5, headers=None):
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            r = requests.get(url, headers=headers or get_headers(), timeout=REQUEST_TIMEOUT)
            if r.status_code == 429 or r.status_code >= 500:
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        wait = base_delay * (2 ** attempt)
                else:
                    wait = base_delay * (2 ** attempt)
                wait = min(wait, 30)
                if attempt < max_retries:
                    debug(f"[fetch_bytes_retry] {r.status_code} on {url} — retrying in {wait:.1f}s (attempt {attempt+1}/{max_retries})")
                    time.sleep(wait)
                    continue
            r.raise_for_status()
            return r.content
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt < max_retries:
                wait = base_delay * (2 ** attempt)
                debug(f"[fetch_bytes_retry] error on {url} ({e}) — retrying in {wait:.1f}s")
                time.sleep(wait)
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError(f"fetch_bytes_retry: exhausted retries for {url}")

def rewrite_playlist(original_playlist_url: str, playlist_text: str):
    lines = playlist_text.splitlines()
    new_lines = []
    seq = 0
    master_token = parse_qs(urlparse(original_playlist_url).query).get("token", [None])[0]
    for line in lines:
        line_stripped = line.strip()
        if not line_stripped or line_stripped.startswith('#'):
            new_lines.append(line)
            continue
        abs_url = urljoin(original_playlist_url, line_stripped)
        if master_token:
            parts = urlparse(abs_url)
            qs = parse_qs(parts.query)
            if "token" not in qs:
                qs["token"] = [master_token]
                abs_url = urlunparse(parts._replace(query=urlencode(qs, doseq=True)))
        if ".m3u8" in abs_url:
            proxied = f"/proxy_playlist?url={quote_plus(abs_url)}"
        else:
            proxied = f"/segment?u={quote_plus(abs_url)}&i={seq}"
            seq += 1
        new_lines.append(proxied)
    return "\n".join(new_lines)

@app.after_request
def add_cors(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Headers'] = '*'
    return resp

@app.route("/proxy_playlist")
def proxy_playlist():
    url = request.args.get("url")
    if not url:
        return "Missing url param", 400
    now = time.time()
    cached = _playlist_cache.get(url)
    if cached and now - cached['ts'] < CACHE_TTL:
        return Response(cached['data'], mimetype="application/vnd.apple.mpegurl")
    try:
        # A couple of quick retries absorbs the rare transient 5xx/524
        # (origin timeout) blip from the upstream CDN without ever
        # surfacing an error to the player.
        pl_bytes = fetch_bytes_retry(url, max_retries=2, base_delay=0.75, headers=get_headers_for_stream(url))
    except Exception as e:
        return f"Failed to fetch playlist: {e}", 502
    pl_text = pl_bytes.decode('utf-8', errors='ignore')
    if not pl_text.lstrip().startswith('#EXTM3U'):
        # The upstream didn't actually return a playlist (e.g. a Cloudflare
        # error page). Refuse to rewrite/serve it as if it were valid HLS
        # content — that was corrupting the response and confusing hls.js.
        # Returning a clean error here lets hls.js's own fragment/manifest
        # error+retry handling do the right thing instead.
        debug(f"[proxy_playlist] Upstream did not return a valid playlist for {url}")
        return "Upstream did not return a valid playlist", 502
    rewritten = rewrite_playlist(url, pl_text)
    _playlist_cache[url] = {'data': rewritten, 'ts': now}
    return Response(rewritten, mimetype="application/vnd.apple.mpegurl")

@app.route("/segment")
def segment():
    u = request.args.get("u")
    if not u:
        return "Missing u param", 400
    url = unquote_plus(u)
    try:
        remote_bytes = fetch_bytes_retry(url, max_retries=2, base_delay=0.5, headers=get_headers_for_stream(url))
        remote_bytes = extract_ts_packets(remote_bytes)
    except Exception as e:
        return f"Failed to fetch remote segment: {e}", 502

    total_len = len(remote_bytes)

    # Native HLS players (tvOS's AVPlayer in particular, used for true
    # remote AirPlay playback rather than mirroring) sometimes probe or
    # fetch segments with a Range header. Previously this was ignored and
    # the full segment was always returned, which can make a strict client
    # treat the source as broken/unsupported. Honor Range properly with a
    # real 206 response when asked.
    range_header = request.headers.get('Range')
    if range_header:
        m = re.match(r'bytes=(\d*)-(\d*)', range_header)
        if m:
            start_str, end_str = m.groups()
            start = int(start_str) if start_str else 0
            end = int(end_str) if end_str else total_len - 1
            end = min(end, total_len - 1)
            if total_len == 0 or start > end or start >= total_len:
                resp = Response(status=416)
                resp.headers['Content-Range'] = f'bytes */{total_len}'
                return resp
            chunk = remote_bytes[start:end + 1]
            resp = Response(chunk, status=206, mimetype="video/MP2T")
            resp.headers['Content-Range'] = f'bytes {start}-{end}/{total_len}'
            resp.headers['Accept-Ranges'] = 'bytes'
            resp.headers['Content-Length'] = str(len(chunk))
            return resp

    resp = Response(remote_bytes, mimetype="video/MP2T")
    resp.headers['Content-Length'] = str(total_len)
    resp.headers['Accept-Ranges'] = 'bytes'
    return resp

WIDELY_SUPPORTED_VIDEO_CODECS = {"h264"}
WIDELY_SUPPORTED_AUDIO_CODECS = {"aac", "mp3"}

def probe_codecs(path: str):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", path],
            capture_output=True, text=True, timeout=60
        )
        info = json.loads(result.stdout or "{}")
    except Exception as e:
        debug(f"[download_mp4] ffprobe failed: {e}")
        return None, None
    video_codec = None
    audio_codec = None
    for stream in info.get("streams", []):
        codec_type = stream.get("codec_type")
        codec_name = stream.get("codec_name")
        if codec_type == "video" and video_codec is None:
            video_codec = codec_name
        elif codec_type == "audio" and audio_codec is None:
            audio_codec = codec_name
    return video_codec, audio_codec

def _attach_token(base_url: str, target_url: str) -> str:
    token = parse_qs(urlparse(base_url).query).get("token", [None])[0]
    if not token:
        return target_url
    parts = urlparse(target_url)
    qs = parse_qs(parts.query)
    if "token" in qs:
        return target_url
    qs["token"] = [token]
    return urlunparse(parts._replace(query=urlencode(qs, doseq=True)))

_downloads_in_progress = set()
_downloads_lock = threading.Lock()

SEGMENT_FETCH_DELAY = 0.2



# ─────────────────────────────────────────────────────────────────────────────
# Background download system
#
# /download_mp4   — starts a background job, returns a job_id immediately
# /download_ready — poll for job status; serves the file when done
#
# Running the work in a daemon thread means the Flask request thread is freed
# instantly, so the server stays fully responsive while downloads are in progress.
# ─────────────────────────────────────────────────────────────────────────────

import uuid

# job_id -> {"status": "pending"|"done"|"error", "path": str|None, "title": str, "msg": str|None}
_jobs: dict = {}
_jobs_lock = threading.Lock()

# How long a finished MP4 is kept on disk waiting to be downloaded to the
# phone before we clean it up automatically. Downloads are no longer
# triggered automatically when a job finishes, so without this sweep a
# completed file that's never tapped would sit on disk forever.
JOB_RETENTION_SECONDS = 24 * 60 * 60  # 1 day — the hard cap for a file nobody ever downloaded


def _cleanup_stale_jobs():
    while True:
        time.sleep(60)
        now = time.time()
        stale = []
        with _jobs_lock:
            for jid, job in list(_jobs.items()):
                if job.get("status") == "done" and now - job.get("completed_at", now) > JOB_RETENTION_SECONDS:
                    stale.append((jid, job.get("path")))
            for jid, _ in stale:
                del _jobs[jid]
        for jid, path in stale:
            if path and os.path.isfile(path):
                try:
                    os.remove(path)
                    debug(f"[cleanup] Removed stale, never-downloaded file for job {jid}: {path}")
                except OSError as e:
                    debug(f"[cleanup] Failed to remove {path}: {e}")


threading.Thread(target=_cleanup_stale_jobs, daemon=True).start()


def _run_download_job(job_id: str, url: str, title: str, dl_headers: dict, quality: str = ""):
    """Runs entirely in a daemon thread — Flask is not involved.

    `quality`, if given, is the target vertical resolution (e.g. "1080")
    taken from whatever quality the player had selected at the moment the
    download was started. We pick the rendition whose height is closest to
    that. An empty/unparseable `quality` means "Auto" was selected in the
    player, so we fall back to the previous behavior of grabbing the
    highest-resolution rendition available.
    """
    ts_path = mp4_path = None
    try:
        # resolve variant
        pl_bytes = fetch_bytes_retry(url, headers=dl_headers)
        pl_text  = pl_bytes.decode("utf-8", errors="ignore")

        variants = re.findall(r'(#EXT-X-STREAM-INF:[^\n]+\n)([^\n]+\.m3u8[^\n]*)', pl_text)
        variant_url = url
        if variants:
            def _height(v):
                m = re.search(r"RESOLUTION=(\d+)x(\d+)", v[0])
                return int(m.group(2)) if m else 0

            target_height = None
            if quality:
                try:
                    target_height = int(quality)
                except ValueError:
                    target_height = None

            if target_height:
                # Closest match to the resolution the player had selected,
                # preferring an exact match when one exists.
                best = min(variants, key=lambda v: abs(_height(v) - target_height))
                debug(f"[job {job_id}] quality={target_height}p requested -> picked {_height(best)}p variant")
            else:
                best = max(variants, key=_height)
                debug(f"[job {job_id}] quality=auto -> picked highest available ({_height(best)}p)")

            variant_url = _attach_token(url, urljoin(url, best[1].strip()))
            pl_bytes = fetch_bytes_retry(variant_url, headers=dl_headers)
            pl_text  = pl_bytes.decode("utf-8", errors="ignore")

        segment_urls = [
            _attach_token(variant_url, urljoin(variant_url, line.strip()))
            for line in pl_text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if not segment_urls:
            raise RuntimeError("No segments found in playlist")

        total = len(segment_urls)
        debug(f"[job {job_id}] {total} segments to fetch")

        ts_fd, ts_path   = tempfile.mkstemp(suffix=".ts")
        mp4_fd, mp4_path = tempfile.mkstemp(suffix=".mp4")
        os.close(ts_fd); os.close(mp4_fd)

        with open(ts_path, "wb") as f:
            for i, seg_url in enumerate(segment_urls):
                try:
                    data = extract_ts_packets(fetch_bytes_retry(seg_url, headers=dl_headers))
                    f.write(data)
                except Exception as e:
                    debug(f"[job {job_id}] segment {i} skipped: {e}")
                pct = round((i + 1) / total * 90)
                with _jobs_lock:
                    if _jobs.get(job_id, {}).get("status") == "pending":
                        _jobs[job_id]["pct"] = pct
                if i < total - 1:
                    time.sleep(SEGMENT_FETCH_DELAY)

        if os.path.getsize(ts_path) == 0:
            raise RuntimeError("All segment downloads failed — nothing to remux")

        # codec detection & ffmpeg remux
        video_codec, audio_codec = probe_codecs(ts_path)
        debug(f"[job {job_id}] codecs: video={video_codec}, audio={audio_codec}")
        video_args = ["-c:v", "copy"]
        audio_args = ["-c:a", "copy"]
        if audio_codec and audio_codec not in WIDELY_SUPPORTED_AUDIO_CODECS:
            audio_args = ["-c:a", "aac", "-b:a", "192k"]

        with _jobs_lock:
            if _jobs.get(job_id, {}).get("status") == "pending":
                _jobs[job_id]["pct"] = 95

        cmd = ["ffmpeg", "-y", "-i", ts_path, *video_args, *audio_args,
               "-movflags", "+faststart", mp4_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {result.stderr[-400:]}")

        with _jobs_lock:
            _jobs[job_id].update({"status": "done", "path": mp4_path, "pct": 100, "completed_at": time.time()})
        debug(f"[job {job_id}] done → {mp4_path}")

    except Exception as e:
        debug(f"[job {job_id}] error: {e}")
        with _jobs_lock:
            _jobs[job_id].update({"status": "error", "msg": str(e)})
        if mp4_path:
            try: os.remove(mp4_path)
            except OSError: pass
    finally:
        if ts_path:
            try: os.remove(ts_path)
            except OSError: pass


@app.route("/download_mp4")
def download_mp4():
    """Start a background download job; return job_id immediately."""
    url     = request.args.get("url")
    title   = request.args.get("title", "").strip()
    quality = request.args.get("quality", "").strip()  # target height in px, e.g. "1080"; "" = auto/highest
    if not url:
        return "Missing url param", 400
    if not shutil.which("ffmpeg"):
        return "ffmpeg not found — install with: sudo apt install ffmpeg", 500

    job_id     = str(uuid.uuid4())
    dl_headers = get_headers_for_stream(url)

    with _jobs_lock:
        _jobs[job_id] = {"status": "pending", "path": None, "title": title, "msg": None, "pct": 0}

    t = threading.Thread(target=_run_download_job, args=(job_id, url, title, dl_headers, quality), daemon=True)
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/download_ready")
def download_ready():
    """
    Poll this endpoint with ?job_id=...
    Returns JSON while pending/error, or streams the MP4 file when done.
    """
    job_id = request.args.get("job_id")
    if not job_id:
        return "Missing job_id", 400
    peek = request.args.get("peek") == "1"

    with _jobs_lock:
        job = dict(_jobs.get(job_id, {}))

    if not job:
        return jsonify({"status": "unknown"}), 404

    if job["status"] == "pending":
        return jsonify({"status": "pending", "pct": job.get("pct", 0)})

    if job["status"] == "error":
        with _jobs_lock:
            _jobs.pop(job_id, None)
        return jsonify({"status": "error", "msg": job.get("msg", "unknown error")}), 500

    # status == "done"
    if peek:
        # Just reporting status for the UI — leave the file in place until
        # the user actually taps "Download to device".
        return jsonify({"status": "done"})

    # Not a peek: stream the file to the client. We deliberately do NOT pop
    # the job or delete the file here — only once generate() below confirms
    # every byte was actually sent. If the connection drops partway through
    # (backgrounded tab, flaky network, etc.), the job and file are left
    # exactly as they were so the client can just retry the same job_id.
    mp4_path = job["path"]
    title    = job.get("title", "video")

    if not mp4_path or not os.path.isfile(mp4_path):
        with _jobs_lock:
            _jobs.pop(job_id, None)
        return "File not found on server", 404

    safe_title = re.sub(r'[^A-Za-z0-9 ._-]', '', title).strip() or "video"
    filename   = safe_title + ".mp4"
    mp4_size   = os.path.getsize(mp4_path)

    def generate():
        completed = False
        try:
            with open(mp4_path, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        completed = True
                        break
                    yield chunk
        finally:
            if completed:
                with _jobs_lock:
                    _jobs.pop(job_id, None)
                try:
                    os.remove(mp4_path)
                    debug(f"[job {job_id}] Fully delivered to client — removed {mp4_path}")
                except OSError:
                    pass
            else:
                debug(f"[job {job_id}] Transfer interrupted before completion — keeping file for retry")

    resp = Response(generate(), mimetype="video/mp4")
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp.headers["Content-Length"] = str(mp4_size)
    return resp


@app.route("/autocomplete")
def autocomplete():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])
    results = search_tmdb(query)
    suggestions = []
    for r in results:
        if r["media_type"] == "movie":
            year = r.get("release_date", "")[:4]
        else:
            year = r.get("first_air_date", "")[:4]
        display = (
            f"{r['title'] if r['media_type'] == 'movie' else r['name']} ({year})"
            if year else
            r['title'] if r['media_type'] == 'movie' else r['name']
        )
        suggestions.append(display)
    seen = set()
    unique_suggestions = []
    for s in suggestions:
        if s.lower() not in seen:
            unique_suggestions.append(s)
            seen.add(s.lower())
    titles_only = [s.split(" (")[0] for s in unique_suggestions]
    matches = [
        unique_suggestions[idx]
        for title, score, idx in process.extract(
            query, titles_only, scorer=fuzz.token_sort_ratio, limit=5
        )
    ]
    return jsonify(matches)

@app.route("/seasons")
def seasons():
    title = request.args.get("title", "").strip()
    if not title:
        return jsonify([])
    results = search_tmdb(title)
    best = get_best_match(title, results)
    if not best or best.get("media_type") != "tv":
        return jsonify([])
    tmdb_id = best["id"]
    seasons_list = get_seasons(tmdb_id)
    seasons_list = [s for s in seasons_list if s["season_number"] != 0]
    return jsonify({"tmdb_id": tmdb_id, "seasons": seasons_list})

@app.route("/episodes")
def episodes():
    tmdb_id = request.args.get("tmdb_id")
    season_number = request.args.get("season_number")
    if not tmdb_id or not season_number:
        return jsonify([])
    try:
        tmdb_id = int(tmdb_id)
        season_number = int(season_number)
    except ValueError:
        return jsonify([])
    return jsonify(get_released_episodes(tmdb_id, season_number))

@app.route("/get_m3u8")
def get_m3u8():
    title = request.args.get("title")
    season = request.args.get("season")
    episode = request.args.get("episode")
    year = request.args.get("year")
    if not title:
        return "", 400

    update_msg = []
    results = search_tmdb(title)
    update_msg.append(f"TMDb returned {len(results)} results")

    if year:
        filtered = []
        for r in results:
            r_year = None
            if r["media_type"] == "movie":
                r_year = r.get("release_date", "")[:4]
            else:
                r_year = r.get("first_air_date", "")[:4]
            if r_year == year:
                filtered.append(r)
        if filtered:
            results = filtered
            update_msg.append(f"Filtered results by year: {year} -> {len(results)} results")

    best = get_best_match(title, results)
    if not best:
        return "", 404

    tmdb_id = best["id"]
    type_ = best.get("media_type", "movie").lower()

    if type_ == "movie":
        external_url = f"https://api.themoviedb.org/3/movie/{tmdb_id}/external_ids?api_key={TMDB_API_KEY}"
    else:
        external_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/external_ids?api_key={TMDB_API_KEY}"

    external_resp = requests.get(external_url).json()
    imdb_id = external_resp.get("imdb_id")
    if not imdb_id:
        update_msg.append("IMDb ID not found")
        debug("\n".join(update_msg))
        return "", 404

    if type_ == "tv" and season and episode:
        vsrc_embed = f"https://vsrc.su/embed/tv?imdb={imdb_id}&season={season}&episode={episode}&dts=dd"
    else:
        vsrc_embed = f"https://vsrc.su/embed/movie?imdb={imdb_id}&dts=dd"

    iframe_src = get_player_iframe_src(vsrc_embed)
    if not iframe_src:
        return "", 404
    update_msg.append(f"Iframe src obtained: {iframe_src}")

    first_m3u8 = capture_first_m3u8(iframe_src, retries=3)
    if not first_m3u8:
        return "", 404
    update_msg.append(f"First m3u8 captured: {first_m3u8}")
    debug("\n".join(update_msg))

    remember_stream_origin(first_m3u8, _current_origin["value"])
    return first_m3u8


# ─────────────────────────────────────────────────────────────────────────────
# Episode prescraping
#
# While an episode is playing, once it's within its last few minutes we
# kick off a background scrape of the *next* episode (rolling over into the
# next season if the current one is finished) and cache the result. If the
# person then loads that next episode, /get_prescraped serves the
# already-captured m3u8 instantly instead of repeating the whole
# search+scrape pipeline.
# ─────────────────────────────────────────────────────────────────────────────

_prescrape_cache: dict = {}   # key -> {"status": "pending"|"done"|"error", "m3u8": str|None, "ts": float}
_prescrape_lock = threading.Lock()
PRESCRAPE_TTL_SECONDS = 60 * 60  # captured links are typically valid for hours; keep for 1h to stay safely fresh


def _prescrape_key(tmdb_id, season, episode) -> str:
    return f"{tmdb_id}:{season}:{episode}"


def _cleanup_stale_prescrapes():
    while True:
        time.sleep(120)
        now = time.time()
        with _prescrape_lock:
            stale = [k for k, v in _prescrape_cache.items() if now - v.get("ts", now) > PRESCRAPE_TTL_SECONDS]
            for k in stale:
                del _prescrape_cache[k]


threading.Thread(target=_cleanup_stale_prescrapes, daemon=True).start()


def _next_episode_for(tmdb_id: int, season: int, episode: int):
    """Returns (season, episode) of the next released episode after the
    given one, rolling over into the next season (e.g. S2E10 -> S3E1) once
    the current season is exhausted. Returns None if there's nothing next
    yet (e.g. the show hasn't aired further episodes)."""
    eps = get_released_episodes(tmdb_id, season)
    later_in_season = sorted(e["episode_number"] for e in eps if e["episode_number"] > episode)
    if later_in_season:
        return season, later_in_season[0]
    next_season_eps = get_released_episodes(tmdb_id, season + 1)
    if next_season_eps:
        return season + 1, min(e["episode_number"] for e in next_season_eps)
    return None


def _resolve_tv_episode_stream(tmdb_id: int, season: int, episode: int):
    """Same resolution pipeline as /get_m3u8's TV branch, but starting from
    an already-known tmdb_id instead of a title search — used for
    prescraping the next episode in the background."""
    external_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/external_ids?api_key={TMDB_API_KEY}"
    external_resp = requests.get(external_url, timeout=REQUEST_TIMEOUT).json()
    imdb_id = external_resp.get("imdb_id")
    if not imdb_id:
        return None
    vsrc_embed = f"https://vsrc.su/embed/tv?imdb={imdb_id}&season={season}&episode={episode}&dts=dd"
    iframe_src = get_player_iframe_src(vsrc_embed)
    if not iframe_src:
        return None
    return capture_first_m3u8(iframe_src, retries=3)


def _run_prescrape_job(key: str, tmdb_id: int, season: int, episode: int):
    # Scraping repoints the shared "current origin" while it works (the
    # same mechanism /get_m3u8 uses). Save/restore it around the job so a
    # prescrape running alongside an actively-playing stream doesn't leave
    # that global origin pointed at the wrong show any longer than the
    # scrape itself takes — the per-host header cache (remember_stream_
    # origin/get_headers_for_stream) is the real protection for the live
    # stream's own proxy requests; this is just belt-and-suspenders.
    saved_origin = _current_origin["value"]
    try:
        m3u8 = _resolve_tv_episode_stream(tmdb_id, season, episode)
        if m3u8:
            remember_stream_origin(m3u8, _current_origin["value"])
        with _prescrape_lock:
            _prescrape_cache[key] = {"status": "done" if m3u8 else "error", "m3u8": m3u8, "ts": time.time()}
        debug(f"[prescrape {key}] {'captured ' + m3u8 if m3u8 else 'failed to capture'}")
    except Exception as e:
        debug(f"[prescrape {key}] error: {e}")
        with _prescrape_lock:
            _prescrape_cache[key] = {"status": "error", "m3u8": None, "ts": time.time()}
    finally:
        _current_origin["value"] = saved_origin


@app.route("/prescrape_next")
def prescrape_next():
    """Idempotently kicks off a background scrape of the episode after the
    given one (rolling into the next season if needed). Fire-and-forget —
    the caller doesn't wait on this; it just polls /get_prescraped later."""
    tmdb_id = request.args.get("tmdb_id")
    season = request.args.get("season")
    episode = request.args.get("episode")
    if not tmdb_id or not season or not episode:
        return jsonify({"status": "invalid"}), 400
    try:
        tmdb_id, season, episode = int(tmdb_id), int(season), int(episode)
    except ValueError:
        return jsonify({"status": "invalid"}), 400

    nxt = _next_episode_for(tmdb_id, season, episode)
    if not nxt:
        return jsonify({"status": "none"})
    next_season, next_episode = nxt
    key = _prescrape_key(tmdb_id, next_season, next_episode)

    with _prescrape_lock:
        existing = _prescrape_cache.get(key)
        if existing and existing["status"] in ("pending", "done"):
            return jsonify({"status": existing["status"], "season": next_season, "episode": next_episode})
        _prescrape_cache[key] = {"status": "pending", "m3u8": None, "ts": time.time()}

    threading.Thread(target=_run_prescrape_job, args=(key, tmdb_id, next_season, next_episode), daemon=True).start()
    debug(f"[prescrape {key}] started (S{next_season}E{next_episode})")
    return jsonify({"status": "started", "season": next_season, "episode": next_episode})


@app.route("/get_prescraped")
def get_prescraped():
    tmdb_id = request.args.get("tmdb_id")
    season = request.args.get("season")
    episode = request.args.get("episode")
    if not tmdb_id or not season or not episode:
        return jsonify({"status": "none"})
    try:
        tmdb_id, season, episode = int(tmdb_id), int(season), int(episode)
    except ValueError:
        return jsonify({"status": "none"})
    key = _prescrape_key(tmdb_id, season, episode)
    with _prescrape_lock:
        entry = _prescrape_cache.get(key)
    if not entry:
        return jsonify({"status": "none"})
    if entry["status"] == "done" and entry["m3u8"]:
        return jsonify({"status": "done", "m3u8": entry["m3u8"]})
    return jsonify({"status": entry["status"]})


PLAYER_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="theme-color" content="#0c0c0e">
<title>Flex Stream</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
* { box-sizing: border-box; }
:root {
  --bg: #0c0c0e;
  --bg-elevated: #17171a;
  --bg-elevated-2: #1e1e22;
  --border: rgba(255,255,255,0.08);
  --border-soft: rgba(255,255,255,0.06);
  --cyan: #00e5e5;
  --cyan-bright: cyan;
  --cyan-dim: #00cccc;
  --text: #f2f2f4;
  --text-dim: #9a9aa2;
  --text-faint: #6b6b74;
  --danger: #ff6b6b;
  --radius: 14px;
  --radius-sm: 10px;
}
html {
  scrollbar-color: var(--border) transparent;
  background-color: var(--bg);
  height: 100%;
}
body {
  margin: 0;
  font-family: 'Inter', 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
  background: transparent;
  color: var(--text);
  display: flex;
  flex-direction: column;
  align-items: center;
  min-height: 100%;
  padding: max(32px, env(safe-area-inset-top)) max(20px, env(safe-area-inset-right)) max(32px, env(safe-area-inset-bottom)) max(20px, env(safe-area-inset-left));
  -webkit-font-smoothing: antialiased;
  overscroll-behavior-y: none;
  position: relative;
}
/* Oversized backdrop: bigger than the viewport in every direction and
   pinned with position:fixed, so no matter how far a browser's bounce/
   rubber-band overscroll goes, it's still showing this layer rather than
   revealing bare black underneath. */
body::before {
  content: '';
  position: fixed;
  top: -25vh; left: -25vw;
  width: 150vw; height: 150vh;
  background-color: var(--bg);
  background-image:
    radial-gradient(circle at 15% 0%, rgba(0,229,229,0.07), transparent 45%),
    radial-gradient(circle at 85% 100%, rgba(0,229,229,0.05), transparent 40%);
  z-index: -1;
  pointer-events: none;
}
h1 {
  margin: 0 0 26px;
  font-weight: 800;
  font-size: 1.9rem;
  letter-spacing: -0.02em;
  display: flex; align-items: center; gap: 2px;
}
h1 .cyan {
  color: var(--cyan-bright);
  text-shadow: 0 0 22px rgba(0,255,255,0.35);
}
#controls {
  display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 22px;
  width: 100%; max-width: 900px; justify-content: center;
  background: linear-gradient(180deg, var(--bg-elevated), rgba(23,23,26,0.6));
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px;
  box-shadow: 0 8px 30px rgba(0,0,0,0.35);
}
input, select {
  padding: 11px 14px; border-radius: var(--radius-sm); border: 1px solid var(--border);
  flex: 1 1 150px; max-width: 250px; font-size: 16px;
  background: var(--bg-elevated-2); color: var(--text);
  font-family: inherit; transition: border-color 0.15s ease, box-shadow 0.15s ease;
  outline: none;
}
input::placeholder { color: var(--text-faint); }
input:focus, select:focus {
  border-color: var(--cyan);
  box-shadow: 0 0 0 3px rgba(0,229,229,0.15);
}
select { cursor: pointer; }
/* Swapped box treatment: title input is now the light box, season/episode keep the dark box */
#title {
  background: #eef1f2;
  color: #14181a;
}
#title::placeholder { color: #6b7378; }
button {
  padding: 11px 22px; background: linear-gradient(135deg, var(--cyan-bright), var(--cyan-dim));
  color: #000;
  border: none; border-radius: var(--radius-sm); cursor: pointer; font-weight: 700; font-size: 0.95rem;
  font-family: inherit;
  transition: transform 0.15s ease, box-shadow 0.15s ease, filter 0.15s ease;
  box-shadow: 0 4px 14px rgba(0,229,229,0.22);
}
button:hover { filter: brightness(1.06); box-shadow: 0 6px 20px rgba(0,229,229,0.32); transform: translateY(-1px); }
button:active { transform: translateY(0); }
#video-container {
  position: relative; width: 80%; max-width: 900px;
  border-radius: var(--radius); overflow: hidden;
  box-shadow: 0 12px 40px rgba(0,0,0,0.5);
  border: 1px solid var(--border);
}
#downloadBtn { margin-top: 18px; }
#downloadBtn:disabled { background: #3a3a3f; color: #74747c; cursor: not-allowed; box-shadow: none; transform: none; filter: none; }
#downloadQuality {
  -webkit-appearance: none; -moz-appearance: none; appearance: none;
  display: inline-block;
  flex: none;
  margin-top: 18px; margin-left: 10px;
  width: auto; min-width: 130px; max-width: 160px;
  height: 44px; line-height: 20px;
  padding: 0 14px;
  font-size: 0.95rem;
  text-align: center; text-align-last: center;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  background: var(--bg-elevated-2);
  color: var(--text);
  font-family: inherit;
  vertical-align: middle;
}
#downloads-panel {
  width: 80%; max-width: 900px; margin-top: 14px;
  display: flex; flex-direction: column; gap: 10px;
}
.download-item {
  background: linear-gradient(180deg, var(--bg-elevated), var(--bg));
  border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 12px 16px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.25);
}
.download-item .dl-title-row {
  display: flex; justify-content: space-between; align-items: center;
  font-size: 0.9rem; margin-bottom: 8px; gap: 10px;
}
.download-item .dl-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; font-weight: 500; }
.download-item .dl-status { color: var(--text-dim); font-size: 0.78rem; white-space: nowrap; font-weight: 500; }
.download-item .dl-status.error { color: var(--danger); }
.download-item .dl-status.done  { color: var(--cyan-bright); }
.download-item .dl-bar-track { width: 100%; height: 6px; background: var(--bg-elevated-2); border-radius: 4px; overflow: hidden; }
.download-item .dl-bar-fill  { height: 100%; background: linear-gradient(90deg, var(--cyan-dim), var(--cyan-bright)); width: 0%; transition: width 0.2s ease; }
.download-item .dl-bar-fill.indeterminate { width: 30%; animation: dl-indeterminate 1.2s ease-in-out infinite; }
@keyframes dl-indeterminate { 0% { margin-left: -30%; } 100% { margin-left: 100%; } }
.download-item .dl-action-row { margin-top: 10px; text-align: right; }
.download-item .dl-save-btn {
  padding: 7px 16px; background: linear-gradient(135deg, var(--cyan-bright), var(--cyan-dim)); color: #000; border: none;
  border-radius: 8px; cursor: pointer; font-weight: 700; font-size: 0.82rem;
  font-family: inherit; transition: transform 0.15s ease, filter 0.15s ease;
}
.download-item .dl-save-btn:hover { filter: brightness(1.06); transform: translateY(-1px); }
.download-item .dl-save-btn:disabled { background: #3a3a3f; color: #74747c; cursor: not-allowed; transform: none; filter: none; }
#video { width: 100%; display: block; background: #000; aspect-ratio: 16 / 9; }
#loading {
  position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
  display: none; align-items: center; justify-content: center;
  width: 64px; height: 64px; border-radius: 50%;
  background: rgba(10,10,12,0.6);
  backdrop-filter: blur(6px);
  border: 1px solid var(--border);
}
.spinner {
  width: 32px; height: 32px;
  border: 3px solid rgba(255,255,255,0.2);
  border-top-color: var(--cyan-bright);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
#debug-overlay {
  margin-top: 14px;
  color: var(--cyan-bright);
  font-size: 11px;
  font-family: 'SFMono-Regular', Menlo, monospace;
  text-align: center;
  min-height: 14px;
  white-space: pre-line;
}
#quality {
  position: absolute; top: 12px; right: 12px; z-index: 100;
  background-color: rgba(10,10,12,0.7); color: #fff; border: 1px solid rgba(255,255,255,0.15);
  border-radius: 8px; padding: 7px 12px; font-size: 0.82rem; max-width: 110px; cursor: pointer;
  font-family: 'Inter', sans-serif; backdrop-filter: blur(6px);
  transition: background-color 0.15s ease;
}
#quality:hover { background-color: rgba(10,10,12,0.9); }
#nextEpisodeBtn {
  /* Sits above the native <video> controls bar (which includes the
     fullscreen button in its bottom-right corner) rather than on top of
     it, so the two never overlap/compete for taps. */
  position: absolute; bottom: 58px; right: 14px; z-index: 100;
  background: rgba(10,10,12,0.6); color: #fff; border: 1px solid rgba(255,255,255,0.2);
  border-radius: 8px; padding: 9px 16px; font-size: 0.85rem; font-weight: 600; cursor: pointer;
  font-family: 'Inter', sans-serif; backdrop-filter: blur(6px);
  display: none; align-items: center; gap: 6px; box-shadow: 0 4px 16px rgba(0,0,0,0.35);
  transition: background 0.15s ease, transform 0.15s ease;
}
#nextEpisodeBtn:hover { background: rgba(10,10,12,0.85); transform: translateY(-1px); }
.autocomplete-dropdown {
  position: absolute; background: var(--bg-elevated-2); color: #fff; list-style: none;
  padding: 6px; margin: 0; border-radius: var(--radius-sm); z-index: 1000; display: none;
  border: 1px solid var(--border); box-shadow: 0 10px 30px rgba(0,0,0,0.45);
}
.autocomplete-dropdown li { cursor: pointer; padding: 7px 10px; border-radius: 6px; font-size: 0.9rem; transition: background-color 0.1s ease; }
.autocomplete-dropdown li:hover { background: rgba(0,229,229,0.12); }
footer { margin-top: auto; padding-top: 24px; text-align: center; padding: 24px 10px 10px; font-size: 0.82rem; color: var(--text-faint); }
@media (max-width: 768px) {
  #controls { flex-direction: column; align-items: stretch; gap: 10px; }
  #controls input, #controls select {
    flex: none; width: 100%; max-width: 100%; box-sizing: border-box;
    text-align: center; text-align-last: center; color: var(--text);
  }
  #controls button {
    flex: none; width: 100%; max-width: 100%; box-sizing: border-box;
    text-align: center; color: #000;
  }
  #video-container { width: 98%; max-width: 100%; }
  #video-container video {
    width: 100%; height: 35vh; max-height: 40vh; margin: 0 auto;
    display: block; object-fit: contain;
  }
  #downloads-panel { width: 98%; }
  footer { margin-top: 10px; margin-bottom: 20px; }
  #controls button { width: 50%; min-height: 60px; padding: 0; align-self: center; font-size: 1.4rem; }
  #controls .select-wrapper { position: relative; width: 100%; }
  #controls select {
    -webkit-appearance: none !important; appearance: none !important;
    width: 100%; padding: 11px; text-align: center; text-align-last: center;
    background: var(--bg-elevated-2); color: var(--text); border: 1px solid var(--border); border-radius: var(--radius-sm); box-sizing: border-box;
  }
  #controls #title {
    background: #eef1f2;
    color: #14181a;
  }
  #loading {
    width: 52px; height: 52px; display: none;
  }
  .spinner { width: 26px; height: 26px; border-width: 3px; }
  #nextEpisodeBtn { bottom: 48px; right: 10px; padding: 8px 12px; font-size: 0.78rem; }
  #downloadQuality { margin-left: 8px; min-width: 110px; max-width: 130px; height: 40px; line-height: 18px; padding: 0 10px; font-size: 0.85rem; }
}
</style>
</head>
<body>
<h1><span class="cyan">Flex</span> Stream</h1>
<div id="controls">
  <input id="title" type="text" placeholder="Title"/>
  <select id="season"><option value="">Season</option></select>
  <select id="episode"><option value="">Episode</option></select>
  <button onclick="load()">Load &amp; Play</button>
  <ul id="autocomplete" class="autocomplete-dropdown"></ul>
</div>
<div id="video-container">
  <video id="video" controls crossorigin playsinline x-webkit-airplay="allow"></video>
  <select id="quality" style="display:none;"><option value="-1">Auto</option></select>
  <button id="nextEpisodeBtn">Next Episode &#9656;</button>
  <div id="loading"><div class="spinner"></div></div>
</div>
<button id="downloadBtn">Download MP4</button>
<select id="downloadQuality">
  <option value="1080">High</option>
  <option value="720" selected>Medium</option>
  <option value="480">Low</option>
</select>
<div id="downloads-panel"></div>
<div id="debug-overlay"></div>
<footer>v2.0</footer>
<script>
const video        = document.getElementById('video');
const loading      = document.getElementById('loading');
const debugOverlay = document.getElementById('debug-overlay');
const titleInput   = document.getElementById('title');
const seasonSelect = document.getElementById('season');
const episodeSelect= document.getElementById('episode');
const qualitySelect= document.getElementById('quality');
const downloadQualitySelect = document.getElementById('downloadQuality');
const dropdown     = document.getElementById('autocomplete');
const downloadBtn  = document.getElementById('downloadBtn');
const nextEpisodeBtn = document.getElementById('nextEpisodeBtn');
let hlsInstance    = null;
let usingNativeHls = false; // true when this browser plays HLS natively (required for real AirPlay)
let currentLevels  = null;  // hls.js levels array from the last MANIFEST_PARSED, used to resolve download quality
let selectedTmdbId = null;
let currentM3u8Url = null;
let currentTitle   = null;
let lastKnownTime  = 0;
let recoveryInProgress = false;
let loadedQueryKey = null;
let loadedTmdbId     = null;
let loadedSeasonNum  = null;
let loadedEpisodeNum = null;
let prescrapeTriggeredForKey = null;
let nextEpisodeTarget = null; // {season, episode} once known, shown via nextEpisodeBtn

function currentQueryKey(){
    const {title,year}=parseTitleAndYear(titleInput.value.trim());
    return [title.trim().toLowerCase(), year||'', seasonSelect.value||'', episodeSelect.value||''].join('|');
}

// Once we're within the last 5 minutes of a TV episode — whether reached
// by watching naturally or by seeking/skipping there — kick off a
// background scrape of the next episode (rolling into the next season if
// this one's finished), so advancing to it later can skip straight to
// playback. Only fires once per loaded episode. The same prescrape lookup
// tells us what the next episode actually is, so we reuse it to reveal the
// "Next Episode" button once we know where it should take the viewer.
const PRESCRAPE_WINDOW_SECONDS = 300;
function maybeTriggerNextEpisodePrescrape(){
    if (!loadedTmdbId || !loadedSeasonNum || !loadedEpisodeNum) return;
    if (prescrapeTriggeredForKey === loadedQueryKey) return;
    if (!isFinite(video.duration) || video.duration <= 0) return;
    if (video.duration - video.currentTime > PRESCRAPE_WINDOW_SECONDS) return;
    prescrapeTriggeredForKey = loadedQueryKey; // only ever try once per loaded episode
    fetch('/prescrape_next?tmdb_id='+encodeURIComponent(loadedTmdbId)+'&season='+encodeURIComponent(loadedSeasonNum)+'&episode='+encodeURIComponent(loadedEpisodeNum))
        .then(r=>r.json())
        .then(function(d){
            if (d && d.season && d.episode) {
                console.log('Preloading next episode: S'+d.season+'E'+d.episode);
                nextEpisodeTarget = { season: d.season, episode: d.episode };
                nextEpisodeBtn.style.display = 'flex';
            }
        })
        .catch(()=>{});
}

video.addEventListener('timeupdate', () => {
    lastKnownTime = video.currentTime;
    maybeTriggerNextEpisodePrescrape();
});

function showLoading(show){ loading.style.display = show ? 'flex' : 'none'; }
function updateDebug(msg){ debugOverlay.textContent = msg; }

// Lets AirPlay (and the lock screen / Now Playing widget) show the actual
// title of what's playing instead of just this page's title ("Flex
// Stream"). Supported by Safari on macOS/iOS/iPadOS as well as Chrome.
function updateMediaSessionMetadata(title){
    if (!('mediaSession' in navigator)) return;
    try{
        navigator.mediaSession.metadata = new MediaMetadata({ title: title || 'Video' });
    }catch(e){}
}

function levelLabel(level){
    if(level.height)   return level.height + 'p';
    if(level.bitrate)  return Math.round(level.bitrate/1000) + ' kbps';
    return 'Unknown';
}

function populateQualityLevels(levels){
    qualitySelect.innerHTML = '<option value="-1">Auto</option>';
    if(!levels || levels.length < 2){ qualitySelect.style.display='none'; return; }
    levels
        .map((lvl,idx) => ({idx,lvl}))
        .sort((a,b) => (b.lvl.height||0)-(a.lvl.height||0) || (b.lvl.bitrate||0)-(a.lvl.bitrate||0))
        .forEach(({idx,lvl}) => {
            const opt = document.createElement('option');
            opt.value = idx; opt.textContent = levelLabel(lvl);
            qualitySelect.appendChild(opt);
        });
    qualitySelect.value = '-1';
    qualitySelect.style.display = 'inline-block';
}

qualitySelect.addEventListener('change', () => {
    if(!hlsInstance) return;
    hlsInstance.currentLevel = parseInt(qualitySelect.value, 10);
});

// ── Download quality ────────────────────────────────────────────────────
// Playback quality (above) auto-switches with the connection — that's
// handled internally by hls.js's ABR when hls.js is driving playback, or
// by the browser/OS itself (AVFoundation) when native HLS is used for
// AirPlay-capable Safari. A download is a one-shot file rather than
// adaptive playback, so it gets its own fixed Low/Medium/High selector
// (always available, even before anything has loaded — Download triggers
// its own load if needed). Each option is just a target resolution; the
// server already picks whichever actual rendition in the stream's master
// playlist is closest to that target, so this "rounds" to the nearest
// quality the stream actually offers.

// Resolves the Low/Medium/High choice into a target vertical resolution
// (e.g. 720) to send to /download_mp4. The server matches this against the
// stream's actual available renditions and picks whichever is closest —
// so this always "rounds" to the nearest quality the stream really offers.
function getSelectedDownloadQuality(){
    return parseInt(downloadQualitySelect.value, 10);
}

const RECENT_KEY = 'flexstream_recent_searches';
function getRecent(){ try{ const r=localStorage.getItem(RECENT_KEY); const a=r?JSON.parse(r):[]; return Array.isArray(a)?a:[]; }catch(e){return[];} }
function addRecent(t){ if(!t)return; let a=getRecent().filter(s=>s.toLowerCase()!==t.toLowerCase()); a.unshift(t); a=a.slice(0,5); try{localStorage.setItem(RECENT_KEY,JSON.stringify(a));}catch(e){} }

function showDropdownList(items,{recent}={}){
    if(!items||items.length===0){dropdown.style.display='none';return;}
    dropdown.innerHTML='';
    if(recent){
        const h=document.createElement('li'); h.textContent='Recent searches';
        h.style.cssText='opacity:.6;cursor:default;font-size:.8em;'; dropdown.appendChild(h);
    }
    items.forEach(s=>{
        const li=document.createElement('li'); li.textContent=s;
        li.addEventListener('click',()=>{ titleInput.value=s; dropdown.style.display='none'; loadSeasons(s.replace(/\s+\(\d{4}\)$/,'')); });
        dropdown.appendChild(li);
    });
    const rect=titleInput.getBoundingClientRect();
    dropdown.style.top=rect.bottom+window.scrollY+'px';
    dropdown.style.left=rect.left+window.scrollX+'px';
    dropdown.style.width=rect.width+'px';
    dropdown.style.display='block';
}

titleInput.addEventListener('focus',()=>{ if(titleInput.value.trim())return; const r=getRecent(); if(r.length)showDropdownList(r,{recent:true}); });
titleInput.addEventListener('input',()=>{
    const q=titleInput.value.trim();
    selectedTmdbId=null;
    seasonSelect.innerHTML='<option value="">Season</option>';
    episodeSelect.innerHTML='<option value="">Episode</option>';
    dropdown.style.display='none';
    if(!q){ const r=getRecent(); if(r.length)showDropdownList(r,{recent:true}); return; }
    fetch('/autocomplete?q='+encodeURIComponent(q)).then(r=>r.json()).then(s=>{
        if(Array.isArray(s)&&s.length) showDropdownList(s);
    });
});
document.addEventListener('click',(e)=>{
    if(!titleInput.contains(e.target)&&!dropdown.contains(e.target)) dropdown.style.display='none';
});

function loadSeasons(title){
    fetch('/seasons?title='+encodeURIComponent(title)).then(r=>r.json()).then(data=>{
        selectedTmdbId=data.tmdb_id;
        seasonSelect.innerHTML='<option value="">Season</option>';
        data.seasons.forEach(s=>{ const o=document.createElement('option'); o.value=s.season_number; o.textContent=s.name; seasonSelect.appendChild(o); });
        episodeSelect.innerHTML='<option value="">Episode</option>';
    });
}

seasonSelect.addEventListener('change',()=>{
    const sn=seasonSelect.value; if(!sn||!selectedTmdbId) return;
    fetch('/episodes?tmdb_id='+selectedTmdbId+'&season_number='+sn).then(r=>r.json()).then(eps=>{
        episodeSelect.innerHTML='<option value="">Episode</option>';
        const today=new Date();
        eps.forEach(ep=>{
            if(!ep.air_date||new Date(ep.air_date)>today) return;
            const o=document.createElement('option'); o.value=ep.episode_number;
            o.textContent=ep.episode_number+': '+ep.name; episodeSelect.appendChild(o);
        });
        if(episodeSelect.options.length===1){
            const o=document.createElement('option'); o.textContent='No released episodes'; o.disabled=true; episodeSelect.appendChild(o);
        }
    });
});

function parseTitleAndYear(f){ const m=f.match(/^(.*)\s+\((\d{4})\)$/); return m?{title:m[1],year:m[2]}:{title:f,year:null}; }

// ── Stream cache ─────────────────────────────────────────────────────────
// Whenever a stream loads successfully we remember its m3u8 URL. If the
// player later errors out, the fastest fix is usually to just re-attach to
// this same URL (most errors are transient upstream hiccups, not an
// actually-expired link) — only falling back to a full TMDb search + scrape
// (10-30s) when re-attaching also fails. Kept in localStorage (not just a
// JS variable) so it also survives the tab being fully reloaded/killed
// while backgrounded, not just briefly suspended.
const STREAM_CACHE_KEY = 'flexstream_last_stream';
function saveStreamCache(m3u8, title, queryKey){
    try{ localStorage.setItem(STREAM_CACHE_KEY, JSON.stringify({ m3u8, title, queryKey, ts: Date.now() })); }catch(e){}
}
function getStreamCache(){
    try{ const r=localStorage.getItem(STREAM_CACHE_KEY); return r?JSON.parse(r):null; }catch(e){ return null; }
}

// Shared attach logic for both a freshly-resolved URL and a cached one.
// onReady()/onFail() let callers (quick reattach vs. full resolve) react
// differently to success/failure instead of duplicating the hls.js wiring.
//
// Native HLS (video.src = proxied url, decoded by the browser/OS itself) is
// tried FIRST wherever the browser supports it (Safari on macOS/iOS/iPadOS
// and tvOS). That's required for AirPlay to hand off *real remote
// playback* — full duration/scrub bar/timestamps and audio — to an Apple
// TV, instead of the "screen mirroring only" fallback you get when the
// element is driven by hls.js/MediaSource. hls.js is only used as a
// fallback on browsers with no native HLS support (Chrome, Firefox,
// Android). Safari's own built-in video controls already expose an
// AirPlay icon for the native path, so no extra button is needed here.
function attachStream(m3u8, resumeAt, onReady, onFail){
    const proxied='/proxy_playlist?url='+encodeURIComponent(m3u8);
    let readyFired=false;
    const nativeHlsSupported = video.canPlayType('application/vnd.apple.mpegurl');

    if(nativeHlsSupported){
        usingNativeHls = true;
        currentLevels = null; // native playback has no manual per-level list to expose
        if(hlsInstance){ hlsInstance.destroy(); hlsInstance = null; }
        qualitySelect.style.display='none';
        video.src=proxied;
        video.addEventListener('loadedmetadata',function onMeta(){
            if(resumeAt && resumeAt>1) video.currentTime=resumeAt;
            video.play().catch(()=>{});
            video.removeEventListener('loadedmetadata', onMeta);
            readyFired=true;
            updateDebug('Playing (native HLS, AirPlay-ready)');
            if(onReady) onReady();
        });
        video.addEventListener('error', function onErr(){
            video.removeEventListener('error', onErr);
            if (!readyFired && onFail) onFail();
        }, { once: true });
    } else if(Hls.isSupported()){
        usingNativeHls = false;
        if(hlsInstance) hlsInstance.destroy();
        hlsInstance=new Hls();
        hlsInstance.loadSource(proxied);
        hlsInstance.attachMedia(video);
        hlsInstance.on(Hls.Events.MANIFEST_PARSED,(event,data)=>{
            currentLevels = data.levels;
            populateQualityLevels(data.levels);
            if(resumeAt && resumeAt>1) video.currentTime=resumeAt;
            video.play().catch(()=>{});
            readyFired=true;
            if(onReady) onReady();
        });
        hlsInstance.on(Hls.Events.LEVEL_SWITCHED,(event,data)=>{
            const lvl=hlsInstance.levels[data.level];
            if(lvl) updateDebug('Playing: '+levelLabel(lvl)+(qualitySelect.value==='-1'?' (auto)':''));
        });
        hlsInstance.on(Hls.Events.ERROR,(e,data)=>{
            console.error('Hls.js error:', data);
            if (!data.fatal) return;
            switch (data.type) {
                case Hls.ErrorTypes.NETWORK_ERROR:
                    updateDebug('Network hiccup, recovering...');
                    hlsInstance.startLoad();
                    break;
                case Hls.ErrorTypes.MEDIA_ERROR:
                    updateDebug('Playback hiccup, recovering...');
                    hlsInstance.recoverMediaError();
                    break;
                default:
                    // Not internally recoverable by hls.js itself.
                    if (!readyFired && onFail) onFail();
                    else reresolveAndResume();
                    break;
            }
        });
    } else {
        updateDebug('HLS not supported in this browser');
        if (onFail) onFail(); else alert('HLS not supported in this browser');
    }
}

function resolveAndPlay(resumeAt){
    // Resolves whatever is currently in the title/season/episode fields,
    // starts it playing, and returns a Promise of the resolved m3u8 URL (or
    // null on failure). Used by the Load & Play button, the Download
    // button, and as the fallback path when a cached-URL quick reattach
    // fails, so downloading/recovery never need a separate load step first.
    const {title,year}=parseTitleAndYear(titleInput.value.trim());
    if(!title){ alert('Enter a title'); return Promise.resolve(null); }
    const season=seasonSelect.value, episode=episodeSelect.value;
    const queryKey=[title.trim().toLowerCase(), year||'', season||'', episode||''].join('|');
    currentM3u8Url=null;
    currentTitle=titleInput.value.trim()||title;
    if(season) currentTitle+=' S'+season;
    if(episode) currentTitle+='E'+episode;
    showLoading(true); updateDebug('Searching TMDb...'); qualitySelect.style.display='none';

    function finish(m3u8){
        showLoading(false);
        if(!m3u8){ updateDebug('No video found'); alert('No video found'); return null; }
        updateDebug('Video URL captured. Loading HLS...');
        currentM3u8Url=m3u8;
        loadedQueryKey=queryKey;
        loadedTmdbId=selectedTmdbId||null;
        loadedSeasonNum=season?parseInt(season,10):null;
        loadedEpisodeNum=episode?parseInt(episode,10):null;
        prescrapeTriggeredForKey=null; // this episode hasn't triggered its own next-episode prescrape yet
        nextEpisodeTarget=null;
        nextEpisodeBtn.style.display='none';
        downloadBtn.disabled=false;
        addRecent(titleInput.value.trim()||title);
        saveStreamCache(m3u8, currentTitle, loadedQueryKey);
        updateMediaSessionMetadata(currentTitle);
        attachStream(m3u8, resumeAt);
        return m3u8;
    }

    function fullScrape(){
        let url='/get_m3u8?title='+encodeURIComponent(title);
        if(year) url+='&year='+year;
        if(season) url+='&season='+season;
        if(episode) url+='&episode='+episode;
        return fetch(url).then(r=>r.text()).then(finish)
            .catch(err=>{ showLoading(false); updateDebug('Error: '+err); alert('Failed to load video'); return null; });
    }

    // If this exact episode was already prescraped in the background while
    // the previous one was finishing up, skip the search+scrape entirely
    // and use that instantly.
    if (selectedTmdbId && season && episode) {
        const preUrl='/get_prescraped?tmdb_id='+encodeURIComponent(selectedTmdbId)+'&season='+encodeURIComponent(season)+'&episode='+encodeURIComponent(episode);
        return fetch(preUrl).then(r=>r.json()).then(function(pre){
            if (pre && pre.status==='done' && pre.m3u8) {
                updateDebug('Using preloaded stream...');
                return finish(pre.m3u8);
            }
            return fullScrape();
        }).catch(fullScrape);
    }
    return fullScrape();
}

function load(){
    resolveAndPlay();
}

// ── Next Episode ─────────────────────────────────────────────────────────
// Populates the season/episode dropdowns to match nextEpisodeTarget (set by
// maybeTriggerNextEpisodePrescrape once the upcoming episode is known),
// then loads it. Reuses /get_prescraped under the hood via resolveAndPlay,
// so if the background prescrape already finished this jumps straight to
// playback with no extra search/scrape wait.
function goToNextEpisode(){
    if (!nextEpisodeTarget || !selectedTmdbId) return;
    const target = nextEpisodeTarget;
    nextEpisodeBtn.disabled = true;
    seasonSelect.value = target.season;
    fetch('/episodes?tmdb_id='+selectedTmdbId+'&season_number='+target.season).then(r=>r.json()).then(eps=>{
        episodeSelect.innerHTML='<option value="">Episode</option>';
        const today=new Date();
        eps.forEach(ep=>{
            if(!ep.air_date||new Date(ep.air_date)>today) return;
            const o=document.createElement('option'); o.value=ep.episode_number;
            o.textContent=ep.episode_number+': '+ep.name; episodeSelect.appendChild(o);
        });
        episodeSelect.value = target.episode;
        nextEpisodeBtn.style.display = 'none';
        nextEpisodeBtn.disabled = false;
        resolveAndPlay();
    }).catch(()=>{ nextEpisodeBtn.disabled = false; });
}

nextEpisodeBtn.addEventListener('click', goToNextEpisode);

// ── Auto-recovery ────────────────────────────────────────────────────────
// If playback dies (a rare transient upstream hiccup) or the tab/phone was
// backgrounded for a while (the stream's signed m3u8 URL can expire, or the
// connection can just drop), this quietly recovers instead of showing a
// dead player. It tries the fast path first — just re-attach to the same
// cached m3u8 URL — and only pays for a full TMDb search + re-scrape if
// that fails too (e.g. the link genuinely expired).
function isStreamBroken(){
    if (!currentM3u8Url) return false;
    if (video.error) return true;
    if (video.networkState === HTMLMediaElement.NETWORK_NO_SOURCE) return true;
    return false;
}

function reresolveAndResume(){
    if (recoveryInProgress) return;
    recoveryInProgress = true;
    const resumeTime = lastKnownTime;
    const cached = getStreamCache();
    const cachedUrl = currentM3u8Url || (cached && cached.m3u8);

    function fullResolve(){
        updateDebug('Reconnecting (full search)...');
        resolveAndPlay(resumeTime).then(function(){ recoveryInProgress = false; })
            .catch(function(){ recoveryInProgress = false; });
    }

    if (cachedUrl) {
        updateDebug('Reconnecting...');
        let settled = false;
        attachStream(cachedUrl, resumeTime, function onReady(){
            if (settled) return;
            settled = true;
            currentM3u8Url = cachedUrl;
            recoveryInProgress = false;
        }, function onFail(){
            if (settled) return;
            settled = true;
            fullResolve();
        });
    } else {
        fullResolve();
    }
}

document.addEventListener('visibilitychange', function(){
    if (document.visibilityState !== 'visible') return;
    if (!currentM3u8Url || recoveryInProgress) return;
    // Give the browser a moment to report its real state after resuming
    // before deciding the stream actually broke.
    setTimeout(function(){
        if (isStreamBroken()) reresolveAndResume();
    }, 500);
});


// ── Download ─────────────────────────────────────────────────────────────
// The actual fetch+remux work happens entirely server-side in a background
// thread (see /download_mp4 + /download_ready), so it keeps running even if
// this tab is backgrounded, frozen, or closed. We persist the job list in
// localStorage so reopening the page reconnects to anything still running
// or finished waiting to be saved, instead of losing track of it.
const downloadsPanel = document.getElementById('downloads-panel');
const ACTIVE_JOBS_KEY = 'flexstream_active_jobs';

function getActiveJobs(){ try{ const r=localStorage.getItem(ACTIVE_JOBS_KEY); const a=r?JSON.parse(r):[]; return Array.isArray(a)?a:[]; }catch(e){ return []; } }
function saveActiveJobs(jobs){ try{ localStorage.setItem(ACTIVE_JOBS_KEY, JSON.stringify(jobs)); }catch(e){} }
function rememberJob(jobId, title){ const jobs=getActiveJobs().filter(j=>j.job_id!==jobId); jobs.push({job_id:jobId, title:title}); saveActiveJobs(jobs); }
function forgetJob(jobId){ saveActiveJobs(getActiveJobs().filter(j=>j.job_id!==jobId)); }

function trackJob(jobId, title) {
    const item = document.createElement('div');
    item.className = 'download-item';
    const safeDisplay = title.replace(/</g, '&lt;');
    item.innerHTML =
        '<div class="dl-title-row">' +
            '<span class="dl-title">' + safeDisplay + '</span>' +
            '<span class="dl-status">Checking…</span>' +
        '</div>' +
        '<div class="dl-bar-track"><div class="dl-bar-fill indeterminate"></div></div>';
    downloadsPanel.prepend(item);

    const statusEl  = item.querySelector('.dl-status');
    const barEl     = item.querySelector('.dl-bar-fill');
    const safeTitle = title.replace(/[^A-Za-z0-9 ._-]/g, '').trim() || 'video';

    function setError(msg) {
        barEl.classList.remove('indeterminate');
        statusEl.classList.add('error');
        statusEl.textContent = 'Failed: ' + msg;
        forgetJob(jobId);
    }

    function setProgress(pct) {
        barEl.classList.remove('indeterminate');
        barEl.style.width = pct + '%';
        statusEl.textContent = pct + '%';
    }

    function triggerSave(btn) {
        // Hand this off to the browser's own download manager instead of
        // fetching it into JS as a Blob. Blob-buffering requires holding
        // the entire file in memory before it can be saved, which is why
        // large (multi-GB) files were getting killed mid-transfer on
        // mobile. A plain navigation to the URL lets the browser stream
        // straight to disk — the server already sends
        // Content-Disposition: attachment, so this triggers a real
        // download rather than opening the video in the tab.
        const a = document.createElement('a');
        a.href = '/download_ready?job_id=' + encodeURIComponent(jobId);
        a.download = safeTitle + '.mp4';
        document.body.appendChild(a);
        a.click();
        a.remove();

        statusEl.classList.add('done');
        statusEl.textContent = "Saving";
        if (btn) { btn.disabled = true; }

        // The browser has now taken over the transfer to disk. We no longer
        // need to track this job client-side, so drop it from localStorage
        // and remove its card from the page — there's nothing further to
        // poll or retry from here (the browser's own download manager owns
        // the rest of the transfer).
        forgetJob(jobId);
        if (pollTimer) clearTimeout(pollTimer);
        item.style.transition = 'opacity 0.3s ease';
        item.style.opacity = '0';
        setTimeout(function() { item.remove(); }, 300);
    }

    function showDownloadReady() {
        setProgress(100);
        statusEl.classList.add('done');
        statusEl.textContent = 'Ready';

        const actionRow = document.createElement('div');
        actionRow.className = 'dl-action-row';
        const saveBtn = document.createElement('button');
        saveBtn.className = 'dl-save-btn';
        saveBtn.textContent = 'Save';
        saveBtn.addEventListener('click', function() {
            triggerSave(saveBtn);
        });
        actionRow.appendChild(saveBtn);
        item.appendChild(actionRow);
    }

    // Poll /download_ready in "peek" mode (status only, doesn't consume the
    // file) every 2 s until the server-side job finishes or errors.
    let pollTimer = null;
    function poll() {
        fetch('/download_ready?job_id=' + encodeURIComponent(jobId) + '&peek=1')
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.status === 'pending') {
                    statusEl.textContent = 'Downloading…';
                    if (d.pct != null) setProgress(d.pct);
                    pollTimer = setTimeout(poll, 2000);
                } else if (d.status === 'done') {
                    showDownloadReady();
                } else if (d.status === 'unknown') {
                    setError('job no longer exists on server');
                } else {
                    setError(d.msg || 'unknown error');
                }
            })
            .catch(function() {
                // transient network glitch — keep polling
                pollTimer = setTimeout(poll, 3000);
            });
    }
    poll();
}

function startDownload(m3u8Url, title, quality) {
    let url = '/download_mp4?url=' + encodeURIComponent(m3u8Url) + '&title=' + encodeURIComponent(title);
    if (quality) url += '&quality=' + encodeURIComponent(quality);
    fetch(url)
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (!data.job_id) { alert('Failed to start download'); return; }
            rememberJob(data.job_id, title);
            trackJob(data.job_id, title);
        })
        .catch(function(err) { alert('Failed to start download: ' + err); });
}

downloadBtn.addEventListener('click', function(){
    const alreadyLoaded = currentM3u8Url && !isStreamBroken() && loadedQueryKey === currentQueryKey();
    if (alreadyLoaded) {
        startDownload(currentM3u8Url, currentTitle || 'video', getSelectedDownloadQuality());
        return;
    }
    const originalText = downloadBtn.textContent;
    downloadBtn.disabled = true;
    downloadBtn.textContent = 'Loading…';
    resolveAndPlay().then(function(m3u8){
        downloadBtn.disabled = false;
        downloadBtn.textContent = originalText;
        if(!m3u8) return; // resolveAndPlay already alerted on failure
        startDownload(currentM3u8Url, currentTitle || 'video', getSelectedDownloadQuality());
    });
});

// Reconnect to any downloads that were started before this page load (e.g.
// the tab was closed or backgrounded while the server kept working).
getActiveJobs().forEach(function(j){ trackJob(j.job_id, j.title); });
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(PLAYER_HTML)

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000, threaded=True, use_reloader=False)
