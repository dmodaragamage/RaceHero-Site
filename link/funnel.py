#!/usr/bin/env python3
"""
RaceHero Link — dev-funnel diagnostics poster (v2.7.1, stdlib only).

Link's PC-side problems (a wheel that vanished, an auto-map that
wouldn't lock, a stream that died) used to be inferable only from
absences on the phone side. This module posts Link's OWN view of
events to the founder's dev funnel (racehero.ca/api/dev-ingest) the
moment they happen:

    lock / unlock / saved-map-loaded   (auto-map state + axis map)
    device connect / disconnect        (which controllers, by name)
    stream start / stop                (HID streaming lifecycle)
    heartbeat every 60 s while streaming (frames sent, map state)
    device_inventory (v2.6)            (startup + every device-set
                                        change: EVERY controller
                                        Windows can enumerate, with
                                        VID/PID identity + whether
                                        it is answering)
    automap_state (v2.6)               (the mapper's candidate
                                        picture whenever it changes
                                        materially, with the reason
                                        class for the current stage)
    The 60 s heartbeat also carries per-axis activity (min/max/
    variance) and the mapper's candidate state since v2.6. Since
    v2.7 it also names the input API doing the reading
    ("inputApi": "directinput" | "winmm") and carries
    "diFallback": true when DirectInput failed to initialise and
    the legacy winmm reader took over.
    v2.7.1 adds the failure DETAIL that flag never had: a di_error
    event the moment any DirectInput stage fails (stage name,
    exception, HRESULT), an input_api event on every API
    transition, and a "diError" field in every heartbeat while
    DirectInput is unhealthy — the fallback can never be silent
    again.

Design rules, in order:
  * NEVER block or delay the HID stream. post() only enqueues;
    one daemon thread does the HTTP. A dead network costs nothing.
  * Failures spool to %APPDATA%/RaceHero/funnel_spool.json
    (bounded: newest 50) and retry on the next start and after the
    next successful post.
  * Config flag: ENABLED below (default ON for this dev build).
    Set ENABLED = False, or set the environment variable
    RACEHERO_NO_FUNNEL=1, to silence the poster entirely.

The token is the revocable DEV token the founder's app builds carry
(BuildConfig FUNNEL_TOKEN; it matches DEV_FUNNEL_TOKEN on Netlify).
It gates a private debugging inbox for one user's own rig. Rotate by
replacing it here, in the app, and in the Netlify env var together.
"""

import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request

LINK_VERSION = "2.7.1"
FUNNEL_URL = "https://racehero.ca/api/dev-ingest"
FUNNEL_TOKEN = "fdfacc39fd1d684572fd573afb12b9ce56f06f0cc4c1798c88b2d49bb09c139f"
ENABLED = True

_SPOOL_MAX = 50
_UA = f"RaceHeroLink/{LINK_VERSION}"

_q = queue.Queue()
_started = False
_start_lock = threading.Lock()
_log = print


def enabled():
    return ENABLED and os.environ.get("RACEHERO_NO_FUNNEL") != "1"


def _spool_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "RaceHero")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "funnel_spool.json")


def _spool_load():
    try:
        with open(_spool_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _spool_save(items):
    try:
        with open(_spool_path(), "w", encoding="utf-8") as f:
            json.dump(items[-_SPOOL_MAX:], f)
    except OSError:
        pass


def _send(payload):
    """One HTTP attempt. True only on a 2xx from the funnel."""
    try:
        req = urllib.request.Request(
            FUNNEL_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-dev-token": FUNNEL_TOKEN,
                "User-Agent": _UA,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def _worker():
    spool = _spool_load()
    if spool:
        _log(f"Dev funnel: retrying {len(spool)} spooled "
             "diagnostic(s) from last run…")
        spool = [p for p in spool if not _send(p)]
        _spool_save(spool)
    while True:
        payload = _q.get()
        if _send(payload):
            if spool:  # link is healthy again — drain the backlog
                spool = [p for p in spool if not _send(p)]
                _spool_save(spool)
        else:
            spool.append(payload)
            _spool_save(spool)


def start(log=print):
    """Start the poster thread once; drains the spool from last run."""
    global _started, _log
    if not enabled():
        return
    with _start_lock:
        if _started:
            return
        _started = True
        _log = log
        t = threading.Thread(target=_worker, name="racehero-funnel",
                             daemon=True)
        t.start()


def post(event):
    """Fire-and-forget: enrich + enqueue. Never raises, never waits."""
    if not enabled():
        return
    try:
        payload = dict(event)
        payload.setdefault("kind", "link")
        payload.setdefault("pcName",
                           os.environ.get("COMPUTERNAME", "PC"))
        payload.setdefault("linkVersion", LINK_VERSION)
        payload["ts"] = int(time.time() * 1000)
        _q.put_nowait(payload)
    except Exception:
        pass
