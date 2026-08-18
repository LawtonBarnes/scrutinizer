#!/usr/bin/env python3
"""System Health Monitor + App Menu for the Metal Shop Pi.

The boot-default "home" screen -- amber-CRT/MS-DOS-monitor style CPU/WiFi
dashboard, box-drawn with the IBM VGA CP437 font. Same headless-pygame +
direct-/dev/fb0 + evdev architecture as bars.py/loudness (see bars.py's
module docstring for the full rationale: SDL's kmsdrm driver doesn't
survive composite output, so pygame here only builds surfaces/fonts, the
framebuffer is written to directly, and the keyboard is read via evdev
instead of through SDL).

This single process owns the health dashboard and the app-selector list
(formerly a separate `menu.py` process, then a separate `self.screen`
mode, now just page index MENU_PAGE_INDEX of `self.current_page` as of
the 2026-08-15 McBrain navigation redesign -- see PAGE_COUNT/
MENU_PAGE_INDEX). Nothing here launches a child process to switch pages;
that's a holdover concern from when the dashboard and app-selector were
two separate programs handing the console back and forth via
subprocess.run(), which meant a visible pause and text-console flash on
every switch (process startup, font reloading, KD_TEXT/KD_GRAPHICS
transitions). Only the real apps (bars/loudness/channel38/weatherstar)
still get launched as actual subprocesses via launch_app() -- see that
method -- or assigned to a remote puppet via assign_to_puppet().

Font: body text uses VCR_OSD_MONO_1.001.ttf (same font as bars/loudness/
channel38, switched 2026-08-14 for legibility -- was Px437_IBM_VGA_9x16.ttf).
VCR OSD MONO has no Unicode box-drawing glyphs (checked directly: rendering
BOX_H/BOX_V/corners with it matches a known-missing-glyph's bounding box
exactly), so panel borders still render with Px437_IBM_VGA_9x16.ttf via a
second, separately-sized _border_font -- see draw_panel_frame(). Both fonts
are square-pixel ("Px"/non-aspect-corrected) variants, not aspect-corrected
("Ac") ones -- FrameBuffer.write_surface() below already stretches this
app's 720x480 canvas to whatever the real framebuffer resolution is (a 4:3
target), which already performs the NTSC-style non-square-pixel correction;
an Ac font here would double-correct. Re-check this once ever run on real
composite/CRT output where that stretch may not happen at all.
"""
import fcntl
import json
import os
import re
import selectors
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import evdev
import psutil
from evdev import ecodes

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402  (must come after SDL env vars are set)

VERSION = "2.6"

BASE_DIR = Path(__file__).resolve().parent
FONT_PATH = BASE_DIR / "VCR_OSD_MONO_1.001.ttf"
BORDER_FONT_PATH = BASE_DIR / "Px437_IBM_VGA_9x16.ttf"  # box-drawing glyphs only, VCR OSD MONO lacks them
SETTINGS_PATH = BASE_DIR / "settings.json"  # gitignored, same pattern as STRINGS's state.json -- local preference, not source

FRAME_W, FRAME_H = 720, 480
UNDERSCAN = 0.10
TARGET_WIDTH = 40

BLACK = (0, 0, 0)

# Three selectable color schemes (name, primary, warning) -- the Settings
# page's COLOR: row cycles through these via apply_color_scheme(). RED
# stays the warning color for both ORANGE and GREEN (still reads as
# "alert" against either), but WHITE gets a GRAY warning instead since
# red-on-white doesn't read as distinctly different from the rest of the
# (also-bright) body text the way red-on-black/red-on-green does.
COLOR_SCHEMES = [
    ("ORANGE", (0xFF, 0xA5, 0x00), (220, 30, 30)),
    ("GREEN", (0x00, 0xFF, 0x00), (220, 30, 30)),
    ("WHITE", (0xFF, 0xFF, 0xFF), (160, 160, 160)),
]
DEFAULT_COLOR_SCHEME_INDEX = 0

# PRIMARY_COLOR/WARNING_COLOR are the two module globals every draw
# function in this file reads (directly, or -- for draw_text/draw_bar --
# via a color=None default resolved inside the function body rather than
# baked in at def time) -- see apply_color_scheme(). A scheme switch just
# reassigns these two names; nothing else needs to know it happened.
PRIMARY_COLOR = COLOR_SCHEMES[DEFAULT_COLOR_SCHEME_INDEX][1]
WARNING_COLOR = COLOR_SCHEMES[DEFAULT_COLOR_SCHEME_INDEX][2]


def apply_color_scheme(index):
    global PRIMARY_COLOR, WARNING_COLOR
    PRIMARY_COLOR = COLOR_SCHEMES[index][1]
    WARNING_COLOR = COLOR_SCHEMES[index][2]


def load_color_scheme_index():
    try:
        data = json.loads(SETTINGS_PATH.read_text())
        index = int(data["color_scheme_index"])
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT_COLOR_SCHEME_INDEX
    return index if 0 <= index < len(COLOR_SCHEMES) else DEFAULT_COLOR_SCHEME_INDEX


def save_color_scheme_index(index):
    try:
        SETTINGS_PATH.write_text(json.dumps({"color_scheme_index": index}))
    except OSError as exc:
        print(f"Failed to save settings: {exc}", file=sys.stderr)

KDSETMODE = 0x4B3A
KD_TEXT = 0x00
KD_GRAPHICS = 0x01

STATS_REFRESH_SECONDS = 1.0  # health screen: how often to re-poll CPU/WiFi stats
HARDWARE_REFRESH_SECONDS = 30  # how often to re-poll sibling-app hardware/connectivity checks
IDLE_POLL_TIMEOUT = 1.0
IDLE_TIMEOUT_SECONDS = 120  # menu screen: screensaver, hand off to health after 2 idle minutes

# McBrain remote monitoring -- Up/Down on the health screen cycles which
# of these (or LOCAL) is displayed. IPs match the p1-p4 SSH aliases in
# ~/.ssh/config (see project_naming_conventions). Port matches STRINGS's
# fixed listen port on every puppet.
PUPPETS = [
    ("P1", "192.168.68.72"),
    ("P2", "192.168.68.65"),
    ("P3", "192.168.68.68"),
    ("P4", "192.168.68.64"),
    # production joined the fleet as a real STRINGS-supervised target
    # 2026-08-16 -- SCRUTE no longer auto-launches there (manual
    # fallback only), so this is genuinely just another puppet from
    # MP's perspective now. No other special-casing needed anywhere --
    # RemotePoller/assign_to_puppet/_send_relay_key are all already
    # fully generic over PUPPETS.
    ("PRODUCTION", "192.168.68.71"),
]
MONITOR_TARGETS = ["LOCAL"] + [name for name, _ip in PUPPETS]
PUPPET_PORT = 8420

# Keycodes forwarded live to a puppet's running app in control mode (see
# assign_to_puppet()) -- must match STRINGS's RELAY_KEYS allowlist by
# name exactly. Deliberately excludes Home/Back/Q/Esc/Compose, which
# stay local (exit control mode / open Select Target / control SCRUTE
# itself) rather than being relayed -- those mean "exit this app" in
# every sibling app's own handle_keycode, so relaying them would
# kill/restart whatever's running on the puppet instead of just
# adjusting it. Power is relayed too (2026-08-17) -- see the dedicated
# handling in handle_keycode(), which routes it here instead of opening
# SCRUTE's own local power dialog whenever current_page ==
# CONTROL_PAGE_INDEX, so the *target*'s confirm dialog shows on the
# machine you're actually controlling. This only became safe to relay
# once every fleet
# machine's logind stopped independently reacting to a raw KEY_POWER
# event (HandlePowerKey=ignore) -- otherwise the relayed keypress would
# instantly power off the puppet before its own app dialog ever showed,
# the same bug this whole change stemmed from on MP itself.
CONTROL_RELAY_KEYS = {
    ecodes.KEY_UP: "KEY_UP",
    ecodes.KEY_DOWN: "KEY_DOWN",
    ecodes.KEY_LEFT: "KEY_LEFT",
    ecodes.KEY_RIGHT: "KEY_RIGHT",
    ecodes.KEY_ENTER: "KEY_ENTER",
    ecodes.KEY_KPENTER: "KEY_ENTER",
    ecodes.KEY_VOLUMEUP: "KEY_VOLUMEUP",
    ecodes.KEY_VOLUMEDOWN: "KEY_VOLUMEDOWN",
    ecodes.KEY_POWER: "KEY_POWER",
}

# On-screen labels for the live-control screen's D-pad boxes (2026-08-16).
# Plain literal strings for now -- deliberately not per-app yet -- but
# kept as a lookup dict rather than inlined into the drawing code so a
# future per-app label system (e.g. sourced from STRINGS's /status) can
# swap these in later without touching _build_control_mode_screen.
CONTROL_DPAD_LABELS = {"up": "UP", "down": "DOWN", "left": "LEFT", "right": "RIGHT"}

# Maps each keycode handled on the live-control screen to the button-box
# id _build_control_mode_screen draws it as -- used both to relay/act on
# the press and to know which box to flash (see _flash_button). "NO"
# (the remote's air-mouse toggle) has no entry -- its actual keycode,
# if any, isn't identified yet (see REMOTE-MENU-LAYOUT.txt questions),
# so it can't flash until that's known.
CONTROL_BUTTON_IDS = {
    ecodes.KEY_UP: "up",
    ecodes.KEY_DOWN: "down",
    ecodes.KEY_LEFT: "left",
    ecodes.KEY_RIGHT: "right",
    ecodes.KEY_ENTER: "ok",
    ecodes.KEY_KPENTER: "ok",
    ecodes.KEY_VOLUMEUP: "volup",
    ecodes.KEY_VOLUMEDOWN: "mute",
    ecodes.KEY_HOMEPAGE: "apps",
    ecodes.KEY_HOME: "apps",
    ecodes.KEY_BACK: "back",
}

# How long a pressed button's box stays inverted (orange fill, black
# text) on the live-control screen -- a purely visual "yes, this was
# received" confirmation for troubleshooting whether input is actually
# reaching SCRUTE (2026-08-16, user request). Blocks briefly like every
# other confirmation flash in this file already does (assign_to_puppet,
# etc.) -- imperceptible for a single keypress.
BUTTON_FLASH_SECONDS = 0.15

# Six pages in one Left/Right rotation, all handled by the same
# current_page int -- gauge pages (0/1), the app menu (2), Settings (3),
# Select Target (4), and Remote Control Help (5). Select Target and
# Remote Control Help used to be separate "overlay_mode" full-screen
# states layered on top of current_page, entered only via the
# TARGET/hamburger button or right after a successful puppet assignment
# -- folded into the normal page rotation 2026-08-17 so Left/Right alone
# can reach them too; TARGET/hamburger and auto-jump-after-assign still
# work as fast-paths into the same two page indices. Left/Right skips
# CONTROL_PAGE_INDEX whenever monitor_target is LOCAL (see
# _cycle_page) -- relaying keypresses to yourself doesn't mean
# anything, so that page only exists in the rotation once a remote
# target is actually selected. Settings (2026-08-18) is never skipped --
# it's local to SCRUTE's own display regardless of monitor_target, same
# as the app menu. Up/Down means "move the menu/target cursor" on pages
# 2/4 and does nothing on the gauge pages (removed 2026-08-16 in favor
# of TARGET/hamburger, see _handle_gauge_page_keycode). Selecting an app
# on the menu page acts on whichever machine is currently selected
# (self.monitor_target): launches locally for LOCAL, assigns remotely
# via STRINGS for a puppet -- and for a puppet, that assignment is also
# what puts you in live control of it (see assign_to_puppet).
MENU_PAGE_INDEX = 2
SETTINGS_PAGE_INDEX = 3
TARGET_SELECT_PAGE_INDEX = 4
CONTROL_PAGE_INDEX = 5
PAGE_COUNT = 6
PUPPET_POLL_TIMEOUT_SECONDS = 2
PUPPET_POLL_INTERVAL_SECONDS = 3

# The physical remote's OK button fires both KEY_ENTER and BTN_LEFT for
# one press (separate keyboard/mouse HID interfaces on the same
# device) -- see _activate_menu_selection for the full story. This
# debounces the app-menu activation specifically, since that's the one
# "confirm" action with a real, race-prone side effect (launching a
# process on a puppet); well under a human's fastest plausible second
# press, comfortably over the near-simultaneous dual-interface gap.
MENU_ACTIVATION_DEBOUNCE_SECONDS = 0.5

POWER_OPTIONS = ["NO", "YES", "RESTART"]
MOUSE_MOVE_THRESHOLD = 12  # cumulative REL_X/REL_Y units before it counts as one direction press

# The 4 real apps' Home button exits with this so launch_app() knows to
# switch straight to the health screen instead of the menu screen once
# the subprocess returns -- see launch_app() below.
EXIT_GOTO_HOME = 42

WIFI_IFACE = "wlan0"

# CP437 box-drawing glyphs, used for panel borders.
BOX_TL, BOX_TR, BOX_BL, BOX_BR = "┌", "┐", "└", "┘"
BOX_H, BOX_V = "─", "│"


def find_keyboard_devices():
    # See bars.py for why this filters by EV_KEY capability rather than
    # hardcoding paths (remote controls split across several /dev/input
    # nodes, node numbers aren't stable across reboots).
    devices = []
    for path in evdev.list_devices():
        dev = evdev.InputDevice(path)
        if dev.capabilities().get(ecodes.EV_KEY):
            devices.append(dev)
    if not devices:
        # Same headless tolerance as bars.py/loudness.py/channel38.py --
        # MP is *meant* to always have the remote attached, but rather
        # than refuse to run at all in the gap before it's plugged in
        # (or if it's ever briefly unplugged), stay up and show the
        # dashboard non-interactively. The evdev selector loop below
        # just has nothing registered and always times out.
        print("No keyboard input device found -- running non-interactively until one is attached.", file=sys.stderr)
    return devices


class FrameBuffer:
    """Direct writer for /dev/fb0, bypassing DRM page-flips entirely.

    Verbatim copy of bars.py's FrameBuffer -- see that file for the full
    rationale. Geometry is read from sysfs at open time since it depends
    on whichever output (composite/HDMI) is currently active.
    """

    def __init__(self, dev="/dev/fb0"):
        import mmap
        import numpy as np

        self._np = np
        sys_dir = Path("/sys/class/graphics") / Path(dev).name
        self.width, self.height = (int(x) for x in (sys_dir / "virtual_size").read_text().split(","))
        self.bpp = int((sys_dir / "bits_per_pixel").read_text())
        self.stride = int((sys_dir / "stride").read_text())
        self.bypp = self.bpp // 8
        self.row_bytes = self.width * self.bypp
        size = self.stride * self.height
        self.fd = os.open(dev, os.O_RDWR)
        self.mm = mmap.mmap(self.fd, size, mmap.MAP_SHARED, mmap.PROT_WRITE | mmap.PROT_READ)
        if self.bpp not in (16, 32):
            raise RuntimeError(f"Unsupported framebuffer depth: {self.bpp}bpp")

    def write_surface(self, surface):
        np = self._np
        if surface.get_size() != (self.width, self.height):
            surface = pygame.transform.scale(surface, (self.width, self.height))
        arr = pygame.surfarray.pixels3d(surface).transpose(1, 0, 2)  # (H, W, RGB) uint8
        if self.bpp == 16:
            r = arr[:, :, 0].astype(np.uint16) >> 3
            g = arr[:, :, 1].astype(np.uint16) >> 2
            b = arr[:, :, 2].astype(np.uint16) >> 3
            raw = ((r << 11) | (g << 5) | b).astype("<u2").tobytes()
        else:
            alpha = np.zeros((self.height, self.width, 1), dtype=np.uint8)
            raw = np.concatenate([arr[:, :, ::-1], alpha], axis=2).astype(np.uint8).tobytes()

        if self.stride == self.row_bytes:
            self.mm.seek(0)
            self.mm.write(raw)
        else:
            for y in range(self.height):
                self.mm.seek(y * self.stride)
                self.mm.write(raw[y * self.row_bytes : (y + 1) * self.row_bytes])

    def close(self):
        self.mm.close()
        os.close(self.fd)


########  Stat-gathering  ######################################################

def _vcgencmd(*args):
    try:
        result = subprocess.run(["vcgencmd", *args], capture_output=True, text=True, timeout=2)
        return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def get_cpu_temp():
    out = _vcgencmd("measure_temp")
    try:
        return float(out.split("=")[1].split("'")[0])
    except (IndexError, ValueError):
        return None


def get_cpu_clock_mhz():
    out = _vcgencmd("measure_clock", "arm")
    try:
        hz = int(out.split("=")[1])
        return hz / 1_000_000
    except (IndexError, ValueError):
        return None


def get_throttled_status():
    out = _vcgencmd("get_throttled")
    try:
        value = int(out.split("=")[1], 16)
    except (IndexError, ValueError):
        return "UNKNOWN"
    # Bits 0-3 are *current* conditions; bits 16-19 are "has happened since
    # boot" versions of the same four. Only the current bits matter for a
    # live dashboard -- a footnote about past throttling isn't actionable.
    if value & 0x1:
        return "UNDERVOLTAGE"
    if value & 0x4:
        return "THROTTLED"
    if value & 0x8:
        return "TEMP LIMIT"
    if value & 0x2:
        return "FREQ CAPPED"
    return "OK"


def get_ip_address():
    # Same UDP-connect trick as bars.py's get_ip_address() -- doesn't
    # actually send anything, just asks the kernel which local address
    # would be used to reach an external host.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "NO NETWORK"
    finally:
        s.close()


def get_wifi_link():
    """Returns (quality_0_to_70, level_dbm) for WIFI_IFACE from
    /proc/net/wireless, or (None, None) if not found."""
    try:
        lines = Path("/proc/net/wireless").read_text().splitlines()
    except OSError:
        return None, None
    for line in lines:
        line = line.strip()
        if not line.startswith(f"{WIFI_IFACE}:"):
            continue
        fields = line.split(":", 1)[1].split()
        try:
            quality = float(fields[1])
            level = float(fields[2])
            return quality, level
        except (IndexError, ValueError):
            return None, None
    return None, None


def get_wifi_ssid():
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"],
            capture_output=True, text=True, timeout=3,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    for line in result.stdout.splitlines():
        if line.startswith("yes:"):
            return line.split(":", 1)[1]
    return None


class StatsPoller:
    """Gathers all dashboard stats on a timer rather than every frame --
    several of these shell out or hit the network, so re-running them on
    every redraw would make the whole UI feel laggy (same precedent as
    refresh_hardware_status() below)."""

    def __init__(self):
        self.stats = {}
        self._last_net = None  # (timestamp, bytes_sent, bytes_recv)
        psutil.cpu_percent(percpu=True)  # prime the delta -- first call is meaningless
        self.refresh()

    def refresh(self):
        self.stats["cpu_temp"] = get_cpu_temp()
        self.stats["cpu_clock_mhz"] = get_cpu_clock_mhz()
        self.stats["throttled"] = get_throttled_status()
        self.stats["cpu_percore"] = psutil.cpu_percent(percpu=True)
        self.stats["loadavg"] = os.getloadavg()
        self.stats["mem"] = psutil.virtual_memory()
        self.stats["disk"] = psutil.disk_usage("/")
        self.stats["wifi_quality"], self.stats["wifi_level"] = get_wifi_link()
        self.stats["wifi_ssid"] = get_wifi_ssid()
        self.stats["ip"] = get_ip_address()

        now = time.time()
        try:
            io = psutil.net_io_counters(pernic=True).get(WIFI_IFACE)
        except OSError:
            io = None
        if io is not None:
            if self._last_net is not None:
                elapsed = now - self._last_net[0]
                if elapsed > 0:
                    self.stats["net_up_kbs"] = (io.bytes_sent - self._last_net[1]) / 1024 / elapsed
                    self.stats["net_down_kbs"] = (io.bytes_recv - self._last_net[2]) / 1024 / elapsed
            self._last_net = (now, io.bytes_sent, io.bytes_recv)


class RemotePoller:
    """Polls every puppet's STRINGS /status endpoint over the LAN, on its
    own background thread -- unlike StatsPoller (called synchronously
    from the main loop, fine since local vcgencmd/psutil calls are fast),
    an HTTP request to a slow or offline puppet could block for the full
    timeout, and with 4 puppets that's long enough to visibly stall input
    handling if done inline. One puppet being unreachable also must never
    hold up polling the other three, so each is fetched independently."""

    def __init__(self):
        self._stats = {name: None for name, _ip in PUPPETS}
        self._fresh = {name: False for name, _ip in PUPPETS}
        self._lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def get(self, name):
        """Returns (stats_dict_or_None, fresh_bool)."""
        with self._lock:
            return self._stats.get(name), self._fresh.get(name, False)

    def set_app_hint(self, name, app):
        """Optimistically overwrites the cached 'app' field for `name`
        right after a successful /assign (see assign_to_puppet) -- the
        background poll thread only refreshes every
        PUPPET_POLL_INTERVAL_SECONDS, and even once it does, STRINGS
        itself only updates its own reported `app` after a launch
        actually succeeds, so without this the control-mode footer
        would keep showing whatever was running *before* the new
        assignment for a few seconds after picking the new app. Gets
        overwritten again by the next real poll regardless, so a wrong
        guess (e.g. the launch fails right after) self-heals within one
        poll interval."""
        with self._lock:
            stats = self._stats.get(name)
            if stats is not None:
                stats["app"] = app
            else:
                self._stats[name] = {"app": app}

    def _run(self):
        while True:
            for name, ip in PUPPETS:
                self._poll_one(name, ip)
            time.sleep(PUPPET_POLL_INTERVAL_SECONDS)

    def _poll_one(self, name, ip):
        try:
            url = f"http://{ip}:{PUPPET_PORT}/status"
            with urllib.request.urlopen(url, timeout=PUPPET_POLL_TIMEOUT_SECONDS) as resp:
                data = json.loads(resp.read())
        except (OSError, ValueError):
            with self._lock:
                self._fresh[name] = False
            return
        # STRINGS reports mem/disk as plain JSON objects; the existing
        # draw_memory_panel/draw_storage_panel were written against
        # psutil's namedtuple-style objects (attribute access, e.g.
        # mem.percent) -- wrapping in SimpleNamespace lets those
        # functions run completely unchanged against remote data too.
        if data.get("mem") is not None:
            data["mem"] = SimpleNamespace(**data["mem"])
        if data.get("disk") is not None:
            data["disk"] = SimpleNamespace(**data["disk"])
        with self._lock:
            self._stats[name] = data
            self._fresh[name] = True


########  App-menu data  #######################################################

LOUDNESS_SETTINGS_PATH = "/opt/loudness/settings.ini"
LOUDNESS_DEVICE_RE = re.compile(r"^device\s*=\s*plughw:(\d+),(\d+)", re.MULTILINE)


def check_sdr_dongle():
    # Unused now that retro-radar is retired (moved aside, not deleted --
    # see the *.retired-20260814 paths -- user's plan is to revisit it
    # later). Kept rather than removed so re-adding retro-radar to APPS
    # doesn't require reconstructing this check.
    #
    # There's no software-visible signal for "an antenna is attached" --
    # this only confirms readsb has successfully claimed an RTL-SDR
    # dongle over USB. readsb auto-restart-loops (systemd state
    # "activating", never settling on "active") when no dongle is
    # present, which makes `systemctl is-active` a solid proxy without
    # reimplementing USB device scanning here.
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "readsb.service"],
            capture_output=True, text=True, timeout=3,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return result.stdout.strip() == "active"


def check_loudness_mic():
    # Reads the *currently configured* device out of LOUDNESS's own
    # settings.ini rather than checking for "any mic" -- this is the
    # same card/device pair LOUDNESS itself will try to open, so it
    # catches the exact failure mode that already bit this Pi once
    # (settings.ini's card number going stale after a USB re-enumeration).
    try:
        text = Path(LOUDNESS_SETTINGS_PATH).read_text()
    except OSError:
        return False
    match = LOUDNESS_DEVICE_RE.search(text)
    if not match:
        return False
    card, device = match.groups()
    return Path(f"/dev/snd/pcmC{card}D{device}c").exists()


def check_internet():
    # Connects to a literal IP (not a hostname) so a dead connection
    # fails fast instead of also waiting out a DNS timeout on top of the
    # socket timeout.
    try:
        with socket.create_connection(("8.8.8.8", 53), timeout=1.5):
            return True
    except OSError:
        return False


# (key char, label, description, launcher command, main script -- read
# for its own VERSION, hardware check -- None if the app has no
# hardware dependency to poll for)
APPS = [
    ("1", "BARS ULRICH", "NTSC test pattern", "bars", "/opt/bars/bars.py", None),
    ("2", "LOUDNESS", "Audio spectrum visualizer", "loudness", "/opt/loudness/loudness.py", check_loudness_mic),
    ("3", "WEATHERSTAR 4000", "Current conditions", "weatherstar", "/opt/weatherstar/weatherstar_launcher.py", check_internet),
    ("4", "CHANNEL 38", "Ole Miss sports ticker", "channel38", "/opt/channel38/channel38.py", check_internet),
]

HW_STATUS_LABELS = {
    "not_installed": "NOT INSTALLED HERE",
    "hardware_not_found": "HARDWARE NOT FOUND",
}

VERSION_RE = re.compile(r"""VERSION\s*=\s*['"]([^'"]+)['"]""")


def read_app_version(script_path):
    # Each app owns its VERSION constant in its own source file (they may
    # end up as separate GitHub repos eventually) -- scanning the source
    # text for it avoids importing the module, which would drag in each
    # app's own venv/hardware assumptions (retro-radar's kmsdrm venv in
    # particular) just to read a string.
    try:
        text = Path(script_path).read_text()
    except OSError:
        return "?"
    match = VERSION_RE.search(text)
    return match.group(1) if match else "?"


########  Rendering  ###########################################################

class Panel:
    """A titled, box-drawn rectangle in character-cell coordinates. Content
    is drawn by whatever draw_fn(canvas, content_rect, stats) is passed in
    -- content_rect is the pixel rect *inside* the border, in pixels."""

    def __init__(self, col, row, w_chars, h_chars, title, draw_fn=None, subtitle=None):
        self.col, self.row = col, row
        self.w_chars, self.h_chars = w_chars, h_chars
        self.title = title
        self.subtitle = subtitle  # optional, right-justified in the bottom border
        self.draw_fn = draw_fn


class HealthDisplay:
    def __init__(self, fb):
        # Layout math is against FRAME_W/FRAME_H (the fixed canvas render()
        # actually draws onto), NOT fb.width/fb.height (the real hardware
        # framebuffer's resolution, e.g. 1024x768 on HDMI today) -- those
        # are two different things, and FrameBuffer.write_surface() is what
        # stretches the FRAME_W x FRAME_H canvas to fit whichever real
        # resolution is active. Mixing the two caused the dashboard to
        # render far too large and run off-screen the first time around.
        self._fb = fb

        self._margin_x = int(FRAME_W * UNDERSCAN)
        self._margin_y = int(FRAME_H * UNDERSCAN)
        usable_w = FRAME_W - 2 * self._margin_x
        usable_h = FRAME_H - 2 * self._margin_y

        # Pick the largest point size whose 'M' width still fits
        # TARGET_WIDTH columns in the underscanned area -- a hardcoded
        # point size doesn't track how many actual pixels a TTF's glyphs
        # end up occupying, and badly undersized the whole dashboard the
        # first time around (channel38's Display uses this same
        # measure-then-pick approach for the same reason). Shared by both
        # the health dashboard and the app-menu screen -- one font, one
        # character grid, for the whole program now.
        self._font = self._fit_font(TARGET_WIDTH, usable_w)
        self._label_font = self._font

        self._char_w = self._font.size("M")[0]
        self._char_h = self._font.get_linesize()
        max_width = max(1, usable_w // self._char_w)
        self._width = min(TARGET_WIDTH, max_width)
        self._height = max(1, usable_h // self._char_h)

        # Border-only font (see module docstring) -- fit to one character
        # cell of the body font's grid, not the whole row, since it only
        # ever draws one glyph per cell.
        self._border_font = self._fit_font(1, self._char_w, font_path=BORDER_FONT_PATH)

    @staticmethod
    def _fit_font(target_width_chars, usable_w, max_size=64, min_size=8, font_path=None):
        font_path = font_path or FONT_PATH
        for size in range(max_size, min_size - 1, -1):
            candidate = pygame.font.Font(str(font_path), size)
            if candidate.size("M")[0] * target_width_chars <= usable_w:
                return candidate
        return pygame.font.Font(str(font_path), min_size)

    def char_px(self, col, row):
        return (self._margin_x + col * self._char_w, self._margin_y + row * self._char_h)

    def draw_text(self, canvas, col, row, text, color=None, bold=False, font=None):
        # color=None resolved here, not defaulted to PRIMARY_COLOR up in
        # the signature -- a default *argument value* is evaluated once,
        # at def time, so it would freeze in whatever scheme was active
        # when the class body first ran and never track a later
        # apply_color_scheme() call (confirmed the hard way: panel
        # borders/titles, which never pass color explicitly, stayed
        # ORANGE forever until this was fixed).
        if color is None:
            color = PRIMARY_COLOR
        font = font or self._font
        font.set_bold(bold)
        surf = font.render(text, True, color)
        canvas.blit(surf, self.char_px(col, row))

    def draw_panel_frame(self, canvas, panel):
        """Box-drawn border in character-cell coordinates, with an
        optional title embedded in the top border (left-justified, right
        after the top-left corner) and an optional subtitle embedded in
        the bottom border (right-justified, right before the bottom-right
        corner -- mirrors the title's placement on the opposite corner).
        Used both for the health dashboard's gauge panels and the app-menu's
        outer box.

        The border glyphs (corners/BOX_H/BOX_V) are drawn one cell at a
        time with self._border_font (see module docstring -- the body
        font, VCR OSD MONO, has no box-drawing glyphs). Title/subtitle
        text is drawn with the normal body font -- the two fonts don't
        share a baseline/cap-height, so a title blitted *on top of* a
        BOX_H line left a sliver of the line visible under the text
        (confirmed on the real CRT 2026-08-14). Fixed by leaving the
        title/subtitle's cells out of the BOX_H fill entirely -- true
        blank cells, not an overlap -- so there's nothing under the text
        to peek out: corner, blank run, title, blank run, BOX_H the rest
        of the way."""
        x, y = self.char_px(panel.col, panel.row)
        inner_w = panel.w_chars - 2
        # Title sits 2 extra glyphs in from the corner (2026-08-14, user
        # request) -- corner, 2x BOX_H, blank run, title, blank run, then
        # BOX_H the rest of the way. Subtitle (bottom-right) is unchanged.
        TITLE_INDENT = 3
        title_text = (f" {panel.title} " if panel.title else "")[: max(0, inner_w - (TITLE_INDENT - 1))]
        subtitle_text = (f" {panel.subtitle} " if panel.subtitle else "")[:inner_w]
        title_cols = set(range(TITLE_INDENT, TITLE_INDENT + len(title_text)))
        subtitle_start = panel.w_chars - 1 - len(subtitle_text)
        subtitle_cols = set(range(subtitle_start, panel.w_chars - 1))
        # Border glyphs (corners/BOX_H/BOX_V) stay bold -- confirmed fix
        # for real interlace flicker on CRT output (these strokes are
        # only ~1px at this font size; a single-field-thin line visibly
        # flickers at half the frame rate on an interlaced display, and
        # synthetic bold is the only way to thicken a font-rendered
        # glyph, there's no line-width knob like pygame.draw.rect has).
        # title_text/subtitle_text below are body-font TEXT, not part of
        # that border-line fix, and used to incidentally ride along on
        # the same bold=True -- switched to normal 2026-08-17 (confirmed
        # more legible on real small CRTs than the synthetic-bold
        # rendering) after a stray-bold bug surfaced from this: _font
        # and _label_font are the same object, so bold=True here used to
        # leak into anything that rendered _label_font text directly
        # afterward without going through draw_text's own bold reset
        # (caught live: the control-mode headline rendered bold on first
        # paint, then correctly normal once some later draw_text call
        # happened to reset it first). Now that neither call below ever
        # sets bold=True, _font's bold state never gets mutated in this
        # function at all, so that whole class of leak is moot -- no
        # explicit reset needed here anymore.
        bottom_row = panel.row + panel.h_chars - 1
        self.draw_text(canvas, panel.col, panel.row, BOX_TL, bold=True, font=self._border_font)
        self.draw_text(canvas, panel.col + panel.w_chars - 1, panel.row, BOX_TR, bold=True, font=self._border_font)
        self.draw_text(canvas, panel.col, bottom_row, BOX_BL, bold=True, font=self._border_font)
        self.draw_text(canvas, panel.col + panel.w_chars - 1, bottom_row, BOX_BR, bold=True, font=self._border_font)
        for c in range(1, panel.w_chars - 1):
            if c not in title_cols:
                self.draw_text(canvas, panel.col + c, panel.row, BOX_H, bold=True, font=self._border_font)
            if c not in subtitle_cols:
                self.draw_text(canvas, panel.col + c, bottom_row, BOX_H, bold=True, font=self._border_font)
        if title_text:
            self.draw_text(canvas, panel.col + TITLE_INDENT, panel.row, title_text)
        if subtitle_text:
            self.draw_text(canvas, panel.col + subtitle_start, bottom_row, subtitle_text)
        for r in range(1, panel.h_chars - 1):
            self.draw_text(canvas, panel.col, panel.row + r, BOX_V, bold=True, font=self._border_font)
            self.draw_text(canvas, panel.col + panel.w_chars - 1, panel.row + r, BOX_V, bold=True, font=self._border_font)

        content_x = x + self._char_w
        content_y = y + self._char_h
        content_w = (panel.w_chars - 2) * self._char_w
        content_h = (panel.h_chars - 2) * self._char_h
        return pygame.Rect(content_x, content_y, content_w, content_h)

    def draw_bar(self, canvas, rect, fraction, label=None, color=None):
        """`rect` is the *full* available width for bar + label combined --
        the label's actual rendered width is measured first and the bar
        gets whatever's left, rather than assuming a fixed label width
        that doesn't track font size (that fixed-budget approach is what
        let labels run past the panel edge at larger auto-fit sizes).
        The bar's own left edge is inset by one character width so it
        lines up with the left-indented text lines above/below it
        (2026-08-14). Also 2px shorter than the caller's rect, with the
        top edge moved down to absorb it (bottom edge unchanged) -- a
        small gap between the bar and the line of text above it, and
        closer to the text glyph height next to it (2026-08-14)."""
        if color is None:  # see draw_text's color=None comment -- same reason
            color = PRIMARY_COLOR
        fraction = max(0.0, min(1.0, fraction))
        bar_rect = pygame.Rect(rect.x + self._char_w, rect.y + 2, rect.width - self._char_w, rect.height - 2)
        label_surf = None
        if label:
            label_surf = self._label_font.render(label, True, PRIMARY_COLOR)
            bar_rect.width -= label_surf.get_width() + 12
        # Border is 2px, not 1px -- a single-scanline-thin horizontal edge
        # only lands on one of the two interlaced fields each frame, which
        # reads as visible jitter/flicker on a real CRT (inherent to
        # interlacing, not a rendering bug -- confirmed on real composite
        # output). 2px puts the top/bottom edges on both fields every
        # frame. The fill inset grows to match so it still sits cleanly
        # inside the thicker border rather than overlapping it.
        pygame.draw.rect(canvas, PRIMARY_COLOR, bar_rect, 2)
        fill_w = int((bar_rect.width - 4) * fraction)
        if fill_w > 0:
            pygame.draw.rect(canvas, color, (bar_rect.x + 2, bar_rect.y + 2, fill_w, bar_rect.height - 4))
        if label_surf:
            canvas.blit(label_surf, (bar_rect.right + 8, rect.y + (rect.height - label_surf.get_height()) // 2))

    def render_page(self, canvas, panels, stats):
        canvas.fill(BLACK)
        for panel in panels:
            content_rect = self.draw_panel_frame(canvas, panel)
            panel.draw_fn(self, canvas, content_rect, stats)


########  Health panel content  ################################################

def draw_cpu_panel(display, canvas, rect, stats):
    # TEMP/CLOCK share one row (only 14 usable rows exist at this font
    # size for both panels' combined minimum content -- see the
    # panel-height computation in HealthApp.__init__).
    y = rect.y
    temp = stats.get("cpu_temp")
    temp_text = f"{temp * 9 / 5 + 32:.1f} F" if temp is not None else "N/A"
    clock = stats.get("cpu_clock_mhz")
    # Width-4 so a 3-digit clock speed (600) gets a leading space to match
    # a 4-digit one (1400) -- keeps CLOCK/MHZ from jittering left-right as
    # the value crosses that digit-count boundary.
    clock_text = f"{clock:4.0f} MHZ" if clock is not None else "N/A"
    surf = display._font.render(f" TEMP: {temp_text}   CLOCK: {clock_text}", True, PRIMARY_COLOR)
    canvas.blit(surf, (rect.x, y))
    y += display._char_h

    percore = stats.get("cpu_percore") or []
    for i, pct in enumerate(percore):
        bar_rect = pygame.Rect(rect.x, y, rect.width, display._char_h - 4)
        display.draw_bar(canvas, bar_rect, pct / 100.0, label=f" CORE{i} {pct:4.0f}% ")
        y += display._char_h

    # Flows directly after the core bars rather than pinned to rect.bottom
    # -- anchoring it independently of how much space the bars above
    # actually used is what caused it to overlap the last core's bar.
    load1, load5, load15 = stats.get("loadavg", (0, 0, 0))
    surf = display._font.render(f" LOAD: {load1:.2f} {load5:.2f} {load15:.2f}", True, PRIMARY_COLOR)
    canvas.blit(surf, (rect.x, y))
    y += display._char_h

    status = stats.get("throttled", "UNKNOWN")
    surf = display._font.render(f" STATUS: {status}", True, PRIMARY_COLOR)
    canvas.blit(surf, (rect.x, y))


def draw_memory_panel(display, canvas, rect, stats):
    mem = stats.get("mem")
    if mem is None:
        return
    y = rect.y
    bar_rect = pygame.Rect(rect.x, y, rect.width, display._char_h - 4)
    display.draw_bar(canvas, bar_rect, mem.percent / 100.0, label=f" {mem.percent:.0f}% ")
    y += display._char_h

    used_mb = mem.used / (1024 * 1024)
    total_mb = mem.total / (1024 * 1024)
    surf = display._font.render(f" USED:  {used_mb:.0f} MB", True, PRIMARY_COLOR)
    canvas.blit(surf, (rect.x, y))
    y += display._char_h
    surf = display._font.render(f" TOTAL: {total_mb:.0f} MB", True, PRIMARY_COLOR)
    canvas.blit(surf, (rect.x, y))


def draw_storage_panel(display, canvas, rect, stats):
    disk = stats.get("disk")
    if disk is None:
        return
    y = rect.y
    bar_rect = pygame.Rect(rect.x, y, rect.width, display._char_h - 4)
    display.draw_bar(canvas, bar_rect, disk.percent / 100.0, label=f" {disk.percent:.0f}% ")
    y += display._char_h

    used_gb = disk.used / (1024 ** 3)
    free_gb = disk.free / (1024 ** 3)
    surf = display._font.render(f" {used_gb:.0f} GB USED   {free_gb:.0f} GB FREE", True, PRIMARY_COLOR)
    canvas.blit(surf, (rect.x, y))


def draw_wifi_panel(display, canvas, rect, stats):
    y = rect.y
    ssid = stats.get("wifi_ssid") or "NOT CONNECTED"
    surf = display._font.render(f" SSID: {ssid}", True, PRIMARY_COLOR)
    canvas.blit(surf, (rect.x, y))
    y += display._char_h

    ip = stats.get("ip", "N/A")
    surf = display._font.render(f" IP: {ip}", True, PRIMARY_COLOR)
    canvas.blit(surf, (rect.x, y))
    y += display._char_h

    quality = stats.get("wifi_quality")
    level = stats.get("wifi_level")
    if quality is not None:
        bar_rect = pygame.Rect(rect.x, y, rect.width, display._char_h - 4)
        display.draw_bar(canvas, bar_rect, quality / 70.0, label=f" {quality:.0f}/70 ")
        y += display._char_h
        surf = display._font.render(f" SIGNAL: {level:.0f} DBM", True, PRIMARY_COLOR)
        canvas.blit(surf, (rect.x, y))
    else:
        surf = display._font.render(" NO WIRELESS LINK", True, PRIMARY_COLOR)
        canvas.blit(surf, (rect.x, y))


def draw_network_panel(display, canvas, rect, stats):
    y = rect.y
    up = stats.get("net_up_kbs")
    down = stats.get("net_down_kbs")
    up_text = f"{up:6.1f} KB/S" if up is not None else "  --.- KB/S"
    down_text = f"{down:6.1f} KB/S" if down is not None else "  --.- KB/S"
    surf = display._font.render(f" UP:   {up_text}", True, PRIMARY_COLOR)
    canvas.blit(surf, (rect.x, y))
    y += display._char_h
    surf = display._font.render(f" DOWN: {down_text}", True, PRIMARY_COLOR)
    canvas.blit(surf, (rect.x, y))


########  App  ##################################################################

class HealthApp:
    def __init__(self):
        self._quit_requested = False
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        pygame.display.init()
        pygame.font.init()
        pygame.display.set_mode((FRAME_W, FRAME_H))  # headless (dummy driver); needed for .convert()

        self.fb = FrameBuffer()
        self.display = HealthDisplay(self.fb)
        self.poller = StatsPoller()

        self.osd_font = pygame.font.Font(str(FONT_PATH), 32)
        self.option_font = pygame.font.Font(str(FONT_PATH), 28)

        # No more separate "screen" concept -- the app menu is just page
        # index MENU_PAGE_INDEX of self.current_page now (2026-08-15
        # redesign). Switching pages is just updating that int + a
        # redraw (see handle_keycode()), not a process launch -- see the
        # module docstring for why.

        # Health-dashboard panel layout. Panel columns/rows are computed
        # from the display's actual usable grid (not hardcoded) so the
        # dashboard fills the underscanned area regardless of font size/
        # resolution -- one row is reserved at the TOP for the headline
        # (see build_health_canvas), and panels start at row 2 (not row
        # 1) to leave a blank row of breathing room under it (2026-08-17,
        # matching the same change on the menu/target-select screens --
        # both pages already had exactly 1 row of unused space at the
        # bottom with the old top row of 1, so bumping PANEL_TOP_ROW
        # down by 1 relocates that same slack to the top instead of
        # growing the page).
        # Stacked (full-width, split-height) rather than side-by-side.
        # All four panels are now sized to their own exact content need
        # (a fixed row count) rather than CPU/WIFI taking "whatever's
        # left" -- that remainder-based sizing was carrying slack (1
        # blank row in CPU, 3 in WIFI, 1 in NETWORK below their last
        # content line) that this tightens up, per the user's request
        # 2026-08-14. Any leftover space below the panels is deliberately
        # left blank, not redistributed anywhere.
        w = self.display._width
        CPU_CONTENT_ROWS = 7  # TEMP/CLOCK line, 4 core bars, LOAD, STATUS
        MEM_CONTENT_ROWS = 3  # bar, USED line, TOTAL line
        WIFI_CONTENT_ROWS = 4  # SSID, IP, quality bar, SIGNAL -- gap row removed 2026-08-14
        NET_CONTENT_ROWS = 2  # UP, DOWN
        STORAGE_CONTENT_ROWS = 2  # bar, USED/FREE line
        cpu_h = CPU_CONTENT_ROWS + 2  # +2 for border -- no spare left in the row budget
        mem_h = MEM_CONTENT_ROWS + 2
        wifi_h = WIFI_CONTENT_ROWS + 2
        net_h = NET_CONTENT_ROWS + 2
        storage_h = STORAGE_CONTENT_ROWS + 2
        PANEL_TOP_ROW = 2
        self.pages = [
            [
                Panel(0, PANEL_TOP_ROW, w, cpu_h, "CPU", draw_cpu_panel),
                Panel(0, PANEL_TOP_ROW + cpu_h, w, mem_h, "MEMORY", draw_memory_panel),
            ],
            [
                Panel(0, PANEL_TOP_ROW, w, wifi_h, "WIFI", draw_wifi_panel),
                Panel(0, PANEL_TOP_ROW + wifi_h, w, net_h, "NETWORK", draw_network_panel),
                Panel(0, PANEL_TOP_ROW + wifi_h + net_h, w, storage_h, "STORAGE", draw_storage_panel),
            ],
        ]
        self.current_page = 0
        self.hostname = socket.gethostname().upper()
        self.remote_poller = RemotePoller()
        self.monitor_target = "LOCAL"  # set via the Select Target overlay -- "LOCAL" or a PUPPETS name

        # Settings-page state (2026-08-18). Persisted color choice loads
        # before anything renders, so the very first frame already shows
        # the user's chosen scheme instead of flashing ORANGE first.
        self.color_scheme_index = load_color_scheme_index()
        apply_color_scheme(self.color_scheme_index)
        self.settings_selected = self.color_scheme_index  # cursor on the Settings page -- see _cycle_page's reset-on-landing

        # App-menu screen state.
        self.selected = 0
        self._last_activation_time = 0.0  # debounces _activate_menu_selection -- see MENU_ACTIVATION_DEBOUNCE_SECONDS
        self.hardware_status = {}
        self.refresh_hardware_status()

        self.power_dialog_active = False
        self.power_dialog_selection = 0
        self.pending_power_action = None
        self._rel_accum = {"x": 0, "y": 0}

        # Select Target and Remote Control Help are just pages 4/5 of
        # current_page now (2026-08-17, see the comment above
        # TARGET_SELECT_PAGE_INDEX/CONTROL_PAGE_INDEX) -- no separate
        # overlay_mode/control_mode_target state needed anymore.
        # Whichever machine is being relayed to on the control page is
        # simply self.monitor_target, same as everywhere else.
        self.control_mode_last_result = None  # None, "OK", or "UNREACHABLE" -- last relay attempt's outcome
        self.target_selected = 0  # cursor position on the Select Target page
        self.last_pressed_button = None  # button-box id currently flashed on the live-control screen, or None

        self.kbd_devices = find_keyboard_devices()
        self.selector = selectors.DefaultSelector()
        for dev in self.kbd_devices:
            self.selector.register(dev, selectors.EVENT_READ)

        self.tty_fd = None
        self.console_graphics_mode = False
        self.acquire_console()

    def _handle_signal(self, signum, frame):
        self._quit_requested = True

    def acquire_console(self):
        self.fb = FrameBuffer()
        self.display._fb = self.fb
        self.tty_fd = None
        self.console_graphics_mode = False
        try:
            self.tty_fd = os.open("/dev/tty", os.O_RDWR)
            fcntl.ioctl(self.tty_fd, KDSETMODE, KD_GRAPHICS)
            self.console_graphics_mode = True
        except OSError as exc:
            print(f"Console graphics mode not available: {exc}", file=sys.stderr)

    def release_console(self):
        if self.console_graphics_mode:
            fcntl.ioctl(self.tty_fd, KDSETMODE, KD_TEXT)
            os.write(self.tty_fd, b"\033[2J\033[H")
        if self.tty_fd is not None:
            os.close(self.tty_fd)
            self.tty_fd = None
        self.fb.close()

    def drain_stale_events(self):
        # evdev delivers to every open reader, so our own fds are still
        # holding whatever key just quit the child app, unread.
        for dev in self.kbd_devices:
            try:
                while dev.read_one() is not None:
                    pass
            except (BlockingIOError, OSError):
                pass

    def refresh_hardware_status(self):
        # Run at startup and again every time control returns from a
        # launched app (see launch_app) rather than on every redraw --
        # these checks shell out / hit the network, so re-running them on
        # every arrow-key press would make navigation feel laggy.
        #
        # Status strings, not bools (2026-08-15, matches STRINGS's
        # ReadinessChecker) -- "not installed on this machine at all"
        # and "installed but hw_check failed" used to collapse into the
        # same generic "HARDWARE NOT FOUND," which is both confusing
        # (WEATHERSTAR/CHANNEL 38's checks are check_internet, not a
        # device probe) and was wrong on MP specifically: an app that
        # was never installed there still showed as ready, since this
        # never checked for the launcher at all.
        status = {}
        for _key, _label, _desc, cmd, _script, hw_check in APPS:
            if not Path(f"/usr/local/bin/{cmd}").exists():
                status[cmd] = "not_installed"
                continue
            if hw_check is None:
                status[cmd] = "ready"
                continue
            try:
                status[cmd] = "ready" if hw_check() else "hardware_not_found"
            except Exception:
                # A broken check should read as "hardware not found," not
                # take the whole menu down.
                status[cmd] = "hardware_not_found"
        self.hardware_status = status

    def render(self):
        canvas = pygame.Surface((FRAME_W, FRAME_H))
        if self.current_page == CONTROL_PAGE_INDEX:
            self._build_control_mode_screen(canvas)
        elif self.current_page == TARGET_SELECT_PAGE_INDEX:
            self._build_target_select_screen(canvas)
        elif self.current_page == SETTINGS_PAGE_INDEX:
            self._build_settings_page(canvas)
        else:
            self.build_health_canvas(canvas)  # dispatches to the menu page internally for MENU_PAGE_INDEX
        if self.power_dialog_active:
            self.draw_power_dialog(canvas)
        self.fb.write_surface(canvas)

    def _page_headline(self, target_label):
        """'CONTROLLING: <name>' (2026-08-16, originally just for remote
        targets once selected via the Select Target overlay; unified
        2026-08-17 to cover LOCAL too, since MP is itself always "the
        machine you're driving" the same way a puppet is once
        selected) -- matches the live-control screen's own headline
        style, so the whole page sequence reads as one consistent
        experience regardless of which machine is selected."""
        return f"CONTROLLING: {target_label}"

    def build_health_canvas(self, canvas):
        if self.current_page == MENU_PAGE_INDEX:
            self._build_menu_page(canvas)
            return

        if self.monitor_target == "LOCAL":
            stats, offline = self.poller.stats, False
            target_label = self.hostname
            panels = self.pages[self.current_page]
        else:
            stats, fresh = self.remote_poller.get(self.monitor_target)
            offline = not fresh or stats is None
            # Full hostname (e.g. "PUPPET-2"), not the short P2 name --
            # only available once we've actually heard from it at least
            # once, so fall back to the short name until then/if offline.
            target_label = (stats or {}).get("hostname", self.monitor_target).upper()
            panels = self.pages[self.current_page]

        if offline:
            canvas.fill(BLACK)
            msg = f"{target_label} OFFLINE / UNREACHABLE"
            msg_surf = self.display._font.render(msg, True, WARNING_COLOR)
            canvas.blit(msg_surf, ((FRAME_W - msg_surf.get_width()) // 2, (FRAME_H - msg_surf.get_height()) // 2))
        else:
            self.display.render_page(canvas, panels, stats)

        headline = self._page_headline(target_label)
        headline_surf = self.display._label_font.render(headline, True, PRIMARY_COLOR)
        row0_y = self.display.char_px(0, 0)[1]
        canvas.blit(headline_surf, ((FRAME_W - headline_surf.get_width()) // 2, row0_y))

    def _build_menu_page(self, canvas):
        """Page MENU_PAGE_INDEX -- selecting a row here acts on whichever
        machine self.monitor_target currently points at: launches
        locally for LOCAL (unchanged from before the 2026-08-15
        redesign), assigns remotely via STRINGS for a puppet (see
        assign_to_puppet()). Hardware-readiness (HARDWARE NOT FOUND)
        comes from that same machine -- self.hardware_status for LOCAL,
        the puppet's own STRINGS-reported `hardware` field otherwise, so
        it reflects what's actually attached *there*, not to MP."""
        canvas.fill(BLACK)
        d = self.display

        hardware_status = self._current_hardware_status()
        if self.monitor_target == "LOCAL":
            title = f"CENTRAL SCRUTINIZER {VERSION}".upper()
            target_label = self.hostname
        else:
            stats, fresh = self.remote_poller.get(self.monitor_target)
            title = f"ASSIGN TO {self.monitor_target}{'' if fresh else ' (OFFLINE)'}".upper()
            # Full hostname for the headline below, same fallback as
            # build_health_canvas -- box title keeps the short P2-style
            # name, it's compact and already fits the border nicely.
            target_label = (stats or {}).get("hostname", self.monitor_target).upper()

        # Same headline pages 0/1 show (see _page_headline), in the same
        # top-line position (row 0, above the box) -- keeps the headline
        # position consistent across all 3 pages instead of nesting it
        # inside the box like before.
        headline = self._page_headline(target_label)
        headline_surf = d._label_font.render(headline, True, PRIMARY_COLOR)
        row0_y = d.char_px(0, 0)[1]
        canvas.blit(headline_surf, ((FRAME_W - headline_surf.get_width()) // 2, row0_y))

        rows_per_app = 2  # label+version row, description row

        # box_row leaves one blank row between the headline and the box
        # (2026-08-15, cosmetic per user request). box_rows now fills
        # the rest of the screen (2026-08-17, reverting the earlier
        # exact-fit sizing per user request) so "BY METAL SHOP" sits at
        # the bottom of the available space instead of right under the
        # last app row -- gives the box some breathing room instead of
        # shrink-wrapping to content. start_row leaves one further blank
        # row inside the box, under the title, before the first app row
        # (2026-08-17, same request).
        box_row = 2
        box_rows = d._height - box_row
        self.display.draw_panel_frame(canvas, Panel(
            0, box_row, d._width, box_rows, title, subtitle="BY METAL SHOP"))

        start_row = box_row + 2

        row = start_row
        for idx in range(len(APPS)):
            selected = idx == self.selected
            text_color = BLACK if selected else PRIMARY_COLOR
            highlight_x, px_y = d.char_px(1, row)
            px_x = highlight_x + d._char_w  # one extra space between the border/highlight and the text
            if selected:
                highlight_w = (d._width - 2) * d._char_w
                # Inset from the outer box's border by a few px on each
                # side (2026-08-14) -- previously expanded *outward* from
                # the exact content width instead, which put the
                # highlight right up against/into the border.
                HIGHLIGHT_GAP = 4
                pygame.draw.rect(canvas, PRIMARY_COLOR, (
                    highlight_x + HIGHLIGHT_GAP, px_y - 2,
                    highlight_w - 2 * HIGHLIGHT_GAP, d._char_h * 2 + 2))

            _key, label, desc, cmd, script_path, _hw_check = APPS[idx]
            app_version = read_app_version(script_path)
            line = d._font.render(f"{label} {app_version}".upper(), True, text_color)
            canvas.blit(line, (px_x, px_y))
            hw_status = hardware_status.get(cmd, "ready")
            hw_ok = hw_status == "ready"
            desc_text = desc.upper() if hw_ok else HW_STATUS_LABELS.get(hw_status, "HARDWARE NOT FOUND")
            # The warning color reads clearly against the black page
            # background; on the selected row's highlight it stays BLACK
            # like the rest of that row's text instead -- warning-on-fill
            # is low contrast for every scheme, and the text itself
            # already reads as a warning.
            desc_color = text_color if (hw_ok or selected) else WARNING_COLOR
            desc_line = d._font.render(desc_text, True, desc_color)
            canvas.blit(desc_line, (px_x + d._char_w * 2, px_y + d._char_h))
            row += rows_per_app

    def draw_power_dialog(self, canvas):
        lines = ["ARE YOU SURE YOU WANT", "TO SHUT DOWN?"]
        line_surfs = [self.osd_font.render(line, True, PRIMARY_COLOR) for line in lines]
        option_surfs = [
            self.option_font.render(opt, True, BLACK if i == self.power_dialog_selection else PRIMARY_COLOR)
            for i, opt in enumerate(POWER_OPTIONS)
        ]

        pad_x, pad_y, gap = 40, 24, 40
        options_w = sum(s.get_width() for s in option_surfs) + gap * (len(option_surfs) - 1)
        content_w = max(max(s.get_width() for s in line_surfs), options_w)
        content_h = (sum(s.get_height() for s in line_surfs) + 10 * (len(line_surfs) - 1)
                     + 30 + option_surfs[0].get_height())

        box = pygame.Surface((content_w + pad_x * 2, content_h + pad_y * 2))
        box.fill(BLACK)
        pygame.draw.rect(box, PRIMARY_COLOR, box.get_rect(), 3)

        y = pad_y
        for surf in line_surfs:
            box.blit(surf, ((box.get_width() - surf.get_width()) // 2, y))
            y += surf.get_height() + 10
        y += 20

        x = (box.get_width() - options_w) // 2
        for i, surf in enumerate(option_surfs):
            if i == self.power_dialog_selection:
                highlight = pygame.Rect(x - 10, y - 6, surf.get_width() + 20, surf.get_height() + 12)
                pygame.draw.rect(box, PRIMARY_COLOR, highlight)
            box.blit(surf, (x, y))
            x += surf.get_width() + gap

        canvas.blit(box, ((FRAME_W - box.get_width()) // 2, (FRAME_H - box.get_height()) // 2))

    def launch_app(self, cmd):
        """Launches one of the real apps as an actual subprocess (unlike
        switching pages, these are genuinely separate programs needing
        real console ownership). Jumps to page 0 if the app's Home
        button was pressed (it exits with EXIT_GOTO_HOME to signal
        that) -- otherwise stays on the menu page and refreshes
        hardware status."""
        self.release_console()
        result = None
        try:
            result = subprocess.run([cmd])
        except OSError as exc:
            # _activate_menu_selection() already checks readiness before
            # calling this, so this should be unreachable in practice --
            # kept as a backstop so a launcher that's missing/broken
            # some other way can never take the whole dashboard down
            # with it.
            print(f"Failed to launch {cmd}: {exc}", file=sys.stderr)
        finally:
            self.acquire_console()
            self.drain_stale_events()
        if result is not None and result.returncode == EXIT_GOTO_HOME:
            self.current_page = 0
            self.poller.refresh()
        else:
            self.refresh_hardware_status()
        self.render()

    def assign_to_puppet(self, target, cmd):
        """Remote-puppet equivalent of launch_app() -- assigns `cmd` to
        `target` (a PUPPETS name) via its STRINGS /assign endpoint
        instead of launching it locally. _activate_menu_selection()
        already checks readiness before ever calling this, using the
        same data the menu page renders from -- this call is the
        authoritative check, since that cached data could be stale
        (STRINGS's own /assign independently re-validates and rejects
        with 409 if the app isn't actually installed there, see
        strings.py's do_POST -- this is what surfaces that).

        On success, goes straight into control mode (2026-08-16) --
        picking an app is what puts the remote in live control of it,
        not a separate gesture afterward (that used to be OK on a
        gauge page; removed, see _handle_gauge_page_keycode). On
        failure, shows a blocking confirmation screen instead --
        there's nothing to control if the assignment didn't take, so
        this is the one remaining case that still needs an explicit
        message."""
        ip = dict(PUPPETS)[target]
        ok = False
        reason = "UNREACHABLE"
        try:
            req = urllib.request.Request(
                f"http://{ip}:{PUPPET_PORT}/assign",
                data=json.dumps({"app": cmd}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=2)
            ok = True
        except urllib.error.HTTPError as exc:
            reason = "NOT INSTALLED THERE" if exc.code == 409 else f"REJECTED ({exc.code})"
        except OSError:
            reason = "UNREACHABLE"

        if ok:
            self.remote_poller.set_app_hint(target, cmd)
            self.current_page = CONTROL_PAGE_INDEX
            self.control_mode_last_result = None
            self.render()
            return

        canvas = pygame.Surface((FRAME_W, FRAME_H))
        canvas.fill(BLACK)
        lines = [f"{target}: ASSIGN {cmd.upper()}", f"FAILED -- {reason}"]
        colors = [PRIMARY_COLOR, WARNING_COLOR]
        y = self.display._margin_y
        for line, color in zip(lines, colors):
            surf = self.display._font.render(line, True, color)
            canvas.blit(surf, ((FRAME_W - surf.get_width()) // 2, y))
            y += self.display._char_h
        self.fb.write_surface(canvas)

        time.sleep(2)
        self.render()

    def _current_hardware_status(self):
        """The same hardware-readiness dict the menu page renders from
        (self.hardware_status for LOCAL, the selected puppet's own
        STRINGS-reported `hardware` field otherwise) -- factored out so
        _activate_menu_selection() can check readiness before acting,
        not just display it."""
        if self.monitor_target == "LOCAL":
            return self.hardware_status
        stats, _fresh = self.remote_poller.get(self.monitor_target)
        return (stats or {}).get("hardware") or {}

    def _show_not_ready_message(self, cmd, hw_status="hardware_not_found"):
        canvas = pygame.Surface((FRAME_W, FRAME_H))
        canvas.fill(BLACK)
        where = "HERE" if self.monitor_target == "LOCAL" else f"ON {self.monitor_target}"
        reason = "NOT INSTALLED" if hw_status == "not_installed" else "NOT READY"
        lines = [f"{cmd.upper()} {reason}", where]
        y = self.display._margin_y
        for line in lines:
            surf = self.display._font.render(line, True, WARNING_COLOR)
            canvas.blit(surf, ((FRAME_W - surf.get_width()) // 2, y))
            y += self.display._char_h
        self.fb.write_surface(canvas)
        time.sleep(1.5)
        self.render()

    def _activate_menu_selection(self):
        """Enter on the menu page -- acts on whichever machine is
        currently selected: launches locally for LOCAL, assigns
        remotely for a puppet. Checks readiness first using the same
        data the row's own
        HARDWARE NOT FOUND label came from -- avoids a pointless network
        round-trip for the common case (a puppet doesn't have the app
        installed) and gives instant feedback; launch_app()/
        assign_to_puppet() each still have their own backstop for the
        cases this cached check could be stale for.

        Debounced (2026-08-16) -- the remote's OK button fires both
        KEY_ENTER (its keyboard HID interface) and BTN_LEFT (its mouse
        HID interface) for a single physical press, confirmed live via
        each device's reported EV_KEY capabilities. _handle_menu_page_
        keycode already treats both as equivalent "confirm" triggers
        (needed for the remote's air-mouse mode), so one real press was
        always calling this twice -- harmless before, when it only
        re-showed the same static confirmation screen, but now that a
        real app assignment launches an actual process on the puppet,
        two back-to-back /assign calls race STRINGS's openvt console
        handoff and crash the just-launched app (confirmed live: exit
        code 8, "Couldn't deallocate console 1", auto-recovers via
        STRINGS's own restart within ~3s, but visibly launches-quits-
        relaunches). A short debounce absorbs the duplicate without
        being perceptible on a genuine second press."""
        now = time.time()
        if now - self._last_activation_time < MENU_ACTIVATION_DEBOUNCE_SECONDS:
            return
        self._last_activation_time = now
        cmd = APPS[self.selected][3]
        hw_status = self._current_hardware_status().get(cmd, "ready")
        if hw_status != "ready":
            self._show_not_ready_message(cmd, hw_status)
            return
        if self.monitor_target == "LOCAL":
            self.launch_app(cmd)
        else:
            self.assign_to_puppet(self.monitor_target, cmd)

    def handle_keycode(self, code):
        """Returns True if the app should redraw after this key."""
        if self.power_dialog_active:
            return self.handle_power_dialog_keycode(code)
        if code == ecodes.KEY_COMPOSE:
            # TARGET/hamburger is global (2026-08-16) -- always jumps
            # straight to the Select Target page from any state,
            # including mid-control (this implicitly drops out of it,
            # since current_page just gets overwritten). Checked here,
            # once, instead of in each sub-handler below, which
            # previously had three different local meanings for this
            # same key -- see _build_target_select_screen. The power
            # dialog above stays modal; TARGET can't interrupt it. Flash
            # TARGET's box first if we're leaving the live-control
            # screen specifically -- elsewhere there's no button-box UI
            # to flash.
            if self.current_page == CONTROL_PAGE_INDEX:
                self._flash_button("target")
            self.current_page = TARGET_SELECT_PAGE_INDEX
            self.target_selected = MONITOR_TARGETS.index(self.monitor_target)
            return True
        if code in (ecodes.KEY_Q, ecodes.KEY_ESC):
            self._quit_requested = True
        elif code == ecodes.KEY_POWER and self.current_page != CONTROL_PAGE_INDEX:
            # Only opens SCRUTE's own local dialog outside the control
            # page -- while controlling a remote target, Power falls
            # through to the CONTROL_PAGE_INDEX branch below instead,
            # which relays it (now in CONTROL_RELAY_KEYS) so the
            # *target*'s own confirm dialog shows on the machine
            # actually being controlled, not on MP. See
            # CONTROL_RELAY_KEYS's comment for why this needed
            # HandlePowerKey=ignore fleet-wide first.
            self.power_dialog_active = True
            self.power_dialog_selection = 0
        elif code == ecodes.KEY_LEFT and self.current_page != CONTROL_PAGE_INDEX:
            self._cycle_page(-1)
        elif code == ecodes.KEY_RIGHT and self.current_page != CONTROL_PAGE_INDEX:
            self._cycle_page(1)
        elif self.current_page == TARGET_SELECT_PAGE_INDEX:
            return self._handle_target_select_keycode(code)
        elif self.current_page == CONTROL_PAGE_INDEX:
            return self._handle_control_mode_keycode(code)
        elif self.current_page == MENU_PAGE_INDEX:
            return self._handle_menu_page_keycode(code)
        elif self.current_page == SETTINGS_PAGE_INDEX:
            return self._handle_settings_page_keycode(code)
        else:
            return self._handle_gauge_page_keycode(code)
        return True

    def _cycle_page(self, direction):
        """Left/Right, handled globally for every page except
        CONTROL_PAGE_INDEX (2026-08-17 --
        previously each per-page handler had its own identical copy of
        this, before Select Target/Remote Control Help joined the
        normal rotation). NOT called at all while on CONTROL_PAGE_INDEX
        -- handle_keycode only routes Left/Right here when current_page
        != CONTROL_PAGE_INDEX, since Left/Right are also two of the
        keys CONTROL_RELAY_KEYS forwards to the target app there (e.g.
        BARS's pattern cycling) -- paging away instead of relaying
        would silently break that. Skips CONTROL_PAGE_INDEX whenever
        monitor_target is LOCAL -- relaying keypresses to yourself
        doesn't mean anything, so that page only exists in the rotation
        once a remote target is actually selected (see the comment
        above TARGET_SELECT_PAGE_INDEX). Resets control_mode_last_result
        on a fresh landing on the control page so a stale UNREACHABLE
        from a previous visit doesn't linger -- and re-syncs
        target_selected to whatever monitor_target actually is on a
        fresh landing on the target-select page, matching what the
        TARGET/hamburger fast-path already does (2026-08-17 fix --
        paging here with Left/Right used to leave the cursor wherever
        it was last left, possibly stale/not matching the real current
        target)."""
        page = self.current_page
        for _ in range(PAGE_COUNT):
            page = (page + direction) % PAGE_COUNT
            if page == CONTROL_PAGE_INDEX and self.monitor_target == "LOCAL":
                continue
            break
        if page == CONTROL_PAGE_INDEX and self.current_page != CONTROL_PAGE_INDEX:
            self.control_mode_last_result = None
        elif page == TARGET_SELECT_PAGE_INDEX and self.current_page != TARGET_SELECT_PAGE_INDEX:
            self.target_selected = MONITOR_TARGETS.index(self.monitor_target)
        elif page == SETTINGS_PAGE_INDEX and self.current_page != SETTINGS_PAGE_INDEX:
            # Re-sync the cursor to whatever's actually active on a fresh
            # landing, same reasoning as target_selected above -- without
            # this, leaving mid-browse (before pressing OK) and coming
            # back later would show the cursor on the stale spot instead
            # of the real current scheme.
            self.settings_selected = self.color_scheme_index
        self.current_page = page

    def _handle_gauge_page_keycode(self, code):
        """Pages 0/1 (CPU/MEM, WIFI/NET). Left/Right is handled globally
        now (see _cycle_page), before this is ever reached. Up/Down no
        longer switches which machine is shown here (2026-08-16,
        removed) -- that was the exact "confusing fast" behavior Select
        Target replaced; monitor_target is now only ever changed via
        TARGET/hamburger or the Select Target page (also handled
        globally/via current_page, before this is ever reached). OK/
        Enter no longer does anything on a gauge page either
        (2026-08-16, removed) -- control mode is entered by picking an
        app on the menu page now (see
        _activate_menu_selection/assign_to_puppet), not by a separate
        gesture on the stats pages."""
        if code in (ecodes.KEY_HOMEPAGE, ecodes.KEY_HOME, ecodes.KEY_BACK):
            self.current_page = 0  # Home always means "page 0" specifically now
        else:
            return False
        return True

    def _handle_control_mode_keycode(self, code):
        """Active on CONTROL_PAGE_INDEX -- D-pad/OK/Vol/Power relay live
        to monitor_target's running app (see _send_relay_key). House/
        APPS (2026-08-16) exits to that same target's app menu -- freed
        up by TARGET's move to a global hamburger binding (see
        handle_keycode), so House inherited hamburger's old fast-path
        job here. BACK exits all the way out to page 0. Q/Esc/TARGET/
        Left/Right are already handled globally in handle_keycode
        before this is ever reached -- Left/Right leaving this page is
        exactly what ends the relay (see _cycle_page), no separate
        "stop controlling" gesture needed. Power (2026-08-17) is
        deliberately NOT handled globally while on this page --
        handle_keycode's own KEY_POWER branch is skipped here on
        purpose so it falls through to the CONTROL_RELAY_KEYS branch
        below like any other relayed key. Every branch flashes its own
        button box first (see _flash_button) -- for the two that exit
        this screen, that means one frame of the live-control screen
        with the box inverted, then the transition, same as TARGET's
        own flash in handle_keycode."""
        button_id = CONTROL_BUTTON_IDS.get(code)
        if code in (ecodes.KEY_HOMEPAGE, ecodes.KEY_HOME):
            self._flash_button(button_id)
            self.current_page = MENU_PAGE_INDEX
        elif code == ecodes.KEY_BACK:
            self._flash_button(button_id)
            self.current_page = 0
        elif code in CONTROL_RELAY_KEYS:
            self._send_relay_key(code)
            self._flash_button(button_id)
        else:
            return False
        return True

    def _flash_button(self, button_id):
        """Briefly inverts one of the live-control screen's button
        boxes (orange fill, black text) to confirm a press was actually
        received -- see BUTTON_FLASH_SECONDS. Renders once with the
        flash showing, sleeps, then clears; the caller's own subsequent
        render() (every handle_keycode call site already does one when
        a handler returns True) shows the settled, non-flashing state
        -- same blocking-flash pattern as assign_to_puppet() elsewhere
        in this file."""
        if button_id is None:
            return
        self.last_pressed_button = button_id
        self.render()
        time.sleep(BUTTON_FLASH_SECONDS)
        self.last_pressed_button = None

    def _send_relay_key(self, code):
        """POSTs one keypress to monitor_target's STRINGS /input -- same
        blocking, short-timeout, HTTPError-vs-OSError shape every other
        SCRUTE-to-puppet call already uses (see assign_to_puppet). A
        failed send just updates the on-screen indicator; it doesn't
        exit the control page, since a single dropped packet on a flaky
        connection shouldn't kick you out of what could still be a
        perfectly usable session."""
        key_name = CONTROL_RELAY_KEYS[code]
        ip = dict(PUPPETS)[self.monitor_target]
        try:
            req = urllib.request.Request(
                f"http://{ip}:{PUPPET_PORT}/input",
                data=json.dumps({"key": key_name}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=1)
            self.control_mode_last_result = "OK"
        except (urllib.error.HTTPError, OSError):
            self.control_mode_last_result = "UNREACHABLE"

    def _handle_target_select_keycode(self, code):
        """Active on TARGET_SELECT_PAGE_INDEX -- reachable by paging
        there with Left/Right same as any other page, or jumped to
        directly via the global TARGET/hamburger binding (see
        handle_keycode) from any state. Up/Down moves the cursor over
        MONITOR_TARGETS, OK picks it (sets monitor_target and jumps
        straight to that machine's app menu, mirroring
        _activate_menu_selection's own landing choice). Back/Home goes
        to page 0, same as every other page's Home/Back (2026-08-17 --
        used to be a pure cancel back to whatever page you'd come from,
        but that "remember the previous page" state doesn't have a
        natural home now that this is a real page in the rotation
        rather than an overlay on top of one)."""
        if code in (ecodes.KEY_HOMEPAGE, ecodes.KEY_HOME, ecodes.KEY_BACK):
            self.current_page = 0
        elif code == ecodes.KEY_UP:
            self.target_selected = (self.target_selected - 1) % len(MONITOR_TARGETS)
        elif code == ecodes.KEY_DOWN:
            self.target_selected = (self.target_selected + 1) % len(MONITOR_TARGETS)
        elif code in (ecodes.KEY_ENTER, ecodes.KEY_KPENTER, ecodes.BTN_LEFT, ecodes.BTN_MOUSE):
            self.monitor_target = MONITOR_TARGETS[self.target_selected]
            self.current_page = MENU_PAGE_INDEX
        else:
            return False
        return True

    def _build_control_mode_screen(self, canvas):
        """Dedicated full-screen view for CONTROL_PAGE_INDEX --
        deliberately not a box-per-panel style page like the others, so
        it's unambiguous that the remote is now relaying to a puppet's
        own running app instead of navigating SCRUTE itself (the exact
        confusion this whole feature exists to fix). Always shows
        monitor_target -- Left/Right skips this page entirely whenever
        that's LOCAL (see _cycle_page), so it's always a real puppet by
        the time this renders.

        Layout matches the physical remote's actual button positions
        1:1 (built + approved 2026-08-16 from a hand-drawn 40x16 ASCII
        layout checked against the real remote -- see
        REMOTE-MENU-LAYOUT.txt / help_screen_preview.png), not the
        box-per-panel style the other pages use. Grid coordinates below
        are read straight off that layout."""
        canvas.fill(BLACK)
        d = self.display
        stats, fresh = self.remote_poller.get(self.monitor_target)
        target_label = (stats or {}).get("hostname", self.monitor_target).upper()

        headline = f"CONTROLLING: {target_label}"
        headline_surf = d._label_font.render(headline, True, PRIMARY_COLOR)
        row0_y = d.char_px(0, 0)[1]
        canvas.blit(headline_surf, ((FRAME_W - headline_surf.get_width()) // 2, row0_y))

        def centered(text, width):
            pad = width - len(text)
            left = pad // 2
            return (" " * left) + text + (" " * (pad - left))

        # Flash fill for a *bordered* box sits inside the border's own
        # content rect (same rect draw_panel_frame itself returns --
        # one full char cell in from each edge, so the fill never
        # touches the border ring's cells at all) plus a further
        # BOX_FLASH_GAP px, so there's genuine black space visible
        # between the fill and the border rather than the two abutting
        # (2026-08-17 -- the original small FLASH_GAP inset from the
        # box's outer pixel edge, not the border's own cell, left the
        # fill overlapping most of the border ring, so a "pressed" box
        # just read as solid-orange-with-no-visible-border on the real
        # CRT). OK's own flash below is unboxed and keeps the old,
        # smaller OK_FLASH_GAP -- there's no border there to get lost
        # against.
        BOX_FLASH_GAP = 3
        OK_FLASH_GAP = 4

        def draw_box(col, row, w, h, label, button_id=None):
            # Pressed box gets an inset orange fill (drawn before the
            # border/text so they layer on top, same order draw_panel_
            # frame's title/subtitle already uses) with black text.
            # See _flash_button/last_pressed_button.
            pressed = button_id is not None and button_id == self.last_pressed_button
            if pressed:
                x, y = d.char_px(col, row)
                fill_x = x + d._char_w + BOX_FLASH_GAP
                fill_y = y + d._char_h + BOX_FLASH_GAP
                fill_w = (w - 2) * d._char_w - 2 * BOX_FLASH_GAP
                fill_h = (h - 2) * d._char_h - 2 * BOX_FLASH_GAP
                pygame.draw.rect(canvas, PRIMARY_COLOR, (fill_x, fill_y, fill_w, fill_h))
            d.draw_panel_frame(canvas, Panel(col, row, w, h, title=None))
            content_row = row + (h - 1) // 2
            d.draw_text(canvas, col + 1, content_row, centered(label, w - 2),
                         color=BLACK if pressed else PRIMARY_COLOR)

        draw_box(0, 1, 10, 3, "APPS", "apps")
        draw_box(30, 1, 10, 3, "NOFX")  # no keycode identified yet -- never flashes, see CONTROL_BUTTON_IDS

        draw_box(14, 3, 12, 3, CONTROL_DPAD_LABELS["up"], "up")

        draw_box(1, 5, 12, 3, CONTROL_DPAD_LABELS["left"], "left")
        ok_pressed = self.last_pressed_button == "ok"
        if ok_pressed:  # deliberately unboxed (user's design choice) -- inset fill matches the boxed buttons'
            x, y = d.char_px(14, 6)
            pygame.draw.rect(canvas, PRIMARY_COLOR, (
                x + OK_FLASH_GAP, y, 10 * d._char_w - 2 * OK_FLASH_GAP, d._char_h))
        d.draw_text(canvas, 14 + 1, 6, centered("OK", 10), color=BLACK if ok_pressed else PRIMARY_COLOR)
        draw_box(27, 5, 12, 3, CONTROL_DPAD_LABELS["right"], "right")

        draw_box(14, 7, 12, 3, CONTROL_DPAD_LABELS["down"], "down")

        draw_box(0, 9, 10, 3, "TARGET", "target")
        draw_box(30, 9, 10, 3, "BACK", "back")

        draw_box(0, 12, 10, 3, "MUTE", "mute")
        draw_box(30, 12, 10, 3, "VOL+", "volup")

        # Footer slot doubles as the status line -- swapped for a warning
        # instead of growing the layout with an extra row, since the
        # 40x16 grid this screen is built against has no spare row below
        # the MUTE/VOL+ boxes.
        if not fresh:
            footer, footer_color = f"{target_label} NOT RESPONDING", WARNING_COLOR
        elif self.control_mode_last_result == "UNREACHABLE":
            footer, footer_color = "LAST SEND FAILED -- UNREACHABLE", WARNING_COLOR
        else:
            app_cmd = (stats or {}).get("app")
            if app_cmd:
                app_label = next(
                    (label for _key, label, _desc, cmd, *_ in APPS if cmd == app_cmd),
                    app_cmd.upper(),
                )
            else:
                app_label = "NONE"
            footer, footer_color = f"RUNNING: {app_label}", PRIMARY_COLOR
        footer_surf = d._font.render(footer, True, footer_color)
        row15_y = d.char_px(0, 15)[1]
        canvas.blit(footer_surf, ((FRAME_W - footer_surf.get_width()) // 2, row15_y))

    def _build_target_select_screen(self, canvas):
        """TARGET/hamburger overlay (2026-08-16) -- a flat picker over
        MONITOR_TARGETS (LOCAL/P1-P4/PRODUCTION), styled like the app
        menu's box but simpler: one row per machine, no version/hardware
        columns, since machines don't have those the way apps do.
        Selecting a row hands off to _handle_target_select_keycode."""
        canvas.fill(BLACK)
        d = self.display

        headline_surf = d._label_font.render("SELECT TARGET", True, PRIMARY_COLOR)
        row0_y = d.char_px(0, 0)[1]
        canvas.blit(headline_surf, ((FRAME_W - headline_surf.get_width()) // 2, row0_y))

        title = f"CENTRAL SCRUTINIZER {VERSION}".upper()
        box_row = 2
        # Fills the rest of the screen and leaves a blank row under the
        # title before the first row -- same 2026-08-17 change as
        # _build_menu_page, for the same "give it some breathing room"
        # request.
        box_rows = d._height - box_row
        self.display.draw_panel_frame(canvas, Panel(
            0, box_row, d._width, box_rows, title, subtitle="BY METAL SHOP"))

        row = box_row + 2
        for idx, name in enumerate(MONITOR_TARGETS):
            selected = idx == self.target_selected
            text_color = BLACK if selected else PRIMARY_COLOR
            highlight_x, px_y = d.char_px(1, row)
            px_x = highlight_x + d._char_w
            if selected:
                highlight_w = (d._width - 2) * d._char_w
                HIGHLIGHT_GAP = 4
                pygame.draw.rect(canvas, PRIMARY_COLOR, (
                    highlight_x + HIGHLIGHT_GAP, px_y - 2,
                    highlight_w - 2 * HIGHLIGHT_GAP, d._char_h + 2))

            if name == "LOCAL":
                label = self.hostname
            else:
                stats, _fresh = self.remote_poller.get(name)
                label = (stats or {}).get("hostname", name).upper()
            line = d._font.render(label, True, text_color)
            canvas.blit(line, (px_x, px_y))
            row += 1

    def _build_settings_page(self, canvas):
        """Page SETTINGS_PAGE_INDEX (2026-08-18) -- currently just the one
        COLOR: setting, stacked vertically (2026-08-18 revision -- the
        original horizontal ORANGE/GREEN/WHITE row on one line conflicted
        with Left/Right's global page-navigation meaning, so there was no
        way to move between the three options at all). Deliberately NOT
        run through _page_headline like the gauge/menu pages --
        "CONTROLLING: <target>" doesn't make sense here, since a color
        scheme is SCRUTE's own display preference, not something tied to
        whichever puppet monitor_target happens to point at. Mirrors
        _build_target_select_screen's fixed-headline/box-list structure,
        including its exact highlight-bar row style, since this is now
        the same shape of UI: a vertical list, Up/Down moves the cursor,
        OK commits (see _handle_settings_page_keycode)."""
        canvas.fill(BLACK)
        d = self.display

        headline_surf = d._label_font.render("SETTINGS", True, PRIMARY_COLOR)
        row0_y = d.char_px(0, 0)[1]
        canvas.blit(headline_surf, ((FRAME_W - headline_surf.get_width()) // 2, row0_y))

        title = f"CENTRAL SCRUTINIZER {VERSION}".upper()
        box_row = 2
        box_rows = d._height - box_row
        self.display.draw_panel_frame(canvas, Panel(
            0, box_row, d._width, box_rows, title, subtitle="BY METAL SHOP"))

        row = box_row + 2
        label_x, px_y = d.char_px(1, row)
        label_surf = d._font.render(" COLOR:", True, PRIMARY_COLOR)
        canvas.blit(label_surf, (label_x, px_y))
        row += 1

        for idx, (name, _primary, _warn) in enumerate(COLOR_SCHEMES):
            selected = idx == self.settings_selected
            text_color = BLACK if selected else PRIMARY_COLOR
            highlight_x, opt_y = d.char_px(1, row)
            opt_x = highlight_x + d._char_w
            if selected:
                highlight_w = (d._width - 2) * d._char_w
                HIGHLIGHT_GAP = 4
                pygame.draw.rect(canvas, PRIMARY_COLOR, (
                    highlight_x + HIGHLIGHT_GAP, opt_y - 2,
                    highlight_w - 2 * HIGHLIGHT_GAP, d._char_h + 2))
            line = d._font.render(name, True, text_color)
            canvas.blit(line, (opt_x, opt_y))
            row += 1

    def _handle_menu_page_keycode(self, code):
        """Page MENU_PAGE_INDEX -- Left/Right is handled globally now
        (see _cycle_page), before this is ever reached. Up/Down here
        means moving the app-selection cursor; monitor_target (which
        machine this menu acts on) is changed via TARGET/hamburger or
        the Select Target page, not from here. Enter activates the
        highlighted row against monitor_target (see
        _activate_menu_selection)."""
        if code in (ecodes.KEY_HOMEPAGE, ecodes.KEY_HOME, ecodes.KEY_BACK):
            self.current_page = 0
        elif code == ecodes.KEY_UP:
            self.selected = (self.selected - 1) % len(APPS)
        elif code == ecodes.KEY_DOWN:
            self.selected = (self.selected + 1) % len(APPS)
        elif code in (ecodes.KEY_ENTER, ecodes.KEY_KPENTER, ecodes.BTN_LEFT, ecodes.BTN_MOUSE):
            self._activate_menu_selection()
            return False  # already rendered
        else:
            return False
        return True

    def _handle_settings_page_keycode(self, code):
        """Page SETTINGS_PAGE_INDEX -- Left/Right is handled globally
        (see _cycle_page) for page navigation, so it can't also mean
        "change the COLOR option" here (that was the original
        2026-08-18 design's bug -- Left/Right just paged away instead).
        Up/Down moves self.settings_selected over the three stacked
        COLOR_SCHEMES rows instead, mirroring the app menu/Select
        Target's own Up/Down-then-OK convention exactly. OK commits the
        highlighted row -- applies it live and persists it -- moving the
        cursor alone does NOT change the active scheme, same as
        highlighting an app on the menu page doesn't launch it."""
        if code in (ecodes.KEY_HOMEPAGE, ecodes.KEY_HOME, ecodes.KEY_BACK):
            self.current_page = 0
        elif code == ecodes.KEY_UP:
            self.settings_selected = (self.settings_selected - 1) % len(COLOR_SCHEMES)
        elif code == ecodes.KEY_DOWN:
            self.settings_selected = (self.settings_selected + 1) % len(COLOR_SCHEMES)
        elif code in (ecodes.KEY_ENTER, ecodes.KEY_KPENTER, ecodes.BTN_LEFT, ecodes.BTN_MOUSE):
            self.color_scheme_index = self.settings_selected
            apply_color_scheme(self.color_scheme_index)
            save_color_scheme_index(self.color_scheme_index)
        else:
            return False
        return True

    def handle_power_dialog_keycode(self, code):
        if code in (ecodes.KEY_LEFT, ecodes.KEY_UP):
            self.power_dialog_selection = (self.power_dialog_selection - 1) % len(POWER_OPTIONS)
        elif code in (ecodes.KEY_RIGHT, ecodes.KEY_DOWN):
            self.power_dialog_selection = (self.power_dialog_selection + 1) % len(POWER_OPTIONS)
        elif code in (ecodes.KEY_ENTER, ecodes.KEY_KPENTER, ecodes.BTN_LEFT, ecodes.BTN_MOUSE):
            choice = POWER_OPTIONS[self.power_dialog_selection]
            if choice == "NO":
                self.power_dialog_active = False
            else:
                return "shutdown" if choice == "YES" else "restart"
        elif code in (ecodes.KEY_ESC, ecodes.KEY_BACK, ecodes.KEY_POWER):
            self.power_dialog_active = False
        else:
            return False
        return True

    def handle_rel_event(self, code, value):
        if code == ecodes.REL_X:
            axis = "x"
        elif code == ecodes.REL_Y:
            axis = "y"
        else:
            return False
        self._rel_accum[axis] += value
        accum = self._rel_accum[axis]
        if abs(accum) < MOUSE_MOVE_THRESHOLD:
            return False
        self._rel_accum[axis] = 0
        if axis == "x":
            synthetic = ecodes.KEY_RIGHT if accum > 0 else ecodes.KEY_LEFT
        else:
            synthetic = ecodes.KEY_DOWN if accum > 0 else ecodes.KEY_UP
        return self.handle_keycode(synthetic)

    def run(self):
        try:
            self.render()
            last_stats_refresh = time.time()
            last_hw_refresh = time.time()
            last_input_time = time.time()
            while not self._quit_requested:
                for key, _ in self.selector.select(timeout=IDLE_POLL_TIMEOUT):
                    device = key.fileobj
                    for event in device.read():
                        if event.type == ecodes.EV_KEY and event.value == 1:
                            result = self.handle_keycode(event.code)
                        elif event.type == ecodes.EV_REL:
                            result = self.handle_rel_event(event.code, event.value)
                        else:
                            continue
                        last_input_time = time.time()
                        if result in ("shutdown", "restart"):
                            self.pending_power_action = result
                            self._quit_requested = True
                        elif result:
                            self.render()
                    if self._quit_requested:
                        break

                if self._quit_requested:
                    break

                now = time.time()
                if self.current_page != MENU_PAGE_INDEX and now - last_stats_refresh >= STATS_REFRESH_SECONDS:
                    self.poller.refresh()
                    last_stats_refresh = now
                    self.render()
                # Screensaver: 2 minutes idle while sitting on the menu
                # page (not a gauge page, not mid-dialog) bounces back to
                # page 0 exactly like a Back/Home press would.
                elif (self.current_page == MENU_PAGE_INDEX and not self.power_dialog_active
                        and now - last_input_time >= IDLE_TIMEOUT_SECONDS):
                    self.current_page = 0
                    self.poller.refresh()
                    last_input_time = now
                    self.render()

                # Independent of the page-specific branch above -- a
                # hardware/connectivity check (e.g. WEATHERSTAR's
                # internet-reachability check) that fails once used to
                # stay "HARDWARE NOT FOUND" forever, since the only
                # other triggers were startup and returning from a
                # launched app. Re-polling periodically means a
                # transient blip self-heals instead of needing a
                # restart to notice it cleared. Only re-renders on the
                # menu page, the only place this is ever shown.
                if now - last_hw_refresh >= HARDWARE_REFRESH_SECONDS:
                    self.refresh_hardware_status()
                    last_hw_refresh = now
                    if self.current_page == MENU_PAGE_INDEX:
                        self.render()
        finally:
            self.fb.close()
            if self.console_graphics_mode:
                fcntl.ioctl(self.tty_fd, KDSETMODE, KD_TEXT)
                os.write(self.tty_fd, b"\033[2J\033[H")
            if self.tty_fd is not None:
                os.close(self.tty_fd)
            pygame.quit()

        if self.pending_power_action == "shutdown":
            subprocess.run(["sudo", "shutdown", "-h", "now"])
        elif self.pending_power_action == "restart":
            subprocess.run(["sudo", "shutdown", "-r", "now"])


def main():
    HealthApp().run()


if __name__ == "__main__":
    main()
