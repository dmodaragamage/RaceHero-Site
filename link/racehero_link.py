#!/usr/bin/env python3
"""
RaceHero Link v2 — zero-touch sim telemetry enabler (Windows).

You do NOTHING after installing this. It listens on your WiFi for your
phone shouting "I'm here" (RaceHero app -> RACE SIM -> START SIM LINK),
learns the phone's IP by itself, and configures your games automatically:

  * F1 24   -> patches Documents/My Games/F1 24/hardwaresettings/
               hardware_settings_config.xml (UDP on, phone IP, port 20778,
               60 Hz, format 2024). Re-patches if the game rewrites the file.
  * ACC     -> writes Documents/Assetto Corsa Competizione/Config/
               broadcasting.json (broadcast API on, port 9000).
  * AC1     -> the RaceHero AC bridge broadcasts on its own; nothing to do.

Forza Motorsport / Horizon 5 is the ONE exception: its telemetry switch
lives inside the game profile (no file to patch), so it needs ~30 seconds
in-game, once: Settings -> HUD and Gameplay -> DATA OUT -> ON, Car Dash
format, IP = your phone's IP (shown in the app), port 20779.

Install: double-click START_HERE.bat (installs Python first if needed).
It adds itself to Windows startup, so from then on it's invisible:
tap START SIM LINK on the phone and drive.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

LINK_PORT = 20781
BEACON_PREFIX = "RACEHERO_SIM|"
ACK_PREFIX = "RACEHERO_LINK|"
PHONE_PORTS = {"f1": 20778, "forza": 20779, "acc": 20780, "ac1": 20777}
F1_EXE = "F1_24.exe"
AC_EXE = "acs.exe"  # original Assetto Corsa

# Dev-funnel diagnostics (v2.6): Link posts its own lock/device/
# stream events to the founder's funnel so a PC-side problem is
# visible the moment it happens. Best-effort — the Link runs exactly
# the same if the poster module is missing or the network is dead.
try:
    import funnel
    LINK_VERSION = funnel.LINK_VERSION
except Exception:
    funnel = None
    LINK_VERSION = "2.7.2"


# ---------------------------------------------------------------- state ---

def state_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "RaceHero")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "link_state.json")


def load_state():
    try:
        with open(state_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        with open(state_path(), "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except OSError:
        pass


# --------------------------------------------------------------- helpers ---

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def documents():
    return os.path.join(os.path.expanduser("~"), "Documents")


def valid_ipv4(s):
    try:
        socket.inet_aton(s)
        return s.count(".") == 3
    except OSError:
        return False


def backup(path):
    bak = path + ".racehero.bak"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
        log(f"Backup saved: {bak}")


def _no_window():
    """creationflags that keep console tools (tasklist) from flashing a
    window when spawned from our windowless exe."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def game_running(exe_name):
    """True if a process with this image name is alive (Windows)."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=_no_window(),
        ).stdout
        return exe_name.lower() in out.lower()
    except (OSError, subprocess.SubprocessError):
        return False


def find_bridge():
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (
        os.path.join(here, "ac_bridge.py"),
        os.path.normpath(os.path.join(here, "..", "ac-bridge", "ac_bridge.py")),
    ):
        if os.path.exists(cand):
            return cand
    return None


def bridge_pid_path():
    return os.path.join(os.path.dirname(state_path()), "bridge.pid")


def bridge_alive():
    try:
        with open(bridge_pid_path(), "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=_no_window(),
        ).stdout
        return str(pid) in out
    except (OSError, subprocess.SubprocessError):
        return False


def bridge_python():
    if getattr(sys, "frozen", False):
        return shutil.which("pythonw") or shutil.which("python")
    return sys.executable


def maybe_start_bridge():
    """Original Assetto Corsa can't emit UDP itself: while acs.exe is alive,
    keep the shared-memory -> UDP bridge running silently in the background.

    The bridge is this same program re-launched with --bridge, so a frozen
    install needs no Python on the PC and Task Manager shows one app."""
    if not game_running(AC_EXE) or bridge_alive():
        return
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--bridge", "--port", str(PHONE_PORTS["ac1"])]
    else:
        bridge = find_bridge()
        if not bridge:
            log("Assetto Corsa is running but ac_bridge.py wasn't found next "
                "to racehero_link.py — bridge not started.")
            return
        cmd = [sys.executable, bridge, "--port", str(PHONE_PORTS["ac1"])]
    try:
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw
        )
        with open(bridge_pid_path(), "w", encoding="utf-8") as f:
            f.write(str(p.pid))
        log("Assetto Corsa detected — telemetry bridge started in background.")
    except OSError as e:
        log(f"Could not start the AC bridge: {e}")


# ------------------------------------------------------------------ F1 ---

def f1_xml_path():
    return os.path.join(
        documents(), "My Games", "F1 24", "hardwaresettings",
        "hardware_settings_config.xml",
    )


def f1_needs_patch(phone_ip):
    """True if the F1 config is missing or points at the wrong IP."""
    path = f1_xml_path()
    if not os.path.exists(path):
        return False  # game not installed; nothing to do
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            xml = f.read()
    except OSError:
        return False
    m = re.search(r'<udp\b[^>]*>', xml)
    if not m:
        return True
    tag = m.group(0)
    return not (
        'enabled="true"' in tag
        and f'ip="{phone_ip}"' in tag
        and f'port="{PHONE_PORTS["f1"]}"' in tag
    )


def patch_f1(phone_ip):
    path = f1_xml_path()
    if not os.path.exists(path):
        return "not installed"
    if game_running(F1_EXE):
        return "deferred (game is running — will patch when it's closed)"
    backup(path)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            xml = f.read()
    except OSError as e:
        return f"failed ({e})"
    new_tag = (
        f'<udp enabled="true" ip="{phone_ip}" port="{PHONE_PORTS["f1"]}" '
        f'sendRate="60" format="2024" />'
    )
    if re.search(r"<udp\b[^>]*>", xml):
        xml = re.sub(r"<udp\b[^>]*>", new_tag, xml, count=1)
    elif "<motion_platform" in xml:
        xml = xml.replace("<motion_platform>",
                          "<motion_platform>\n    " + new_tag, 1)
    else:
        return "failed (no <udp> or <motion_platform> element)"
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(xml)
    except OSError as e:
        return f"failed ({e})"
    return f"patched -> {phone_ip}:{PHONE_PORTS['f1']}"


# ----------------------------------------------------------------- ACC ---

def patch_acc():
    cfg_dir = os.path.join(documents(), "Assetto Corsa Competizione", "Config")
    if not os.path.isdir(cfg_dir):
        return "not installed"
    path = os.path.join(cfg_dir, "broadcasting.json")
    want = {"updListenerPort": 9000, "connectionPassword": "",
            "commandPassword": ""}
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                cur = json.load(f)
            if cur.get("updListenerPort") == 9000:
                return "already on"
            backup(path)
    except (OSError, ValueError):
        pass
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(want, f, indent=4)
    except OSError as e:
        return f"failed ({e})"
    return "enabled (port 9000)"


# --------------------------------------------------------------- ensure ---

def ensure_all(phone_ip, state, quiet=False):
    """Patch/verify every game for this phone IP. Returns True if all good."""
    results = {}
    results["F1 24"] = patch_f1(phone_ip)
    results["ACC"] = patch_acc()
    ok = True
    for game, res in results.items():
        if res in ("not installed", "already on") or res.startswith("patched"):
            continue
        ok = False
        if not quiet:
            log(f"{game}: {res}")
    if not quiet:
        for game, res in results.items():
            if res not in ("not installed",):
                log(f"{game}: {res}")
    if not state.get("forza_hint_shown"):
        log("Forza Motorsport/Horizon needs 30 seconds in-game, ONCE:")
        log("  Settings -> HUD and Gameplay -> DATA OUT -> ON, Car Dash,")
        log(f"  IP = {phone_ip} (shown in the RaceHero app), "
            f"port {PHONE_PORTS['forza']}.")
        state["forza_hint_shown"] = True
        save_state(state)
    return ok


# -------------------------------------------------------------- install ---

def install_startup():
    """Add a startup entry so Link runs silently at every boot."""
    if os.name != "nt":
        print("Automatic startup install is Windows-only.")
        return False
    startup = os.path.join(
        os.environ.get("APPDATA", ""),
        r"Microsoft\Windows\Start Menu\Programs\Startup",
    )
    if not os.path.isdir(startup):
        print(f"Startup folder not found: {startup}")
        return False
    bat = os.path.join(startup, "RaceHero Link.bat")
    script = os.path.abspath(__file__)
    with open(bat, "w", encoding="utf-8") as f:
        f.write(f'@echo off\r\nstart "" /min pythonw "{script}"\r\n')
    print(f"Installed: {bat}")
    print("RaceHero Link will now start silently with Windows.")
    return True


# ----------------------------------------------------------------- main ---

def single_instance():
    """True if this is the only copy running. On Windows a second launch
    exits with a message — two copies fight over the same UDP ports (on
    Windows SO_REUSEADDR lets both bind, so the port check below can't
    catch it).

    v2.7.2 hardening: the v2.7.1 version called CreateMutexW through a
    bare ctypes.windll handle — no restype (the 64-bit HANDLE came back
    truncated to a 32-bit int) and the last-error read went through a
    second, separately-wrapped call that ctypes is free to interleave
    with its own bookkeeping. The check now goes through a WinDLL
    handle created with use_last_error=True, declares the HANDLE
    return type properly, and reads the error via get_last_error() —
    the one pattern ctypes guarantees. If the check itself cannot run
    it still fails open (a Link that runs beats a Link that won't
    start), but it now LOGS that it failed instead of staying silent."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        mutex = kernel32.CreateMutexW(
            None, False, "Global\\RaceHeroLinkSingleInstance")
        if not mutex:
            log("Single-instance check unavailable (CreateMutexW "
                f"failed, error {ctypes.get_last_error()}) — "
                "continuing without it.")
            return True
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(mutex)
            return False
        single_instance._mutex = mutex  # hold for the life of the process
        return True
    except Exception as e:
        log(f"Single-instance check unavailable ({e}) — continuing "
            "without it.")
        return True


# ----------------------------------------------------------------- tray ---

def _tray_image():
    """The app icon for the tray: decode the shipped .ico when it can
    be found; a plain two-tone square otherwise — art must never stop
    the tray (or the Link) from starting."""
    from PIL import Image
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "racehero.ico"),
                 os.path.join(here, "..", "racehero.ico")):
        try:
            if os.path.exists(cand):
                return Image.open(cand)
        except Exception:
            pass
    img = Image.new("RGB", (64, 64), (12, 12, 14))
    for x in range(64):
        for y in range(64):
            if 10 <= x <= 53 and 26 <= y <= 37:
                img.putpixel((x, y), (255, 122, 0))
    return img


def start_tray(status_fn, log):
    """System-tray presence: version + live mapping status + Exit.
    There is deliberately NO action item for the pedals — mapping is
    automatic; the tray only REPORTS where it stands. Best-effort:
    any failure here (no pystray in a dev run, no desktop session)
    leaves the Link running exactly as before."""
    try:
        import threading
        import pystray

        def on_exit(icon, _item):
            icon.stop()
            os._exit(0)

        menu = pystray.Menu(
            pystray.MenuItem(f"RaceHero Link v{LINK_VERSION}", None,
                             enabled=False),
            pystray.MenuItem(lambda _item: status_fn(), None,
                             enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", on_exit),
        )
        icon = pystray.Icon("racehero-link", _tray_image(),
                            "RaceHero Link", menu)
        t = threading.Thread(target=icon.run, name="racehero-tray",
                             daemon=True)
        t.start()
        log("Tray icon up — right-click it for mapping status.")
        return icon
    except Exception as e:
        log(f"Tray icon unavailable ({e}) — Link continues without it.")
        return None


# ------------------------------------------------------------- discovery ---

class Discovery:
    """PC <-> phone discovery state machine (v2.7.2).

    The v2.7.1 loop was single-shot and lossy: it ACKed a beacon only
    when one happened to arrive, announced itself never, and treated
    the saved phone IP as a permanent fact — the HID streamer kept
    "sending" frames to it (framesSent climbing) long after the phone
    had left that address, while the phone sat at "PC Link: waiting"
    and nothing anywhere recorded a beacon arriving or not arriving.
    One lost beacon/ACK pair, one stale DHCP lease, one extra copy of
    Link holding the port — any of them stranded the system in
    "PC alive, phone dark" until a human restarted something.

    This class makes every piece of that state self-healing:

      * ACK — every beacon is answered at the packet's SOURCE address
        (where the phone's socket provably is), not only at the
        address the beacon text claims; a claim/source mismatch is
        logged and posted, never silently trusted.
      * PRESENCE — Link broadcasts "RACEHERO_LINK|<pc>" on a timer.
        The phone treats any such packet as the acknowledgement it is
        waiting for, so the phone can (re)connect even if every
        phone->PC beacon is being dropped on the inbound path:
        discovery no longer depends on the inbound path at all.
      * EXPIRY — the phone beacons every ~2 s for as long as it is
        listening, so a silent phone is a gone (or re-addressed)
        phone: after PHONE_IP_TTL_S of beacon silence the saved IP
        is dropped, the streamer stops blasting a dead address, and
        the next beacon — from whatever address — re-registers
        instantly. Presence broadcasts continue either way.
      * OBSERVABILITY — phone_found / beacon_resumed /
        beacon_ip_mismatch / phone_ip_expired go to the dev funnel,
        and diag() feeds phoneIp / lastBeaconAgeS / beaconCount into
        every streamer heartbeat. A dead discovery path is now a
        visible state with an age, not an absence to guess at.
    """

    PRESENCE_EVERY_S = 10.0     # broadcast presence cadence
    PHONE_IP_TTL_S = 180.0      # beacon silence before the IP expires
    RESUME_GAP_S = 30.0         # beacon gap worth a funnel event

    def __init__(self, pc_name, on_phone_ip, log=print, emit=None,
                 broadcast_addr="255.255.255.255", clock=time.monotonic):
        self.pc_name = pc_name
        self.on_phone_ip = on_phone_ip  # fn(ip_or_None, reason)
        self.log = log
        self.emit = emit                # funnel.post or None
        self.broadcast_addr = broadcast_addr
        self.clock = clock
        self.phone_ip = None
        self.started_at = clock()
        self.last_beacon_at = None     # clock() of the last beacon
        self.beacon_count = 0
        self._last_presence_at = 0.0

    # -- wiring ------------------------------------------------------

    def seed(self, saved_ip):
        """Adopt the phone IP saved by a previous run — as a hint.
        It streams immediately (the common case: same phone, same
        lease), but the expiry clock runs from process start until a
        real beacon confirms it."""
        if saved_ip and valid_ipv4(saved_ip):
            self.phone_ip = saved_ip

    def _post(self, event):
        if self.emit is None:
            return
        try:
            self.emit(event)
        except Exception:
            pass

    # -- events ------------------------------------------------------

    def handle(self, data, addr, sock):
        """One datagram on the discovery socket. Returns True if it
        was a phone beacon (and has been ACKed)."""
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return False
        if not text.startswith(BEACON_PREFIX):
            return False
        now = self.clock()
        claimed = text[len(BEACON_PREFIX):].strip()
        source = addr[0] if addr else None
        # The packet's source address is ground truth on a LAN; the
        # claimed address is what the phone BELIEVES its IP is. They
        # agree on any healthy network — when they don't, trust the
        # wire and say so.
        if source and valid_ipv4(source):
            effective = source
        elif valid_ipv4(claimed):
            effective = claimed
        else:
            return False
        gap = None if self.last_beacon_at is None \
            else now - self.last_beacon_at
        self.last_beacon_at = now
        self.beacon_count += 1
        if valid_ipv4(claimed) and source and claimed != source:
            self.log(f"Beacon claims {claimed} but arrived from "
                     f"{source} — trusting the wire.")
            self._post({"event": "beacon_ip_mismatch",
                        "claimedIp": claimed, "sourceIp": source})
        # Acknowledge so the phone shows "PC Link: connected" — at the
        # source address, and at the claimed one too when they differ
        # (the phone listens on all its interfaces).
        ack = (ACK_PREFIX + self.pc_name).encode("utf-8")
        for target in {effective, claimed}:
            if target and valid_ipv4(target):
                try:
                    sock.sendto(ack, (target, LINK_PORT))
                except OSError:
                    pass
        if effective != self.phone_ip:
            prev = self.phone_ip
            self.phone_ip = effective
            self._post({"event": "phone_found", "phoneIp": effective,
                        "prevIp": prev, "beaconCount": self.beacon_count})
            self.on_phone_ip(effective, "beacon")
        elif gap is not None and gap >= self.RESUME_GAP_S:
            self._post({"event": "beacon_resumed", "phoneIp": effective,
                        "gapS": round(gap, 1),
                        "beaconCount": self.beacon_count})
        return True

    def tick(self, sock):
        """Timer housekeeping, called on every receive timeout:
        presence broadcasts + saved-IP expiry."""
        now = self.clock()
        if now - self._last_presence_at >= self.PRESENCE_EVERY_S:
            self._last_presence_at = now
            presence = (ACK_PREFIX + self.pc_name).encode("utf-8")
            try:
                sock.sendto(presence, (self.broadcast_addr, LINK_PORT))
            except OSError:
                pass
            if self.phone_ip:
                # Unicast as well: broadcast frames are the first
                # thing a busy AP drops; the phone we already know
                # gets a direct copy.
                try:
                    sock.sendto(presence, (self.phone_ip, LINK_PORT))
                except OSError:
                    pass
        if self.phone_ip:
            ref = self.last_beacon_at \
                if self.last_beacon_at is not None else self.started_at
            age = now - ref
            if age > self.PHONE_IP_TTL_S:
                expired = self.phone_ip
                self.phone_ip = None
                self.log(f"No beacon from {expired} for "
                         f"{age:.0f}s — forgetting that address; "
                         "the phone will re-register itself.")
                self._post({"event": "phone_ip_expired",
                            "phoneIp": expired,
                            "beaconAgeS": round(age, 1)})
                self.on_phone_ip(None, "expired")

    # -- diagnostics ---------------------------------------------------

    def diag(self):
        """Discovery state for the streamer heartbeat (v2.7.2)."""
        age = None
        if self.last_beacon_at is not None:
            age = round(self.clock() - self.last_beacon_at, 1)
        return {"phoneIp": self.phone_ip,
                "lastBeaconAgeS": age,
                "beaconCount": self.beacon_count}


def main():
    if "--bridge" in sys.argv:
        # AC1 shared-memory bridge mode: same binary, no mutex, no listener.
        sys.argv.remove("--bridge")
        import ac_bridge
        ac_bridge.main()
        return

    if "--install" in sys.argv:
        ok = install_startup()
        sys.exit(0 if ok else 1)

    if not single_instance():
        # SAY something. Quietly exiting here is how an old copy in the
        # tray silently blocked every newer version from ever starting
        # (no pedal streamer, no fixes) while looking "installed".
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None,
                "RaceHero Link is already running (check the system tray).\n\n"
                "If that's an older version, right-click its tray icon "
                "and Exit it first, then launch this one again — "
                "otherwise the old copy keeps running and the new one "
                "never starts.",
                "RaceHero Link", 0x40)
        except Exception:
            pass
        sys.exit(0)

    pc_name = os.environ.get("COMPUTERNAME", "PC")
    print("=" * 62)
    print(f"  RaceHero Link v{LINK_VERSION} — waiting for your phone…")
    print("  On the phone: RaceHero -> RACE SIM -> START SIM LINK")
    print("  (Phone and PC must be on the same WiFi.)")
    print("=" * 62)

    state = load_state()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    # v2.7.2: NO SO_REUSEADDR on this socket, and on Windows ask for
    # SO_EXCLUSIVEADDRUSE. The old flag let a second copy of Link bind
    # UDP 20781 alongside the first and quietly split the phone's
    # beacons between them — one copy heartbeating happily to the
    # funnel while the other ate the discovery traffic is the exact
    # shape of the v2.7.1 field failure. Exclusive bind makes a
    # conflict loud (bind error below) instead of silent.
    if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        try:
            sock.setsockopt(socket.SOL_SOCKET,
                            socket.SO_EXCLUSIVEADDRUSE, 1)
        except OSError:
            pass
    bind_error = None
    for _attempt in range(10):
        try:
            sock.bind(("", LINK_PORT))
            bind_error = None
            break
        except OSError as e:
            bind_error = e
            # A previous copy may still be exiting (the installer
            # kills it just before launching this one) — give it a
            # few seconds before calling it a conflict.
            time.sleep(1.5)
    if bind_error is not None:
        log(f"Can't listen on UDP {LINK_PORT}: {bind_error}")
        log("Is another copy of RaceHero Link already running?")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None,
                "RaceHero Link could not start listening for your "
                "phone — another copy of RaceHero Link appears to "
                "be holding the connection.\n\n"
                "Right-click every RaceHero Link tray icon and Exit "
                "them, then launch RaceHero Link again.",
                "RaceHero Link", 0x40)
        except Exception:
            pass
        sys.exit(1)

    last_ip = state.get("phone_ip")
    if last_ip and valid_ipv4(last_ip):
        log(f"Last known phone IP: {last_ip} — verifying games…")
        ensure_all(last_ip, state)

    # Direct wheel/pedal capture: streams the driver's raw inputs to
    # the phone (feeds like ACC broadcast carry none). The axes map
    # THEMSELVES (v2.4 auto-mapping — the driver never calibrates
    # anything). Best-effort — any failure here must never take the
    # Link itself down.
    ip_holder = {"ip": last_ip if last_ip and valid_ipv4(last_ip) else None}

    # Discovery state machine (v2.7.2): owns the phone IP from here
    # on. Its callback is the ONLY writer of ip_holder/state — a
    # beacon confirms or replaces the address, expiry clears it.
    def _on_phone_ip(ip, reason):
        nonlocal last_ip
        if ip is None:
            ip_holder["ip"] = None
            last_ip = None
            state.pop("phone_ip", None)
            save_state(state)
            return
        ip_holder["ip"] = ip
        if ip != last_ip:
            log(f"Phone found at {ip} — configuring games…")
            ensure_all(ip, state)
            state["phone_ip"] = ip
            save_state(state)
            last_ip = ip

    disc = Discovery(
        pc_name, _on_phone_ip, log,
        emit=(funnel.post if funnel is not None else None))
    disc.seed(last_ip)

    streamer = None
    try:
        import hid_inputs
        streamer = hid_inputs.start_streamer(
            lambda: ip_holder["ip"], log,
            emit=(funnel.post if funnel is not None else None),
            get_link_diag=disc.diag)
    except Exception as e:
        log(f"Direct input capture unavailable: {e}")

    # Funnel poster starts after the streamer so a saved map's
    # "loaded" event is already queued and goes out with link_start.
    if funnel is not None:
        funnel.start(log)
        funnel.post({"event": "link_start", "phoneIp": last_ip})

    # Tray: version + mapping status ("Pedals: auto-mapped" /
    # "Pedals: learning — drive a lap"). Status only — no actions
    # exist for the driver to take.
    start_tray(
        lambda: streamer.mapper_status() if streamer is not None
        else "Pedals: capture unavailable",
        log)

    log("Listening for the phone beacon… (Ctrl+C to stop)")
    sock.settimeout(2.0)
    while True:
        try:
            data, addr = sock.recvfrom(512)
        except socket.timeout:
            # Discovery self-healing (v2.7.2): broadcast presence so
            # the phone can find us even if its beacons are being
            # dropped inbound, and expire a phone IP that has gone
            # beacon-silent. Runs on the same tick as the game-config
            # self-healing below — nothing in this loop ever settles
            # into a dead state and waits for a human.
            disc.tick(sock)
            cur_ip = ip_holder["ip"]
            # Re-verify F1 (the game rewrites its XML on exit).
            if cur_ip and f1_needs_patch(cur_ip) and not game_running(F1_EXE):
                log("F1 24 config changed — re-patching…")
                ensure_all(cur_ip, state)
            # Self-healing: re-verify ACC's broadcast config on the same
            # tick (it's a cheap file read when already correct). If the
            # game or a cleanup tool rewrote broadcasting.json, the feed
            # would otherwise stay dead until the Link happened to
            # restart — customers shouldn't need to know any of this.
            acc_res = patch_acc()
            if acc_res.startswith("enabled"):
                log(f"ACC: {acc_res} (config restored)")
            # Original AC needs its bridge while the game is up.
            maybe_start_bridge()
            continue
        except KeyboardInterrupt:
            break
        disc.handle(data, addr, sock)

    if funnel is not None:
        funnel.post({"event": "link_stop"})


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
