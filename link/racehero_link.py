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
    exits silently — two copies fight over the same UDP ports (on Windows
    SO_REUSEADDR lets both bind, so the port check below can't catch it)."""
    if os.name != "nt":
        return True
    import ctypes
    kernel32 = ctypes.windll.kernel32
    mutex = kernel32.CreateMutexW(None, False, "Global\\RaceHeroLinkSingleInstance")
    if not mutex:
        return True  # couldn't create the mutex; don't block startup
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(mutex)
        return False
    single_instance._mutex = mutex  # hold for the life of the process
    return True


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
        # No console in the --noconsole build; just leave quietly.
        sys.exit(0)

    pc_name = os.environ.get("COMPUTERNAME", "PC")
    print("=" * 62)
    print("  RaceHero Link v2 — waiting for your phone…")
    print("  On the phone: RaceHero -> RACE SIM -> START SIM LINK")
    print("  (Phone and PC must be on the same WiFi.)")
    print("=" * 62)

    state = load_state()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", LINK_PORT))
    except OSError as e:
        log(f"Can't listen on UDP {LINK_PORT}: {e}")
        log("Is another copy of RaceHero Link already running?")
        sys.exit(1)

    last_ip = state.get("phone_ip")
    if last_ip and valid_ipv4(last_ip):
        log(f"Last known phone IP: {last_ip} — verifying games…")
        ensure_all(last_ip, state)

    log("Listening for the phone beacon… (Ctrl+C to stop)")
    sock.settimeout(5.0)
    while True:
        try:
            data, _ = sock.recvfrom(512)
        except socket.timeout:
            # Re-verify F1 (the game rewrites its XML on exit).
            if last_ip and f1_needs_patch(last_ip) and not game_running(F1_EXE):
                log("F1 24 config changed — re-patching…")
                ensure_all(last_ip, state)
            # Original AC needs its bridge while the game is up.
            maybe_start_bridge()
            continue
        except KeyboardInterrupt:
            break
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            continue
        if not text.startswith(BEACON_PREFIX):
            continue
        ip = text[len(BEACON_PREFIX):].strip()
        if not valid_ipv4(ip):
            continue
        # Acknowledge so the phone shows "PC Link: connected".
        try:
            sock.sendto((ACK_PREFIX + pc_name).encode("utf-8"), (ip, LINK_PORT))
        except OSError:
            pass
        if ip != last_ip:
            log(f"Phone found at {ip} — configuring games…")
            ensure_all(ip, state)
            state["phone_ip"] = ip
            save_state(state)
            last_ip = ip


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
