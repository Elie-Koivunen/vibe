# VLC Bookmark Studio (bm4vlc)

A playlist-aware visual bookmarking, segment-selection, navigation and
looping system for VLC Media Player. See [PROJECT_SPEC.md](PROJECT_SPEC.md)
for the full design and engineering specification (198 sections).

## Status

Early scaffolding. Package layout, domain model, and DB schema are in
place; VLC integration (Lua bridge, HTTP fallback), waveform pipeline, and
the PySide6 UI are not yet implemented.

## Architecture

```text
PySide6 UI  +  Domain Logic  +  SQLite
                    |
             Playback Adapter
              /            \
   Enhanced Lua Bridge   VLC HTTP fallback
              \            /
               VLC Media Player
```

VLC performs playback; Python performs the product logic. Full rationale
in PROJECT_SPEC.md sections 2, 18-30, 196.

## Layout

```text
src/bookmark_studio/   application package (see PROJECT_SPEC.md #116)
vlc/bookmarkstudio.lua thin VLC Lua HTTP bridge (spec #18-#27)
migrations/            SQLite schema migrations (spec #126)
tests/                 unit / integration / ui / vlc / fixtures
```

## Setup

```bash
pip install -e ".[dev]"
pytest
```

## Non-goals

Not a DAW, not a sample-accurate audio editor, not a replacement media
player. Original media is never modified. See spec #177.
