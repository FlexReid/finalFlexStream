"""
Discogs Record Side Timer
==========================
A single-file Flask app for a Raspberry Pi 4 (or anywhere else).

- Syncs your Discogs collection into a local SQLite database.
- Lets you pick an album + side (A/B/C/D... or Disc N, or a flat
  "Tracks" list for CD/digital) and shows how long it takes to play.
- Admin panel (password protected) to link a release in your collection
  to a *different* Discogs pressing when your own copy is missing
  track-length data - your copy still displays everywhere as itself,
  but the time calculation is pulled from the linked pressing.
- Manual per-track duration overrides, for when no pressing on Discogs
  has the times and you have to stopwatch it yourself.
- A playback speed control on the side page, so a 33 1/3 record played
  back at 45 (or a 78 played on a 33 table) shows the real running time.
- An admin-wide "extra time per side" pad, added to every side total to
  account for lead-in groove, gaps and getting up to flip the record.
- A server-side countdown: pressing Play starts a timer that lives in the
  Flask process itself, not the browser tab. When it reaches zero it calls
  the Tuya Cloud API to switch off a smart plug (your turntable, an amp,
  whatever's plugged into it) - even if the phone that started it has
  locked, closed the tab, or walked out of WiFi range.

Setup
-----
    pip install Flask Flask-SQLAlchemy requests python-dotenv tuya-connector-python

Credentials live in the Config class below so this runs as-is. These
environment variables override them if you ever want them to (a .env
file next to this script works too):

    DISCOGS_USERNAME   your Discogs username
    DISCOGS_TOKEN      personal access token from
                        https://www.discogs.com/settings/developers
    ADMIN_PASSWORD     password to access /admin
    SECRET_KEY         random string for signing the session cookie
    TUYA_ACCESS_ID     from the Tuya IoT Platform, Cloud > your project
    TUYA_ACCESS_SECRET from the same place
    TUYA_DEVICE_ID     the plug's device ID (Devices tab)
    TUYA_ENDPOINT      the data-center endpoint for your account, e.g.
                        https://openapi.tuyacn.com (China),
                        https://openapi.tuyaus.com (US),
                        https://openapi.tuyaeu.com (EU),
                        https://openapi.tuyain.com (India)
    TUYA_SWITCH_CODE   the DP code the plug's switch responds to
                        (almost always "switch_1")
    LOG_LEVEL          DEBUG, INFO (default), WARNING, or ERROR. DEBUG logs
                        every timer tick, status poll, and Tuya API call -
                        handy while setting up the smart plug, noisy for
                        everyday use.

Run
---
    python app.py                           # http://<pi-ip>:5000

This uses Flask's own development server. That's fine for a single
household device like this sitting on your LAN - it's not handling
public internet traffic or serious concurrent load, just you and
maybe a couple of phones on the same WiFi.

Upgrading an existing install
-----------------------------
The two new tables (track_override, setting) are created automatically
by db.create_all() on first run. No existing columns changed, so your
current app.db carries over as-is.

The countdown timer's state lives in memory, not the database - it's
one physical turntable, so there's only ever one timer, and restarting
the app (or the Pi) is the same as the record having stopped anyway.
If the app restarts mid-side, the plug will NOT switch off on its own;
you'd need to do that by hand or start a fresh timer.

A systemd unit for autostart on boot is included as a comment at the
bottom of this file.
"""

import functools
import json
import logging
import os
import re
import threading
import time
from datetime import datetime

import requests
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from jinja2 import DictLoader

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass


# ============================================================================
# Logging
# ============================================================================
# LOG_LEVEL controls verbosity: DEBUG shows every timer tick, poll, and
# plug API call in detail (useful while wiring up the Tuya side of this);
# INFO (the default) shows the high-level events only. Logs go to stdout,
# so under systemd they land in `journalctl -u side-timer -f`.

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("side_timer")
plug_log = logging.getLogger("side_timer.plug")
timer_log = logging.getLogger("side_timer.timer")
log.debug("Logging configured at %s", LOG_LEVEL)


def mask(value, keep=4):
    """Never put a full secret in the logs. 'abcd1234efgh' -> 'abcd…efgh'."""
    if not value:
        return "(empty)"
    value = str(value)
    if len(value) <= keep * 2:
        return "…" * len(value)
    return f"{value[:keep]}…{value[-keep:]}"


# ============================================================================
# Config
# ============================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "please-change-this-secret-key")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "app.db")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Baked in so this just runs on the Pi without a .env file. Environment
    # variables still win if they're set.
    DISCOGS_USERNAME = os.environ.get("DISCOGS_USERNAME", "Flex_Reid")
    DISCOGS_TOKEN = os.environ.get("DISCOGS_TOKEN", "MwMKvpYaecGTwaCzeNByaCVKSqSifknXqUfofcOW")
    DISCOGS_USER_AGENT = os.environ.get("DISCOGS_USER_AGENT", "MyDiscogsApp/1.0 +https://github.com")

    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "password")

    # Smart plug (Tuya Cloud). The endpoint below has the "openapi."
    # subdomain that Tuya's China data center actually uses - the
    # tuyacn.com example on their docs page is missing it.
    TUYA_ACCESS_ID = os.environ.get("TUYA_ACCESS_ID", "nnn87wfj9xx7ds9svpm3")
    TUYA_ACCESS_SECRET = os.environ.get("TUYA_ACCESS_SECRET", "5eb2204f013941beb66786917ce74bf9")
    TUYA_DEVICE_ID = os.environ.get("TUYA_DEVICE_ID", "bfa6710fe5d66c9987s8bh")
    TUYA_ENDPOINT = os.environ.get("TUYA_ENDPOINT", "https://openapi.tuyaeu.com")
    TUYA_SWITCH_CODE = os.environ.get("TUYA_SWITCH_CODE", "switch_1")

db = SQLAlchemy()

log.debug("Config: DISCOGS_USERNAME=%s DISCOGS_TOKEN=%s", Config.DISCOGS_USERNAME, mask(Config.DISCOGS_TOKEN))
log.debug("Config: TUYA_ENDPOINT=%s TUYA_ACCESS_ID=%s TUYA_ACCESS_SECRET=%s TUYA_DEVICE_ID=%s TUYA_SWITCH_CODE=%s",
          Config.TUYA_ENDPOINT, mask(Config.TUYA_ACCESS_ID), mask(Config.TUYA_ACCESS_SECRET),
          mask(Config.TUYA_DEVICE_ID), Config.TUYA_SWITCH_CODE)


# ============================================================================
# Models
# ============================================================================

class Release(db.Model):
    """A Discogs release. Rows are used both for items actually in your
    collection AND for 'reference' pressings kept only to supply timing
    data for a PressingLink."""

    id = db.Column(db.Integer, primary_key=True)
    discogs_id = db.Column(db.Integer, unique=True, nullable=False, index=True)

    title = db.Column(db.String(500))
    artists = db.Column(db.String(500))
    year = db.Column(db.Integer)
    thumb_url = db.Column(db.String(500))
    cover_url = db.Column(db.String(500))
    master_id = db.Column(db.Integer, nullable=True)
    formats_desc = db.Column(db.String(300))
    discogs_url = db.Column(db.String(500))

    tracklist_json = db.Column(db.Text, nullable=True)
    details_fetched_at = db.Column(db.DateTime, nullable=True)

    in_collection = db.Column(db.Boolean, default=False, index=True)
    instance_id = db.Column(db.Integer, nullable=True)
    date_added = db.Column(db.DateTime, nullable=True)
    last_synced = db.Column(db.DateTime, default=datetime.utcnow)

    link = db.relationship(
        "PressingLink",
        foreign_keys="PressingLink.source_release_id",
        uselist=False,
        backref="source",
        cascade="all, delete-orphan",
    )

    overrides = db.relationship(
        "TrackOverride",
        backref="release",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def tracklist(self):
        if not self.tracklist_json:
            return []
        return json.loads(self.tracklist_json)

    def has_details(self):
        return bool(self.tracklist_json)

    def timing_source(self):
        """Release whose tracklist should actually be used for timing -
        either this release, or its linked reference pressing if set."""
        if self.link and self.link.target_release and self.link.target_release.has_details():
            return self.link.target_release
        return self

    def is_using_linked_pressing(self):
        return bool(self.link and self.link.target_release)

    def override_map(self):
        return {o.position: o for o in self.overrides}

    def effective_tracklist(self):
        """The timing source's tracklist with any manual duration
        overrides applied on top. Overrides always belong to *this*
        release, so they survive linking and unlinking a pressing."""
        om = self.override_map()
        out = []
        for idx, track in enumerate(self.timing_source().tracklist()):
            track = dict(track)
            o = om.get(track_key(track, idx))
            if o:
                track["duration"] = o.duration
                track["overridden"] = True
            out.append(track)
        return out

    def override_count(self):
        return len(self.overrides)

    def sides(self):
        return group_into_sides(self.effective_tracklist())

    def side_summary(self, extra_seconds=None):
        if extra_seconds is None:
            extra_seconds = get_extra_seconds()
        summary = []
        for side_name, tracks in self.sides():
            music, complete = sum_durations(tracks)
            total = None if music is None else music + extra_seconds
            summary.append(
                {
                    "name": side_name,
                    "track_count": len([t for t in tracks if t.get("type_", "track") == "track"]),
                    "music_seconds": music,
                    "extra_seconds": extra_seconds,
                    "total_seconds": total,
                    "total_display": format_seconds(total) if total else None,
                    "complete": complete,
                }
            )
        return summary


class PressingLink(db.Model):
    """Links a collection release (source) to a different pressing
    (target) whose tracklist/durations should be used instead."""

    id = db.Column(db.Integer, primary_key=True)
    source_release_id = db.Column(db.Integer, db.ForeignKey("release.id"), unique=True, nullable=False)
    target_release_id = db.Column(db.Integer, db.ForeignKey("release.id"), nullable=False)
    target_release = db.relationship("Release", foreign_keys=[target_release_id])
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class TrackOverride(db.Model):
    """A hand-entered duration for one track of one release. Keyed by
    track position ('A1', '2-4') or, for releases with blank positions,
    by a synthetic '#3' key based on order in the tracklist."""

    id = db.Column(db.Integer, primary_key=True)
    release_id = db.Column(db.Integer, db.ForeignKey("release.id"), nullable=False, index=True)
    position = db.Column(db.String(40), nullable=False)
    duration = db.Column(db.String(20), nullable=False)   # normalised "3:45"
    seconds = db.Column(db.Integer, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint("release_id", "position", name="uq_override_release_pos"),)


class Setting(db.Model):
    """Tiny key/value store for app-wide admin settings."""

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.String(200))


# ----------------------------------------------------------------------
# Settings helpers
# ----------------------------------------------------------------------

EXTRA_PER_SIDE_KEY = "extra_seconds_per_side"


def get_setting(key, default=None):
    row = Setting.query.filter_by(key=key).first()
    if row is None or row.value is None:
        return default
    return row.value


def set_setting(key, value):
    row = Setting.query.filter_by(key=key).first()
    if row is None:
        row = Setting(key=key)
        db.session.add(row)
    row.value = str(value)
    db.session.commit()
    return row


def get_extra_seconds():
    """Seconds added to every side total. Never negative."""
    try:
        return max(0, int(get_setting(EXTRA_PER_SIDE_KEY, 0) or 0))
    except (TypeError, ValueError):
        return 0


# ----------------------------------------------------------------------
# Tracklist / side / duration helpers
# ----------------------------------------------------------------------

_LETTER_SIDE = re.compile(r"^\s*([A-Za-z]+)")
_DISC_TRACK = re.compile(r"^\s*(\d+)[\-\.]")


def track_key(track, idx):
    """Stable identifier for a track within a tracklist. Uses the Discogs
    position where there is one; CD/digital releases often leave position
    blank, so fall back to ordinal position."""
    pos = (track.get("position") or "").strip()
    return pos if pos else f"#{idx + 1}"


def group_into_sides(tracklist):
    """Groups a Discogs tracklist into sides using the 'position' field.
    Handles 'A1'/'B1'-style vinyl sides, multi-disc box sets (each letter
    is its own side), disc/track numbering ('1-1','1-2' -> Disc 1), and
    CD/digital releases with plain/blank positions (-> 'Tracks')."""
    sides = {}
    order = []
    for t in tracklist:
        pos = (t.get("position") or "").strip()
        side = None
        m = _LETTER_SIDE.match(pos)
        if m:
            side = m.group(1).upper()
        else:
            m2 = _DISC_TRACK.match(pos)
            if m2:
                side = f"Disc {m2.group(1)}"
        if not side:
            side = "Tracks"
        if side not in sides:
            sides[side] = []
            order.append(side)
        sides[side].append(t)
    return [(s, sides[s]) for s in order]


def parse_duration_to_seconds(duration_str):
    if not duration_str:
        return None
    duration_str = duration_str.strip()
    if not duration_str:
        return None
    parts = duration_str.split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return None
    if any(p < 0 for p in parts):
        return None
    secs = 0
    for p in parts:
        secs = secs * 60 + p
    return secs


def format_seconds(total_seconds):
    if total_seconds is None:
        return None
    total_seconds = int(round(total_seconds))
    h, rem = divmod(total_seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def sum_durations(tracks):
    """Returns (total_seconds_or_None, complete_bool)."""
    total = 0
    any_known = False
    complete = True
    for t in tracks:
        if t.get("type_", "track") != "track":
            continue
        secs = parse_duration_to_seconds(t.get("duration"))
        if secs is None:
            complete = False
            continue
        any_known = True
        total += secs
    if not any_known:
        return None, False
    return total, complete


def editable_tracks(release):
    """Rows for the admin duration editor: every real track of the timing
    source, with its Discogs duration and any override already set."""
    om = release.override_map()
    rows = []
    for idx, t in enumerate(release.timing_source().tracklist()):
        if t.get("type_", "track") != "track":
            continue
        key = track_key(t, idx)
        o = om.get(key)
        rows.append(
            {
                "key": key,
                "position": t.get("position") or key,
                "title": t.get("title") or "",
                "original": t.get("duration") or "",
                "override": o.duration if o else "",
            }
        )
    return rows


# ============================================================================
# Discogs API client
# ============================================================================

DISCOGS_BASE = "https://api.discogs.com"


class DiscogsError(Exception):
    pass


class DiscogsClient:
    def __init__(self, token, username, user_agent="DiscogsSideTimer/1.0"):
        self.token = token
        self.username = username
        self.headers = {"User-Agent": user_agent, "Authorization": f"Discogs token={token}"}

    def _get(self, url, params=None):
        if not self.token:
            raise DiscogsError("No Discogs API token configured. Set DISCOGS_TOKEN.")
        r = requests.get(url, headers=self.headers, params=params, timeout=20)
        if r.status_code == 429:
            time.sleep(5)
            r = requests.get(url, headers=self.headers, params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    def get_collection_page(self, page=1, per_page=100, folder=0):
        if not self.username:
            raise DiscogsError("No Discogs username configured. Set DISCOGS_USERNAME.")
        url = f"{DISCOGS_BASE}/users/{self.username}/collection/folders/{folder}/releases"
        return self._get(url, {"page": page, "per_page": per_page, "sort": "artist"})

    def iter_collection(self, per_page=100, folder=0):
        page = 1
        while True:
            data = self.get_collection_page(page=page, per_page=per_page, folder=folder)
            for item in data.get("releases", []):
                yield item
            pagination = data.get("pagination", {})
            if page >= pagination.get("pages", 1):
                break
            page += 1
            time.sleep(1.0)  # be polite to Discogs' rate limit

    def get_release(self, release_id):
        url = f"{DISCOGS_BASE}/releases/{release_id}"
        return self._get(url)

    def get_master_versions(self, master_id, page=1, per_page=50):
        url = f"{DISCOGS_BASE}/masters/{master_id}/versions"
        return self._get(url, {"page": page, "per_page": per_page})

    def search_release(self, query):
        url = f"{DISCOGS_BASE}/database/search"
        return self._get(url, {"q": query, "type": "release", "token": self.token})


def get_client(app):
    return DiscogsClient(
        token=app.config["DISCOGS_TOKEN"],
        username=app.config["DISCOGS_USERNAME"],
        user_agent=app.config["DISCOGS_USER_AGENT"],
    )


# ============================================================================
# Sync / linking services
# ============================================================================

def sync_collection(app):
    """Fast sync: pulls basic_information for every collection item and
    upserts Release rows. Full tracklists are fetched lazily elsewhere."""
    client = get_client(app)
    seen = set()
    count_new = 0
    count_updated = 0

    for item in client.iter_collection():
        info = item.get("basic_information", {})
        discogs_id = info.get("id")
        if not discogs_id:
            continue
        seen.add(discogs_id)

        release = Release.query.filter_by(discogs_id=discogs_id).first()
        is_new = release is None
        if is_new:
            release = Release(discogs_id=discogs_id)
            db.session.add(release)

        artists = ", ".join(a.get("name", "") for a in info.get("artists", []))
        formats = info.get("formats", [])
        formats_desc = ", ".join(
            f.get("name", "") + (f" ({', '.join(f.get('descriptions', []))})" if f.get("descriptions") else "")
            for f in formats
        )

        release.title = info.get("title")
        release.artists = artists
        release.year = info.get("year") or None
        release.thumb_url = info.get("thumb")
        release.cover_url = info.get("cover_image")
        release.master_id = info.get("master_id") or None
        release.formats_desc = formats_desc
        release.discogs_url = f"https://www.discogs.com/release/{discogs_id}"
        release.in_collection = True
        release.instance_id = item.get("instance_id")
        added = item.get("date_added")
        if added:
            try:
                release.date_added = datetime.strptime(added, "%Y-%m-%dT%H:%M:%S%z")
            except ValueError:
                pass
        release.last_synced = datetime.utcnow()

        if is_new:
            count_new += 1
        else:
            count_updated += 1

    db.session.commit()
    return {"new": count_new, "updated": count_updated, "total_seen": len(seen)}


def fetch_release_details(app, release):
    """Fetch full tracklist/duration data for a single Release row."""
    client = get_client(app)
    data = client.get_release(release.discogs_id)
    tracklist = data.get("tracklist", [])
    release.tracklist_json = json.dumps(tracklist)
    release.details_fetched_at = datetime.utcnow()
    if not release.master_id:
        release.master_id = data.get("master_id")
    if not release.title:
        release.title = data.get("title")
    db.session.commit()
    return release


def get_or_create_reference_release(app, discogs_release_id):
    """Ensures a Release row exists (and has details) for an arbitrary
    Discogs release id, used when linking to an alternate pressing."""
    release = Release.query.filter_by(discogs_id=discogs_release_id).first()
    if release and release.has_details():
        return release
    if not release:
        release = Release(discogs_id=discogs_release_id, in_collection=False)
        db.session.add(release)
        db.session.commit()
    fetch_release_details(app, release)
    return release


def set_pressing_link(app, source_release, target_discogs_id):
    target_release = get_or_create_reference_release(app, target_discogs_id)
    if target_release.id == source_release.id:
        raise ValueError("Can't link a release to itself.")
    link = PressingLink.query.filter_by(source_release_id=source_release.id).first()
    if link:
        link.target_release_id = target_release.id
    else:
        link = PressingLink(source_release_id=source_release.id, target_release_id=target_release.id)
        db.session.add(link)
    db.session.commit()
    return link


def remove_pressing_link(source_release):
    link = PressingLink.query.filter_by(source_release_id=source_release.id).first()
    if link:
        db.session.delete(link)
        db.session.commit()


def save_overrides(release, form):
    """Reads dur__<key> fields off a submitted form. Blank clears the
    override; anything unparseable is reported back to the caller."""
    saved = 0
    cleared = 0
    bad = []
    for row in editable_tracks(release):
        raw = (form.get("dur__" + row["key"]) or "").strip()
        existing = TrackOverride.query.filter_by(release_id=release.id, position=row["key"]).first()
        if not raw:
            if existing:
                db.session.delete(existing)
                cleared += 1
            continue
        secs = parse_duration_to_seconds(raw)
        if secs is None:
            bad.append(row["position"])
            continue
        display = format_seconds(secs)
        if existing:
            if existing.seconds != secs:
                existing.duration = display
                existing.seconds = secs
                saved += 1
        else:
            db.session.add(
                TrackOverride(release_id=release.id, position=row["key"], duration=display, seconds=secs)
            )
            saved += 1
    db.session.commit()
    return {"saved": saved, "cleared": cleared, "bad": bad}


def clear_overrides(release):
    count = TrackOverride.query.filter_by(release_id=release.id).delete()
    db.session.commit()
    return count


# ============================================================================
# Smart plug (Tuya Cloud)
# ============================================================================

def tuya_configured(app):
    c = app.config
    ok = bool(c["TUYA_ACCESS_ID"] and c["TUYA_ACCESS_SECRET"] and c["TUYA_DEVICE_ID"] and c["TUYA_ENDPOINT"])
    plug_log.debug("tuya_configured() -> %s (endpoint=%s access_id=%s device_id=%s)",
                    ok, c["TUYA_ENDPOINT"], mask(c["TUYA_ACCESS_ID"]), mask(c["TUYA_DEVICE_ID"]))
    return ok


def turn_off_plug(app):
    """Switches the configured Tuya device's switch off. Returns
    {"ok": bool, "message": str} - never raises, since this runs from a
    background timer thread with nothing to catch it."""
    plug_log.info("turn_off_plug() called")

    if not tuya_configured(app):
        plug_log.warning("Aborting: Tuya credentials aren't configured (see TUYA_* env vars).")
        return {"ok": False, "message": "Tuya credentials aren't configured."}

    try:
        from tuya_connector import TuyaOpenAPI
        plug_log.debug("tuya_connector imported OK")
    except ImportError:
        plug_log.error("tuya-connector-python isn't installed. Run: pip install tuya-connector-python")
        return {"ok": False, "message": "tuya-connector-python isn't installed. "
                                         "Run: pip install tuya-connector-python"}

    endpoint = app.config["TUYA_ENDPOINT"]
    access_id = app.config["TUYA_ACCESS_ID"]
    device_id = app.config["TUYA_DEVICE_ID"]
    switch_code = app.config["TUYA_SWITCH_CODE"]
    plug_log.debug("Connecting: endpoint=%s access_id=%s device_id=%s switch_code=%s",
                    endpoint, mask(access_id), mask(device_id), switch_code)

    try:
        client = TuyaOpenAPI(endpoint, access_id, app.config["TUYA_ACCESS_SECRET"])
        connect_resp = client.connect()
        plug_log.debug("connect() returned: %r", connect_resp)

        commands = {"commands": [{"code": switch_code, "value": False}]}
        path = f"/v1.0/iot-03/devices/{device_id}/commands"
        plug_log.debug("POST %s body=%s", path, commands)

        resp = client.post(path, commands)
        plug_log.debug("Tuya API raw response: %r", resp)

        if isinstance(resp, dict) and resp.get("success"):
            plug_log.info("Plug %s switched off successfully.", mask(device_id))
            return {"ok": True, "message": "Plug switched off."}

        err = (resp or {}).get("msg") if isinstance(resp, dict) else None
        err_code = (resp or {}).get("code") if isinstance(resp, dict) else None
        plug_log.error("Tuya API didn't confirm success: code=%s msg=%s raw=%r", err_code, err, resp)
        return {"ok": False, "message": err or f"Tuya API didn't confirm success: {resp}"}
    except Exception as e:
        plug_log.exception("Exception while calling the Tuya API")
        return {"ok": False, "message": f"Couldn't reach the plug: {e}"}


# ============================================================================
# Server-side countdown
# ============================================================================
#
# There's exactly one turntable, so there's exactly one timer, held in
# memory rather than the database - starting a new one always replaces
# whatever was running. It's driven by threading.Timer, whose callback
# fires in a background thread and calls the Tuya API directly, so the
# plug goes off on schedule regardless of whether any browser tab is
# open, the phone that started it has locked, or it's left WiFi range.

class ServerTimer:
    def __init__(self):
        self.lock = threading.Lock()
        self._thread = None
        self.label = None
        self.total_seconds = 0
        self.remaining_seconds = 0
        self.ends_at = None            # epoch seconds while running, else None
        self.status = "idle"           # idle | running | paused | done | error
        self.plug_result = None        # {"ok": bool, "message": str} from the last attempt
        self.updated_at = time.time()

    def _cancel_thread(self):
        if self._thread is not None:
            timer_log.debug("Cancelling existing threading.Timer (label=%r)", self.label)
            self._thread.cancel()
            self._thread = None

    def _fire(self, app):
        timer_log.info("Countdown reached zero for %r - firing plug-off", self.label)
        with self.lock:
            self._thread = None
            self.status = "done"
            self.ends_at = None
            self.remaining_seconds = 0
            self.updated_at = time.time()
        result = turn_off_plug(app)
        timer_log.debug("turn_off_plug() returned %r", result)
        with self.lock:
            self.plug_result = result
            self.updated_at = time.time()
        if not result["ok"]:
            timer_log.warning("Side %r finished but the plug did NOT switch off: %s", self.label, result["message"])

    def start(self, app, seconds, label):
        seconds = max(1, int(seconds))
        timer_log.info("start(): label=%r seconds=%s (replacing status=%s)", label, seconds, self.status)
        with self.lock:
            self._cancel_thread()
            self.label = label
            self.total_seconds = seconds
            self.remaining_seconds = seconds
            self.ends_at = time.time() + seconds
            self.status = "running"
            self.plug_result = None
            self.updated_at = time.time()
            self._thread = threading.Timer(seconds, self._fire, args=(app,))
            self._thread.daemon = True
            self._thread.start()
        timer_log.debug("Scheduled plug-off in %ss for %r (thread=%s)", seconds, label, self._thread.name)

    def pause(self):
        with self.lock:
            if self.status != "running":
                timer_log.debug("pause() ignored: status is %r, not running", self.status)
                return
            self._cancel_thread()
            self.remaining_seconds = max(0, self.ends_at - time.time())
            self.ends_at = None
            self.status = "paused"
            self.updated_at = time.time()
        timer_log.info("Paused %r with %.1fs remaining", self.label, self.remaining_seconds)

    def resume(self, app):
        with self.lock:
            if self.status != "paused" or self.remaining_seconds <= 0:
                timer_log.debug("resume() ignored: status=%r remaining=%s", self.status, self.remaining_seconds)
                return
            self.ends_at = time.time() + self.remaining_seconds
            self.status = "running"
            self.updated_at = time.time()
            self._thread = threading.Timer(self.remaining_seconds, self._fire, args=(app,))
            self._thread.daemon = True
            self._thread.start()
        timer_log.info("Resumed %r, %.1fs left on the clock", self.label, self.remaining_seconds)

    def reset(self):
        """Cancels the plug schedule and goes back to the top of the side."""
        timer_log.info("reset(): cancelling schedule for %r (was status=%s)", self.label, self.status)
        with self.lock:
            self._cancel_thread()
            self.remaining_seconds = self.total_seconds
            self.ends_at = None
            self.status = "idle" if self.total_seconds else "idle"
            self.plug_result = None
            self.updated_at = time.time()

    def snapshot(self):
        with self.lock:
            remaining = self.remaining_seconds
            if self.status == "running" and self.ends_at is not None:
                remaining = max(0, self.ends_at - time.time())
            snap = {
                "status": self.status,
                "label": self.label,
                "total_seconds": self.total_seconds,
                "remaining_seconds": round(remaining, 1),
                "ends_at": self.ends_at,
                "plug_result": self.plug_result,
                "updated_at": self.updated_at,
            }
        timer_log.debug("snapshot() -> %s", snap)
        return snap


server_timer = ServerTimer()


# ============================================================================
# Templates (inline, single-file - served via a Jinja DictLoader)
# ============================================================================

BASE_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#1b1d29">
  <title>{% block title %}Record Side Timer{% endblock %}</title>
  <style>
    :root {
      --bg: #12131a; --panel: #1b1d29; --panel-2: #232636; --text: #eef0f7;
      --muted: #9298b0; --accent: #7c9bff; --good: #57d68d; --warn: #ffb454;
      --bad: #ff6b6b; --radius: 14px;
      --safe-top: env(safe-area-inset-top, 0px);
      --safe-bottom: env(safe-area-inset-bottom, 0px);
    }
    * { box-sizing: border-box; -webkit-tap-highlight-color: rgba(124,155,255,0.15); }
    /* Author-level display rules below would otherwise beat the UA's
        [hidden] rule, so make hidden actually hide. */
    [hidden] { display: none !important; }
    html, body { margin:0; padding:0; background:var(--bg); color:var(--text);
      font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
      font-size:16px; -webkit-text-size-adjust:100%; }
    body.sheet-open { overflow:hidden; }
    .topbar { display:flex; align-items:center; justify-content:space-between;
      padding:calc(14px + var(--safe-top)) 16px 14px;
      background:var(--panel); position:sticky; top:0; z-index:10; border-bottom:1px solid #2a2d3d; }
    .brand { color:var(--text); font-weight:700; font-size:1.1rem; text-decoration:none; }
    .topbar nav a { color:var(--accent); text-decoration:none; margin-left:16px; font-size:0.95rem; }
    .container { max-width:720px; margin:0 auto; padding:16px; padding-bottom:calc(48px + var(--safe-bottom)); }
    h1 { font-size:1.4rem; margin:8px 0 16px; }
    h2 { font-size:1.15rem; margin:24px 0 10px; }
    p.muted, .muted { color:var(--muted); }
    a { color:var(--accent); }
    .search-form { display:flex; gap:8px; margin-bottom:16px; }
    .search-form input[type="text"] { flex:1; }
    input[type="text"], input[type="password"], input[type="number"] {
      width:100%; padding:12px 14px; border-radius:10px; border:1px solid #333750;
      background:var(--panel-2); color:var(--text); font-size:1rem; }
    button, .btn { display:inline-block; padding:12px 18px; border-radius:10px; border:none;
      background:var(--accent); color:#0c0d14; font-weight:600; font-size:0.95rem;
      text-decoration:none; cursor:pointer; min-height:44px; line-height:1.2;
      font-family:inherit; }
    button.secondary, .btn.secondary { background:var(--panel-2); color:var(--text); border:1px solid #3a3e58; }
    button.danger, .btn.danger { background:var(--bad); color:#250b0b; }
    button:active, .btn:active { transform:translateY(1px); }
    button:focus-visible, .btn:focus-visible, input:focus-visible, a:focus-visible {
      outline:2px solid var(--accent); outline-offset:2px; }
    form.inline { display:inline; }
    .album-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(150px, 1fr)); gap:14px; }
    .album-card { background:var(--panel); border-radius:var(--radius); overflow:hidden;
      text-decoration:none; color:var(--text); display:block; border:1px solid #262939;
      transition:transform .12s ease; }
    .album-card:active { transform:scale(0.97); }
    .album-card img { width:100%; aspect-ratio:1/1; object-fit:cover; display:block; background:#2a2d3d; }
    .album-card .info { padding:8px 10px 12px; }
    .album-card .title { font-weight:600; font-size:0.9rem; line-height:1.25;
      display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
    .album-card .artist { color:var(--muted); font-size:0.8rem; margin-top:2px; }
    .release-header { display:flex; gap:16px; margin-bottom:20px; }
    .release-header img { width:120px; height:120px; object-fit:cover; border-radius:10px;
      background:#2a2d3d; flex-shrink:0; }
    .release-header .meta { min-width:0; }
    .release-header .meta h1 { margin:0 0 4px; }
    .release-header .meta .artist { font-size:1rem; color:var(--muted); }
    .release-header .meta .format { font-size:0.85rem; color:var(--muted); margin-top:6px; }
    .badge { display:inline-block; font-size:0.72rem; font-weight:700; padding:3px 8px;
      border-radius:999px; margin-top:6px; }
    .badge.linked { background:rgba(124,155,255,0.18); color:var(--accent); }
    .badge.warn { background:rgba(255,180,84,0.18); color:var(--warn); }
    .badge.edited { background:rgba(87,214,141,0.18); color:var(--good); }
    .side-list { display:flex; flex-direction:column; gap:10px; }
    .side-row { display:flex; align-items:center; justify-content:space-between; background:var(--panel);
      border:1px solid #262939; border-radius:var(--radius); padding:16px 18px; text-decoration:none;
      color:var(--text); transition:transform .12s ease; }
    .side-row:active { transform:scale(0.985); }
    .side-row .side-name { font-size:1.15rem; font-weight:700; }
    .side-row .side-sub { color:var(--muted); font-size:0.85rem; margin-top:2px; }
    .side-row .side-time { font-size:1.3rem; font-weight:700; color:var(--accent); font-variant-numeric:tabular-nums; }
    .side-row .side-time.incomplete { color:var(--warn); }
    .big-time { text-align:center; padding:28px 12px; background:var(--panel); border-radius:var(--radius); margin-bottom:20px; }
    .big-time .number { font-size:3rem; font-weight:800; color:var(--accent); line-height:1; font-variant-numeric:tabular-nums; }
    .big-time .label { color:var(--muted); margin-top:6px; }
    table.tracklist { width:100%; border-collapse:collapse; }
    table.tracklist td { padding:10px 6px; border-bottom:1px solid #262939; font-size:0.95rem; }
    table.tracklist tr:last-child td { border-bottom:none; }
    table.tracklist td.pos { color:var(--muted); width:38px; }
    table.tracklist td.dur { text-align:right; color:var(--muted); white-space:nowrap; font-variant-numeric:tabular-nums; }
    table.tracklist td.dur.edited { color:var(--good); }
    table.tracklist td.edit { width:104px; }
    table.tracklist td.edit input { padding:8px 10px; font-size:0.9rem; text-align:right; }
    .flashes { margin-bottom:16px; }
    .flash { padding:10px 14px; border-radius:10px; margin-bottom:8px; font-size:0.9rem; }
    .flash-success { background:rgba(87,214,141,0.15); color:var(--good); }
    .flash-error { background:rgba(255,107,107,0.15); color:var(--bad); }
    .card { background:var(--panel); border:1px solid #262939; border-radius:var(--radius); padding:16px; margin-bottom:16px; }
    .list-plain { list-style:none; padding:0; margin:0; }
    .list-plain li { display:flex; justify-content:space-between; align-items:center; padding:10px 0;
      border-bottom:1px solid #262939; gap:10px; }
    .list-plain li:last-child { border-bottom:none; }
    .empty-state { text-align:center; padding:40px 16px; color:var(--muted); }
    .version-row { display:flex; gap:12px; align-items:center; padding:12px 0; border-bottom:1px solid #262939; }
    .version-row img { width:56px; height:56px; object-fit:cover; border-radius:8px; background:#2a2d3d; }
    .version-row .vmeta { flex:1; min-width:0; font-size:0.9rem; }
    .version-row .vmeta .vtitle { font-weight:600; }
    .version-row .vmeta .vsub { color:var(--muted); font-size:0.8rem; }
    .stack { display:flex; flex-direction:column; gap:10px; }
    .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
    footer.note { text-align:center; color:var(--muted); font-size:0.8rem; margin-top:32px; }

    /* ---- speed buttons ---- */
    .speed .head { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:10px; }
    .speed .rate { font-weight:700; color:var(--accent); font-variant-numeric:tabular-nums; }
    .speeds { display:grid; grid-template-columns:repeat(4, 1fr); gap:8px; }
    .speeds button { padding:10px 4px; min-height:46px; font-size:0.88rem; width:100%; }
    .speeds button[aria-pressed="true"] { background:var(--accent); color:#0c0d14; border-color:var(--accent); }
    .speeds button:disabled { opacity:.4; pointer-events:none; }
    .pitch { font-size:0.8rem; margin-top:10px; min-height:1.1em; }
    .timer-sub { font-size:0.85rem; margin-top:6px; min-height:1.2em; }
    .timer-sub.live { color:var(--good); font-weight:600; }
    .timer-sub.err { color:var(--bad); }

    /* ---- countdown timer ---- */
    .timer { position:relative; width:220px; height:220px; margin:4px auto 18px; }
    .timer .ring { width:100%; height:100%; transform:rotate(-90deg); }
    .timer .ring circle { fill:none; stroke-width:9; stroke-linecap:round; }
    .timer .ring .ring-bg { stroke:#2a2d3d; }
    .timer .ring .ring-fg { stroke:var(--accent); transition:stroke-dashoffset .25s linear, stroke .2s ease; }
    .timer.running .ring .ring-fg { stroke:var(--good); }
    .timer.done .ring .ring-fg { stroke:var(--warn); }
    .timer .face { position:absolute; inset:0; display:flex; flex-direction:column;
      align-items:center; justify-content:center; text-align:center; padding:0 18px; }
    .timer .face .time { font-size:2.7rem; font-weight:800; line-height:1; font-variant-numeric:tabular-nums; }
    .timer .face .sub { font-size:0.78rem; margin-top:8px; }
    .timer-actions { display:grid; grid-template-columns:1fr; gap:10px; margin-top:16px; }
    .timer-actions.with-reset { grid-template-columns:2fr 1fr; }
    .timer-actions button { width:100%; min-height:52px; font-size:1rem; }
    @media (prefers-reduced-motion: reduce) {
      .timer .ring .ring-fg { transition:none; }
      .album-card, .side-row { transition:none; }
    }

    /* ---- bottom sheet ---- */
    .sheet-backdrop { position:fixed; inset:0; background:rgba(6,7,12,0.66); z-index:50;
      opacity:0; transition:opacity .22s ease; }
    .sheet-backdrop.open { opacity:1; }
    .sheet { position:fixed; left:0; right:0; bottom:0; z-index:60; background:var(--panel);
      border-top:1px solid #30344a; border-radius:22px 22px 0 0; max-height:92vh; max-height:92dvh;
      display:flex; flex-direction:column; transform:translateY(101%);
      transition:transform .3s cubic-bezier(.2,.85,.25,1); box-shadow:0 -18px 50px rgba(0,0,0,0.5); }
    .sheet.open { transform:translateY(0); }
    .sheet .grab { width:44px; height:4px; border-radius:999px; background:#3a3e58; margin:10px auto 2px; flex:none; }
    .sheet-head { display:flex; gap:12px; align-items:center; padding:8px 14px 12px;
      border-bottom:1px solid #262939; flex:none; }
    .sheet-head img { width:48px; height:48px; border-radius:8px; object-fit:cover; background:#2a2d3d; flex:none; }
    .sheet-head .stitle { flex:1; min-width:0; }
    .sheet-head .stitle .t { font-weight:700; font-size:0.98rem; line-height:1.2;
      overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .sheet-head .stitle .a { font-size:0.82rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .icon-btn { background:var(--panel-2); border:1px solid #3a3e58; color:var(--text);
      width:40px; min-height:40px; height:40px; padding:0; border-radius:999px; font-size:1rem; flex:none; }
    .sheet-body { overflow-y:auto; -webkit-overflow-scrolling:touch;
      padding:16px 16px calc(20px + var(--safe-bottom)); }
    .side-buttons { display:flex; flex-direction:column; gap:10px; }
    .side-btn { display:flex; align-items:center; justify-content:space-between; gap:12px;
      width:100%; text-align:left; background:var(--panel-2); color:var(--text);
      border:1px solid #3a3e58; border-radius:var(--radius); padding:14px 16px; min-height:64px; }
    .side-btn .n { font-size:1.1rem; font-weight:700; }
    .side-btn .s { color:var(--muted); font-size:0.82rem; margin-top:2px; font-weight:400; }
    .side-btn .t { font-size:1.2rem; font-weight:700; color:var(--accent); font-variant-numeric:tabular-nums; }
    .side-btn .t.incomplete { color:var(--warn); }
    .sheet details.tracks { margin-top:18px; border-top:1px solid #262939; padding-top:8px; }
    .sheet details.tracks summary { cursor:pointer; padding:10px 0; color:var(--muted); font-size:0.9rem; }
    @media (min-width:640px) {
      .sheet { max-width:520px; margin:0 auto; left:16px; right:16px; bottom:24px;
        border-radius:22px; border:1px solid #30344a; max-height:86vh; max-height:86dvh; }
      .sheet.open { transform:translateY(0); }
    }
    @media (prefers-reduced-motion: reduce) {
      .sheet, .sheet-backdrop { transition:none; }
    }
  </style>
</head>
<body>
  <header class="topbar">
    <a class="brand" href="{{ url_for('index') }}">Side Timer</a>
    <nav>
      {% if session.get('is_admin') %}
        <a href="{{ url_for('admin_index') }}">Admin</a>
        <a href="{{ url_for('admin_logout') }}">Logout</a>
      {% else %}
        <a href="{{ url_for('admin_login') }}">Admin</a>
      {% endif %}
    </nav>
  </header>
  <main class="container">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        <div class="flashes">
          {% for category, message in messages %}
            <div class="flash flash-{{ category }}">{{ message }}</div>
          {% endfor %}
        </div>
      {% endif %}
    {% endwith %}
    {% block content %}{% endblock %}
  </main>
  <script>
  // Shared helpers: time formatting, end-of-side chime, and the countdown
  // engine used by both the bottom sheet and the standalone side page.
  window.ST = (function () {
    var audio = null;

    function pad(n) { return (n < 10 ? '0' : '') + n; }

    function fmt(seconds) {
      seconds = Math.max(0, Math.round(seconds));
      var h = Math.floor(seconds / 3600);
      var m = Math.floor((seconds % 3600) / 60);
      var s = seconds % 60;
      if (h) { return h + ':' + pad(m) + ':' + pad(s); }
      return m + ':' + pad(s);
    }

    function chime() {
      try {
        var Ctx = window.AudioContext || window.webkitAudioContext;
        if (Ctx) {
          audio = audio || new Ctx();
          if (audio.state === 'suspended') { audio.resume(); }
          [0, 0.5, 1.0].forEach(function (offset) {
            var osc = audio.createOscillator();
            var gain = audio.createGain();
            osc.type = 'sine';
            osc.frequency.value = 784;
            osc.connect(gain);
            gain.connect(audio.destination);
            var at = audio.currentTime + offset;
            gain.gain.setValueAtTime(0.0001, at);
            gain.gain.exponentialRampToValueAtTime(0.3, at + 0.02);
            gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.38);
            osc.start(at);
            osc.stop(at + 0.42);
          });
        }
      } catch (e) { /* no audio, no problem */ }
      if (navigator.vibrate) {
        try { navigator.vibrate([250, 120, 250]); } catch (e) {}
      }
    }

    function primeAudio() {
      try {
        var Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) { return; }
        audio = audio || new Ctx();
        if (audio.state === 'suspended') { audio.resume(); }
      } catch (e) {}
    }

    // els: { root, display, ring, play, reset, actions }
    // A countdown that's actually driven by the server: pressing Play,
    // Pause, Resume or Reset makes an API call, and what's on screen is
    // just a display of whatever the server last reported. That way the
    // plug switches off on schedule even if this tab closes, and any
    // other tab or phone open at the same time shows the same thing.
    //
    // There's exactly one physical timer for the one turntable, so
    // "busy" (running or paused) always refers to whatever side was
    // last started from ANY screen - which is why the label always
    // reflects the server's truth rather than whatever's locally armed.
    function createServerTimer(els) {
      var CIRC = 2 * Math.PI * 54;
      if (els.ring) { els.ring.style.strokeDasharray = CIRC; }
      var wakeLock = null;
      var pollHandle = null;
      var rafHandle = null;
      // What Play would start if pressed right now - set via arm().
      var armed = { seconds: 0, label: '' };
      // The server's actual, possibly-unrelated countdown.
      var state = { status: 'idle', total_seconds: 0, remaining_seconds: 0, ends_at: null, label: null, plug_result: null };
      var listeners = [];

      // True only when the server's countdown IS the side armed on this
      // screen. If someone started a different side from another tab,
      // this is false here - Play then means "take over", not "pause".
      function matches() { return !!armed.label && state.label === armed.label && state.status !== 'idle'; }

      function wake(on) {
        try {
          if (on && 'wakeLock' in navigator && !wakeLock) {
            navigator.wakeLock.request('screen').then(function (l) { wakeLock = l; }, function () {});
          } else if (!on && wakeLock) {
            wakeLock.release();
            wakeLock = null;
          }
        } catch (e) {}
      }

      function currentRemaining() {
        var m = matches();
        if (m && state.status === 'running' && state.ends_at) {
          return Math.max(0, state.ends_at - Date.now() / 1000);
        }
        if (m && (state.status === 'paused' || state.status === 'done')) {
          return state.remaining_seconds;
        }
        return armed.seconds;
      }

      function paint() {
        var m = matches();
        var remaining = currentRemaining();
        var basis = (m && state.total_seconds) ? state.total_seconds : (armed.seconds || 1);
        var runningHere = m && state.status === 'running';
        var pausedHere = m && state.status === 'paused';
        var doneHere = m && state.status === 'done';

        els.display.textContent = fmt(remaining);
        if (els.ring) {
          var left = basis > 0 ? Math.max(0, Math.min(1, remaining / basis)) : 0;
          els.ring.style.strokeDashoffset = String(CIRC * (1 - left));
        }
        els.play.textContent = runningHere ? 'Pause' : pausedHere ? 'Resume' : doneHere ? 'Play again' : 'Play';
        els.root.classList.toggle('running', runningHere);
        els.root.classList.toggle('done', doneHere);
        var showReset = pausedHere || doneHere;
        if (els.reset) {
          els.reset.hidden = !showReset;
          if (els.actions) { els.actions.classList.toggle('with-reset', showReset); }
        }
        wake(runningHere);
        listeners.forEach(function (fn) { fn(state, m); });
      }

      function loop() {
        if (!(matches() && state.status === 'running')) { rafHandle = null; return; }
        paint();
        rafHandle = requestAnimationFrame(loop);
      }

      function applyServerState(next) {
        var wasDoneHere = matches() && state.status === 'done';
        state = next;
        var runningHere = matches() && state.status === 'running';
        if (runningHere && !rafHandle) { rafHandle = requestAnimationFrame(loop); }
        if (!runningHere && rafHandle) { cancelAnimationFrame(rafHandle); rafHandle = null; }
        if (!wasDoneHere && matches() && state.status === 'done') { chime(); }
        paint();
      }

      function poll() {
        fetch('/api/timer/status').then(function (r) { return r.json(); })
          .then(applyServerState).catch(function () {});
      }

      function call(url, body) {
        return fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: body ? JSON.stringify(body) : undefined
        }).then(function (r) { return r.json(); }).then(applyServerState);
      }

      var api = {
        // Sets what Play would start. Never touches an in-progress
        // countdown by itself - only calling play()/reset() does that.
        arm: function (seconds, label) {
          armed.seconds = Math.max(0, seconds || 0);
          armed.label = label || '';
          paint();
        },
        play: function () {
          if (matches() && state.status === 'running') { call('/api/timer/pause'); return; }
          if (matches() && state.status === 'paused') { call('/api/timer/resume'); return; }
          if (armed.seconds <= 0) { return; }
          primeAudio();
          call('/api/timer/start', { seconds: armed.seconds, label: armed.label });
        },
        reset: function () {
          if (!matches()) { return; }
          call('/api/timer/reset').then(function () { api.arm(armed.seconds, armed.label); });
        },
        // fn(state, matches) - state.label/status describe whatever the
        // server is actually counting down, which may belong to a
        // different side than the one armed here; matches says which.
        onChange: function (fn) { listeners.push(fn); },
        matches: matches,
        refresh: poll
      };

      els.play.addEventListener('click', api.play);
      if (els.reset) { els.reset.addEventListener('click', api.reset); }
      document.addEventListener('visibilitychange', function () {
        if (!document.hidden) { poll(); }
      });

      poll();
      pollHandle = setInterval(poll, 3000);
      window.addEventListener('pagehide', function () { if (pollHandle) { clearInterval(pollHandle); } });

      return api;
    }

    // Shared wiring for a set of speed buttons. onChange gets the percentage.
    function wireSpeeds(container, onChange) {
      var buttons = Array.prototype.slice.call(container.querySelectorAll('button[data-speed]'));
      var current = 100;
      function select(pct) {
        current = pct;
        buttons.forEach(function (b) {
          b.setAttribute('aria-pressed', String(parseInt(b.dataset.speed, 10) === pct));
        });
        onChange(pct);
      }
      buttons.forEach(function (b) {
        b.addEventListener('click', function () {
          if (b.disabled) { return; }
          select(parseInt(b.dataset.speed, 10));
        });
      });
      return {
        select: select,
        get: function () { return current; },
        setDisabled: function (on) { buttons.forEach(function (b) { b.disabled = !!on; }); }
      };
    }

    function pitchText(pct, hasExtra) {
      if (pct === 100) { return ''; }
      var semis = 12 * Math.log(pct / 100) / Math.log(2);
      return 'Pitched down ' + Math.abs(semis).toFixed(1) + ' semitones.'
        + (hasExtra ? ' The added time per side stays fixed.' : '');
    }

    return { fmt: fmt, chime: chime, createServerTimer: createServerTimer, wireSpeeds: wireSpeeds, pitchText: pitchText };
  })();
  </script>
  {% block sheet %}{% endblock %}
</body>
</html>
"""

SHEET_HTML = """
<div class="sheet-backdrop" id="sheetBackdrop" hidden></div>
<section class="sheet" id="sheet" role="dialog" aria-modal="true" aria-labelledby="sheetTitle" hidden>
  <div class="grab" aria-hidden="true"></div>
  <header class="sheet-head">
    <button type="button" class="icon-btn" id="sheetBack" hidden aria-label="Back to sides">&larr;</button>
    <img id="sheetCover" src="" alt="">
    <div class="stitle">
      <div class="t" id="sheetTitle"></div>
      <div class="a muted" id="sheetArtist"></div>
    </div>
    <button type="button" class="icon-btn" id="sheetClose" aria-label="Close">&#10005;</button>
  </header>
  <div class="sheet-body">
    <div id="sheetLoading" class="empty-state" style="padding:24px;">Loading…</div>

    <div id="stepSides" hidden>
      <div class="side-buttons" id="sideButtons"></div>
      <p class="muted" id="sidesNote" style="margin-top:14px;" hidden></p>
    </div>

    <div id="stepTimer" hidden>
      <div class="timer" id="timerRoot">
        <svg class="ring" viewBox="0 0 120 120" aria-hidden="true">
          <circle class="ring-bg" cx="60" cy="60" r="54"></circle>
          <circle class="ring-fg" id="timerRing" cx="60" cy="60" r="54"></circle>
        </svg>
        <div class="face">
          <div class="time" id="timerDisplay" role="timer" aria-live="off">0:00</div>
          <div class="timer-sub muted" id="timerSub"></div>
        </div>
      </div>
      <div class="speeds" id="speedButtons">
        <button type="button" class="secondary" data-speed="100" aria-pressed="true">As recorded</button>
        <button type="button" class="secondary" data-speed="90">&minus;10%</button>
        <button type="button" class="secondary" data-speed="80">&minus;20%</button>
        <button type="button" class="secondary" data-speed="50">&minus;50%</button>
      </div>
      <p class="muted pitch" id="timerPitch"></p>
      <div class="timer-actions" id="timerActions">
        <button type="button" id="timerPlay">Play</button>
        <button type="button" class="secondary" id="timerReset" hidden>Reset</button>
      </div>
      <details class="tracks">
        <summary id="tracksSummary">Tracklist</summary>
        <table class="tracklist" id="sheetTracks"></table>
      </details>
    </div>
  </div>
</section>
<script>
(function () {
  var sheet = document.getElementById('sheet');
  var backdrop = document.getElementById('sheetBackdrop');
  if (!sheet || !window.fetch) { return; }

  var els = {
    cover: document.getElementById('sheetCover'),
    title: document.getElementById('sheetTitle'),
    artist: document.getElementById('sheetArtist'),
    back: document.getElementById('sheetBack'),
    close: document.getElementById('sheetClose'),
    loading: document.getElementById('sheetLoading'),
    stepSides: document.getElementById('stepSides'),
    stepTimer: document.getElementById('stepTimer'),
    sideButtons: document.getElementById('sideButtons'),
    sidesNote: document.getElementById('sidesNote'),
    sub: document.getElementById('timerSub'),
    pitch: document.getElementById('timerPitch'),
    tracks: document.getElementById('sheetTracks'),
    tracksSummary: document.getElementById('tracksSummary')
  };

  var timer = ST.createServerTimer({
    root: document.getElementById('timerRoot'),
    display: document.getElementById('timerDisplay'),
    ring: document.getElementById('timerRing'),
    play: document.getElementById('timerPlay'),
    reset: document.getElementById('timerReset'),
    actions: document.getElementById('timerActions')
  });

  var cache = {};
  var album = null;
  var side = null;
  var open = false;
  var pushed = false;
  var speeds = ST.wireSpeeds(document.getElementById('speedButtons'), function () { applySpeed(); });

  timer.onChange(function (state, m) {
    if (m && state.status === 'running') {
      els.sub.className = 'timer-sub live';
      els.sub.textContent = 'Plug scheduled off in ' + ST.fmt(state.remaining_seconds) + '.';
    } else if (m && state.status === 'paused') {
      els.sub.className = 'timer-sub muted';
      els.sub.textContent = 'Paused · plug won\\'t switch off until you resume.';
    } else if (m && state.status === 'done') {
      var msg = state.plug_result ? state.plug_result.message : 'Finished.';
      els.sub.className = 'timer-sub ' + (state.plug_result && !state.plug_result.ok ? 'err' : 'muted');
      els.sub.textContent = msg;
    } else if (!m && (state.status === 'running' || state.status === 'paused')) {
      els.sub.className = 'timer-sub muted';
      els.sub.textContent = (state.label || 'Another side') + ' is currently playing. Press Play to take over.';
    } else {
      els.sub.className = 'timer-sub muted';
      els.sub.textContent = side ? sideSubText(side) : '';
    }
    var busyHere = !!m && (state.status === 'running' || state.status === 'paused');
    speeds.setDisabled(busyHere);
    if (busyHere) { els.pitch.textContent = ''; }
  });

  function sideSubText(s) {
    var bits = ['Side ' + s.name];
    if (album.extra_seconds) { bits.push('includes ' + ST.fmt(album.extra_seconds)); }
    if (!s.complete) { bits.push('times incomplete'); }
    return bits.join(' · ');
  }

  function show(step) {
    els.loading.hidden = step !== 'loading';
    els.stepSides.hidden = step !== 'sides';
    els.stepTimer.hidden = step !== 'timer';
    els.back.hidden = step !== 'timer' || !album || album.sides.length < 2;
  }

  function openSheet() {
    if (open) { return; }
    open = true;
    document.body.classList.add('sheet-open');
    backdrop.hidden = false;
    sheet.hidden = false;
    requestAnimationFrame(function () {
      backdrop.classList.add('open');
      sheet.classList.add('open');
    });
    if (!pushed) {
      try { history.pushState({ sheet: true }, ''); pushed = true; } catch (e) {}
    }
  }

  function closeSheet(fromPop) {
    if (!open) { return; }
    open = false;
    // Deliberately NOT stopping the timer here - it's running on the
    // server and should keep counting down (and switch off the plug on
    // schedule) whether or not this sheet is open to watch it.
    sheet.classList.remove('open');
    backdrop.classList.remove('open');
    document.body.classList.remove('sheet-open');
    setTimeout(function () {
      if (!open) { sheet.hidden = true; backdrop.hidden = true; }
    }, 320);
    if (pushed && !fromPop) {
      pushed = false;
      try { history.back(); } catch (e) {}
    } else if (fromPop) {
      pushed = false;
    }
  }

  function renderSides() {
    els.sideButtons.innerHTML = '';
    album.sides.forEach(function (s) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'side-btn';
      var left = document.createElement('div');
      var n = document.createElement('div');
      n.className = 'n';
      n.textContent = 'Side ' + s.name;
      var sub = document.createElement('div');
      sub.className = 's';
      sub.textContent = s.track_count + (s.track_count === 1 ? ' track' : ' tracks');
      left.appendChild(n);
      left.appendChild(sub);
      var t = document.createElement('div');
      t.className = 't' + (s.complete ? '' : ' incomplete');
      t.textContent = s.total_display || 'no data';
      b.appendChild(left);
      b.appendChild(t);
      b.addEventListener('click', function () { pickSide(s); });
      els.sideButtons.appendChild(b);
    });
    var incomplete = album.sides.some(function (s) { return !s.complete; });
    els.sidesNote.hidden = !incomplete;
    els.sidesNote.textContent = incomplete
      ? 'Some track times are missing on this pressing, so those totals are short.' : '';
    show('sides');
  }

  function applySpeed() {
    if (!side) { return; }
    var pct = speeds.get();
    var factor = pct / 100;
    var music = 0;
    var rows = '';
    side.tracks.forEach(function (t) {
      var shown = '—';
      if (t.seconds != null) {
        music += t.seconds / factor;
        shown = ST.fmt(t.seconds / factor);
      }
      rows += '<tr><td class="pos">' + (t.position || '') + '</td><td>' + esc(t.title)
        + '</td><td class="dur' + (t.overridden ? ' edited' : '') + '">' + shown + '</td></tr>';
    });
    els.tracks.innerHTML = rows;
    var total = music + album.extra_seconds;
    var label = (album.title || 'Album') + ' \\u00b7 Side ' + side.name;
    timer.arm(total, label);
    if (!timer.matches()) { els.pitch.textContent = ST.pitchText(pct, album.extra_seconds > 0); }
    els.tracksSummary.textContent = 'Tracklist (' + side.track_count + ')';
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function pickSide(s) {
    side = s;
    speeds.select(100);
    show('timer');
    sheet.querySelector('.sheet-body').scrollTop = 0;
  }

  function load(releaseId, sideName) {
    album = null;
    side = null;
    show('loading');
    els.title.textContent = '';
    els.artist.textContent = '';
    openSheet();

    var done = function (data) {
      album = data;
      els.title.textContent = data.title || '';
      els.artist.textContent = data.artists || '';
      els.cover.src = data.cover || '';
      if (!data.sides.length) {
        els.loading.textContent = 'No track times for this release yet.';
        show('loading');
        return;
      }
      var wanted = sideName && data.sides.filter(function (s) { return s.name === sideName; })[0];
      if (wanted) { pickSide(wanted); }
      else if (data.sides.length === 1) { pickSide(data.sides[0]); }
      else { renderSides(); }
    };

    if (cache[releaseId]) { done(cache[releaseId]); return; }
    fetch('/api/release/' + releaseId, { headers: { 'Accept': 'application/json' } })
      .then(function (r) { if (!r.ok) { throw new Error('http ' + r.status); } return r.json(); })
      .then(function (data) { cache[releaseId] = data; done(data); })
      .catch(function () {
        els.loading.textContent = 'Couldn\\'t load this album. Tap the title to open the full page.';
        show('loading');
      });
  }

  els.close.addEventListener('click', function () { closeSheet(); });
  backdrop.addEventListener('click', function () { closeSheet(); });
  els.back.addEventListener('click', renderSides);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && open) { closeSheet(); }
  });
  window.addEventListener('popstate', function () {
    if (open) { closeSheet(true); }
  });

  // Any link carrying data-release opens the sheet instead of navigating.
  document.addEventListener('click', function (e) {
    var trigger = e.target.closest('[data-release]');
    if (!trigger) { return; }
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) { return; }
    e.preventDefault();
    load(trigger.dataset.release, trigger.dataset.side || null);
  });
})();
</script>
"""

INDEX_HTML = """
{% extends "base.html" %}
{% block title %}Your Collection{% endblock %}
{% block content %}
<form class="search-form" method="get" action="{{ url_for('index') }}">
  <input type="text" name="q" placeholder="Search artist or title…" value="{{ q }}">
  <button type="submit">Search</button>
</form>
{% if not has_any_collection %}
  <div class="empty-state">
    <p>No collection loaded yet.</p>
    {% if session.get('is_admin') %}
      <form method="post" action="{{ url_for('sync_now') }}"><button type="submit">Sync from Discogs now</button></form>
    {% else %}
      <p class="muted">Log in as <a href="{{ url_for('admin_login') }}">admin</a> to run the first sync.</p>
    {% endif %}
  </div>
{% elif releases|length == 0 %}
  <div class="empty-state"><p>No albums match "{{ q }}".</p></div>
{% else %}
  <div class="album-grid">
    {% for r in releases %}
      <a class="album-card" href="{{ url_for('release_detail', release_id=r.id) }}" data-release="{{ r.id }}">
        <img src="{{ r.cover_url or r.thumb_url }}" alt="" loading="lazy">
        <div class="info">
          <div class="title">{{ r.title }}</div>
          <div class="artist">{{ r.artists }}</div>
        </div>
      </a>
    {% endfor %}
  </div>
{% endif %}
<footer class="note">{{ releases|length if releases else 0 }} album(s) shown</footer>
{% endblock %}
{% block sheet %}{% include "_sheet.html" %}{% endblock %}
"""

RELEASE_HTML = """
{% extends "base.html" %}
{% block title %}{{ release.title }}{% endblock %}
{% block content %}
<div class="release-header">
  <img src="{{ release.cover_url or release.thumb_url }}" alt="">
  <div class="meta">
    <h1>{{ release.title }}</h1>
    <div class="artist">{{ release.artists }}{% if release.year %} · {{ release.year }}{% endif %}</div>
    {% if release.formats_desc %}<div class="format">{{ release.formats_desc }}</div>{% endif %}
    {% if release.is_using_linked_pressing() %}<span class="badge linked">Using timing from a linked pressing</span>{% endif %}
    {% if release.override_count() %}<span class="badge edited">{{ release.override_count() }} time(s) entered by hand</span>{% endif %}
  </div>
</div>
{% if not release.has_details() %}
  <div class="empty-state"><p>Couldn't load track data for this release from Discogs.</p></div>
{% else %}
  <h2>Choose a side</h2>
  <div class="side-list">
    {% for s in sides %}
      <a class="side-row" href="{{ url_for('side_detail', release_id=release.id, side_name=s.name) }}"
          data-release="{{ release.id }}" data-side="{{ s.name }}">
        <div>
          <div class="side-name">Side {{ s.name }}</div>
          <div class="side-sub">{{ s.track_count }} track{{ 's' if s.track_count != 1 else '' }}</div>
        </div>
        <div class="side-time {{ '' if s.complete else 'incomplete' }}">
          {% if s.total_display %}{{ s.total_display }}{% if not s.complete %}*{% endif %}{% else %}<span class="muted" style="font-size:0.85rem;">no data</span>{% endif %}
        </div>
      </a>
    {% endfor %}
  </div>
  {% if extra_seconds %}
    <p class="muted" style="margin-top:12px;">Each side includes the {{ extra_display }} you've added for lead-in and flipping.</p>
  {% endif %}
  {% if sides|selectattr('complete', 'equalto', false)|list %}
    <p class="muted" style="margin-top:12px;">* some durations are missing on this pressing for that side.
      {% if session.get('is_admin') %}<a href="{{ url_for('admin_release', release_id=release.id) }}">Link a different pressing or type the times in</a> in the admin panel to fix this.{% endif %}
    </p>
  {% endif %}
{% endif %}
<p style="margin-top:20px;"><a href="{{ release.discogs_url }}" target="_blank" rel="noopener">View on Discogs ↗</a></p>
{% endblock %}
{% block sheet %}{% include "_sheet.html" %}{% endblock %}
"""

SIDE_HTML = """
{% extends "base.html" %}
{% block title %}Side {{ side_name }} – {{ release.title }}{% endblock %}
{% block content %}
<p><a href="{{ url_for('release_detail', release_id=release.id) }}">← {{ release.title }}</a></p>
{% if total_display %}
  <div class="card" id="pgCard">
    <div class="timer" id="pgTimer">
      <svg class="ring" viewBox="0 0 120 120" aria-hidden="true">
        <circle class="ring-bg" cx="60" cy="60" r="54"></circle>
        <circle class="ring-fg" id="pgRing" cx="60" cy="60" r="54"></circle>
      </svg>
      <div class="face">
        <div class="time" id="pgDisplay">{{ total_display }}</div>
        <div class="timer-sub muted" id="pgSub">Side {{ side_name }}</div>
      </div>
    </div>
    <div class="speeds" id="pgSpeeds">
      <button type="button" class="secondary" data-speed="100" aria-pressed="true">As recorded</button>
      <button type="button" class="secondary" data-speed="90">&minus;10%</button>
      <button type="button" class="secondary" data-speed="80">&minus;20%</button>
      <button type="button" class="secondary" data-speed="50">&minus;50%</button>
    </div>
    <p class="muted pitch" id="pgPitch"></p>
    <div class="timer-actions" id="pgActions">
      <button type="button" id="pgPlay">Play</button>
      <button type="button" class="secondary" id="pgReset" hidden>Reset</button>
    </div>
  </div>
{% else %}
  <div class="big-time">
    <div class="number" style="font-size:1.6rem; color:var(--muted);">No duration data</div>
    <div class="label">This pressing doesn't list track times for side {{ side_name }}</div>
  </div>
{% endif %}
{% if using_linked %}
  <p class="badge linked">Times calculated from linked pressing: {{ timing_source.title }}{% if timing_source.formats_desc %} ({{ timing_source.formats_desc }}){% endif %}</p>
{% endif %}
<table class="tracklist">
  {% for t in tracks %}
    <tr>
      <td class="pos">{{ t.position }}</td>
      <td>{{ t.title }}</td>
      <td class="dur {{ 'edited' if t.overridden else '' }}"
          {% if t.seconds %}data-seconds="{{ t.seconds }}"{% endif %}>{{ t.duration if t.duration else '—' }}</td>
    </tr>
  {% endfor %}
</table>
{% if has_overrides %}<p class="muted" style="margin-top:10px;">Times in green were entered by hand.</p>{% endif %}
{% if session.get('is_admin') %}
  <p style="margin-top:20px;"><a href="{{ url_for('admin_release', release_id=release.id) }}">Manage timing for this album →</a></p>
{% endif %}
{% if total_display %}
<script>
(function () {
  var cells = Array.prototype.slice.call(document.querySelectorAll('td.dur[data-seconds]'));
  var extra = Number("{{ extra_seconds }}");
  var complete = {{ 'true' if complete else 'false' }};
  var releaseTitle = {{ release.title|tojson }};
  var sideName = "{{ side_name }}";
  var sub = document.getElementById('pgSub');
  var pitch = document.getElementById('pgPitch');

  function localSub() {
    var bits = ['Side ' + sideName];
    if (extra) { bits.push('includes ' + ST.fmt(extra)); }
    if (!complete) { bits.push('times incomplete'); }
    return bits.join(' · ');
  }

  var timer = ST.createServerTimer({
    root: document.getElementById('pgTimer'),
    display: document.getElementById('pgDisplay'),
    ring: document.getElementById('pgRing'),
    play: document.getElementById('pgPlay'),
    reset: document.getElementById('pgReset'),
    actions: document.getElementById('pgActions')
  });

  timer.onChange(function (state, m) {
    if (m && state.status === 'running') {
      sub.className = 'timer-sub live';
      sub.textContent = 'Plug scheduled off in ' + ST.fmt(state.remaining_seconds) + '.';
    } else if (m && state.status === 'paused') {
      sub.className = 'timer-sub muted';
      sub.textContent = 'Paused · plug won\\'t switch off until you resume.';
    } else if (m && state.status === 'done') {
      var msg = state.plug_result ? state.plug_result.message : 'Finished.';
      sub.className = 'timer-sub ' + (state.plug_result && !state.plug_result.ok ? 'err' : 'muted');
      sub.textContent = msg;
    } else if (!m && (state.status === 'running' || state.status === 'paused')) {
      sub.className = 'timer-sub muted';
      sub.textContent = (state.label || 'Another side') + ' is currently playing. Press Play to take over.';
    } else {
      sub.className = 'timer-sub muted';
      sub.textContent = localSub();
    }
    var busyHere = !!m && (state.status === 'running' || state.status === 'paused');
    speeds.setDisabled(busyHere);
    if (busyHere) { pitch.textContent = ''; }
  });

  var speeds = ST.wireSpeeds(document.getElementById('pgSpeeds'), function (pct) {
    var factor = pct / 100;
    var music = 0;
    cells.forEach(function (cell) {
      var base = parseFloat(cell.getAttribute('data-seconds'));
      if (isNaN(base)) { return; }
      music += base / factor;
      cell.textContent = ST.fmt(base / factor);
    });
    var total = music + extra;
    var label = releaseTitle + ' \\u00b7 Side ' + sideName;
    timer.arm(total, label);
    if (!timer.matches()) { pitch.textContent = ST.pitchText(pct, extra > 0); }
  });

  timer.arm(Number("{{ total_seconds or 0 }}"), releaseTitle + ' \\u00b7 Side ' + sideName);
})();
</script>
{% endif %}
{% endblock %}
"""

LOGIN_HTML = """
{% extends "base.html" %}
{% block title %}Admin login{% endblock %}
{% block content %}
<h1>Admin login</h1>
<form method="post" class="stack" style="max-width:320px;">
  <input type="password" name="password" placeholder="Admin password" autofocus>
  <button type="submit">Log in</button>
</form>
{% endblock %}
"""

NOT_FOUND_HTML = """
{% extends "base.html" %}
{% block title %}Not found{% endblock %}
{% block content %}
<div class="empty-state"><p>Not found.</p><a class="btn" href="{{ url_for('index') }}">Back home</a></div>
{% endblock %}
"""

ADMIN_INDEX_HTML = """
{% extends "base.html" %}
{% block title %}Admin{% endblock %}
{% block content %}
<h1>Admin</h1>
{% if not configured %}
  <div class="card"><strong>Discogs credentials aren't set.</strong>
    <p class="muted">Set DISCOGS_USERNAME and DISCOGS_TOKEN environment variables and restart the app before syncing.</p></div>
{% endif %}
<div class="card row" style="justify-content:space-between;">
  <div><strong>{{ total }}</strong> album(s) in your synced collection.</div>
  <form method="post" action="{{ url_for('sync_now') }}"><button type="submit">Sync collection from Discogs</button></form>
</div>
<div class="card">
  <h2 style="margin-top:0;">Extra time per side</h2>
  <p class="muted">Added to every side total, everywhere in the app — lead-in groove, gaps between tracks,
    and the walk back to the turntable. Enter seconds (<code>20</code>) or minutes and seconds (<code>1:30</code>).
    Set it to 0 to switch it off.</p>
  <form method="post" action="{{ url_for('admin_settings') }}" class="row">
    <input type="text" name="extra_per_side" value="{{ extra_display }}" style="max-width:140px;" placeholder="0:20">
    <button type="submit">Save</button>
  </form>
  <p class="muted" style="margin-bottom:0;">Currently adding {{ extra_seconds }} second(s) to each side.</p>
</div>
<div class="card">
  <h2 style="margin-top:0;">Smart plug</h2>
  {% if plug_configured %}
    <p class="muted">Configured, device <code>{{ plug_device_id }}</code>. When a side's countdown reaches
      zero, this plug is switched off — from the server itself, so it happens even if no phone has the app open.</p>
  {% else %}
    <p class="muted"><strong>Not fully configured.</strong> Set TUYA_ACCESS_ID, TUYA_ACCESS_SECRET, TUYA_DEVICE_ID
      and TUYA_ENDPOINT (see the docstring at the top of app.py) and restart the app.</p>
  {% endif %}
  {% if timer_snapshot.status in ('running', 'paused') %}
    <p class="muted">Timer right now: <strong>{{ timer_snapshot.status }}</strong> — {{ timer_snapshot.label }},
      {{ (timer_snapshot.remaining_seconds // 60)|int }}:{{ '%02d'|format((timer_snapshot.remaining_seconds % 60)|int) }} left.</p>
  {% elif timer_snapshot.status == 'done' and timer_snapshot.plug_result %}
    <p class="{{ 'muted' if timer_snapshot.plug_result.ok else 'flash flash-error' }}">
      Last side ({{ timer_snapshot.label }}) finished — {{ timer_snapshot.plug_result.message }}</p>
  {% endif %}
  <form method="post" action="{{ url_for('admin_plug_test') }}">
    <button type="submit" class="secondary">Turn plug off now (test)</button>
  </form>
</div>
<h2>Missing track data ({{ missing_details|length }})</h2>
{% if missing_details %}
  <ul class="list-plain">
    {% for r in missing_details %}
      <li><span>{{ r.artists }} – {{ r.title }}</span><a class="btn secondary" href="{{ url_for('admin_release', release_id=r.id) }}">Fix</a></li>
    {% endfor %}
  </ul>
{% else %}<p class="muted">All synced releases have track data loaded.</p>{% endif %}
<h2>Incomplete durations ({{ incomplete_timing|length }})</h2>
<p class="muted">These have track data, but at least one side is missing timing for one or more tracks — link a better
  pressing, or type the missing times in by hand.</p>
{% if incomplete_timing %}
  <ul class="list-plain">
    {% for r in incomplete_timing %}
      <li><span>{{ r.artists }} – {{ r.title }}{% if r.link %} <span class="badge linked">linked</span>{% endif %}</span>
        <a class="btn secondary" href="{{ url_for('admin_release', release_id=r.id) }}">Manage</a></li>
    {% endfor %}
  </ul>
{% else %}<p class="muted">Nothing incomplete.</p>{% endif %}
<h2>Linked pressings ({{ linked|length }})</h2>
{% if linked %}
  <ul class="list-plain">
    {% for r in linked %}
      <li><span>{{ r.artists }} – {{ r.title }} → <span class="muted">{{ r.link.target_release.title }}</span></span>
        <a class="btn secondary" href="{{ url_for('admin_release', release_id=r.id) }}">Manage</a></li>
    {% endfor %}
  </ul>
{% else %}<p class="muted">No releases are currently linked to an alternate pressing.</p>{% endif %}
<h2>Hand-entered times ({{ overridden|length }})</h2>
{% if overridden %}
  <ul class="list-plain">
    {% for r in overridden %}
      <li><span>{{ r.artists }} – {{ r.title }} <span class="badge edited">{{ r.override_count() }}</span></span>
        <a class="btn secondary" href="{{ url_for('admin_release', release_id=r.id) }}">Manage</a></li>
    {% endfor %}
  </ul>
{% else %}<p class="muted">No manual durations saved yet.</p>{% endif %}
{% endblock %}
"""

ADMIN_RELEASE_HTML = """
{% extends "base.html" %}
{% block title %}Admin – {{ release.title }}{% endblock %}
{% block content %}
<p><a href="{{ url_for('admin_index') }}">← Admin</a></p>
<div class="release-header">
  <img src="{{ release.cover_url or release.thumb_url }}" alt="">
  <div class="meta">
    <h1>{{ release.title }}</h1>
    <div class="artist">{{ release.artists }}{% if release.year %} · {{ release.year }}{% endif %}</div>
    <div class="format">{{ release.formats_desc }}</div>
    <div class="muted" style="font-size:0.8rem; margin-top:4px;">Discogs release ID: {{ release.discogs_id }}{% if release.master_id %} · master ID: {{ release.master_id }}{% endif %}</div>
  </div>
</div>
{% if not release.has_details() %}
  <div class="card"><p>No track data fetched yet for this pressing.</p>
    <form method="post" action="{{ url_for('admin_fetch_details', release_id=release.id) }}"><button type="submit">Fetch track data from Discogs</button></form>
  </div>
{% else %}
  <h2>Your pressing — side timing</h2>
  {% if extra_seconds %}<p class="muted">Totals include the {{ extra_display }} added to every side.</p>{% endif %}
  <ul class="list-plain">
    {% for s in summary %}
      <li><span>Side {{ s.name }} ({{ s.track_count }} tracks)</span>
        <span class="{{ '' if s.complete else 'badge warn' }}">{{ s.total_display or 'no data' }}{% if not s.complete %} incomplete{% endif %}</span></li>
    {% endfor %}
  </ul>
  <form method="post" action="{{ url_for('admin_fetch_details', release_id=release.id) }}"><button type="submit" class="secondary">Re-fetch track data</button></form>

  <h2>Track durations</h2>
  <p class="muted">Anything you type here wins over the Discogs data, whether that's a missing time or one
    that's simply wrong. Enter <code>3:45</code>, <code>1:02:30</code> or plain seconds. Clear a box to go back
    to the Discogs value.</p>
  <form method="post" action="{{ url_for('admin_save_overrides', release_id=release.id) }}">
    <table class="tracklist">
      {% for t in tracks %}
        <tr>
          <td class="pos">{{ t.position }}</td>
          <td>{{ t.title }}</td>
          <td class="dur">{{ t.original or '—' }}</td>
          <td class="edit"><input type="text" inputmode="numeric" name="dur__{{ t.key }}"
              value="{{ t.override }}" placeholder="{{ t.original or 'm:ss' }}"
              aria-label="Duration for {{ t.position }} {{ t.title }}"></td>
        </tr>
      {% endfor %}
    </table>
    <div class="row" style="margin-top:14px;"><button type="submit">Save durations</button></div>
  </form>
  {% if release.override_count() %}
    <form method="post" action="{{ url_for('admin_clear_overrides', release_id=release.id) }}" style="margin-top:10px;">
      <button type="submit" class="danger">Clear all {{ release.override_count() }} hand-entered time(s)</button>
    </form>
  {% endif %}
{% endif %}
<h2>Pressing link</h2>
{% if release.link %}
  <div class="card">
    <p>Timing is currently pulled from a linked pressing:</p>
    <div class="version-row" style="border:none;">
      <img src="{{ release.link.target_release.cover_url or release.link.target_release.thumb_url }}" alt="">
      <div class="vmeta"><div class="vtitle">{{ release.link.target_release.title }}</div>
        <div class="vsub">{{ release.link.target_release.formats_desc }} · Discogs ID {{ release.link.target_release.discogs_id }}</div></div>
    </div>
    <p class="muted">Your collection still shows as "{{ release.title }}" everywhere — only the time calculation uses this other pressing.</p>
    <form method="post" action="{{ url_for('admin_unlink', release_id=release.id) }}"><button type="submit" class="danger">Remove link (use this release's own data)</button></form>
  </div>
{% else %}
  <p class="muted">Not linked to another pressing — times are calculated from this release's own Discogs data.</p>
{% endif %}
<div class="card">
  <h2 style="margin-top:0;">Link to a different pressing</h2>
  <p class="muted">If your copy is missing track times, find another pressing of the same album on Discogs that has them, and link it here.</p>
  <div class="row"><a class="btn secondary" href="{{ url_for('admin_find_versions', release_id=release.id) }}">Browse other pressings of this album</a></div>
  <form method="get" action="{{ url_for('admin_manual_search', release_id=release.id) }}" class="row" style="margin-top:16px;">
    <input type="text" name="q" placeholder="Or search Discogs manually (artist - title)"><button type="submit" class="secondary">Search</button>
  </form>
  <form method="post" action="{{ url_for('admin_set_link', release_id=release.id) }}" class="row" style="margin-top:16px;">
    <input type="number" name="target_discogs_id" placeholder="Discogs release ID"><button type="submit">Link by ID</button>
  </form>
</div>
{% endblock %}
"""

ADMIN_SEARCH_HTML = """
{% extends "base.html" %}
{% block title %}Find a pressing – {{ release.title }}{% endblock %}
{% block content %}
<p><a href="{{ url_for('admin_release', release_id=release.id) }}">← {{ release.title }}</a></p>
<h1>{{ 'Search results' if manual else 'Other pressings' }}</h1>
<p class="muted">Pick the pressing you want to use for timing data on <strong>{{ release.title }}</strong>.</p>
{% if error %}<div class="flash flash-error">{{ error }}</div>{% endif %}
{% if not manual %}
  {% for v in versions %}
    <div class="version-row">
      <img src="{{ v.thumb }}" alt="">
      <div class="vmeta"><div class="vtitle">{{ v.title }} ({{ v.released or v.year }})</div>
        <div class="vsub">{{ v.format }} · {{ v.country }} · ID {{ v.id }}</div></div>
      <form method="post" action="{{ url_for('admin_set_link', release_id=release.id) }}">
        <input type="hidden" name="target_discogs_id" value="{{ v.id }}"><button type="submit" class="secondary">Use this</button>
      </form>
    </div>
  {% else %}{% if not error %}<p class="muted">No other versions found.</p>{% endif %}{% endfor %}
{% else %}
  {% for r in results %}
    <div class="version-row">
      <img src="{{ r.thumb }}" alt="">
      <div class="vmeta"><div class="vtitle">{{ r.title }}</div>
        <div class="vsub">{{ r.format|join(', ') if r.format else '' }} · {{ r.country }} · {{ r.year }} · ID {{ r.id }}</div></div>
      <form method="post" action="{{ url_for('admin_set_link', release_id=release.id) }}">
        <input type="hidden" name="target_discogs_id" value="{{ r.id }}"><button type="submit" class="secondary">Use this</button>
      </form>
    </div>
  {% else %}{% if q %}<p class="muted">No results for "{{ q }}".</p>{% endif %}{% endfor %}
{% endif %}
{% endblock %}
"""

TEMPLATES = {
    "base.html": BASE_HTML,
    "_sheet.html": SHEET_HTML,
    "index.html": INDEX_HTML,
    "release.html": RELEASE_HTML,
    "side.html": SIDE_HTML,
    "login.html": LOGIN_HTML,
    "404.html": NOT_FOUND_HTML,
    "admin/index.html": ADMIN_INDEX_HTML,
    "admin/release.html": ADMIN_RELEASE_HTML,
    "admin/search_versions.html": ADMIN_SEARCH_HTML,
}


# ============================================================================
# App factory + routes
# ============================================================================

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    app.jinja_loader = DictLoader(TEMPLATES)
    db.init_app(app)

    with app.app_context():
        db.create_all()

    register_routes(app)
    return app


def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def register_routes(app):

    @app.route("/")
    def index():
        q = request.args.get("q", "").strip()
        query = Release.query.filter_by(in_collection=True)
        if q:
            like = f"%{q}%"
            query = query.filter(db.or_(Release.title.ilike(like), Release.artists.ilike(like)))
        releases = query.order_by(Release.artists, Release.title).all()
        has_any_collection = Release.query.filter_by(in_collection=True).first() is not None
        return render_template("index.html", releases=releases, q=q, has_any_collection=has_any_collection)

    @app.route("/release/<int:release_id>")
    def release_detail(release_id):
        release = Release.query.get_or_404(release_id)
        if not release.has_details():
            try:
                fetch_release_details(app, release)
            except DiscogsError as e:
                flash(str(e), "error")
            except Exception as e:
                flash(f"Couldn't fetch track data from Discogs: {e}", "error")
        extra = get_extra_seconds()
        sides = release.side_summary(extra_seconds=extra)
        return render_template("release.html", release=release, sides=sides,
                                extra_seconds=extra, extra_display=format_seconds(extra))

    @app.route("/release/<int:release_id>/side/<side_name>")
    def side_detail(release_id, side_name):
        release = Release.query.get_or_404(release_id)
        all_sides = dict(release.sides())
        tracks = all_sides.get(side_name)
        if tracks is None:
            flash("That side couldn't be found for this release.", "error")
            return redirect(url_for("release_detail", release_id=release.id))

        extra = get_extra_seconds()
        music_seconds, complete = sum_durations(tracks)
        total_seconds = None if music_seconds is None else music_seconds + extra

        track_rows = []
        has_overrides = False
        for t in tracks:
            if t.get("type_", "track") != "track":
                continue
            secs = parse_duration_to_seconds(t.get("duration"))
            if t.get("overridden"):
                has_overrides = True
            track_rows.append({"position": t.get("position"), "title": t.get("title"),
                                "duration": t.get("duration") or None, "seconds": secs,
                                "overridden": bool(t.get("overridden"))})

        return render_template(
            "side.html", release=release, side_name=side_name, tracks=track_rows,
            music_seconds=music_seconds, total_seconds=total_seconds,
            total_display=format_seconds(total_seconds) if total_seconds else None,
            extra_seconds=extra, extra_display=format_seconds(extra),
            complete=complete, has_overrides=has_overrides,
            using_linked=release.is_using_linked_pressing(),
            timing_source=release.timing_source(),
        )

    @app.route("/api/release/<int:release_id>")
    def api_release(release_id):
        """Everything the bottom sheet needs to draw the side buttons, the
        tracklist and the countdown, in one request."""
        release = Release.query.get_or_404(release_id)
        if not release.has_details():
            try:
                fetch_release_details(app, release)
            except Exception:
                pass
        extra = get_extra_seconds()
        sides = []
        for name, tracks in release.sides():
            music, complete = sum_durations(tracks)
            rows = []
            for t in tracks:
                if t.get("type_", "track") != "track":
                    continue
                rows.append({
                    "position": t.get("position") or "",
                    "title": t.get("title") or "",
                    "duration": t.get("duration") or None,
                    "seconds": parse_duration_to_seconds(t.get("duration")),
                    "overridden": bool(t.get("overridden")),
                })
            sides.append({
                "name": name,
                "track_count": len(rows),
                "music_seconds": music,
                "total_seconds": None if music is None else music + extra,
                "total_display": format_seconds(music + extra) if music is not None else None,
                "complete": complete,
                "tracks": rows,
                "url": url_for("side_detail", release_id=release.id, side_name=name),
            })
        return jsonify({
            "id": release.id,
            "title": release.title,
            "artists": release.artists,
            "cover": release.cover_url or release.thumb_url or "",
            "formats": release.formats_desc,
            "linked": release.is_using_linked_pressing(),
            "extra_seconds": extra,
            "sides": sides,
        })

    @app.route("/api/timer/status")
    def api_timer_status():
        return jsonify(server_timer.snapshot())

    @app.route("/api/timer/start", methods=["POST"])
    def api_timer_start():
        data = request.get_json(silent=True) or {}
        seconds = data.get("seconds")
        label = (data.get("label") or "").strip()[:200] or "Side timer"
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            seconds = None
        if not seconds or seconds <= 0:
            return jsonify({"error": "seconds must be a positive number"}), 400
        server_timer.start(app, seconds, label)
        return jsonify(server_timer.snapshot())

    @app.route("/api/timer/pause", methods=["POST"])
    def api_timer_pause():
        server_timer.pause()
        return jsonify(server_timer.snapshot())

    @app.route("/api/timer/resume", methods=["POST"])
    def api_timer_resume():
        server_timer.resume(app)
        return jsonify(server_timer.snapshot())

    @app.route("/api/timer/reset", methods=["POST"])
    def api_timer_reset():
        server_timer.reset()
        return jsonify(server_timer.snapshot())

    @app.route("/sync", methods=["POST"])
    @admin_required
    def sync_now():
        try:
            result = sync_collection(app)
            flash(f"Synced collection: {result['new']} new, {result['updated']} updated "
                  f"({result['total_seen']} total items).", "success")
        except DiscogsError as e:
            flash(str(e), "error")
        except Exception as e:
            flash(f"Sync failed: {e}", "error")
        return redirect(request.referrer or url_for("index"))

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            configured = app.config["ADMIN_PASSWORD"]
            password = request.form.get("password", "")
            if password and configured and password == configured:
                session["is_admin"] = True
                flash("Logged in.", "success")
                return redirect(request.args.get("next") or url_for("admin_index"))
            else:
                flash("Wrong password.", "error")
        return render_template("login.html")

    @app.route("/admin/logout")
    def admin_logout():
        session.pop("is_admin", None)
        return redirect(url_for("index"))

    @app.route("/admin/")
    @admin_required
    def admin_index():
        extra = get_extra_seconds()
        releases = Release.query.filter_by(in_collection=True).order_by(Release.artists, Release.title).all()
        missing_details, incomplete_timing, linked, overridden = [], [], [], []
        for r in releases:
            if r.override_count():
                overridden.append(r)
            if not r.has_details():
                missing_details.append(r)
                continue
            if r.link:
                linked.append(r)
            summary = r.side_summary(extra_seconds=extra)
            if any(not s["complete"] for s in summary):
                incomplete_timing.append(r)
        return render_template("admin/index.html", total=len(releases), missing_details=missing_details,
                                incomplete_timing=incomplete_timing, linked=linked, overridden=overridden,
                                extra_seconds=extra, extra_display=format_seconds(extra),
                                configured=bool(app.config["DISCOGS_USERNAME"] and app.config["DISCOGS_TOKEN"]),
                                plug_configured=tuya_configured(app), plug_device_id=app.config["TUYA_DEVICE_ID"],
                                timer_snapshot=server_timer.snapshot())

    @app.route("/admin/plug/test", methods=["POST"])
    @admin_required
    def admin_plug_test():
        result = turn_off_plug(app)
        flash(result["message"], "success" if result["ok"] else "error")
        return redirect(url_for("admin_index"))

    @app.route("/admin/settings", methods=["POST"])
    @admin_required
    def admin_settings():
        raw = (request.form.get("extra_per_side") or "").strip()
        secs = 0 if raw in ("", "0") else parse_duration_to_seconds(raw)
        if secs is None:
            flash(f"Couldn't read \"{raw}\" as a time. Use seconds (20) or m:ss (1:30).", "error")
        else:
            set_setting(EXTRA_PER_SIDE_KEY, secs)
            if secs:
                flash(f"Adding {format_seconds(secs)} to every side.", "success")
            else:
                flash("Extra time per side switched off.", "success")
        return redirect(url_for("admin_index"))

    @app.route("/admin/release/<int:release_id>")
    @admin_required
    def admin_release(release_id):
        release = Release.query.get_or_404(release_id)
        extra = get_extra_seconds()
        summary = release.side_summary(extra_seconds=extra) if release.has_details() else []
        tracks = editable_tracks(release) if release.has_details() else []
        return render_template("admin/release.html", release=release, summary=summary, tracks=tracks,
                                extra_seconds=extra, extra_display=format_seconds(extra))

    @app.route("/admin/release/<int:release_id>/durations", methods=["POST"])
    @admin_required
    def admin_save_overrides(release_id):
        release = Release.query.get_or_404(release_id)
        result = save_overrides(release, request.form)
        if result["bad"]:
            flash("Couldn't read a time for: " + ", ".join(result["bad"]) + ". Use m:ss or plain seconds.", "error")
        if result["saved"] or result["cleared"]:
            flash(f"Saved {result['saved']} duration(s), cleared {result['cleared']}.", "success")
        elif not result["bad"]:
            flash("No changes to save.", "success")
        return redirect(url_for("admin_release", release_id=release.id))

    @app.route("/admin/release/<int:release_id>/durations/clear", methods=["POST"])
    @admin_required
    def admin_clear_overrides(release_id):
        release = Release.query.get_or_404(release_id)
        count = clear_overrides(release)
        flash(f"Cleared {count} hand-entered duration(s). Back to the Discogs data.", "success")
        return redirect(url_for("admin_release", release_id=release.id))

    @app.route("/admin/release/<int:release_id>/fetch", methods=["POST"])
    @admin_required
    def admin_fetch_details(release_id):
        release = Release.query.get_or_404(release_id)
        try:
            fetch_release_details(app, release)
            flash("Fetched track details from Discogs.", "success")
        except Exception as e:
            flash(f"Couldn't fetch details: {e}", "error")
        return redirect(url_for("admin_release", release_id=release.id))

    @app.route("/admin/release/<int:release_id>/versions")
    @admin_required
    def admin_find_versions(release_id):
        release = Release.query.get_or_404(release_id)
        versions = []
        error = None
        if release.master_id:
            try:
                client = get_client(app)
                data = client.get_master_versions(release.master_id)
                versions = data.get("versions", [])
            except Exception as e:
                error = str(e)
        else:
            error = ("This release has no master_id on Discogs, so 'other pressings' can't be listed "
                      "automatically. Use manual search below instead.")
        return render_template("admin/search_versions.html", release=release, versions=versions, error=error, manual=False)

    @app.route("/admin/release/<int:release_id>/search")
    @admin_required
    def admin_manual_search(release_id):
        release = Release.query.get_or_404(release_id)
        q = request.args.get("q", "").strip()
        results = []
        error = None
        if q:
            try:
                client = get_client(app)
                data = client.search_release(q)
                results = data.get("results", [])
            except Exception as e:
                error = str(e)
        return render_template("admin/search_versions.html", release=release, versions=[], results=results,
                                q=q, error=error, manual=True)

    @app.route("/admin/release/<int:release_id>/link", methods=["POST"])
    @admin_required
    def admin_set_link(release_id):
        release = Release.query.get_or_404(release_id)
        target_id = request.form.get("target_discogs_id", type=int)
        if not target_id:
            flash("No target release id given.", "error")
            return redirect(url_for("admin_release", release_id=release.id))
        try:
            set_pressing_link(app, release, target_id)
            flash(f"Linked to Discogs release {target_id} for timing data.", "success")
        except Exception as e:
            flash(f"Couldn't link that release: {e}", "error")
        return redirect(url_for("admin_release", release_id=release.id))

    @app.route("/admin/release/<int:release_id>/unlink", methods=["POST"])
    @admin_required
    def admin_unlink(release_id):
        release = Release.query.get_or_404(release_id)
        remove_pressing_link(release)
        flash("Removed pressing link. Timing will use this release's own data again.", "success")
        return redirect(url_for("admin_release", release_id=release.id))

    @app.errorhandler(404)
    def not_found(e):
        return render_template("404.html"), 404


app = create_app()

if __name__ == "__main__":
    # host 0.0.0.0 so it's reachable from your phone on the LAN
    app.run(host="0.0.0.0", port=5001, debug=False)


# ============================================================================
# systemd unit (for reference - save as /etc/systemd/system/side-timer.service)
# ============================================================================
#
# [Unit]
# Description=Discogs Record Side Timer
# After=network-online.target
# Wants=network-online.target
#
# [Service]
# WorkingDirectory=/home/pi/side-timer
# EnvironmentFile=/home/pi/side-timer/.env
# ExecStart=/home/pi/side-timer/venv/bin/python app.py
# Restart=always
# RestartSec=5
# User=pi
#
# [Install]
# WantedBy=multi-user.target
