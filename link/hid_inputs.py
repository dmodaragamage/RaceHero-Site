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

v2.6 is the two-device fix. The founder's rig is a Moza R9 wheelbase
plus a separate P1000 pedal unit — TWO controllers — and the pedals
never produced data: enumeration used to require a device to answer
joyGetPosEx at the single startup scan, so a pedal unit that woke
late (or answered slowly) was dropped silently for the whole
session, and nothing ever rescanned. Now enumeration reports every
controller Windows can see (answering or not, with VID/PID identity
— two devices that share one generic Windows name are told apart),
the streamer reads the UNION of all of them, a device-set change
re-scans mid-session, and persisted maps re-resolve by VID/PID
first. The funnel gains a device_inventory event (startup + every
set change), automap_state events (the mapper's candidate picture,
with reasons), and a heartbeat carrying per-axis activity plus the
mapper's candidate state — so the next silent rig is diagnosable
from the other side of the world.

v2.6.1 fixes a startup collision with the game itself: the
auto-mapper's game-motion reference used to attach to the game's
shared-memory page through a call that CREATES the page when it is
absent. Link started before ACC therefore claimed the game's page
name first, and ACC died with a fatal error when a session loaded.
The reference reader is now open-only (see _GameMotion) — Link
never creates another process's named objects.

v2.7 pivots device reading from winmm to DirectInput8 — the API
the games themselves read through. Live funnel data proved the
legacy winmm joystick API cannot see this class of rig: on the
founder's PC winmm enumerated three devices, ALL wearing the
generic name "Microsoft PC-joystick driver", and through five
driven laps the pedal unit's axes never moved in winmm's view —
the mapper found steering plus exactly one pedal-like axis and
stalled at "pedal-candidates<2". ACC's own controls screen, reading
the same rig through DirectInput, names the devices plainly: a
"MOZA R9 Base" and a SEPARATE "P1000" pedal unit, its axes in
DirectInput object order. So Link now enumerates and reads through
IDirectInput8 (pure ctypes, still stdlib-only): real product
names, per-device VID/PID identity, and all eight DIJOYSTATE2
axes — including the Rx/Ry rotation axes winmm has no slots for,
which is where this pedal unit's brake lives. winmm remains,
untouched, as the fallback if DirectInput cannot initialise at
all (the heartbeat then carries "diFallback": true) and as the
calibration wizard's reader. Everything downstream of the read —
the AutoMapper, the (device, axis) channel model, the persisted
map, the phone's frame format, the funnel event shapes — is
unchanged; a map saved in the winmm era re-resolves by VID/PID
under DirectInput (axis slots 0-5 mean the same physical axes in
both layouts) or invalidates and relearns, exactly as v2.6.1
maps do.
"""

import ctypes
import json
import os
import socket
import struct
import threading
import time

PHONE_INPUT_PORT = 20782
JOY_RETURNALL = 0x000000FF
MMRESULT_OK = 0
MAX_AXES = 6        # winmm JOYINFOEX slots per device
DI_MAX_AXES = 8     # DIJOYSTATE2 axis slots per device (v2.7)

# The mapper's axis letters, by slot index. Slots 0-5 are shared by
# both readers so a persisted channel means the same physical axis
# whichever API read it; slots 6-7 exist only under DirectInput.
#
#   slot  letter  winmm field      DirectInput (DIJOYSTATE2)  ACC #
#   ----  ------  ---------------  -------------------------  -----
#     0     X     dwXpos           lX                           1
#     1     Y     dwYpos           lY                           2
#     2     Z     dwZpos           lZ                           3
#     3     R     dwRpos           lRz                          6
#     4     U     dwUpos           rglSlider[0]                 7
#     5     V     dwVpos           rglSlider[1]                 8
#     6     Rx        —            lRx                          4
#     7     Ry        —            lRy                          5
#
# ACC numbers axes in DirectInput object order (X, Y, Z, Rx, Ry,
# Rz, sliders) — so the founder's P1000 shows throttle/brake/clutch
# as ACC 6/5/4, which are DI Rz/Ry/Rx: slots 3/7/6 here. Nothing is
# hardcoded from that: the AutoMapper still DISCOVERS which slot is
# which control by watching driving and correlating with the game.
AXIS_NAMES = ["X", "Y", "Z", "R", "U", "V", "Rx", "Ry"]


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


def _enumerate_winmm():
    """Every game controller the LEGACY winmm joystick API can
    enumerate: [{index, name, vid, pid, ranges:[(min,max)×6],
    num_axes, num_buttons, responding, backend:"winmm"}].

    Since v2.7 this is the fallback path (used only when
    DirectInput cannot initialise) and the calibration wizard's
    view. Its limits are why it is no longer primary: identical
    generic names on distinct devices, six axis slots with no
    Rx/Ry, and — on the founder's rig — pedal axes that simply
    never move in its reports.

    A device whose caps read fine but which does not answer
    joyGetPosEx right now (asleep, still waking, slow to its first
    report) is INCLUDED with responding=False. v2.5 dropped it
    silently — on a two-device rig the pedal unit could wake a
    minute after the wheel and simply never exist for that session.

    Identity is (index, vid, pid), never the name alone: Windows
    hands every device without a registered OEM product name the
    same generic driver name, so a wheel base and a pedal unit can
    present as two identical strings. The name in the funnel label
    cannot tell them apart; the VID/PID in the inventory can."""
    mm = _winmm()
    if mm is None:
        return []
    out = []
    count = mm.joyGetNumDevs()
    for i in range(min(count, 16)):
        caps = _JOYCAPS()
        if mm.joyGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) != MMRESULT_OK:
            continue  # nothing at this port — the normal empty case
        info = _JOYINFOEX()
        info.dwSize = ctypes.sizeof(info)
        info.dwFlags = JOY_RETURNALL
        responding = mm.joyGetPosEx(i, ctypes.byref(info)) == MMRESULT_OK
        ranges = [
            (caps.wXmin, caps.wXmax), (caps.wYmin, caps.wYmax),
            (caps.wZmin, caps.wZmax), (caps.wRmin, caps.wRmax),
            (caps.wUmin, caps.wUmax), (caps.wVmin, caps.wVmax),
        ]
        out.append({
            "index": i,
            "name": (caps.szPname or f"Controller {i + 1}").strip(),
            "vid": int(caps.wMid),
            "pid": int(caps.wPid),
            "ranges": ranges,
            "num_axes": int(caps.wNumAxes) or MAX_AXES,
            "num_buttons": int(caps.wNumButtons),
            "responding": bool(responding),
            "backend": "winmm",
        })
    return out


def list_devices():
    """Connected (answering) game controllers in the winmm view —
    the calibration wizard reads axes through read_axes(), which
    is winmm, so the wizard keeps enumerating through winmm too
    (unchanged since v2.5). The streamer works from the full
    enumerate_devices() union instead."""
    return [d for d in _enumerate_winmm() if d["responding"]]


def device_signature(devices):
    """Stable identity of a device SET: two enumerations with the
    same signature are the same rig in the same state. The first
    five fields are a device's identity; the sixth is whether it is
    answering right now. Under DirectInput the identity slot is the
    device's instance GUID, not its enumeration position — DI can
    renumber a set between polls without the set changing, and a
    renumbering must not read as a device-set change."""
    return tuple(sorted(
        (d.get("key") or d["index"], d.get("vid"), d.get("pid"),
         d["name"], d["num_axes"], bool(d.get("responding", True)))
        for d in devices))


def _device_public(d):
    pub = {"index": d["index"], "name": d["name"],
           "vid": d.get("vid"), "pid": d.get("pid"),
           "numAxes": d["num_axes"],
           "numButtons": d.get("num_buttons"),
           "responding": bool(d.get("responding", True))}
    if d.get("backend"):
        pub["backend"] = d["backend"]
    if d.get("key"):
        pub["key"] = d["key"]
    return pub


def inventory_event(devices, reason, mapper=None):
    """The device_inventory funnel payload (v2.6): EVERY controller
    Windows can enumerate — including ones not answering — with the
    (index, VID/PID) identity that tells two identically named
    devices apart, and what the streamer is doing with them. The
    streamer never picks ONE device: it reads the union of all of
    them and the auto-mapper assigns channels per (device, axis),
    which is what a two-box rig (wheel base + separate pedals)
    needs."""
    ev = {
        "event": "device_inventory",
        "reason": reason,
        "devices": [_device_public(d) for d in devices],
        "observedDevices": [d["index"] for d in devices],
        "inputApi": input_api(),
        "selection": "union — streamer reads every enumerated "
                     "controller; auto-mapper assigns channels per "
                     "(device, axis)",
    }
    if mapper is not None:
        ev["calibrated"] = bool(mapper.locked)
        if mapper.locked:
            ev["axisMap"] = mapper.axis_map_summary()
    return ev


def read_axes(index, ranges):
    """winmm: raw axis positions normalized 0..1 by the device caps
    range. Returns a MAX_AXES (6) list or None. Since v2.7 the
    streamer reads through _read_device(), which dispatches here
    only for winmm-enumerated devices (the fallback + the wizard)."""
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


# ------------------------------------------------- DirectInput (v2.7) --
#
# IDirectInput8 through pure ctypes COM: no headers, no third-party
# packages — the vtable layouts, structures and constants below are
# dinput.h transcribed. COM functions are called through function
# pointers read out of the object's vtable; prototypes use
# WINFUNCTYPE (stdcall) where the platform has it.
#
# DIJOYSTATE2 byte offsets (the state buffer GetDeviceState fills
# under the c_dfDIJoystick2 data format we build below):
#   lX 0 · lY 4 · lZ 8 · lRx 12 · lRy 16 · lRz 20 ·
#   rglSlider 24/28 · rgdwPOV 32..47 · rgbButtons 48..175 ·
#   velocity/acceleration/force blocks 176..271 — 272 bytes total.
# Our snapshot slots read [lX, lY, lZ, lRz, slider0, slider1, lRx,
# lRy] — see the AXIS_NAMES table at the top of the module.

import uuid  # GUID <-> bytes_le is exactly the COM GUID memory layout

_FT = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)  # stdcall on Win
_HRESULT = ctypes.c_long
_THIS = ctypes.c_void_p

_DI_VERSION = 0x0800
_DI8DEVCLASS_GAMECTRL = 4
_DIEDFL_ATTACHEDONLY = 0x00000001
_DIENUM_CONTINUE = 1
_DISCL_NONEXCLUSIVE = 0x00000002
_DISCL_BACKGROUND = 0x00000008
_DIPH_DEVICE = 0
_DIPH_BYOFFSET = 2
# Property identifiers are pseudo-GUIDs: small integers carried in a
# GUID* (MAKEDIPROP in dinput.h). A device that doesn't know one
# fails the call and we fall back — see _di_vidpid/_di_range.
_DIPROP_RANGE_PROP = 4
_DIPROP_VIDPID_PROP = 11
_DIDF_ABSAXIS = 0x00000001
_DIDFT_OPTIONAL = 0x80000000
_DIDFT_AXIS = 0x00000003
_DIDFT_BUTTON = 0x0000000C
_DIDFT_POV = 0x00000010
_DIDFT_ANYINSTANCE = 0x00FFFF00

_DI_STATE_SIZE = 272          # sizeof(DIJOYSTATE2)
# Snapshot slot -> DIJOYSTATE2 byte offset, in AXIS_NAMES order.
_DI_SLOT_OFFSETS = [0, 4, 8, 20, 24, 28, 12, 16]
_DI_DEF_RANGE = (0, 65535)


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]


def _make_guid(text):
    g = _GUID()
    ctypes.memmove(ctypes.byref(g), uuid.UUID(text).bytes_le, 16)
    return g


def _guid_text(g):
    return str(uuid.UUID(
        bytes_le=ctypes.string_at(ctypes.byref(g), 16)))


_IID_IDirectInput8W = "{7844E811-73A5-11CF-9C04-00AA0061AC3C}"
_GUID_XAXIS = _make_guid("{A36D02E0-C9F3-11CF-BFC7-444553540000}")
_GUID_YAXIS = _make_guid("{A36D02E1-C9F3-11CF-BFC7-444553540000}")
_GUID_ZAXIS = _make_guid("{A36D02E2-C9F3-11CF-BFC7-444553540000}")
_GUID_RXAXIS = _make_guid("{A36D02F4-C9F3-11CF-BFC7-444553540000}")
_GUID_RYAXIS = _make_guid("{A36D02F5-C9F3-11CF-BFC7-444553540000}")
_GUID_RZAXIS = _make_guid("{A36D02F6-C9F3-11CF-BFC7-444553540000}")
_GUID_SLIDER = _make_guid("{A36D02E4-C9F3-11CF-BFC7-444553540000}")
_GUID_POV = _make_guid("{A36D02F2-C9F3-11CF-BFC7-444553540000}")
_GUID_BUTTON = _make_guid("{A36D02E8-C9F3-11CF-BFC7-444553540000}")


class _DIDEVICEINSTANCEW(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32), ("guidInstance", _GUID),
        ("guidProduct", _GUID), ("dwDevType", ctypes.c_uint32),
        ("tszInstanceName", ctypes.c_wchar * 260),
        ("tszProductName", ctypes.c_wchar * 260),
        ("guidFFDriver", _GUID),
        ("wUsagePage", ctypes.c_uint16), ("wUsage", ctypes.c_uint16),
    ]


class _DIDEVCAPS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint32) for n in (
        "dwSize", "dwFlags", "dwDevType", "dwAxes", "dwButtons",
        "dwPOVs", "dwFFSamplePeriod", "dwFFMinTimeResolution",
        "dwFirmwareRevision", "dwHardwareRevision",
        "dwFFDriverVersion")]


class _DIPROPHEADER(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_uint32),
                ("dwHeaderSize", ctypes.c_uint32),
                ("dwObj", ctypes.c_uint32), ("dwHow", ctypes.c_uint32)]


class _DIPROPDWORD(ctypes.Structure):
    _fields_ = [("diph", _DIPROPHEADER), ("dwData", ctypes.c_uint32)]


class _DIPROPRANGE(ctypes.Structure):
    _fields_ = [("diph", _DIPROPHEADER), ("lMin", ctypes.c_long),
                ("lMax", ctypes.c_long)]


class _DIOBJECTDATAFORMAT(ctypes.Structure):
    _fields_ = [("pguid", ctypes.POINTER(_GUID)),
                ("dwOfs", ctypes.c_uint32),
                ("dwType", ctypes.c_uint32),
                ("dwFlags", ctypes.c_uint32)]


class _DIDATAFORMAT(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_uint32),
                ("dwObjSize", ctypes.c_uint32),
                ("dwFlags", ctypes.c_uint32),
                ("dwDataSize", ctypes.c_uint32),
                ("dwNumObjs", ctypes.c_uint32),
                ("rgodf", ctypes.POINTER(_DIOBJECTDATAFORMAT))]


def _joystick2_format():
    """c_dfDIJoystick2, rebuilt: DIJOYSTATE2's 140 objects in
    c_df order (axes X/Y/Z/Rx/Ry/Rz, 2 sliders, 4 POVs, 128 buttons),
    every object OPTIONAL so a device missing one is skipped rather
    than failing SetDataFormat. Returns (format, objects) — the
    objects array must stay alive across the SetDataFormat call."""
    spec = []
    for guid, ofs in ((_GUID_XAXIS, 0), (_GUID_YAXIS, 4),
                      (_GUID_ZAXIS, 8), (_GUID_RXAXIS, 12),
                      (_GUID_RYAXIS, 16), (_GUID_RZAXIS, 20),
                      (_GUID_SLIDER, 24), (_GUID_SLIDER, 28)):
        spec.append((guid, ofs, _DIDFT_AXIS))
    for n in range(4):
        spec.append((_GUID_POV, 32 + 4 * n, _DIDFT_POV))
    for n in range(128):
        spec.append((_GUID_BUTTON, 48 + n, _DIDFT_BUTTON))
    objs = (_DIOBJECTDATAFORMAT * len(spec))()
    for i, (guid, ofs, kind) in enumerate(spec):
        objs[i].pguid = ctypes.pointer(guid)
        objs[i].dwOfs = ofs
        objs[i].dwType = _DIDFT_OPTIONAL | kind | _DIDFT_ANYINSTANCE
        objs[i].dwFlags = 0
    fmt = _DIDATAFORMAT()
    fmt.dwSize = ctypes.sizeof(_DIDATAFORMAT)
    fmt.dwObjSize = ctypes.sizeof(_DIOBJECTDATAFORMAT)
    fmt.dwFlags = _DIDF_ABSAXIS
    fmt.dwDataSize = _DI_STATE_SIZE
    fmt.dwNumObjs = len(spec)
    fmt.rgodf = objs
    return fmt, objs


# COM vtable method prototypes (index in the interface's vtable).
def _vt(obj, index, proto):
    vtbl = ctypes.cast(
        obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return proto(vtbl[index])


# IDirectInput8: 3 CreateDevice, 4 EnumDevices
_DI_CreateDevice = _FT(_HRESULT, _THIS, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p)
_DI_EnumDevices = _FT(_HRESULT, _THIS, ctypes.c_uint32, ctypes.c_void_p,
                      ctypes.c_void_p, ctypes.c_uint32)
# IDirectInputDevice8: 2 Release, 3 GetCapabilities, 5 GetProperty,
# 7 Acquire, 8 Unacquire, 9 GetDeviceState, 11 SetDataFormat,
# 13 SetCooperativeLevel, 25 Poll
_DEV_Release = _FT(ctypes.c_ulong, _THIS)
_DEV_GetCapabilities = _FT(_HRESULT, _THIS, ctypes.c_void_p)
_DEV_GetProperty = _FT(_HRESULT, _THIS, ctypes.c_void_p, ctypes.c_void_p)
_DEV_Acquire = _FT(_HRESULT, _THIS)
_DEV_Unacquire = _FT(_HRESULT, _THIS)
_DEV_GetDeviceState = _FT(_HRESULT, _THIS, ctypes.c_uint32,
                          ctypes.c_void_p)
_DEV_SetDataFormat = _FT(_HRESULT, _THIS, ctypes.c_void_p)
_DEV_SetCooperativeLevel = _FT(_HRESULT, _THIS, ctypes.c_void_p,
                               ctypes.c_uint32)
_DEV_Poll = _FT(_HRESULT, _THIS)
_EnumCallback = _FT(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

_VT = {"create": 3, "enum": 4}
_DVT = {"release": 2, "caps": 3, "getprop": 5, "acquire": 7,
        "unacquire": 8, "state": 9, "format": 11, "coop": 13,
        "poll": 25}


def _di_create(dll):
    proto = _FT(_HRESULT, ctypes.c_void_p, ctypes.c_uint32,
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    return proto(("DirectInput8Create", dll))


def _di_vidpid(dev, guid_product):
    """(vid, pid) for a DI device, or (None, None). Two routes,
    each failure-tolerant: DIPROP_VIDPID (the low word is the
    vendor ID, the high word the product ID), then the documented
    HID behaviour that guidProduct's Data1 is MAKELONG(VID, PID)
    (Microsoft's own IsXInputDevice sample relies on it)."""
    try:
        prop = _DIPROPDWORD()
        prop.diph.dwSize = ctypes.sizeof(_DIPROPDWORD)
        prop.diph.dwHeaderSize = ctypes.sizeof(_DIPROPHEADER)
        prop.diph.dwObj = 0
        prop.diph.dwHow = _DIPH_DEVICE
        hr = _vt(dev, _DVT["getprop"], _DEV_GetProperty)(
            dev, ctypes.c_void_p(_DIPROP_VIDPID_PROP),
            ctypes.addressof(prop))
        if hr == 0 and prop.dwData:
            return prop.dwData & 0xFFFF, (prop.dwData >> 16) & 0xFFFF
    except Exception:
        pass
    try:
        data1 = guid_product.Data1
        vid, pid = data1 & 0xFFFF, (data1 >> 16) & 0xFFFF
        if vid:
            return vid, pid
    except Exception:
        pass
    return None, None


def _di_range(dev, ofs):
    """DIPROP_RANGE for one axis object (by DIJOYSTATE2 offset), or
    the DI default. Failure-tolerant on purpose: the mapper learns
    rest/full from observed travel anyway — this only scales the
    snapshot into 0..1 the way the device itself reports."""
    try:
        rng = _DIPROPRANGE()
        rng.diph.dwSize = ctypes.sizeof(_DIPROPRANGE)
        rng.diph.dwHeaderSize = ctypes.sizeof(_DIPROPHEADER)
        rng.diph.dwObj = ofs
        rng.diph.dwHow = _DIPH_BYOFFSET
        hr = _vt(dev, _DVT["getprop"], _DEV_GetProperty)(
            dev, ctypes.c_void_p(_DIPROP_RANGE_PROP),
            ctypes.addressof(rng))
        if hr == 0 and rng.lMax > rng.lMin:
            return int(rng.lMin), int(rng.lMax)
    except Exception:
        pass
    return _DI_DEF_RANGE


class _DiDevice:
    """One opened IDirectInputDevice8, read-ready. Created lazily
    by the backend's reader cache; a read that fails re-acquires
    once and retries before reporting the device silent."""

    def __init__(self, backend, guid_instance):
        self._be = backend
        self._guid = guid_instance    # _GUID (owned copy)
        self._dev = None

    def _open(self):
        dev = self._be._create_device(self._guid)
        if dev is None:
            return False
        try:
            fmt, _objs = _joystick2_format()  # array lives for the call
            hr = _vt(dev, _DVT["format"], _DEV_SetDataFormat)(
                dev, ctypes.addressof(fmt))
            if hr != 0:
                self._release(dev)
                return False
            hwnd = self._be._coop_hwnd()
            if hwnd:
                # DISCL_BACKGROUND | DISCL_NONEXCLUSIVE: read the
                # rig while the game owns the foreground, share it
                # with every other tool. A failure here is not
                # fatal — acquisition decides what we can read.
                try:
                    _vt(dev, _DVT["coop"], _DEV_SetCooperativeLevel)(
                        dev, hwnd,
                        _DISCL_BACKGROUND | _DISCL_NONEXCLUSIVE)
                except Exception:
                    pass
            _vt(dev, _DVT["acquire"], _DEV_Acquire)(dev)
        except Exception:
            self._release(dev)
            return False
        self._dev = dev
        return True

    @staticmethod
    def _release(dev):
        try:
            _vt(dev, _DVT["release"], _DEV_Release)(dev)
        except Exception:
            pass

    def close(self):
        if self._dev is not None:
            try:
                _vt(self._dev, _DVT["unacquire"], _DEV_Unacquire)(
                    self._dev)
            except Exception:
                pass
            self._release(self._dev)
            self._dev = None

    def read(self, ranges):
        """8 normalized axis values (AXIS_NAMES order), or None."""
        if self._dev is None and not self._open():
            return None
        buf = ctypes.create_string_buffer(_DI_STATE_SIZE)
        for attempt in (0, 1):
            try:
                _vt(self._dev, _DVT["poll"], _DEV_Poll)(self._dev)
                hr = _vt(self._dev, _DVT["state"], _DEV_GetDeviceState)(
                    self._dev, _DI_STATE_SIZE, ctypes.addressof(buf))
            except Exception:
                hr = -1
            if hr == 0:
                break
            if attempt == 0:
                # Input lost / not acquired (the game took the
                # foreground, a replug): re-acquire and retry once.
                try:
                    _vt(self._dev, _DVT["acquire"], _DEV_Acquire)(
                        self._dev)
                except Exception:
                    pass
        else:
            return None
        out = []
        for slot, ofs in enumerate(_DI_SLOT_OFFSETS):
            raw = struct.unpack_from("<l", buf.raw, ofs)[0]
            lo, hi = ranges[slot] if slot < len(ranges) \
                else _DI_DEF_RANGE
            span = max(hi - lo, 1)
            out.append(min(1.0, max(0.0, (raw - lo) / span)))
        return out


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint),
                ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", ctypes.c_void_p),
                ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p),
                ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", ctypes.c_wchar_p),
                ("lpszClassName", ctypes.c_wchar_p)]


class _DirectInput8:
    """The DirectInput backend: enumerate + read. Construction IS
    the initialisation test — any failure raises, and the module
    falls back to winmm for the life of the process."""

    API = "directinput"

    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("DirectInput is Windows-only")
        dll = ctypes.WinDLL("dinput8.dll")
        k32 = ctypes.windll.kernel32
        k32.GetModuleHandleW.restype = ctypes.c_void_p
        hinst = k32.GetModuleHandleW(None)
        iid = _make_guid(_IID_IDirectInput8W)
        out = ctypes.c_void_p()
        hr = _di_create(dll)(hinst, _DI_VERSION,
                             ctypes.addressof(iid),
                             ctypes.addressof(out), None)
        if hr != 0 or not out.value:
            raise RuntimeError(f"DirectInput8Create failed: {hr:#x}")
        self._di = out.value
        self._readers = {}     # instance-guid text -> _DiDevice
        self._hwnd = False     # False = untried, None = unavailable

    # ---------------------------------------------------------- com --

    def _create_device(self, guid_instance):
        out = ctypes.c_void_p()
        try:
            hr = _vt(self._di, _VT["create"], _DI_CreateDevice)(
                self._di, ctypes.addressof(guid_instance),
                ctypes.addressof(out), None)
        except Exception:
            return None
        return out.value if hr == 0 and out.value else None

    def _enum_instances(self):
        """[(guidInstance _GUID, productName, instanceName,
        guidProduct _GUID)] for every ATTACHED game controller."""
        found = []

        def _cb(ptr, _ref):
            try:
                inst = ctypes.cast(
                    ptr, ctypes.POINTER(_DIDEVICEINSTANCEW)).contents
                gi, gp = _GUID(), _GUID()
                ctypes.memmove(ctypes.byref(gi),
                               ctypes.byref(inst.guidInstance), 16)
                ctypes.memmove(ctypes.byref(gp),
                               ctypes.byref(inst.guidProduct), 16)
                found.append((gi, inst.tszProductName or "",
                              inst.tszInstanceName or "", gp))
            except Exception:
                pass
            return _DIENUM_CONTINUE

        cb = _EnumCallback(_cb)
        try:
            _vt(self._di, _VT["enum"], _DI_EnumDevices)(
                self._di, _DI8DEVCLASS_GAMECTRL, cb, None,
                _DIEDFL_ATTACHEDONLY)
        except Exception:
            return []
        return found

    def _coop_hwnd(self):
        """A never-shown window of our own process for
        SetCooperativeLevel (BACKGROUND needs a window handle).
        The window proc is user32's DefWindowProcW itself, so no
        Python callback has to outlive anything. Any failure ->
        None: cooperative level is skipped, reads still work for
        devices that acquire without it."""
        if self._hwnd is not False:
            return self._hwnd
        self._hwnd = None
        try:
            user32 = ctypes.windll.user32
            k32 = ctypes.windll.kernel32
            k32.GetModuleHandleW.restype = ctypes.c_void_p
            user32.CreateWindowExW.restype = ctypes.c_void_p
            wc = _WNDCLASSW()
            wc.lpfnWndProc = ctypes.cast(
                user32.DefWindowProcW, ctypes.c_void_p).value
            wc.hInstance = k32.GetModuleHandleW(None)
            wc.lpszClassName = "RaceHeroLinkDI"
            user32.RegisterClassW(ctypes.byref(wc))  # may already exist
            hwnd = user32.CreateWindowExW(
                0, "RaceHeroLinkDI", "RaceHero Link", 0,
                0, 0, 0, 0, None, None, wc.hInstance, None)
            self._hwnd = hwnd or None
        except Exception:
            self._hwnd = None
        return self._hwnd

    # ------------------------------------------------------ backend --

    def enumerate(self):
        """The DI device dicts (same shape as _enumerate_winmm's,
        plus backend/key and EIGHT ranges). Identity comes from a
        brief CreateDevice probe per device (caps, VID/PID, axis
        ranges) — created and released here; the streaming readers
        open their own handles lazily in read()."""
        out = []
        live_keys = set()
        for i, (gi, prod_name, inst_name, gp) in enumerate(
                self._enum_instances()):
            key = _guid_text(gi)
            live_keys.add(key)
            name = (prod_name or inst_name
                    or f"Controller {i + 1}").strip()
            vid = pid = None
            num_axes, num_buttons = DI_MAX_AXES, 0
            ranges = [_DI_DEF_RANGE] * DI_MAX_AXES
            responding = False
            dev = self._create_device(gi)
            if dev is not None:
                responding = True
                try:
                    caps = _DIDEVCAPS()
                    caps.dwSize = ctypes.sizeof(_DIDEVCAPS)
                    if _vt(dev, _DVT["caps"], _DEV_GetCapabilities)(
                            dev, ctypes.addressof(caps)) == 0:
                        num_axes = min(int(caps.dwAxes), DI_MAX_AXES) \
                            or DI_MAX_AXES
                        num_buttons = int(caps.dwButtons)
                    vid, pid = _di_vidpid(dev, gp)
                    ranges = [_di_range(dev, ofs)
                              for ofs in _DI_SLOT_OFFSETS]
                except Exception:
                    pass
                _DiDevice._release(dev)
            out.append({
                "index": i, "name": name, "vid": vid, "pid": pid,
                "ranges": ranges, "num_axes": num_axes,
                "num_buttons": num_buttons,
                "responding": responding,
                "backend": "di", "key": key,
            })
        # Forget readers for devices that have left the set, so a
        # replugged namesake never inherits a dead handle.
        for key in list(self._readers):
            if key not in live_keys:
                self._readers.pop(key).close()
        return out

    def read(self, dev):
        reader = self._readers.get(dev.get("key") or "")
        if reader is None:
            try:
                guid = _make_guid(dev["key"])
            except Exception:
                return None
            reader = _DiDevice(self, guid)
            self._readers[dev["key"]] = reader
        return reader.read(dev.get("ranges")
                           or [_DI_DEF_RANGE] * DI_MAX_AXES)


_DI_TRIED = False
_DI_BACKEND = None
_DI_FALLBACK = False


def _di():
    """The process-wide DirectInput backend, or None. Tried once:
    before Windows there is nothing to try; ON Windows a failed
    init means the winmm fallback for the rest of the run, and
    _DI_FALLBACK remembers so the heartbeat can say so."""
    global _DI_TRIED, _DI_BACKEND, _DI_FALLBACK
    if _DI_TRIED:
        return _DI_BACKEND
    _DI_TRIED = True
    if os.name != "nt":
        return None
    try:
        _DI_BACKEND = _DirectInput8()
    except Exception:
        _DI_BACKEND = None
        _DI_FALLBACK = True
    return _DI_BACKEND


def di_fallback_active():
    """True when DirectInput failed to initialise on Windows and
    devices are being read through legacy winmm instead."""
    _di()
    return _DI_FALLBACK


def input_api():
    return "directinput" if _di() is not None else "winmm"


def enumerate_devices():
    """Every game controller the ACTIVE reader can enumerate —
    DirectInput since v2.7 (real product names, VID/PID identity,
    eight axis slots), winmm when DirectInput is unavailable.
    [{index, name, vid, pid, ranges, num_axes, num_buttons,
    responding, backend, key?}] — see _enumerate_winmm for why
    non-answering devices are included rather than dropped."""
    be = _di()
    if be is not None:
        return be.enumerate()
    return _enumerate_winmm()


def _read_device(d):
    """One tick of axes (0..1 list) for an enumerated device dict,
    through whichever reader enumerated it; None if it does not
    answer this tick."""
    if d.get("backend") == "di":
        be = _di()
        return be.read(d) if be is not None else None
    return read_axes(d["index"], d["ranges"])


# ------------------------------------------------------------- mapping ---

def config_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "RaceHero")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "hid_inputs.json")


def _channel(device, axis, rest, full, center=None, device_name=None,
             device_vid=None, device_pid=None):
    ch = {"device": device, "axis": axis,
          "rest": rest, "full": full, "center": center,
          "deviceName": device_name}
    if device_vid is not None:
        # The device DIMENSION (v2.6): VID/PID survive renumbering
        # AND identical generic names; the index alone survives
        # neither. Omitted (not null) on maps the wizard wrote.
        ch["deviceVid"] = device_vid
        ch["devicePid"] = device_pid
    return ch


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
    Channel device indices are re-resolved by device identity:
    VID/PID first (v2.6 — it survives renumbering AND the identical
    generic names two devices can share), then by NAME (Windows
    re-numbers controllers across reboots, and a stale index reads
    the wrong device — another flatline factory). Unresolvable maps
    are discarded — the mapper relearns from live data."""
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return None
    if not saved.get("channels") or not saved.get("calibrated"):
        return None
    by_vidpid = {(d["vid"], d["pid"]): d for d in devices
                 if d.get("vid") is not None}
    by_name = {d["name"]: d for d in devices}
    by_index = {d["index"]: d for d in devices}
    for ch in saved["channels"].values():
        vid, pid = ch.get("deviceVid"), ch.get("devicePid")
        if vid is not None and (vid, pid) in by_vidpid:
            ch["device"] = by_vidpid[(vid, pid)]["index"]
            continue
        if vid is not None:
            # The channel carries a recorded device identity and
            # that device is NOT CONNECTED. Never fall back to the
            # bare name — with two same-named controllers that would
            # silently re-point the pedals at the wheel (the exact
            # v2.5 failure, replayed from the saved map). The map is
            # unusable as-is; the mapper relearns from live data.
            return None
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
    ("acpmf_physics"). Used ONLY as the correlation reference that
    tells throttle from brake — the HID axes stay the streamed data.
    Absent or insane values simply mean "no reference", and the
    mapper falls back to excursion statistics. SPageFilePhysics
    head layout (both games): packetId i32 @0, gas f32 @4,
    brake f32 @8, fuel f32 @12, gear i32 @16, rpms i32 @20,
    steerAngle f32 @24, speedKmh f32 @28.

    OPEN-ONLY — NEVER CREATE (v2.6.1; this is the ACC-crash fix).
    That page is the GAME's kernel object, not ours. The reader
    used to attach with the stdlib tagged-mmap call, which CPython
    implements as CreateFileMappingW — so when Link started before
    the game, LINK was the process that created "acpmf_physics":
    a small PAGE_READONLY page, claimed seconds after Link launch
    and re-claimed on every retry. The game creates its physics
    page only when a session loads, and then it found its own name
    already taken by a foreign page it could not adopt as its
    writable physics page — ACC died with a fatal error at exactly
    that moment, every time. Starting Link after entering the
    session worked because the game's page already existed and the
    call merely attached to it. Now we attach the way ac_bridge
    does: OpenFileMappingW only. Absence (game not running, or in
    menus) fails cleanly, leaves NO footprint of any kind, and is
    retried on a slow poll — Link's startup is fully passive.
    """

    _READ_LEN = 32      # the head fields end at speedKmh (@28 + 4)
    _RETRY_S = 5.0

    def __init__(self):
        self._handle = None   # mapping handle from OpenFileMappingW
        self._view = None     # address from MapViewOfFile
        self._tried = 0.0
        self.gas = []    # (t, 0..1)
        self.brake = []  # (t, 0..1)
        self.speed = []  # (t, km/h)
        self.available = False

    @staticmethod
    def _kernel32():
        k32 = ctypes.windll.kernel32
        # c_void_p restypes: 64-bit handles/addresses must not be
        # truncated to C int by ctypes' default restype.
        k32.OpenFileMappingW.restype = ctypes.c_void_p
        k32.OpenFileMappingW.argtypes = [
            ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p]
        k32.MapViewOfFile.restype = ctypes.c_void_p
        k32.MapViewOfFile.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_size_t]
        k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        return k32

    def _open(self):
        # Named shared memory is a Windows facility; off Windows (dev
        # runs) there is simply no reference — never an error.
        if os.name != "nt":
            return
        FILE_MAP_READ = 0x0004
        try:
            k32 = self._kernel32()
            # OpenFileMapping ONLY — if the game hasn't created the
            # page this returns NULL, and that absence is the normal
            # pre-session state, not an error.
            handle = k32.OpenFileMappingW(
                FILE_MAP_READ, False, "acpmf_physics")
            if not handle:
                return
            # Size 0 maps the whole page, whatever size the GAME
            # made it; we only ever read the 32-byte head.
            view = k32.MapViewOfFile(handle, FILE_MAP_READ, 0, 0, 0)
            if not view:
                k32.CloseHandle(handle)
                return
            self._handle, self._view = handle, view
        except Exception:
            self._handle = self._view = None

    def _close(self):
        if os.name == "nt" and (self._view or self._handle):
            try:
                k32 = self._kernel32()
                if self._view:
                    k32.UnmapViewOfFile(self._view)
                if self._handle:
                    k32.CloseHandle(self._handle)
            except Exception:
                pass
        self._handle = self._view = None

    def update(self, now):
        if self._view is None:
            if now - self._tried < self._RETRY_S:
                return
            self._tried = now
            self._open()
            if self._view is None:
                self.available = False
                return
        try:
            raw = ctypes.string_at(self._view, self._READ_LEN)
            gas = struct.unpack_from("<f", raw, 4)[0]
            brake = struct.unpack_from("<f", raw, 8)[0]
            speed = struct.unpack_from("<f", raw, 28)[0]
        except Exception:
            self._close()
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
        # Diagnostics (v2.6): the mapper's current learning picture —
        # stage + reason class + live candidates — stashed by every
        # _evaluate pass for the heartbeat, and emitted as an
        # automap_state event whenever the picture changes materially.
        self._diag_stage = "locked" if self.locked else "no-data"
        self._diag_reason = ("saved map loaded — re-validating"
                             if self.locked else
                             "no axis data observed yet")
        self._diag_candidates = {"steer": None, "pedals": [],
                                 "throttle": None, "brake": None}
        self._state_sig = None
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
        """{channel: {device, axis, deviceVid, devicePid}} for the
        current map — what the funnel (and a human debugging a rig)
        actually needs to see: WHICH physical device each channel
        lives on, identified past the display name (v2.6)."""
        out = {}
        for name, ch in (self.mapping or {}).get("channels", {}).items():
            ax = ch["axis"]
            rec = self.devices.get(ch["device"]) or {}
            out[name] = {
                "device": ch.get("deviceName")
                          or self.names.get(ch["device"]),
                "deviceIndex": ch["device"],
                "deviceVid": ch.get("deviceVid", rec.get("vid")),
                "devicePid": ch.get("devicePid", rec.get("pid")),
                "axis": AXIS_NAMES[ax] if 0 <= ax < len(AXIS_NAMES)
                        else ax,
            }
        return out

    # --------------------------------------------- diagnostics (v2.6) --

    def _cand(self, key, f=None, gas=None, brake=None):
        """One candidate axis as the funnel should see it: device
        identity (name + index + VID/PID), axis, and its evidence."""
        dev = key[0]
        rec = self.devices.get(dev) or {}
        out = {"device": self.names.get(dev),
               "deviceIndex": dev,
               "deviceVid": rec.get("vid"),
               "devicePid": rec.get("pid"),
               "axis": AXIS_NAMES[key[1]]
                       if 0 <= key[1] < len(AXIS_NAMES) else key[1]}
        if f is not None:
            out["range"] = round(f["range"], 3)
            out["duty"] = round(f["duty"], 3)
        if gas is not None:
            out["gasCorr"] = round(gas, 3)
        if brake is not None:
            out["brakeCorr"] = round(brake, 3)
        return out

    def _set_diag(self, stage, reason, steer=None, pedals=None,
                  throttle=None, brake=None):
        self._diag_stage = stage
        self._diag_reason = reason
        self._diag_candidates = {"steer": steer,
                                 "pedals": pedals or [],
                                 "throttle": throttle, "brake": brake}
        self._note_state()

    def _state_signature(self):
        c = self._diag_candidates

        def k(cand):
            return None if cand is None else (cand["deviceIndex"],
                                              cand["axis"])
        return (self._diag_stage, k(c["steer"]),
                tuple(sorted(k(p) for p in c["pedals"])),
                k(c["throttle"]), k(c["brake"]))

    def _note_state(self):
        """automap_state funnel event (v2.6): the moment the mapper's
        candidate picture changes materially — first candidate seen,
        a candidate set lost, the stage moving — report it WITH the
        reason class, so "why won't it lock" is answerable remotely.
        Streak ticks alone are not material and do not emit."""
        sig = self._state_signature()
        if sig == self._state_sig:
            return
        self._state_sig = sig
        self._emit_event({
            "event": "automap_state",
            "hidDevice": self.device_label(),
            "calibrated": self.locked,
            **self.diagnostics(),
        })

    def diagnostics(self):
        """The mapper's current picture, for the funnel heartbeat and
        automap_state events (v2.6): locked or not; if not, what
        stage learning is at and WHY (the reason class), plus the
        live candidates with their evidence."""
        out = {
            "locked": bool(self.locked),
            "stage": "locked" if self.locked else self._diag_stage,
            "reason": ("map locked" if self.locked
                       else self._diag_reason),
            "streak": self._streak,
            "candidates": self._diag_candidates,
        }
        if self.locked:
            out["axisMap"] = self.axis_map_summary()
        return out

    def axis_activity(self, now, window_s=60.0):
        """Per-axis recent activity for the heartbeat (v2.6): for
        every (device, axis) the mapper has been watching, sample
        count, min/max and variance over the last window — values
        only, no raw firehose. A pedal axis that never moves shows
        up as var 0.0 (or is absent entirely, which says the device
        it lives on was never read — the inventory separates those
        two stories)."""
        out = {}
        for (dev, ax), s in self.series.items():
            vals = [v for t, v in s if t > now - window_s]
            if len(vals) < 2:
                continue
            mean = sum(vals) / len(vals)
            var = sum((v - mean) ** 2 for v in vals) / len(vals)
            name = self.names.get(dev) or f"Controller {dev + 1}"
            out.setdefault(f"{name}#{dev}", {})[
                AXIS_NAMES[ax] if 0 <= ax < len(AXIS_NAMES) else str(ax)
            ] = {"n": len(vals), "min": round(min(vals), 3),
                 "max": round(max(vals), 3), "var": round(var, 4)}
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
            self._set_diag("no-data", "no axis data observed yet")
            return
        t0 = min(s[0][0] for s in self.series.values() if s)
        if now - t0 < self.MIN_OBSERVE_S:
            self._set_diag(
                "observing",
                f"observing — {int(now - t0)}s of "
                f"{int(self.MIN_OBSERVE_S)}s needed before judging axes")
            return
        feats = {}
        for key in list(self.series):
            f = self._features(key)
            if f and f["range"] >= 0.05:      # dead axes never qualify
                feats[key] = f
        if not feats:
            self._set_diag(
                "no-travel",
                "controllers are being read but no axis has moved "
                "(every axis range < 0.05 in the window) — the rig "
                "is sitting still, or the moving axes live on a "
                "device that is not answering")
            return
        driving = (
            (self.motion.speed and max(v for _, v in self.motion.speed) > 25.0)
            or any(f["range"] > 0.3 for f in feats.values())
        )
        if not driving:
            self._set_diag(
                "not-driving",
                "axes move but no driving detected (game speed "
                "≤ 25 km/h and no axis range > 0.3)")
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
            self._set_diag(
                "no-steer-candidate",
                "no axis travels both ways from a returning centre "
                "(steering not identified yet)")
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
            self._set_diag(
                "pedal-candidates<2",
                f"steering found, but only {len(cands)} pedal-like "
                "axis(es) — need 2 (one-sided excursion resting at "
                "an extreme, range ≥ 0.25). If the pedals are a "
                "separate device, its axes are not in the read set",
                steer=self._cand(steer_key, feats[steer_key]),
                pedals=[self._cand(k, f) for k, f in cands])
            return
        (k1, f1), (k2, f2) = cands[0], cands[1]
        cand1 = self._cand(k1, f1)
        cand2 = self._cand(k2, f2)
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
            cand1 = self._cand(k1, f1, gas=g1, brake=b1)
            cand2 = self._cand(k2, f2, gas=g2, brake=b2)
        if thr_key is None:
            # Duty cycle: throttle is worked far more of a lap.
            if abs(f1["duty"] - f2["duty"]) < 0.03:
                self._streak = 0
                self._set_diag(
                    "undecided",
                    "two pedal candidates, but throttle vs brake "
                    "cannot be told apart yet (game correlation "
                    "inconclusive, duty cycles within 0.03)",
                    steer=self._cand(steer_key, feats[steer_key]),
                    pedals=[cand1, cand2])
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
        thr_cand = cand1 if thr_key == k1 else cand2
        brk_cand = cand2 if thr_key == k1 else cand1
        self._set_diag(
            "candidate",
            f"full candidate set (steer + throttle + brake) — "
            f"must win {self.LOCK_STREAK} consecutive evaluations, "
            f"streak {self._streak}/{self.LOCK_STREAK}",
            steer=self._cand(steer_key, feats[steer_key]),
            pedals=[cand1, cand2],
            throttle=thr_cand, brake=brk_cand)
        if self._streak < self.LOCK_STREAK:
            return
        self._lock(pick)

    def _lock(self, pick):
        steer_key, thr_key, brk_key = pick
        chans = {}
        f = self._features(steer_key)
        rec = self.devices.get(steer_key[0]) or {}
        chans["steer"] = _channel(
            steer_key[0], steer_key[1], f["lo"], f["hi"],
            center=f["med"], device_name=self.names.get(steer_key[0]),
            device_vid=rec.get("vid"), device_pid=rec.get("pid"))
        for name, key in (("throttle", thr_key), ("brake", brk_key)):
            f = self._features(key)
            rest, full = (f["lo"], f["hi"]) if f["rest_is_lo"] \
                else (f["hi"], f["lo"])
            rec = self.devices.get(key[0]) or {}
            chans[name] = _channel(
                key[0], key[1], rest, full,
                device_name=self.names.get(key[0]),
                device_vid=rec.get("vid"), device_pid=rec.get("pid"))
        self.mapping = {"channels": chans, "calibrated": True,
                        "auto": True, "version": 2}
        self.locked = True
        self._diag_stage = "locked"
        self._diag_reason = "map locked"
        self._state_sig = self._state_signature()
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
                self._set_diag(
                    "no-data",
                    f"map dropped (mapped {name} flatlined) — "
                    "relearning from live driving")
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
        self._last_sig = None    # last device-set signature posted

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

    def _heartbeat_event(self, label, mapper, devices, now):
        """The 60 s funnel heartbeat payload. v2.7 adds which input
        API is reading the rig — and, when DirectInput failed to
        initialise and the legacy winmm reader took over, a
        "diFallback": true flag so that state is visible from the
        funnel instead of masquerading as a healthy DI rig."""
        ev = {
            "event": "heartbeat",
            "hidDevice": label,
            "calibrated": mapper.locked,
            "devicePresent": True,
            "framesSent": self._frames_sent,
            "deviceCount": len(devices),
            "inputApi": input_api(),
            "axes": mapper.axis_activity(now),
            "mapper": mapper.diagnostics(),
        }
        if di_fallback_active():
            ev["diFallback"] = True
        return ev

    def run(self):
        # NEVER die silently. The v2.2 streamer listed controllers once
        # at startup and exited if none were visible — wheels wake up
        # with the game, not with Windows, so a normal launch order
        # killed pedal streaming for the whole session with no sign
        # anywhere. Now: keep looking, and say what's happening.
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Device inventory (v2.6): post what Windows can enumerate
        # before anything else — including the empty answer — and
        # again whenever the SET changes. In v2.5 a pedal unit that
        # woke after the startup scan was invisible for the whole
        # session; now its arrival is an event, and the stream
        # rebuilds around the fresh union.
        devices = enumerate_devices()
        self._last_sig = device_signature(devices)
        self._emit_event(inventory_event(devices, "startup"))
        while True:
            devices = enumerate_devices()
            sig = device_signature(devices)
            if sig != self._last_sig:
                self._last_sig = sig
                self._emit_event(
                    inventory_event(devices, "device-set change"))
            if not any(d.get("responding", True) for d in devices):
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
        ever_ok = False
        last_hb = time.time()
        # Device-set watch (v2.6): this stream was built from one
        # enumeration. A controller that appears later — pedals
        # waking after the wheel — must JOIN the union mid-session,
        # not wait for the next Link launch.
        entered_sig = device_signature(devices)
        entered_ids = {s[:5] for s in entered_sig}
        last_set_check = time.time()
        pending_sig = None
        while True:
            time.sleep(0.02)  # ~50 Hz
            snapshot = {}
            any_ok = False
            for d in devices:
                axes = _read_device(d)
                snapshot[d["index"]] = axes
                if axes is not None:
                    any_ok = True
            now = time.time()
            if now - last_set_check >= 5.0:
                last_set_check = now
                fresh = enumerate_devices()
                fsig = device_signature(fresh)
                if fsig != entered_sig:
                    if pending_sig == fsig:
                        # Confirmed on two consecutive polls — a
                        # flaky port can't thrash the mapper.
                        if {s[:5] for s in fsig} != entered_ids:
                            self.log("Direct inputs: controller set "
                                     "changed — rescanning.")
                            return  # run() re-enters with the union
                        # Mere answering-state flip: report it, keep
                        # streaming the same union.
                        self._emit_event(inventory_event(
                            fresh, "device-set change"))
                        self._last_sig = fsig
                        entered_sig = fsig
                        pending_sig = None
                    else:
                        pending_sig = fsig
                else:
                    pending_sig = None
            if not any_ok:
                if not ever_ok:
                    # Nothing has answered yet in this stream (a
                    # device enumerated but still waking) — probing
                    # is not a disconnect; keep looking quietly.
                    time.sleep(0.25)
                    continue
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
            ever_ok = True
            mapper.observe(snapshot, now)
            if now - last_hb >= 60.0:
                # Funnel heartbeat (v2.5, extended v2.6/v2.7): proof
                # of life with the numbers a remote diagnosis needs
                # — no payload data; v2.6 adds per-axis activity and
                # the mapper's candidate state, so a silent axis is
                # attributable to a device, an axis, or a stage.
                last_hb = now
                self._emit_event(
                    self._heartbeat_event(label, mapper, devices, now))
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
        axes = _read_device(d)
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
