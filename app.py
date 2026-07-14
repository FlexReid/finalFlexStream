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


app = Flask(__name__)

# ----------------------------
# Configuration
# ----------------------------
TMDB_API_KEY = "123240ec331a97bb476ad9a05f86c3bf"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Origin": "https://cloudorchestranova.com",
    "Referer": "https://cloudorchestranova.com/",
}
REQUEST_TIMEOUT = 15
CACHE_TTL = 5
_playlist_cache = {}

# ----------------------------
# Debug helper
# ----------------------------
def debug(msg):
    print(f"[DEBUG] {msg}")

# ----------------------------
# Pi4-compatible Chrome version detection
# Replaces the Windows registry approach entirely.
# Tries the common binary names used on Raspberry Pi OS / Debian.
# ----------------------------
def get_chrome_major_version():
    candidates = [
        "chromium-browser",   # Raspberry Pi OS default name
        "chromium",           # some distros
        "google-chrome",      # if manually installed
        "google-chrome-stable",
    ]
    for binary in candidates:
        try:
            result = subprocess.run(
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            # Output is like: "Chromium 124.0.6367.82" or "Google Chrome 124.0.6367.78"
            version_str = result.stdout.strip()
            match = re.search(r"(\d+)\.\d+\.\d+", version_str)
            if match:
                major = int(match.group(1))
                debug(f"Detected Chrome/Chromium version {major} via '{binary}'")
                return major
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
            continue

    # Last resort: check if chromedriver itself reports a version
    for binary in ["chromedriver", "chromedriver-browser"]:
        try:
            result = subprocess.run(
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            match = re.search(r"(\d+)\.\d+\.\d+", result.stdout.strip())
            if match:
                major = int(match.group(1))
                debug(f"Detected version {major} via '{binary} --version'")
                return major
        except Exception:
            continue

    debug("Could not detect Chrome/Chromium version; passing None to uc.Chrome")
    return None


# ----------------------------
# Resolve the correct Chromium binary path for Pi4.
# undetected_chromedriver defaults to 'google-chrome' which doesn't exist on Pi.
# ----------------------------
def get_chromium_binary():
    candidates = [
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ]
    for path in candidates:
        if os.path.isfile(path):
            debug(f"Using Chromium binary: {path}")
            return path
    debug("No Chromium binary found in standard paths; letting uc find it automatically")
    return None


# ----------------------------
# TMDb helpers
# ----------------------------
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
    r = requests.get(
        f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={TMDB_API_KEY}",
        timeout=REQUEST_TIMEOUT
    )
    data = r.json()
    seasons = [{"season_number": s["season_number"], "name": s["name"]} for s in data.get("seasons", [])]
    return seasons

def get_released_episodes(tmdb_id, season_number):
    url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_number}?api_key={TMDB_API_KEY}"
    resp = requests.get(url, timeout=REQUEST_TIMEOUT).json()
    episodes = resp.get("episodes", [])
    today = datetime.date.today()
    released = [
        {
            "episode_number": ep["episode_number"],
            "name": ep.get("name", f"Episode {ep['episode_number']}"),
            "air_date": ep.get("air_date")
        }
        for ep in episodes
        if ep.get("air_date") and datetime.datetime.strptime(ep["air_date"], "%Y-%m-%d").date() <= today
    ]
    return released

def is_released(item):
    today = datetime.date.today()
    if item['type'] == 'movie':
        release_date = datetime.datetime.strptime(item['release_date'], "%Y-%m-%d").date()
        return release_date <= today
    elif item['type'] == 'tv':
        seasons = get_seasons(item['tmdb_id'])
        for season in seasons:
            released_eps = get_released_episodes(item['tmdb_id'], season['season_number'])
            if released_eps:
                return True
        return False


@app.route("/get_episodes")
def get_episodes():
    tmdb_id = request.args.get("tmdb_id")
    season = request.args.get("season")
    if not tmdb_id or not season:
        return jsonify([])
    released_episodes = get_released_episodes(tmdb_id, int(season))
    return jsonify(released_episodes)


# ----------------------------
# vsrc.su iframe & m3u8 capture
# ----------------------------
def get_player_iframe_src(vsrc_url: str) -> str:
    debug(f"Fetching vsrc page: {vsrc_url}")
    r = requests.get(vsrc_url, headers=HEADERS, timeout=10)
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
    return src


def capture_first_m3u8(page_url: str, retries=3) -> str:
    """
    Dual-engine m3u8 capture:
      - Real Chromium via undetected_chromedriver (background thread) — Pi4 compatible
      - Playwright WebKit (main thread)
    Whichever captures the .m3u8 URL first wins.
    Retries up to `retries` times if both fail.
    """

    def attempt() -> str:
        result = {"url": None}
        lock = threading.Lock()
        done = threading.Event()
        chrome_failed = threading.Event()

        # ------------------------------------------------------------------
        # Chrome worker — runs in a background thread
        # Key Pi4 changes:
        #   • detect binary via get_chromium_binary()
        #   • pass binary_location to ChromeOptions
        #   • Pi4 needs --disable-setuid-sandbox (no kernel namespace support)
        #   • removed Windows-specific registry call
        # ------------------------------------------------------------------
        def chrome_worker():
            chrome_version = get_chrome_major_version()
            chromium_binary = get_chromium_binary()

            options = uc.ChromeOptions()

            # Set the binary explicitly so uc doesn't look for 'google-chrome'
            if chromium_binary:
                options.binary_location = chromium_binary

            # Core flags — required on Pi4 / low-memory ARM systems
            options.add_argument("--no-sandbox")
            options.add_argument("--window-size=1280,720")
            options.add_argument("--remote-debugging-port=0")
            options.add_argument("--disable-setuid-sandbox")   # Pi4: no SUID sandbox
            options.add_argument("--disable-dev-shm-usage")    # Pi4: /dev/shm is tiny
            options.add_argument("--disable-gpu")
            options.add_argument("--autoplay-policy=no-user-gesture-required")

            # Pi4 memory / performance tuning
            options.add_argument("--disable-software-rasterizer")
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_argument("--disable-extensions")
            options.add_argument("--disable-background-networking")
            options.add_argument("--disable-default-apps")
            options.add_argument("--mute-audio")

            # Required for capturing network logs
            options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
            options.page_load_strategy = "eager"

            # On Pi4, uc tries to patch/copy the chromedriver binary.
            # /usr/bin/chromedriver is root-owned so uc can't copy it — use a
            # writable copy in the user's home directory instead.
            import shutil as _shutil
            home_chromedriver = os.path.expanduser("~/chromedriver")
            system_chromedriver = None

            # Auto-copy from system if the home copy doesn't exist yet
            if not os.path.isfile(home_chromedriver):
                for src in [
                    "/usr/bin/chromedriver",
                    "/usr/lib/chromium-browser/chromedriver",
                    "/usr/lib/chromium/chromedriver",
                ]:
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
                # Last resort: try system paths directly
                for candidate in [
                    "/usr/bin/chromedriver",
                    "/usr/lib/chromium-browser/chromedriver",
                    "/usr/lib/chromium/chromedriver",
                ]:
                    if os.path.isfile(candidate):
                        system_chromedriver = candidate
                        debug(f"[Chrome] Using system chromedriver: {candidate}")
                        break

            kwargs = {"options": options, "use_subprocess": True}
            if chrome_version:
                kwargs["version_main"] = chrome_version
            if system_chromedriver:
                kwargs["driver_executable_path"] = system_chromedriver

            # Make sure Chrome can find the display
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

                # Try common player selectors, fall back to centre-click
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
                    time.sleep(0.3)   # slightly longer sleep on Pi to reduce CPU pressure

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

        # ------------------------------------------------------------------
        # Playwright WebKit — runs on the main thread
        # WebKit is well-supported on ARM Linux via Playwright's bundled build.
        # ------------------------------------------------------------------
        chrome_thread = threading.Thread(target=chrome_worker, daemon=True)
        chrome_thread.start()
        debug("[Playwright] Starting WebKit...")

        try:
            with sync_playwright() as p:
                # headless=True is fine on Pi (no display required for WebKit)
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

                # Poll for up to 30 s in 500 ms increments
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

    # ------------------------------------------------------------------
    # Retry loop
    # ------------------------------------------------------------------
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
    r = requests.get(m3u8_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    variants = re.findall(r'(#EXT-X-STREAM-INF:[^\n]+\n)([^\n]+\.m3u8)', r.text)
    if not variants:
        return m3u8_url
    best_variant = max(
        variants,
        key=lambda v: int(re.search(r"RESOLUTION=(\d+)x(\d+)", v[0]).group(2)
                          if re.search(r"RESOLUTION=(\d+)x(\d+)", v[0]) else 0)
    )[1]
    final_url = urljoin(m3u8_url, best_variant)

    # The variant path from the master playlist usually doesn't carry the
    # ?token=... query param, since urljoin() only keeps query params that
    # are explicitly part of the relative path. Re-attach the token from the
    # master m3u8 request so the variant URL stays authenticated.
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


# ----------------------------
# MPEG-TS extraction
# ----------------------------
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


# ----------------------------
# HLS Proxy helpers
# ----------------------------
def fetch_bytes(url):
    r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.content

def rewrite_playlist(original_playlist_url: str, playlist_text: str):
    lines = playlist_text.splitlines()
    new_lines = []
    seq = 0

    # Variant/segment URLs pulled from a master playlist often don't carry
    # the ?token=... query param (urljoin only keeps params that are
    # explicitly part of the relative path). Re-attach it from the parent
    # playlist URL so every proxied request stays authenticated.
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
            # This line points to another playlist (a quality variant in a
            # master playlist) rather than a media segment. Proxy it
            # recursively so hls.js can discover it as a selectable level.
            proxied = f"/proxy_playlist?url={quote_plus(abs_url)}"
        else:
            proxied = f"/segment?u={quote_plus(abs_url)}&i={seq}"
            seq += 1
        new_lines.append(proxied)
    return "\n".join(new_lines)


# ----------------------------
# Flask endpoints
# ----------------------------
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
        pl_bytes = fetch_bytes(url)
    except Exception as e:
        return f"Failed to fetch playlist: {e}", 502

    pl_text = pl_bytes.decode('utf-8', errors='ignore')
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
        remote_bytes = fetch_bytes(url)
        remote_bytes = extract_ts_packets(remote_bytes)
    except Exception as e:
        return f"Failed to fetch remote segment: {e}", 502

    resp = Response(remote_bytes, mimetype="video/MP2T")
    resp.headers['Content-Length'] = str(len(remote_bytes))
    return resp


# ----------------------------
# Autocomplete endpoint
# ----------------------------
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
    seasons = get_seasons(tmdb_id)
    seasons = [s for s in seasons if s["season_number"] != 0]
    return jsonify({"tmdb_id": tmdb_id, "seasons": seasons})

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
    released_episodes = get_released_episodes(tmdb_id, season_number)
    return jsonify(released_episodes)


# ----------------------------
# Endpoint to get m3u8 URL via TMDb lookup
# ----------------------------
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

    # Return the raw playlist URL as-is (rather than pre-resolving to the
    # single best-quality variant). If this is a master playlist, /proxy_playlist
    # will expose every quality variant it references so hls.js can build a
    # quality selector and switch between them client-side.
    return first_m3u8


# ----------------------------
# HTML Player
# ----------------------------
PLAYER_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Flex Stream</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
<style>
body {
  margin: 0;
  font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
  background: #121212;
  color: #fff;
  display: flex;
  flex-direction: column;
  align-items: center;
  min-height: 100vh;
  padding: 20px;
}

h1 {
  margin-bottom: 10px;
  font-weight: 600;
}

h1 .cyan {
  color: cyan;
}

#controls {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 20px;
  width: 100%;
  max-width: 900px;
  justify-content: center;
}

input,
select {
  padding: 10px;
  border-radius: 6px;
  border: none;
  flex: 1 1 150px;
  max-width: 250px;
  font-size: 1rem;
}

button {
  padding: 10px 20px;
  background-color: cyan;
  color: #000;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-weight: 600;
  transition: 0.2s;
}

button:hover {
  background-color: #00cccc;
}

#video-container {
  position: relative;
  width: 80%;
  max-width: 900px;
}

#video {
  width: 100%;
  border-radius: 8px;
  background: #000;
  aspect-ratio: 16 / 9;
}

#loading {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  color: #fff;
  font-size: 20px;
  background: rgba(0, 0, 0, 0.6);
  padding: 12px 20px;
  border-radius: 6px;
  display: none;
}

#debug-overlay {
  position: absolute;
  top: 10px;
  left: 10px;
  color: cyan;
  font-size: 12px;
  font-family: monospace;
  background-color: rgba(0, 0, 0, 0.3);
  padding: 4px 8px;
  border-radius: 4px;
  pointer-events: none;
  z-index: 100;
  white-space: pre-line;
}

#quality {
  position: absolute;
  top: 10px;
  right: 10px;
  z-index: 100;
  background-color: rgba(0, 0, 0, 0.65);
  color: #fff;
  border: 1px solid rgba(255, 255, 255, 0.3);
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 0.85rem;
  max-width: 110px;
  cursor: pointer;
}

#quality:hover {
  background-color: rgba(0, 0, 0, 0.85);
}

.autocomplete-dropdown {
  position: absolute;
  background: #222;
  color: #fff;
  list-style: none;
  padding: 5px;
  margin: 0;
  border-radius: 4px;
  z-index: 1000;
  display: none;
}

.autocomplete-dropdown li {
  cursor: pointer;
  padding: 3px 6px;
}

footer {
  margin-top: auto;
  text-align: center;
  padding: 10px;
  font-size: 0.9rem;
  color: #888;
}

@media (max-width: 768px) {
  #controls {
      flex-direction: column;
      align-items: stretch;
      gap: 10px;
  }
  #controls input,
  #controls select,
  #controls button {
      flex: none;
      width: 100%;
      max-width: 100%;
      box-sizing: border-box;
      text-align: center;
      text-align-last: center;
      color: #000;
  }
  #video-container {
      width: 98%;
      max-width: 100%;
  }
  #video-container video {
      width: 100%;
      height: 35vh;
      max-height: 40vh;
      margin: 0 auto;
      display: block;
      object-fit: contain;
      border-radius: 8px;
  }
  footer {
      margin-top: 10px;
      margin-bottom: 20px;
  }
  html, body {
      overflow: hidden;
      touch-action: none;
      height: 100%;
  }
  #controls button {
      width: 50%;
      min-height: 60px;
      padding: 0;
      align-self: center;
      font-size: 1.5rem;
  }
  #controls .select-wrapper {
      position: relative;
      width: 100%;
  }
  #controls select {
      -webkit-appearance: none !important;
      appearance: none !important;
      width: 100%;
      padding: 10px;
      text-align: center;
      text-align-last: center;
      background: #e5e5e5;
      color: #000;
      border: none;
      border-radius: 6px;
      box-sizing: border-box;
  }
  #loading {
      font-size: 16px;
      padding: 10px 16px;
      border-radius: 6px;
      background: rgba(0, 0, 0, 0.6);
      color: #fff;
      display: none;
      text-align: center;
      max-width: 90%;
      box-sizing: border-box;
  }
}
</style>
</head>
<body>
<h1><span class="cyan">Flex</span> Stream</h1>
<div id="controls">
<input id="title" type="text" placeholder="Title"/>
<select id="season"><option value="">Season</option></select>
<select id="episode"><option value="">Episode</option></select>
<button onclick="load()">Load & Play</button>
<ul id="autocomplete" class="autocomplete-dropdown"></ul>
</div>
<div id="video-container">
<video id="video" controls crossorigin playsinline></video>
<select id="quality" style="display:none;"><option value="-1">Auto</option></select>
<div id="loading">Loading video, please wait...</div>
<div id="debug-overlay"></div>
</div>
<footer>Powered by <span class="cyan">Flex</span> Stream</footer>
<script>
const video = document.getElementById('video');
const loading = document.getElementById('loading');
const debugOverlay = document.getElementById('debug-overlay');
const titleInput = document.getElementById('title');
const seasonSelect = document.getElementById('season');
const episodeSelect = document.getElementById('episode');
const qualitySelect = document.getElementById('quality');
const dropdown = document.getElementById('autocomplete');
let hlsInstance = null;
let selectedTmdbId = null;

function showLoading(show){ loading.style.display = show ? 'block' : 'none'; }
function updateDebug(msg){ debugOverlay.textContent = msg; }

function levelLabel(level){
    if(level.height) return `${level.height}p`;
    if(level.bitrate) return `${Math.round(level.bitrate/1000)} kbps`;
    return 'Unknown';
}

function populateQualityLevels(levels){
    qualitySelect.innerHTML = '<option value="-1">Auto</option>';
    if(!levels || levels.length < 2){
        qualitySelect.style.display = 'none';
        return;
    }
    // Highest quality first
    levels
        .map((lvl, idx) => ({idx, lvl}))
        .sort((a, b) => (b.lvl.height||0) - (a.lvl.height||0) || (b.lvl.bitrate||0) - (a.lvl.bitrate||0))
        .forEach(({idx, lvl}) => {
            const opt = document.createElement('option');
            opt.value = idx;
            opt.textContent = levelLabel(lvl);
            qualitySelect.appendChild(opt);
        });
    qualitySelect.value = '-1';
    qualitySelect.style.display = 'inline-block';
}

qualitySelect.addEventListener('change', () => {
    if(!hlsInstance) return;
    hlsInstance.currentLevel = parseInt(qualitySelect.value, 10); // -1 = auto
});

titleInput.addEventListener('input', () => {
    const query = titleInput.value.trim();
    selectedTmdbId = null;
    seasonSelect.innerHTML = '<option value="">Season</option>';
    episodeSelect.innerHTML = '<option value="">Episode</option>';
    dropdown.style.display = 'none';
    if(!query) return;

    fetch(`/autocomplete?q=${encodeURIComponent(query)}`)
        .then(r=>r.json())
        .then(suggestions=>{
            if(!Array.isArray(suggestions) || suggestions.length===0) return;
            dropdown.innerHTML='';
            suggestions.forEach(s=>{
                const li=document.createElement('li');
                li.textContent = s;
                li.addEventListener('click', ()=>{
                    titleInput.value = s;
                    dropdown.style.display='none';
                    loadSeasons(s.replace(/\s+\(\d{4}\)$/, ''));
                });
                dropdown.appendChild(li);
            });
            const rect = titleInput.getBoundingClientRect();
            dropdown.style.top = rect.bottom + window.scrollY + 'px';
            dropdown.style.left = rect.left + window.scrollX + 'px';
            dropdown.style.width = rect.width + 'px';
            dropdown.style.display='block';
        });
});

document.addEventListener('click', (e)=>{
    if(!titleInput.contains(e.target) && !dropdown.contains(e.target)){
        dropdown.style.display='none';
    }
});

function loadSeasons(title){
    fetch(`/seasons?title=${encodeURIComponent(title)}`)
    .then(r=>r.json())
    .then(data=>{
        selectedTmdbId = data.tmdb_id;
        seasonSelect.innerHTML = '<option value="">Season</option>';
        data.seasons.forEach(s=>{
            const opt=document.createElement('option');
            opt.value = s.season_number;
            opt.textContent = s.name;
            seasonSelect.appendChild(opt);
        });
        episodeSelect.innerHTML='<option value="">Episode</option>';
    });
}

seasonSelect.addEventListener('change', ()=>{
    const seasonNumber = seasonSelect.value;
    if(!seasonNumber || !selectedTmdbId) return;
    fetch(`/episodes?tmdb_id=${selectedTmdbId}&season_number=${seasonNumber}`)
        .then(r=>r.json())
        .then(eps=>{
            episodeSelect.innerHTML = '<option value="">Episode</option>';
            const today = new Date();
            eps.forEach(ep=>{
                if(!ep.air_date || new Date(ep.air_date) > today) return;
                const opt=document.createElement('option');
                opt.value=ep.episode_number;
                opt.textContent=`${ep.episode_number}: ${ep.name}`;
                episodeSelect.appendChild(opt);
            });
            if(episodeSelect.options.length===1){
                const opt=document.createElement('option');
                opt.textContent='No released episodes';
                opt.disabled=true;
                episodeSelect.appendChild(opt);
            }
        });
});

function parseTitleAndYear(fullTitle){
    const match = fullTitle.match(/^(.*)\s+\((\d{4})\)$/);
    return match ? {title: match[1], year: match[2]} : {title: fullTitle, year:null};
}

function load(){
    const {title, year} = parseTitleAndYear(titleInput.value.trim());
    if(!title){ alert('Enter a title'); return; }
    const season = seasonSelect.value;
    const episode = episodeSelect.value;

    showLoading(true);
    updateDebug('Searching TMDb...');
    qualitySelect.style.display = 'none';
    let url = `/get_m3u8?title=${encodeURIComponent(title)}`;
    if(year) url += `&year=${year}`;
    if(season) url += `&season=${season}`;
    if(episode) url += `&episode=${episode}`;

    fetch(url).then(r=>r.text()).then(url=>{
        showLoading(false);
        if(!url){ updateDebug('No video found'); alert('No video found'); return; }
        updateDebug('Video URL captured. Loading HLS...');
        const proxied = '/proxy_playlist?url='+encodeURIComponent(url);

        if(Hls.isSupported()){
            if(hlsInstance) hlsInstance.destroy();
            hlsInstance = new Hls();
            hlsInstance.loadSource(proxied);
            hlsInstance.attachMedia(video);
            hlsInstance.on(Hls.Events.MANIFEST_PARSED, (event, data)=>{
                populateQualityLevels(data.levels);
                video.play().catch(()=>{});
            });
            hlsInstance.on(Hls.Events.LEVEL_SWITCHED, (event, data)=>{
                const lvl = hlsInstance.levels[data.level];
                if(lvl) updateDebug(`Playing: ${levelLabel(lvl)}${qualitySelect.value === '-1' ? ' (auto)' : ''}`);
            });
            hlsInstance.on(Hls.Events.ERROR,(e,data)=>{ console.error('Hls.js error:',data); });
        } else if(video.canPlayType('application/vnd.apple.mpegurl')){
            // Native HLS (Safari) handles adaptive quality internally;
            // manual level selection via JS isn't available here.
            qualitySelect.style.display = 'none';
            video.src = proxied;
            video.addEventListener('loadedmetadata', ()=>{ video.play().catch(()=>{}); });
        } else{
            updateDebug('HLS not supported in this browser');
            alert('HLS not supported in this browser');
        }
    }).catch(err=>{
        showLoading(false);
        updateDebug('Error: '+err);
        alert('Failed to load video');
    });
}
</script>
</body>
</html>
"""


# ----------------------------
# Main route
# ----------------------------
@app.route("/")
def index():
    return render_template_string(PLAYER_HTML)


# ----------------------------
# Run
# ----------------------------
if __name__ == "__main__":
    # threaded=True is important — each /get_m3u8 request spins up Chrome + Playwright
    # use_reloader=False prevents Flask from forking the process, which confuses uc
    app.run(debug=True, host="0.0.0.0", port=5000, threaded=True, use_reloader=False)
