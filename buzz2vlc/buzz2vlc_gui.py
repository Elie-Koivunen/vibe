#!/usr/bin/env python3
"""
buzz2vlc GUI - tkinter front-end for configuring VLC instances and button
mappings, launching VLC, and running the HID->VLC listener.

Uses the same ~/.buzz2vlc.json as the CLI (buzz2vlc.py), so you can map
with the GUI once and then run the headless listener on a media PC.
"""
from __future__ import annotations

import math
import platform
import queue
import subprocess
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Callable, Optional

import buzz2vlc as core

LAUNCH_LABELS = {
    "not_launched": "not launched",
    "starting": "starting...",
    "running": "running",
    "unreachable": "unreachable",
}
# Color-emoji code points (the "real" traffic-light glyphs) render as
# plain hollow outlines in Tk on Windows -- GDI-based text rendering
# there doesn't support color emoji fonts. A small PhotoImage circle
# drawn pixel-by-pixel (see _make_circle_icon) renders reliably
# everywhere and, via the Treeview's tree column, stays scoped to just
# the "launched" column instead of tinting the whole row.
LAUNCH_ICON_COLORS = {
    "not_launched": "#999999",
    "starting": "#d9a400",
    "running": "#2ca02c",
    "unreachable": "#d62728",
}


def _make_circle_icon(color: str, size: int = 14, bg: str = "#ffffff") -> tk.PhotoImage:
    """Draws a solid-color circle as a PhotoImage, pixel by pixel -- for
    the "launched" traffic-light icon. Color-emoji characters render as
    plain hollow outlines in Tk on Windows, verified live, so this is the
    reliable cross-platform way to get a colored dot."""
    img = tk.PhotoImage(width=size, height=size)
    cx = cy = size / 2.0
    r = size / 2.0 - 1
    for y in range(size):
        row = []
        for x in range(size):
            dx, dy = x - cx + 0.5, y - cy + 0.5
            row.append(color if dx * dx + dy * dy <= r * r else bg)
        img.put("{" + " ".join(row) + "}", to=(0, y))
    return img

# Pale (idle) / bright (pressed) fill colors for the live button grid,
# keyed by the same colour names buzz2vlc.BUTTON_ORDER_DEFAULT uses.
BUTTON_COLORS = {
    "red": ("#e6b3b3", "#e63333"),
    "yellow": ("#e6e2b3", "#e6d633"),
    "green": ("#b3e6b3", "#33cc33"),
    "orange": ("#e6cdb3", "#e68a33"),
    "blue": ("#b3c2e6", "#3366e6"),
}

# Left-to-right VISUAL layout on the physical controller: the big red
# button, then blue/orange/green/yellow in a row. This is independent of
# buzz2vlc.BUTTON_ORDER_DEFAULT, which is the HID *bit* order used to
# decode reports (hardware-driven, config driven, unrelated to how the
# buttons are laid out for a human to look at).
DISPLAY_ORDER = ["red", "blue", "orange", "green", "yellow"]
BIG_BUTTON_COLOR = "red"  # the oversized button on each buzzer

ACTIONS = [
    "play", "pause", "play_pause", "stop", "next", "prev", "seek+10s", "seek-10s",
    "vol_up", "vol_down", "mute", "fullscreen", "shuffle", "loop", "focus", "identify",
]
GLOBAL_ACTIONS = ["all_pause", "all_stop"]

FONT_MIN_SIZE = 7
FONT_MAX_SIZE = 32
FONT_STEP = 1


class FontScaler:
    """Menu- and shortcut-driven font size control for the whole GUI.

    Rescaling the handful of named Tk fonts (TkDefaultFont, TkTextFont,
    TkMenuFont, TkHeadingFont, ...) cascades to virtually every ttk widget
    -- labels, buttons, comboboxes, notebook tabs, the menu bar -- since
    they reference those named fonts unless individually overridden. The
    Treeview's row/heading font and row height need an explicit ttk.Style
    tweak on top of that, and any widget with an explicitly-assigned font
    (like the log Text box) needs to be registered separately."""

    NAMED_FONTS = [
        "TkDefaultFont", "TkTextFont", "TkFixedFont", "TkMenuFont",
        "TkHeadingFont", "TkCaptionFont", "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont",
    ]

    def __init__(self, root: tk.Tk):
        self.root = root
        self.style = ttk.Style(root)
        self.delta = 0
        self._base_sizes: dict[str, int] = {}
        for name in self.NAMED_FONTS:
            try:
                self._base_sizes[name] = tkfont.nametofont(name).cget("size")
            except tk.TclError:
                pass
        self._base_tree_size = 9
        self._base_row_height = 20
        self._extra_fonts: list[tuple[tkfont.Font, int]] = []  # (font, base_size)
        self.on_change: Optional[Callable[[], None]] = None  # e.g. re-run column autosizing

    def register(self, font: tkfont.Font) -> None:
        """Track an explicitly-assigned font (e.g. a Text widget's) so it
        scales along with everything else."""
        self._extra_fonts.append((font, font.cget("size")))

    def _clamped(self, base: int) -> int:
        return max(FONT_MIN_SIZE, min(FONT_MAX_SIZE, base + self.delta))

    def _apply(self) -> None:
        for name, base in self._base_sizes.items():
            tkfont.nametofont(name).configure(size=self._clamped(base))
        for font, base in self._extra_fonts:
            font.configure(size=self._clamped(base))
        tree_size = self._clamped(self._base_tree_size)
        self.style.configure("Treeview", font=("", tree_size))
        self.style.configure("Treeview.Heading", font=("", tree_size))
        self.style.configure("Treeview", rowheight=max(16, self._base_row_height + self.delta * 2))
        if self.on_change:
            self.on_change()

    def increase(self) -> None:
        self.delta = min(self.delta + FONT_STEP, FONT_MAX_SIZE - min(self._base_sizes.values(), default=10))
        self._apply()

    def decrease(self) -> None:
        self.delta = max(self.delta - FONT_STEP, FONT_MIN_SIZE - max(self._base_sizes.values(), default=10))
        self._apply()

    def reset(self) -> None:
        self.delta = 0
        self._apply()


def _enable_windows_dpi_awareness() -> None:
    if platform.system() != "Windows":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _bind_mousewheel(widget: tk.Widget, target) -> None:
    """X11 sends Button-4/5; Windows/macOS send <MouseWheel> with a delta."""
    def on_wheel(event):
        delta = -1 if getattr(event, "num", None) == 5 else (1 if getattr(event, "num", None) == 4 else 0)
        if delta == 0 and hasattr(event, "delta"):
            delta = 1 if event.delta > 0 else -1
        target.yview_scroll(-delta, "units")

    widget.bind("<Button-4>", on_wheel)
    widget.bind("<Button-5>", on_wheel)
    widget.bind("<MouseWheel>", on_wheel)


class ListenerThread:
    """Runs the HID->VLC bridge in a background thread, same PressTracker
    and Dispatcher the CLI uses, so a config saved here behaves identically
    under buzz2vlc.py."""

    def __init__(self, cfg: dict, log_fn):
        self.cfg = cfg
        self.log_fn = log_fn
        self.engine: Optional[core.Engine] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self.engine = core.Engine(self.cfg)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self.engine.run_forever()
        except Exception as e:  # noqa: BLE001
            self.log_fn(f"listener stopped: {e}")

    def stop(self) -> None:
        if self.engine:
            self.engine.shutdown()
        if self._thread:
            self._thread.join(timeout=2)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


class App(tk.Tk):
    """The buzz2vlc GUI's main window: VLC instances / Button mappings /
    Live buttons tabs, the activity log, and the listener lifecycle.
    Reads/writes the same ~/.buzz2vlc.json as the CLI."""

    def __init__(self):
        super().__init__()
        _enable_windows_dpi_awareness()
        self.title("buzz2vlc")
        self.geometry("820x560")

        self.cfg, errors, warnings = core.load_config()
        for w in warnings:
            self._log_startup(f"warning: {w}")
        for e in errors:
            self._log_startup(f"error: {e}")

        self.listener: Optional[ListenerThread] = None
        self._detect_until = 0.0
        self._detect_queue: "queue.Queue[str]" = queue.Queue()
        self._instance_pids: dict[str, int] = {}
        self._instance_procs: dict[str, subprocess.Popen] = {}
        self._button_queue: "queue.Queue[tuple[str, bool]]" = queue.Queue()
        self._logical_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._action_queue: "queue.Queue[str]" = queue.Queue()
        self._double_tap_timers: dict[str, str] = {}
        # A second, independent tap-counting state machine purely for the
        # Live buttons display. The engine's own tracker only bothers
        # distinguishing double/triple taps for buttons that actually have
        # a double_tap/triple_tap *binding* (so an unmapped button's tap
        # isn't delayed for nothing) -- which means a button with only a
        # plain "tap" action never shows "2"/"3" even when you genuinely
        # double/triple-tap it. This tracker always counts, regardless of
        # what's bound, so the visualization reflects the real physical
        # press pattern rather than only what's already been configured.
        self._live_tracker = core.PressTracker(
            double_tap_ms=self.cfg.get("double_tap_ms", 300),
            has_double=lambda btn: True,
            has_triple=lambda btn: True,
            emit=self._on_live_tracker_event,
        )
        self.fonts = FontScaler(self)

        self._build_menu()
        self._build_ui()
        self.fonts.on_change = self._autosize_instances_columns
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(200, self._poll_ui)
        self.after(2500, self._auto_refresh_status)

    def _build_menu(self) -> None:
        """Builds the View (font size) and Help (Manual/About) menus and
        their keyboard shortcuts."""
        menubar = tk.Menu(self)
        view_menu = tk.Menu(menubar, tearoff=False)
        view_menu.add_command(label="Increase Font Size", accelerator="Ctrl++",
                               command=self.fonts.increase)
        view_menu.add_command(label="Decrease Font Size", accelerator="Ctrl+-",
                               command=self.fonts.decrease)
        view_menu.add_command(label="Reset Font Size", accelerator="Ctrl+0",
                               command=self.fonts.reset)
        menubar.add_cascade(label="View", menu=view_menu)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="Manual", command=self._show_manual)
        help_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.config(menu=menubar)

        for seq in ("<Control-plus>", "<Control-equal>", "<Control-KP_Add>"):
            self.bind_all(seq, lambda e: self.fonts.increase())
        for seq in ("<Control-minus>", "<Control-KP_Subtract>"):
            self.bind_all(seq, lambda e: self.fonts.decrease())
        self.bind_all("<Control-0>", lambda e: self.fonts.reset())
        self.bind_all("<Control-MouseWheel>", self._on_ctrl_wheel)

    def _on_ctrl_wheel(self, event) -> None:
        if event.delta > 0:
            self.fonts.increase()
        else:
            self.fonts.decrease()

    def _read_contact_info(self) -> str:
        """CONTACT.txt (next to this file) holds GitHub link / developer
        email / license as plain "Label: value" lines, kept out of source
        so it can be edited without touching code. Missing file/lines
        degrade gracefully -- About still works, just without that line."""
        contact_path = Path(__file__).resolve().parent / "CONTACT.txt"
        try:
            lines = [ln.strip() for ln in contact_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except OSError:
            return ""
        return "\n".join(lines)

    def _show_about(self) -> None:
        contact = self._read_contact_info()
        text = (
            "buzz2vlc\n\n"
            "Control multiple VLC instances with a PS2/PS3 Buzz controller.\n\n"
            "Reads the Buzz USB HID receiver directly and drives one or more "
            "VLC instances over VLC's built-in HTTP interface -- each buzzer "
            "can control its own player and playlist.\n\n"
            "See Help → Manual for full usage, or README.md next to this program."
        )
        if contact:
            text += "\n\n" + contact
        messagebox.showinfo("About buzz2vlc", text)

    def _show_manual(self) -> None:
        readme_path = Path(__file__).resolve().parent / "README.md"
        try:
            text = readme_path.read_text(encoding="utf-8")
        except OSError as e:
            messagebox.showerror("buzz2vlc", f"Could not open {readme_path.name}: {e}")
            return

        win = tk.Toplevel(self)
        win.title("buzz2vlc manual")
        win.geometry("760x600")

        body = scrolledtext.ScrolledText(win, wrap="word", font=self._log_font)
        body.pack(fill="both", expand=True, padx=6, pady=6)
        body.insert("1.0", text)
        body.config(state="disabled")

        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 6))

    def _log_startup(self, msg: str) -> None:
        print(msg)

    # -- UI construction --------------------------------------------------
    def _build_ui(self) -> None:
        """Builds the notebook (3 tabs), the listener/save/reconnect/quit
        button row, and the activity log."""
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)

        self.instances_tab = ttk.Frame(notebook)
        self.mappings_tab = ttk.Frame(notebook)
        self.live_tab = ttk.Frame(notebook)
        notebook.add(self.instances_tab, text="VLC instances")
        notebook.add(self.mappings_tab, text="Button mappings")
        notebook.add(self.live_tab, text="Live buttons")

        self._build_instances_tab()
        self._build_mappings_tab()
        self._build_live_tab()

        bottom = ttk.Frame(self)
        bottom.pack(fill="x")
        self.listener_btn = ttk.Button(bottom, text="Start listener", command=self._toggle_listener)
        self.listener_btn.pack(side="left", padx=4, pady=4)
        ttk.Button(bottom, text="Save config", command=self._save).pack(side="left", padx=4, pady=4)
        ttk.Button(bottom, text="Reconnect receiver", command=self._reconnect_receiver).pack(
            side="left", padx=4, pady=4)
        ttk.Button(bottom, text="Quit", command=self._on_close).pack(side="right", padx=4, pady=4)

        log_bar = ttk.Frame(self)
        log_bar.pack(fill="x")
        ttk.Label(log_bar, text="Activity", font=("", 9, "bold")).pack(side="left", padx=4)
        self.verbose_controller_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(log_bar, text="Verbose controller activity",
                         variable=self.verbose_controller_var).pack(side="left", padx=8)
        ttk.Button(log_bar, text="Save log...", command=self._save_log).pack(side="right", padx=4, pady=2)
        ttk.Button(log_bar, text="Clear", command=self._clear_log).pack(side="right", padx=4, pady=2)

        self._log_font = tkfont.Font(font=tkfont.nametofont("TkFixedFont"))
        self.fonts.register(self._log_font)
        self.log_text = tk.Text(self, height=8, state="disabled", font=self._log_font)
        self.log_text.pack(fill="both", expand=False)

    def _build_instances_tab(self) -> None:
        """Builds the VLC instances Treeview and its action buttons."""
        # The "launched" traffic light lives in the tree column (#0), which
        # is the only Treeview column that supports a per-row icon; the
        # other fields are plain data columns.
        self._launch_icons = {state: _make_circle_icon(color) for state, color in LAUNCH_ICON_COLORS.items()}

        columns = ("name", "host", "port", "password", "playlist")
        self.instances_tree = ttk.Treeview(self.instances_tab, columns=columns, show="tree headings", height=8)
        self.instances_tree.heading("#0", text="launched")
        # stretch=False on every column -- otherwise ttk redistributes any
        # extra space beyond the configured widths proportionally across
        # columns whenever the window is wider than their sum, which
        # silently overrides content-based autosizing the moment the user
        # resizes the window at all.
        self.instances_tree.column("#0", width=130, anchor="w", stretch=False)
        for c in columns:
            self.instances_tree.heading(c, text=c)
            self.instances_tree.column(c, width=110, stretch=False)
        self.instances_tree.pack(fill="both", expand=True, padx=4, pady=4)
        self.instances_tree.bind("<Double-1>", self._on_instance_double_click)
        self._refresh_instances_tree()

        hint = ttk.Label(self.instances_tab, text="Double-click a row to edit it.", foreground="#666666")
        hint.pack(anchor="w", padx=4)

        btns = ttk.Frame(self.instances_tab)
        btns.pack(fill="x")
        ttk.Button(btns, text="Add", command=self._add_instance).pack(side="left", padx=2)
        ttk.Button(btns, text="Edit", command=self._edit_instance).pack(side="left", padx=2)
        ttk.Button(btns, text="Remove", command=self._remove_instance).pack(side="left", padx=2)
        ttk.Button(btns, text="Launch selected", command=self._launch_selected).pack(side="left", padx=8)
        ttk.Button(btns, text="Launch ALL", command=self._launch_all).pack(side="left", padx=2)
        ttk.Button(btns, text="Refresh status", command=self._refresh_status).pack(side="left", padx=8)
        ttk.Button(btns, text="Find window", command=self._find_window_selected).pack(side="left", padx=2)
        ttk.Button(btns, text="Identify", command=self._identify_selected).pack(side="left", padx=2)
        ttk.Button(btns, text="Shutdown ALL", command=self._shutdown_all).pack(side="left", padx=8)

    def _build_mappings_tab(self) -> None:
        """Builds the scrollable mapping grid: one controller dropdown per
        quadrant (see _on_buzzer_var_changed for the swap logic) and one
        row per button."""
        outer = ttk.Frame(self.mappings_tab)
        outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(outer)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        _bind_mousewheel(canvas, canvas)

        inner = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        ttk.Label(
            self.mappings_tab,
            text="\"controller\" picks which physical buzzer (1-4) this row's functions listen on -- "
                 "swap it here instead of redoing tap/hold/instance settings if a handset's battery dies "
                 "and you move to a spare. Two rows can't share the same controller; reassigning one "
                 "swaps it with whichever row currently has that number.",
            wraplength=760, justify="left", foreground="#444444",
        ).pack(anchor="w", padx=4, pady=(4, 0))

        headers = ["controller", "button", "instance", "tap", "hold", "double-tap", "triple-tap"]
        for col, h in enumerate(headers):
            ttk.Label(inner, text=h, font=("", 9, "bold")).grid(row=0, column=col, padx=4, pady=2)

        self.mapping_rows: dict[str, dict] = {}
        self._quadrant_vars: list[tk.StringVar] = []
        self._last_buzzer_values: dict[int, str] = {}
        self._suppress_buzzer_trace = False
        row = 1
        for slot_index, default_buzzer in enumerate(core.BUZZERS):
            buzzer_var = tk.StringVar(value=default_buzzer)
            self._quadrant_vars.append(buzzer_var)
            self._last_buzzer_values[id(buzzer_var)] = default_buzzer
            buzzer_var.trace_add("write", lambda *_a, v=buzzer_var: self._on_buzzer_var_changed(v))

            # One dropdown per quadrant, spanning its 5 color rows -- a
            # single control that's genuinely one selection, rather than
            # the same StringVar repeated on every row.
            buzzer_cb = ttk.Combobox(inner, textvariable=buzzer_var, values=list(core.BUZZERS), width=6,
                                      state="readonly")
            buzzer_cb.grid(row=row, column=0, rowspan=len(DISPLAY_ORDER), padx=4, sticky="n")

            for color in DISPLAY_ORDER:
                self._build_mapping_row(inner, row, slot_index, color, default_buzzer, buzzer_var)
                row += 1

        bottom = ttk.Frame(self.mappings_tab)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="Detect pressed button", command=self._detect_button).pack(side="left", padx=4,
                                                                                             pady=4)
        self.detect_label = ttk.Label(bottom, text="")
        self.detect_label.pack(side="left", padx=4)

    def _on_buzzer_var_changed(self, changed_var: tk.StringVar) -> None:
        """Keeps controller assignment a bijection: reassigning one
        quadrant's controller number swaps it with whichever quadrant
        currently holds that number, so two rows can never end up
        listening on the same physical buzzer."""
        if self._suppress_buzzer_trace:
            return
        new_value = changed_var.get()
        old_value = self._last_buzzer_values.get(id(changed_var), new_value)
        if new_value == old_value:
            return
        for other_var in self._quadrant_vars:
            if other_var is changed_var:
                continue
            if other_var.get() == new_value:
                self._suppress_buzzer_trace = True
                try:
                    other_var.set(old_value)
                finally:
                    self._suppress_buzzer_trace = False
                self._last_buzzer_values[id(other_var)] = old_value
                break
        self._last_buzzer_values[id(changed_var)] = new_value

    def _mapping_instance_choices(self) -> list[str]:
        names = list(self.cfg.get("vlc_instances", {}).keys())
        return names + [n for n in ("sfx",) if n not in names]

    def _build_mapping_row(self, parent, row: int, slot_index: int, color: str, default_buzzer: str,
                            buzzer_var: tk.StringVar) -> None:
        """Builds one button's row: instance/tap/hold/double/triple
        dropdowns, loaded from the config under `original_key`."""
        # The controller dropdown itself is built once per quadrant (see
        # _build_mappings_tab), spanning all 5 of its color rows -- this
        # row only places the fixed color label in that column.
        ttk.Label(parent, text=color).grid(row=row, column=1, sticky="w", padx=4)

        instance_var = tk.StringVar()
        instance_cb = ttk.Combobox(parent, textvariable=instance_var, values=self._mapping_instance_choices(),
                                    width=10, state="readonly")
        instance_cb.grid(row=row, column=2, padx=2)

        tap_var = tk.StringVar()
        tap_cb = ttk.Combobox(parent, textvariable=tap_var, values=[""] + ACTIONS + GLOBAL_ACTIONS, width=12,
                               state="readonly")
        tap_cb.grid(row=row, column=3, padx=2)

        hold_var = tk.StringVar()
        hold_cb = ttk.Combobox(parent, textvariable=hold_var, values=[""] + ACTIONS + GLOBAL_ACTIONS, width=12,
                                state="readonly")
        hold_cb.grid(row=row, column=4, padx=2)

        double_var = tk.StringVar()
        double_cb = ttk.Combobox(parent, textvariable=double_var, values=[""] + ACTIONS + GLOBAL_ACTIONS, width=12,
                                  state="readonly")
        double_cb.grid(row=row, column=5, padx=2)

        triple_var = tk.StringVar()
        triple_cb = ttk.Combobox(parent, textvariable=triple_var, values=[""] + ACTIONS + GLOBAL_ACTIONS, width=12,
                                  state="readonly")
        triple_cb.grid(row=row, column=6, padx=2)

        # The config key at load time -- always "<original buzzer>:<color>",
        # since that's the physical button this row's saved binding
        # belongs to regardless of any later controller reassignment.
        original_key = f"{default_buzzer}:{color}"
        binding = self.cfg.get("mappings", {}).get(original_key)
        norm = core._config_to_binding(binding)
        instance, verb = self._split_action(norm.get("tap", ""))
        instance_var.set(instance)
        if verb.startswith("horn:"):
            # "horn" isn't one of this editor's selectable actions -- there's
            # no GUI path to configure it, only hand-editing the JSON config.
            # Leave tap blank rather than show a value the dropdown can't
            # actually represent; _collect_mappings preserves the existing
            # horn binding untouched as long as this row isn't saved
            # completely empty (see there).
            pass
        else:
            tap_var.set(verb)
        hold_instance, hold_verb = self._split_action(norm.get("hold", ""))
        if hold_instance and hold_instance != instance:
            pass  # hold/double/triple on a different instance isn't shown separately; kept in raw config
        hold_var.set(hold_verb)
        _, dbl_verb = self._split_action(norm.get("double_tap", ""))
        double_var.set(dbl_verb)
        _, triple_verb = self._split_action(norm.get("triple_tap", ""))
        triple_var.set(triple_verb)

        row_id = f"slot{slot_index}:{color}"
        self.mapping_rows[row_id] = {
            "buzzer_var": buzzer_var, "color": color, "original_key": original_key,
            "instance": instance_var, "instance_cb": instance_cb, "tap": tap_var, "hold": hold_var,
            "double": double_var, "triple": triple_var,
        }

    def _refresh_mapping_instance_choices(self) -> None:
        """Called whenever vlc_instances changes, so an instance added
        after the mapping tab was built still shows up in every row's
        instance dropdown without needing to reopen the app."""
        choices = self._mapping_instance_choices()
        for row in self.mapping_rows.values():
            row["instance_cb"]["values"] = choices

    def _build_live_tab(self) -> None:
        """Builds the 4x5 live button-press grid (see _set_button_visual /
        _show_tap_count for how it's updated)."""
        container = ttk.Frame(self.live_tab)
        container.pack(fill="both", expand=True, padx=8, pady=8)

        ttk.Label(
            container,
            text="Lit while a button is physically held; shows \"2\" or \"3\" briefly on a double- "
                 "or triple-tap, so you can tell them apart from a plain press. Works while the "
                 "listener is running, or during Detect pressed button.",
            wraplength=520, justify="left",
        ).pack(anchor="w", pady=(0, 8))

        radius_normal, radius_big, gap, col_x0, row_h = 20, 30, 16, 140, 78

        col_x: dict[str, int] = {}
        col_radius: dict[str, int] = {}
        x = col_x0
        prev_radius = 0
        for i, color in enumerate(DISPLAY_ORDER):
            radius = radius_big if color == BIG_BUTTON_COLOR else radius_normal
            x = col_x0 if i == 0 else x + prev_radius + gap + radius
            col_x[color] = x
            col_radius[color] = radius
            prev_radius = radius

        canvas_w = x + prev_radius + 20
        canvas_h = 40 + len(core.BUZZERS) * row_h

        self.live_canvas = tk.Canvas(container, width=canvas_w, height=canvas_h, highlightthickness=0)
        self.live_canvas.pack(anchor="w")

        for color in DISPLAY_ORDER:
            self.live_canvas.create_text(col_x[color], 16, text=color, font=("", 9, "bold"))

        self._button_shapes: dict[str, int] = {}
        self._button_labels: dict[str, int] = {}
        for row, buzzer in enumerate(core.BUZZERS):
            y = 36 + row * row_h + row_h // 2
            self.live_canvas.create_text(55, y, text=f"Buzzer {buzzer}", font=("", 10))
            for color in DISPLAY_ORDER:
                cx, r = col_x[color], col_radius[color]
                key = f"{buzzer}:{color}"
                dim, _bright = BUTTON_COLORS[color]
                item = self.live_canvas.create_oval(cx - r, y - r, cx + r, y + r,
                                                      fill=dim, outline="#333333", width=2)
                label = self.live_canvas.create_text(cx, y, text="", font=("", 13, "bold"))
                self._button_shapes[key] = item
                self._button_labels[key] = label

        self.live_last_label = ttk.Label(container, text="last event: (none)")
        self.live_last_label.pack(anchor="w", pady=(8, 0))

    def _set_button_visual(self, key: str, pressed: bool) -> None:
        """Reflects real physical state only -- bright exactly while held,
        dim exactly when released -- via the engine's paired press/release
        events, with no time-based guessing about how long a hold lasts."""
        item = self._button_shapes.get(key)
        if item is None:
            return
        _, _, color = key.partition(":")
        dim, bright = BUTTON_COLORS.get(color, ("#cccccc", "#666666"))
        self.live_canvas.itemconfig(item, fill=bright if pressed else dim,
                                     outline="#000000" if pressed else "#333333",
                                     width=3 if pressed else 2)
        self.live_last_label.config(text=f"last event: {key} {'pressed (held)' if pressed else 'released'}")
        if not pressed:
            self._clear_double_tap(key)

    def _reset_button_visuals(self) -> None:
        for key in list(self._button_shapes.keys()):
            self._set_button_visual(key, False)
        for key in list(self._button_labels.keys()):
            self._clear_double_tap(key)

    def _show_tap_count(self, key: str, count: str) -> None:
        """Briefly overlays "2" or "3" on a button's circle so a
        double-tap and a triple-tap are visually distinguishable from a
        plain press -- useful when they're mapped to different actions."""
        label = self._button_labels.get(key)
        if label is None:
            return
        self.live_canvas.itemconfig(label, text=count)
        pending = self._double_tap_timers.pop(key, None)
        if pending:
            try:
                self.after_cancel(pending)
            except Exception:
                pass
        self._double_tap_timers[key] = self.after(900, lambda k=key: self._clear_double_tap(k))

    def _clear_double_tap(self, key: str) -> None:
        label = self._button_labels.get(key)
        if label is not None:
            self.live_canvas.itemconfig(label, text="")
        pending = self._double_tap_timers.pop(key, None)
        if pending:
            try:
                self.after_cancel(pending)
            except Exception:
                pass

    def _on_raw_button_change(self, btn: str, is_pressed: bool) -> None:
        """Called from the engine's HID-reading thread; must stay
        thread-safe (queue-only for Tk state) since widgets may only be
        touched from the main thread. PressTracker itself is thread-safe
        (internal lock + threading.Timer), so feeding _live_tracker
        directly here is fine -- its emit callback is what queues the
        eventual "2"/"3" for the main thread to pick up."""
        self._button_queue.put((btn, is_pressed))
        if is_pressed:
            self._live_tracker.press(btn)
        else:
            self._live_tracker.release(btn)

    def _on_live_tracker_event(self, btn: str, event: str) -> None:
        """emit callback for _live_tracker; may run on a threading.Timer
        thread, so queue-only here too."""
        if event == "double_tap":
            self._logical_queue.put((btn, "2"))
        elif event == "triple_tap":
            self._logical_queue.put((btn, "3"))

    def _on_engine_action(self, action: str) -> None:
        """engine.on_action callback: fires with the resolved action
        string right before it runs. Called from the Dispatcher thread,
        so queue-only here too."""
        self._action_queue.put(action)

    @staticmethod
    def _split_action(action: str) -> tuple[str, str]:
        """Splits "instance/verb" into (instance, verb); a global action
        (no instance) returns ("", action)."""
        if not action:
            return "", ""
        if action in GLOBAL_ACTIONS:
            return "", action
        instance, _, verb = action.partition("/")
        return instance, verb

    def _browse_playlist(self, var: tk.StringVar) -> None:
        path = filedialog.askopenfilename(
            title="Choose playlist or media file",
            filetypes=[
                ("Playlists", "*.xspf *.m3u *.m3u8 *.pls"),
                ("Media files", "*.wav *.mp3 *.flac *.ogg *.mp4 *.mkv *.avi *.webm"),
                ("All files", "*.*"),
            ],
        )
        if path:
            var.set(path)

    # -- instances tab actions ---------------------------------------
    def _refresh_instances_tree(self) -> None:
        self.instances_tree.delete(*self.instances_tree.get_children())
        for name, inst in self.cfg.get("vlc_instances", {}).items():
            state = "running" if name in self._instance_procs else "not_launched"
            self.instances_tree.insert("", "end", iid=name, text=" " + LAUNCH_LABELS[state],
                                        image=self._launch_icons[state], values=(
                name, inst.get("host", "127.0.0.1"), inst["port"], inst.get("password", ""),
                inst.get("playlist") or ""))
        self._autosize_instances_columns()

    def _autosize_instances_columns(self, max_width: int = 420) -> None:
        """Sizes each column to fit its widest current value (header
        included) instead of a fixed guess -- a long playlist path no
        longer gets truncated while "port" stays narrow."""
        font = tkfont.nametofont("TkDefaultFont")
        pad = 24

        icon_allowance = 26  # rough width of the traffic-light icon plus its leading space
        tree_w = icon_allowance + font.measure(self.instances_tree.heading("#0")["text"]) + pad
        for iid in self.instances_tree.get_children():
            tree_w = max(tree_w, icon_allowance + font.measure(self.instances_tree.item(iid, "text")) + pad)
        self.instances_tree.column("#0", width=min(tree_w, max_width), stretch=False)

        for c in ("name", "host", "port", "password", "playlist"):
            col_w = font.measure(str(self.instances_tree.heading(c)["text"])) + pad
            for iid in self.instances_tree.get_children():
                col_w = max(col_w, font.measure(str(self.instances_tree.set(iid, c))) + pad)
            self.instances_tree.column(c, width=min(col_w, max_width), stretch=False)

    def _selected_instance_name(self) -> Optional[str]:
        sel = self.instances_tree.selection()
        return sel[0] if sel else None

    def _on_instance_double_click(self, event) -> None:
        row_id = self.instances_tree.identify_row(event.y)
        if row_id:
            self.instances_tree.selection_set(row_id)
            self._edit_instance()

    def _add_instance(self) -> None:
        self._instance_dialog(None)

    def _edit_instance(self) -> None:
        name = self._selected_instance_name()
        if name:
            self._instance_dialog(name)

    def _rename_instance_in_mappings(self, old_name: str, new_name: str) -> None:
        """A renamed instance's old name otherwise lingers inside every
        mapping's action strings ("oldname/verb", stored as plain text,
        not a reference) -- verified live: without this, a mapped row
        keeps showing the no-longer-existing old name as its selected
        instance, since it's not even in the dropdown's valid choices
        anymore. Rewrites cfg["mappings"] in place, then updates any
        already-built row widget currently showing the old name so the
        change is visible immediately, not just after a restart."""
        old_prefix, new_prefix = f"{old_name}/", f"{new_name}/"

        def rewrite(action):
            if isinstance(action, str) and action.startswith(old_prefix):
                return new_prefix + action[len(old_prefix):]
            return action

        for key, binding in self.cfg.get("mappings", {}).items():
            if isinstance(binding, str):
                self.cfg["mappings"][key] = rewrite(binding)
            elif isinstance(binding, dict):
                for slot, action in list(binding.items()):
                    binding[slot] = rewrite(action)

        for row in self.mapping_rows.values():
            if row["instance"].get() == old_name:
                row["instance"].set(new_name)

    def _instance_dialog(self, existing_name: Optional[str]) -> None:
        """Opens the add/edit dialog for one VLC instance. `existing_name`
        is None for Add, or the instance being edited (double-click or
        the Edit button both land here)."""
        dlg = tk.Toplevel(self)
        dlg.title("VLC instance")
        fields = ["name", "host", "port", "password", "playlist"]
        vars_ = {f: tk.StringVar() for f in fields}
        if existing_name:
            inst = self.cfg["vlc_instances"][existing_name]
            vars_["name"].set(existing_name)
            vars_["host"].set(inst.get("host", "127.0.0.1"))
            vars_["port"].set(str(inst["port"]))
            vars_["password"].set(inst.get("password", ""))
            vars_["playlist"].set(inst.get("playlist") or "")
        else:
            vars_["host"].set("127.0.0.1")
            vars_["password"].set("buzz")

        for i, f in enumerate(fields):
            ttk.Label(dlg, text=f).grid(row=i, column=0, sticky="w", padx=4, pady=2)
            ttk.Entry(dlg, textvariable=vars_[f], width=30).grid(row=i, column=1, padx=4, pady=2)
            if f == "playlist":
                ttk.Button(dlg, text="Browse...", width=10,
                           command=lambda v=vars_[f]: self._browse_playlist(v)).grid(row=i, column=2, padx=4)

        def save():
            new_name = vars_["name"].get().strip()
            if not new_name:
                messagebox.showerror("buzz2vlc", "name is required")
                return
            try:
                port = int(vars_["port"].get())
            except ValueError:
                messagebox.showerror("buzz2vlc", "port must be a number")
                return

            instances = self.cfg["vlc_instances"]
            # port-clash check must run AFTER accounting for a rename of
            # the same instance, or renaming "kitchen"->"diner" on its own
            # port collides with itself.
            for other_name, other in instances.items():
                if other_name == existing_name:
                    continue
                if other["port"] == port:
                    messagebox.showerror("buzz2vlc", f"port {port} is already used by '{other_name}'")
                    return

            renamed = existing_name and existing_name != new_name
            if renamed:
                del instances[existing_name]
                # a rename invalidates any tracked launch/PID for the old
                # name; drop it rather than showing stale status under a
                # name that no longer exists.
                self._instance_procs.pop(existing_name, None)
                self._instance_pids.pop(existing_name, None)

            instances[new_name] = {
                "host": vars_["host"].get() or "127.0.0.1",
                "port": port,
                "password": vars_["password"].get(),
                "playlist": vars_["playlist"].get() or None,
            }
            if renamed:
                self._rename_instance_in_mappings(existing_name, new_name)
            self._refresh_instances_tree()
            self._refresh_mapping_instance_choices()
            dlg.destroy()

        ttk.Button(dlg, text="Save", command=save).grid(row=len(fields), column=0, columnspan=2, pady=6)

    def _remove_instance(self) -> None:
        name = self._selected_instance_name()
        if name and name in self.cfg["vlc_instances"]:
            del self.cfg["vlc_instances"][name]
            self._instance_procs.pop(name, None)
            self._instance_pids.pop(name, None)
            self._refresh_instances_tree()
            self._refresh_mapping_instance_choices()
            # Removing a configured instance shrinks _tile_geometry's
            # grid -- re-tile whichever remaining instances are still
            # running into it.
            for other_name, other_proc in self._instance_procs.items():
                if other_proc.poll() is not None:
                    continue
                ox, oy, ow, oh = self._tile_geometry(other_name)
                threading.Thread(target=core.position_window, args=(other_proc.pid, ox, oy, ow, oh),
                                  daemon=True).start()

    def _launch_selected(self) -> None:
        name = self._selected_instance_name()
        if name:
            self._launch(name)

    def _launch_all(self) -> None:
        for name in list(self.cfg.get("vlc_instances", {})):
            self._launch(name)

    def _tile_geometry(self, name: str) -> tuple[int, int, int, int]:
        """Computes this instance's slot in a grid covering the desktop's
        usable work area (never the taskbar, wherever it's docked --
        SPI_GETWORKAREA handles top/bottom/left/right and auto-hide
        correctly, unlike guessing a fixed height to subtract), inset by
        a further 2cm margin from each edge. Slot = the instance's
        position in the config's instance order, so it stays stable
        across launches regardless of launch order."""
        names = list(self.cfg.get("vlc_instances", {}).keys())
        total = max(len(names), 1)
        index = names.index(name) if name in names else 0
        columns = math.ceil(math.sqrt(total))
        rows = math.ceil(total / columns)

        area = core.usable_screen_area()
        if area is None:  # non-Windows fallback: full screen, no taskbar API to query
            area = (0, 0, self.winfo_screenwidth(), self.winfo_screenheight())
        area_x, area_y, area_w, area_h = area

        margin = core.cm_to_px(2.0)
        x0, y0 = area_x + margin, area_y + margin
        usable_w = max(area_w - 2 * margin, 100)
        usable_h = max(area_h - 2 * margin, 100)

        col, row = index % columns, index // columns
        w, h = usable_w // columns, usable_h // rows
        return x0 + col * w, y0 + row * h, w, h

    def _launch(self, name: str) -> None:
        inst = self.cfg["vlc_instances"][name]
        try:
            proc = core.launch_instance(name, inst, self.cfg.get("vlc_path"))
            self._instance_pids[name] = proc.pid
            self._instance_procs[name] = proc
            if self.listener and self.listener.engine:
                self.listener.engine.register_pid(name, proc.pid)
            self._set_launch_status(name, "starting")
            self._log(f"launched {name} (pid={proc.pid})")
            x, y, w, h = self._tile_geometry(name)
            threading.Thread(target=core.wait_and_position_window, args=(proc.pid, x, y, w, h),
                              daemon=True).start()
            # Launching this instance can change the grid size (e.g. going
            # from 1 to 2 running instances), which leaves any
            # already-running instance's window at its *old*, now-wrong
            # slot -- verified live: launching instances one at a time
            # left the first one spanning the full screen (its 1x1-grid
            # position from when it was the only one), overlapping the
            # rest. Reposition every other already-launched instance too.
            for other_name, other_proc in self._instance_procs.items():
                if other_name == name or other_proc.poll() is not None:
                    continue
                ox, oy, ow, oh = self._tile_geometry(other_name)
                threading.Thread(target=core.position_window, args=(other_proc.pid, ox, oy, ow, oh),
                                  daemon=True).start()
            self.after(1500, self._refresh_status)  # give the HTTP interface a moment to bind
        except RuntimeError as e:
            messagebox.showerror("buzz2vlc", str(e))

    def _safe_after(self, ms: int, fn) -> None:
        """self.after() from a background thread, but the app can close
        while that thread is still mid-flight (e.g. blocked in an HTTP
        call with a multi-second timeout) -- verified live: calling
        self.after() after the Tk root is destroyed raises RuntimeError
        ("main thread is not in main loop"), an unhandled exception in
        the worker thread. Swallow that race here instead of letting
        every call site guard against it individually."""
        try:
            self.after(ms, fn)
        except (RuntimeError, tk.TclError):
            pass

    def _auto_refresh_status(self) -> None:
        self._refresh_status()
        self.after(2500, self._auto_refresh_status)

    def _refresh_status(self) -> None:
        def worker():
            for name, inst in self.cfg.get("vlc_instances", {}).items():
                proc = self._instance_procs.get(name)
                if proc is None:
                    self._safe_after(0, lambda n=name: self._set_launch_status(n, "not_launched"))
                    continue
                if proc.poll() is not None:  # process exited/crashed
                    self._safe_after(0, lambda n=name: self._set_launch_status(n, "not_launched"))
                    continue
                remote = core.VLCRemote(name, inst.get("host", "127.0.0.1"), inst["port"], inst.get("password", ""))
                reachable = remote.reachable()
                state = "running" if reachable else "unreachable"
                self._safe_after(0, lambda n=name, s=state: self._set_launch_status(n, s))

        threading.Thread(target=worker, daemon=True).start()

    def _set_launch_status(self, name: str, state: str) -> None:
        if self.instances_tree.exists(name):
            self.instances_tree.item(name, text=" " + LAUNCH_LABELS[state], image=self._launch_icons[state])

    def _shutdown_all(self) -> None:
        """Closes every still-running launched instance gracefully (see
        core.close_instance), after a confirmation prompt."""
        procs = {name: proc for name, proc in self._instance_procs.items() if proc.poll() is None}
        if not procs:
            self._log("no launched instances to shut down")
            return
        if not messagebox.askyesno("buzz2vlc", f"Shut down all {len(procs)} launched instance(s)?"):
            return

        def worker():
            for name, proc in procs.items():
                self._safe_after(0, lambda n=name: self._log(f"shutting down {n}..."))
                core.close_instance(proc)  # WM_CLOSE first, hard-kill only as a fallback
                self._safe_after(0, lambda n=name: self._on_instance_closed(n))

        threading.Thread(target=worker, daemon=True).start()

    def _on_instance_closed(self, name: str) -> None:
        self._instance_procs.pop(name, None)
        self._set_launch_status(name, "not_launched")
        self._log(f"{name} shut down")
        # Note: _tile_geometry's grid is sized by configured instance
        # count, not how many are currently *running* -- shutting one
        # down doesn't shrink it, so no repositioning is needed here.
        # Removing an instance from the config entirely does shrink it;
        # see _remove_instance.

    def _find_window_selected(self) -> None:
        name = self._selected_instance_name()
        if name:
            core.raise_window(f"buzz2vlc: {name}", pid=self._instance_pids.get(name))

    def _identify_selected(self) -> None:
        name = self._selected_instance_name()
        if name:
            threading.Thread(target=core.identify_window,
                              kwargs={"title_hint": f"buzz2vlc: {name}", "pid": self._instance_pids.get(name)},
                              daemon=True).start()

    # -- mapping detect ------------------------------------------------
    def _detect_button(self) -> None:
        self.detect_label.config(text="press a button... (5s)")
        self._detect_until = time.monotonic() + 5.0
        if not (self.listener and self.listener.running):
            threading.Thread(target=self._detect_worker, daemon=True).start()

    def _detect_worker(self) -> None:
        """Reports the first newly-pressed button for the mapping-detect
        text label, but -- since we removed time-based auto-revert from
        the live-buttons grid in favor of only ever showing real physical
        state -- keeps reading a little longer to forward that button's
        actual release too, so its circle doesn't stay lit forever. A
        short safety-net timeout covers the case where release is somehow
        never seen."""
        device = core.BuzzDevice(self.cfg.get("button_byte", 2), self.cfg.get("button_order"))
        try:
            device.open()
        except ConnectionError as e:
            self._detect_queue.put(f"error: {e}")
            return
        prev: set[str] = set()
        detected_btn: Optional[str] = None
        release_deadline = 0.0
        try:
            while True:
                now = time.monotonic()
                if detected_btn is None and now >= self._detect_until:
                    break
                if detected_btn is not None and now >= release_deadline:
                    self._button_queue.put((detected_btn, False))
                    self._live_tracker.release(detected_btn)
                    return
                report = device.read(timeout_ms=100)
                if report:
                    pressed = device.parse_buttons(report)
                    for btn in pressed - prev:
                        self._button_queue.put((btn, True))
                        self._live_tracker.press(btn)
                    for btn in prev - pressed:
                        self._button_queue.put((btn, False))
                        self._live_tracker.release(btn)
                    if detected_btn is None:
                        newly = pressed - prev
                        if newly:
                            detected_btn = sorted(newly)[0]
                            self._detect_queue.put(detected_btn)
                            release_deadline = now + 3.0
                    elif detected_btn not in pressed:
                        return  # its real release was already forwarded above
                    prev = pressed
        finally:
            device.close()
        if detected_btn is None:
            self._detect_queue.put("")

    def _poll_ui(self) -> None:
        try:
            while True:
                result = self._detect_queue.get_nowait()
                if result.startswith("error:"):
                    self.detect_label.config(text=result)
                elif result:
                    self.detect_label.config(text=f"detected: {result}")
                    self._highlight_row(result)
                else:
                    self.detect_label.config(text="no press detected")
        except queue.Empty:
            pass
        try:
            while True:
                btn, pressed = self._button_queue.get_nowait()
                self._set_button_visual(btn, pressed)
                if self.verbose_controller_var.get():
                    self._log(f"controller: {btn} {'pressed' if pressed else 'released'}")
        except queue.Empty:
            pass
        try:
            while True:
                btn, count = self._logical_queue.get_nowait()
                self._show_tap_count(btn, count)
                if self.verbose_controller_var.get():
                    label = {"2": "double-tap", "3": "triple-tap"}.get(count, count)
                    self._log(f"controller: {btn} {label}")
        except queue.Empty:
            pass
        try:
            while True:
                action = self._action_queue.get_nowait()
                if self.verbose_controller_var.get():
                    self._log(f"action: {action}")
        except queue.Empty:
            pass
        self.after(200, self._poll_ui)

    def _highlight_row(self, key: str) -> None:
        # Mapping rows don't currently keep widget refs for highlighting;
        # detect still reports the key in the label above the grid.
        pass

    # -- save / listener -------------------------------------------------
    def _collect_mappings(self) -> dict:
        mappings = {}
        for row_id, row in self.mapping_rows.items():
            key = f"{row['buzzer_var'].get()}:{row['color']}"
            instance = row["instance"].get()
            tap = row["tap"].get()
            hold = row["hold"].get()
            double = row["double"].get()
            triple = row["triple"].get()

            binding = {}
            if tap and instance:
                binding["tap"] = tap if tap in GLOBAL_ACTIONS else f"{instance}/{tap}"
            elif tap in GLOBAL_ACTIONS:
                binding["tap"] = tap
            if hold and instance:
                binding["hold"] = hold if hold in GLOBAL_ACTIONS else f"{instance}/{hold}"
            if double and instance:
                binding["double_tap"] = double if double in GLOBAL_ACTIONS else f"{instance}/{double}"
            if triple and instance:
                binding["triple_tap"] = triple if triple in GLOBAL_ACTIONS else f"{instance}/{triple}"

            # Preserve unmapped fields (like repeat, or a horn:<path> tap
            # -- "horn" isn't a selectable action in this editor, so a
            # legacy horn binding would otherwise vanish the moment this
            # row is saved with nothing else changed) using THIS row's
            # originally-loaded binding as the base -- not whatever
            # currently sits at the (possibly reassigned) target key,
            # which belongs to a different row.
            existing = core._config_to_binding(self.cfg.get("mappings", {}).get(row["original_key"]))
            if not binding and not existing:
                continue
            merged = {**existing, **binding}
            mappings[key] = core._binding_to_config(merged) if len(merged) > 1 else list(merged.values())[0]
        return mappings

    def _save(self) -> None:
        self.cfg["mappings"] = self._collect_mappings()
        core.save_config(self.cfg)
        if self.listener and self.listener.running:
            self.listener.cfg = self.cfg  # hot-update; new instances need a listener restart
            self._log("config saved (listener hot-updated mappings; restart it after adding instances)")
        else:
            self._log("config saved")

    def _toggle_listener(self) -> None:
        """Starts or stops the HID->VLC listener and wires the engine's
        callbacks (button/action/logical-event) to this window's queues."""
        if self.listener and self.listener.running:
            self.listener.stop()
            self.listener = None
            self.listener_btn.config(text="Start listener")
            self._log("listener stopped")
            self._reset_button_visuals()
            return

        self.cfg["mappings"] = self._collect_mappings()
        self.listener = ListenerThread(self.cfg, self._log)
        self.listener.start()
        self.listener.engine.on_button_change = self._on_raw_button_change
        self.listener.engine.on_action = self._on_engine_action
        for name, pid in self._instance_pids.items():
            self.listener.engine.register_pid(name, pid)
        self.listener_btn.config(text="Stop listener")
        self._log("listener started (waits for the receiver if unplugged)")

    def _reconnect_receiver(self) -> None:
        """Forces an immediate rescan for a Buzz receiver -- useful right
        after swapping in a different physical controller (e.g. a spare
        set when one's batteries die), so it doesn't wait out the poll
        interval. All mappings/calibration live in the saved config, not
        tied to any particular receiver, so the new controller picks up
        the exact same instance and button settings automatically."""
        if self.listener and self.listener.running:
            self.listener.engine.request_reconnect()
            self._log("reconnect requested; rescanning for a Buzz receiver")
        else:
            self._log("start the listener first, then Reconnect receiver picks up a swapped controller")

    def _log(self, msg: str) -> None:
        """Appends one timestamped line to the activity log. Must be
        called from the main thread -- background threads should go
        through _safe_after(0, lambda: self._log(...)) instead."""
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.config(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {msg}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _clear_log(self) -> None:
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def _save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save activity log",
            defaultextension=".log",
            filetypes=[("Log files", "*.log"), ("Text files", "*.txt"), ("All files", "*.*")],
            initialfile=f"buzz2vlc-{time.strftime('%Y%m%d-%H%M%S')}.log",
        )
        if not path:
            return
        try:
            Path(path).write_text(self.log_text.get("1.0", "end"), encoding="utf-8")
            self._log(f"log saved to {path}")
        except OSError as e:
            messagebox.showerror("buzz2vlc", f"Could not save log: {e}")

    def _on_close(self) -> None:
        """Graceful shutdown: stops the listener and live tracker, cancels
        pending Tk callbacks, then destroys the window. Used by the
        window's own close button and the Quit button alike."""
        if self.listener and self.listener.running:
            self.listener.stop()
        self._live_tracker.stop()
        # cancel any pending Tk callbacks before the interpreter tears down
        for after_id in self.tk.eval("after info").split():
            try:
                self.after_cancel(after_id)
            except Exception:
                pass
        self.destroy()


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
