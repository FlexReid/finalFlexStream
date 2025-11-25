#!/usr/bin/env python3
import re
import time
import requests
from flask import Flask, request, Response, render_template_string, jsonify
from urllib.parse import urljoin, quote_plus, unquote_plus
from playwright.sync_api import sync_playwright
from rapidfuzz import process, fuzz
import os
import datetime

app = Flask(__name__)

# ----------------------------
# Configuration
# ----------------------------
TMDB_API_KEY = "123240ec331a97bb476ad9a05f86c3bf"  # Replace with your TMDb API key
HEADERS = {"User-Agent": "Mozilla/5.0"}
REQUEST_TIMEOUT = 15
CACHE_TTL = 5
_playlist_cache = {}

# ----------------------------
# Debug helper
# ----------------------------
def debug(msg):
    print(f"[DEBUG] {msg}")

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
    return results[idx] if score and score > 60 else None  # Adjust threshold if needed

def get_seasons(tmdb_id: int):
    r = requests.get(
        f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={TMDB_API_KEY}",
        timeout=REQUEST_TIMEOUT
    )
    data = r.json()
    seasons = [{"season_number": s["season_number"], "name": s["name"]} for s in data.get("seasons", [])]
    return seasons

def get_released_episodes(tmdb_id, season_number):
    """Return a list of episodes for the season that have aired already."""
    url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_number}?api_key={TMDB_API_KEY}"
    resp = requests.get(url, timeout=REQUEST_TIMEOUT).json()
    episodes = resp.get("episodes", [])
    today = datetime.date.today()
    released = [
        {
            "episode_number": ep["episode_number"],
            "name": ep.get("name", f"Episode {ep['episode_number']}"),
            "air_date": ep.get("air_date")  # include air_date
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
    """AJAX endpoint to fetch released episodes for a given season."""
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
    for attempt in range(1, retries + 1):
        debug(f"Playwright attempt {attempt} for {page_url}")
        with sync_playwright() as p:
            browser = p.webkit.launch()
            context = browser.new_context()
            page = context.new_page()
            captured = {"url": None}

            def on_request(req):
                url = req.url
                if ".m3u8" in url and not captured["url"]:
                    captured["url"] = url
                    debug(f"Captured m3u8 request: {url}")

            page.on("request", on_request)

            try:
                page.goto(page_url, wait_until="load", timeout=30000)
            except Exception as e:
                debug(f"Page load error: {e}")

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

            for _ in range(30):
                if captured["url"]:
                    break
                page.wait_for_timeout(500)

            context.close()
            browser.close()

            if captured["url"]:
                debug(f"Successfully captured m3u8: {captured['url']}")
                return captured["url"]

    debug("All Playwright attempts failed")
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
    for line in lines:
        line_stripped = line.strip()
        if not line_stripped or line_stripped.startswith('#'):
            new_lines.append(line)
            continue
        abs_url = urljoin(original_playlist_url, line_stripped)
        proxied = f"/segment?u={quote_plus(abs_url)}&i={seq}"
        new_lines.append(proxied)
        seq += 1
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
    except Exception as e:
        return f"Failed to fetch remote segment: {e}", 502

    # Always pass raw bytes — this avoids random missing segments
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
    names = [r["title"] if r["media_type"] == "movie" else r["name"] for r in results]
    matches = [title for title, score, idx in process.extract(query, names, scorer=fuzz.token_sort_ratio, limit=5)]
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
    # Filter out specials (season_number 0)
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
@app.route("/titles")
def titles():
    all_items = get_all_titles()  # returns movies and TV shows
    released_items = [item for item in all_items if is_released(item)]
    
    return render_template("titles.html", items=released_items)

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

/* 🔹 Mobile-friendly adjustments */
@media (max-width: 768px) {

  /* Stack controls vertically and make them full width */
  #controls {
      flex-direction: column;
      align-items: stretch;
      gap: 10px;
  }

  #controls input,
  #controls select,
  #controls button {
      flex: none;          /* ignore previous flex-grow/shrink */
      width: 100%;         /* full width for mobile */
      max-width: 100%;     /* prevent shrinking */
      box-sizing: border-box; 
      text-align: center;
      text-align-last: center; /* iOS fix */
      color: #000;         /* fix blue text */
  }

  /* Video adjustments remain */
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

  /* Footer right below video */
  footer {
      margin-top: 10px;
      margin-bottom: 20px;
  }

  /* Disable scrolling */
  html, body {
      overflow: hidden;
      touch-action: none;
      height: 100%;
  }

/* 🔹 Make Load & Play button taller and narrower */
  #controls button {
      width: 50%;      /* half the container width */
      min-height: 60px; /* double previous 42px */
      padding: 0;      /* optional: reduce padding since height increased */
      align-self: center; /* center it horizontally */
      font-size: 1.5rem; /* slightly bigger text for tall button */
  }

/* Wrap the select in a relative container */
  #controls .select-wrapper {
      position: relative;
      width: 100%;
  }

  #controls select {
      -webkit-appearance: none !important; /* removes default arrow on iOS */
      appearance: none !important;         /* removes arrow on other browsers */
      width: 100%;
      padding: 10px;                        /* simple padding all around */
      text-align: center;                    /* center the text */
      text-align-last: center;               /* iOS fix for select text centering */
      background: #e5e5e5;                  /* plain background */
      color: #000;
      border: none;
      border-radius: 6px;
      box-sizing: border-box;
  }

 #video-container {
      position: relative; /* ensure loading popup is positioned relative to video */
      width: 98%;
      max-width: 100%;
  }

  #video-container video {
      width: 100%;
      height: 35vh;    /* short and wide */
      max-height: 40vh;
      margin: 0 auto;
      display: block;
      object-fit: contain;
      border-radius: 8px;
  }

  #loading {
      position: absolute;
      top: 50%;
      left: 50%;
      transform: translate(-50%, -50%);
      font-size: 16px;           /* slightly smaller on mobile */
      padding: 10px 16px;        /* reduce padding for mobile */
      border-radius: 6px;
      background: rgba(0, 0, 0, 0.6);
      color: #fff;
      display: none;
      text-align: center;
      max-width: 90%;            /* ensure it doesn’t overflow video */
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

    // 🔹 Reset season & episode dropdowns whenever the title changes
    selectedTmdbId = null;
    seasonSelect.innerHTML = '<option value="">Season</option>';
    episodeSelect.innerHTML = '<option value="">Episode</option>';

    if (!query) {
        dropdown.style.display = 'none';
        return;
    }

    fetch(`/autocomplete?q=${encodeURIComponent(query)}`)
        .then(r => r.json())
        .then(suggestions => {
            if (!Array.isArray(suggestions)) {
                console.error('Autocomplete API returned non-array:', suggestions);
                dropdown.style.display = 'none';
                return;
            }

            // Remove duplicates (case-insensitive)
            const seen = new Set();
            const uniqueSuggestions = suggestions.filter(s => {
                const lower = s.toLowerCase();
                if (seen.has(lower)) return false;
                seen.add(lower);
                return true;
            });

            dropdown.innerHTML = '';
            if (uniqueSuggestions.length === 0) {
                dropdown.style.display = 'none';
                return;
            }

            uniqueSuggestions.forEach(s => {
                const li = document.createElement('li');
                li.textContent = s;
                li.addEventListener('click', () => {
                    titleInput.value = s;
                    dropdown.style.display = 'none';
                    loadSeasons(s); // Load seasons after user picks a title
                });
                dropdown.appendChild(li);
            });

            const rect = titleInput.getBoundingClientRect();
            dropdown.style.top = rect.bottom + window.scrollY + 'px';
            dropdown.style.left = rect.left + window.scrollX + 'px';
            dropdown.style.width = rect.width + 'px';
            dropdown.style.display = 'block';
        })
        .catch(err => {
            console.error('Autocomplete fetch error:', err);
            dropdown.style.display = 'none';
        });
});


document.addEventListener('click', (e) => {
    if(!titleInput.contains(e.target) && !dropdown.contains(e.target)){ dropdown.style.display='none'; }
});

function loadSeasons(title){
    fetch(`/seasons?title=${encodeURIComponent(title)}`)
        .then(r => r.json())
        .then(data => {
            selectedTmdbId = data.tmdb_id;
            const seasons = data.seasons;
            seasonSelect.innerHTML = '<option value="">Season</option>';
            seasons.forEach(s => {
                const opt = document.createElement('option');
                opt.value = s.season_number;
                opt.textContent = s.name;
                seasonSelect.appendChild(opt);
            });
            episodeSelect.innerHTML = '<option value="">Episode</option>';
        });
}

seasonSelect.addEventListener('change', () => {
    const seasonNumber = seasonSelect.value;
    if(!seasonNumber || !selectedTmdbId) return;

    fetch(`/episodes?tmdb_id=${selectedTmdbId}&season_number=${seasonNumber}`)
        .then(r => r.json())
        .then(eps => {
            episodeSelect.innerHTML = '<option value="">Episode</option>';
            const today = new Date();

            eps.forEach(ep => {
                // Only include episodes that have already aired
                if(!ep.air_date || new Date(ep.air_date) > today) return;
                const opt = document.createElement('option');
                opt.value = ep.episode_number;
                opt.textContent = `${ep.episode_number}: ${ep.name}`;
                episodeSelect.appendChild(opt);
            });

            if(episodeSelect.options.length === 1){
                const opt = document.createElement('option');
                opt.textContent = 'No released episodes';
                opt.disabled = true;
                episodeSelect.appendChild(opt);
            }
        });
});

function load(){
    const title = titleInput.value.trim();
    const season = seasonSelect.value;
    const episode = episodeSelect.value;
    if(!title){ alert('Enter a title'); return; }
    showLoading(true);
    updateDebug('Searching TMDb...');
    fetch(`/get_m3u8?title=${encodeURIComponent(title)}&season=${season}&episode=${episode}`)
        .then(r => r.text())
        .then(url => {
            showLoading(false);
            if(!url){ updateDebug('No video found'); alert('No video found'); return; }
            updateDebug('Video URL captured. Loading HLS...');
            const proxied = '/proxy_playlist?url=' + encodeURIComponent(url);
            if(Hls.isSupported()){
                if(hlsInstance) hlsInstance.destroy();
                hlsInstance = new Hls();
                hlsInstance.loadSource(proxied);
                hlsInstance.attachMedia(video);
                hlsInstance.on(Hls.Events.MANIFEST_PARSED, () => { updateDebug('HLS manifest parsed. Playing video...'); video.play().catch(()=>{}); });
               hlsInstance.on(Hls.Events.ERROR, (event, data) => {
    // updateDebug('Hls.js error: ' + JSON.stringify(data)); // disabled
    console.error('Hls.js error:', data); // still logs to console if you want
});

            } else if(video.canPlayType('application/vnd.apple.mpegurl')){
                video.src = proxied;
                video.addEventListener('loadedmetadata', () => { updateDebug('Video metadata loaded. Playing...'); video.play().catch(()=>{}); });
            } else { updateDebug('HLS not supported in this browser'); alert('HLS not supported in this browser'); }
        })
        .catch(err => { showLoading(false); updateDebug('Error: '+err); alert('Failed to load video'); });
}
</script>
</body>
</html>
"""

# ----------------------------
# Endpoint to get m3u8 URL via TMDb lookup
# ----------------------------
@app.route("/get_m3u8")
def get_m3u8():
    title = request.args.get("title")
    season = request.args.get("season")
    episode = request.args.get("episode")
    if not title:
        return "", 400

    update_msg = []

    # Step 1: Search TMDb instead of OMDb
    results = search_tmdb(title)
    update_msg.append(f"TMDB returned {len(results)} results")

    best = get_best_match(title, results)
    if not best:
        return "", 404

    # Step 2: Get IMDb ID from TMDb external IDs
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

    # Step 3: Build vsrc embed URL
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

    final_m3u8 = get_best_variant(first_m3u8)
    update_msg.append(f"Final m3u8 URL: {final_m3u8}")
    debug("\n".join(update_msg))

    return final_m3u8
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
    app.run(debug=True, host="0.0.0.0", port=5000)




