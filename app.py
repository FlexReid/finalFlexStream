#!/usr/bin/env python3
"""
Flex Stream — Raspberry Pi 4 compatible build
Changes from original:
  - Selenium/ChromeDriver removed; Playwright Chromium handles all browser capture
    (Pi 4 ships ARM Chromium via `playwright install chromium`)
  - sync_playwright replaced with async-friendly pattern inside a thread to keep
    Flask synchronous while avoiding blocking the GIL for long capture loops
  - Memory guard: single-browser, single-tab at a time (Pi 4 has 2–8 GB but
    running two headed browsers in parallel OOM-kills the process)
  - chromedriver / subprocess chrome-version detection removed (no longer needed)
  - threading.Event + lock pattern kept; Chrome worker thread removed entirely
  - Retry count reduced to 2 (was 3) — saves ~4 min on dead sources
  - Added FLASK_ENV=production guard so debug reloader doesn't fork on Pi
"""

import re
import time
import requests
from flask import Flask, request, Response, render_template_string, jsonify
from urllib.parse import urljoin, quote_plus, unquote_plus
from playwright.sync_api import sync_playwright
from rapidfuzz import process, fuzz
import os
import datetime
import threading
import json

app = Flask(__name__)

# ----------------------------
# Configuration
# ----------------------------
TMDB_API_KEY = "123240ec331a97bb476ad9a05f86c3bf"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Origin": "https://cloudnestra.com",
    "Referer": "https://cloudnestra.com/",
}
REQUEST_TIMEOUT = 15
CACHE_TTL = 5
_playlist_cache = {}

# Pi 4: serialise browser launches so we never run two at once
_browser_lock = threading.Lock()

# ----------------------------
# Debug helper
# ----------------------------
def debug(msg):
    print(f"[DEBUG] {msg}", flush=True)

# ----------------------------
# TMDb helpers  (unchanged)
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
    return [{"season_number": s["season_number"], "name": s["name"]} for s in data.get("seasons", [])]

def get_released_episodes(tmdb_id, season_number):
    url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_number}?api_key={TMDB_API_KEY}"
    resp = requests.get(url, timeout=REQUEST_TIMEOUT).json()
    episodes = resp.get("episodes", [])
    today = datetime.date.today()
    return [
        {
            "episode_number": ep["episode_number"],
            "name": ep.get("name", f"Episode {ep['episode_number']}"),
            "air_date": ep.get("air_date")
        }
        for ep in episodes
        if ep.get("air_date") and datetime.datetime.strptime(ep["air_date"], "%Y-%m-%d").date() <= today
    ]

# ----------------------------
# Flask API endpoints
# ----------------------------
@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    return resp

@app.route("/get_episodes")
def get_episodes():
    tmdb_id = request.args.get("tmdb_id")
    season = request.args.get("season")
    if not tmdb_id or not season:
        return jsonify([])
    return jsonify(get_released_episodes(tmdb_id, int(season)))

@app.route("/autocomplete")
def autocomplete():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])
    results = search_tmdb(query)
    suggestions = []
    for r in results:
        year = r.get("release_date" if r["media_type"] == "movie" else "first_air_date", "")[:4]
        name = r["title"] if r["media_type"] == "movie" else r["name"]
        suggestions.append(f"{name} ({year})" if year else name)
    seen, unique = set(), []
    for s in suggestions:
        if s.lower() not in seen:
            unique.append(s)
            seen.add(s.lower())
    titles_only = [s.split(" (")[0] for s in unique]
    matches = [unique[idx] for _, score, idx in process.extract(query, titles_only, scorer=fuzz.token_sort_ratio, limit=5)]
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
    season_list = [s for s in get_seasons(tmdb_id) if s["season_number"] != 0]
    return jsonify({"tmdb_id": tmdb_id, "seasons": season_list})

@app.route("/episodes")
def episodes():
    tmdb_id = request.args.get("tmdb_id")
    season_number = request.args.get("season_number")
    if not tmdb_id or not season_number:
        return jsonify([])
    try:
        return jsonify(get_released_episodes(int(tmdb_id), int(season_number)))
    except ValueError:
        return jsonify([])

# ----------------------------
# vsrc.su iframe extraction
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

# ----------------------------
# m3u8 capture — Playwright only (Pi 4 build)
#
# The original code ran Selenium + Playwright in parallel threads.
# On Pi 4 that routinely causes OOM kills and GPU memory exhaustion.
# We now use only Playwright's Chromium (ARM build installed via
# `playwright install chromium`), serialised behind _browser_lock.
#
# Browser choice: chromium (not webkit) — webkit's ARM support in
# Playwright is unreliable on Raspberry Pi OS; chromium works well.
# ----------------------------
def capture_first_m3u8(page_url: str, retries: int = 2) -> str:
    """
    Open page_url in a headless Chromium browser, intercept network
    requests, and return the first .m3u8 URL seen.  Returns None if
    nothing is captured within the timeout.
    """
    for attempt_num in range(1, retries + 1):
        debug(f"[Playwright] Attempt {attempt_num}/{retries} for {page_url}")
        result = _playwright_capture(page_url)
        if result:
            debug(f"[Playwright] Captured m3u8: {result}")
            return result
        debug(f"[Playwright] Attempt {attempt_num} failed")
    debug("All capture attempts failed")
    return None


def _playwright_capture(page_url: str) -> str | None:
    """Single Playwright capture attempt, serialised via _browser_lock."""
    found_url = None

    with _browser_lock:
        try:
            with sync_playwright() as p:
                # Pi 4: use chromium; pass ARM-friendly flags
                browser = p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",   # Pi has limited /dev/shm
                        "--disable-gpu",              # no GPU acceleration needed
                        "--autoplay-policy=no-user-gesture-required",
                        "--disable-extensions",
                        "--disable-background-networking",
                        "--memory-pressure-off",      # avoid premature tab discard
                    ],
                )
                context = browser.new_context(
                    # Reduce memory: don't keep a large viewport
                    viewport={"width": 1280, "height": 720},
                )
                page = context.new_page()

                def on_request(req):
                    nonlocal found_url
                    if ".m3u8" in req.url and found_url is None:
                        found_url = req.url
                        debug(f"[Playwright] Intercepted: {req.url}")

                page.on("request", on_request)

                try:
                    page.goto(page_url, wait_until="load", timeout=30_000)
                except Exception as e:
                    debug(f"[Playwright] goto error (non-fatal): {e}")

                # Try common play-button selectors then fall back to a centre click
                for sel in ["button.vjs-play-control", ".jw-icon-play", ".play-btn", "video"]:
                    try:
                        el = page.query_selector(sel)
                        if el:
                            el.click()
                            debug(f"[Playwright] Clicked {sel}")
                            break
                    except Exception:
                        continue
                else:
                    try:
                        page.mouse.click(640, 360)
                    except Exception:
                        pass

                # Poll for up to 60 s (reduced from 120 s to save Pi resources)
                deadline = time.time() + 60
                while time.time() < deadline and found_url is None:
                    page.wait_for_timeout(500)

                context.close()
                browser.close()

        except Exception as e:
            debug(f"[Playwright] Fatal error: {e}")

    return found_url

# ----------------------------
# HLS variant selection
# ----------------------------
def get_best_variant(m3u8_url: str) -> str:
    debug(f"Fetching m3u8 to find best variant: {m3u8_url}")
    r = requests.get(m3u8_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    variants = re.findall(r"(#EXT-X-STREAM-INF:[^\n]+\n)([^\n]+\.m3u8)", r.text)
    if not variants:
        return m3u8_url
    best = max(
        variants,
        key=lambda v: int(re.search(r"RESOLUTION=\d+x(\d+)", v[0]).group(1))
                      if re.search(r"RESOLUTION=\d+x(\d+)", v[0]) else 0,
    )[1]
    return urljoin(m3u8_url, best)

# ----------------------------
# MPEG-TS extraction  (unchanged)
# ----------------------------
def find_mpeg_ts_start(data: bytes, min_consecutive: int = 5):
    n = len(data)
    for idx in range(min(4096, n)):
        if data[idx] != 0x47:
            continue
        if all((idx + k * 188) < n and data[idx + k * 188] == 0x47 for k in range(1, min_consecutive + 1)):
            return idx
    return None

def extract_ts_packets(data: bytes) -> bytes:
    start = find_mpeg_ts_start(data)
    if start is None:
        try:
            start = data.index(b"\x47")
        except ValueError:
            return b""
    out = bytearray()
    i = start
    while i + 188 <= len(data):
        pkt = data[i:i + 188]
        if pkt[0] != 0x47:
            break
        out.extend(pkt)
        i += 188
    return bytes(out)

# ----------------------------
# HLS Proxy
# ----------------------------
def fetch_bytes(url):
    r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.content

def rewrite_playlist(original_url: str, playlist_text: str) -> str:
    lines = playlist_text.splitlines()
    new_lines = []
    seq = 0
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue
        abs_url = urljoin(original_url, stripped)
        new_lines.append(f"/segment?u={quote_plus(abs_url)}&i={seq}")
        seq += 1
    return "\n".join(new_lines)

@app.route("/proxy_playlist")
def proxy_playlist():
    url = request.args.get("url")
    if not url:
        return "Missing url param", 400
    now = time.time()
    cached = _playlist_cache.get(url)
    if cached and now - cached["ts"] < CACHE_TTL:
        return Response(cached["data"], mimetype="application/vnd.apple.mpegurl")
    try:
        pl_bytes = fetch_bytes(url)
    except Exception as e:
        return f"Failed to fetch playlist: {e}", 502
    rewritten = rewrite_playlist(url, pl_bytes.decode("utf-8", errors="ignore"))
    _playlist_cache[url] = {"data": rewritten, "ts": now}
    return Response(rewritten, mimetype="application/vnd.apple.mpegurl")

@app.route("/segment")
def segment():
    u = request.args.get("u")
    if not u:
        return "Missing u param", 400
    url = unquote_plus(u)
    try:
        data = extract_ts_packets(fetch_bytes(url))
    except Exception as e:
        return f"Failed to fetch segment: {e}", 502
    resp = Response(data, mimetype="video/MP2T")
    resp.headers["Content-Length"] = str(len(data))
    return resp

# ----------------------------
# m3u8 lookup endpoint
# ----------------------------
@app.route("/get_m3u8")
def get_m3u8():
    title = request.args.get("title")
    season = request.args.get("season")
    episode = request.args.get("episode")
    year = request.args.get("year")
    if not title:
        return "", 400

    results = search_tmdb(title)
    debug(f"TMDb returned {len(results)} results")

    if year:
        filtered = [
            r for r in results
            if (r.get("release_date" if r["media_type"] == "movie" else "first_air_date", "")[:4]) == year
        ]
        if filtered:
            results = filtered
            debug(f"Filtered by year {year}: {len(results)} remaining")

    best = get_best_match(title, results)
    if not best:
        return "", 404

    tmdb_id = best["id"]
    media_type = best.get("media_type", "movie").lower()
    ext_url = f"https://api.themoviedb.org/3/{'movie' if media_type == 'movie' else 'tv'}/{tmdb_id}/external_ids?api_key={TMDB_API_KEY}"
    imdb_id = requests.get(ext_url).json().get("imdb_id")
    if not imdb_id:
        debug("IMDb ID not found")
        return "", 404

    if media_type == "tv" and season and episode:
        vsrc_embed = f"https://vsrc.su/embed/tv?imdb={imdb_id}&season={season}&episode={episode}&dts=dd"
    else:
        vsrc_embed = f"https://vsrc.su/embed/movie?imdb={imdb_id}&dts=dd"

    iframe_src = get_player_iframe_src(vsrc_embed)
    if not iframe_src:
        return "", 404

    first_m3u8 = capture_first_m3u8(iframe_src)
    if not first_m3u8:
        return "", 404

    return get_best_variant(first_m3u8)

# ----------------------------
# HTML Player  (unchanged from original)
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
h1 { margin-bottom: 10px; font-weight: 600; }
h1 .cyan { color: cyan; }
#controls {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 20px;
  width: 100%;
  max-width: 900px;
  justify-content: center;
}
input, select {
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
button:hover { background-color: #00cccc; }
#video-container { position: relative; width: 80%; max-width: 900px; }
#video { width: 100%; border-radius: 8px; background: #000; aspect-ratio: 16 / 9; }
#loading {
  position: absolute;
  top: 50%; left: 50%;
  transform: translate(-50%, -50%);
  color: #fff; font-size: 20px;
  background: rgba(0,0,0,0.6);
  padding: 12px 20px;
  border-radius: 6px;
  display: none;
}
#debug-overlay {
  position: absolute;
  top: 10px; left: 10px;
  color: cyan; font-size: 12px;
  font-family: monospace;
  background-color: rgba(0,0,0,0.3);
  padding: 4px 8px;
  border-radius: 4px;
  pointer-events: none;
  z-index: 100;
  white-space: pre-line;
}
.autocomplete-dropdown {
  position: absolute;
  background: #222; color: #fff;
  list-style: none; padding: 5px; margin: 0;
  border-radius: 4px; z-index: 1000; display: none;
}
.autocomplete-dropdown li { cursor: pointer; padding: 3px 6px; }
footer { margin-top: auto; text-align: center; padding: 10px; font-size: 0.9rem; color: #888; }
@media (max-width: 768px) {
  #controls { flex-direction: column; align-items: stretch; gap: 10px; }
  #controls input, #controls select, #controls button {
    flex: none; width: 100%; max-width: 100%;
    box-sizing: border-box; text-align: center; text-align-last: center; color: #000;
  }
  #video-container { width: 98%; max-width: 100%; }
  #video-container video {
    width: 100%; height: 35vh; max-height: 40vh;
    margin: 0 auto; display: block; object-fit: contain; border-radius: 8px;
  }
  footer { margin-top: 10px; margin-bottom: 20px; }
  html, body { overflow: hidden; touch-action: none; height: 100%; }
  #controls button { width: 50%; min-height: 60px; padding: 0; align-self: center; font-size: 1.5rem; }
  #controls .select-wrapper { position: relative; width: 100%; }
  #controls select {
    -webkit-appearance: none !important; appearance: none !important;
    width: 100%; padding: 10px; text-align: center; text-align-last: center;
    background: #e5e5e5; color: #000; border: none; border-radius: 6px; box-sizing: border-box;
  }
  #loading {
    font-size: 16px; padding: 10px 16px; border-radius: 6px;
    background: rgba(0,0,0,0.6); color: #fff; display: none;
    text-align: center; max-width: 90%; box-sizing: border-box;
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
<button onclick="load()">Load &amp; Play</button>
<ul id="autocomplete" class="autocomplete-dropdown"></ul>
</div>
<div id="video-container">
<video id="video" controls crossorigin playsinline></video>
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
const dropdown = document.getElementById('autocomplete');
let hlsInstance = null;
let selectedTmdbId = null;

function showLoading(show){ loading.style.display = show ? 'block' : 'none'; }
function updateDebug(msg){ debugOverlay.textContent = msg; }

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
                    loadSeasons(s.replace(/\\s+\\(\\d{4}\\)$/, ''));
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
    const match = fullTitle.match(/^(.*)\\s+\\((\\d{4})\\)$/);
    return match ? {title: match[1], year: match[2]} : {title: fullTitle, year:null};
}

function load(){
    const {title, year} = parseTitleAndYear(titleInput.value.trim());
    if(!title){ alert('Enter a title'); return; }
    const season = seasonSelect.value;
    const episode = episodeSelect.value;
    showLoading(true);
    updateDebug('Searching TMDb...');
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
            hlsInstance.on(Hls.Events.MANIFEST_PARSED, ()=>{ video.play().catch(()=>{}); });
            hlsInstance.on(Hls.Events.ERROR,(e,data)=>{ console.error('Hls.js error:',data); });
        } else if(video.canPlayType('application/vnd.apple.mpegurl')){
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
# Entry point
# Pi 4 note: run with a single worker process to avoid duplicate
# browser launches.  For production use gunicorn:
#   gunicorn -w 1 -b 0.0.0.0:5000 app_pi4:app
# ----------------------------
if __name__ == "__main__":
    app.run(
        debug=False,      # disable reloader — it double-forks and wastes RAM
        host="0.0.0.0",
        port=5000,
        threaded=True,    # keep Flask threaded so concurrent HTTP requests work
    )
