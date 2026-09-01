--[[
bookmarkstudio.lua — VLC Lua interface, thin HTTP bridge for VLC Bookmark Studio.

Install location (spec #153): <VLC user config>\lua\intf\bookmarkstudio.lua
Launch (spec #154-#155): vlc --extraintf=luaintf --lua-intf=bookmarkstudio

This script is intentionally thin (spec #2, #18-#19): it exposes VLC's
playlist/player state and transport controls over a localhost-only,
authenticated JSON HTTP API under /bookmarkstudio/v1/. All business logic
(playlist recognition, bookmarks, waveform, undo/redo, persistence) lives
in the Python application, never here.

Endpoints (spec #22-#27):
  GET /bookmarkstudio/v1/health
  GET /bookmarkstudio/v1/status
  GET /bookmarkstudio/v1/playlist
  GET /bookmarkstudio/v1/control?command=play|pause|stop|next|previous|goto&id=<n>
  GET /bookmarkstudio/v1/seek?time_us=<n>
  GET /bookmarkstudio/v1/rate?value=<float>

Security (spec #20-#21, #158-#159):
  - binds 127.0.0.1 only, never 0.0.0.0
  - HTTP Basic Auth with a random per-install token (never logged)
  - whitelist of commands only, all numeric params validated
]]

-- TODO: implement. This is a scaffolding placeholder (spec #116 file layout);
-- see PROJECT_SPEC.md sections 18-28 for the full bridge design before writing
-- the vlc.httpd() handler and JSON encode/decode helpers.
