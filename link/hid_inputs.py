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
                          "steer":-1..1, "device":"MOZA … + …"}

Axis mapping is per-rig, so there is a one-time calibration wizard
(--calibrate-inputs, tkinter) that watches which axis moves when the
driver works each control and saves the map to
%APPDATA%/RaceHero/hid_inputs.json. Until then, a heuristic default
(wheel-named device, axis 0 = steering, axes 1/2 = pedals) gives a
starting point the wizard then makes exact.
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


def _channel(device, axis, rest, full, center=None):
    return {"device": device, "axis": axis,
            "rest": rest, "full": full, "center": center}


def heuristic_mapping(devices):
    """Best guess before calibration: a wheel-named device steers on
    axis 0; throttle/brake ride axes 1/2 of the pedal-ish device
    (or the wheel's own axes when the pedals plug into the base)."""
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
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            saved = json.load(f)
        if saved.get("channels"):
            return saved
    except (OSError, ValueError):
        pass
    return heuristic_mapping(devices)


def save_mapping(mapping):
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2)


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


# ------------------------------------------------------------ streamer ---

class InputStreamer(threading.Thread):
    """Reads the mapped controllers and streams inputs to the phone."""

    def __init__(self, get_phone_ip, log=print):
        super().__init__(daemon=True, name="racehero-hid")
        self.get_phone_ip = get_phone_ip
        self.log = log
        self._last_sent = None
        self._last_sent_at = 0.0
        self._announced = False

    def run(self):
        devices = list_devices()
        if not devices:
            return  # no controllers — nothing to stream, stay silent
        mapping = load_mapping(devices)
        if not mapping:
            return
        by_index = {d["index"]: d for d in devices}
        chans = mapping["channels"]
        names = []
        for ch in chans.values():
            d = by_index.get(ch["device"])
            if d and d["name"] not in names:
                names.append(d["name"])
        label = " + ".join(names) if names else "Wheel/pedals"
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        cache = {}

        def current(ch):
            idx = ch["device"]
            if idx not in cache:
                d = by_index.get(idx)
                if d is None:
                    return None
                cache[idx] = read_axes(idx, d["ranges"])
            return cache[idx]

        while True:
            time.sleep(0.02)  # ~50 Hz
            cache.clear()
            ip = self.get_phone_ip()
            if not ip:
                continue
            vals = {}
            ok = True
            for name, ch in chans.items():
                axes = current(ch)
                if axes is None or ch["axis"] >= len(axes):
                    ok = False
                    break
                raw = axes[ch["axis"]]
                vals[name] = (_norm_steer(ch, raw) if name == "steer"
                              else _norm_pedal(ch, raw))
            if not ok:
                continue
            payload = {
                "throttle": round(vals.get("throttle", 0.0), 3),
                "brake": round(vals.get("brake", 0.0), 3),
                "steer": round(vals.get("steer", 0.0), 3),
                "device": label,
            }
            now = time.time()
            changed = (
                self._last_sent is None
                or any(abs(payload[k] - self._last_sent[k]) > 0.004
                       for k in ("throttle", "brake", "steer"))
            )
            if changed or now - self._last_sent_at > 0.25:
                try:
                    sock.sendto(json.dumps(payload).encode("utf-8"),
                                (ip, PHONE_INPUT_PORT))
                    self._last_sent = payload
                    self._last_sent_at = now
                    if not self._announced:
                        self._announced = True
                        self.log(f"Direct inputs streaming: {label}"
                                 + ("" if mapping.get("calibrated")
                                    else " (uncalibrated — run "
                                         "--calibrate-inputs once)"))
                except OSError:
                    pass


def start_streamer(get_phone_ip, log=print):
    t = InputStreamer(get_phone_ip, log)
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
