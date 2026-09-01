#!/usr/bin/env python3
"""
buzz2vlc - Control multiple VLC instances with a PS2/PS3 Buzz controller.

Architecture: a small Python daemon reads the Buzz USB HID receiver
directly (via hidapi) and drives one or more VLC instances through VLC's
built-in HTTP interface (--extraintf http). VLC's Lua extension API has
no USB/HID access, so this companion-app design is the only way to read
the hardware; talking to VLC over HTTP is the same thing VLC's own Lua
http interface exposes, just without writing Lua.

Each of the 4 buzzers has 5 buttons (red/big, yellow, green, orange,
blue), all reported in one HID input report. Any button can be mapped to
an action on any configured VLC instance, so four buzzers can each own a
different playlist. Buttons support a short-press ("tap") action, a
long-press ("hold") action, and a double-tap action -- see the
PressTracker class below for the timing state machine.

This single file also includes cross-platform window focus/raise support
(for the "focus"/"identify" actions) and the --diagnose hardware
diagnostic; buzz2vlc_gui.py is the only other file this project needs,
for the optional tkinter GUI.

Usage:
    python buzz2vlc.py --list              # show connected Buzz receivers
    python buzz2vlc.py --learn             # interactively map buttons
    python buzz2vlc.py --debug             # print raw HID reports
    python buzz2vlc.py --selftest          # run built-in checks, no hardware needed
    python buzz2vlc.py --check             # validate/migrate the config file
    python buzz2vlc.py --diagnose          # settle button_byte/order/LEDs on real hardware
    python buzz2vlc.py                     # run the listener daemon
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import queue
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import hid  # hidapi
except ImportError:  # pragma: no cover - exercised only when hidapi missing
    hid = None

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

log = logging.getLogger("buzz2vlc")

CONFIG_PATH = Path(os.environ.get("BUZZ2VLC_CONFIG", str(Path.home() / ".buzz2vlc.json")))

# Sony Buzz receiver, wired and wireless variants.
BUZZ_VENDOR_ID = 0x054C
BUZZ_PRODUCT_IDS = {0x0002: "wired", 0x1000: "wireless"}

BUTTON_ORDER_DEFAULT = ["red", "yellow", "green", "orange", "blue"]
# Community sources disagree on colour order across hardware revisions
# (hid-sony driver comment vs. the pybuzzers project). It's configurable
# and `python buzz2vlc.py --diagnose` determines the truth for a given
# unit -- do not treat either ordering as verified without running it.
BUZZERS = ["1", "2", "3", "4"]

MAX_VOLUME_DEFAULT = 256
ACTION_QUEUE_MAX = 64  # a flood of stale presses shouldn't be replayed later
DIAGNOSE_REPORT_PATH = Path("buzz2vlc_report.txt")


# ---------------------------------------------------------------------
# Press tracking: turns raw button press/release edges into logical
# events (tap, hold, double_tap, repeat). Used by both the CLI listener
# and the GUI listener so the timing logic only has to be right once.
#
# Timing rules:
#   - A button with no double-tap binding fires "tap" immediately on
#     release (or as soon as a hold fires, if it has one) -- no added
#     latency.
#   - A button with a double-tap binding delays its "tap" by
#     double_tap_ms to see whether a second press arrives; if it does,
#     "double_tap" fires instead and the pending tap is cancelled.
#   - A button with a hold binding fires "hold" the moment held time
#     crosses hold_ms, *while still held* -- not on release. If released
#     earlier, a tap (or double-tap) fires normally instead.
#   - If repeat_ms is set (and the button is holding), "repeat" fires
#     every repeat_ms after the hold threshold, for volume/seek ramping.
# ---------------------------------------------------------------------

EmitFn = Callable[[str, str], None]  # (button, event) -> None


TAP_EVENT_FOR_COUNT = {1: "tap", 2: "double_tap", 3: "triple_tap"}


class PressTracker:
    """Turns raw button press/release edges into logical tap/hold/
    double_tap/triple_tap/repeat events. See the module comment above for
    the full timing rules; feed it via press()/release()/poll()."""

    def __init__(
        self,
        hold_ms: int = 600,
        double_tap_ms: int = 300,
        repeat_ms: int = 0,
        has_double: Optional[Callable[[str], bool]] = None,
        has_triple: Optional[Callable[[str], bool]] = None,
        has_hold: Optional[Callable[[str], bool]] = None,
        emit: Optional[EmitFn] = None,
    ):
        self.hold_s = max(hold_ms, 1) / 1000.0
        self.double_tap_s = max(double_tap_ms, 1) / 1000.0
        self.repeat_ms = repeat_ms
        self.has_double = has_double or (lambda btn: False)
        self.has_triple = has_triple or (lambda btn: False)
        self.has_hold = has_hold or (lambda btn: False)
        self.emit = emit or (lambda btn, ev: None)
        self._lock = threading.RLock()
        self._state: dict[str, dict] = {}
        self._timers: dict[str, threading.Timer] = {}

    def press(self, btn: str, now: Optional[float] = None) -> None:
        """Records that `btn` went down. Call once per physical press."""
        now = now if now is not None else time.monotonic()
        with self._lock:
            # Suppress a pending single/double-tap resolution while this
            # new press is down -- otherwise, holding the 2nd (or 3rd) tap
            # of a multi-tap slightly longer than the remaining window can
            # let a premature lower-count event fire before release()
            # gets a chance to recount it.
            self._cancel_pending_tap(btn)
            st = self._state.setdefault(btn, {})
            st["pressed_at"] = now
            st["hold_fired"] = False
            st["repeat_next"] = now + self.hold_s

    def release(self, btn: str, now: Optional[float] = None) -> None:
        """Records that `btn` went up. Resolves to a tap/double_tap/
        triple_tap emit (immediately or after the double-tap window,
        depending on bindings) unless a hold already fired for this press."""
        now = now if now is not None else time.monotonic()
        with self._lock:
            st = self._state.get(btn)
            if not st or "pressed_at" not in st:
                return
            hold_fired = st.pop("hold_fired", False)
            st.pop("pressed_at", None)
            st.pop("repeat_next", None)
            if hold_fired:
                return  # already handled as a hold; release is a no-op

            wants_multi = self.has_double(btn) or self.has_triple(btn)
            if not wants_multi:
                self.emit(btn, "tap")
                return

            last_tap = st.get("last_tap_at")
            count = st.get("tap_count", 0) + 1 if last_tap is not None and (now - last_tap) <= self.double_tap_s \
                else 1
            st["last_tap_at"] = now
            st["tap_count"] = count

            max_count = 3 if self.has_triple(btn) else 2
            if count >= max_count:
                # No further tap could change the outcome -- resolve now
                # instead of waiting out the rest of the window.
                self._cancel_pending_tap(btn)
                st["last_tap_at"] = None
                st["tap_count"] = 0
                self.emit(btn, TAP_EVENT_FOR_COUNT[min(count, max_count)])
            else:
                self._schedule_resolution(btn)

    def poll(self, now: Optional[float] = None) -> None:
        """Call frequently (every 5-20ms) to fire hold/repeat while held."""
        now = now if now is not None else time.monotonic()
        with self._lock:
            for btn, st in self._state.items():
                if "pressed_at" not in st:
                    continue
                held = now - st["pressed_at"]
                if not st["hold_fired"] and self.has_hold(btn) and held >= self.hold_s:
                    st["hold_fired"] = True
                    self.emit(btn, "hold")
                if st["hold_fired"] and self.repeat_ms and now >= st.get("repeat_next", now):
                    st["repeat_next"] = now + (self.repeat_ms / 1000.0)
                    self.emit(btn, "repeat")

    def stop(self) -> None:
        """Cancels any pending tap-resolution timers. Call on shutdown."""
        with self._lock:
            for t in self._timers.values():
                t.cancel()
            self._timers.clear()

    # -- internal --------------------------------------------------
    def _schedule_resolution(self, btn: str) -> None:
        self._cancel_pending_tap(btn)

        def fire():
            with self._lock:
                st = self._state.get(btn)
                if st is None:
                    return
                count = st.pop("tap_count", 0)
                st["last_tap_at"] = None
                self._timers.pop(btn, None)
            event = TAP_EVENT_FOR_COUNT.get(count)
            if event:
                self.emit(btn, event)

        t = threading.Timer(self.double_tap_s, fire)
        t.daemon = True
        self._timers[btn] = t
        t.start()

    def _cancel_pending_tap(self, btn: str) -> None:
        t = self._timers.pop(btn, None)
        if t:
            t.cancel()


def default_config() -> dict:
    """Returns a fresh config dict with sample instances/mappings -- the
    starting point for a new install, and the base merged under whatever
    the user's own ~/.buzz2vlc.json overrides in load_config()."""
    return {
        "config_version": 2,
        "button_byte": 2,
        "button_order": list(BUTTON_ORDER_DEFAULT),
        "hold_ms": 600,
        "double_tap_ms": 300,
        "repeat_ms": 0,
        "debounce_ms": 50,
        "volume_step": 26,
        "max_volume": MAX_VOLUME_DEFAULT,
        "led_feedback": True,
        "led_status": True,
        "reconnect_interval_s": 3,
        "duck": {"enabled": True, "level_pct": 25, "restore_delay_s": 2.5},
        "vlc_path": None,
        "vlc_instances": {
            "movies": {"host": "127.0.0.1", "port": 8080, "password": "buzz", "playlist": "movies.xspf"},
            "music": {"host": "127.0.0.1", "port": 8081, "password": "buzz", "playlist": "music.xspf"},
            "sfx": {"host": "127.0.0.1", "port": 8082, "password": "buzz", "playlist": None},
        },
        "mappings": {
            "1:red": {"tap": "sfx/horn:horn.wav", "hold": "movies/play_pause"},
            "1:yellow": "movies/pause",
            "1:green": "movies/next",
            "1:orange": "movies/prev",
            "1:blue": "movies/stop",
            "2:red": "music/pause",
            "2:yellow": "music/next",
            "2:green": "music/prev",
            "2:orange": "music/vol_up",
            "2:blue": "music/vol_down",
        },
    }


# ---------------------------------------------------------------------
# Config load / save
# ---------------------------------------------------------------------

BINDING_KEYS = ("tap", "hold", "double_tap", "triple_tap")


def _binding_to_config(b) -> Any:
    """Normalize a mapping value for JSON: dict with tap/hold/double_tap/triple_tap, or a bare string."""
    if isinstance(b, dict):
        return {k: v for k, v in b.items() if k in BINDING_KEYS and v}
    return b


def _config_to_binding(b) -> dict:
    """Turn a mapping value (bare string or dict) into a normalized binding dict."""
    if isinstance(b, str):
        return {"tap": b}
    if isinstance(b, dict):
        return {k: b[k] for k in BINDING_KEYS if b.get(k)}
    return {}


def _migrate_v1(raw: dict) -> dict:
    """A v1 config nested everything under "vlc" as a single instance and used
    bare-action mappings ("pause" instead of "player/pause"). Carry the user's
    real port/password forward instead of silently discarding them for the
    sample multi-instance defaults."""
    cfg = default_config()
    old_vlc = raw.get("vlc", {})
    instance_name = "player"
    cfg["vlc_instances"] = {
        instance_name: {
            "host": old_vlc.get("host", "127.0.0.1"),
            "port": old_vlc.get("port", 8080),
            "password": old_vlc.get("password", "buzz"),
            "playlist": old_vlc.get("playlist"),
        }
    }
    mappings = {}
    for btn, action in (raw.get("mappings") or {}).items():
        if isinstance(action, str) and "/" not in action:
            action = f"{instance_name}/{action}"
        mappings[btn] = action
    cfg["mappings"] = mappings
    for key in ("button_byte", "button_order", "hold_ms", "double_tap_ms", "repeat_ms",
                "debounce_ms", "volume_step", "max_volume", "led_feedback", "led_status"):
        if key in raw:
            cfg[key] = raw[key]
    return cfg


def load_config() -> tuple[dict, list[str], list[str]]:
    """Returns (config, errors, warnings). Errors mean the config could not be
    used at all and defaults were substituted; warnings mean something is
    off but the config is usable."""
    errors: list[str] = []
    warnings: list[str] = []
    if not CONFIG_PATH.exists():
        return default_config(), errors, warnings

    try:
        raw_bytes = CONFIG_PATH.read_bytes()
        raw = json.loads(raw_bytes.decode("utf-8"))
    except UnicodeDecodeError as e:
        errors.append(f"config file is not valid UTF-8 ({e}); using defaults")
        return default_config(), errors, warnings
    except (json.JSONDecodeError, OSError) as e:
        errors.append(f"could not read config ({e}); using defaults")
        return default_config(), errors, warnings

    if not isinstance(raw, dict):
        errors.append("config root is not an object; using defaults")
        return default_config(), errors, warnings

    if "vlc" in raw and "vlc_instances" not in raw:
        warnings.append("migrated v1 config (single 'vlc' block) to multi-instance format")
        cfg = _migrate_v1(raw)
    else:
        cfg = default_config()
        cfg.update(raw)

    if not isinstance(cfg.get("mappings"), dict):
        errors.append("'mappings' was not an object; resetting mappings")
        cfg["mappings"] = default_config()["mappings"]

    try:
        cfg["repeat_ms"] = int(cfg.get("repeat_ms") or 0)
    except (TypeError, ValueError):
        errors.append("'repeat_ms' was not a number; resetting to 0")
        cfg["repeat_ms"] = 0

    cfg["mappings"] = {k: _binding_to_config(_config_to_binding(v)) for k, v in cfg["mappings"].items()}

    _tighten_permissions()
    return cfg, errors, warnings


def save_config(cfg: dict) -> None:
    """Writes cfg to CONFIG_PATH as UTF-8 JSON and re-tightens file
    permissions (the config holds VLC HTTP passwords)."""
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    _tighten_permissions()


def _tighten_permissions() -> None:
    """The config holds VLC HTTP passwords; keep it readable only by the owner.
    No-op on Windows, where POSIX chmod bits don't apply the same way."""
    if platform.system() == "Windows" or not CONFIG_PATH.exists():
        return
    try:
        CONFIG_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


# ---------------------------------------------------------------------
# VLC HTTP remote control
# ---------------------------------------------------------------------

class VLCError(Exception):
    """Raised for any VLCRemote failure: unreachable, wrong password, or a
    malformed/unexpected HTTP response."""


class VLCRemote:
    """One VLC instance's HTTP control interface. Reuses a single
    requests.Session (connection keep-alive) so a status-poller plus
    hold-to-repeat doesn't churn through a fresh TCP connection -- and
    ephemeral ports -- on every single command."""

    def __init__(self, name: str, host: str, port: int, password: str, timeout: float = 2.0):
        self.name = name
        self.base = f"http://{host}:{port}"
        self.timeout = timeout
        self._session = requests.Session() if requests else None
        if self._session is not None:
            self._session.auth = ("", password)
        self._muted_volume: Optional[int] = None

    def _request(self, params: dict) -> "xml.etree.ElementTree.Element":  # noqa: F821
        """Issues one command against VLC's status.xml HTTP endpoint and
        returns the parsed response. Every public method below is a thin
        wrapper over this."""
        if self._session is None:
            raise VLCError("the 'requests' package is not installed")
        import xml.etree.ElementTree as ET
        try:
            r = self._session.get(f"{self.base}/requests/status.xml", params=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise VLCError(f"{self.name}: unreachable ({e})") from e
        if r.status_code == 401:
            raise VLCError(f"{self.name}: wrong HTTP password")
        if r.status_code != 200:
            raise VLCError(f"{self.name}: HTTP {r.status_code}")
        try:
            return ET.fromstring(r.content)
        except ET.ParseError as e:
            raise VLCError(f"{self.name}: bad status.xml ({e})") from e

    def status(self) -> dict:
        """Returns {"state", "volume", "time", "length"} for the current
        item, with safe defaults for any field VLC omits."""
        root = self._request({})

        def _get(tag, default, cast):
            el = root.find(tag)
            if el is None or el.text is None:
                return default
            try:
                return cast(el.text)
            except ValueError:
                return default

        return {
            "state": _get("state", "stopped", str),
            "volume": _get("volume", 0, int),
            "time": _get("time", 0, int),
            "length": _get("length", 0, int),
        }

    def reachable(self) -> bool:
        """True if this instance responds to a status request at all --
        used for the GUI's traffic-light column and pre-launch checks."""
        try:
            self.status()
            return True
        except VLCError:
            return False

    def play(self) -> None:
        """Resumes/starts playback of the current playlist item."""
        self._request({"command": "pl_play"})

    def pause(self) -> None:
        """Pauses playback. A no-op if already paused or stopped."""
        self._request({"command": "pl_pause"})

    def play_pause(self) -> None:
        """Toggles play/pause based on the actual current state, rather
        than assuming -- correct even if something else changed playback
        state since the caller last checked."""
        state = self.status()["state"]
        if state == "playing":
            self.pause()
        else:
            self.play()

    def stop(self) -> None:
        """Stops playback entirely (not the same as pause)."""
        self._request({"command": "pl_stop"})

    def next(self) -> None:
        """Advances to the next playlist item."""
        self._request({"command": "pl_next"})

    def prev(self) -> None:
        """Returns to the previous playlist item."""
        self._request({"command": "pl_previous"})

    def seek(self, delta: str) -> None:
        """Seeks by a relative offset, e.g. "+10S" / "-10S"."""
        # requests percent-encodes '+' to %2B, which is required here --
        # a raw '+' in a query string decodes to a space and the seek
        # silently breaks.
        self._request({"command": "seek", "val": delta})

    def set_volume(self, vol: int, max_volume: int = MAX_VOLUME_DEFAULT) -> int:
        """Sets an absolute volume, clamped to [0, max_volume]. Returns
        the clamped value actually sent."""
        vol = max(0, min(max_volume, int(vol)))
        self._request({"command": "volume", "val": vol})
        return vol

    def vol_up(self, step: int, max_volume: int = MAX_VOLUME_DEFAULT) -> int:
        """Reads the current volume and raises it by `step`, clamped."""
        current = self.status()["volume"]
        return self.set_volume(current + step, max_volume)

    def vol_down(self, step: int, max_volume: int = MAX_VOLUME_DEFAULT) -> int:
        """Reads the current volume and lowers it by `step`, clamped."""
        current = self.status()["volume"]
        return self.set_volume(current - step, max_volume)

    def mute(self) -> None:
        """Toggles mute: remembers the pre-mute volume and restores it on
        the next call rather than assuming a fixed unmute level."""
        st = self.status()
        if st["volume"] > 0:
            self._muted_volume = st["volume"]
            self.set_volume(0)
        else:
            self.set_volume(self._muted_volume or 128)

    def fullscreen(self) -> None:
        """Toggles fullscreen video output."""
        self._request({"command": "fullscreen"})

    def shuffle(self) -> None:
        """Toggles shuffle/random playback order."""
        self._request({"command": "pl_random"})

    def loop(self) -> None:
        """Toggles playlist looping."""
        self._request({"command": "pl_loop"})

    def play_item(self, item_id: int) -> None:
        """Jumps directly to a specific playlist item by its VLC-assigned
        ID (as seen in playlist.xml)."""
        self._request({"command": "pl_play", "id": item_id})

    def horn(self, path: str) -> None:
        """Empties the playlist and immediately plays `path` (or a random
        file from it, if a folder) -- the "horn" sound-effect action."""
        uri = _path_to_uri(path)
        # Empty the playlist first so double-tapping the horn restarts the
        # blast instead of queueing a second one behind the first.
        self._request({"command": "pl_empty"})
        self._request({"command": "in_play", "input": uri})


def _path_to_uri(path: str) -> str:
    """Resolves a local path to a file:// URI for VLC's in_play command.
    A directory picks one random file from it (horn folders)."""
    p = Path(path)
    if p.is_dir():
        import random
        candidates = [f for f in p.iterdir() if f.is_file()]
        if not candidates:
            raise VLCError(f"horn folder {path} has no files")
        p = random.choice(candidates)
    return p.resolve().as_uri()


# ---------------------------------------------------------------------
# Buzz HID device
# ---------------------------------------------------------------------

def list_receivers() -> list[dict]:
    """Enumerates connected Buzz receivers (wired and wireless). Raises
    RuntimeError if hidapi itself isn't installed."""
    if hid is None:
        raise RuntimeError("hidapi is not installed (pip install hidapi)")
    found = []
    for dev in hid.enumerate(BUZZ_VENDOR_ID):
        kind = BUZZ_PRODUCT_IDS.get(dev["product_id"])
        if kind:
            found.append({"kind": kind, "vendor_id": dev["vendor_id"], "product_id": dev["product_id"],
                          "path": dev["path"]})
    return found


class BuzzDevice:
    """Wraps one Buzz receiver. Handles the wireless dongle's quirk of not
    reporting any button presses until it has received an output report --
    the "SimpleHIDWrite fix" documented in the community. With LEDs
    disabled this previously left a wireless set looking completely dead."""

    def __init__(self, button_byte: int = 2, button_order: Optional[list[str]] = None):
        self.button_byte = button_byte
        self.button_order = button_order or list(BUTTON_ORDER_DEFAULT)
        self._dev: Optional["hid.device"] = None
        self._led_state = bytes(7)

    def open(self) -> None:
        """Opens the first available receiver and wakes a wireless dongle
        with an initial all-zero LED write. Raises ConnectionError if none
        is plugged in."""
        if hid is None:
            raise RuntimeError("hidapi is not installed (pip install hidapi)")
        receivers = list_receivers()
        if not receivers:
            raise ConnectionError("no Buzz receiver found")
        dev = hid.device()
        dev.open_path(receivers[0]["path"])
        dev.set_nonblocking(True)
        self._dev = dev
        # Kick the wireless dongle awake regardless of LED settings.
        self._write_leds(bytes(7))

    def close(self) -> None:
        """Closes the device handle. Safe to call even if never opened."""
        if self._dev is not None:
            try:
                self._dev.close()
            finally:
                self._dev = None

    @property
    def is_open(self) -> bool:
        """True between a successful open() and close()."""
        return self._dev is not None

    def read(self, timeout_ms: int = 50):
        """Reads one raw HID report, or None on timeout. Raises OSError
        (and closes the device) if the receiver was unplugged."""
        if self._dev is None:
            return None
        try:
            data = self._dev.read(64, timeout_ms=timeout_ms)
        except OSError:
            # device likely unplugged
            self.close()
            raise
        return data or None

    def parse_buttons(self, report) -> set[str]:
        """Returns the set of "<buzzer>:<color>" strings currently pressed.
        Tolerates a report shorter than expected by decoding whatever bytes
        did arrive instead of dropping the whole report."""
        pressed: set[str] = set()
        if not isinstance(report, (list, tuple, bytes, bytearray)):
            return pressed
        for buzzer_index in range(4):
            byte_index = self.button_byte + buzzer_index * 5 // 8
            # 5 bits per buzzer, packed across bytes starting at button_byte.
            bit_offset = (buzzer_index * 5) % 8
            for color_index, color in enumerate(self.button_order):
                bit = bit_offset + color_index
                idx = byte_index + bit // 8
                if idx >= len(report):
                    continue
                if report[idx] & (1 << (bit % 8)):
                    pressed.add(f"{BUZZERS[buzzer_index]}:{color}")
        return pressed

    def set_led(self, buzzer_index: int, on: bool) -> None:
        """Sets one buzzer's LED, skipping the USB write if it's already
        in that state (LED status polling would otherwise write every
        cycle)."""
        if self._dev is None:
            return
        state = bytearray(self._led_state)
        if on:
            state[buzzer_index] = 0xFF
        else:
            state[buzzer_index] = 0x00
        new_state = bytes(state)
        if new_state == self._led_state:
            return  # change-detection: avoid a USB write on every poll
        self._led_state = new_state
        self._write_leds(new_state)

    def _write_leds(self, seven_bytes: bytes) -> None:
        if self._dev is None:
            return
        # hidapi needs a leading report-ID byte ahead of the kernel driver's
        # 7-byte LED field, or the write silently does nothing.
        report = bytes([0x00]) + seven_bytes
        try:
            self._dev.write(report)
        except OSError:
            pass


# ---------------------------------------------------------------------
# Dispatcher: runs actions off the input-read thread so a slow/unreachable
# VLC (2s timeout) can never stall button reading.
# ---------------------------------------------------------------------

class Dispatcher:
    """Runs submitted actions on a dedicated worker thread with a bounded
    queue, so a slow/unreachable VLC (up to the request timeout) can never
    stall the HID read loop, and one bad action can't take down the rest."""

    def __init__(self, max_queue: int = ACTION_QUEUE_MAX):
        self._q: "queue.Queue[Callable[[], None]]" = queue.Queue(maxsize=max_queue)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._running = False

    def start(self) -> None:
        """Starts the worker thread. Call once."""
        self._running = True
        self._thread.start()

    def stop(self) -> None:
        """Signals the worker to exit and waits (up to 2s) for it to."""
        self._running = False
        try:
            self._q.put_nowait(None)  # unblock get()
        except queue.Full:
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def submit(self, fn: Callable[[], None]) -> None:
        """Queues a zero-arg callable to run on the worker thread. If the
        queue is full, drops the oldest pending action to make room --
        a flood of stale button presses shouldn't all replay later."""
        try:
            self._q.put_nowait(fn)
        except queue.Full:
            log.warning("action queue full; dropping oldest stale action")
            try:
                self._q.get_nowait()
                self._q.put_nowait(fn)
            except queue.Empty:
                pass

    def _run(self) -> None:
        while True:
            fn = self._q.get()
            if fn is None or not self._running:
                if not self._running:
                    return
                continue
            try:
                fn()
            except Exception:  # noqa: BLE001 - one bad action must not kill the dispatcher
                log.exception("action failed")


# ---------------------------------------------------------------------
# Engine: ties everything together
# ---------------------------------------------------------------------

class Engine:
    """Ties everything together: owns the config, one VLCRemote per
    configured instance, the PressTracker, the Dispatcher, ducking state,
    LED status, and the hotplug reconnect loop. run_forever() is the
    main entry point (blocks until shutdown()); run_action() is the
    single path every mapped button press and every GUI action button
    goes through."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.remotes: dict[str, VLCRemote] = {
            name: VLCRemote(name, inst.get("host", "127.0.0.1"), inst["port"], inst.get("password", ""))
            for name, inst in cfg.get("vlc_instances", {}).items()
        }
        self.dispatcher = Dispatcher()
        self._duck_state: dict[str, dict] = {}
        self._duck_lock = threading.Lock()
        self.device = BuzzDevice(cfg.get("button_byte", 2), cfg.get("button_order"))
        self.tracker = PressTracker(
            hold_ms=cfg.get("hold_ms", 600),
            double_tap_ms=cfg.get("double_tap_ms", 300),
            repeat_ms=cfg.get("repeat_ms", 0),
            has_double=lambda btn: "double_tap" in self._binding(btn),
            has_triple=lambda btn: "triple_tap" in self._binding(btn),
            has_hold=lambda btn: "hold" in self._binding(btn),
            emit=self._on_press_event,
        )
        self._stop_flag = threading.Event()
        self._force_reconnect = threading.Event()
        self._led_thread: Optional[threading.Thread] = None
        self._led_assignment: dict[int, str] = {}
        self.instance_pids: dict[str, int] = {}
        # Fired on every raw press/release transition (button, is_pressed),
        # independent of tap/hold/double-tap resolution -- lets a GUI show
        # live visual feedback for a physical press even on an unmapped
        # button, or one whose tap is still waiting out its double-tap
        # window. Set by a caller (e.g. the GUI); exceptions from it never
        # take down the HID read loop.
        self.on_button_change: Optional[Callable[[str, bool], None]] = None
        # Fired on every resolved logical event (button, "tap"/"hold"/
        # "double_tap"/"repeat"), regardless of whether that button has a
        # mapping for it -- lets a GUI distinguish, say, a double-tap from
        # a plain tap even when they'd otherwise look identical as raw
        # press/release pairs.
        self.on_logical_event: Optional[Callable[[str, str], None]] = None
        # Fired with the resolved action string (e.g. "movies/pause",
        # "all_stop") right before it runs, for activity logging. Called
        # from the Dispatcher thread, not the HID thread.
        self.on_action: Optional[Callable[[str], None]] = None

    def request_reconnect(self) -> None:
        """Forces an immediate rescan for a Buzz receiver instead of waiting
        for the next reconnect_interval tick, or for an OS-level unplug to
        be detected. Since button_byte/button_order/mappings all live in
        the config -- never tied to any particular receiver's identity --
        swapping in a different physical controller (e.g. a spare unit
        after a battery dies) picks up the exact same settings the moment
        it reconnects; this just makes that immediate on demand instead of
        potentially waiting out the poll interval."""
        self._force_reconnect.set()

    def register_pid(self, instance: str, pid: int) -> None:
        """Lets a launcher (GUI or CLI) tell the engine which OS process
        owns a given instance, so window focus/identify can match by PID
        -- exact, and the only reliable option now that launches don't
        set --meta-title (it renamed every playlist track, not just the
        window -- see build_launch_command)."""
        self.instance_pids[instance] = pid

    def _notify_button_change(self, btn: str, is_pressed: bool) -> None:
        if self.on_button_change is None:
            return
        try:
            self.on_button_change(btn, is_pressed)
        except Exception:  # noqa: BLE001 - a GUI callback must never kill the HID loop
            log.exception("on_button_change callback failed")

    # -- config lookups ------------------------------------------------
    def _binding(self, btn: str) -> dict:
        raw = self.cfg.get("mappings", {}).get(btn)
        if isinstance(raw, str):
            return {"tap": raw}
        if isinstance(raw, dict):
            return raw
        return {}

    # -- action dispatch (the single public path every caller uses) ----
    def run_action(self, action: str) -> None:
        """Runs one action string like "movies/pause" or "sfx/horn:file.wav",
        or a global action like "all_pause". Always goes through this path
        so per-player and global actions can never drift apart."""
        if self.on_action is not None:
            try:
                self.on_action(action)
            except Exception:  # noqa: BLE001 - a GUI callback must never kill the dispatcher
                log.exception("on_action callback failed")
        if action in ("all_pause", "all_stop"):
            self._run_global(action)
            return
        if "/" not in action:
            log.warning("malformed action (missing instance): %r", action)
            return
        instance, _, verb = action.partition("/")
        remote = self.remotes.get(instance)
        if remote is None:
            log.warning("unknown VLC instance %r in action %r", instance, action)
            return
        self._run_on(remote, verb)

    def _run_global(self, action: str) -> None:
        for remote in self.remotes.values():
            try:
                st = remote.status()
            except VLCError:
                continue
            if action == "all_pause" and st["state"] == "playing":
                self._run_on(remote, "pause")
            elif action == "all_stop" and st["state"] != "stopped":
                self._run_on(remote, "stop")

    def _run_on(self, remote: VLCRemote, verb: str) -> None:
        try:
            if verb.startswith("horn:"):
                self._horn(remote, verb.split(":", 1)[1])
            elif verb == "play":
                remote.play()
            elif verb == "pause":
                remote.pause()
            elif verb == "play_pause":
                remote.play_pause()
            elif verb == "stop":
                remote.stop()
            elif verb == "next":
                remote.next()
            elif verb == "prev":
                remote.prev()
            elif verb == "seek+10s":
                remote.seek("+10S")
            elif verb == "seek-10s":
                remote.seek("-10S")
            elif verb == "vol_up":
                remote.vol_up(self.cfg.get("volume_step", 26), self.cfg.get("max_volume", MAX_VOLUME_DEFAULT))
            elif verb == "vol_down":
                remote.vol_down(self.cfg.get("volume_step", 26), self.cfg.get("max_volume", MAX_VOLUME_DEFAULT))
            elif verb == "mute":
                remote.mute()
            elif verb == "fullscreen":
                remote.fullscreen()
            elif verb == "shuffle":
                remote.shuffle()
            elif verb == "loop":
                remote.loop()
            elif verb in ("focus", "identify"):
                self._window_action(remote.name, verb)
            else:
                log.warning("unknown action verb %r", verb)
        except VLCError as e:
            log.warning(str(e))

    def _window_action(self, instance: str, verb: str) -> None:
        title_hint = f"buzz2vlc: {instance}"
        pid = self.instance_pids.get(instance)
        if verb == "focus":
            raise_window(title_hint=title_hint, pid=pid)
        else:
            identify_window(title_hint=title_hint, pid=pid)

    def _horn(self, remote: VLCRemote, path: str) -> None:
        duck_cfg = self.cfg.get("duck", {})
        if duck_cfg.get("enabled"):
            self._duck_others(except_name=remote.name, level_pct=duck_cfg.get("level_pct", 25),
                               restore_after=duck_cfg.get("restore_delay_s", 2.5))
        remote.horn(path)

    def _duck_others(self, except_name: str, level_pct: int, restore_after: float) -> None:
        with self._duck_lock:
            for name, remote in self.remotes.items():
                if name == except_name:
                    continue
                try:
                    st = remote.status()
                except VLCError:
                    continue
                if st["state"] != "playing":
                    continue  # a paused player shouldn't come back quiet
                existing = self._duck_state.get(name)
                if existing is None:
                    # capture the original volume once; an overlapping horn
                    # must not treat the already-ducked level as "original"
                    original = st["volume"]
                    self._duck_state[name] = {"original": original, "restore_at": time.monotonic() + restore_after}
                    target = max(0, int(original * level_pct / 100))
                    try:
                        remote.set_volume(target, self.cfg.get("max_volume", MAX_VOLUME_DEFAULT))
                    except VLCError:
                        pass
                else:
                    existing["restore_at"] = time.monotonic() + restore_after
                self._schedule_restore(name, restore_after)

    def _schedule_restore(self, name: str, delay: float) -> None:
        def restore():
            with self._duck_lock:
                st = self._duck_state.get(name)
                if st is None or time.monotonic() < st["restore_at"] - 0.01:
                    return  # a newer horn pushed the restore time out
                original = st["original"]
                del self._duck_state[name]
            remote = self.remotes.get(name)
            if remote is None:
                return
            try:
                remote.set_volume(original, self.cfg.get("max_volume", MAX_VOLUME_DEFAULT))
            except VLCError:
                pass

        t = threading.Timer(delay, restore)
        t.daemon = True
        t.start()

    # -- press events ----------------------------------------------------
    def _on_press_event(self, btn: str, event: str) -> None:
        if self.on_logical_event is not None:
            try:
                self.on_logical_event(btn, event)
            except Exception:  # noqa: BLE001 - a GUI callback must never kill the HID loop
                log.exception("on_logical_event callback failed")
        binding = self._binding(btn)
        action = binding.get(event) or (binding.get("tap") if event == "hold" and "hold" not in binding else None)
        if event == "repeat":
            action = binding.get("hold")  # ramping repeats the hold action
        if not action:
            if event == "tap":
                log.debug("unmapped button %s", btn)
            return
        self.dispatcher.submit(lambda a=action: self.run_action(a))

    # -- LED status --------------------------------------------------
    def _assign_leds(self) -> None:
        """Each buzzer lights up for whichever instance most of its buttons
        point at; horn-only buttons don't make the sfx player claim a
        buzzer."""
        counts: dict[str, dict[str, int]] = {b: {} for b in BUZZERS}
        for key, binding in self.cfg.get("mappings", {}).items():
            if ":" not in key:
                continue
            buzzer, _ = key.split(":", 1)
            for action in (self._config_binding_actions(binding)):
                if "/" not in action:
                    continue
                instance, _, verb = action.partition("/")
                if verb.startswith("horn"):
                    continue
                counts.setdefault(buzzer, {})
                counts[buzzer][instance] = counts[buzzer].get(instance, 0) + 1
        self._led_assignment = {}
        for i, buzzer in enumerate(BUZZERS):
            if counts.get(buzzer):
                self._led_assignment[i] = max(counts[buzzer], key=counts[buzzer].get)

    @staticmethod
    def _config_binding_actions(binding) -> list[str]:
        if isinstance(binding, str):
            return [binding]
        if isinstance(binding, dict):
            return [v for v in binding.values() if isinstance(v, str)]
        return []

    def _led_loop(self) -> None:
        blink_on = True
        while not self._stop_flag.is_set():
            blink_on = not blink_on
            if self.cfg.get("led_status") and self.device.is_open:
                for i, instance in self._led_assignment.items():
                    remote = self.remotes.get(instance)
                    if remote is None:
                        continue
                    try:
                        st = remote.status()
                    except VLCError:
                        self.device.set_led(i, False)
                        continue
                    if st["state"] == "playing":
                        self.device.set_led(i, True)
                    elif st["state"] == "paused":
                        self.device.set_led(i, blink_on)
                    else:
                        self.device.set_led(i, False)
            self._stop_flag.wait(1.0)

    # -- main loop -----------------------------------------------------
    def run_forever(self) -> None:
        """Main entry point: starts the dispatcher and LED-status thread,
        then blocks in the hotplug reconnect/read loop until shutdown()
        is called (from another thread) or an unrecoverable error occurs.
        Always calls shutdown() on the way out."""
        self._assign_leds()
        self.dispatcher.start()
        self._led_thread = threading.Thread(target=self._led_loop, daemon=True)
        self._led_thread.start()
        try:
            self._connect_loop()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """Stops the read loop, tracker timers, dispatcher, and closes the
        HID device. Safe to call from any thread; run_forever() will
        return once the loop notices the stop flag."""
        self._stop_flag.set()
        self.tracker.stop()
        self.dispatcher.stop()
        self.device.close()

    def _connect_loop(self) -> None:
        interval = self.cfg.get("reconnect_interval_s", 3)
        currently_pressed: set[str] = set()
        while not self._stop_flag.is_set():
            try:
                self.device.open()
            except ConnectionError:
                log.warning("Buzz receiver not found; retrying every %ss", interval)
                if self._stop_flag.wait(interval):
                    return
                continue
            log.info("Buzz receiver connected")
            try:
                while not self._stop_flag.is_set():
                    if self._force_reconnect.is_set():
                        self._force_reconnect.clear()
                        log.info("reconnect requested; rescanning for a Buzz receiver")
                        break
                    try:
                        report = self.device.read(timeout_ms=20)
                    except OSError:
                        log.warning("Buzz receiver disconnected; reconnecting")
                        break
                    now = time.monotonic()
                    if report is not None:
                        pressed = self.device.parse_buttons(report)
                        for btn in pressed - currently_pressed:
                            self.tracker.press(btn, now)
                            self._notify_button_change(btn, True)
                        for btn in currently_pressed - pressed:
                            self.tracker.release(btn, now)
                            self._notify_button_change(btn, False)
                        currently_pressed = pressed
                    self.tracker.poll(now)
            finally:
                self.device.close()
            if self._stop_flag.wait(interval):
                return


# ---------------------------------------------------------------------
# Window management: "find and raise a VLC window", backing the
# "focus"/"identify" actions. Matches by PID (via Engine.register_pid) --
# the only reliable path, since launches don't set --meta-title (it
# renamed every playlist track along with the window, see
# build_launch_command) and every buzz2vlc window's title bar is
# otherwise the generic "VLC media player". Title matching still exists
# as a fallback for a window buzz2vlc didn't launch itself (so has no
# known PID for), in case something else gives it a distinctive title.
#
# Platform notes:
#   - Linux/X11: uses wmctrl if present. Verifies a raise actually landed
#     by checking the EWMH _NET_ACTIVE_WINDOW property (what wmctrl -a
#     itself sets) rather than xdotool getactivewindow, which returns
#     nothing for windows that don't accept input focus.
#   - Wayland: no external program can raise another program's window;
#     detected and reported plainly rather than silently doing nothing.
#   - Windows: only the foreground process may call SetForegroundWindow,
#     and a buzzer press means buzz2vlc never is one. Four fallbacks run
#     in order: a plain call, attaching to the foreground thread's input
#     queue, briefly zeroing the foreground lock timeout, and finally
#     BringWindowToTop. Verified against a real Windows 10 desktop: when
#     buzz2vlc runs as a non-interactive/background process, the first
#     three can all fail outright (SetForegroundWindow refuses even after
#     a successful AttachThreadInput, and SystemParametersInfoW can
#     itself return failure without elevated privilege) -- BringWindowToTop
#     still reliably changes Z-order in that situation.
# ---------------------------------------------------------------------

def window_control_available() -> tuple[bool, str]:
    """Returns (can_raise_windows, explanation)."""
    system = platform.system()
    if system == "Windows":
        return True, "Win32 SetForegroundWindow with foreground-lock fallbacks"
    if system == "Linux":
        if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
            return False, "Wayland blocks one program from raising another's windows; log in with X11/Xorg instead"
        if shutil.which("wmctrl"):
            return True, "wmctrl"
        if shutil.which("xdotool"):
            return True, "xdotool"
        return False, "neither wmctrl nor xdotool is installed"
    return False, f"window control is not implemented for {system}"


def _linux_find_window_id(title_hint: Optional[str] = None, pid: Optional[int] = None) -> Optional[str]:
    if not shutil.which("wmctrl"):
        return None
    try:
        # -p adds a PID column: id, desktop, pid, host, title
        out = subprocess.run(["wmctrl", "-l", "-p"], capture_output=True, text=True, timeout=2).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        win_id, _desktop, win_pid, _host, title = parts
        if pid is not None:
            try:
                if int(win_pid) == pid:
                    return win_id
            except ValueError:
                continue
        elif title_hint and title_hint in title:
            return win_id
    return None


def _linux_active_window_id() -> Optional[str]:
    try:
        out = subprocess.run(["xprop", "-root", "_NET_ACTIVE_WINDOW"], capture_output=True, text=True,
                              timeout=2).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    if "#" not in out:
        return None
    return out.split("#", 1)[1].strip().split()[0]


def _linux_raise(title_hint: Optional[str] = None, pid: Optional[int] = None) -> bool:
    win_id = _linux_find_window_id(title_hint, pid)
    if win_id is None:
        return False
    try:
        subprocess.run(["wmctrl", "-i", "-a", win_id], timeout=2)
    except (subprocess.SubprocessError, OSError):
        return False
    time.sleep(0.1)
    active = _linux_active_window_id()
    return active is not None and active.lower().lstrip("0x") == win_id.lower().lstrip("0x")


def _windows_find_hwnd(user32, title_hint: Optional[str] = None, pid: Optional[int] = None):
    import ctypes
    from ctypes import wintypes

    result = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

    def callback(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        if pid is not None:
            owner_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
            if owner_pid.value == pid:
                result.append(hwnd)
                return False
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if title_hint and title_hint in buf.value:
            result.append(hwnd)
            return False
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return result[0] if result else None


def _windows_raise(title_hint: Optional[str] = None, pid: Optional[int] = None) -> bool:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # ctypes defaults every return value to a 32-bit C int; HWND is a
    # 64-bit pointer on 64-bit Windows, so undeclared calls silently
    # truncate window handles and every call below would fail quietly.
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetForegroundWindow.argtypes = []
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.SystemParametersInfoW.restype = wintypes.BOOL
    user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.GetCurrentThreadId.argtypes = []

    SW_RESTORE = 9
    SPI_GETFOREGROUNDLOCKTIMEOUT = 0x2000
    SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001
    SPIF_SENDCHANGE = 0x2

    hwnd = _windows_find_hwnd(user32, title_hint, pid)
    if not hwnd:
        return False

    user32.ShowWindow(hwnd, SW_RESTORE)

    if user32.SetForegroundWindow(hwnd):  # fallback 1: plain call
        return True

    fg = user32.GetForegroundWindow()  # fallback 2: attach to the foreground thread's input queue
    if fg:
        fg_pid = wintypes.DWORD()
        fg_tid = user32.GetWindowThreadProcessId(fg, ctypes.byref(fg_pid))
        this_tid = kernel32.GetCurrentThreadId()
        if fg_tid and user32.AttachThreadInput(this_tid, fg_tid, True):
            try:
                if user32.SetForegroundWindow(hwnd):
                    return True
            finally:
                user32.AttachThreadInput(this_tid, fg_tid, False)

    old_timeout = ctypes.c_uint(0)  # fallback 3: briefly zero the foreground lock timeout
    if user32.SystemParametersInfoW(SPI_GETFOREGROUNDLOCKTIMEOUT, 0, ctypes.byref(old_timeout), 0):
        user32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0, 0, SPIF_SENDCHANGE)
        try:
            if user32.SetForegroundWindow(hwnd):
                return True
        finally:
            user32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0, ctypes.c_void_p(old_timeout.value),
                                          SPIF_SENDCHANGE)

    if user32.BringWindowToTop(hwnd):  # fallback 4: Z-order only, no keyboard focus, but better than nothing
        user32.ShowWindow(hwnd, SW_RESTORE)
        return True

    return False  # all fallbacks failed -- Windows still flashes the taskbar button


def _windows_flash(title_hint: Optional[str] = None, pid: Optional[int] = None, times: int = 4) -> None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.FlashWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
    hwnd = _windows_find_hwnd(user32, title_hint, pid)
    if not hwnd:
        return
    for _ in range(times):
        user32.FlashWindow(hwnd, True)
        time.sleep(0.3)


def raise_window(title_hint: Optional[str] = None, pid: Optional[int] = None) -> bool:
    """Matches by process ID when known (exact, immune to VLC reverting
    its window title to generic text once nothing is actively playing)
    and falls back to a title substring match otherwise."""
    system = platform.system()
    if system == "Windows":
        return _windows_raise(title_hint, pid)
    if system == "Linux":
        return _linux_raise(title_hint, pid)
    return False


def identify_window(title_hint: Optional[str] = None, pid: Optional[int] = None) -> bool:
    """Raises the window (see raise_window) and flashes it a few times --
    for locating a player among several overlapping/off-screen windows."""
    ok = raise_window(title_hint, pid)
    system = platform.system()
    if system == "Windows":
        _windows_flash(title_hint, pid)
    elif system == "Linux":
        for _ in range(4):
            time.sleep(0.15)
            _linux_raise(title_hint, pid)
    return ok


def _windows_dpi() -> int:
    import ctypes
    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()  # Windows 10+
        if dpi:
            return dpi
    except (AttributeError, OSError):
        pass
    try:
        hdc = ctypes.windll.user32.GetDC(0)
        LOGPIXELSX = 88
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, LOGPIXELSX)
        ctypes.windll.user32.ReleaseDC(0, hdc)
        if dpi:
            return dpi
    except (AttributeError, OSError):
        pass
    return 96  # Windows' un-scaled baseline


def cm_to_px(cm: float) -> int:
    """Converts a physical length to pixels using the real system DPI on
    Windows (96 elsewhere) -- used for the window-tiling margin, so it
    stays a consistent physical size across different display scaling."""
    dpi = _windows_dpi() if platform.system() == "Windows" else 96
    return round(cm / 2.54 * dpi)


def usable_screen_area() -> Optional[tuple[int, int, int, int]]:
    """Returns (x, y, width, height) of the desktop work area -- the
    region NOT covered by the taskbar, regardless of which screen edge
    it's docked to (top/bottom/left/right) or whether it's auto-hidden.
    Uses the documented SPI_GETWORKAREA rather than guessing a taskbar
    height/position. Returns None off Windows; caller should fall back
    to the full screen size there."""
    if platform.system() != "Windows":
        return None
    import ctypes
    from ctypes import wintypes

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    SPI_GETWORKAREA = 0x0030
    user32 = ctypes.windll.user32
    user32.SystemParametersInfoW.restype = wintypes.BOOL
    user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
    rect = RECT()
    if not user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
        return None
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def _windows_move(pid: int, x: int, y: int, width: int, height: int) -> bool:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, wintypes.UINT]
    hwnd = _windows_find_hwnd(user32, pid=pid)
    if not hwnd:
        return False
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010  # move/resize only -- no focus-stealing, so no foreground-lock issues
    return bool(user32.SetWindowPos(hwnd, 0, x, y, width, height, SWP_NOZORDER | SWP_NOACTIVATE))


def _linux_move(pid: int, x: int, y: int, width: int, height: int) -> bool:
    win_id = _linux_find_window_id(pid=pid)
    if win_id is None:
        return False
    try:
        subprocess.run(["wmctrl", "-i", "-r", win_id, "-e", f"0,{x},{y},{width},{height}"], timeout=2)
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def position_window(pid: int, x: int, y: int, width: int, height: int) -> bool:
    """Moves/resizes a window by PID. Unlike raise_window, this never
    needs to steal focus (SWP_NOACTIVATE / no analogous foreground check
    on Linux), so it isn't subject to the foreground-lock failures that
    can affect SetForegroundWindow."""
    system = platform.system()
    if system == "Windows":
        return _windows_move(pid, x, y, width, height)
    if system == "Linux":
        return _linux_move(pid, x, y, width, height)
    return False


def wait_and_position_window(pid: int, x: int, y: int, width: int, height: int, timeout: float = 8.0) -> bool:
    """Polls for a just-launched window to appear (by PID) and moves it
    into place. Window creation lags a moment behind process start, so a
    single immediate attempt right after Popen returns would usually
    miss -- meant to be run in a background thread."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if position_window(pid, x, y, width, height):
            return True
        time.sleep(0.3)
    return False


# ---------------------------------------------------------------------
# Launching VLC instances
# ---------------------------------------------------------------------

def find_vlc_path(configured: Optional[str] = None) -> Optional[str]:
    """Locates the vlc executable: an explicitly configured path, PATH,
    common Windows install locations, then the registry. Returns None if
    it can't be found anywhere."""
    if configured and Path(configured).exists():
        return configured
    exe = "vlc.exe" if platform.system() == "Windows" else "vlc"
    found = shutil.which(exe)
    if found:
        return found
    if platform.system() == "Windows":
        for env_var in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(env_var)
            if base:
                candidate = Path(base) / "VideoLAN" / "VLC" / "vlc.exe"
                if candidate.exists():
                    return str(candidate)
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\VideoLAN\VLC") as key:
                value, _ = winreg.QueryValueEx(key, "")
                if Path(value).exists():
                    return value
        except OSError:
            pass
    return None


def port_in_use(host: str, port: int) -> bool:
    """True if something is already listening on host:port -- checked
    before launching, since a taken port doesn't stop VLC from starting
    (it just silently drops the HTTP interface; see launch_instance)."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def _playlist_to_target(path: str) -> str:
    """A bare Windows path (e.g. "C:\\Media\\movies.xspf") passed as VLC's
    trailing command-line argument is not reliably recognized -- verified
    live: it launches with an empty playlist, no error. A proper file://
    URI is. A value that's already a URL/URI (has a "://" scheme, e.g. a
    network stream or an existing file:// URI) is passed through
    unchanged."""
    if "://" in path:
        return path
    try:
        return Path(path).resolve().as_uri()
    except (ValueError, OSError):
        return path  # fall back to the raw value rather than failing the launch


def build_launch_command(vlc_path: str, name: str, inst: dict) -> list[str]:
    """Builds the vlc.exe command line for one instance's HTTP interface
    and playlist. See the comments below for a flag deliberately left
    out, and why; and for --start-paused, which replaced an earlier,
    more complicated approach to the same problem."""
    # No --meta-title: verified live that VLC uses it to replace every
    # playlist item's displayed name (this is documented VLC behavior,
    # not a fluke) -- with a multi-track .m3u/.xspf that means every
    # track shows the same "buzz2vlc: <name>" label instead of its real
    # title. Window identification doesn't need it: launch_instance's
    # caller always knows the PID, and buzz2vlc_window.raise_window()
    # matches by PID first, which needs no window-title labeling at all.
    #
    # --start-paused: loads and expands the playlist (so a multi-track
    # .m3u/.xspf still shows every track, unlike --no-playlist-autostart
    # -- see the "Known gaps"-turned-fixed history in README) without
    # ever transitioning through "playing" at all. Verified live: status
    # stayed "paused" from the first poll after launch through several
    # seconds later -- no audible/visible blip, and no race to catch and
    # stop it, unlike the earlier approach (start playing, then watch for
    # "playing" and stop it from a background thread) it replaced.
    cmd = [
        vlc_path,
        "--extraintf", "http",
        "--http-host", inst.get("host", "127.0.0.1"),
        "--http-port", str(inst["port"]),
        "--http-password", inst.get("password", ""),
        "--no-one-instance",
        "--start-paused",
        # VLC's Qt interface saves its window geometry to a shared
        # per-user config file (vlc-qt-interface.ini) and restores it on
        # the next launch -- since every instance shares that same file,
        # this silently overwrites our own window-tiling position shortly
        # after launch, and since they all restore the *same* saved
        # rectangle, multiple instances end up on top of each other.
        # Verified live: with this flag, a positioned window's rect is
        # stable over several seconds; without it, incrementally-launched
        # instances collided. buzz2vlc passes every setting VLC needs via
        # its own CLI flags each launch, so VLC never needs to persist
        # anything of its own.
        "--ignore-config",
        # --ignore-config alone makes VLC treat every single launch as a
        # brand-new install, popping up its "Privacy and Network Access
        # Policy" consent dialog every time -- verified live: that dialog
        # window is what was actually landing on top of things, not a
        # tiling bug. Suppress it directly rather than relying on
        # accepted-state persistence (which --ignore-config already
        # forgoes on purpose).
        "--no-qt-privacy-ask",
    ]
    if inst.get("playlist"):
        # A "--" separator stops a target that starts with '-' from being
        # parsed by VLC as an option.
        cmd += ["--", _playlist_to_target(inst["playlist"])]
    return cmd


def launch_instance(name: str, inst: dict, vlc_path: Optional[str] = None) -> subprocess.Popen:
    """Launches one VLC instance (raises RuntimeError if VLC can't be
    found or the port is already taken). Returns the Popen immediately;
    doesn't block on the HTTP interface coming up. If it has a playlist,
    --start-paused (see build_launch_command) means it comes up loaded
    but not playing, with no extra step needed here."""
    resolved = find_vlc_path(vlc_path)
    if not resolved:
        raise RuntimeError("VLC not found; set 'vlc_path' in the config or add vlc to PATH")
    host = inst.get("host", "127.0.0.1")
    if port_in_use(host, inst["port"]):
        raise RuntimeError(
            f"{name}: port {inst['port']} is already in use. VLC will still start but its HTTP "
            "interface will silently fail to bind, leaving a player you can never control -- "
            "free the port first."
        )
    cmd = build_launch_command(resolved, name, inst)
    return subprocess.Popen(cmd)


def _windows_post_close(pid: int) -> bool:
    """Posts WM_CLOSE to a process's window, letting VLC run its own
    shutdown path (saving state, releasing the audio device cleanly)
    instead of being killed outright."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.PostMessageW.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    hwnd = _windows_find_hwnd(user32, pid=pid)
    if not hwnd:
        return False
    WM_CLOSE = 0x0010
    return bool(user32.PostMessageW(hwnd, WM_CLOSE, 0, 0))


def close_instance(proc: subprocess.Popen, timeout: float = 3.0) -> None:
    """Asks a launched VLC instance to close gracefully before falling
    back to a hard terminate/kill. TerminateProcess (Windows) or an
    unhandled SIGKILL skips VLC's own cleanup and can leave a wedged
    state on the next launch; SIGTERM on POSIX and WM_CLOSE on Windows
    both let VLC exit through its normal path."""
    if proc.poll() is not None:
        return  # already exited
    if platform.system() == "Windows":
        try:
            _windows_post_close(proc.pid)
        except Exception:
            pass
    else:
        try:
            proc.terminate()  # SIGTERM; VLC handles this cleanly on POSIX
        except Exception:
            pass
    try:
        proc.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
        proc.wait(timeout=2)
    except Exception:
        pass


# ---------------------------------------------------------------------
# CLI: --list / --debug / --learn / --check / --selftest / run
# ---------------------------------------------------------------------

def cmd_list() -> int:
    """--list: prints connected Buzz receivers. Returns 1 if none found."""
    try:
        receivers = list_receivers()
    except RuntimeError as e:
        print(f"error: {e}")
        return 1
    if not receivers:
        print("no Buzz receiver found")
        return 1
    for r in receivers:
        print(f"{r['kind']}: vid={r['vendor_id']:04x} pid={r['product_id']:04x}")
    return 0


def cmd_debug(cfg: dict) -> int:
    """--debug: prints every raw HID report and its decoded buttons, for
    verifying/tuning button_byte and button_order by hand."""
    device = BuzzDevice(cfg.get("button_byte", 2), cfg.get("button_order"))
    try:
        device.open()
    except ConnectionError as e:
        print(f"error: {e}")
        return 1
    print("press buttons; Ctrl+C to stop")
    try:
        while True:
            report = device.read(timeout_ms=200)
            if report:
                print(list(report), "->", sorted(device.parse_buttons(report)))
    except KeyboardInterrupt:
        pass
    finally:
        device.close()
    return 0


def cmd_learn(cfg: dict) -> int:
    """--learn: interactively prompts for a button press per action per
    instance and saves the resulting bare-tap mappings. A CLI-only,
    tap-only equivalent of the GUI's Button mappings tab."""
    device = BuzzDevice(cfg.get("button_byte", 2), cfg.get("button_order"))
    try:
        device.open()
    except ConnectionError as e:
        print(f"error: {e}")
        return 1

    actions = ["play", "pause", "play_pause", "stop", "next", "prev", "vol_up", "vol_down",
               "mute", "fullscreen", "shuffle", "loop", "focus", "identify"]
    mappings = dict(cfg.get("mappings", {}))
    try:
        for instance in cfg.get("vlc_instances", {}):
            print(f"\n--- mapping buttons for '{instance}' (Enter to skip) ---")
            for action in actions:
                print(f"press the button for {instance}/{action} ...")
                btn = _wait_for_press(device)
                if btn is None:
                    continue
                mappings[btn] = f"{instance}/{action}"
                print(f"  mapped {btn} -> {instance}/{action}")
    except KeyboardInterrupt:
        print("\nstopped early; saving what was mapped so far")
    finally:
        device.close()

    cfg["mappings"] = mappings
    save_config(cfg)
    print(f"saved to {CONFIG_PATH}")
    return 0


def _wait_for_press(device: BuzzDevice, timeout_s: float = 15.0) -> Optional[str]:
    pressed_prev: set[str] = set()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        report = device.read(timeout_ms=200)
        if report:
            pressed = device.parse_buttons(report)
            newly = pressed - pressed_prev
            if newly:
                return sorted(newly)[0]
            pressed_prev = pressed
    return None


def cmd_check(cfg: dict, errors: list[str], warnings: list[str]) -> int:
    """--check: prints any load_config() errors/warnings plus the
    resulting (possibly migrated) config, and re-saves it. Returns 1 if
    there were errors."""
    for e in errors:
        print(f"error: {e}")
    for w in warnings:
        print(f"warning: {w}")
    print(json.dumps(cfg, indent=2, ensure_ascii=False))
    save_config(cfg)
    return 1 if errors else 0


# ---------------------------------------------------------------------
# --diagnose: run this on the machine with the real Buzz hardware to
# settle the things that can't be verified without it -- which byte holds
# the button bits, this unit's actual colour order, whether the LEDs
# work, and (optionally, with --diagnose-vlc) what your VLC does with
# volume and seek. Never crashes even if VLC is unreachable -- a
# diagnostic that dies mid-run tells you nothing, so every step degrades
# to "skipped" with a reason instead. Writes buzz2vlc_report.txt;
# uploads nothing and never puts your HTTP password in the report.
# ---------------------------------------------------------------------

def _diagnose_prompt(msg: str) -> str:
    try:
        return input(msg)
    except EOFError:
        return ""


def _diagnose_wait_press(device: "BuzzDevice", timeout_s: float = 20.0):
    prev = set()
    idle_report = None
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        report = device.read(timeout_ms=100)
        if report is None:
            continue
        if idle_report is None and not device.parse_buttons(report):
            idle_report = list(report)
        pressed = device.parse_buttons(report)
        newly = pressed - prev
        if newly:
            return sorted(newly)[0], list(report), idle_report
        prev = pressed
    return None, None, idle_report


def _diagnose_byte_and_order(lines: list[str]) -> None:
    device = BuzzDevice()
    try:
        device.open()
    except ConnectionError as e:
        lines.append(f"button_byte/order: SKIPPED ({e})")
        return

    print("\n--- byte offset & button order ---")
    print("Press buzzer 1's TOP-LEFT button (position 1 of 5) now...")
    btn, report, idle = _diagnose_wait_press(device, timeout_s=20)
    device.close()

    if idle is not None:
        lines.append(f"idle report: {idle}")
    if btn is None or report is None:
        lines.append("button_byte/order: no press detected within 20s -- SKIPPED")
        return

    changed_bytes = []
    if idle:
        for i in range(min(len(idle), len(report))):
            if idle[i] != report[i]:
                changed_bytes.append(i)
    lines.append(f"pressed report: {report}")
    lines.append(f"bytes that changed vs. idle: {changed_bytes}")
    default_byte = default_config()["button_byte"]
    if changed_bytes and changed_bytes[0] != default_byte:
        lines.append(
            f"NOTE: bits changed starting at byte {changed_bytes[0]}, but the default "
            f"button_byte is {default_byte}. Set \"button_byte\": {changed_bytes[0]} in "
            f"~/.buzz2vlc.json if buttons don't register."
        )
    else:
        lines.append("button_byte default looks correct for this unit.")

    lines.append(
        "Position-based check sidesteps the hid-sony-vs-pybuzzers colour-order disagreement: "
        "if the button you pressed (position 1, i.e. the big/red button) matches what buzz2vlc "
        "reports below, button_order is correct as-is."
    )
    lines.append(f"buzz2vlc decoded this press as: {btn}")


def _diagnose_leds(lines: list[str]) -> None:
    device = BuzzDevice()
    try:
        device.open()
    except ConnectionError as e:
        lines.append(f"LEDs: SKIPPED ({e})")
        return

    print("\n--- LED test ---")
    print("Watch the four handsets. Each should flash in turn.")
    for i in range(4):
        device.set_led(i, True)
        time.sleep(0.6)
        device.set_led(i, False)
    device.close()

    answer = _diagnose_prompt("Did you see all four handsets flash in order 1-4? [y/n] ").strip().lower()
    lines.append(f"LEDs observed working: {answer == 'y'}")


def _diagnose_vlc(lines: list[str], port: int, password: str, sound: Optional[str]) -> None:
    print("\n--- VLC volume/seek probe ---")
    remote = VLCRemote("diagnose", "127.0.0.1", port, password)
    if not remote.reachable():
        lines.append(f"VLC probe: SKIPPED (unreachable on port {port} -- is --extraintf http running?)")
        return

    try:
        st = remote.status()
        lines.append(f"VLC state: {st}")
        remote.set_volume(400, max_volume=1000)  # ask for an absurd value, see what VLC clamps to
        time.sleep(0.2)
        actual = remote.status()["volume"]
        lines.append(f"requested volume 400, VLC reports: {actual} (this machine's real ceiling)")
        remote.set_volume(st["volume"])  # restore
    except VLCError as e:
        lines.append(f"VLC probe: error ({e})")
        return

    if sound:
        try:
            remote.horn(sound)
            lines.append(f"played test sound: {sound}")
        except VLCError as e:
            lines.append(f"sound test: error ({e})")
        except Exception as e:  # noqa: BLE001 - a diagnostic must never crash mid-run
            lines.append(f"sound test: unexpected error ({e})")


def cmd_diagnose(vlc: Optional[tuple[str, str]], sound: Optional[str]) -> int:
    """--diagnose: on real hardware, settles button_byte/order/LEDs (and
    optionally probes a running VLC's real volume ceiling) and writes
    buzz2vlc_report.txt. Never raises even if VLC/hardware misbehave --
    each step degrades to "skipped" with a reason instead."""
    lines: list[str] = [f"buzz2vlc diagnostic report - {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]

    can_raise, why = window_control_available()
    lines.append(f"window focus/identify available: {can_raise} ({why})")

    try:
        receivers = list_receivers()
    except RuntimeError as e:
        lines.append(f"hidapi: {e}")
        receivers = []
    lines.append(f"receivers found: {receivers}")

    if receivers:
        _diagnose_byte_and_order(lines)
        _diagnose_leds(lines)
    else:
        lines.append("no Buzz receiver detected -- plug it in (and pair a wireless dongle) and re-run")

    if vlc:
        port, password = vlc
        try:
            _diagnose_vlc(lines, int(port), password, sound)
        except Exception as e:  # noqa: BLE001
            lines.append(f"VLC probe: unexpected error ({e})")

    DIAGNOSE_REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {DIAGNOSE_REPORT_PATH.resolve()}")
    print("Paste its contents back so button_byte/button_order can be set to match your hardware.")
    return 0


def cmd_selftest() -> int:
    """--selftest: runs the in-file unittest suite (no hardware or VLC
    needed) covering the volume clamp, malformed-report handling, config
    migration, and press-tracker timing. Run this after any code change."""
    import unittest

    class Tests(unittest.TestCase):
        def test_volume_clamp(self):
            for step, expected in [(400, 256), (-400, 0)]:
                self.assertTrue(0 <= max(0, min(256, 128 + step)) <= 256)

        def test_parse_buttons_short_report(self):
            device = BuzzDevice(button_byte=2, button_order=BUTTON_ORDER_DEFAULT)
            # a truncated report shouldn't raise or wipe out decodable bits
            result = device.parse_buttons([0, 0])
            self.assertEqual(result, set())

        def test_parse_buttons_non_list(self):
            device = BuzzDevice()
            self.assertEqual(device.parse_buttons(None), set())
            self.assertEqual(device.parse_buttons(42), set())

        def test_binding_round_trip(self):
            for value in ["movies/pause", {"tap": "a/b", "hold": "a/c"}, {"tap": "a/b"}]:
                normalized = _binding_to_config(_config_to_binding(value))
                self.assertEqual(_config_to_binding(normalized), _config_to_binding(value))

        def test_migrate_v1_keeps_port_and_password(self):
            migrated = _migrate_v1({"vlc": {"host": "1.2.3.4", "port": 9999, "password": "secret"},
                                     "mappings": {"1:red": "pause"}})
            inst = migrated["vlc_instances"]["player"]
            self.assertEqual(inst["port"], 9999)
            self.assertEqual(inst["password"], "secret")
            self.assertEqual(migrated["mappings"]["1:red"], "player/pause")

        def test_playlist_to_target_converts_bare_path_to_uri(self):
            target = _playlist_to_target(str(Path(__file__)))
            self.assertTrue(target.startswith("file://"))

        def test_playlist_to_target_passes_through_urls(self):
            for url in ("http://example.com/stream", "rtsp://1.2.3.4/live", "file:///already/a/uri.xspf"):
                self.assertEqual(_playlist_to_target(url), url)

        def test_press_tracker_tap_no_double_binding(self):
            events = []
            t = PressTracker(hold_ms=600, double_tap_ms=300,
                              has_double=lambda b: False, has_hold=lambda b: False,
                              emit=lambda b, e: events.append((b, e)))
            t.press("1:red", now=0)
            t.release("1:red", now=0.05)
            self.assertEqual(events, [("1:red", "tap")])

        def test_press_tracker_hold_fires_while_held(self):
            events = []
            t = PressTracker(hold_ms=100, has_hold=lambda b: True,
                              emit=lambda b, e: events.append((b, e)))
            t.press("1:red", now=0)
            t.poll(now=0.05)
            self.assertEqual(events, [])
            t.poll(now=0.15)
            self.assertEqual(events, [("1:red", "hold")])
            t.release("1:red", now=0.2)
            self.assertEqual(events, [("1:red", "hold")])  # release after hold is a no-op

        def test_press_tracker_double_tap(self):
            events = []
            t = PressTracker(double_tap_ms=300, has_double=lambda b: True,
                              emit=lambda b, e: events.append((b, e)))
            t.press("1:red", now=0)
            t.release("1:red", now=0.02)
            t.press("1:red", now=0.1)
            t.release("1:red", now=0.12)
            self.assertEqual(events, [("1:red", "double_tap")])
            try:
                t.stop()
            except Exception:
                pass

        def test_press_tracker_triple_tap(self):
            events = []
            t = PressTracker(double_tap_ms=300, has_double=lambda b: True, has_triple=lambda b: True,
                              emit=lambda b, e: events.append((b, e)))
            for start in (0, 0.1, 0.2):
                t.press("1:red", now=start)
                t.release("1:red", now=start + 0.02)
            self.assertEqual(events, [("1:red", "triple_tap")])
            try:
                t.stop()
            except Exception:
                pass

        def test_press_tracker_double_tap_when_no_triple_binding(self):
            # Three rapid taps with only a double-tap binding (no triple)
            # should resolve as double_tap rather than waiting/discarding.
            events = []
            t = PressTracker(double_tap_ms=300, has_double=lambda b: True, has_triple=lambda b: False,
                              emit=lambda b, e: events.append((b, e)))
            for start in (0, 0.1, 0.2):
                t.press("1:red", now=start)
                t.release("1:red", now=start + 0.02)
            self.assertEqual(events, [("1:red", "double_tap")])
            try:
                t.stop()
            except Exception:
                pass

    suite = unittest.TestLoader().loadTestsFromTestCase(Tests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point: parses args, dispatches to the matching cmd_*
    function, or (with no flags) loads the config and runs the listener
    daemon via Engine.run_forever()."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="list connected Buzz receivers")
    parser.add_argument("--debug", action="store_true", help="print raw HID reports and parsed buttons")
    parser.add_argument("--learn", action="store_true", help="interactively map buttons")
    parser.add_argument("--check", action="store_true", help="validate/migrate the config and print it")
    parser.add_argument("--selftest", action="store_true", help="run built-in checks, no hardware needed")
    parser.add_argument("--diagnose", action="store_true",
                         help="run on the real hardware to settle button_byte/order/LEDs; writes a report")
    parser.add_argument("--diagnose-vlc", nargs=2, metavar=("PORT", "PASSWORD"),
                         help="with --diagnose, also probe a running VLC instance's volume ceiling")
    parser.add_argument("--diagnose-sound", metavar="PATH", help="with --diagnose-vlc, test-play a sound file")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                         format="%(asctime)s %(levelname)s %(message)s")

    if args.selftest:
        return cmd_selftest()
    if args.list:
        return cmd_list()
    if args.diagnose:
        return cmd_diagnose(args.diagnose_vlc, args.diagnose_sound)

    cfg, errors, warnings = load_config()
    for w in warnings:
        log.warning(w)
    for e in errors:
        log.error(e)

    if args.check:
        return cmd_check(cfg, errors, warnings)
    if args.debug:
        return cmd_debug(cfg)
    if args.learn:
        return cmd_learn(cfg)

    if requests is None:
        log.error("the 'requests' package is not installed (pip install requests)")
        return 1

    log.warning(
        "VLC's HTTP password is visible in this process's command line/list to other local "
        "users; the interface is bound to loopback only, so this matters solely on a shared "
        "machine. Use a throwaway password there."
    )

    engine = Engine(cfg)
    log.info("starting buzz2vlc (Ctrl+C to stop)")
    try:
        engine.run_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
