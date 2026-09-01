# VLC Bookmark Studio
## Complete Software Design & Engineering Specification

**Document status:** Coding baseline
**Revision:** 1.0
**Target OS:** Windows 10 and Windows 11
**Primary host application:** VLC Media Player 3.x, forward-compatible design for VLC 4.x
**Primary development language:** Python
**UI framework:** PySide6 / Qt 6
**VLC integration:** VLC Lua interface + localhost HTTP control bridge
**Database:** SQLite
**Media analysis:** FFmpeg + NumPy
**Packaging:** PyInstaller + Windows installer

---

# 1. Product Definition

VLC Bookmark Studio is a visual bookmarking, segment-selection, navigation and looping system for VLC Media Player.

The application shall allow the user to visually inspect the waveform of the currently selected VLC media, paint regions of that waveform, convert those regions into named bookmarks, loop them, edit them, organize them, and persist them.

Bookmark Studio must understand the **currently loaded VLC playlist** and automatically associate bookmarks with:

**Playlist + Song**

rather than merely:

**Song**

This means that the same media file may have different bookmark sets when it occurs in different playlists.

Example:

```text
Playlist: Guitar Practice
└── Hotel California.mp3
    ├── Intro Solo       00:00.000 → 00:52.300
    ├── Chorus           01:17.400 → 01:48.100
    └── Main Solo        04:19.220 → 05:41.800

Playlist: Party Playlist
└── Hotel California.mp3
    └── Fade Out         05:57.000 → 06:20.000
```

Bookmark Studio shall automatically switch bookmark context when VLC changes playlist or current song.

---

# 2. Product Architecture

The product consists of two cooperating components distributed as one application.

```text
┌──────────────────────────────────────────────┐
│ VLC Bookmark Studio                         │
│ Python / PySide6                            │
│                                              │
│  Playlist Manager                           │
│  Playlist Recognition                       │
│  Media Resolver                             │
│  Waveform Editor                            │
│  Bookmark Manager                           │
│  Loop Controller                            │
│  SQLite                                     │
│  Project Import / Export                    │
└───────────────────┬──────────────────────────┘
                    │
                    │ HTTP / JSON
                    │ localhost only
                    ▼
┌──────────────────────────────────────────────┐
│ bookmarkstudio.lua                          │
│ VLC Lua Interface                           │
│                                              │
│ VLC playlist access                         │
│ VLC media state                             │
│ VLC microsecond playback time               │
│ exact seek requests                         │
│ transport control                           │
│ metadata                                    │
└───────────────────┬──────────────────────────┘
                    │
                    ▼
              VLC Media Player
```

The Python application owns essentially all business logic.

The VLC Lua component is intentionally thin.

This separation is required because VLC's Lua dialog API is adequate for basic controls but is not suitable for a sophisticated waveform/timeline editor. VLC's documented Lua UI consists primarily of buttons, labels, text fields, checkboxes, dropdowns, lists and images.

---

# 3. Core Technology Stack

Recommended stack:

```text
Python 3.13 x64
PySide6 / Qt 6
NumPy
SQLite via Python sqlite3
FFmpeg
pytest
pytest-qt
ruff
mypy
PyInstaller
```

Python 3.13 is recommended initially because it provides a mature modern runtime while remaining well supported by the planned tooling.

PySide6 is the official Python binding for Qt.

SQLite is available through Python's standard `sqlite3` module and requires no database server.

PyInstaller can package Python applications and their dependencies so end users do not require a separate Python installation.

---

# 4. Why PySide6

The visual editor shall use Qt's Graphics View framework.

Primary classes:

```python
QGraphicsView
QGraphicsScene
QGraphicsItem
QGraphicsObject
QPainter
QTransform
```

`QGraphicsView` provides scrolling, coordinate transformations, zooming, event translation and interactive graphical objects.

Recommended custom hierarchy:

```text
WaveformView : QGraphicsView
    │
    └── WaveformScene : QGraphicsScene
           │
           ├── WaveformItem
           ├── TimeRulerItem
           ├── SelectionItem
           ├── PlayheadItem
           ├── BookmarkRegionItem*
           ├── BookmarkPointItem*
           ├── BookmarkHandleItem*
           └── SnapGuideItem
```

Do **not** create one QGraphicsItem per waveform sample.

The waveform itself should be one custom-rendered `WaveformItem` using `QPainter`.

Bookmarks may be individual interactive graphical objects.

---

# 5. Primary UX References

The interaction model should adopt proven concepts from audio applications.

Audacity demonstrates:

* drag-to-select waveform regions;
* exact start/end time controls;
* point and region labels;
* marking left and right selection boundaries during playback;
* Ctrl+B to label a selected region;
* snapping;
* next/previous label navigation.

Adobe Audition distinguishes explicitly between point markers and range markers, and allows marker boundaries to be dragged.

Sonic Visualiser demonstrates editable timed regions, labels, direct dragging and snapping selections to region boundaries.

Peaks.js is a strong reference for a combined overview waveform, zoomable waveform, points, segments, dragging and external-player integration.

Bookmark Studio shall combine these patterns while remaining a VLC-oriented tool rather than an audio editor.

---

# 6. Fundamental UX Rule

The primary interaction rule is:

> Anything visible on the timeline should be directly manipulable.

Therefore:

```text
Click empty waveform
→ seek VLC

Drag empty waveform
→ create temporary range selection

Double-click empty waveform
→ optional point bookmark

Click bookmark
→ select bookmark

Double-click bookmark
→ play bookmark

Drag bookmark body
→ move bookmark

Drag left handle
→ modify start

Drag right handle
→ modify end

Right-click bookmark
→ context menu

Ctrl + mouse wheel
→ zoom

Mouse wheel / Shift + wheel
→ scroll

[
→ capture current playback position as Start

]
→ capture current playback position as End
```

---

# 7. Main Window

Recommended layout:

```text
┌───────────────────────────────────────────────────────────────────────────┐
│ File  Edit  View  Bookmark  Playback  Playlist  Tools  Help              │
├──────────────────┬────────────────────────────────────────────────────────┤
│ VLC PLAYLIST     │ CURRENT MEDIA                                          │
│                  │ Hotel California — Eagles                              │
│ ▶ Hotel Calif. 4 │ Playlist: Guitar Practice                              │
│   Life in...   2 │                                                        │
│   Desperado    3 │                                                        │
│   Take It...   0 ├────────────────────────────────────────────────────────┤
│                  │ 0:40         1:00         1:20         1:40            │
│ Filter...        │ │            │            │            │               │
│                  │                                                        │
│                  │ ▂▃▅████▅▂▃██████▆▂▃████████▅▂▂██████                  │
│                  │                                                        │
│                  │          [====== CHORUS ======]                        │
│                  │                         │                              │
│                  │                       PLAYHEAD                         │
├──────────────────┴────────────────────────────────────────────────────────┤
│ OVERVIEW                                                                  │
│ 0:00 ───────────────[████ visible viewport ████]─────────────── 6:31     │
├───────────────────────────────────────────┬───────────────────────────────┤
│ BOOKMARKS                                 │ BOOKMARK INSPECTOR            │
│                                           │                               │
│ Intro       00:00.000 → 00:52.300         │ Name   Chorus                 │
│ Chorus      01:17.400 → 01:48.100   ∞     │ Start  01:17.400             │
│ Solo        04:19.220 → 05:41.800   ×5    │ End    01:48.100             │
│                                           │ Loop   ☑                     │
│                                           │ Repeat Forever                │
├───────────────────────────────────────────┴───────────────────────────────┤
│ B◀    ◀ Track   -5s    ■    ▶/Ⅱ    +5s   Track ▶    ▶B                  │
│                    01:23.381 / 06:31.220                                  │
└───────────────────────────────────────────────────────────────────────────┘
```

Panels shall use `QSplitter` so users can resize them.

Window geometry and splitter positions shall be stored using `QSettings`. Qt provides platform-independent persistent application settings through `QSettings`.

---

# 8. Active Context Indicator

The application shall always visibly indicate:

```text
Playlist:
Guitar Practice

Track:
Hotel California

Bookmark context:
Playlist-specific

Bookmarks:
4
```

A condensed breadcrumb may be used:

```text
Guitar Practice › Hotel California › 4 bookmarks
```

This prevents accidental editing of the wrong playlist's bookmark collection.

---

# 9. Playlist-Level Bookmark Context

The bookmark scope is:

```text
Playlist
    +
Media
```

Therefore a bookmark's effective key contains:

```text
playlist_id
media_id
bookmark_id
```

Bookmarks must never be linked to playlist ordinal position alone.

Incorrect:

```text
Playlist item #4
→ bookmarks
```

Correct:

```text
Playlist UUID
+
Media UUID
→ bookmarks
```

Reordering the playlist therefore does not move bookmarks to another song.

---

# 10. Playlist Detection

Bookmark Studio shall continuously monitor VLC's active playlist.

The VLC Lua API exposes playlist access, current item identification, transport and flat/tree playlist retrieval.

Playlist recognition must work even if VLC does not expose the original playlist filename.

Recognition therefore uses several signals.

Priority:

```text
1. Explicit known playlist source URI, if available
2. Existing active Bookmark Studio context
3. Exact ordered media identity signature
4. Previously stored playlist signature alias
5. High-confidence similarity match
6. User association
7. New ad-hoc playlist context
```

---

# 11. Playlist Signature

A playlist signature shall be calculated from its ordered media identities.

Concept:

```python
ordered_media_ids = [
    media_id_1,
    media_id_2,
    media_id_3,
    ...
]
```

Then:

```python
strict_signature = sha256(
    b"\0".join(media_id.bytes for media_id in ordered_media_ids)
).hexdigest()
```

The signature must preserve:

* order;
* duplicate entries.

---

# 12. Playlist Similarity Matching

A playlist can evolve.

Example:

```text
Previous:
A
B
C
D

Current:
A
B
X
C
D
```

This must not automatically become a completely unrelated bookmark project.

The matcher shall calculate:

```text
Media overlap score
+
Ordering similarity score
```

Recommended implementation:

```python
score =
    0.70 * multiset_jaccard +
    0.30 * adjacency_similarity
```

Where multiset Jaccard respects repeated media.

Suggested decisions:

```text
Exact source URI
→ automatic match

Exact strict signature
→ automatic match

Known signature alias
→ automatic match

Similarity >= 0.95
AND unique candidate margin >= 0.10
→ automatic match

0.75 – 0.95
→ ask user

< 0.75
→ create new context
```

Thresholds shall be configurable internally and covered by tests.

---

# 13. Playlist Mutation During Current Session

If Bookmark Studio is already tracking Playlist A and VLC adds, deletes or reorders items, Bookmark Studio shall treat this as a mutation of the current playlist context rather than a new playlist.

Flow:

```text
Known active context
       ↓
playlist snapshot changes
       ↓
update PlaylistItems
       ↓
calculate new signature
       ↓
store signature as alias
       ↓
retain playlist UUID
```

This is critical.

Otherwise a single playlist edit could cause Bookmark Studio to switch projects unexpectedly.

---

# 14. Unsaved VLC Playlists

Users may create playlists manually in VLC without loading an M3U/XSPF file.

Bookmark Studio shall create an ad-hoc context:

```text
Unsaved VLC Playlist
01 September 2026 14:44
```

The user may rename it.

This context works exactly like a saved playlist.

If the same ordered set later reappears, Bookmark Studio may recognize it by signature.

---

# 15. Duplicate Media Within One Playlist

Example:

```text
1. Song A
2. Song B
3. Song A
```

Default behavior:

```text
Playlist A + Song A
```

has one shared bookmark set.

Both Song A entries therefore expose the same bookmarks.

Architecture must retain playlist occurrence records so entry-specific bookmarks could be introduced later.

---

# 16. Bookmark Scope Modes

Bookmark model shall support:

```text
PLAYLIST_MEDIA
GLOBAL_MEDIA
```

Default:

```text
PLAYLIST_MEDIA
```

Optional global bookmarks permit:

> Show this bookmark whenever this media appears in any playlist.

UI filter:

```text
Bookmarks:
● This playlist
○ Global
○ Combined
```

The default remains playlist-specific as required.

---

# 17. Current-Song Transition

Whenever VLC changes current media:

```text
VLC current playlist ID changes
          ↓
PlaybackMonitor detects change
          ↓
resolve current VLC item
          ↓
MediaResolver.resolve()
          ↓
active_playlist_id + media_id
          ↓
BookmarkRepository.list()
          ↓
load waveform
          ↓
render bookmark regions
```

If waveform data is cached, this process should visually complete in less than approximately 250 ms.

---

# 18. VLC Integration Architecture

A custom VLC Lua **interface**, not a conventional Lua extension dialog, is the recommended integration component.

Filename:

```text
bookmarkstudio.lua
```

User-local VLC Lua interfaces are supported by VLC. On Windows, VLC searches the user's configuration tree under `lua/intf/` before global Lua locations.

The interface can be started using the `luaintf` mechanism. VLC's own examples use:

```text
-I luaintf --lua-intf <script>
```

and additional interfaces can be configured through `extraintf=luaintf`.

---

# 19. Lua HTTP Bridge

The bridge should use VLC's own `vlc.httpd()` API.

VLC documents:

```lua
local h = vlc.httpd()
h:handler(
    url,
    user,
    password,
    callback,
    data
)
```

and supports authentication directly on each handler.

This is preferable to implementing an independent socket/event framework.

It also avoids relying on VLC Lua `net.poll`, `net.read`, and `net.write`, which are explicitly unavailable on Windows.

---

# 20. Bridge Binding

The bridge shall listen only on:

```text
127.0.0.1
```

Recommended default port:

```text
43119
```

The actual port may be configurable.

Never bind intentionally to:

```text
0.0.0.0
```

or another external network interface.

---

# 21. Bridge Authentication

At first startup Bookmark Studio generates:

```python
secrets.token_urlsafe(32)
```

The token is stored in the user's Bookmark Studio settings.

Use:

```text
HTTP Basic Authentication

username:
bookmarkstudio

password:
<random generated token>
```

The token shall never appear in logs.

---

# 22. Bridge API

Namespace:

```text
/bookmarkstudio/v1/
```

Required endpoints:

```text
GET /bookmarkstudio/v1/health
GET /bookmarkstudio/v1/status
GET /bookmarkstudio/v1/playlist
GET /bookmarkstudio/v1/media
GET /bookmarkstudio/v1/control
GET /bookmarkstudio/v1/seek
GET /bookmarkstudio/v1/rate
```

All responses:

```text
Content-Type: application/json
Cache-Control: no-store
```

---

# 23. Health Endpoint

Request:

```text
GET /bookmarkstudio/v1/health
```

Response:

```json
{
  "ok": true,
  "protocol_version": 1,
  "vlc_version": "3.0.23",
  "bridge_version": "1.0.0"
}
```

VLC Lua exposes `misc.version()` from interface scripts.

---

# 24. Status Endpoint

Response:

```json
{
  "state": "playing",
  "time_us": 83221450,
  "position": 0.21124,
  "rate": 1.0,
  "current_playlist_item_id": 17,
  "duration_us": 391220000,
  "media_uri": "file:///C:/Music/song.flac"
}
```

VLC Lua provides playback time in **microseconds**, playback position, rate, current item URI and duration.

This is the preferred timing source.

---

# 25. Playlist Endpoint

Response:

```json
{
  "current_id": 17,
  "items": [
    {
      "vlc_id": 14,
      "uri": "file:///C:/Music/song1.flac",
      "name": "Song 1",
      "duration_s": 244.2
    },
    {
      "vlc_id": 17,
      "uri": "file:///C:/Music/song2.flac",
      "name": "Song 2",
      "duration_s": 212.6
    }
  ]
}
```

Implementation:

```lua
local playlist = vlc.playlist.get("playlist", false)
```

Each returned entry contains an ID and media information; VLC also exposes the current playlist item ID.

---

# 26. Transport Control Endpoint

Example:

```text
GET /bookmarkstudio/v1/control?command=play
GET /bookmarkstudio/v1/control?command=pause
GET /bookmarkstudio/v1/control?command=stop
GET /bookmarkstudio/v1/control?command=next
GET /bookmarkstudio/v1/control?command=previous
GET /bookmarkstudio/v1/control?command=goto&id=17
```

Lua mapping:

```lua
vlc.playlist.play()
vlc.playlist.pause()
vlc.playlist.stop()
vlc.playlist.next()
vlc.playlist.prev()
vlc.playlist.goto(id)
```

These playlist operations are directly documented by VLC.

---

# 27. Exact Seek Endpoint

Request:

```text
GET /bookmarkstudio/v1/seek?time_us=83221450
```

Implementation:

```lua
vlc.player.seek_by_time_absolute(time_us)
```

VLC exposes absolute and relative time seeking in microseconds.

This endpoint is important.

The built-in VLC HTTP interface's conventional time seek parser operates on integer seconds; although percentage seeking can use fractional values, Bookmark Studio should use the custom bridge when available for explicit microsecond targets.

---

# 28. Built-In VLC HTTP Fallback

If `bookmarkstudio.lua` is unavailable, Bookmark Studio may connect to VLC's regular HTTP interface.

VLC's built-in API exposes:

```text
status.json
playlist.json
pl_play
pl_pause
pl_forcepause
pl_forceresume
pl_stop
pl_next
pl_previous
seek
rate
```

The built-in status handler reports:

* playback time;
* position;
* current playlist ID;
* rate;
* state;
* media length.

Fallback mode shall therefore support most functionality.

UI status:

```text
VLC Integration: Standard
```

versus:

```text
VLC Integration: Enhanced
```

Enhanced means Bookmark Studio Lua bridge available.

---

# 29. VLC 4 Strategy

The current VLC master code contains native A-B loop APIs including:

```text
libvlc_media_player_set_abloop_time
libvlc_media_player_set_abloop_position
libvlc_media_player_reset_abloop
```

and internal player equivalents.

However, those controls are not currently documented in the VLC Lua player interface.

Therefore the baseline architecture **must not assume VLC 4 native A-B loop access from Lua**.

Baseline:

```text
VLC 3 → Python software loop controller
VLC 4 → Python software loop controller
```

Future:

```text
VLC 4
→ native helper
or new Lua API
→ native A-B implementation
```

The playback adapter isolates this change.

---

# 30. Playback Adapter Interface

Python:

```python
from typing import Protocol

class PlaybackAdapter(Protocol):
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def get_status(self) -> "PlaybackStatus": ...
    def get_playlist(self) -> list["VlcPlaylistItem"]: ...
    def play(self) -> None: ...
    def pause(self) -> None: ...
    def stop(self) -> None: ...
    def next_track(self) -> None: ...
    def previous_track(self) -> None: ...
    def goto_item(self, vlc_id: int) -> None: ...
    def seek_absolute_us(self, time_us: int) -> None: ...
    def seek_relative_us(self, delta_us: int) -> None: ...
    def set_rate(self, rate: float) -> None: ...
```

Implementations:

```text
EnhancedLuaPlaybackAdapter
StandardHttpPlaybackAdapter
MockPlaybackAdapter
```

`MockPlaybackAdapter` is essential for automated UI testing.

---

# 31. Playback Monitoring

Network polling shall not drive UI rendering directly.

Recommended architecture:

```text
VLC status request
       ↓
PlaybackSnapshot
       ↓
PlaybackClock
       ↓
local monotonic interpolation
       ↓
60 Hz playhead rendering
```

Use:

```python
time.monotonic_ns()
```

The last VLC sample supplies:

```text
VLC time
local monotonic timestamp
state
rate
```

When VLC is playing:

```python
estimated_position_us = (
    sampled_position_us
    + elapsed_monotonic_us * playback_rate
)
```

This produces smooth visual movement without making 60 HTTP requests per second.

---

# 32. Polling Intervals

Normal state:

```text
status:
100–200 ms

playlist:
500–1000 ms
```

When looping and within approximately 500 ms of B:

```text
status:
20–40 ms
```

When VLC is paused:

```text
status:
250–500 ms
```

When disconnected:

```text
reconnect:
1000 ms initially
then bounded backoff up to 5000 ms
```

---

# 33. Loop Controller

Class:

```python
class LoopController(QObject):
    ...
```

State enum:

```python
class LoopState(Enum):
    IDLE = auto()
    ARMED = auto()
    PLAYING = auto()
    SEEKING_BACK = auto()
    GAP = auto()
    COMPLETED = auto()
```

Configuration:

```python
@dataclass(frozen=True)
class LoopSpec:
    start_us: int
    end_us: int
    repeat_count: int | None
    gap_ms: int
    completion_action: CompletionAction
```

`repeat_count=None` means forever.

---

# 34. Loop Algorithm

```text
Start Loop
    ↓
validate A < B
    ↓
seek A
    ↓
play
    ↓
monitor interpolated VLC time
    ↓
approaching B?
    ↓
increase polling rate
    ↓
time >= B
    ↓
repeat remaining?
  /             \
yes              no
 │                │
gap?              completion action
 │
seek A
```

Loop completion actions:

```text
Continue normal playback
Pause
Stop
Next bookmark
Previous bookmark
Next Segment Queue item
Next VLC track
```

---

# 35. Loop Precision

VLC Bookmark Studio is not a DAW.

For compressed media, decoder/keyframe behavior may introduce seek variation.

Acceptance target for VLC 3 software loops:

```text
Typical audio:
≤ approximately 100 ms visible loop-boundary error

Worst acceptable under normal local-file conditions:
≤ approximately 200 ms
```

Measurements shall be gathered during automated/integration testing.

The UI shall not advertise sample-accurate looping.

---

# 36. Temporary Selection

A drag selection is not automatically a bookmark.

Data object:

```python
@dataclass
class Selection:
    start_us: int
    end_us: int
```

Lifecycle:

```text
Mouse drag
→ temporary Selection

Play
→ preview Selection

Loop
→ temporary loop

Adjust
→ Selection changes

Bookmark
→ persistent Bookmark created
```

Escape clears selection.

---

# 37. Paint-to-Bookmark Workflow

Main interaction:

```text
1. User presses mouse button over waveform.
2. Selection start is captured.
3. User drags horizontally.
4. Highlight follows cursor.
5. Start, End and Duration update live.
6. User releases mouse.
7. Selection remains visible.
8. Floating actions appear:
   Bookmark
   Play
   Loop
   Clear
9. Bookmark creates persistent record.
```

---

# 38. Waveform Mouse Events

`WaveformView` shall override:

```python
mousePressEvent()
mouseMoveEvent()
mouseReleaseEvent()
wheelEvent()
keyPressEvent()
```

Use:

```python
scene_pos = self.mapToScene(event.position().toPoint())
time_us = self.scene_x_to_time_us(scene_pos.x())
```

Clamp:

```python
0 <= time_us <= media_duration_us
```

---

# 39. Scene Coordinate System

Recommended mapping:

```text
1 scene X unit = 1 millisecond
```

Functions:

```python
def time_us_to_scene_x(time_us: int) -> float:
    return time_us / 1000.0

def scene_x_to_time_us(x: float) -> int:
    return max(0, round(x * 1000))
```

Domain model continues to use integers.

QGraphics coordinates are only a presentation conversion.

---

# 40. Point Bookmarks

Point bookmark:

```text
02:14.420
```

Visual representation:

```text
───────────────▲────────────────
               │
           Guitar enters
```

Fields:

```python
start_us
end_us = None
```

Actions:

* seek;
* play from point;
* rename;
* move;
* notes;
* tags;
* delete;
* convert to range.

---

# 41. Segment Bookmarks

Segment bookmark:

```text
01:17.400 → 01:48.100
```

Visual:

```text
         Chorus
      ┌───────────────────┐
──────│███████████████████│────────
      └───────────────────┘
```

Interactive components:

```text
left resize handle
region body
right resize handle
label
```

---

# 42. Bookmark Editing

Selected bookmark inspector:

```text
Name:
[ Chorus                ]

Start:
[ 00:01:17.400 ]

End:
[ 00:01:48.100 ]

Duration:
00:00:30.700

[-10s] [-1s] [-100ms] [+100ms] [+1s] [+10s]

Loop:
☑

Repeat:
[ Forever ▼ ]

Gap:
[ 0 ms ]

After loop:
[ Pause ▼ ]

Lane:
[ Song Structure ▼ ]

Tags:
chorus, practice

Notes:
[________________________]
```

---

# 43. Precise Time Entry

Parser shall support:

```text
17
17.450
1:17
1:17.450
00:01:17.450
```

Internal representation:

```text
integer microseconds
```

Parser:

```python
parse_timecode(text: str) -> int
```

Formatter:

```python
format_timecode(time_us: int) -> str
```

Preferred display precision:

```text
HH:MM:SS.mmm
```

---

# 44. Time Adjustment Commands

Required increments:

```text
-10 seconds
-1 second
-100 milliseconds
+100 milliseconds
+1 second
+10 seconds
```

Keyboard nudge:

```text
Alt+Left/Right:
100 ms

Alt+Shift+Left/Right:
10 ms

Alt+Ctrl+Left/Right:
1 second
```

User-adjustable.

---

# 45. Mark While Listening

Adopt the efficient Audacity-style model:

```text
[
→ mark Start at VLC current position

]
→ mark End at VLC current position
```

Audacity uses the same left/right boundary workflow while playback continues.

Then:

```text
Ctrl+B
→ create bookmark
```

This workflow must operate without pausing VLC.

---

# 46. Bookmark Naming

After Ctrl+B:

```text
Inline label editor appears
```

The user types:

```text
Chorus
```

and presses:

```text
Enter
```

No modal dialog should appear during the normal fast-capture workflow.

Escape cancels naming.

---

# 47. Bookmark Lanes

Optional but included in the architecture.

Example:

```text
Waveform
██████████████████████████████████████

Structure
      [VERSE]       [CHORUS]

Practice
                   [DIFFICULT]

Solo
                              [SOLO]

Notes
          ▲                    ▲
```

Lane fields:

```text
id
playlist_id
name
order_index
visible
locked
color_key
created_at
```

---

# 48. Overlapping Bookmarks

Overlapping regions must be supported.

Collision handling:

```text
same lane + overlap
→ stack visually within lane

different lanes
→ independent
```

Do not prohibit overlapping time ranges.

---

# 49. Bookmark Dragging

Dragging body:

```python
new_start = snap(original_start + delta)
new_end = snap(original_end + delta)
```

Clamp at media boundaries.

If new start would be negative:

```text
preserve duration
move entire region to zero
```

If new end exceeds media duration:

```text
preserve duration
move region left
```

---

# 50. Bookmark Resize

Left handle:

```text
changes start only
```

Right handle:

```text
changes end only
```

Minimum segment duration:

```text
50 ms default
```

Configurable.

If handles cross:

```text
do not silently reverse
```

Clamp to minimum duration.

---

# 51. Snapping

Snap modes:

```text
Off
10 ms
100 ms
500 ms
1 second
Bookmark boundaries
```

Future:

```text
Detected transient
Beat grid
Video frame
```

Sonic Visualiser demonstrates useful region-boundary snapping behavior.

---

# 52. Snap Priority

When multiple candidates exist:

```text
1. Bookmark edge
2. Explicit time grid
3. Future transient/beat
```

Only snap if screen-distance is less than:

```text
8 pixels default
```

This makes snapping dependent on visual relevance rather than an arbitrary time difference.

---

# 53. Snap Guides

When snapping occurs:

```text
vertical guide appears
timestamp tooltip appears
```

Guide disappears after drag ends.

---

# 54. Zoom

Controls:

```text
Ctrl + mouse wheel
Ctrl++
Ctrl+-
toolbar buttons
overview viewport resize
```

Anchor zoom around mouse cursor.

Use `QGraphicsView.scale()` / transforms.

Qt's Graphics View is explicitly designed to support transformations such as scaling.

---

# 55. Auto-Follow Playhead

Modes:

```text
Off
Follow when playhead leaves viewport
Center playhead
Page follow
```

Default:

```text
Follow when playhead reaches 80% of viewport
```

Do not constantly center the playhead by default because this makes manual inspection difficult.

---

# 56. Overview Waveform

Full-song waveform shall appear below the detailed waveform.

The overview shows:

```text
whole song
current playhead
bookmarks
detailed viewport rectangle
```

Dragging viewport rectangle pans detailed view.

Peaks.js uses the same overview/zoom-view model.

---

# 57. Waveform Generation

Recommended pipeline:

```text
media file
   ↓
FFmpeg
   ↓
PCM float32
   ↓
NumPy
   ↓
min/max peak reduction
   ↓
multi-resolution pyramid
   ↓
cache
   ↓
WaveformItem
```

---

# 58. FFmpeg Invocation

Use argument arrays only.

Example:

```python
args = [
    ffmpeg_path,
    "-v", "error",
    "-i", str(media_path),
    "-map", "0:a:0",
    "-ac", "1",
    "-ar", "8000",
    "-f", "f32le",
    "pipe:1",
]
```

Never use:

```python
shell=True
```

for paths originating from media or project files.

---

# 59. Waveform Sampling

Recommended analysis rate:

```text
8000 samples/sec
```

This is sufficient for visual selection while drastically reducing processing volume.

Stereo mode may decode two channels.

Waveform rendering does not define bookmark times; it is purely a visual representation.

Bookmark timing always uses VLC/media timeline time.

---

# 60. NumPy Peak Calculation

PCM:

```python
samples = np.frombuffer(chunk, dtype="<f4")
```

Peak block:

```python
block = samples.reshape(-1, block_size)
minimum = block.min(axis=1)
maximum = block.max(axis=1)
```

Store:

```text
[min, max]
```

per visual sample.

---

# 61. Waveform Pyramid

Example levels at 8 kHz:

```text
Level 0:
64 PCM samples per peak
≈ 8 ms

Level 1:
256
≈ 32 ms

Level 2:
1024
≈ 128 ms

Level 3:
4096
≈ 512 ms

Level 4:
16384
≈ 2.048 sec
```

Continue until full-media overview contains a manageable point count.

Renderer selects the nearest level to roughly:

```text
1–2 peak columns per screen pixel
```

---

# 62. Waveform Cache

Cache directory:

```text
%LOCALAPPDATA%\VLCBookmarkStudio\waveforms\
```

Key:

```python
sha256(
    media_fingerprint
    + waveform_algorithm_version
    + sample_rate
    + channel_mode
)
```

Cache metadata stored in SQLite.

Peak arrays stored as binary NumPy files.

Suggested:

```text
<cache-key>.npz
```

---

# 63. Waveform Cache Invalidations

Invalidate if:

```text
media fingerprint changes
algorithm version changes
analysis sample rate changes
channel mode changes
cache is corrupted
```

Never invalidate solely because a media file moved.

---

# 64. Waveform Background Processing

No waveform analysis may run on the Qt GUI thread.

Use:

```text
QThreadPool
QRunnable
```

Qt documents `QRunnable` + `QThreadPool` specifically for executing tasks on worker threads.

Required jobs:

```text
WaveformGenerationJob
MediaFingerprintJob
PlaylistAnalysisJob
ImportJob
ExportJob
MissingMediaSearchJob
```

---

# 65. Job Cancellation

Every long worker shall support cooperative cancellation.

Example:

```python
class CancellationToken:
    cancelled: threading.Event
```

FFmpeg processes must be terminated when cancellation occurs.

Do not allow switching songs repeatedly to leave dozens of waveform decoders running.

---

# 66. Waveform Request Deduplication

If three components request the same waveform:

```text
only one job runs
```

Use:

```python
inflight: dict[WaveformKey, Future]
```

Consumers subscribe to the same job result.

---

# 67. Media Identity

Media must remain identifiable if renamed or moved.

Store:

```text
canonical_uri
file_size
mtime_ns
duration_us
fast_fingerprint
optional full_sha256
```

---

# 68. Fast Media Fingerprint

Recommended algorithm:

```text
SHA-256(
    file_size
    +
    first 1 MiB
    +
    middle 1 MiB
    +
    final 1 MiB
)
```

For files below the threshold, hash the complete contents once.

Purpose:

```text
identity matching
```

not cryptographic authentication.

---

# 69. Media Resolution

Matching order:

```text
1. exact canonical URI
2. known URI alias
3. fast fingerprint
4. full SHA-256 if present
5. filename + size + duration heuristic
6. user selects replacement
```

When a moved media file matches by fingerprint:

```text
update URI alias
do not create new media ID
```

---

# 70. URI Normalization

Use:

```python
pathlib.Path.resolve(strict=False)
```

for local filesystem paths.

Store canonical database URI as:

```text
file:///...
```

Do not lower-case all path strings indiscriminately.

For Windows matching, normalize using filesystem semantics separately from display.

---

# 71. Database

File:

```text
%LOCALAPPDATA%\VLCBookmarkStudio\data\bookmarkstudio.db
```

SQLite configuration on open:

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
```

Only the repository layer may perform SQL.

---

# 72. Database Tables

Required tables:

```text
schema_migrations
playlists
playlist_signatures
media
media_uri_aliases
playlist_items
bookmarks
lanes
bookmark_tags
settings_metadata
waveform_cache
recent_projects
```

---

# 73. Playlist Table

```sql
CREATE TABLE playlists (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_uri TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT,
    is_ad_hoc INTEGER NOT NULL DEFAULT 0
);
```

IDs should use UUIDs.

---

# 74. Playlist Signature Table

```sql
CREATE TABLE playlist_signatures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id TEXT NOT NULL,
    signature TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (playlist_id)
        REFERENCES playlists(id)
        ON DELETE CASCADE
);

CREATE UNIQUE INDEX idx_playlist_signature
ON playlist_signatures(signature);
```

Every meaningful historical form of a recognized playlist may therefore remain recognizable.

---

# 75. Media Table

```sql
CREATE TABLE media (
    id TEXT PRIMARY KEY,
    canonical_uri TEXT,
    filename TEXT,
    title TEXT,
    artist TEXT,
    album TEXT,
    duration_us INTEGER,
    file_size INTEGER,
    mtime_ns INTEGER,
    fast_fingerprint TEXT,
    full_sha256 TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT
);
```

---

# 76. Playlist Items

Preserve occurrences:

```sql
CREATE TABLE playlist_items (
    id TEXT PRIMARY KEY,
    playlist_id TEXT NOT NULL,
    media_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    occurrence_index INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (playlist_id)
        REFERENCES playlists(id)
        ON DELETE CASCADE,
    FOREIGN KEY (media_id)
        REFERENCES media(id)
);
```

Bookmarks do **not** normally reference this occurrence table.

---

# 77. Bookmark Table

```sql
CREATE TABLE bookmarks (
    id TEXT PRIMARY KEY,
    playlist_id TEXT,
    media_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    lane_id TEXT,
    bookmark_type TEXT NOT NULL,
    name TEXT NOT NULL,
    start_us INTEGER NOT NULL,
    end_us INTEGER,
    loop_enabled INTEGER NOT NULL DEFAULT 0,
    repeat_count INTEGER,
    loop_gap_ms INTEGER NOT NULL DEFAULT 0,
    completion_action TEXT NOT NULL DEFAULT 'continue',
    color_key TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (playlist_id)
        REFERENCES playlists(id),
    FOREIGN KEY (media_id)
        REFERENCES media(id),
    FOREIGN KEY (lane_id)
        REFERENCES lanes(id)
);
```

`repeat_count = NULL` means forever.

`end_us = NULL` means point bookmark.

---

# 78. Bookmark Validation

Rules:

```text
start_us >= 0
start_us <= duration if duration known

end_us is NULL
OR
end_us > start_us

end_us <= duration if duration known

loop requires end_us

repeat_count is NULL or >= 1

loop_gap_ms >= 0
```

Validation occurs at:

```text
domain object
service layer
database boundary
```

Never rely exclusively on UI validation.

---

# 79. Repository Interfaces

```python
class PlaylistRepository:
    find_by_signature(...)
    get(...)
    insert(...)
    update(...)
    add_signature(...)
    list_recent(...)

class MediaRepository:
    resolve_by_uri(...)
    resolve_by_fingerprint(...)
    insert(...)
    update(...)

class BookmarkRepository:
    list_for_playlist_media(...)
    list_global_for_media(...)
    insert(...)
    update(...)
    delete(...)

class WaveformCacheRepository:
    lookup(...)
    put(...)
    invalidate(...)
```

---

# 80. Transactions

Bookmark creation:

```text
BEGIN
insert bookmark
update playlist timestamp
COMMIT
```

Import:

```text
BEGIN
create/update playlist
resolve media
import lanes
import bookmarks
COMMIT
```

Any error:

```text
ROLLBACK
```

---

# 81. Autosave

Bookmark Studio is database-backed and automatically persistent.

Operations committed immediately after completion:

```text
create
rename
move
resize
delete
change loop
change repeat
change notes
change tags
change lane
```

Dragging should not write to SQLite for every mouse movement.

Correct workflow:

```text
mouse movement
→ update visual model only

mouse release
→ commit one command
→ one database transaction
```

---

# 82. Undo / Redo

Use Qt's undo framework:

```text
QUndoStack
QUndoCommand
```

Qt's undo stack is explicitly designed to store editing commands with redo/undo semantics.

Commands:

```text
CreateBookmarkCommand
DeleteBookmarkCommand
MoveBookmarkCommand
ResizeBookmarkCommand
RenameBookmarkCommand
ChangeLoopCommand
MoveBookmarkLaneCommand
```

---

# 83. Command Compression

Dragging should become one undo operation.

Implement:

```python
QUndoCommand.id()
QUndoCommand.mergeWith()
```

or delay command creation until release.

Result:

```text
drag bookmark continuously for 3 seconds
Ctrl+Z
→ one undo
```

not hundreds.

---

# 84. Keyboard Shortcuts

Defaults:

| Shortcut     | Function                         |
| ------------ | --------------------------------- |
| Space        | Play/Pause                       |
| `[`          | Mark selection start             |
| `]`          | Mark selection end               |
| Ctrl+B       | Bookmark current selection       |
| Ctrl+Shift+B | Point bookmark at playhead       |
| L            | Loop temporary selection         |
| Ctrl+L       | Toggle bookmark loop             |
| Alt+Left     | Previous bookmark                |
| Alt+Right    | Next bookmark                    |
| Left         | Seek -5 s                        |
| Right        | Seek +5 s                        |
| Shift+Left   | Seek -100 ms                     |
| Shift+Right  | Seek +100 ms                     |
| Ctrl++       | Zoom in                          |
| Ctrl+-       | Zoom out                         |
| Ctrl+0       | Fit entire media                 |
| F2           | Rename bookmark                  |
| Delete       | Delete selected bookmark         |
| Ctrl+Z       | Undo                             |
| Ctrl+Y       | Redo                             |
| Escape       | Clear selection/cancel operation |

All shortcuts shall be configurable.

---

# 85. Previous / Next Bookmark

Navigation uses:

```text
start_us ascending
```

Tie-break:

```text
lane order
then creation time
```

Navigation wraps optionally.

Setting:

```text
Wrap bookmark navigation:
On / Off
```

---

# 86. Segment Queue

A Segment Queue allows bookmarks to function as a playback playlist.

Example:

```text
Practice Session
1. Song A — Verse ×3
2. Song A — Chorus ×5
3. Song B — Solo ×3
4. Song C — Difficult transition ×10
```

Each queue entry contains:

```text
bookmark_id
override_repeat_count
override_gap
override_completion
```

This should be P1, but the data model shall anticipate it.

---

# 87. Compact Mode

Compact window:

```text
┌──────────────────────────────┐
│ Guitar Practice             │
│ Hotel California            │
│                              │
│ 01:23.441                    │
│                              │
│ [ Mark A ]   [ Mark B ]     │
│                              │
│ [Bookmark]   [Loop]         │
│                              │
│ B◀  ◀   ■   ▶/Ⅱ   ▶   ▶B  │
└──────────────────────────────┘
```

Purpose:

```text
bookmark creation while VLC is visually primary
```

Always-on-top setting:

```text
optional
```

---

# 88. Bookmark Context Menu

Right-click:

```text
Play
Play Once
Loop
Jump to Start
Jump to End
Rename
Edit Times
Duplicate
Convert Point to Range
Move to Lane
Change Color
Add Tag
Add to Segment Queue
Copy Timestamp
Copy Range
Export
Delete
```

---

# 89. Search

Search across:

```text
bookmark name
playlist
song title
artist
album
lane
tag
notes
```

Result:

```text
Guitar Practice
  Hotel California
    Main Solo    04:19.220
```

Double-click result:

```text
load/select playlist context
select track
seek bookmark
```

If VLC does not currently contain the relevant playlist, Bookmark Studio may display the saved context offline but shall not silently rewrite VLC's playlist unless the user explicitly requests it.

---

# 90. Project Export

Extension:

```text
.vlcbmk
```

Recommended format:

```text
ZIP container
```

Contents:

```text
manifest.json
bookmarks.json
playlists.json
media.json
lanes.json
```

Do not embed original media by default.

Optional future:

```text
portable bundle with media
```

---

# 91. Project Manifest

Example:

```json
{
  "format": "vlc-bookmark-studio",
  "format_version": 1,
  "application_version": "1.0.0",
  "created_utc": "2026-09-01T12:00:00Z"
}
```

Unknown fields must be ignored when reading for forward compatibility.

Unsupported higher major format version:

```text
reject cleanly
```

---

# 92. CSV Export

Columns:

```text
playlist
media
artist
title
bookmark
type
start
end
duration
loop
repeats
lane
tags
notes
```

UTF-8 with BOM optional for Excel compatibility on Windows.

---

# 93. Audacity Label Import / Export

Strong P1 feature.

Audacity-style labels naturally map to:

```text
point bookmark
range bookmark
```

This improves interoperability with the UX model already adopted.

---

# 94. Playlist File Formats

When a playlist source file is available, support recognition of:

```text
M3U
M3U8
XSPF
PLS
```

Bookmark Studio does **not** need to replace VLC's playlist parser.

It primarily observes the playlist as VLC resolved it.

---

# 95. FFmpeg Clip Export

P1 feature.

A segment bookmark may optionally be exported as a clip.

Input:

```text
media URI
start_us
end_us
```

Use FFmpeg.

Modes:

```text
fast stream copy where possible
accurate re-encode
```

Never modify original media.

---

# 96. Loop Trainer

P1/P2 feature.

Example:

```text
Bookmark:
Chorus

Repeat:
10

Start speed:
70%

Increase:
5%

Every:
2 repeats

Final speed:
100%

Pause between loops:
500 ms
```

State machine should be implemented above the standard LoopController rather than duplicating loop logic.

---

# 97. Playback Rate

Controls:

```text
0.50×
0.60×
0.70×
0.75×
0.80×
0.90×
1.00×
1.10×
1.25×
1.50×
2.00×
```

Custom rate entry allowed.

Lua exposes playback-rate read and write operations.

---

# 98. Optional Beat / Onset Analysis

Do not include in MVP.

Future Python providers:

```text
librosa
aubio
custom onset detector
```

Potential features:

```text
beat grid
snap to beat
transient snap
bar markers
automatic loop quantization
```

These are optional analysis layers, never required for core bookmark functionality.

---

# 99. Video Support

For video:

```text
waveform = first selected audio stream
```

Bookmarks continue to reference media time.

P1/P2 enhancements:

```text
thumbnail at bookmark start
frame stepping
video-frame snap
scene thumbnails
```

VLC Lua already exposes next-frame control.

---

# 100. Streams

Network streams require special handling.

Possible states:

```text
duration unknown
seeking unsupported
media unavailable to FFmpeg
```

Behavior:

```text
point bookmarks
→ allowed where meaningful

segment bookmarks
→ allowed only when stable time reference exists

loop
→ disabled if seeking cannot be validated

waveform
→ timeline-only fallback if analysis unavailable
```

Show explicit status:

```text
Waveform unavailable for this media.
```

---

# 101. Seek Capability Detection

Because the current Lua API documentation does not expose a straightforward `can_seek()` method, Bookmark Studio shall not fabricate a capability flag.

Strategy:

```text
duration unavailable
→ assume uncertain

local regular file
→ seek expected

network stream
→ seek uncertain

after requested seek
→ compare subsequent reported position
```

Maintain observed capability:

```text
UNKNOWN
SUPPORTED
FAILED
```

A future VLC/native adapter may expose explicit capability information.

---

# 102. Missing Media

Playlist/media panel state:

```text
⚠ Missing
```

Actions:

```text
Locate File
Search Folder
Ignore
Remove Saved Reference
```

Folder search uses fast fingerprint matching.

Do not recursively search an entire disk automatically.

---

# 103. File Rename / Move

If fingerprint identifies moved file:

```text
Media ID remains unchanged
canonical URI updated
old URI added to aliases
```

Bookmarks therefore remain attached.

---

# 104. Playback Disconnection

If VLC closes:

```text
Bookmark Studio remains open.
Waveform remains visible.
Bookmarks remain editable.
Playback controls disabled.
Connection status changes.
```

Connection status:

```text
● Connected
● Reconnecting
● Offline
```

Do not block UI.

---

# 105. VLC Restart

When VLC restarts:

```text
health succeeds
        ↓
version negotiation
        ↓
playlist refresh
        ↓
playlist recognition
        ↓
current song resolution
        ↓
bookmark context loaded
```

No manual reload should be needed.

---

# 106. Protocol Versioning

Bridge responses contain:

```json
{
  "protocol_version": 1
}
```

Python client shall support:

```text
min_protocol
max_protocol
```

If incompatible:

```text
Bookmark Studio VLC integration is out of date.
```

Offer repair/reinstall integration.

---

# 107. Bridge Error Format

```json
{
  "ok": false,
  "error": {
    "code": "INVALID_TIME",
    "message": "time_us must be non-negative"
  }
}
```

Stable machine codes:

```text
INVALID_REQUEST
INVALID_TIME
INVALID_ITEM
NO_MEDIA
NO_PLAYLIST
UNSUPPORTED
INTERNAL_ERROR
```

---

# 108. HTTP Client

Python class:

```python
class VlcBridgeClient(QObject):
    status_received = Signal(object)
    playlist_received = Signal(object)
    bridge_error = Signal(object)
```

Recommended implementation uses Qt networking:

```text
QNetworkAccessManager
QNetworkRequest
QNetworkReply
```

Advantages:

```text
native Qt event loop integration
no network calls on UI-blocking worker
asynchronous requests
built-in reply signals
```

Avoid synchronous HTTP calls from GUI event handlers.

---

# 109. Request Timeouts

Recommended:

```text
status:
500 ms

transport command:
1000 ms

playlist:
1500 ms

startup health:
1000 ms
```

A failed status request must not freeze the UI.

---

# 110. Playlist Poll Optimization

Do not fetch the full playlist 10 times per second.

Maintain:

```text
current_playlist_item_id
playlist_signature_hint
last_playlist_fetch
```

Refresh full playlist when:

```text
current ID unknown
periodic interval elapsed
VLC reconnect occurred
playlist count/hash hint changed
user requests refresh
```

---

# 111. UI Rendering Frequency

Target:

```text
60 Hz where display supports it
```

Use a Qt timer approximately:

```text
16 ms
```

Only repaint dynamic areas:

```text
playhead
selection overlay
active loop indicators
```

Do not reconstruct QGraphicsScene every frame.

---

# 112. Waveform Painting

`WaveformItem.paint()` shall calculate the visible scene rectangle and select the appropriate peak level.

Pseudo-code:

```python
def paint(self, painter, option, widget=None):
    visible = option.exposedRect
    start_us = self.scene_x_to_time_us(visible.left())
    end_us = self.scene_x_to_time_us(visible.right())

    level = self.cache.best_level(
        start_us,
        end_us,
        pixel_width=visible_width
    )

    peaks = level.slice(start_us, end_us)
    draw_peaks(painter, peaks)
```

Never render the complete 4-hour waveform when the user is viewing 10 seconds.

---

# 113. Bookmark Rendering

Custom:

```python
class BookmarkRegionItem(QGraphicsObject):
    ...
```

Signals:

```text
moveStarted
movePreview
moveFinished
resizeStarted
resizePreview
resizeFinished
activated
contextMenuRequested
```

Graphics item never writes database records itself.

It sends intent to controller/service.

---

# 114. UI Architecture

Recommended pattern:

```text
MVVM-inspired / Presenter-Service
```

Not strict framework-heavy MVVM.

Layers:

```text
Views
↓
View Models / Controllers
↓
Application Services
↓
Domain
↓
Repositories / Playback Adapter
```

Example:

```text
BookmarkRegionItem dragged
↓ signal
WaveformController
↓ command
BookmarkService.move_bookmark()
↓ repository
SQLite
```

---

# 115. Domain Events

Events:

```text
VlcConnected
VlcDisconnected
PlaybackStateChanged
PlaybackPositionSampled
CurrentVlcItemChanged
PlaylistSnapshotChanged
PlaylistContextChanged
MediaResolved
WaveformRequested
WaveformReady
SelectionChanged
BookmarkCreated
BookmarkUpdated
BookmarkDeleted
BookmarkActivated
LoopStarted
LoopIterationChanged
LoopCompleted
```

Qt signals may carry events inside UI/process boundaries.

---

# 116. Application Package Layout

```text
src/
└── bookmark_studio/
    │
    ├── __main__.py
    │
    ├── bootstrap.py
    │
    │
    ├── app/
    │   ├── application.py
    │   ├── commands.py
    │   └── events.py
    │
    ├── domain/
    │   ├── bookmark.py
    │   ├── media.py
    │   ├── playlist.py
    │   ├── lane.py
    │   ├── selection.py
    │   └── loop.py
    │
    ├── playback/
    │   ├── adapter.py
    │   ├── bridge_client.py
    │   ├── enhanced_adapter.py
    │   ├── http_fallback.py
    │   ├── playback_clock.py
    │   └── loop_controller.py
    │
    ├── playlist/
    │   ├── recognition.py
    │   ├── signatures.py
    │   ├── similarity.py
    │   └── synchronizer.py
    │
    ├── media/
    │   ├── resolver.py
    │   ├── fingerprint.py
    │   └── metadata.py
    │
    ├── waveform/
    │   ├── service.py
    │   ├── ffmpeg_decoder.py
    │   ├── peaks.py
    │   ├── pyramid.py
    │   └── cache.py
    │
    ├── persistence/
    │   ├── database.py
    │   ├── migrations.py
    │   ├── playlist_repository.py
    │   ├── media_repository.py
    │   ├── bookmark_repository.py
    │   └── waveform_repository.py
    │
    ├── project/
    │   ├── import_service.py
    │   ├── export_service.py
    │   └── schema.py
    │
    ├── ui/
    │   ├── main_window.py
    │   ├── playlist_panel.py
    │   ├── bookmark_panel.py
    │   ├── inspector.py
    │   ├── transport.py
    │   ├── waveform/
    │   │   ├── view.py
    │   │   ├── scene.py
    │   │   ├── waveform_item.py
    │   │   ├── bookmark_item.py
    │   │   ├── selection_item.py
    │   │   └── playhead_item.py
    │   └── dialogs/
    │
    ├── settings/
    │   └── settings_service.py
    │
    └── logging/
        └── setup.py

vlc/
└── bookmarkstudio.lua

tests/
├── unit/
├── integration/
├── ui/
├── vlc/
└── fixtures/
```

---

# 117. Type Safety

Use:

```python
from __future__ import annotations
```

Use type hints for all public methods.

Use:

```text
mypy
```

in CI.

Domain IDs may use typed wrappers or UUID aliases to prevent accidental playlist/media-ID mixing.

---

# 118. Dataclasses

Example:

```python
@dataclass(frozen=True, slots=True)
class Bookmark:
    id: UUID
    playlist_id: UUID | None
    media_id: UUID
    bookmark_type: BookmarkType
    name: str
    start_us: int
    end_us: int | None
    loop_enabled: bool
    repeat_count: int | None
    loop_gap_ms: int
    completion_action: CompletionAction
```

Immutable domain records reduce accidental UI-side mutation.

Updates produce new values through services.

---

# 119. Enums

Use enums rather than magic strings internally:

```python
class BookmarkType(StrEnum):
    POINT = "point"
    SEGMENT = "segment"

class BookmarkScope(StrEnum):
    PLAYLIST_MEDIA = "playlist_media"
    GLOBAL_MEDIA = "global_media"

class CompletionAction(StrEnum):
    CONTINUE = "continue"
    PAUSE = "pause"
    STOP = "stop"
    NEXT_BOOKMARK = "next_bookmark"
    NEXT_TRACK = "next_track"
```

Database stores stable string values.

---

# 120. Configuration

Use `QSettings` for small application preferences:

```text
window geometry
splitter states
theme
last view mode
waveform display preferences
keyboard shortcuts
VLC executable path
bridge port
```

Do not store bookmark data in QSettings.

Use SQLite for application data.

---

# 121. Secrets

Bridge token should be stored using Windows Credential Manager if practical.

Fallback:

```text
restricted user settings file
```

Do not place token in project exports.

Do not log HTTP Authorization headers.

---

# 122. Logging

Directory:

```text
%LOCALAPPDATA%\VLCBookmarkStudio\logs\
```

Python standard logging.

Rotating files:

```text
bookmarkstudio.log
bookmarkstudio.log.1
...
```

Categories:

```text
APP
VLC
BRIDGE
PLAYLIST
MEDIA
WAVEFORM
BOOKMARK
LOOP
DATABASE
IMPORT
EXPORT
UI
```

---

# 123. Diagnostic Mode

Settings:

```text
Enable diagnostic logging
```

Diagnostics page:

```text
Application version
Python version
Qt version
VLC version
Bridge version
Bridge latency
Database path
Waveform cache path
FFmpeg version
Current playlist ID
Current media ID
```

Button:

```text
Copy Diagnostic Report
```

Never include authentication token.

---

# 124. Error Handling

Domain errors:

```text
InvalidBookmarkRange
UnknownMedia
PlaylistResolutionFailed
WaveformUnavailable
VlcDisconnected
SeekFailed
LoopInterrupted
ProjectFormatUnsupported
```

User-facing messages must be specific.

Bad:

```text
An error occurred.
```

Good:

```text
VLC did not reach the requested bookmark position.
The current stream may not support seeking.
```

---

# 125. Crash Recovery

SQLite WAL helps protect current data.

On startup:

```text
check schema
check DB integrity condition if previous shutdown dirty
remove stale temporary waveform files
recover pending import backup if present
```

Do not autosave the entire application state as a giant JSON document.

---

# 126. Database Migration

Table:

```sql
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
```

Migration files:

```text
001_initial.sql
002_playlist_signatures.sql
003_segment_queue.sql
...
```

Never mutate user database schema ad hoc from random repository functions.

---

# 127. Import Validation

Before modifying DB:

```text
open archive
validate manifest
validate JSON schemas
validate IDs
validate bookmark ranges
resolve media
build import plan
show conflicts if necessary
```

Then one transaction.

---

# 128. Export Atomicity

Write:

```text
project.tmp
```

Finish archive.

`fsync` if appropriate.

Then rename:

```text
project.vlcbmk
```

Do not overwrite good project file with partial output after crash.

---

# 129. Appearance

Support:

```text
System
Light
Dark
```

Use Qt palette/theme infrastructure.

Bookmark colors should come from named palette entries rather than arbitrary unvalidated HTML colors.

Color alone cannot encode bookmark type/status.

---

# 130. Accessibility

Requirements:

```text
keyboard-operable timeline
keyboard alternative for every drag operation
visible focus indicators
accessible names for controls
scalable text
Windows DPI scaling
high contrast compatibility
bookmark labels in addition to colors
```

Drag boundary editing must also be possible through exact Start/End controls.

---

# 131. DPI Testing

Required Windows scales:

```text
100%
125%
150%
175%
200%
```

Test at:

```text
1920×1080
2560×1440
4K
```

---

# 132. Minimum Window Size

Recommended:

```text
900 × 600 logical pixels
```

Below this size:

```text
inspector may collapse
playlist may collapse
```

Waveform remains the primary visible element.

---

# 133. Performance Targets

Application startup:

```text
< 2 seconds typical
```

Cached waveform:

```text
< 250 ms typical
```

Bookmark context switch:

```text
< 150 ms excluding uncached waveform
```

Playlist size:

```text
10,000 entries
```

Bookmarks:

```text
10,000+ database records
```

Interactive waveform:

```text
target 60 FPS
```

Idle CPU:

```text
low single-digit percentage
```

---

# 134. Large Playlist Behavior

Do not generate waveform caches for every playlist track immediately.

Strategy:

```text
current track
→ highest priority

next track
→ optional low-priority pre-cache

visible bookmarked tracks
→ optional

all other tracks
→ lazy
```

A 5,000-song playlist must not launch 5,000 FFmpeg jobs.

---

# 135. Waveform Pre-Fetch

Setting:

```text
Pre-generate waveform for next track:
On
```

When current media stabilizes:

```text
schedule next item at low priority
```

Cancel when playlist changes.

---

# 136. Metadata

Priority:

```text
VLC metadata
then filename fallback
```

Fields:

```text
title
artist
album
track number
duration
artwork URI if available
```

Original VLC URI remains authoritative for playback matching.

---

# 137. Transport Bar

Controls:

```text
Previous Bookmark
Previous Track
Seek Back
Stop
Play/Pause
Seek Forward
Next Track
Next Bookmark
```

State-sensitive enablement.

Example:

```text
no previous bookmark
→ Previous Bookmark disabled

VLC offline
→ all VLC transport disabled
```

---

# 138. Play Selection

If selection exists:

```text
seek start
play
```

Stop automatically at B if:

```text
Play Selection
```

was explicitly requested.

Otherwise ordinary VLC playback continues.

---

# 139. Double-Click Bookmark

Default:

```text
Play bookmark once
```

Configurable alternative:

```text
Seek only
```

---

# 140. Bookmark Activation

Single click:

```text
select
populate inspector
do not interrupt playback
```

This prevents accidental playback changes while editing.

---

# 141. Point Bookmark Double-Click

Because a point has no end:

```text
seek to point
play normally
```

---

# 142. Selection Tooltip

During drag:

```text
Start
01:17.442

End
01:48.103

Length
00:30.661
```

Tooltip follows cursor but must not obscure active edge.

---

# 143. Bookmark Labels

When zoomed out:

```text
truncate visually
```

Tooltip shows full name.

Never shorten underlying bookmark name.

---

# 144. Lane Visibility

Lane:

```text
eye button
→ visible

lock button
→ prevents mouse editing
```

Locked bookmarks remain playable.

---

# 145. Playlist Sidebar

Columns:

```text
Playing
Title
Artist
Duration
Bookmark count
Status
```

Bookmark count must represent currently selected scope.

Example:

```text
4
```

could mean four playlist-specific bookmarks for that media.

---

# 146. Playlist Filtering

Filter must operate locally against cached playlist model.

Search fields:

```text
title
artist
album
filename
```

Filtering the sidebar does **not** modify VLC's playlist.

---

# 147. Playlist Selection

Single click playlist item:

```text
select in Bookmark Studio
```

Default should **not** immediately change VLC playback.

Double click:

```text
play VLC playlist item
```

Optional setting:

```text
Single-click follows VLC
```

disabled by default.

---

# 148. Follow VLC Mode

Default enabled:

```text
Follow currently playing VLC song
```

If user manually selects another track in Bookmark Studio while VLC continues playing:

```text
temporarily suspend follow
```

Show:

```text
Viewing another track
[Return to Playing]
```

This is important for editing bookmarks without interrupting playback.

---

# 149. Bookmark Counts

Playlist row may display:

```text
4
```

Tooltip:

```text
4 playlist bookmarks
2 global bookmarks
```

---

# 150. Save / Load Behavior

Normal operation requires no explicit save.

Database autosaves.

Explicit commands:

```text
Export Bookmark Set
Import Bookmark Set
Backup Database
Restore Backup
```

Traditional:

```text
Save Project
Load Project
```

may be retained as user terminology but internally map to project export/import rather than being necessary for persistence.

---

# 151. Database Backup

Optional automatic backup:

```text
daily
retain 7
```

Directory:

```text
%LOCALAPPDATA%\VLCBookmarkStudio\backups\
```

Only create backup after successful database checkpoint.

---

# 152. Windows Installation

Installer responsibilities:

```text
install Bookmark Studio
install Python runtime via PyInstaller bundle
install Qt runtime
include/locate FFmpeg
install bookmarkstudio.lua
configure VLC interface integration
create Start menu entry
register .vlcbmk optionally
create uninstaller
```

---

# 153. Lua Installation Location

Use VLC user configuration location rather than Program Files whenever possible.

Logical target:

```text
<VLC user config>\lua\intf\bookmarkstudio.lua
```

VLC explicitly searches user Lua interface directories before global ones.

---

# 154. VLC Configuration

Installer should offer:

```text
Enable Bookmark Studio integration automatically
```

Required VLC configuration conceptually:

```text
extraintf=luaintf
lua-intf=bookmarkstudio
```

or launch VLC with:

```text
--extraintf=luaintf
--lua-intf=bookmarkstudio
```

The `extraintf=luaintf` mechanism is used for background Lua interfaces.

---

# 155. Managed VLC Mode

Preferred launch workflow:

```text
Bookmark Studio
      ↓
detect running VLC
      ↓
compatible integration?
  yes       no
   ↓         ↓
attach      offer launch managed VLC
```

Managed VLC launch:

```python
subprocess.Popen([
    vlc_path,
    "--extraintf=luaintf",
    "--lua-intf=bookmarkstudio",
    "--http-host=127.0.0.1",
    f"--http-port={port}",
])
```

Any required token/config shall be passed through a secure configuration mechanism rather than command-line plaintext where practical.

---

# 156. Existing VLC Instance

Bookmark Studio shall not kill or replace a user's existing VLC session automatically.

If integration unavailable:

```text
VLC is running without Bookmark Studio integration.

[Use Standard Interface]
[Restart VLC with Integration]
[Cancel]
```

Restart is always explicit.

---

# 157. Multiple VLC Instances

Potential ambiguity must be handled.

Each bridge reports:

```text
port
VLC version
current media
process association if discoverable
```

User may choose instance if multiple configured ports respond.

MVP may document:

```text
one managed VLC instance supported
```

while maintaining an architecture capable of extension.

---

# 158. Security

Requirements:

```text
loopback binding only
authentication
no shell=True
validate all numeric parameters
limit query lengths
escape JSON correctly
no arbitrary filesystem API in VLC bridge
no arbitrary Lua execution
no arbitrary VLC command passthrough
```

Only a whitelist of bridge commands is allowed.

---

# 159. Bridge Validation

Example:

```lua
if time_us < 0 then
    return error_json("INVALID_TIME")
end
```

Maximum target may be clamped to current media duration if known.

Playlist ID must be numeric and present before `goto`.

---

# 160. Test Toolchain

```text
pytest
pytest-qt
coverage.py
ruff
mypy
```

CI stages:

```text
lint
type-check
unit tests
database migration tests
UI tests
package build
optional VLC integration tests
```

---

# 161. Unit Tests

Minimum areas:

```text
time parser
time formatter
bookmark validation
media fingerprint
playlist strict signatures
playlist similarity
playlist-context resolution
duplicate songs
bookmark scope
snap calculations
waveform pyramid
peak calculation
loop state machine
project validation
DB migrations
```

---

# 162. Playlist Recognition Tests

Fixtures:

```text
exact playlist
renamed playlist
reordered playlist
one song added
one song removed
many songs added
duplicate song
same songs different order
similar but unrelated playlists
empty playlist
one-item playlist
```

Assertions shall ensure bookmarks never silently attach to a clearly wrong playlist.

---

# 163. Bookmark Context Tests

Critical regression:

```text
Playlist A + Song X → Bookmark A
Playlist B + Song X → Bookmark B

Switch A → B
Expected:
only Bookmark B shown

Switch B → A
Expected:
only Bookmark A shown
```

This is a release-blocking test.

---

# 164. UI Tests

Using pytest-qt:

```text
drag creates selection
selection start/end correct
click seeks
bookmark creation
bookmark resize
bookmark move
Ctrl+Z
Ctrl+Y
zoom
lane locking
playlist context switch
```

---

# 165. Waveform Tests

Use deterministic test PCM.

Verify:

```text
correct min/max values
correct pyramid resolution
correct duration mapping
correct cache key
correct cache invalidation
no off-by-one end sample
```

---

# 166. Loop Tests

Mock playback clock first.

Cases:

```text
infinite loop
N repetitions
pause after N
stop after N
next bookmark
media changes unexpectedly
VLC disconnects
user stops
bookmark edited while looping
end reached before status poll
```

---

# 167. VLC Integration Tests

Real VLC integration test environment:

```text
known WAV test file
known MP3 test file
known playlist
```

Verify:

```text
connect
health
playlist
current ID
play
pause
seek
next
previous
goto
reported time
reported media URI
```

---

# 168. Loop Accuracy Benchmark

For each supported VLC release:

```text
A = 10.000 s
B = 12.000 s
iterations = 100
```

Collect:

```text
end overshoot
seek restart position
restart latency
missed loops
mean
p95
maximum
```

Do not claim precision until measured.

---

# 169. Supported Media Test Matrix

At minimum:

```text
WAV
MP3
FLAC
AAC
M4A
OGG
Opus
MP4
MKV
MOV
AVI
```

Tests:

```text
short files
long files
multi-hour files
Unicode paths
spaces
non-ASCII artist/title
network shares
read-only files
renamed files
```

---

# 170. Dependency Management

Use one reproducible lock strategy.

Recommended:

```text
uv
```

or pinned requirements generated through an equivalent lock process.

Repository must commit:

```text
pyproject.toml
lock file
```

No floating production dependencies.

---

# 171. Code Quality

Required:

```text
ruff format
ruff check
mypy
pytest
```

Public API functions require docstrings.

Avoid over-engineering trivial private helpers.

---

# 172. Git Workflow

Recommended branches:

```text
main
feature/*
fix/*
```

Pull requests require:

```text
tests
lint
type-check
```

Database migration included whenever schema changes.

---

# 173. Application Versioning

Semantic versioning:

```text
MAJOR.MINOR.PATCH
```

Examples:

```text
1.0.0
1.1.0
1.1.1
```

Bridge has independent compatibility protocol version.

---

# 174. Feature Priority — MVP / P0

Release 1.0 requires:

1. Windows 10/11 application.
2. Python/PySide6 main UI.
3. VLC connection.
4. Custom Lua bridge.
5. Standard VLC HTTP fallback.
6. VLC playlist reading.
7. Playlist recognition.
8. Ad-hoc playlist contexts.
9. Playlist mutation tracking.
10. Song-level playlist bookmark contexts.
11. Current-song automatic bookmark loading.
12. Point bookmarks.
13. Segment bookmarks.
14. Multiple bookmarks per song.
15. Waveform generation.
16. Waveform cache.
17. Detailed waveform.
18. Overview waveform.
19. Click-to-seek.
20. Paint region.
21. Set Start/End from playback.
22. Exact timestamp editor.
23. Bookmark dragging.
24. Bookmark resizing.
25. Bookmark naming.
26. Delete.
27. Undo/redo.
28. Play selection.
29. Loop selection.
30. Loop bookmark.
31. Infinite loops.
32. Fixed repeat count.
33. Transport controls.
34. Previous/next bookmark.
35. Previous/next VLC song.
36. Autosave.
37. Import/export.
38. Missing file relink.
39. VLC reconnect.
40. Installer.

---

# 175. P1

After stable MVP:

```text
bookmark lanes
tags
notes search
Segment Queue
compact mode
Audacity labels import/export
clip export
automatic DB backup
loop completion actions
global bookmarks
advanced playlist similarity UI
waveform prefetch
playback-rate trainer
```

---

# 176. P2

```text
beat detection
onset detection
beat snapping
video thumbnails
video frame snapping
progressive-speed trainer
global Windows hotkeys
portable media project bundle
multi-VLC-instance support
```

---

# 177. Explicit Non-Goals for 1.0

VLC Bookmark Studio is **not**:

```text
a DAW
a waveform audio editor
a recording application
a multitrack mixing system
a sample-accurate loop engine
a replacement media player
a replacement VLC playlist manager
```

Original media is never modified.

---

# 178. Startup Sequence

```text
Launch Bookmark Studio
       ↓
load settings
       ↓
open/migrate SQLite
       ↓
initialize UI
       ↓
discover VLC
       ↓
probe enhanced bridge
       ↓
fallback probe
       ↓
connected?
    /       \
  yes        no
   ↓          ↓
playlist    offline mode
snapshot
   ↓
recognize playlist
   ↓
resolve current media
   ↓
load bookmarks
   ↓
load/generate waveform
```

---

# 179. Song Change Sequence

```text
PlaybackMonitor detects VLC ID change
→ fetch current item
→ resolve Media UUID
→ verify Playlist UUID
→ query BookmarkRepository
→ cancel obsolete waveform request
→ request new waveform
→ update timeline immediately
→ render bookmark regions
→ when waveform arrives:
   render waveform beneath existing bookmarks
```

Bookmarks should not wait for waveform decoding before appearing.

---

# 180. Playlist Change Sequence

```text
VLC playlist snapshot changes
→ normalize entries
→ resolve media identities
→ determine whether mutation of current context

if yes:
    update current playlist

if unknown:
    calculate signatures
    run PlaylistRecognitionService

→ activate resolved playlist
→ reload current-song bookmark scope
```

---

# 181. Bookmark Creation Sequence

```text
User paints region
→ Selection created
→ user presses Ctrl+B
→ inline name editor
→ Enter
→ BookmarkService.create()
→ validation
→ CreateBookmarkCommand pushed
→ repository INSERT
→ BookmarkCreated event
→ waveform displays permanent bookmark
→ Selection optionally cleared
```

---

# 182. Bookmark Resize Sequence

```text
drag handle
→ visual preview only
→ mouse release
→ validate range
→ ResizeBookmarkCommand
→ repository update
→ autosave
→ inspector refresh
```

---

# 183. Loop Start Sequence

```text
select bookmark
→ LoopController.start(bookmark)
→ seek start_us
→ confirm/estimate VLC clock
→ play
→ active-loop state
→ visual loop badge
→ monitor end
→ repeat
```

---

# 184. Playlist-Specific Bookmark Query

Primary SQL concept:

```sql
SELECT *
FROM bookmarks
WHERE media_id = ?
  AND playlist_id = ?
  AND scope = 'playlist_media'
ORDER BY start_us;
```

Combined mode adds:

```sql
OR (
    media_id = ?
    AND playlist_id IS NULL
    AND scope = 'global_media'
)
```

---

# 185. Database Indices

Required:

```sql
CREATE INDEX idx_bookmark_playlist_media
ON bookmarks(playlist_id, media_id, start_us);

CREATE INDEX idx_bookmark_media
ON bookmarks(media_id);

CREATE INDEX idx_media_fingerprint
ON media(fast_fingerprint);

CREATE INDEX idx_media_uri
ON media(canonical_uri);

CREATE INDEX idx_playlist_items
ON playlist_items(playlist_id, ordinal);
```

---

# 186. Threading Rule

Absolute rule:

> Never access QWidget/QGraphicsItem state from worker threads.

Workers return plain immutable data.

Qt main thread applies results.

---

# 187. Database Threading

Recommended simplest approach:

```text
one repository/database service serialized through main/application thread
```

Writes are short.

If large imports become significant, use a dedicated DB worker with its own SQLite connection.

Never share one SQLite connection concurrently across arbitrary threads.

---

# 188. FFmpeg Discovery

Order:

```text
bundled FFmpeg
configured user FFmpeg
PATH
```

Settings diagnostics show selected binary.

Validate with:

```text
ffmpeg -version
```

---

# 189. FFmpeg Missing

If unavailable:

```text
try QAudioDecoder
```

Qt provides `QAudioDecoder`, which exposes decoded audio through buffers.

Fallback priority:

```text
FFmpeg
QAudioDecoder
timeline-only
```

Waveform failure must not disable bookmarking.

---

# 190. Packaging

Initial PyInstaller:

```text
onedir
```

Recommended because:

```text
Qt plugins easier to diagnose
FFmpeg easier to package
faster startup
clear runtime structure
```

Later evaluate onefile.

PyInstaller must be executed on Windows for the Windows build; its documentation explicitly notes it is not a cross-compiler.

---

# 191. Installer Upgrades

Upgrade must preserve:

```text
SQLite DB
waveform cache unless incompatible
settings
bridge authentication
exports
logs
```

Installer may replace:

```text
application binaries
Lua integration file
```

---

# 192. Uninstall

Ask:

```text
Remove application only
or
Remove all Bookmark Studio data
```

Default:

```text
preserve user bookmark database
```

---

# 193. First Run

Wizard:

```text
VLC Bookmark Studio

VLC detected:
C:\Program Files\VideoLAN\VLC\vlc.exe

Integration:
Not installed

[Install Integration]

FFmpeg:
Bundled

Bookmark database:
...

[Finish]
```

After finish:

```text
launch/connect VLC
```

---

# 194. Smart UX Summary

The final UX combines:

**Audacity**

```text
drag selection
[
]
Ctrl+B
precise selection values
```

**Adobe Audition**

```text
point markers
range markers
draggable start/end
```

**Sonic Visualiser**

```text
annotation layers
editable regions
snapping
```

**Peaks.js**

```text
overview waveform
zoom waveform
segments
points
dragging
external player abstraction
```

These references directly support the planned interaction model.

---

# 195. Three-Pass Design Review Result

## Pass 1 — Functional completeness

Verified inclusion of:

```text
playlist recognition
song-level bookmark context
playlist mutation handling
duplicate songs
ad-hoc playlists
visual painting
point/range bookmarks
looping
transport
exact times
save/load
autosave
waveform
zoom
overview
keyboard control
recovery
import/export
offline operation
```

## Pass 2 — API/architecture correctness

Key corrections:

```text
Do not build final UI inside VLC Lua dialogs.
Use Python/PySide6 for UI.
Use VLC Lua interface rather than extension dialog.
Use VLC's httpd interface support instead of Windows-dependent
nonblocking raw-socket mechanisms.
Use VLC microsecond time/seek APIs through custom bridge.
Keep standard VLC HTTP API as fallback.
Do not assume VLC 4 A-B APIs are directly callable from current Lua.
Keep playback adapter isolated.
```

VLC currently exposes microsecond `player.get_time()` and `seek_by_time_absolute()` through Lua, making this bridge design technically appropriate.

## Pass 3 — Coding/testability

Added or clarified:

```text
package structure
classes
protocol
database schema
indices
state machines
poll frequencies
coordinate system
worker model
cache structure
transactions
undo/redo
security
installer
migration
tests
performance targets
failure states
acceptance criteria
```

---

# 196. Final Architecture Decision

The architecture to code should therefore be:

```text
                   VLC BOOKMARK STUDIO
                       Python 3
                           │
       ┌───────────────────┼──────────────────┐
       │                   │                  │
       ▼                   ▼                  ▼
    PySide6            Domain Logic         SQLite
       │                   │
       │             ┌─────┴──────────┐
       │             │                │
       ▼             ▼                ▼
Waveform Editor  Playlist Engine  Bookmark Engine
       │             │                │
       └─────────────┼────────────────┘
                     │
                     ▼
              Playback Adapter
                     │
              ┌──────┴───────┐
              │              │
              ▼              ▼
        Enhanced Bridge   VLC HTTP
              │           fallback
              │
              ▼
        bookmarkstudio.lua
              │
              ▼
         VLC Media Player
```

The architectural invariant is:

> VLC performs playback. Python performs the product logic.

---

# 197. Release 1.0 Definition of Done

Version 1.0 is acceptable only when all of the following work together in a packaged Windows build:

```text
VLC playlist automatically detected
known playlist automatically recognized
current song automatically resolved
correct playlist/song bookmark set automatically loaded
switching playlist changes bookmark context
switching song changes bookmark set
same song in two playlists retains different bookmarks
paint waveform selection works
point bookmark works
segment bookmark works
drag/resize works
timestamp editing works
bookmark playback works
bookmark loop works
fixed/infinite repeats work
previous/next bookmark works
VLC Play/Pause/Stop/Next/Previous works
waveform cache works
application restart retains data
VLC restart reconnects
moved file can be recovered
project export/import works
undo/redo works
installer configures the VLC integration
tests pass
```

The **playlist + song bookmark isolation test** is a release blocker.

---

# 198. Product Statement

**VLC Bookmark Studio is a playlist-aware visual segment manager for VLC that automatically recognizes the active playlist and song, displays a navigable waveform, lets users paint, name, edit, replay and loop timed regions, and persists a separate bookmark collection for each song within each playlist.**

This statement should remain the governing scope for version 1.x.
