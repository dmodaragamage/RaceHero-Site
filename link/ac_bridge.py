#!/usr/bin/env python3
"""
RaceCoach Assetto Corsa bridge (Windows).

Reads Assetto Corsa's shared-memory telemetry (acpmf_physics / acpmf_graphics)
and broadcasts it as JSON over UDP so the RaceCoach phone app can coach a
sim session live: lap/sector timing, voice cues, AI debrief — all the normal
features, no GPS needed.

Run on the gaming PC while Assetto Corsa is on track:
    python ac_bridge.py [--port 20777] [--hz 20]

Phone and PC must be on the same network (WiFi, or USB tethering — the
packets don't care). In the RaceCoach app: Live tab -> SIM RACING.

Notes from the field (learned the hard way by others):
- mmap with tagname=... CREATES a missing map; we use OpenFileMappingW so
  "game not running" is a clean, detectable state.
- Structs are declared with ctypes (not hand offsets): the graphics struct
  has wchar arrays, so the C compiler pads and hand-computed offsets read
  garbage. ctypes mirrors the alignment for free.
"""
import argparse
import ctypes
import json
import socket
import sys
import time

# ---------------------------------------------------------------- structs
# SPageFilePhysics (head only — up to accG, the last field we read).

class SPageFilePhysics(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packetId", ctypes.c_int),
        ("gas", ctypes.c_float),
        ("brake", ctypes.c_float),
        ("fuel", ctypes.c_float),
        ("gear", ctypes.c_int),
        ("rpms", ctypes.c_int),
        ("steerAngle", ctypes.c_float),
        ("speedKmh", ctypes.c_float),
        ("velocity", ctypes.c_float * 3),
        ("accG", ctypes.c_float * 3),  # x=lateral, y=vertical, z=longitudinal
    ]


class SPageFileGraphic(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packetId", ctypes.c_int),
        ("status", ctypes.c_int),   # 0=off 1=replay 2=live 3=pause
        ("session", ctypes.c_int),  # 1=practice 2=quali 3=race ...
        ("currentTime", ctypes.c_wchar * 15),
        ("lastTime", ctypes.c_wchar * 15),
        ("bestTime", ctypes.c_wchar * 15),
        ("split", ctypes.c_wchar * 15),
        ("completedLaps", ctypes.c_int),
        ("position", ctypes.c_int),
        ("iCurrentTime", ctypes.c_int),
        ("iLastTime", ctypes.c_int),
        ("iBestTime", ctypes.c_int),
        ("sessionTimeLeft", ctypes.c_float),
        ("distanceTraveled", ctypes.c_float),
        ("isInPit", ctypes.c_int),
        ("currentSectorIndex", ctypes.c_int),
        ("lastSectorTime", ctypes.c_int),
        ("numberOfLaps", ctypes.c_int),
        ("tyreCompound", ctypes.c_wchar * 33),
        ("replayTimeMultiplier", ctypes.c_float),
        ("normalizedCarPosition", ctypes.c_float),  # 0..1 around the lap
        ("carCoordinates", ctypes.c_float * 3),     # x, y, z in metres
    ]


class SPageFileStatic(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("smVersion", ctypes.c_wchar * 15),
        ("acVersion", ctypes.c_wchar * 15),
        ("numberOfSessions", ctypes.c_int),
        ("numCars", ctypes.c_int),
        ("carModel", ctypes.c_wchar * 33),
        ("track", ctypes.c_wchar * 33),
        ("playerName", ctypes.c_wchar * 33),
        ("playerSurname", ctypes.c_wchar * 33),
        ("playerNick", ctypes.c_wchar * 33),
    ]


# ---------------------------------------------------------------- shm open

def open_shm(name, struct_type):
    """Open an existing shared-memory mapping (never create)."""
    FILE_MAP_READ = 0x4
    h = ctypes.windll.kernel32.OpenFileMappingW(FILE_MAP_READ, False, name)
    if not h:
        raise FileNotFoundError(
            f"Shared memory '{name}' not found — is Assetto Corsa on track?"
        )
    addr = ctypes.windll.kernel32.MapViewOfFile(
        h, FILE_MAP_READ, 0, 0, ctypes.sizeof(struct_type)
    )
    if not addr:
        raise RuntimeError(f"MapViewOfFile failed for '{name}'")
    return ctypes.cast(addr, ctypes.POINTER(struct_type)).contents


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=20777)
    ap.add_argument("--hz", type=float, default=20.0)
    args = ap.parse_args()

    try:
        physics = open_shm("acpmf_physics", SPageFilePhysics)
        graphics = open_shm("acpmf_graphics", SPageFileGraphic)
        static = open_shm("acpmf_static", SPageFileStatic)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    dest = ("255.255.255.255", args.port)
    dt = 1.0 / args.hz

    track = static.track
    car = static.carModel
    print(f"RaceCoach bridge live: {car} @ {track} -> UDP {args.port} @ {args.hz} Hz")
    print("Waiting for the car to be on track (status=2)...")

    was_live = False
    while True:
        try:
            live = graphics.status == 2
            if live and not was_live:
                print("On track — streaming.")
            elif not live and was_live:
                print("Not on track — paused.")
            was_live = live
            if not live:
                time.sleep(0.5)
                continue

            pkt = {
                "game": "ac",
                "t": int(time.time() * 1000),
                "x": graphics.carCoordinates[0],
                "z": graphics.carCoordinates[2],
                "speedKmh": physics.speedKmh,
                "gear": physics.gear,
                "rpm": physics.rpms,
                "brake": physics.brake,
                "throttle": physics.gas,
                "gLat": physics.accG[0],
                "gLon": physics.accG[2],
                "lap": graphics.completedLaps + 1,
                "track": track,
                "car": car,
            }
            sock.sendto(json.dumps(pkt).encode("utf-8"), dest)
            time.sleep(dt)
        except KeyboardInterrupt:
            print("\nStopped.")
            break


if __name__ == "__main__":
    main()
