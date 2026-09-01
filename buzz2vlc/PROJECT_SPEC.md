---
name: project-buzz2vlc-spec
description: Full specification of the buzz2vlc project (PS2/PS3 Buzz controller -> multi-instance VLC control) — enough detail to rebuild it from scratch if the code at C:\ADM\_claude\buzz2vlc is lost.
metadata: 
  node_type: memory
  type: project
  originSessionId: 36fd5770-3673-4d5f-98d2-b57e0fb92f5b
  modified: 2026-09-01T10:42:15.454Z
---

# buzz2vlc — full rebuild specification

**Current code location:** `C:\ADM\_claude\buzz2vlc\` — read that first if it still
exists; this memory is the disaster-recovery fallback plus the reasoning
trail (the "why") that isn't visible from the code alone.

**Provenance:** originally built in a long Claude.ai chat (many rounds of
live debugging against real VLC/hardware), then rebuilt from that chat's
spec via Claude Code in this session because the share-link export only
preserved initial file writes, not later edits. Since the rebuild, it has
been through several more rounds of **live verification against real
installed VLC on Windows** (not just written and assumed correct) — most
of the value in this memory is the list of real bugs that live testing
caught, several of which contradicted reasonable-sounding first
attempts.

## Goal

A small Python daemon reads a PS2/PS3 "Buzz" USB HID controller (4
handsets, 5 buttons each: 1 big red + yellow/green/orange/blue) and
drives **multiple** VLC instances (each with its own playlist) over
VLC's built-in HTTP interface. Rationale: VLC's Lua extension API has no
USB/HID access at all, so a companion-process design talking to VLC over
HTTP is the only way to do this; the HTTP interface is the same endpoint
VLC's own Lua interface uses, so this uses that surface without writing
Lua.

## File layout (2 Python files by design, not accidental)

- **`buzz2vlc.py`** — everything headless. No `tkinter` import, so it
  runs on a headless media PC / minimal install. Contains, in order:
  - `PressTracker` class (tap/hold/double_tap/triple_tap state machine)
  - `default_config()`, config load/save/migrate (`_migrate_v1`, v1→v2)
  - `VLCRemote` (HTTP client, `requests.Session` for keep-alive)
  - `BuzzDevice` (hidapi wrapper, button decode, LED control)
  - `Dispatcher` (bounded queue + worker thread, isolates one bad action)
  - `Engine` (ties it together: config, remotes, tracker, dispatcher,
    ducking, hotplug reconnect loop, LED status loop)
  - Window management section (`raise_window`/`identify_window`,
    Linux/X11 + Windows ctypes, PID-based matching)
  - `find_vlc_path`, `port_in_use`, `build_launch_command`,
    `launch_instance`, `close_instance`
  - CLI: `--list --debug --learn --check --selftest --diagnose
    --diagnose-vlc --diagnose-sound`
  - `cmd_selftest`: an in-file `unittest` suite (12 tests as of last
    edit), runs with no hardware/VLC needed — always run this after
    editing and paste the tail of its output as proof, don't just claim
    "should work".
- **`buzz2vlc_gui.py`** — tkinter GUI, imports `buzz2vlc as core` for
  everything except widgets. 3 tabs: VLC instances, Button mappings,
  Live buttons. This is the *only* file with a tkinter dependency.
- **`Launch_buzz2vlc.bat`** — double-click entry point. **Must have CRLF
  line endings** — cmd.exe's parenthesized `if` blocks silently break on
  LF-only files (`- was unexpected at this time.`); this bit us once.
  Uses `goto`-based branching (more robust than nested `if(...)` blocks
  regardless of line-ending risk). Checks Python present, installs
  `hidapi`/`requests` on first run, launches via `pythonw` (no console).
- **`README.md`** — kept in sync with every change; has a "Known gaps"
  section and inline callouts for every fix that came from live testing
  (search it for "verified live").
- **`CONTACT.txt`** — plain "Label: value" lines (GitHub / Email /
  License), read by the GUI's Help -> About dialog
  (`App._read_contact_info`). Kept out of source deliberately so it's
  editable without touching code. **Ships with placeholder values**
  (`your-username`, `your-email@example.com`) — real project info was
  never provided, don't invent it if asked to "fill it in."

No `buzz2vlc_press.py` / `buzz2vlc_window.py` / `buzz2vlc_diagnose.py` —
those were separate files in an earlier pass and were folded into
`buzz2vlc.py` on request ("minimize excess number of files"). Don't
re-split them without being asked.

## Config schema (`~/.buzz2vlc.json`, override via `BUZZ2VLC_CONFIG`)

```json
{
  "config_version": 2,
  "button_byte": 2,
  "button_order": ["red","yellow","green","orange","blue"],
  "hold_ms": 600, "double_tap_ms": 300, "repeat_ms": 0, "debounce_ms": 50,
  "volume_step": 26, "max_volume": 256,
  "led_feedback": true, "led_status": true,
  "reconnect_interval_s": 3,
  "duck": {"enabled": true, "level_pct": 25, "restore_delay_s": 2.5},
  "vlc_path": null,
  "vlc_instances": {
    "movies": {"host": "127.0.0.1", "port": 8080, "password": "buzz", "playlist": "movies.xspf"}
  },
  "mappings": {
    "1:red": {"tap": "sfx/horn:horn.wav", "hold": "movies/play_pause",
              "double_tap": "movies/next", "triple_tap": "movies/prev"},
    "1:yellow": "movies/pause"
  }
}
```
- Mapping key = `"<buzzer 1-4>:<color>"` — this is the **physical**
  HID-decoded key, always, regardless of any GUI-level "controller"
  reassignment (see below).
- Binding value: bare string (shorthand for `{"tap": ...}`) or dict with
  keys `tap`/`hold`/`double_tap`/`triple_tap` (see `BINDING_KEYS` in
  code). Actions: `play play_pause stop next prev seek+10s seek-10s
  vol_up vol_down mute fullscreen shuffle loop focus identify
  horn:<path-or-folder>`, plus global `all_pause`/`all_stop`.

## PressTracker semantics (the trickiest part, get this right)

- Tap-only button: fires "tap" immediately on release, zero latency.
- Hold: fires the moment held time crosses `hold_ms` **while still
  held**, not on release; releasing early fires tap instead.
- Multi-tap (double/triple): only counted for a button that actually
  **has** a `double_tap` or `triple_tap` binding (`has_double`/
  `has_triple` callbacks) — this is deliberate, so a genuinely unmapped
  tap never gets delayed waiting for a second press that will never
  matter. 3 rapid taps with only a double binding (no triple) resolve as
  double_tap, not discarded. Resolution timer restarts are guarded: a
  new `press()` cancels any pending resolution timer so a stale
  lower-count event can't fire while the next tap is still physically
  held down slightly longer than the window.
- **GUI-side gotcha already hit once:** the Live-buttons visual "2"/"3"
  indicator must NOT reuse the engine's action-dispatch tracker, because
  that tracker only counts multi-taps for buttons with real bindings —
  so a button with only `tap` configured would never show "2"/"3" even
  when physically double/triple-tapped. Fix: GUI runs its own **second,
  independent** `PressTracker` (`self._live_tracker` in
  `buzz2vlc_gui.py`) with `has_double`/`has_triple` always `True`, fed
  from the same raw press/release stream (`on_button_change`), so the
  display reflects real physical presses regardless of what's bound.

## GUI structure notes

- **Menu bar**: View (font size controls) and Help (Manual — opens a
  `Toplevel` with a read-only `scrolledtext.ScrolledText` showing
  `README.md` read fresh from disk next to `buzz2vlc_gui.py`; About —
  a `messagebox.showinfo` with a short description plus contact info
  appended from `CONTACT.txt`, see File layout). Quit button
  (bottom-right of the VLC instances tab) calls the same `_on_close` the
  window's own X button uses, so it stops the listener and
  `_live_tracker` cleanly rather than just killing the process.
- **Window tiling on launch** (`App._tile_geometry`, called from
  `_launch`): grid slot from the instance's position in
  `cfg["vlc_instances"]` order, computed within
  `core.usable_screen_area()` (taskbar-excluded work area, any edge) —
  falls back to the full Tk screen size only when that returns `None`
  (non-Windows) — inset by `core.cm_to_px(2.0)` on all four sides. Actual
  positioning still goes through the pre-existing
  `core.wait_and_position_window` (polls by PID, moves via
  `SetWindowPos`/`wmctrl`).
- **VLC instances tab**: Treeview with `show="tree headings"` — the
  `#0` tree column holds a "launched" traffic-light icon (colored
  `PhotoImage` circle drawn pixel-by-pixel: gray=not launched,
  amber=starting/unreachable, green=running, red=crashed). **Do not use
  color-emoji characters** (🟢🔴 etc.) for this — verified live that Tk on
  Windows renders them as plain hollow outlines (GDI text rendering
  doesn't support color emoji glyphs); a `PhotoImage` circle is the
  reliable cross-platform approach. Double-click a row → opens the same
  edit dialog as the Edit button. Edit dialog has a Browse... button for
  the playlist field. Auto-refreshes launch status every 2.5s. Columns
  auto-size to content (`_autosize_instances_columns`, measures header +
  every cell via `tkfont.Font.measure`, capped at 420px) instead of a
  fixed guess-width — re-runs on every `_refresh_instances_tree` call and
  whenever `FontScaler` changes size (wired via `FontScaler.on_change`).
  **Every `.column()` call must also pass `stretch=False`** — ttk's
  default column behavior redistributes any extra space beyond the
  configured widths proportionally across columns the moment the window
  is wider than their sum, which silently overrides content-based
  autosizing as soon as the user resizes the window at all. Missed this
  the first time the autosize feature was added; verified live (widened
  the window, confirmed via screenshot that columns stayed their
  content-fit width with blank space to the right, instead of stretching).
- **Launching tiles windows instead of stacking them.** Every launch
  (whether via Launch selected or Launch ALL, both go through `_launch`)
  computes a grid slot from the instance's position in
  `cfg["vlc_instances"]` order (`App._tile_geometry`: columns =
  `ceil(sqrt(total))`, rows = `ceil(total/columns)`, slot size =
  screen/(cols,rows)) and spawns a background thread calling
  `core.wait_and_position_window(pid, x, y, w, h)`, which polls by PID
  (window creation lags behind process start) and moves it via
  `core.position_window` → `SetWindowPos`
  (`SWP_NOZORDER|SWP_NOACTIVATE`, Windows) / `wmctrl -r <id> -e
  0,x,y,w,h` (Linux). Deliberately does NOT use `SetForegroundWindow` --
  moving/resizing doesn't need focus, so this is immune to the
  foreground-lock failures documented in bug #4 below. Verified live:
  launched 4 real VLC instances, confirmed via `GetWindowRect` (not just
  visually) that they land in 4 distinct, non-overlapping screen
  quadrants.
- **Button mappings tab**: grouped into 4 "quadrants" (one per physical
  buzzer slot 1-4). Each quadrant has **one** controller dropdown
  (values "1"-"4") spanning its 5 color rows via `rowspan` — NOT a
  dropdown repeated on every row (tried that first, user correctly
  called it out as redundant UI for what's functionally one selection).
  Reassigning a quadrant's controller number **swaps** it with whichever
  quadrant currently holds that number (enforced via a `trace_add`
  callback on each quadrant's `StringVar`) — since there are only 4
  physical slots, a bijection is always maintained, no duplicates
  possible. This is the mechanism for "swap controllers on the fly if a
  battery dies without redoing all the button functions" — the row's
  tap/hold/double/triple/instance/sound settings stay put; only which
  physical buzzer number they listen on changes. Color (button) stays
  fixed per row — only the buzzer number is reassignable, not the color.
  Also has a "Reconnect receiver" button (forces `Engine.request_reconnect()`,
  an immediate rescan instead of waiting the poll interval — for when
  you've physically swapped the receiver dongle itself). Columns:
  controller, button (color), instance, tap, hold, double-tap,
  triple-tap — **no sound/horn column**. There was one; it was removed
  (see bug #9) because `"horn"` was never added to the tap/hold/etc.
  dropdown's action list, making the sound-file field permanently
  unreachable for any new mapping. `horn:<path>` is now config-file-only
  — set it by hand-editing the JSON. The GUI still correctly *preserves*
  an existing horn binding on save (see bug #9 for the mechanism), it
  just can't create or edit one.
- **Live buttons tab**: 4x5 grid of colored circles, columns in
  **physical visual layout order** `DISPLAY_ORDER = ["red","blue","orange","green","yellow"]`
  (independent of `BUTTON_ORDER_DEFAULT`, which is the HID *bit* decode
  order — don't conflate them). Red drawn larger (`radius_big=30` vs
  `radius_normal=20`) since it's the big button on the real hardware.
  Circle stays lit exactly as long as the button is physically held (no
  fixed-duration flash — that was tried first and is wrong for long
  holds); shows "2"/"3" text overlay briefly on double/triple-tap (see
  PressTracker note above for why this needs its own tracker).

## Real bugs found via live testing (not from reading code — from actually running it)

These are the ones worth knowing about because they contradict
reasonable first-draft assumptions:

1. **Bare Windows path as VLC's trailing CLI arg doesn't reliably load.**
   `vlc.exe ... -- C:\Media\movies.xspf` launched with an *empty*
   playlist, no error. Fix: convert to a proper `file:///` URI first
   (`_playlist_to_target`); pass URLs (`http://`, `rtsp://`, existing
   `file://`) through unchanged.
2. **`--no-playlist-autostart` breaks playlist FILE expansion; solved in
   two iterations.** First attempt: `--no-playlist-autostart` stops
   auto-play, but *also* suppresses VLC's parsing of a `.m3u`/`.xspf`
   into its individual tracks — expansion only happens as part of
   opening item 1 for autostart, so every track collapsed into one
   unparsed entry. Fix v1: removed the flag; let autostart run normally,
   then a background thread (`_stop_after_autoplay`, since removed —
   see v2) raced to catch `state == "playing"` via HTTP polling and stop
   it — worked, but left a real window where the media actually played
   (audible/visible blip) before being caught, and depended on a
   polling race. **Fix v2 (current): `--start-paused`.** Verified live:
   `state` stays `"paused"` from the very first poll after launch,
   *never* transitioning through `"playing"` at all — no blip, no race,
   no background thread needed, and the full track list still expands
   correctly. Strictly better than v1 in every respect; `launch_instance`
   is back to a simple `subprocess.Popen` + return, no extra step.
3. **`--meta-title` renames every playlist item, not just the window.**
   This is *documented* VLC behavior ("replace filename in playlist"),
   not a bug — but it meant every track in a multi-track playlist showed
   `buzz2vlc: <instance>` instead of its real title. Fix: dropped the
   flag entirely; window ID now relies solely on PID matching (always
   known — `launch_instance`'s caller registers it), which never needed
   the title anyway.
4. **`SetForegroundWindow`'s whole documented fallback chain can fail
   outright** when the calling process isn't itself interactive
   (verified: plain call, `AttachThreadInput`, and even
   `SystemParametersInfoW` for the foreground-lock-timeout trick all
   returned failure in one real test). `BringWindowToTop` still reliably
   changes Z-order in that situation — added as a 4th fallback.
5. **Title-based window matching breaks once a player goes idle** — VLC
   reverts the window title to generic "VLC media player" when nothing
   is actively playing, confirmed live. This is why PID-based matching
   is primary, not a fallback.
6. **Windows batch files need CRLF.** LF-only line endings broke
   `cmd.exe`'s parenthesized `if` blocks with an opaque
   `- was unexpected at this time.` — switched to `goto` branching.
7. **A taken HTTP port doesn't stop VLC from starting** — it silently
   drops the web interface and keeps playing, leaving a player reported
   only as "unreachable". `launch_instance()` checks the port first and
   raises a clear error.
8. **Volume clamp trap (from the *original* Claude.ai build, carried
   forward as a fix, not rediscovered by us):** a clamp can be computed
   correctly and then not actually applied if the code sends the
   unclamped value by mistake — worth a regression test
   (`test_volume_clamp`) precisely because this class of bug produces no
   error, just silently-wrong behavior.
9. **A dropdown-driven action editor silently orphaned a feature.** The
   mapping table's sound/horn column let you type a sound file path, but
   `"horn"` was never added to `ACTIONS` (the tap/hold/double/triple
   dropdown's value list) and those dropboxes are `state="readonly"` —
   so there was no way to ever *select* horn as an action for a new
   mapping; the field only ever showed a value if the JSON had been
   hand-edited. User caught this from the outside ("doesn't seem to
   serve a purpose") without knowing the mechanism; asked whether to fix
   the dropdown or remove the column — chose remove. When removing it,
   the naive fix (`if not binding: continue` when saving a row) would
   have **silently deleted** any pre-existing horn-only binding the next
   time someone opened the GUI and hit Save without touching that row
   (a legitimate risk: e.g. the big red button's tap is very plausibly
   *only* a horn, no hold/double/triple set). Fix: changed the skip
   condition to `if not binding and not existing: continue` — safe
   because the merge (`{**existing, **binding}`) already falls back to
   the previously-saved value for any field the widgets don't explicitly
   set (true for every field, not just horn — field-level "clear by
   picking blank" was never actually functional in this editor, so this
   change doesn't remove a capability, it just stops an untouched row
   from being dropped from the saved config entirely). Verified live: a
   horn-only row and a horn+hold row both round-trip through
   `_collect_mappings()` unchanged when nothing is touched.
10. **A background worker thread calling `self.after(...)` after the app
    is closed raises an unhandled `RuntimeError`.** Found in a "debug 5x"
    concurrency pass, not from a user report: `_refresh_status`'s (and
    `_shutdown_all`'s) worker threads can be mid-flight -- e.g. blocked
    in an HTTP call with a multi-second timeout -- when `_on_close()`
    destroys the Tk root. The `after`/`after_cancel` cleanup in
    `_on_close` only cancels *already-scheduled* callbacks; it can't stop
    a worker thread from trying to schedule a *new* one moments later.
    Verified live: closing the app 0.1s after starting a status refresh
    against an unreachable host printed a full traceback from a
    background thread. Fix: `App._safe_after(ms, fn)` wraps `self.after`
    in `try/except (RuntimeError, tk.TclError)`; every background-thread
    call site (`_refresh_status`, `_shutdown_all`) uses it instead of
    calling `self.after` directly. `wait_and_position_window` doesn't
    need this guard -- pure `core.py` logic, no Tk calls at all.
11. **Windows taskbar isn't reliably at the bottom.** Verified live on
    the actual dev machine: its taskbar is docked to the **left** edge.
    A "subtract N px from screen height" approach to avoid covering it
    would have failed outright here. `SPI_GETWORKAREA`
    (`core.usable_screen_area`) returns the true usable region regardless
    of which edge the taskbar is on, or whether it's auto-hidden — use
    that, never a guessed inset.
12. **Dead code found by a real "debug 5x" static pass, not by
    inspection:** `from urllib.parse import quote` was imported and never
    used (removed); `window_control_available()` was defined and never
    called anywhere (wired into `--diagnose`'s output instead of just
    deleting it, since it's a genuinely useful one-liner for that
    report). Caught via an actual `ast`-based scan
    (`ast.walk` + count occurrences), not by reading through the file.
13. **`--ignore-config` (added to stop VLC's saved window geometry from
    fighting our own tiling) makes VLC show its first-run "Privacy and
    Network Access Policy" dialog on *every* launch**, since it also
    means VLC never remembers having been dismissed before. That dialog
    -- not a tiling-math bug -- was what actually looked like "windows
    on top of each other." Fixed with `--no-qt-privacy-ask` alongside
    `--ignore-config`, found via `vlc --longhelp --advanced | grep -i
    privacy`. General lesson: a flag that fixes one cross-instance-state
    problem can trade it for a different one; verify the *combination*
    live, not just the one flag in isolation.
14. **Launching instances incrementally leaves earlier ones at a stale
    grid slot.** `_tile_geometry`'s grid size depends on the *current*
    instance count; an instance launched when there was only 1 (full
    screen) is never repositioned when a 2nd and 3rd are added later --
    it just keeps occupying the whole area, overlapping the new ones.
    This was the *other* real contributor to the "not tiled" report (on
    top of bug #13). Fixed by having both `_launch` (on a new launch)
    and `_remove_instance` (removing a configured instance shrinks the
    grid too) reposition every other still-running instance to its
    updated slot, not just the one just launched/removed. (Shutting an
    instance down does *not* need this -- the grid is sized by
    *configured* instance count, not how many are currently running, so
    stopping one doesn't change anyone else's slot.)
15. **Renaming an instance doesn't cascade into `mappings`.** A mapping's
    action is stored as plain text (`"<instance>/<verb>"`), not a
    reference -- renaming only touched `vlc_instances`, so every row
    mapped to that instance kept showing the old, no-longer-existent
    name as its selected value (confirmed live: not even present in the
    dropdown's own valid-choices list -- a stale value stuck in a
    readonly combobox). Reported by the user as "old entries" after they
    'd renamed several instances. Fix: `App._rename_instance_in_mappings`
    rewrites every affected action string in `cfg["mappings"]` and
    updates any already-built row's `instance` StringVar to match,
    called from `_instance_dialog`'s save() right after the rename is
    applied to `vlc_instances`. Verified live: correct immediately (no
    restart needed) and correct again after a full rebuild (simulating
    a reload from saved config).
16. **"debug 3x" pass (2026-09-01) on the rename-cascade fix (#15) and the
    reposition-on-launch/remove fix (#14): no new bugs found**, all 3
    passes came back clean. Pass 1 (`ast`-based static scan, same method
    as #12): the same 5 `buzz2vlc.py` functions flagged as "possibly
    unused" by a same-file-only scan (`cm_to_px`, `usable_screen_area`,
    `wait_and_position_window`, `close_instance`, `request_reconnect`)
    were re-confirmed as false positives — each has exactly 1 definition
    + 1 real call site in `buzz2vlc_gui.py`, invisible to a same-file
    scan. `VLCRemote.play_item()` remains the one genuinely-unused
    method, kept deliberately (see #12's sibling note) as a documented,
    correct public API on a general-purpose HTTP-control class. Pass 2
    (adversarial): fed `_rename_instance_in_mappings` a same-prefix
    collision (`movies` vs `movies2` — confirmed it does NOT
    false-positive-match), a rename with no matching mappings, and mixed
    dict bindings with both instance-scoped and global actions in the
    same binding (`{tap: "x/next", hold: "all_stop"}` — confirmed only
    the instance-scoped slot gets rewritten). Also fed `_tile_geometry`
    a name absent from `vlc_instances` and a config with 0 instances;
    both silently default rather than raising (`index = ... if name in
    names else 0`) — traced every real call site and confirmed none can
    ever pass such a name (config and `_instance_procs` are kept in
    sync synchronously on rename/remove before any reposition loop
    runs), so this is unreachable defensive code, not a live bug. Pass 3
    (integration, with `core.launch_instance`/`position_window` mocked
    to avoid needing a real VLC binary): launched two instances, renamed
    one *while its process was tracked as running*, then removed the
    other while a mapping still referenced it. Confirmed removing an
    instance leaves its mapping bindings dangling (unlike renaming,
    removal does not cascade) — but this degrades safely: `Engine.
    run_action()` already logs `"unknown VLC instance %r in action %r"`
    and returns for any action naming an instance not in `self.remotes`
    (buzz2vlc.py, in `run_action`), so a dangling mapped button is inert
    and logged, never a crash. Not treated as a bug to fix — an
    intentional-enough tradeoff (mapping isn't auto-pruned on remove) is
    consistent with letting the user re-add a same-named instance
    later. `--selftest` re-run afterward: 12/12 still passing, no
    regressions. Per standing convention (see below), the code was
    archived to a zip before this pass began even though it turned out
    no changes were needed.
17. **`load_config()` silently discarded any config file saved with a
    UTF-8 BOM, reverting to defaults with no obvious symptom.** Found
    live while building a demo config for README screenshots: PowerShell's
    `Out-File -Encoding utf8` (and Notepad's default "UTF-8" save) both
    write a leading BOM; `raw_bytes.decode("utf-8")` rejects that BOM as
    invalid UTF-8, hits the `UnicodeDecodeError` branch, and falls back to
    `default_config()` — the GUI then shows the built-in sample instances
    instead of the user's real config, with the mismatch logged only as
    an easy-to-miss warning string, not a visible error. Fixed by
    decoding with `"utf-8-sig"` instead of `"utf-8"` (strips a BOM if
    present, no-op otherwise — safe for files buzz2vlc's own
    `save_config()` writes, which never include one). Added
    `test_load_config_tolerates_utf8_bom` to the `--selftest` suite so
    this can't silently regress. Verified live both ways: reproduced the
    original failure with a real BOM'd file before the fix, then
    confirmed the same file loads cleanly after it. `--selftest`: 13/13
    passing.

## Known unverified items (flag if asked, don't claim otherwise)

- `button_byte` / `button_order` defaults are configurable but genuinely
  unverified for any specific physical unit — two community sources
  (hid-sony driver vs. pybuzzers) disagree on colour order. Run
  `python buzz2vlc.py --diagnose` on real hardware before trusting them.
- LED write behavior verified structurally (8-byte report with leading
  report-ID byte, matches kernel driver comment) but not against real
  hardware in this session — no physical Buzz controller has been
  available during development; testing has all been VLC-side.

## Documentation pass

Every class and every public (non-`_`) function in `buzz2vlc.py`, plus
the non-trivial methods in `buzz2vlc_gui.py`, now has a real
`"""docstring"""` (not just a preceding `#` comment) -- checked via an
`ast.get_docstring()` sweep, not by eyeballing. Keep this up: when adding
a new public function/class, give it a one-line docstring even if a
`#` comment above it already explains the deeper "why" -- they serve
different purposes (docstring = `help()`/IDE-tooltip quick reference,
comment = rationale for the non-obvious parts).

## Activity log (bottom of the GUI)

Every `App._log()` line is timestamped (`[HH:MM:SS]`). A **Verbose
controller activity** checkbox (`self.verbose_controller_var`, on by
default) gates logging of every raw press/release
(`_button_queue`), double/triple-tap (`_logical_queue`), and resolved
action (`_action_queue`, fed by a new `Engine.on_action` hook that fires
in `run_action()` right before dispatch) -- all drained and logged from
`_poll_ui` on the main thread, same queue-handoff pattern as everything
else fed from the HID/Dispatcher threads. **Save log...**
(`filedialog.asksaveasfilename`) writes the full `Text` widget contents
to a `.log`/`.txt` file; **Clear** empties it.

## Working conventions used throughout this project (apply if rebuilding)

- Verify every claim about VLC/Windows behavior by actually launching
  real VLC and querying its HTTP status/playlist XML — several
  "obviously correct" flags turned out to have undocumented side
  effects only visible by testing.
- After any code change, run `python buzz2vlc.py --selftest` and quote
  its tail output, not just "tests should pass."
- Screenshot verification for GUI changes: launch via a background
  process, raise its window by PID with `core.raise_window`, screenshot,
  read the image back — don't just claim a layout "looks right."
- Clean up test artifacts (screenshots, temp scripts, stray launched VLC
  processes, test config files) after verification; never leave the
  user's real `~/.buzz2vlc.json` clobbered by test data — back it up
  before testing against it if it already has real content.
- **Test scripts that drive `App()` (tkinter) MUST call `app.mainloop()`,
  never a manual `for _: app.update(); time.sleep(...)` loop.** Hit this
  twice: background-thread work that calls `self.after(...)` (status
  refresh, window positioning, etc.) raises `RuntimeError: main thread
  is not in main loop` under the manual-loop pattern, because Tkinter's
  threading support requires the real event loop running, not just
  periodic `update()` calls. Correct pattern: schedule test steps with
  chained `app.after(ms, step_fn)` calls, then call `app.mainloop()` for
  real; drive/verify from a *separate* process (screenshot + raise by
  PID, or query real VLC's HTTP API) rather than trying to inspect state
  from inside the same script after mainloop returns.
- **Archive the code before making further changes**, at the user's
  explicit request after the incident below: zip
  `C:\ADM\_claude\buzz2vlc\*` to
  `C:\ADM\_claude\archives\buzz2vlc-<timestamp>.zip` before starting a
  new round of edits, not just at the end.
- **Never delete or overwrite `~/.buzz2vlc.json` (or any file that could
  hold the user's real, unsaved-elsewhere data) without copying it aside
  first -- no exceptions, not even "it's probably just test data."** A
  routine `rm -f ~/.buzz2vlc.json` cleanup command, run out of habit
  after many earlier rounds of safely deleting test-only versions of
  this file, ended up deleting the user's real saved config (4
  instances, full mapping table) with no backup and no recoverable copy
  (command-line `rm` bypasses the Recycle Bin on Windows; no shadow
  copies or restore points were available either). The caution already
  written into the "clean up test artifacts" bullet above wasn't enough
  on its own -- it needs to be an unconditional rule with no judgment
  call involved, because the judgment call is exactly what failed here.
