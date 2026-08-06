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

def _origin_from_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"

def get_headers() -> dict:
    origin = _current_origin["value"] or "https://cloudorchestranova.com"
    return {
        "User-Agent": "Mozilla/5.0",
        "Origin": origin,
        "Referer": origin + "/",
    }

# ─────────────────────────────────────────────────────────────────────────────
# Per-host Origin/Referer for proxying playlists/segments. These now come
# straight from the browser's own request to generate.php (the endpoint the
# CDN actually authenticates the stream against) rather than being derived
# from the embed iframe's own URL — see capture_first_m3u8.
# ─────────────────────────────────────────────────────────────────────────────
_stream_headers = {}
_stream_headers_lock = threading.Lock()

def remember_stream_headers(url_for_host: str, headers: dict):
    """Associates the Origin/Referer to send with a CDN *host* (not the
    exact URL) — variant playlists and segments share the master's host but
    aren't the same URL, so keying by host lets /proxy_playlist and
    /segment look up the right headers for any request belonging to that
    stream. This also means a background prescrape of a different episode
    (which briefly repoints the global "current" origin while it works)
    can't corrupt headers for a host the live stream has already recorded.
    """
    host = urlparse(url_for_host).netloc
    if not host or not headers:
        return
    origin = headers.get("Origin")
    referer = headers.get("Referer")
    if not origin and not referer:
        return
    with _stream_headers_lock:
        _stream_headers[host] = {"Origin": origin, "Referer": referer}
        if len(_stream_headers) > 200:
            oldest_key = next(iter(_stream_headers))
            del _stream_headers[oldest_key]

def get_headers_for_stream(url: str) -> dict:
    host = urlparse(url).netloc
    with _stream_headers_lock:
        stored = _stream_headers.get(host)
    if stored and (stored.get("Origin") or stored.get("Referer")):
        h = {"User-Agent": "Mozilla/5.0"}
        if stored.get("Origin"):
            h["Origin"] = stored["Origin"]
        if stored.get("Referer"):
            h["Referer"] = stored["Referer"]
        return h
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

# ─────────────────────────────────────────────────────────────────────────────
# m3u8 capture
#
# Previously this fetched the vsrc embed page with a plain HTTP request,
# regexed the <iframe src=...> out of it, and loaded THAT resolved iframe
# URL directly in the browser. Now the browser loads the vsrc embed URL
# itself — the iframe is just part of the page and the browser resolves it
# naturally, so there's no separate manual HTTP+regex step. Because the
# player's actual DOM now often lives inside a nested iframe rather than
# being the top-level document, play-button clicking has to search across
# frames too (see both branches below).
#
# Origin/Referer are no longer derived from the iframe's own host — they're
# read directly from the browser's own request to generate.php, which is
# what the CDN actually authenticates the stream against. generate.php's
# response body also contains the playback token, read directly from
# source rather than parsed back out of the captured m3u8 URL's query
# string (which may not always carry it).
# ─────────────────────────────────────────────────────────────────────────────

TOKEN_SOURCE_HINT = "generate.php"

# Scripts the page tries to load that we want to prevent from ever
# executing — the same effect as manually right-clicking a request in
# Chrome DevTools' Network tab and choosing "Block request URL". Matched
# as a plain substring against the full request URL (not a glob/regex),
# so these just need to be distinctive enough not to false-positive on
# something unrelated.
BLOCKED_SCRIPT_PATTERNS = ["sbx.js", "disable-devtool.js", "t_.js"]

# How long to wait after seeing a candidate .m3u8 request before trusting
# it, in case a newer one supersedes it shortly after (some sources issue
# a decoy/probe manifest request before the real one).
DEBOUNCE_SECONDS = 1.2


def _extract_token_from_text(text):
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            for key in ("token", "access_token", "playback_token", "authToken"):
                val = data.get(key)
                if isinstance(val, str) and val:
                    return val
    except Exception:
        pass
    m = re.search(r'token["\']?\s*[:=]\s*["\']?([A-Za-z0-9_\-\.]{20,})', text)
    if m:
        return m.group(1)
    return None


def _headers_from_cdp_request(header_dict):
    """CDP's Network.requestWillBeSent already gives headers as a plain
    dict keyed however the browser sent them (case can vary)."""
    if not isinstance(header_dict, dict):
        return {}
    lower = {k.lower(): v for k, v in header_dict.items()}
    return {"Origin": lower.get("origin"), "Referer": lower.get("referer")}


def capture_first_m3u8(page_url: str, retries=3):
    """Loads page_url (the vsrc embed URL) directly in the browser and
    captures the master .m3u8 URL along with the Origin/Referer/token
    harvested from the generate.php request/response.

    Returns (m3u8_url, headers) where headers is {"Origin":..., "Referer":...}
    (falls back to a same-origin guess if generate.php was never observed),
    or (None, None) if nothing could be captured.
    """
    def attempt():
        result = {"url": None, "headers": {}, "token": None}
        lock = threading.Lock()
        done = threading.Event()
        chrome_failed = threading.Event()

        PLAY_SELECTORS = [
            "button.vjs-play-control", ".vjs-big-play-button",
            ".jw-icon-play", ".jw-icon-display",
            ".play-btn", ".plyr__control--overlaid",
            "[aria-label='Play']", "[aria-label='play']",
            ".ytp-large-play-button", "video",
        ]

        def _find_and_click(driver, max_depth=3):
            """Searches the current frame context, then recurses into
            nested iframes (up to max_depth levels), clicking the first
            matching selector found anywhere. Returns True if something
            was clicked."""
            for sel in PLAY_SELECTORS:
                try:
                    el = driver.find_element("css selector", sel)
                    driver.execute_script("arguments[0].click();", el)
                    return True
                except Exception:
                    continue
            if max_depth <= 0:
                return False
            try:
                iframes = driver.find_elements("tag name", "iframe")
            except Exception:
                iframes = []
            for iframe in iframes:
                try:
                    driver.switch_to.frame(iframe)
                except Exception:
                    continue
                clicked = _find_and_click(driver, max_depth - 1)
                try:
                    driver.switch_to.parent_frame()
                except Exception:
                    try:
                        driver.switch_to.default_content()
                    except Exception:
                        pass
                if clicked:
                    return True
            return False

        def click_play_chrome(driver):
            # The player often isn't fully initialized right when the
            # page's 'load' event fires (especially now that some of its
            # bootstrap scripts are blocked) — retry for a few seconds
            # rather than giving up after a single immediate attempt.
            for _ in range(8):
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass
                if _find_and_click(driver):
                    return True
                time.sleep(0.4)
            # Nothing matched any selector, anywhere. Fall back to a
            # synthetic mouse click via CDP (a "real" trusted-looking
            # click, unlike a JS .click() call) at the center of the
            # viewport, then a plain JS elementFromPoint click as a last
            # resort.
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
            try:
                driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "x": 640, "y": 360, "button": "left", "clickCount": 1})
                driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": 640, "y": 360, "button": "left", "clickCount": 1})
                return True
            except Exception:
                pass
            try:
                driver.execute_script("document.elementFromPoint(640, 360)?.click();")
                return True
            except Exception:
                pass
            return False

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
            # Cross-origin iframes normally run in a separate renderer
            # process ("Site Isolation" / OOPIFs). Network.enable + the
            # performance log via Selenium is scoped to the top-level CDP
            # target it's attached to, and does NOT automatically pick up
            # network events from those out-of-process child frames — even
            # though DevTools' own Network tab (which attaches to every
            # target) shows them fine. Since the real player now often
            # lives a couple of iframes deep on a different origin, this is
            # almost certainly why the master .m3u8 request was invisible
            # to us while it's clearly visible in DevTools. Disabling site
            # isolation keeps same-window iframes in the same process/
            # target as the top page, so their traffic shows up in the same
            # performance log stream we're already reading.
            options.add_argument("--disable-site-isolation-trials")
            options.add_argument("--disable-features=IsolateOrigins,site-per-process")
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

            # get_log("performance") is scoped to whichever window/tab is
            # currently focused, so a popup (a new window handle) needs its
            # own Network.enable/setBlockedURLs call and its own polling
            # pass — it isn't automatically covered by enabling it on the
            # original tab.
            enabled_handles = set()

            def enable_network(handle):
                if handle in enabled_handles:
                    return
                try:
                    driver.execute_cdp_cmd("Network.enable", {})
                except Exception as e:
                    debug(f"[Chrome] Network.enable failed for window {handle}: {e}")
                try:
                    driver.execute_cdp_cmd(
                        "Network.setBlockedURLs",
                        {"urls": [f"*{name}*" for name in BLOCKED_SCRIPT_PATTERNS]},
                    )
                except Exception as e:
                    debug(f"[Chrome] setBlockedURLs failed for window {handle}: {e}")
                enabled_handles.add(handle)
                debug(f"[Chrome] Network monitoring enabled for window {handle}")

            try:
                driver = uc.Chrome(**kwargs)
                debug("[Chrome] Browser launched (site isolation disabled)")
                enable_network(driver.current_window_handle)
                driver.get(page_url)
                try:
                    driver.get_log("performance")  # drain the initial burst before we start diffing
                except Exception:
                    pass
                click_play_chrome(driver)

                deadline = time.time() + 120
                candidate_url = None
                candidate_time = None
                seen_response_ids = set()
                seen_request_urls = set()  # dedupes the diagnostic per-request log line, not capture logic
                known_handles = set(driver.window_handles)

                while time.time() < deadline:
                    if done.is_set():
                        break

                    try:
                        current_handles = driver.window_handles
                    except Exception:
                        current_handles = list(known_handles)
                    for h in current_handles:
                        if h not in known_handles:
                            debug(f"[Chrome] New window/tab/popup detected: {h}")
                            known_handles.add(h)

                    for handle in list(known_handles):
                        if handle not in current_handles:
                            known_handles.discard(handle)
                            enabled_handles.discard(handle)
                            continue
                        try:
                            driver.switch_to.window(handle)
                        except Exception:
                            continue
                        enable_network(handle)
                        try:
                            logs = driver.get_log("performance")
                        except Exception:
                            continue
                        for entry in logs:
                            try:
                                msg = json.loads(entry["message"])["message"]
                                method = msg.get("method")
                                if method == "Network.requestWillBeSent":
                                    req = msg["params"]["request"]
                                    url = req.get("url", "")
                                    doc_url = msg["params"].get("documentURL", "")
                                    # Every request we actually see, logged
                                    # once each — this is the diagnostic
                                    # trail: compare it against what
                                    # DevTools' Network tab shows to see
                                    # exactly which requests we're (still)
                                    # missing versus which ones we catch but
                                    # aren't recognizing as the master.
                                    if url not in seen_request_urls:
                                        seen_request_urls.add(url)
                                        debug(f"[Chrome][req win={handle[-6:]}] doc={doc_url} url={url}")
                                    if TOKEN_SOURCE_HINT in url:
                                        hdrs = _headers_from_cdp_request(req.get("headers", {}))
                                        if hdrs.get("Origin") or hdrs.get("Referer"):
                                            with lock:
                                                result["headers"] = hdrs
                                            debug(f"[Chrome] generate.php request headers captured: {hdrs}")
                                    if ".m3u8" in url:
                                        candidate_url = url
                                        candidate_time = time.time()
                                        debug(f"[Chrome] Candidate m3u8: {url} (doc={doc_url}, win={handle[-6:]})")
                                elif method == "Network.responseReceived":
                                    resp = msg["params"]["response"]
                                    if TOKEN_SOURCE_HINT in resp.get("url", ""):
                                        request_id = msg["params"]["requestId"]
                                        if request_id not in seen_response_ids:
                                            seen_response_ids.add(request_id)
                                            try:
                                                body = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": request_id})
                                                token = _extract_token_from_text(body.get("body", ""))
                                                if token:
                                                    with lock:
                                                        result["token"] = token
                                                    debug("[Chrome] Extracted token from generate.php response")
                                            except Exception as body_err:
                                                debug(f"[Chrome] Could not read generate.php response body: {body_err}")
                            except Exception:
                                continue

                    if candidate_url and (time.time() - candidate_time) >= DEBOUNCE_SECONDS:
                        with lock:
                            if not result["url"]:
                                result["url"] = candidate_url
                                debug(f"[Chrome] Captured m3u8 (settled after debounce): {candidate_url}")
                        done.set()
                        return
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

                def _block_scripts(route):
                    url = route.request.url
                    if any(name in url for name in BLOCKED_SCRIPT_PATTERNS):
                        debug(f"[Playwright] Blocking script: {url}")
                        route.abort()
                    else:
                        route.continue_()

                page.route("**/*", _block_scripts)

                pw_candidate = {"url": None, "time": None}

                def on_request(req):
                    url = req.url
                    if TOKEN_SOURCE_HINT in url:
                        try:
                            hdrs = req.headers
                        except Exception:
                            hdrs = {}
                        origin = hdrs.get("origin")
                        referer = hdrs.get("referer")
                        if origin or referer:
                            with lock:
                                result["headers"] = {"Origin": origin, "Referer": referer}
                            debug(f"[Playwright] generate.php request headers captured: Origin={origin} Referer={referer}")
                    if ".m3u8" in url:
                        with lock:
                            pw_candidate["url"] = url
                            pw_candidate["time"] = time.time()
                        debug(f"[Playwright] Candidate m3u8: {url}")

                def on_response(resp):
                    if TOKEN_SOURCE_HINT in resp.url:
                        try:
                            text = resp.text()
                        except Exception:
                            text = None
                        token = _extract_token_from_text(text)
                        if token:
                            with lock:
                                result["token"] = token
                            debug("[Playwright] Extracted token from generate.php response")

                page.on("request", on_request)
                page.on("response", on_response)
                try:
                    page.goto(page_url, wait_until="load", timeout=30000)
                except Exception as e:
                    debug(f"[Playwright] Page load error: {e}")

                # The player's DOM is often inside a nested iframe now that
                # we load the embed page directly, so search every frame
                # (not just the main page) for something clickable. Retry
                # for a few seconds too — the player often isn't fully
                # initialized right when 'load' fires, especially now that
                # some of its bootstrap scripts are blocked.
                clicked = False
                for _ in range(8):
                    for frame in page.frames:
                        for sel in PLAY_SELECTORS:
                            try:
                                el = frame.query_selector(sel)
                                if el:
                                    el.click()
                                    clicked = True
                                    break
                            except Exception:
                                continue
                        if clicked:
                            break
                    if clicked:
                        break
                    page.wait_for_timeout(400)
                if not clicked:
                    try:
                        page.mouse.click(640, 360)
                    except Exception:
                        pass

                for _ in range(120):
                    if done.is_set():
                        break
                    with lock:
                        cand_url, cand_time = pw_candidate["url"], pw_candidate["time"]
                    if cand_url and not result["url"] and cand_time and (time.time() - cand_time) >= DEBOUNCE_SECONDS:
                        with lock:
                            if not result["url"]:
                                result["url"] = cand_url
                                debug(f"[Playwright] Captured m3u8 (settled after debounce): {cand_url}")
                        done.set()
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
            return None, {}, None

        return result["url"], result["headers"], result["token"]

    for attempt_num in range(1, retries + 1):
        debug(f"[capture] Attempt {attempt_num}/{retries}")
        url, headers, token = attempt()
        if url:
            # Prefer the token read directly from generate.php's response
            # body over whatever (if anything) already sits on the
            # captured URL's own query string.
            if token:
                parts = urlparse(url)
                qs = parse_qs(parts.query)
                qs["token"] = [token]
                url = urlunparse(parts._replace(query=urlencode(qs, doseq=True)))
            page_origin = _origin_from_url(page_url)
            resolved_headers = {
                "Origin": headers.get("Origin") or page_origin,
                "Referer": headers.get("Referer") or (page_origin + "/"),
            }
            debug(f"Final m3u8 (attempt {attempt_num}): {url}")
            debug(f"Resolved stream headers (attempt {attempt_num}): {resolved_headers}")
            return url, resolved_headers
        debug(f"[capture] Attempt {attempt_num} failed, retrying...")

    debug("All capture attempts failed")
    return None, None

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

def _request_header_variants(url: str):
    """A few different request 'shapes' to try in turn against a CDN URL.
    Some CDNs soft-block a request that doesn't look the way they expect —
    not with a 403/429 (which fetch_bytes_retry's status-code retry logic
    already handles) but with a plain 200 and an empty body, which looks
    identical to "successfully fetched nothing" from the HTTP layer alone.
    Since we can't tell in advance which header shape a given CDN wants,
    try progressively different ones rather than giving up after the first
    empty response — this is cheap since each attempt only costs one quick
    request, and it's exactly the "try it a different way" fallback that
    matters when the plain, direct fetch keeps coming back empty despite
    the URL clearly having real content behind it.
    """
    base = get_headers_for_stream(url)
    ua = base.get("User-Agent", "Mozilla/5.0")

    # 1) Exactly what every other request in this proxy uses: the actual
    #    Origin/Referer captured from the browser's own generate.php
    #    request (or a same-origin guess if that was never observed).
    variant_normal = dict(base)

    # 2) No Origin/Referer at all. If the CDN is actually doing a strict
    #    Referer/Origin allowlist check and silently zeroing the body on a
    #    mismatch (rather than rejecting outright), a bare request with
    #    neither header can succeed where a wrong-looking one wouldn't.
    variant_bare = {"User-Agent": ua}

    # 3) A fuller "real browser" header set layered on top of the normal
    #    Origin/Referer, in case what's tripping it up is generic bot
    #    fingerprinting (missing Accept/Accept-Language/Sec-Fetch-* etc.)
    #    rather than anything about Origin/Referer specifically.
    variant_browserish = dict(base)
    variant_browserish.update({
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
    })

    return [variant_normal, variant_bare, variant_browserish]


def fetch_bytes_with_fallback(url: str, max_retries_per_variant: int = 1, base_delay: float = 0.3) -> bytes:
    """Like fetch_bytes_retry, but if a request "succeeds" (2xx) with a
    suspiciously empty body, tries again with a different header shape
    (see _request_header_variants) before giving up. A plain empty body
    isn't distinguishable from a real error by status code alone, so this
    inspects the payload itself and only accepts a result once it actually
    has content."""
    last_exc = None
    for headers in _request_header_variants(url):
        try:
            data = fetch_bytes_retry(url, max_retries=max_retries_per_variant, base_delay=base_delay, headers=headers)
        except Exception as e:
            last_exc = e
            continue
        if data:
            return data
        debug(f"[fetch_bytes_with_fallback] Empty body from {url} using headers={list(headers.keys())} — trying next fallback")
    if last_exc:
        raise last_exc
    raise RuntimeError(f"All header strategies returned an empty body for {url}")


def fetch_playlist_bytes_with_fallback(url: str, max_retries_per_variant: int = 5, base_delay: float = 1.2) -> bytes:
    """Same idea as fetch_bytes_with_fallback, but for the playlist
    endpoint specifically: a response can come back non-empty and still be
    unusable (a CDN error page, a captcha wall, etc. instead of real M3U8
    text), so this checks for the actual '#EXTM3U' signature rather than
    just "did we get any bytes at all" before accepting a result."""
    last_exc = None
    last_bad = None
    for headers in _request_header_variants(url):
        try:
            data = fetch_bytes_retry(url, max_retries=max_retries_per_variant, base_delay=base_delay, headers=headers)
        except Exception as e:
            last_exc = e
            continue
        if data and data.lstrip().startswith(b'#EXTM3U'):
            return data
        last_bad = data
        debug(f"[fetch_playlist_bytes_with_fallback] Non-playlist response from {url} using headers={list(headers.keys())} — trying next fallback")
    if last_exc:
        raise last_exc
    # Every variant returned *something* but none of it was a valid
    # playlist — return the last attempt's bytes (even if empty) so the
    # caller's own "isn't a real playlist" handling can produce the
    # right error message instead of masking it as a network failure.
    return last_bad or b''


_PERMANENT_HTTP_STATUS_CODES = {403, 404, 410}

def fetch_bytes_retry(url, max_retries=6, base_delay=1.5, headers=None):
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            r = requests.get(url, headers=headers or get_headers(), timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as e:
            # A real network-level failure (connection error, timeout,
            # etc.) — this is the transient case retrying is meant for.
            last_exc = e
            if attempt < max_retries:
                wait = base_delay * (2 ** attempt)
                debug(f"[fetch_bytes_retry] error on {url} ({e}) — retrying in {wait:.1f}s")
                time.sleep(wait)
                continue
            raise

        if r.status_code in _PERMANENT_HTTP_STATUS_CODES:
            # Genuinely gone/forbidden, not a transient blip — these
            # ephemeral per-stream CDN links can just be dead on arrival.
            # Retrying with backoff (or trying alternate header shapes)
            # wastes tens of seconds to minutes waiting for a 404 to
            # become a 200, which it essentially never does. Fail fast so
            # the caller (fetch_bytes_with_fallback's next header variant,
            # or the client's own "get a fresh link" recovery) can react
            # right away instead of the person staring at a spinner.
            debug(f"[fetch_bytes_retry] {r.status_code} on {url} — not retrying, this looks permanent")
            r.raise_for_status()

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
        # A handful of retries with growing backoff absorbs both the rare
        # transient 5xx/524 (origin timeout) blip from the upstream CDN,
        # and — more importantly for THIS endpoint specifically, since it's
        # the very first fetch of a freshly-minted manifest URL — gives a
        # brand new token real time to propagate/settle across the CDN's
        # edge network if it 403s immediately after being minted. This is
        # a one-time cost per stream (not per-segment), so it's fine for it
        # to take a bit longer than the segment endpoint's fast-fail retry.
        # fetch_bytes_with_fallback additionally tries alternate request
        # header shapes if a response comes back empty, in case that's the
        # CDN quietly soft-blocking rather than a real transient failure.
        # Genuinely permanent errors (403/404/410) now fail immediately
        # rather than retrying — see fetch_bytes_retry.
        pl_bytes = fetch_playlist_bytes_with_fallback(url, max_retries_per_variant=5, base_delay=1.2)
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
        # Fail fast per attempt (short single retry) rather than a slow
        # backoff — native players (AVPlayer etc.) already retry a failed
        # segment fetch on their own shortly after, so it's better for us
        # to respond quickly and let the player's own retry catch it than
        # to add several more seconds of server-side backoff on top.
        # fetch_bytes_with_fallback additionally tries a couple of
        # different request header shapes if the first attempt "succeeds"
        # with an empty body — the actual issue behind the 416s seen
        # earlier, where the plain fetch was quietly coming back with zero
        # bytes despite the URL clearly having real content.
        remote_bytes = fetch_bytes_with_fallback(url, max_retries_per_variant=1, base_delay=0.3)
    except Exception as e:
        return f"Failed to fetch remote segment: {e}", 502

    # extract_ts_packets only recognizes classic 188-byte-aligned MPEG-TS
    # (looking for 0x47 sync bytes at regular intervals). Some sources
    # serve segments that don't fit that shape (e.g. fMP4/CMAF chunks, or
    # TS that just isn't packet-aligned the way it expects) — in that case
    # it finds nothing and returns empty, which used to silently discard a
    # segment that actually had perfectly valid content (confirmed by the
    # fact that fetching the raw URL directly plays fine). Rather than lose
    # real data, fall back to the untouched fetched bytes whenever
    # extraction comes back empty despite the fetch itself succeeding.
    extracted = extract_ts_packets(remote_bytes)
    if extracted:
        remote_bytes = extracted
    elif remote_bytes:
        debug(f"[segment] extract_ts_packets found no TS sync pattern in {len(remote_bytes)} bytes from {url} — passing raw bytes through instead of discarding them")

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
                # The requested range can't be satisfied — almost always
                # because the underlying fetch above came back empty (a
                # transient upstream failure the single fast retry didn't
                # catch), not because the player asked for something
                # unreasonable. A real 416 here is fatal: native players
                # (AVPlayer in particular) don't gracefully retry after a
                # 416 the way they do after a 502/404 — they just freeze
                # instead of advancing to the next segment. Falling back to
                # a plain 200 with whatever content we actually have (which
                # the player will simply re-request with Range again on its
                # own if it still comes up short) keeps playback moving
                # instead of wedging it.
                debug(f"[segment] Range {range_header} unsatisfiable against {total_len} bytes for {url} — falling back to full content instead of 416")
                resp = Response(remote_bytes, mimetype="video/MP2T")
                resp.headers['Content-Length'] = str(total_len)
                resp.headers['Accept-Ranges'] = 'bytes'
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

    # Loads vsrc_embed directly in the browser — no more manually fetching
    # the embed page over plain HTTP and regexing the iframe out of it
    # beforehand. The browser resolves the iframe naturally as part of
    # rendering the page, and headers/token are harvested from the actual
    # generate.php request/response observed during that render.
    first_m3u8, stream_headers = capture_first_m3u8(vsrc_embed, retries=3)
    if not first_m3u8:
        return "", 404
    update_msg.append(f"First m3u8 captured: {first_m3u8}")
    debug("\n".join(update_msg))

    remember_stream_headers(first_m3u8, stream_headers)
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


_imdb_id_cache: dict = {}   # tmdb_id (tv) -> imdb_id, avoids re-hitting TMDb's external_ids endpoint every time
_imdb_id_cache_lock = threading.Lock()


def get_tv_imdb_id(tmdb_id: int):
    """Resolves a TMDb TV show ID to its IMDb ID, cached in-memory since
    it's the same lookup /get_m3u8 and the prescraper already do per show —
    the intro-skip lookup below needs the same imdb_id again and there's no
    reason to hit TMDb a second time for something that never changes."""
    with _imdb_id_cache_lock:
        cached = _imdb_id_cache.get(tmdb_id)
    if cached:
        return cached
    external_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/external_ids?api_key={TMDB_API_KEY}"
    try:
        imdb_id = requests.get(external_url, timeout=REQUEST_TIMEOUT).json().get("imdb_id")
    except Exception as e:
        debug(f"[get_tv_imdb_id] Failed to resolve imdb_id for tmdb_id={tmdb_id}: {e}")
        return None
    if imdb_id:
        with _imdb_id_cache_lock:
            _imdb_id_cache[tmdb_id] = imdb_id
    return imdb_id


@app.route("/get_intro")
def get_intro():
    """Looks up intro/recap/outro start-end windows for a TV episode from
    api.introdb.app's /segments endpoint, so the player can show a Skip
    button during either intro/recap, and reveal the Next Episode button
    once the outro actually starts. Resolves imdb_id from the already-known
    tmdb_id (same lookup /get_m3u8 does internally) rather than requiring
    the client to know or pass it directly."""
    tmdb_id = request.args.get("tmdb_id")
    season = request.args.get("season")
    episode = request.args.get("episode")
    if not tmdb_id or not season or not episode:
        return jsonify({"status": "invalid"}), 400
    try:
        tmdb_id, season, episode = int(tmdb_id), int(season), int(episode)
    except ValueError:
        return jsonify({"status": "invalid"}), 400

    imdb_id = get_tv_imdb_id(tmdb_id)
    if not imdb_id:
        return jsonify({"status": "none"})

    try:
        r = requests.get(
            "https://api.introdb.app/segments",
            params={"imdb_id": imdb_id, "season": season, "episode": episode},
            timeout=8,
        )
        if r.status_code == 404:
            # No segment data for this episode — not an error, just
            # nothing to show a Skip button for.
            return jsonify({"status": "none"})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        debug(f"[get_intro] Lookup failed for imdb_id={imdb_id} S{season}E{episode}: {e}")
        return jsonify({"status": "error", "msg": str(e)})

    def _parse_segment(seg):
        """{"start_ms","end_ms","start_sec","end_sec",...} -> {"start","end"}
        in seconds, preferring the *_sec fields and falling back to *_ms/1000
        if those are missing. None if the segment is absent or malformed."""
        if not isinstance(seg, dict):
            return None
        start = seg.get("start_sec")
        end = seg.get("end_sec")
        if start is None or end is None:
            start_ms, end_ms = seg.get("start_ms"), seg.get("end_ms")
            if start_ms is not None and end_ms is not None:
                start, end = start_ms / 1000.0, end_ms / 1000.0
        if start is None or end is None:
            return None
        try:
            start, end = float(start), float(end)
        except (TypeError, ValueError):
            return None
        if end <= start:
            return None
        return {"start": start, "end": end}

    intro = _parse_segment(data.get("intro")) if isinstance(data, dict) else None
    recap = _parse_segment(data.get("recap")) if isinstance(data, dict) else None
    outro = _parse_segment(data.get("outro")) if isinstance(data, dict) else None

    if not intro and not recap and not outro:
        return jsonify({"status": "none"})

    result = {"status": "done"}
    if intro:
        result["intro"] = intro
    if recap:
        result["recap"] = recap
    if outro:
        result["outro"] = outro
    return jsonify(result)


def _resolve_tv_episode_stream(tmdb_id: int, season: int, episode: int):
    """Same resolution pipeline as /get_m3u8's TV branch, but starting from
    an already-known tmdb_id instead of a title search — used for
    prescraping the next episode in the background. Returns (m3u8, headers)
    same as capture_first_m3u8."""
    external_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/external_ids?api_key={TMDB_API_KEY}"
    external_resp = requests.get(external_url, timeout=REQUEST_TIMEOUT).json()
    imdb_id = external_resp.get("imdb_id")
    if not imdb_id:
        return None, None
    vsrc_embed = f"https://vsrc.su/embed/tv?imdb={imdb_id}&season={season}&episode={episode}&dts=dd"
    return capture_first_m3u8(vsrc_embed, retries=3)


def _run_prescrape_job(key: str, tmdb_id: int, season: int, episode: int):
    # Scraping repoints the shared "current origin" while it works (the
    # same mechanism /get_m3u8 uses). Save/restore it around the job so a
    # prescrape running alongside an actively-playing stream doesn't leave
    # that global origin pointed at the wrong show any longer than the
    # scrape itself takes — the per-host header cache (remember_stream_
    # headers/get_headers_for_stream) is the real protection for the live
    # stream's own proxy requests; this is just belt-and-suspenders.
    saved_origin = _current_origin["value"]
    try:
        m3u8, stream_headers = _resolve_tv_episode_stream(tmdb_id, season, episode)
        if m3u8:
            remember_stream_headers(m3u8, stream_headers)
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


@app.route("/invalidate_prescraped")
def invalidate_prescraped():
    """Drops a specific prescrape cache entry. Called by the client when a
    prescraped/cached m3u8 turns out to be dead on arrival (e.g. the
    upstream 403'd it), so nobody else gets served that same broken link
    before its normal TTL would have expired it."""
    tmdb_id = request.args.get("tmdb_id")
    season = request.args.get("season")
    episode = request.args.get("episode")
    if not tmdb_id or not season or not episode:
        return jsonify({"status": "invalid"}), 400
    try:
        tmdb_id, season, episode = int(tmdb_id), int(season), int(episode)
    except ValueError:
        return jsonify({"status": "invalid"}), 400
    key = _prescrape_key(tmdb_id, season, episode)
    with _prescrape_lock:
        _prescrape_cache.pop(key, None)
    return jsonify({"status": "ok"})


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
/* In real fullscreen the container's own rounded corners/border become
   visible as an odd curved gap against the fullscreen black canvas, since
   the browser just enlarges this element as-is rather than trimming it —
   strip them while fullscreen so the video reads as a clean rectangle. */
#video-container:fullscreen,
#video-container:-webkit-full-screen {
  /* The container's own max-width: 900px (set below) otherwise survives
     into fullscreen unchanged — width alone doesn't override it — leaving
     the "fullscreen" view stuck at 900px wide instead of actually filling
     the screen. Reset it here, and center the (now letterboxed) video
     within the full-viewport black box. */
  border-radius: 0; border: none; box-shadow: none;
  width: 100vw; height: 100vh; max-width: none;
  display: flex; align-items: center; justify-content: center;
}
#video-container:fullscreen video,
#video-container:-webkit-full-screen video {
  width: 100%; height: 100%; aspect-ratio: unset; object-fit: contain;
}
#downloadBtn { margin-top: 18px; }
#downloadBtn:disabled { background: #3a3a3f; color: #74747c; cursor: not-allowed; box-shadow: none; transform: none; filter: none; }
#downloadQuality {
  -webkit-appearance: none; -moz-appearance: none; appearance: none;
  display: block;
  /* auto left/right margins center this regardless of its own width —
     more robust than relying solely on the parent's flex alignment, and
     avoids the asymmetric-margin bug that was pushing it off-center
     before (a lone margin-left shifts a centered flex item's visible
     content away from true center). */
  flex: 0 0 auto;
  margin: 18px auto 0;
  width: 150px;
  height: 44px; line-height: 20px;
  padding: 0 14px;
  font-size: 0.95rem;
  text-align: center; text-align-last: center;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  background: var(--bg-elevated-2);
  color: var(--text);
  font-family: inherit;
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
#bottomRightControls {
  /* Now holds only Next Episode / Skip Intro — the custom fullscreen
     button has its own separate slot below, in the native button's old
     spot, rather than sharing this row. Raised higher above the native
     controls bar since the buttons themselves are bigger too. */
  position: absolute; bottom: 72px; right: 14px; z-index: 100;
  display: flex; align-items: center; gap: 8px;
}
#nextEpisodeBtn, #skipIntroBtn {
  background: rgba(10,10,12,0.6); color: #fff; border: 1px solid rgba(255,255,255,0.2);
  border-radius: 10px; padding: 12px 20px; font-size: 0.95rem; font-weight: 600; cursor: pointer;
  font-family: 'Inter', sans-serif; backdrop-filter: blur(6px);
  display: none; align-items: center; gap: 6px; box-shadow: 0 4px 16px rgba(0,0,0,0.35);
  transition: background 0.15s ease, transform 0.15s ease, opacity 0.25s ease;
}
#nextEpisodeBtn:hover, #skipIntroBtn:hover { background: rgba(10,10,12,0.85); transform: translateY(-1px); }
#customFullscreenBtn {
  /* Desktop only (see the display:none in the mobile block below, and the
     matching native-button-hide media query further down) — positioned
     relative to #video-container directly (it's a sibling of
     #bottomRightControls in the HTML, not nested inside it), so it sits
     flush in the bottom-right corner exactly where the native controls'
     own fullscreen icon used to be, rather than measuring its offset from
     the row above. */
  position: absolute; bottom: 6px; right: 8px; z-index: 100;
  background: transparent; color: #fff; border: none;
  border-radius: 6px; padding: 6px; font-size: 1.1rem; line-height: 1; cursor: pointer;
  font-family: 'Inter', sans-serif;
  width: 34px; height: 34px;
  display: flex; align-items: center; justify-content: center;
  opacity: 0; pointer-events: none;
  box-shadow: none; transform: none; filter: none;
  transition: opacity 0.2s ease, background 0.15s ease;
}
#customFullscreenBtn:hover {
  background: rgba(255,255,255,0.15);
  box-shadow: none; transform: none; filter: none;
}
#customFullscreenBtn.controls-visible { opacity: 1; pointer-events: auto; }
@media (max-width: 768px) {
  #customFullscreenBtn { display: none !important; }
}
/* Native fullscreen icon is only hidden/replaced on desktop — on mobile
   there's no custom button (see rule above), so removing the native one
   there would leave no way to fullscreen at all. Firefox has no
   equivalent pseudo-element hook for customizing native <video> controls,
   so this is best-effort there. */
@media (min-width: 769px) {
  video::-webkit-media-controls-fullscreen-button {
    display: none !important;
  }
}
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
  #bottomRightControls { bottom: 58px; right: 10px; }
  #nextEpisodeBtn, #skipIntroBtn { padding: 10px 14px; font-size: 0.85rem; }
  #downloadQuality { margin: 12px auto 0; width: 130px; height: 40px; line-height: 18px; padding: 0 10px; font-size: 0.85rem; }
}
/* Phone in landscape: keyed off height rather than width, since a rotated
   phone's width often exceeds the 768px breakpoint above (so that block
   alone wouldn't catch it) — a short viewport height is what actually
   signals "phone turned sideways" regardless of how wide it is. There's
   much more room to work with here than in portrait, so let the video
   claim most of the screen instead of staying pinned to the same ~35vh. */
@media (orientation: landscape) and (max-height: 600px) {
  #video-container video {
    height: 90vh; max-height: 95vh;
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
  <video id="video" controls crossorigin playsinline x-webkit-airplay="allow"></video>
  <select id="quality" style="display:none;"><option value="-1">Auto</option></select>
  <button id="customFullscreenBtn" title="Fullscreen">&#9974;</button>
  <div id="bottomRightControls">
    <button id="nextEpisodeBtn">Next Episode &#9656;</button>
    <button id="skipIntroBtn">Skip Intro &#9656;</button>
  </div>
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
const skipIntroBtn = document.getElementById('skipIntroBtn');
const videoContainer = document.getElementById('video-container');
const customFullscreenBtn = document.getElementById('customFullscreenBtn');
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
let introWindow = null; // {intro: {start,end}|null, recap: {start,end}|null} once known
let outroWindow = null; // {start,end}|null once known (null = no outro data for this episode)
let introLookupKey = null; // guards against a slow /get_intro response from a previous episode landing late
let introLookupDone = false; // true once the /get_intro fetch for the current episode has settled (success, "none", or error) — tells us whether outroWindow's null means "no outro" vs. "not resolved yet"

function currentQueryKey(){
    const {title,year}=parseTitleAndYear(titleInput.value.trim());
    return [title.trim().toLowerCase(), year||'', seasonSelect.value||'', episodeSelect.value||''].join('|');
}

// ── Next Episode prescrape + button reveal ──────────────────────────────
// The background prescrape of the next episode and the moment the "Next
// Episode" button actually becomes visible are deliberately decoupled:
// the prescrape should start a bit early so the link is ready by the time
// it's needed, but the button itself should only appear once the outro is
// actually underway (so it doesn't show up over regular episode content).
//
// When outro timing is known for this episode (from /get_intro):
//   - prescrape starts PRESCRAPE_LEAD_BEFORE_OUTRO_SECONDS before the outro begins
//   - the button appears once the outro actually starts
// When no outro data is available for this episode:
//   - prescrape starts PRESCRAPE_LEAD_NO_OUTRO_SECONDS before the end
//   - the button appears NEXT_BTN_LEAD_NO_OUTRO_SECONDS before the end
const PRESCRAPE_LEAD_BEFORE_OUTRO_SECONDS = 120; // 2 minutes before the outro begins
const PRESCRAPE_LEAD_NO_OUTRO_SECONDS = 240;     // 4 minutes before the end, when there's no outro data
const NEXT_BTN_LEAD_NO_OUTRO_SECONDS = 120;      // 2 minutes before the end, when there's no outro data

function maybeTriggerNextEpisodePrescrape(){
    if (!loadedTmdbId || !loadedSeasonNum || !loadedEpisodeNum) return;
    if (prescrapeTriggeredForKey === loadedQueryKey) return;
    if (!introLookupDone) return; // wait until we know whether this episode has outro data
    if (!isFinite(video.duration) || video.duration <= 0) return;

    const triggerAt = outroWindow
        ? outroWindow.start - PRESCRAPE_LEAD_BEFORE_OUTRO_SECONDS
        : video.duration - PRESCRAPE_LEAD_NO_OUTRO_SECONDS;
    if (video.currentTime < triggerAt) return;

    prescrapeTriggeredForKey = loadedQueryKey; // only ever try once per loaded episode
    fetch('/prescrape_next?tmdb_id='+encodeURIComponent(loadedTmdbId)+'&season='+encodeURIComponent(loadedSeasonNum)+'&episode='+encodeURIComponent(loadedEpisodeNum))
        .then(r=>r.json())
        .then(function(d){
            if (d && d.season && d.episode) {
                console.log('Preloading next episode: S'+d.season+'E'+d.episode);
                nextEpisodeTarget = { season: d.season, episode: d.episode };
                // Button visibility is handled separately by
                // updateNextEpisodeButtonVisibility — it's gated on the
                // outro actually starting (or the near-end fallback),
                // not on the prescrape merely having been kicked off.
            }
        })
        .catch(()=>{});
}

function updateNextEpisodeButtonVisibility(){
    if (!nextEpisodeTarget) { nextEpisodeBtn.style.display = 'none'; return; }
    let showAt;
    if (outroWindow) {
        showAt = outroWindow.start;
    } else if (isFinite(video.duration) && video.duration > 0) {
        showAt = video.duration - NEXT_BTN_LEAD_NO_OUTRO_SECONDS;
    } else {
        nextEpisodeBtn.style.display = 'none';
        return;
    }
    nextEpisodeBtn.style.display = (video.currentTime >= showAt) ? 'flex' : 'none';
}

// ── Skip Intro / Skip Recap ─────────────────────────────────────────────
// Looked up once per episode from /get_intro (a thin wrapper around
// api.introdb.app's /segments endpoint), then shown/hidden purely based on
// current playback position — no separate polling needed once we have the
// windows. Reuses the same slot/style as #nextEpisodeBtn (see the CSS)
// since none of the three ever need to be visible at once: recap and
// intro are near the start, next-episode only near the end.
let activeSkipSegment = null; // {type: 'intro'|'recap', end} for whichever window currentTime is inside right now

function maybeLookupIntro(){
    introWindow = null;
    outroWindow = null;
    introLookupDone = false;
    activeSkipSegment = null;
    skipIntroBtn.style.display = 'none';
    if (!loadedTmdbId || !loadedSeasonNum || !loadedEpisodeNum) {
        // Movies have no season/episode segment data — treat as
        // "resolved, nothing found" so the next-episode logic (which
        // doesn't apply to movies anyway) isn't left waiting forever.
        introLookupDone = true;
        return;
    }
    const key = loadedQueryKey;
    introLookupKey = key;
    fetch('/get_intro?tmdb_id='+encodeURIComponent(loadedTmdbId)+'&season='+encodeURIComponent(loadedSeasonNum)+'&episode='+encodeURIComponent(loadedEpisodeNum))
        .then(r=>r.json())
        .then(function(d){
            // If a different episode has since loaded (or this one was
            // re-resolved) while this request was in flight, its result no
            // longer applies — drop it rather than showing stale windows.
            if (introLookupKey !== key) return;
            introLookupDone = true;
            if (!d || d.status !== 'done') return;
            introWindow = {
                intro: (d.intro && typeof d.intro.start === 'number' && typeof d.intro.end === 'number') ? d.intro : null,
                recap: (d.recap && typeof d.recap.start === 'number' && typeof d.recap.end === 'number') ? d.recap : null,
            };
            outroWindow = (d.outro && typeof d.outro.start === 'number' && typeof d.outro.end === 'number') ? d.outro : null;
        })
        .catch(function(){
            if (introLookupKey !== key) return;
            introLookupDone = true; // failed lookup falls back to the no-outro-data behavior
        });
}

function updateSkipIntroVisibility(){
    if (!introWindow) return;
    const t = video.currentTime;
    // Recap typically plays before the intro/opening titles, but check
    // both regardless of order — whichever window currentTime actually
    // falls inside wins.
    let match = null;
    if (introWindow.recap && t >= introWindow.recap.start && t < introWindow.recap.end) {
        match = { type: 'recap', end: introWindow.recap.end };
    } else if (introWindow.intro && t >= introWindow.intro.start && t < introWindow.intro.end) {
        match = { type: 'intro', end: introWindow.intro.end };
    }
    activeSkipSegment = match;
    if (match) {
        skipIntroBtn.textContent = (match.type === 'recap' ? 'Skip Recap' : 'Skip Intro') + ' \u25B8';
        skipIntroBtn.style.display = 'flex';
    } else {
        skipIntroBtn.style.display = 'none';
    }
}

skipIntroBtn.addEventListener('click', function(){
    if (!activeSkipSegment) return;
    video.currentTime = activeSkipSegment.end;
    activeSkipSegment = null;
    skipIntroBtn.style.display = 'none';
});

video.addEventListener('timeupdate', () => {
    lastKnownTime = video.currentTime;
    maybeTriggerNextEpisodePrescrape();
    updateNextEpisodeButtonVisibility();
    updateSkipIntroVisibility();
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

// ── Custom fullscreen button (desktop only) ─────────────────────────────
// Hidden entirely on mobile via CSS (see #customFullscreenBtn's media
// query) — this is purely a desktop convenience since mobile has its own
// native fullscreen affordances. Fullscreens #video-container itself
// (not the <video> element), so everything else in the container —
// including the Next Episode / Skip Intro buttons — renders normally on
// top of it instead of being left behind by a video-only fullscreen.
function isInFullscreen(){
    return !!(document.fullscreenElement || document.webkitFullscreenElement);
}
function updateFullscreenButtonIcon(){
    customFullscreenBtn.innerHTML = isInFullscreen() ? '&#10005;' : '&#9974;';
    customFullscreenBtn.title = isInFullscreen() ? 'Exit fullscreen' : 'Fullscreen';
}
function toggleFullscreen(){
    if (isInFullscreen()) {
        if (document.exitFullscreen) document.exitFullscreen().catch(()=>{});
        else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
    } else if (videoContainer.requestFullscreen) {
        videoContainer.requestFullscreen().catch(()=>{});
    } else if (videoContainer.webkitRequestFullscreen) {
        videoContainer.webkitRequestFullscreen();
    }
}
customFullscreenBtn.addEventListener('click', toggleFullscreen);
document.addEventListener('fullscreenchange', updateFullscreenButtonIcon);
document.addEventListener('webkitfullscreenchange', updateFullscreenButtonIcon);

// The CSS pseudo-element hiding rule for the native fullscreen icon only
// hides it visually in the browsers that expose that hook (Chrome/Edge/
// Safari); controlsList="nofullscreen" is what actually disables it
// functionally. Kept desktop-only and reactive to resizing via matchMedia
// rather than a static HTML attribute, so it doesn't linger applied (or
// stay absent) if the window crosses the breakpoint after load.
const DESKTOP_MQ = window.matchMedia('(min-width: 769px)');
function syncNativeFullscreenControl(){
    if (DESKTOP_MQ.matches) {
        video.setAttribute('controlsList', 'nofullscreen');
    } else {
        video.removeAttribute('controlsList');
    }
}
syncNativeFullscreenControl();
DESKTOP_MQ.addEventListener('change', syncNativeFullscreenControl);

// Fades the button in/out roughly in step with the native player's own
// controls (play/seek/volume): visible on mouse activity/paused, hidden
// again after a couple seconds of inactivity during playback, and hidden
// immediately when the cursor leaves the video (matching how native
// controls disappear the instant the mouse exits, rather than waiting out
// the idle timer). There's no way to hook into the browser's actual
// native-controls visibility state directly, so this is a simulation
// tuned to match Chrome/Edge's default ~2s idle timeout.
let controlsIdleTimer = null;
function showFloatingFullscreenButton(){
    customFullscreenBtn.classList.add('controls-visible');
    if (controlsIdleTimer) { clearTimeout(controlsIdleTimer); controlsIdleTimer = null; }
    if (!video.paused) {
        controlsIdleTimer = setTimeout(function(){
            customFullscreenBtn.classList.remove('controls-visible');
        }, 2000);
    }
}
function hideFloatingFullscreenButtonNow(){
    if (video.paused) return; // native controls stay visible while paused
    if (controlsIdleTimer) { clearTimeout(controlsIdleTimer); controlsIdleTimer = null; }
    customFullscreenBtn.classList.remove('controls-visible');
}
videoContainer.addEventListener('mousemove', showFloatingFullscreenButton);
videoContainer.addEventListener('mouseenter', showFloatingFullscreenButton);
videoContainer.addEventListener('mouseleave', hideFloatingFullscreenButtonNow);
videoContainer.addEventListener('click', showFloatingFullscreenButton);
video.addEventListener('play', showFloatingFullscreenButton);
video.addEventListener('pause', showFloatingFullscreenButton);
showFloatingFullscreenButton(); // visible by default until playback actually starts

// ── Mobile: "+10s" skip button during intro/outro ───────────────────────
// iOS's native fullscreen player shows its own built-in "skip 10 seconds"
// button that this page has no direct hook into — there's no discrete
// event for "the +10s button was tapped", only the resulting seek. So
// this watches for a seek that lands ~10 seconds ahead of where playback
// just was, and if that jump started from inside the intro/recap or
// outro window, treats it as "skip the whole thing" instead of a plain
// +10s: jumping straight to the end of the intro/recap, or loading the
// next episode if it was the outro. A manual scrub-bar drag landing by
// coincidence within ~1.5s of a 10-second jump would also trigger this;
// an accepted tradeoff since there's no way to tell the two apart.
const IS_MOBILE_DEVICE = /Mobi|Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
const TEN_SECOND_SKIP_MIN = 8.5;
const TEN_SECOND_SKIP_MAX = 11.5;
let suppressSeekHeuristic = false;

if (IS_MOBILE_DEVICE) {
    video.addEventListener('seeking', function(){
        if (suppressSeekHeuristic) { suppressSeekHeuristic = false; return; }
        const from = lastKnownTime;
        const to = video.currentTime;
        const delta = to - from;
        if (delta < TEN_SECOND_SKIP_MIN || delta > TEN_SECOND_SKIP_MAX) return;

        if (introWindow) {
            const recap = introWindow.recap;
            const intro = introWindow.intro;
            if (recap && from >= recap.start && from < recap.end) {
                suppressSeekHeuristic = true;
                video.currentTime = recap.end;
                return;
            }
            if (intro && from >= intro.start && from < intro.end) {
                suppressSeekHeuristic = true;
                video.currentTime = intro.end;
                return;
            }
        }
        if (outroWindow && from >= outroWindow.start && from < outroWindow.end && nextEpisodeTarget) {
            goToNextEpisode();
        }
    });
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
//
// IMPORTANT: this must only ever be written once playback is *confirmed*
// working (i.e. from attachStream's onReady callback) — never optimistically
// right after a URL is resolved. A link can be resolved successfully but
// still be dead on arrival (e.g. upstream 403s it), and if that URL were
// cached here anyway, every later recovery path that reads this cache
// (reresolveAndResume's quick-reattach fast path, in particular) would just
// keep re-serving the exact same broken link on every subsequent failure —
// which is exactly the "retrying does nothing" symptom this used to cause.
const STREAM_CACHE_KEY = 'flexstream_last_stream';
function saveStreamCache(m3u8, title, queryKey){
    try{ localStorage.setItem(STREAM_CACHE_KEY, JSON.stringify({ m3u8, title, queryKey, ts: Date.now() })); }catch(e){}
}
function getStreamCache(){
    try{ const r=localStorage.getItem(STREAM_CACHE_KEY); return r?JSON.parse(r):null; }catch(e){ return null; }
}
function clearStreamCache(){
    try{ localStorage.removeItem(STREAM_CACHE_KEY); }catch(e){}
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
//
// A load-timeout failsafe backs up the 'error' event: an upstream 403/502
// on the very first manifest fetch doesn't always surface as a fired
// 'error' event on <video> (some browsers just leave it stuck at
// readyState 0 indefinitely instead), which used to show the "can't play"
// icon while never actually calling onFail — so nothing ever recovered.
// If loadedmetadata hasn't happened within LOAD_TIMEOUT_MS, treat it as a
// failure exactly like a real 'error' event would.
const LOAD_TIMEOUT_MS = 20000;
function attachStream(m3u8, resumeAt, onReady, onFail){
    const proxied='/proxy_playlist?url='+encodeURIComponent(m3u8);
    let readyFired=false;
    let failFired=false;
    function fail(){
        if (readyFired || failFired) return;
        failFired = true;
        if (onFail) onFail();
    }
    const nativeHlsSupported = video.canPlayType('application/vnd.apple.mpegurl');

    if(nativeHlsSupported){
        usingNativeHls = true;
        currentLevels = null; // native playback has no manual per-level list to expose
        if(hlsInstance){ hlsInstance.destroy(); hlsInstance = null; }
        qualitySelect.style.display='none';
        video.src=proxied;
        const loadTimeoutId = setTimeout(fail, LOAD_TIMEOUT_MS);
        video.addEventListener('loadedmetadata',function onMeta(){
            clearTimeout(loadTimeoutId);
            if(resumeAt && resumeAt>1) video.currentTime=resumeAt;
            video.play().catch(()=>{});
            video.removeEventListener('loadedmetadata', onMeta);
            readyFired=true;
            updateDebug('Playing (native HLS, AirPlay-ready)');
            if(onReady) onReady();
        });
        video.addEventListener('error', function onErr(){
            video.removeEventListener('error', onErr);
            clearTimeout(loadTimeoutId);
            fail();
        }, { once: true });
    } else if(Hls.isSupported()){
        usingNativeHls = false;
        if(hlsInstance) hlsInstance.destroy();
        hlsInstance=new Hls();
        const loadTimeoutId = setTimeout(fail, LOAD_TIMEOUT_MS);
        hlsInstance.loadSource(proxied);
        hlsInstance.attachMedia(video);
        hlsInstance.on(Hls.Events.MANIFEST_PARSED,(event,data)=>{
            clearTimeout(loadTimeoutId);
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
                    clearTimeout(loadTimeoutId);
                    if (!readyFired) fail();
                    else reresolveAndResume();
                    break;
            }
        });
    } else {
        updateDebug('HLS not supported in this browser');
        fail();
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

    function fullScrape(){
        let url='/get_m3u8?title='+encodeURIComponent(title);
        if(year) url+='&year='+year;
        if(season) url+='&season='+season;
        if(episode) url+='&episode='+episode;
        return fetch(url).then(r=>r.text()).then(function(m3u8){ return finish(m3u8, false); })
            .catch(err=>{ showLoading(false); updateDebug('Error: '+err); alert('Failed to load video'); return null; });
    }

    // Up to two automatic fresh-scrape retries if a resolved link turns out
    // to be dead on arrival (e.g. upstream 403). A single captured link
    // occasionally comes from a bad mirror/edge and a re-scrape gets a
    // working one; this happens silently (spinner keeps showing) rather
    // than surfacing a failure to the person unless *every* attempt fails.
    let attemptsLeft = 2;

    function finish(m3u8, fromCache){
        showLoading(false);
        if(!m3u8){ updateDebug('No video found'); alert('No video found'); return null; }
        updateDebug(fromCache ? 'Using preloaded stream...' : 'Video URL captured. Loading HLS...');
        currentM3u8Url=m3u8;
        loadedQueryKey=queryKey;
        loadedTmdbId=selectedTmdbId||null;
        loadedSeasonNum=season?parseInt(season,10):null;
        loadedEpisodeNum=episode?parseInt(episode,10):null;
        prescrapeTriggeredForKey=null; // this episode hasn't triggered its own next-episode prescrape yet
        nextEpisodeTarget=null;
        nextEpisodeBtn.style.display='none';
        introWindow=null;
        outroWindow=null;
        introLookupDone=false;
        introLookupKey=null;
        skipIntroBtn.style.display='none';
        downloadBtn.disabled=false;
        addRecent(titleInput.value.trim()||title);
        updateMediaSessionMetadata(currentTitle);

        showLoading(true);
        attachStream(m3u8, resumeAt, function onReady(){
            showLoading(false);
            // Only now — playback has actually started — is it safe to
            // remember this link for future quick-reattach recovery.
            saveStreamCache(m3u8, currentTitle, loadedQueryKey);
            maybeLookupIntro();
        }, function onFail(){
            // This exact link never played. Make sure nothing downstream
            // (this tab's recovery paths, or a future load of the same
            // episode) can reuse it: invalidate the server-side prescrape
            // entry for it if it came from one, and clear the client-side
            // stream cache too in case a previous *different* episode's
            // entry is somehow still pointing at it.
            if (loadedTmdbId && season && episode) {
                fetch('/invalidate_prescraped?tmdb_id='+encodeURIComponent(loadedTmdbId)+'&season='+encodeURIComponent(season)+'&episode='+encodeURIComponent(episode)).catch(()=>{});
            }
            const cached = getStreamCache();
            if (cached && cached.m3u8 === m3u8) clearStreamCache();

            if (attemptsLeft > 0) {
                attemptsLeft--;
                updateDebug('Stream link failed, fetching a fresh one...');
                showLoading(true);
                fullScrape();
                return;
            }
            showLoading(false);
            updateDebug('Playback failed to start');
            alert('Playback failed to start after a few tries. The source may be temporarily unavailable — please try again in a bit.');
        });
        return m3u8;
    }

    // If this exact episode was already prescraped in the background while
    // the previous one was finishing up, skip the search+scrape entirely
    // and use that instantly.
    if (selectedTmdbId && season && episode) {
        const preUrl='/get_prescraped?tmdb_id='+encodeURIComponent(selectedTmdbId)+'&season='+encodeURIComponent(season)+'&episode='+encodeURIComponent(episode);
        return fetch(preUrl).then(r=>r.json()).then(function(pre){
            if (pre && pre.status==='done' && pre.m3u8) {
                return finish(pre.m3u8, true);
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
// that fails too (e.g. the link genuinely expired). Because the stream
// cache is now only ever populated after confirmed-successful playback
// (see saveStreamCache's call site above), that fast path can never
// re-serve a link that's already known to be dead.
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
            const c = getStreamCache();
            if (c && c.m3u8 === cachedUrl) clearStreamCache();
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

// ── Stall watchdog ───────────────────────────────────────────────────────
// A player that's already playing can still get stuck mid-stream (e.g. a
// segment 403s). Native players usually self-heal from a brief stall by
// skipping ahead a few seconds — that's normal and left alone. But if it's
// stuck for a long stretch (10s+) rather than the usual brief blip, that's
// worth actually recovering from rather than leaving the viewer stuck.
let stallTimer = null;
video.addEventListener('waiting', function(){
    if (stallTimer || !currentM3u8Url) return;
    stallTimer = setTimeout(function(){
        stallTimer = null;
        if (video.paused || video.readyState < 3) {
            updateDebug('Stalled, attempting recovery...');
            reresolveAndResume();
        }
    }, 10000);
});
video.addEventListener('playing', function(){
    if (stallTimer) { clearTimeout(stallTimer); stallTimer = null; }
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
