#!/usr/bin/env python3
"""
RaceHero Link — direct wheel/pedal input capture (Windows, stdlib only).

The ACC broadcast feed carries no driver inputs, and every game exposes
a different subset. The wheel and pedals, though, are plain USB game
controllers: this module reads them directly through the Windows
multimedia joystick API (winmm via ctypes — no third-party packages,
so the frozen single-file Link build needs nothing extra) and streams
normalized inputs to the phone at ~50 Hz:

    UDP -> phone:20782   {"throttle":0..1, "brake":0..1,
                          "steer":-1..1, "device":"MOZA … + …",
                          "calibrated":true}

Axis mapping is per-rig, and v2.4 maps it AUTOMATICALLY — there is no
calibration step for the driver, ever. The AutoMapper below watches
every axis on every controller while the driver drives: axes that
never move are excluded (a centred, never-read axis streaming a
constant 0.5 was the session-4 failure), the centre-stable,
both-directions axis on the wheel becomes steering, and the two
excursion axes on the pedals become throttle/brake — told apart by
correlation with the car's own gas/brake from the game shared memory
when it is readable, else by excursion duty cycle. Until the map is
LOCKED, the stream sends a heartbeat WITHOUT the input fields (absent
data, never guessed constants) and "calibrated": false. The locked
map persists to %APPDATA%/RaceHero/hid_inputs.json, is re-validated
every session, and is relearned automatically if a mapped channel
ever flatlines through a full session.

Since v2.5 the mapper and streamer also report their state changes
(lock, unlock, device connect/disconnect, stream start/stop, a 60 s
heartbeat) to the dev funnel via an optional emit callback — see
funnel.py. Diagnostics only; the stream itself is unchanged.
"""

import ctypes
import json
import os
import socket
import threading
import time

PHONE_INPUT_PORT = 20782
JOY_RETURNALL = 0x000000FF
MMRESULT_OK = 0
MAX_AXES = 6
AXIS_NAMES = ["X", "Y", "Z", "R", "U", "V"]


# ------------------------------------------------------------ winmm API --

class _JOYINFOEX(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
        ("dwXpos", ctypes.c_uint32), ("dwYpos", ctypes.c_uint32),
        ("dwZpos", ctypes.c_uint32), ("dwRpos", ctypes.c_uint32),
        ("dwUpos", ctypes.c_uint32), ("dwVpos", ctypes.c_uint32),
        ("dwButtons", ctypes.c_uint32), ("dwButtonNumber", ctypes.c_uint32),
        ("dwPOV", ctypes.c_uint32), ("dwReserved1", ctypes.c_uint32),
        ("dwReserved2", ctypes.c_uint32),
    ]


class _JOYCAPS(ctypes.Structure):
    _fields_ = [
        ("wMid", ctypes.c_uint16), ("wPid", ctypes.c_uint16),
        ("szPname", ctypes.c_wchar * 32),
        ("wXmin", ctypes.c_uint), ("wXmax", ctypes.c_uint),
        ("wYmin", ctypes.c_uint), ("wYmax", ctypes.c_uint),
        ("wZmin", ctypes.c_uint), ("wZmax", ctypes.c_uint),
        ("wNumButtons", ctypes.c_uint), ("wPeriodMin", ctypes.c_uint),
        ("wPeriodMax", ctypes.c_uint),
        ("wRmin", ctypes.c_uint), ("wRmax", ctypes.c_uint),
        ("wUmin", ctypes.c_uint), ("wUmax", ctypes.c_uint),
        ("wVmin", ctypes.c_uint), ("wVmax", ctypes.c_uint),
        ("wCaps", ctypes.c_uint), ("wMaxAxes", ctypes.c_uint),
        ("wNumAxes", ctypes.c_uint), ("wMaxButtons", ctypes.c_uint),
        ("szRegKey", ctypes.c_wchar * 32), ("szOEMVxD", ctypes.c_wchar * 260),
    ]


def _winmm():
    if os.name != "nt":
        return None
    try:
        return ctypes.windll.winmm
    except (AttributeError, OSError):
        return None


def list_devices():
    """Connected game controllers: [{index, name, ranges:[(min,max)×6],
    num_axes}]. Empty off Windows or when none are plugged in."""
    mm = _winmm()
    if mm is None:
        return []
    out = []
    count = mm.joyGetNumDevs()
    for i in range(min(count, 16)):
        caps = _JOYCAPS()
        if mm.joyGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) != MMRESULT_OK:
            continue
        info = _JOYINFOEX()
        info.dwSize = ctypes.sizeof(info)
        info.dwFlags = JOY_RETURNALL
        if mm.joyGetPosEx(i, ctypes.byref(info)) != MMRESULT_OK:
            continue  # enumerated but not actually connected
        ranges = [
            (caps.wXmin, caps.wXmax), (caps.wYmin, caps.wYmax),
            (caps.wZmin, caps.wZmax), (caps.wRmin, caps.wRmax),
            (caps.wUmin, caps.wUmax), (caps.wVmin, caps.wVmax),
        ]
        out.append({
            "index": i,
            "name": (caps.szPname or f"Controller {i + 1}").strip(),
            "ranges": ranges,
            "num_axes": int(caps.wNumAxes) or MAX_AXES,
        })
    return out


def read_axes(index, ranges):
    """Raw axis positions normalized 0..1 by the device caps range."""
    mm = _winmm()
    if mm is None:
        return None
    info = _JOYINFOEX()
    info.dwSize = ctypes.sizeof(info)
    info.dwFlags = JOY_RETURNALL
    if mm.joyGetPosEx(index, ctypes.byref(info)) != MMRESULT_OK:
        return None
    raw = [info.dwXpos, info.dwYpos, info.dwZpos,
           info.dwRpos, info.dwUpos, info.dwVpos]
    out = []
    for v, (lo, hi) in zip(raw, ranges):
        span = max(hi - lo, 1)
        out.append(min(1.0, max(0.0, (v - lo) / span)))
    return out


# ------------------------------------------------------------- mapping ---

def config_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "RaceHero")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "hid_inputs.json")


def _channel(device, axis, rest, full, center=None, device_name=None):
    return {"device": device, "axis": axis,
            "rest": rest, "full": full, "center": center,
            "deviceName": device_name}


def heuristic_mapping(devices):
    """DEPRECATED (v2.3 and earlier): a name-based GUESS at the axes.
    Guessed constants are worse than absent data — the guess streamed
    a flat 0.5/0.5/0.0 for a whole session while claiming coverage.
    v2.4 never streams from a guess; this stays only so old code paths
    can be audited. The AutoMapper replaces it."""
    if not devices:
        return None
    wheel = next(
        (d for d in devices
         if any(k in d["name"].lower() for k in ("moza", "wheel", "base", "simucube", "fanatec"))),
        devices[0],
    )
    pedals = next(
        (d for d in devices
         if any(k in d["name"].lower() for k in ("pedal", "sigma", "heusinkveld", "vrs"))),
        wheel,
    )
    chans = {
        "steer": _channel(wheel["index"], 0, 0.5, 1.0, center=0.5),
        "throttle": _channel(pedals["index"], 1, 0.0, 1.0),
        "brake": _channel(pedals["index"], 2, 0.0, 1.0),
    }
    return {"channels": chans, "calibrated": False}


def load_mapping(devices):
    """The persisted map, or None. Only a CONFIDENT map loads — one
    the AutoMapper locked ("auto": true) or the old wizard measured
    ("calibrated": true); a heuristic guess on disk is ignored.
    Channel device indices are re-resolved by device NAME: Windows
    re-numbers controllers across reboots, and a stale index reads
    the wrong device (another flatline factory). Unresolvable maps
    are discarded — the mapper relearns from live data."""
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return None
    if not saved.get("channels") or not saved.get("calibrated"):
        return None
    by_name = {d["name"]: d for d in devices}
    by_index = {d["index"]: d for d in devices}
    for ch in saved["channels"].values():
        name = ch.get("deviceName")
        if name and name in by_name:
            ch["device"] = by_name[name]["index"]
        elif ch.get("device") not in by_index:
            return None
        elif name and by_index[ch["device"]]["name"] != name:
            return None
    return saved


def save_mapping(mapping):
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2)


def discard_mapping():
    try:
        os.remove(config_path())
    except OSError:
        pass


def _norm_pedal(ch, raw):
    rest, full = ch["rest"], ch["full"]
    span = full - rest
    if abs(span) < 1e-6:
        return 0.0
    return min(1.0, max(0.0, (raw - rest) / span))


def _norm_steer(ch, raw):
    # Steer channels store: rest = full-left value, full = full-right
    # value, center = centered value. Piecewise-linear around center
    # so an off-centre raw midpoint never skews the steering feel.
    center = ch["center"] if ch["center"] is not None else ch["rest"]
    if raw < center:
        span = center - ch["rest"]
        if abs(span) < 1e-6:
            return 0.0
        return max(-1.0, (raw - center) / span)
    span = ch["full"] - center
    if abs(span) < 1e-6:
        return 0.0
    return min(1.0, (raw - center) / span)


# ------------------------------------------------- game motion (ref) ---

class _GameMotion:
    """The car's own inputs/speed from the AC/ACC shared memory
    ("acpmf_physics"), read best-effort with the stdlib mmap module.
    Used ONLY as the correlation reference that tells throttle from
    brake — the HID axes stay the streamed data. Absent or insane
    values simply mean "no reference", and the mapper falls back to
    excursion statistics. SPageFilePhysics head layout (both games):
    packetId i32 @0, gas f32 @4, brake f32 @8, fuel f32 @12,
    gear i32 @16, rpms i32 @20, steerAngle f32 @24, speedKmh f32 @28.
    """

    def __init__(self):
        self._mm = None
        self._tried = 0.0
        self.gas = []    # (t, 0..1)
        self.brake = []  # (t, 0..1)
        self.speed = []  # (t, km/h)
        self.available = False

    def _open(self):
        # Named shared memory is a Windows facility; off Windows (dev
        # runs) there is simply no reference — never an error.
        if os.name != "nt":
            self._mm = None
            return
        import mmap
        import struct
        self._struct = struct
        try:
            self._mm = mmap.mmap(-1, 1024, "acpmf_physics",
                                 mmap.ACCESS_READ)
        except Exception:
            self._mm = None

    def update(self, now):
        if self._mm is None:
            if now - self._tried < 5.0:
                return
            self._tried = now
            try:
                self._open()
            except Exception:
                self._mm = None
            if self._mm is None:
                self.available = False
                return
        try:
            raw = self._mm[0:32]
            gas, brake, speed = self._struct.unpack_from("<ff", raw, 4)[0], \
                self._struct.unpack_from("<f", raw, 8)[0], \
                self._struct.unpack_from("<f", raw, 28)[0]
        except Exception:
            self._mm = None
            self.available = False
            return
        sane = (0.0 <= gas <= 1.001 and 0.0 <= brake <= 1.001
                and 0.0 <= speed <= 450.0)
        self.available = bool(sane)
        if not sane:
            return
        for series, v in ((self.gas, gas), (self.brake, brake),
                          (self.speed, speed)):
            series.append((now, v))
            cut = now - 150.0
            while series and series[0][0] < cut:
                series.pop(0)


def _median(vals):
    if not vals:
        return 0.0
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _pearson(xs, ys):
    n = len(xs)
    if n < 40:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx < 1e-6 or syy < 1e-6:
        return 0.0
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / (sxx * syy) ** 0.5


# ------------------------------------------------------- auto-mapper ---

class AutoMapper:
    """Learns which (device, axis) is steer / throttle / brake by
    watching real driving — no wizard, no guesses-as-data.

    Lifecycle: LEARNING (stream sends a heartbeat with NO input
    fields) -> LOCKED (persisted map; values stream with
    "calibrated": true) -> back to LEARNING if any mapped channel
    flatlines through a full session of real driving.

    Identification, over a rolling ~2.5 min of axis history:
      * dead axes (range < 0.05) are excluded outright;
      * STEER = the axis with strong travel BOTH ways from a stable
        centre it keeps returning to, wheel device preferred;
      * PEDALS = one-sided excursion axes resting at an extreme
        (pedal device preferred); throttle vs brake is decided by
        correlation with the game's gas/brake when shared memory is
        readable (decisive), else by duty cycle (throttle works far
        more of a lap than brake).
    A candidate set must win three consecutive evaluations before it
    locks, so a single odd lap can't mint a bad map.
    """

    WINDOW_S = 150.0
    EVAL_EVERY_S = 2.0
    MIN_OBSERVE_S = 20.0
    LOCK_STREAK = 3
    FLAT_UNLOCK_S = 120.0

    def __init__(self, devices, log=print, emit=None):
        self.log = log
        self._emit = emit        # dev-funnel poster (v2.5), optional
        self._evidence = None    # how throttle/brake got decided
        self.devices = {d["index"]: d for d in devices}
        self.names = {d["index"]: d["name"] for d in devices}
        self.series = {}          # (dev, ax) -> [(t, v)]
        self.motion = _GameMotion()
        self.mapping = load_mapping(devices)
        self.locked = self.mapping is not None
        self._eval_at = 0.0
        self._streak = 0
        self._last_pick = None
        self._flat_since = {}     # channel -> t when flatness began
        self._driving_since = None
        self._snapshot = {}       # dev -> [axes] latest tick
        if self.locked:
            log("Direct inputs: saved auto-map loaded — re-validating "
                "against this session.")
            self._emit_event({
                "event": "automap_loaded",
                "hidDevice": self.device_label(),
                "calibrated": True,
                "axisMap": self.axis_map_summary(),
            })

    # ------------------------------------------------------- funnel --

    def _emit_event(self, event):
        """Dev-funnel diagnostics (v2.5): report lock-state changes
        the moment they happen. Fire-and-forget — the poster never
        blocks the stream, and a failure here is swallowed."""
        if self._emit is None:
            return
        try:
            self._emit(event)
        except Exception:
            pass

    def device_label(self):
        names = []
        for n in self.names.values():
            if n not in names:
                names.append(n)
        return " + ".join(names) if names else None

    def axis_map_summary(self):
        """{channel: {device, axis}} for the current map — what the
        funnel (and a human debugging a rig) actually needs to see."""
        out = {}
        for name, ch in (self.mapping or {}).get("channels", {}).items():
            ax = ch["axis"]
            out[name] = {
                "device": ch.get("deviceName")
                          or self.names.get(ch["device"]),
                "axis": AXIS_NAMES[ax] if 0 <= ax < len(AXIS_NAMES)
                        else ax,
            }
        return out

    # ------------------------------------------------------------ io --

    def observe(self, snapshot, now):
        """snapshot: {device_index: [axis values 0..1]} for one tick."""
        self._snapshot = snapshot
        for dev, axes in snapshot.items():
            if axes is None:
                continue
            for ax, v in enumerate(axes):
                key = (dev, ax)
                s = self.series.setdefault(key, [])
                # ~10 Hz is plenty for statistics; ticks arrive ~50 Hz.
                if s and now - s[-1][0] < 0.09:
                    continue
                s.append((now, v))
                cut = now - self.WINDOW_S
                while s and s[0][0] < cut:
                    s.pop(0)
        self.motion.update(now)
        if self.locked:
            self._validate_locked(now)
        elif now >= self._eval_at:
            self._eval_at = now + self.EVAL_EVERY_S
            self._evaluate(now)

    def status_text(self):
        if self.locked:
            return "Pedals: auto-mapped"
        if self.series:
            return "Pedals: learning — drive a lap"
        return "Pedals: no controllers yet"

    def payload(self, label):
        """The datagram for this tick: real values once locked;
        a field-less heartbeat while learning — absent, never faked."""
        if not self.locked or not self.mapping:
            return {"device": label, "calibrated": False}
        vals = {}
        for name, ch in self.mapping["channels"].items():
            axes = self._snapshot.get(ch["device"])
            if axes is None or ch["axis"] >= len(axes):
                return {"device": label, "calibrated": False}
            raw = axes[ch["axis"]]
            vals[name] = (_norm_steer(ch, raw) if name == "steer"
                          else _norm_pedal(ch, raw))
        return {
            "throttle": round(vals.get("throttle", 0.0), 3),
            "brake": round(vals.get("brake", 0.0), 3),
            "steer": round(vals.get("steer", 0.0), 3),
            "device": label,
            "calibrated": True,
        }

    # ------------------------------------------------------ features --

    def _features(self, key):
        s = self.series.get(key) or []
        if len(s) < 30:
            return None
        vals = [v for _, v in s]
        lo, hi = min(vals), max(vals)
        med = _median(vals)
        rest_lo_share = sum(1 for v in vals if v - lo < 0.06) / len(vals)
        rest_hi_share = sum(1 for v in vals if hi - v < 0.06) / len(vals)
        # Which extreme is REST? Not the busier one — a throttle worked
        # hard sits PRESSED more than half the lap, so share-of-samples
        # picks the wrong end and the pedal streams inverted. The
        # truthful witness is the axis's position in the first seconds
        # of observation: Link starts before the driving does, with
        # feet off the pedals and the wheel centred.
        t_start = s[0][0]
        early = [v for t, v in s if t - t_start <= 3.0]
        if early:
            e = sum(early) / len(early)
            rest_is_lo = abs(e - lo) <= abs(e - hi)
        else:
            rest_is_lo = rest_lo_share >= rest_hi_share
        return {
            "lo": lo, "hi": hi, "range": hi - lo, "med": med,
            "exc_lo": med - lo, "exc_hi": hi - med,
            "centre_share": sum(1 for v in vals
                                if abs(v - med) < 0.05) / len(vals),
            "rest_lo_share": rest_lo_share,
            "rest_hi_share": rest_hi_share,
            "rest_is_lo": rest_is_lo,
            "duty": sum(1 for v in vals
                        if (v - lo) > 0.25 * (hi - lo)) / len(vals),
        }

    def _correlate(self, key, motion_series):
        """Pearson between this axis (oriented as an excursion 0..1)
        and a game input series, paired by nearest timestamp."""
        s = self.series.get(key) or []
        if len(s) < 40 or len(motion_series) < 40:
            return 0.0
        f = self._features(key)
        if not f or f["range"] < 0.1:
            return 0.0
        rest_is_lo = f["rest_is_lo"]
        xs, ys = [], []
        j = 0
        for t, v in s:
            while (j + 1 < len(motion_series)
                   and abs(motion_series[j + 1][0] - t)
                   < abs(motion_series[j][0] - t)):
                j += 1
            if abs(motion_series[j][0] - t) > 0.2:
                continue
            norm = (v - f["lo"]) / f["range"] if rest_is_lo \
                else (f["hi"] - v) / f["range"]
            xs.append(norm)
            ys.append(motion_series[j][1])
        return _pearson(xs, ys)

    # ---------------------------------------------------- evaluation --

    def _evaluate(self, now):
        if not self.series:
            return
        t0 = min(s[0][0] for s in self.series.values() if s)
        if now - t0 < self.MIN_OBSERVE_S:
            return
        feats = {}
        for key in list(self.series):
            f = self._features(key)
            if f and f["range"] >= 0.05:      # dead axes never qualify
                feats[key] = f
        if not feats:
            return
        driving = (
            (self.motion.speed and max(v for _, v in self.motion.speed) > 25.0)
            or any(f["range"] > 0.3 for f in feats.values())
        )
        if not driving:
            return
        wheelish = ("moza", "wheel", "base", "simucube", "fanatec",
                    "logitech", "thrustmaster")
        pedalish = ("pedal", "sigma", "heusinkveld", "vrs", "fanatec")

        def dev_rank(key, words):
            name = self.names.get(key[0], "").lower()
            return 0 if any(w in name for w in words) else 1

        # --- steer: travel both ways from a returning centre.
        steer_key, steer_score = None, 0.0
        for key, f in feats.items():
            if f["exc_lo"] < 0.15 or f["exc_hi"] < 0.15:
                continue
            if f["centre_share"] < 0.08:
                continue
            score = min(f["exc_lo"], f["exc_hi"]) \
                - 0.25 * dev_rank(key, wheelish)
            if score > steer_score:
                steer_key, steer_score = key, score
        if steer_key is None:
            self._streak = 0
            return
        # --- pedals: one-sided excursions resting at an extreme.
        cands = []
        for key, f in feats.items():
            if key == steer_key:
                continue
            rest = max(f["rest_lo_share"], f["rest_hi_share"])
            one_sided = min(f["exc_lo"], f["exc_hi"]) \
                <= 0.35 * max(f["exc_lo"], f["exc_hi"])
            if rest < 0.35 or not one_sided or f["range"] < 0.25:
                continue
            cands.append((key, f))
        cands.sort(key=lambda kf: (dev_rank(kf[0], pedalish),
                                   -kf[1]["range"]))
        if len(cands) < 2:
            self._streak = 0
            return
        (k1, f1), (k2, f2) = cands[0], cands[1]
        # --- throttle vs brake.
        thr_key, brk_key = None, None
        if self.motion.available:
            g1 = self._correlate(k1, self.motion.gas)
            b1 = self._correlate(k1, self.motion.brake)
            g2 = self._correlate(k2, self.motion.gas)
            b2 = self._correlate(k2, self.motion.brake)
            pref1 = g1 - b1
            pref2 = g2 - b2
            if abs(pref1) > 0.15 and abs(pref2) > 0.15 \
                    and (pref1 > 0) != (pref2 > 0):
                thr_key, brk_key = (k1, k2) if pref1 > 0 else (k2, k1)
                g_thr, b_brk = (g1, b2) if pref1 > 0 else (g2, b1)
                self._evidence = {
                    "method": "game-correlation",
                    "throttleGasCorr": round(g_thr, 3),
                    "brakeBrakeCorr": round(b_brk, 3),
                }
        if thr_key is None:
            # Duty cycle: throttle is worked far more of a lap.
            if abs(f1["duty"] - f2["duty"]) < 0.03:
                self._streak = 0
                return
            thr_key, brk_key = (k1, k2) if f1["duty"] > f2["duty"] \
                else (k2, k1)
            f_thr, f_brk = (f1, f2) if f1["duty"] > f2["duty"] \
                else (f2, f1)
            self._evidence = {
                "method": "duty-cycle",
                "throttleDuty": round(f_thr["duty"], 3),
                "brakeDuty": round(f_brk["duty"], 3),
            }
        pick = (steer_key, thr_key, brk_key)
        if pick == self._last_pick:
            self._streak += 1
        else:
            self._last_pick, self._streak = pick, 1
        if self._streak < self.LOCK_STREAK:
            return
        self._lock(pick)

    def _lock(self, pick):
        steer_key, thr_key, brk_key = pick
        chans = {}
        f = self._features(steer_key)
        chans["steer"] = _channel(
            steer_key[0], steer_key[1], f["lo"], f["hi"],
            center=f["med"], device_name=self.names.get(steer_key[0]))
        for name, key in (("throttle", thr_key), ("brake", brk_key)):
            f = self._features(key)
            rest, full = (f["lo"], f["hi"]) if f["rest_is_lo"] \
                else (f["hi"], f["lo"])
            chans[name] = _channel(
                key[0], key[1], rest, full,
                device_name=self.names.get(key[0]))
        self.mapping = {"channels": chans, "calibrated": True,
                        "auto": True, "version": 2}
        self.locked = True
        self._flat_since.clear()
        save_mapping(self.mapping)
        self.log("Direct inputs: auto-mapped — "
                 f"steer={self.names.get(steer_key[0])} ax{steer_key[1]}, "
                 f"throttle=ax{thr_key[1]}, brake=ax{brk_key[1]} "
                 "(learned from your driving; saved).")
        self._emit_event({
            "event": "automap_lock",
            "hidDevice": self.device_label(),
            "calibrated": True,
            "axisMap": self.axis_map_summary(),
            "evidence": self._evidence,
        })

    # ---------------------------------------------------- validation --

    def _validate_locked(self, now):
        """A locked channel whose axis stays dead through a full
        session of real driving means the rig changed (or the map was
        wrong): drop the map and relearn."""
        driving = self.motion.speed and \
            max(v for _, v in self.motion.speed) > 25.0
        if not driving:
            strong = any(
                (self._features(k) or {}).get("range", 0) > 0.3
                for k in self.series)
            driving = bool(strong)
        if not driving:
            self._driving_since = None
            return
        if self._driving_since is None:
            self._driving_since = now
            return
        for name, ch in (self.mapping or {}).get("channels", {}).items():
            key = (ch["device"], ch["axis"])
            s = [v for t, v in (self.series.get(key) or [])
                 if t > now - self.WINDOW_S]
            if len(s) < 30:
                continue
            rng = max(s) - min(s)
            if rng >= 0.05:
                self._flat_since.pop(name, None)
                continue
            since = self._flat_since.setdefault(name, now)
            if now - since > self.FLAT_UNLOCK_S:
                self.log(f"Direct inputs: mapped {name} has been dead "
                         "through a full session — dropping the map "
                         "and relearning.")
                self._emit_event({
                    "event": "automap_unlock",
                    "hidDevice": self.device_label(),
                    "calibrated": False,
                    "axisMap": self.axis_map_summary(),
                    "reason": f"mapped {name} flatlined through a "
                              "full session",
                })
                self.locked = False
                self.mapping = None
                self._flat_since.clear()
                self._streak = 0
                self._last_pick = None
                discard_mapping()
                return


# ------------------------------------------------------------ streamer ---

class InputStreamer(threading.Thread):
    """Watches every controller, auto-maps the axes (AutoMapper),
    and streams inputs to the phone. While the map is still being
    learned it sends a heartbeat with NO input fields — the phone
    shows "identifying", and no guessed constant is ever presented
    as data."""

    def __init__(self, get_phone_ip, log=print, emit=None):
        super().__init__(daemon=True, name="racehero-hid")
        self.get_phone_ip = get_phone_ip
        self.log = log
        self._emit = emit        # dev-funnel poster (v2.5), optional
        self._frames_sent = 0
        self._last_sent = None
        self._last_sent_at = 0.0
        self._announced = False
        self._mapper = None

    def _emit_event(self, event):
        """Dev-funnel diagnostics (v2.5), fire-and-forget like the
        mapper's — a poster failure must never touch the stream."""
        if self._emit is None:
            return
        try:
            self._emit(event)
        except Exception:
            pass

    def mapper_status(self):
        """One line for the tray: where auto-mapping stands."""
        m = self._mapper
        return m.status_text() if m is not None \
            else "Pedals: no controllers yet"

    def run(self):
        # NEVER die silently. The v2.2 streamer listed controllers once
        # at startup and exited if none were visible — wheels wake up
        # with the game, not with Windows, so a normal launch order
        # killed pedal streaming for the whole session with no sign
        # anywhere. Now: keep looking, and say what's happening.
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        while True:
            devices = list_devices()
            if not devices:
                if not self._announced:
                    self.log("Direct inputs: no wheel/pedals found yet — "
                             "will keep looking (they may be off or "
                             "asleep).")
                    self._announced = True
                time.sleep(3)
                continue
            self._stream(devices, sock)

    def _stream(self, devices, sock):
        names = []
        for d in devices:
            if d["name"] not in names:
                names.append(d["name"])
        label = " + ".join(names) if names else "Wheel/pedals"
        mapper = AutoMapper(devices, self.log, emit=self._emit)
        self._mapper = mapper
        self._emit_event({
            "event": "device_connect",
            "hidDevice": label,
            "calibrated": mapper.locked,
        })
        announced_stream = False
        dead_reads = 0
        last_hb = time.time()
        while True:
            time.sleep(0.02)  # ~50 Hz
            snapshot = {}
            any_ok = False
            for d in devices:
                axes = read_axes(d["index"], d["ranges"])
                snapshot[d["index"]] = axes
                if axes is not None:
                    any_ok = True
            if not any_ok:
                # Devices vanished (USB sleep, replug) — after ~2 s of
                # dead reads, go back to scanning.
                dead_reads += 1
                if dead_reads > 100:
                    self.log("Direct inputs: controllers stopped "
                             "answering — rescanning.")
                    self._emit_event({
                        "event": "device_disconnect",
                        "hidDevice": label,
                        "calibrated": mapper.locked,
                        "framesSent": self._frames_sent,
                    })
                    self._emit_event({
                        "event": "stream_stop",
                        "hidDevice": label,
                        "framesSent": self._frames_sent,
                        "reason": "controllers stopped answering",
                    })
                    self._announced = False
                    self._mapper = None
                    return
                continue
            dead_reads = 0
            now = time.time()
            mapper.observe(snapshot, now)
            if now - last_hb >= 60.0:
                # Funnel heartbeat (v2.5): proof of life with the
                # numbers a remote diagnosis needs — no payload data.
                last_hb = now
                self._emit_event({
                    "event": "heartbeat",
                    "hidDevice": label,
                    "calibrated": mapper.locked,
                    "devicePresent": True,
                    "framesSent": self._frames_sent,
                })
            ip = self.get_phone_ip()
            if not ip:
                continue
            payload = mapper.payload(label)
            changed = (
                self._last_sent is None
                or payload.keys() != self._last_sent.keys()
                or any(
                    abs(payload[k] - self._last_sent[k]) > 0.004
                    for k in ("throttle", "brake", "steer")
                    if k in payload and k in self._last_sent
                )
            )
            if changed or now - self._last_sent_at > 0.25:
                try:
                    sock.sendto(json.dumps(payload).encode("utf-8"),
                                (ip, PHONE_INPUT_PORT))
                    self._frames_sent += 1
                    self._last_sent = payload
                    self._last_sent_at = now
                    if not announced_stream:
                        announced_stream = True
                        self._emit_event({
                            "event": "stream_start",
                            "hidDevice": label,
                            "calibrated": bool(
                                payload.get("calibrated")),
                        })
                        self.log(
                            f"Direct inputs: {label} — "
                            + ("streaming (auto-mapped)"
                               if payload.get("calibrated")
                               else "identifying axes from your driving "
                                    "(pedals stream once mapped)"))
                except OSError:
                    pass


def start_streamer(get_phone_ip, log=print, emit=None):
    t = InputStreamer(get_phone_ip, log, emit=emit)
    t.start()
    return t


# ------------------------------------------------------- calibration -----

def _snapshot_all(devices):
    """{(device_index, axis): value} for every connected controller."""
    snap = {}
    for d in devices:
        axes = read_axes(d["index"], d["ranges"])
        if axes is None:
            continue
        for a, v in enumerate(axes):
            snap[(d["index"], a)] = v
    return snap


def _biggest_mover(before, after, min_delta=0.15):
    best, best_d = None, min_delta
    for key, v in after.items():
        d = abs(v - before.get(key, v))
        if d > best_d:
            best, best_d = key, d
    return best


def run_calibration(log=print):
    """Tkinter wizard: for each control, capture REST, then capture the
    control fully worked; the axis that moved most is the mapping. No
    typing, four clicks per control."""
    import tkinter as tk
    from tkinter import messagebox

    devices = list_devices()
    if not devices:
        messagebox.showerror(
            "RaceHero Link — input calibration",
            "No game controllers found. Plug in the wheel and pedals, "
            "then run calibration again.")
        return False
    names = {d["index"]: d["name"] for d in devices}

    root = tk.Tk()
    root.title("RaceHero Link — wheel & pedal calibration")
    root.geometry("520x240")
    label = tk.Label(root, text="", wraplength=480, font=("Segoe UI", 12))
    label.pack(padx=20, pady=20, fill="both", expand=True)
    state = {"step": 0, "rest": None, "result": {"channels": {}}}
    btn = tk.Button(root, text="Capture", font=("Segoe UI", 12))

    steps = [
        ("steer", "STEERING — center the wheel and keep it still."),
        ("steer_left", "STEERING — turn the wheel FULLY LEFT and hold it there."),
        ("steer_right", "STEERING — turn the wheel FULLY RIGHT and hold it there."),
        ("throttle", "THROTTLE — take your foot OFF the throttle pedal."),
        ("throttle_full", "THROTTLE — press the throttle ALL the way down and hold it."),
        ("brake", "BRAKE — take your foot OFF the brake pedal."),
        ("brake_full", "BRAKE — press the brake HARD, all the way, and hold it."),
    ]

    def finish():
        chans = state["result"]["channels"]
        if "steer" in state["result"].get("_steer_parts", {}):
            pass
        mapping = {"channels": chans, "calibrated": True}
        save_mapping(mapping)
        used = {names.get(c["device"], "?") for c in chans.values()}
        messagebox.showinfo(
            "RaceHero Link — input calibration",
            "Saved. Inputs will now stream from: " + ", ".join(sorted(used)))
        root.destroy()

    def advance():
        key, _ = steps[state["step"]]
        snap = _snapshot_all(devices)
        res = state["result"]
        if key == "steer":
            res["_steer_rest"] = snap
        elif key in ("steer_left", "steer_right"):
            res.setdefault("_steer_parts", {})[key] = snap
            if key == "steer_right":
                rest = res["_steer_rest"]
                left = res["_steer_parts"]["steer_left"]
                right = res["_steer_parts"]["steer_right"]
                # Axis with the biggest left↔right travel.
                best, best_d = None, 0.2
                for k in right:
                    d = abs(right[k] - left.get(k, right[k]))
                    if d > best_d:
                        best, best_d = k, d
                if best is not None:
                    dev, ax = best
                    res["channels"]["steer"] = _channel(
                        dev, ax, left[best], right[best],
                        center=rest.get(best, (left[best] + right[best]) / 2))
        elif key in ("throttle", "brake"):
            res[f"_{key}_rest"] = snap
        elif key in ("throttle_full", "brake_full"):
            chan = key.replace("_full", "")
            rest = res[f"_{chan}_rest"]
            best = _biggest_mover(rest, snap)
            if best is not None:
                dev, ax = best
                res["channels"][chan] = _channel(dev, ax, rest[best], snap[best])
        state["step"] += 1
        if state["step"] >= len(steps):
            btn.config(text="Save", command=finish)
            label.config(text="Done — click Save to store the calibration.")
        else:
            label.config(text=steps[state["step"]][1] +
                         "\n\nThen click Capture.")

    label.config(text=steps[0][1] + "\n\nThen click Capture.")
    btn.config(command=advance)
    btn.pack(pady=10)
    root.mainloop()
    return True
