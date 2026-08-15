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

VERSION = "2.1"

BASE_DIR = Path(__file__).resolve().parent
FONT_PATH = BASE_DIR / "VCR_OSD_MONO_1.001.ttf"
BORDER_FONT_PATH = BASE_DIR / "Px437_IBM_VGA_9x16.ttf"  # box-drawing glyphs only, VCR OSD MONO lacks them

FRAME_W, FRAME_H = 720, 480
UNDERSCAN = 0.10
TARGET_WIDTH = 40

BLACK = (0, 0, 0)
ORANGE = (0xFF, 0xA5, 0x00)
RED = (220, 30, 30)  # HARDWARE NOT FOUND only -- every other color is ORANGE

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
]
MONITOR_TARGETS = ["LOCAL"] + [name for name, _ip in PUPPETS]
PUPPET_PORT = 8420

# The app menu is a 3rd page (index 2) of the same health screen, not a
# separate "screen" mode -- Up/Down means "switch monitored machine" on
# the gauge pages (0/1) and "move the menu cursor" on the menu page,
# Left/Right always means "change page" regardless of which one you're
# on. Selecting an app on the menu page acts on whichever machine is
# currently selected (self.monitor_target): launches locally for LOCAL,
# assigns remotely via STRINGS for a puppet. 2026-08-15 redesign.
MENU_PAGE_INDEX = 2
PAGE_COUNT = 3
PUPPET_POLL_TIMEOUT_SECONDS = 2
PUPPET_POLL_INTERVAL_SECONDS = 3

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

# Not a real local app -- selecting this menu row (handled separately
# from APPS/launch_app() in _handle_menu_keycode/identify_puppets())
# broadcasts an assign-to-"identify" command to every puppet's STRINGS,
# putting each on SMPTE bars + its own hostname overlay so you can
# match physical CRTs to Pis after the McBrain stack gets moved and
# recabled. Same fallback bars.py runs by default whenever a puppet has
# no real assignment at all -- see STRINGS's IDLE_APP.
IDENTIFY_LABEL = "IDENTIFY PUPPETS"
IDENTIFY_DESC = "SMPTE BARS + HOSTNAME OVERLAY"
MENU_ITEM_COUNT = len(APPS) + 1  # the 4 real apps + the IDENTIFY PUPPETS row

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

    def draw_text(self, canvas, col, row, text, color=ORANGE, bold=False, font=None):
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
        # bold=True: same interlace-flicker reasoning as draw_bar()'s 2px
        # border -- these box-drawing glyphs' strokes are only ~1px at
        # this font size, and synthetic bold is the only way to thicken a
        # font-rendered glyph (there's no line-width knob like
        # pygame.draw.rect has). Applies to the title/subtitle text too,
        # which reads fine.
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
            self.draw_text(canvas, panel.col + TITLE_INDENT, panel.row, title_text, bold=True)
        if subtitle_text:
            self.draw_text(canvas, panel.col + subtitle_start, bottom_row, subtitle_text, bold=True)
        for r in range(1, panel.h_chars - 1):
            self.draw_text(canvas, panel.col, panel.row + r, BOX_V, bold=True, font=self._border_font)
            self.draw_text(canvas, panel.col + panel.w_chars - 1, panel.row + r, BOX_V, bold=True, font=self._border_font)

        content_x = x + self._char_w
        content_y = y + self._char_h
        content_w = (panel.w_chars - 2) * self._char_w
        content_h = (panel.h_chars - 2) * self._char_h
        return pygame.Rect(content_x, content_y, content_w, content_h)

    def draw_bar(self, canvas, rect, fraction, label=None, color=ORANGE):
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
        fraction = max(0.0, min(1.0, fraction))
        bar_rect = pygame.Rect(rect.x + self._char_w, rect.y + 2, rect.width - self._char_w, rect.height - 2)
        label_surf = None
        if label:
            label_surf = self._label_font.render(label, True, ORANGE)
            bar_rect.width -= label_surf.get_width() + 12
        # Border is 2px, not 1px -- a single-scanline-thin horizontal edge
        # only lands on one of the two interlaced fields each frame, which
        # reads as visible jitter/flicker on a real CRT (inherent to
        # interlacing, not a rendering bug -- confirmed on real composite
        # output). 2px puts the top/bottom edges on both fields every
        # frame. The fill inset grows to match so it still sits cleanly
        # inside the thicker border rather than overlapping it.
        pygame.draw.rect(canvas, ORANGE, bar_rect, 2)
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
    surf = display._font.render(f" TEMP: {temp_text}   CLOCK: {clock_text}", True, ORANGE)
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
    surf = display._font.render(f" LOAD: {load1:.2f} {load5:.2f} {load15:.2f}", True, ORANGE)
    canvas.blit(surf, (rect.x, y))
    y += display._char_h

    status = stats.get("throttled", "UNKNOWN")
    surf = display._font.render(f" STATUS: {status}", True, ORANGE)
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
    surf = display._font.render(f" USED:  {used_mb:.0f} MB", True, ORANGE)
    canvas.blit(surf, (rect.x, y))
    y += display._char_h
    surf = display._font.render(f" TOTAL: {total_mb:.0f} MB", True, ORANGE)
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
    surf = display._font.render(f" {used_gb:.0f} GB USED   {free_gb:.0f} GB FREE", True, ORANGE)
    canvas.blit(surf, (rect.x, y))


def draw_app_panel(display, canvas, rect, stats):
    """Used in place of draw_storage_panel when viewing a remote puppet
    (see HealthApp._remote_panels) -- which app it's running, and which
    of the sibling apps are actually ready to run *there* (real
    hardware/connectivity, checked on that puppet -- see STRINGS's
    ReadinessChecker), is more useful there than disk usage."""
    app = stats.get("app")
    text = f" RUNNING: {app.upper()}" if app else " RUNNING: (NONE ASSIGNED)"
    surf = display._font.render(text, True, ORANGE)
    canvas.blit(surf, (rect.x, rect.y))

    hardware = stats.get("hardware") or {}
    ready = [name.upper() for name, ok in hardware.items() if ok]
    ready_text = " READY: " + " ".join(ready) if ready else " READY: (NONE)"
    ready_surf = display._font.render(ready_text, True, ORANGE)
    canvas.blit(ready_surf, (rect.x, rect.y + display._char_h))


def draw_wifi_panel(display, canvas, rect, stats):
    y = rect.y
    ssid = stats.get("wifi_ssid") or "NOT CONNECTED"
    surf = display._font.render(f" SSID: {ssid}", True, ORANGE)
    canvas.blit(surf, (rect.x, y))
    y += display._char_h

    ip = stats.get("ip", "N/A")
    surf = display._font.render(f" IP: {ip}", True, ORANGE)
    canvas.blit(surf, (rect.x, y))
    y += display._char_h

    quality = stats.get("wifi_quality")
    level = stats.get("wifi_level")
    if quality is not None:
        bar_rect = pygame.Rect(rect.x, y, rect.width, display._char_h - 4)
        display.draw_bar(canvas, bar_rect, quality / 70.0, label=f" {quality:.0f}/70 ")
        y += display._char_h
        surf = display._font.render(f" SIGNAL: {level:.0f} DBM", True, ORANGE)
        canvas.blit(surf, (rect.x, y))
    else:
        surf = display._font.render(" NO WIRELESS LINK", True, ORANGE)
        canvas.blit(surf, (rect.x, y))


def draw_network_panel(display, canvas, rect, stats):
    y = rect.y
    up = stats.get("net_up_kbs")
    down = stats.get("net_down_kbs")
    up_text = f"{up:6.1f} KB/S" if up is not None else "  --.- KB/S"
    down_text = f"{down:6.1f} KB/S" if down is not None else "  --.- KB/S"
    surf = display._font.render(f" UP:   {up_text}", True, ORANGE)
    canvas.blit(surf, (rect.x, y))
    y += display._char_h
    surf = display._font.render(f" DOWN: {down_text}", True, ORANGE)
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
        # (see build_health_canvas), panels start at row 1 instead of 0.
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
        self.pages = [
            [
                Panel(0, 1, w, cpu_h, "CPU", draw_cpu_panel),
                Panel(0, 1 + cpu_h, w, mem_h, "MEMORY", draw_memory_panel),
            ],
            [
                Panel(0, 1, w, wifi_h, "WIFI", draw_wifi_panel),
                Panel(0, 1 + wifi_h, w, net_h, "NETWORK", draw_network_panel),
                Panel(0, 1 + wifi_h + net_h, w, storage_h, "STORAGE", draw_storage_panel),
            ],
        ]
        self.current_page = 0
        self.hostname = socket.gethostname().upper()
        self.remote_poller = RemotePoller()
        self.monitor_target = "LOCAL"  # cycled by Up/Down on the health screen -- "LOCAL" or a PUPPETS name

        # App-menu screen state.
        self.selected = 0
        self.hardware_status = {}
        self.refresh_hardware_status()

        self.power_dialog_active = False
        self.power_dialog_selection = 0
        self.pending_power_action = None
        self._rel_accum = {"x": 0, "y": 0}

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
        status = {}
        for _key, _label, _desc, cmd, _script, hw_check in APPS:
            if hw_check is None:
                status[cmd] = True
                continue
            try:
                status[cmd] = hw_check()
            except Exception:
                # A broken check should read as "hardware not found," not
                # take the whole menu down.
                status[cmd] = False
        self.hardware_status = status

    def render(self):
        canvas = pygame.Surface((FRAME_W, FRAME_H))
        self.build_health_canvas(canvas)  # dispatches to the menu page internally for MENU_PAGE_INDEX
        if self.power_dialog_active:
            self.draw_power_dialog(canvas)
        self.fb.write_surface(canvas)

    def _remote_panels(self, page_idx):
        """Page 2 (WIFI/NETWORK/STORAGE) swaps its STORAGE slot for an APP
        panel when viewing a remote puppet -- STRINGS doesn't report WiFi
        stats at all yet (draw_wifi_panel/draw_network_panel already
        degrade gracefully to their "no signal"/placeholder states
        against a stats dict missing those keys, so page 2 isn't very
        informative for a puppet regardless), and knowing which app is
        running is more useful there than remote disk usage anyway."""
        panels = self.pages[page_idx]
        if page_idx != 1:
            return panels
        storage_panel = panels[2]
        app_panel = Panel(storage_panel.col, storage_panel.row, storage_panel.w_chars,
                           storage_panel.h_chars, "APP", draw_app_panel)
        return [panels[0], panels[1], app_panel]

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
            panels = self._remote_panels(self.current_page)

        if offline:
            canvas.fill(BLACK)
            msg = f"{target_label} OFFLINE / UNREACHABLE"
            msg_surf = self.display._font.render(msg, True, RED)
            canvas.blit(msg_surf, ((FRAME_W - msg_surf.get_width()) // 2, (FRAME_H - msg_surf.get_height()) // 2))
        else:
            self.display.render_page(canvas, panels, stats)

        headline = f"{target_label} - PAGE {self.current_page + 1}/{PAGE_COUNT}"
        headline_surf = self.display._label_font.render(headline, True, ORANGE)
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

        # Same "{TARGET} - PAGE n/m" headline pages 0/1 show, in the same
        # top-line position (row 0, above the box) -- keeps the headline
        # position consistent across all 3 pages instead of nesting it
        # inside the box like before.
        headline = f"{target_label} - PAGE {self.current_page + 1}/{PAGE_COUNT}"
        headline_surf = d._label_font.render(headline, True, ORANGE)
        row0_y = d.char_px(0, 0)[1]
        canvas.blit(headline_surf, ((FRAME_W - headline_surf.get_width()) // 2, row0_y))

        box_row = 1  # box starts one row down, directly under the headline
        box_rows = d._height - box_row
        self.display.draw_panel_frame(canvas, Panel(
            0, box_row, d._width, box_rows, title, subtitle="BY METAL SHOP"))

        rows_per_app = 2  # label+version row, description row
        total_rows = rows_per_app * MENU_ITEM_COUNT
        start_row = box_row + 2 + max(0, (box_rows - 3 - total_rows) // 2)

        row = start_row
        for idx in range(MENU_ITEM_COUNT):
            is_identify_row = idx == len(APPS)
            selected = idx == self.selected
            text_color = BLACK if selected else ORANGE
            highlight_x, px_y = d.char_px(1, row)
            px_x = highlight_x + d._char_w  # one extra space between the border/highlight and the text
            if selected:
                highlight_w = (d._width - 2) * d._char_w
                # Inset from the outer box's border by a few px on each
                # side (2026-08-14) -- previously expanded *outward* from
                # the exact content width instead, which put the
                # highlight right up against/into the border.
                HIGHLIGHT_GAP = 4
                pygame.draw.rect(canvas, ORANGE, (
                    highlight_x + HIGHLIGHT_GAP, px_y - 2,
                    highlight_w - 2 * HIGHLIGHT_GAP, d._char_h * 2 + 2))

            if is_identify_row:
                line = d._font.render(IDENTIFY_LABEL, True, text_color)
                canvas.blit(line, (px_x, px_y))
                desc_line = d._font.render(IDENTIFY_DESC, True, text_color)
                canvas.blit(desc_line, (px_x + d._char_w * 2, px_y + d._char_h))
                row += rows_per_app
                continue

            _key, label, desc, cmd, script_path, _hw_check = APPS[idx]
            app_version = read_app_version(script_path)
            line = d._font.render(f"{label} {app_version}".upper(), True, text_color)
            canvas.blit(line, (px_x, px_y))
            hw_ok = hardware_status.get(cmd, True)
            desc_text = desc.upper() if hw_ok else "HARDWARE NOT FOUND"
            # RED on the normal black background makes the warning stand
            # out; on the selected row's orange highlight it stays BLACK
            # like the rest of that row's text -- red-on-orange is low
            # contrast, and the text itself already reads as a warning.
            desc_color = text_color if (hw_ok or selected) else RED
            desc_line = d._font.render(desc_text, True, desc_color)
            canvas.blit(desc_line, (px_x + d._char_w * 2, px_y + d._char_h))
            row += rows_per_app

    def draw_power_dialog(self, canvas):
        lines = ["ARE YOU SURE YOU WANT", "TO SHUT DOWN?"]
        line_surfs = [self.osd_font.render(line, True, ORANGE) for line in lines]
        option_surfs = [
            self.option_font.render(opt, True, BLACK if i == self.power_dialog_selection else ORANGE)
            for i, opt in enumerate(POWER_OPTIONS)
        ]

        pad_x, pad_y, gap = 40, 24, 40
        options_w = sum(s.get_width() for s in option_surfs) + gap * (len(option_surfs) - 1)
        content_w = max(max(s.get_width() for s in line_surfs), options_w)
        content_h = (sum(s.get_height() for s in line_surfs) + 10 * (len(line_surfs) - 1)
                     + 30 + option_surfs[0].get_height())

        box = pygame.Surface((content_w + pad_x * 2, content_h + pad_y * 2))
        box.fill(BLACK)
        pygame.draw.rect(box, ORANGE, box.get_rect(), 3)

        y = pad_y
        for surf in line_surfs:
            box.blit(surf, ((box.get_width() - surf.get_width()) // 2, y))
            y += surf.get_height() + 10
        y += 20

        x = (box.get_width() - options_w) // 2
        for i, surf in enumerate(option_surfs):
            if i == self.power_dialog_selection:
                highlight = pygame.Rect(x - 10, y - 6, surf.get_width() + 20, surf.get_height() + 12)
                pygame.draw.rect(box, ORANGE, highlight)
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

    def _send_identify(self, ip):
        try:
            req = urllib.request.Request(
                f"http://{ip}:{PUPPET_PORT}/assign",
                data=json.dumps({"app": "identify"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=1.5)
            return True
        except OSError:
            return False

    def identify_puppets(self):
        """Broadcasts an assign-to-'identify' command to every puppet
        (see STRINGS's IDLE_APP/LAUNCH_COMMANDS) -- each one switches to
        SMPTE bars + its own hostname overlay, so physical Pi/CRT
        pairings can be read straight off the screens after a move.
        Blocks briefly for the confirmation, same as launch_app()'s
        subprocess.run() already does -- this is a rare, deliberate
        action, not something that needs main-loop-timer integration."""
        results = [(name, self._send_identify(ip)) for name, ip in PUPPETS]

        canvas = pygame.Surface((FRAME_W, FRAME_H))
        canvas.fill(BLACK)
        lines = ["IDENTIFY SENT"] + [f"{name}: {'OK' if ok else 'UNREACHABLE'}" for name, ok in results]
        y = self.display._margin_y
        for line in lines:
            color = RED if line.endswith("UNREACHABLE") else ORANGE
            surf = self.display._font.render(line, True, color)
            canvas.blit(surf, ((FRAME_W - surf.get_width()) // 2, y))
            y += self.display._char_h
        self.fb.write_surface(canvas)

        time.sleep(2)
        self.render()

    def assign_to_puppet(self, target, cmd):
        """Remote-puppet equivalent of launch_app() -- assigns `cmd` to
        `target` (a PUPPETS name) via its STRINGS /assign endpoint
        instead of launching it locally. Blocks briefly for a
        confirmation screen, same pattern as identify_puppets().
        _activate_menu_selection() already checks readiness before ever
        calling this, using the same data the menu page renders from --
        this call is the authoritative check, since that cached data
        could be stale (STRINGS's own /assign independently re-validates
        and rejects with 409 if the app isn't actually installed there,
        see strings.py's do_POST -- this is what surfaces that)."""
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

        canvas = pygame.Surface((FRAME_W, FRAME_H))
        canvas.fill(BLACK)
        lines = [f"{target}: ASSIGN {cmd.upper()}", "OK" if ok else f"FAILED -- {reason}"]
        colors = [ORANGE, ORANGE if ok else RED]
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

    def _show_not_ready_message(self, cmd):
        canvas = pygame.Surface((FRAME_W, FRAME_H))
        canvas.fill(BLACK)
        where = "HERE" if self.monitor_target == "LOCAL" else f"ON {self.monitor_target}"
        lines = [f"{cmd.upper()} NOT READY", where]
        y = self.display._margin_y
        for line in lines:
            surf = self.display._font.render(line, True, RED)
            canvas.blit(surf, ((FRAME_W - surf.get_width()) // 2, y))
            y += self.display._char_h
        self.fb.write_surface(canvas)
        time.sleep(1.5)
        self.render()

    def _activate_menu_selection(self):
        """Enter on the menu page -- IDENTIFY PUPPETS is always a
        fleet-wide broadcast regardless of monitor_target, but a real
        app row acts on whichever machine is currently selected:
        launches locally for LOCAL, assigns remotely for a puppet.
        Checks readiness first using the same data the row's own
        HARDWARE NOT FOUND label came from -- avoids a pointless network
        round-trip for the common case (a puppet doesn't have the app
        installed) and gives instant feedback; launch_app()/
        assign_to_puppet() each still have their own backstop for the
        cases this cached check could be stale for."""
        if self.selected == len(APPS):
            self.identify_puppets()
            return
        cmd = APPS[self.selected][3]
        if not self._current_hardware_status().get(cmd, True):
            self._show_not_ready_message(cmd)
            return
        if self.monitor_target == "LOCAL":
            self.launch_app(cmd)
        else:
            self.assign_to_puppet(self.monitor_target, cmd)

    def handle_keycode(self, code):
        """Returns True if the app should redraw after this key."""
        if self.power_dialog_active:
            return self.handle_power_dialog_keycode(code)
        if code in (ecodes.KEY_Q, ecodes.KEY_ESC):
            self._quit_requested = True
        elif code == ecodes.KEY_POWER:
            self.power_dialog_active = True
            self.power_dialog_selection = 0
        elif self.current_page == MENU_PAGE_INDEX:
            return self._handle_menu_page_keycode(code)
        else:
            return self._handle_gauge_page_keycode(code)
        return True

    def _handle_gauge_page_keycode(self, code):
        """Pages 0/1 (CPU/MEM, WIFI/NET) -- Up/Down switches which
        machine's stats are shown (LOCAL/P1-P4); pick the puppet here
        before paging over to the menu to act on it."""
        if code == ecodes.KEY_COMPOSE:  # hamburger/Menu button -- jump straight to the menu page
            self.current_page = MENU_PAGE_INDEX
        elif code in (ecodes.KEY_HOMEPAGE, ecodes.KEY_HOME, ecodes.KEY_BACK):
            self.current_page = 0  # Home always means "page 0" specifically now
        elif code == ecodes.KEY_LEFT:
            self.current_page = (self.current_page - 1) % PAGE_COUNT
        elif code == ecodes.KEY_RIGHT:
            self.current_page = (self.current_page + 1) % PAGE_COUNT
        elif code == ecodes.KEY_UP:
            idx = MONITOR_TARGETS.index(self.monitor_target)
            self.monitor_target = MONITOR_TARGETS[(idx - 1) % len(MONITOR_TARGETS)]
        elif code == ecodes.KEY_DOWN:
            idx = MONITOR_TARGETS.index(self.monitor_target)
            self.monitor_target = MONITOR_TARGETS[(idx + 1) % len(MONITOR_TARGETS)]
        else:
            return False
        return True

    def _handle_menu_page_keycode(self, code):
        """Page MENU_PAGE_INDEX -- Up/Down here means moving the
        selection cursor instead of switching puppets (that's done from
        pages 0/1 before paging over here); Enter activates the
        highlighted row against monitor_target (see
        _activate_menu_selection)."""
        if code in (ecodes.KEY_HOMEPAGE, ecodes.KEY_HOME, ecodes.KEY_BACK):
            self.current_page = 0
        elif code == ecodes.KEY_COMPOSE:
            pass  # already at the menu page -- no-op
        elif code == ecodes.KEY_LEFT:
            self.current_page = (self.current_page - 1) % PAGE_COUNT
        elif code == ecodes.KEY_RIGHT:
            self.current_page = (self.current_page + 1) % PAGE_COUNT
        elif code == ecodes.KEY_UP:
            self.selected = (self.selected - 1) % MENU_ITEM_COUNT
        elif code == ecodes.KEY_DOWN:
            self.selected = (self.selected + 1) % MENU_ITEM_COUNT
        elif code in (ecodes.KEY_ENTER, ecodes.KEY_KPENTER, ecodes.BTN_LEFT, ecodes.BTN_MOUSE):
            self._activate_menu_selection()
            return False  # already rendered
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
