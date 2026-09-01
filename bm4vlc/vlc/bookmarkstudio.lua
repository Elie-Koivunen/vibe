--[[
bookmarkstudio.lua -- VLC Lua interface, thin HTTP bridge for VLC Bookmark Studio.

Install location (spec #153): <VLC user config>\lua\intf\bookmarkstudio.lua
Launch (spec #154-#155): vlc --extraintf=luaintf --lua-intf=bookmarkstudio

Intentionally thin (spec #2, #18-#19): exposes VLC's playlist/player state and
transport controls over a localhost-only, authenticated JSON HTTP API under
/bookmarkstudio/v1/. All business logic (playlist recognition, bookmarks, waveform,
undo/redo, persistence) lives in the Python application, never here.

Security (spec #20-#21, #158-#159):
  - listens only where the launching process told VLC's core httpd to listen. VLC's
    `vlc.httpd()` (below) does NOT let a Lua script pick its own host/port -- it
    returns VLC's single shared HTTP daemon, bound according to VLC's own
    `--http-host`/`--http-port` startup flags (default, if unset: ALL interfaces, port
    8080 -- confirmed live against VLC 3.0.23: `vlc.httpd()` alone produced "listening
    to * port 8080" in VLC's own log). This means the PYTHON LAUNCHER, not this script,
    is what must always pass `--http-host=127.0.0.1 --http-port=<port>` (spec #155) --
    see app/vlc_launcher.py's launch_managed_vlc(). This script cannot enforce that on
    its own; it can only trust it was launched correctly.
  - HTTP Basic Auth against a token read from a per-user config file the Python app
    writes just before launching a managed VLC instance (spec #155's "secure
    configuration mechanism rather than command-line plaintext") -- never logged.
    NOTE: an earlier version tried `vlc.getenv(...)`, which does not exist in VLC's Lua
    API (confirmed live). Reading a config file is the verified-working alternative;
    vlc.config.configdir() (verified live: resolves to %APPDATA%\vlc on Windows, i.e.
    exactly spec #153's "<VLC user config>") gives a script-independent, well-known
    location for it. Use forward slashes in the path: a literal backslash followed by
    a letter is an invalid Lua string escape and silently produced a load failure here
    when this used "\\" -- also caught live.
  - a fixed whitelist of /control commands only; every numeric parameter is validated
    before being handed to a vlc.* call

Response API (verified live against VLC 3.0.23, replacing an earlier version that
guessed wrong -- see PROJECT_SPEC.md-adjacent debug notes in README.md):
  - httpd:handler()'s callback signature is `function(data, url, request, type,
    in_data)`. `request` is NOT a table/object -- it is `nil` when the request has no
    query string, or the raw query string itself (e.g. "command=play&id=3") when it
    does. There is no `request.client` and no `request.psz_args`; the original code
    assumed both and crashed every real request with "attempt to index a nil value"
    (an uncaught error that reset the TCP connection, which read from curl/Python as a
    bare connection-reset with no HTTP response at all).
  - The callback's return value is the plain response BODY STRING. There is no way to
    set an HTTP status code or Content-Type header found through this API in testing
    (a second return value is silently ignored) -- every response is HTTP 200, and
    success/failure is carried in the JSON body's "ok" field instead, which is exactly
    what BridgeClient (playback/bridge_client.py) already checks first, so no error
    ever gets misread as success on the Python side.
]]

local httpd = vlc.httpd()

local function read_config_file()
    local config_path = vlc.config.configdir() .. "/bookmarkstudio_bridge.conf"
    local values = {}
    local file = io.open(config_path, "r")
    if file then
        for line in file:lines() do
            local key, value = string.match(line, "^(%a+)=(.*)$")
            if key then
                values[key] = value
            end
        end
        file:close()
    end
    return values
end

local CONFIG = read_config_file()
local TOKEN = CONFIG.token or ""
local USERNAME = "bookmarkstudio"
local PROTOCOL_VERSION = 1
local BRIDGE_VERSION = "1.0.0"

-- ---------------------------------------------------------------------------
-- JSON encoding (minimal, deliberately not a general-purpose library: only the
-- shapes this bridge actually emits -- strings, numbers, booleans, nil, flat
-- arrays/objects of the above).
-- ---------------------------------------------------------------------------

local function json_escape(s)
    s = string.gsub(s, "\\", "\\\\")
    s = string.gsub(s, "\"", "\\\"")
    s = string.gsub(s, "\n", "\\n")
    s = string.gsub(s, "\r", "\\r")
    s = string.gsub(s, "\t", "\\t")
    return s
end

local function json_encode(value)
    local t = type(value)
    if value == nil then
        return "null"
    elseif t == "boolean" then
        return value and "true" or "false"
    elseif t == "number" then
        return tostring(value)
    elseif t == "string" then
        return "\"" .. json_escape(value) .. "\""
    elseif t == "table" then
        if value[1] ~= nil or next(value) == nil then
            -- Array (or empty table, treated as an empty array -- this bridge never
            -- emits an intentionally-empty JSON object).
            local parts = {}
            for _, item in ipairs(value) do
                table.insert(parts, json_encode(item))
            end
            return "[" .. table.concat(parts, ",") .. "]"
        else
            local parts = {}
            for key, item in pairs(value) do
                table.insert(parts, "\"" .. json_escape(tostring(key)) .. "\":" .. json_encode(item))
            end
            return "{" .. table.concat(parts, ",") .. "}"
        end
    end
    return "null"
end

-- ---------------------------------------------------------------------------
-- Response helpers
-- ---------------------------------------------------------------------------

local function ok_json(body_table)
    body_table = body_table or {}
    body_table.ok = true
    return json_encode(body_table)
end

local function error_json(code, message)
    return json_encode({ ok = false, error = { code = code, message = message } })
end

-- ---------------------------------------------------------------------------
-- Validation helpers (spec #158-#159: whitelist only, validate every parameter)
-- ---------------------------------------------------------------------------

local ALLOWED_COMMANDS = {
    play = true, pause = true, stop = true, next = true, previous = true, goto = true,
}

local function parse_int(raw)
    if raw == nil then
        return nil
    end
    local n = tonumber(raw)
    if n == nil or n ~= math.floor(n) then
        return nil
    end
    return n
end

-- ---------------------------------------------------------------------------
-- Endpoint handlers. Each receives (client, request-query-table) and returns a
-- JSON body string via ok_json/error_json.
-- ---------------------------------------------------------------------------

local function handle_health(_query)
    return ok_json({
        protocol_version = PROTOCOL_VERSION,
        vlc_version = vlc.misc.version(),
        bridge_version = BRIDGE_VERSION,
    })
end

-- vlc.playlist.get("playlist", false) has NO `.current` field (confirmed live by
-- dumping the whole table -- it has .flags/.name/.item/.id/.duration/.children/
-- .nb_played and nothing identifying "the playing one"). The only reliable signal is
-- matching the currently playing input item's URI against each child's `.path`.
local function find_current_playlist_id(current_uri)
    if current_uri == nil then
        return nil
    end
    local pl = vlc.playlist.get("playlist", false)
    if pl and pl.children then
        for _, node in ipairs(pl.children) do
            if node.path == current_uri then
                return node.id
            end
        end
    end
    return nil
end

local function handle_status(_query)
    local input = vlc.object.input()
    if input == nil then
        return error_json("NO_MEDIA", "no media is currently loaded")
    end

    local item = vlc.input.item()
    local current_uri = item and item:uri() or nil
    local state = vlc.playlist.status()
    local time_us = vlc.var.get(input, "time")       -- microseconds (spec #24)
    local length_us = vlc.var.get(input, "length")
    local position = vlc.var.get(input, "position")
    local rate = vlc.var.get(input, "rate")

    return ok_json({
        state = state,
        time_us = time_us or 0,
        position = position or 0.0,
        rate = rate or 1.0,
        current_playlist_item_id = find_current_playlist_id(current_uri),
        duration_us = (length_us and length_us > 0) and length_us or nil,
        media_uri = current_uri,
    })
end

local function handle_playlist(_query)
    local pl = vlc.playlist.get("playlist", false)
    local items = {}
    if pl and pl.children then
        for _, node in ipairs(pl.children) do
            if node.item then
                -- node.duration (a plain number, seconds) is a real, verified-live
                -- field on each playlist child -- simpler and more reliable than
                -- calling node.item:duration(), which needed a pcall guard because
                -- ':duration and :duration()' (an earlier version) is invalid Lua
                -- colon-call syntax and errored at script-load time.
                local duration_s = (type(node.duration) == "number" and node.duration >= 0) and node.duration or nil
                table.insert(items, {
                    vlc_id = node.id,
                    uri = node.path or (node.item:uri() or ""),
                    name = node.name or (node.item:name() or ""),
                    duration_s = duration_s,
                })
            end
        end
    end
    return ok_json({ current_id = pl and pl.current or nil, items = items })
end

local function handle_control(query)
    local command = query["command"]
    if command == nil or not ALLOWED_COMMANDS[command] then
        return error_json("INVALID_REQUEST", "unknown or missing command")
    end

    if command == "play" then
        vlc.playlist.play()
    elseif command == "pause" then
        vlc.playlist.pause()
    elseif command == "stop" then
        vlc.playlist.stop()
    elseif command == "next" then
        vlc.playlist.next()
    elseif command == "previous" then
        vlc.playlist.prev()
    elseif command == "goto" then
        local id = parse_int(query["id"])
        if id == nil then
            return error_json("INVALID_ITEM", "id must be a non-negative integer")
        end
        vlc.playlist.goto(id)
    end

    return ok_json()
end

local function handle_seek(query)
    local time_us = parse_int(query["time_us"])
    if time_us == nil or time_us < 0 then
        return error_json("INVALID_TIME", "time_us must be a non-negative integer")
    end

    local input = vlc.object.input()
    if input == nil then
        return error_json("NO_MEDIA", "no media is currently loaded")
    end

    -- Clamp to known duration if available (spec #159), rather than passing an
    -- out-of-range value straight to VLC.
    local length_us = vlc.var.get(input, "length")
    if length_us and length_us > 0 and time_us > length_us then
        time_us = length_us
    end

    -- `vlc.player.seek_by_time_absolute` (spec #196's own reference) does not exist in
    -- VLC 3.0.23's Lua API at all (confirmed live: `vlc.player` is nil) -- every /seek
    -- request silently hung with no response at all until this was found. Setting the
    -- "time" input variable directly is VLC's actual seek mechanism and is already used
    -- the same way to *read* time/length/rate elsewhere in this file; confirmed live
    -- that vlc.var.set(input, "time", us) immediately moves playback position.
    vlc.var.set(input, "time", time_us)
    return ok_json()
end

local function handle_rate(query)
    local value = tonumber(query["value"])
    if value == nil or value <= 0 then
        return error_json("INVALID_REQUEST", "value must be a positive number")
    end
    local input = vlc.object.input()
    if input == nil then
        return error_json("NO_MEDIA", "no media is currently loaded")
    end
    vlc.var.set(input, "rate", value)
    return ok_json()
end

-- ---------------------------------------------------------------------------
-- Route table + dispatch
-- ---------------------------------------------------------------------------

local ROUTES = {
    ["/bookmarkstudio/v1/health"] = handle_health,
    ["/bookmarkstudio/v1/status"] = handle_status,
    ["/bookmarkstudio/v1/playlist"] = handle_playlist,
    ["/bookmarkstudio/v1/control"] = handle_control,
    ["/bookmarkstudio/v1/seek"] = handle_seek,
    ["/bookmarkstudio/v1/rate"] = handle_rate,
}

local function dispatch(url, query)
    local handler = ROUTES[url]
    if handler == nil then
        return error_json("INVALID_REQUEST", "unknown endpoint: " .. tostring(url))
    end
    local success, result = pcall(handler, query or {})
    if not success then
        return error_json("INTERNAL_ERROR", tostring(result))
    end
    return result
end

-- Query string parsing: `request` is nil (no query) or the raw query string itself,
-- e.g. "command=play&id=3" -- NOT a table/object with a .psz_args field. Confirmed
-- live by dumping the callback's real arguments against VLC 3.0.23; an earlier
-- version assumed a request object with a nested .client, which does not exist and
-- crashed (uncaught "attempt to index a nil value") on every single real request.
local function parse_query_string(request)
    local query = {}
    if type(request) == "string" then
        for key, value in string.gmatch(request, "([^&=?]+)=([^&]*)") do
            query[key] = value
        end
    end
    return query
end

for url, _ in pairs(ROUTES) do
    httpd:handler(
        url,
        USERNAME,
        TOKEN,
        function(_data, request_url, request, _request_type, _in_data)
            return dispatch(request_url, parse_query_string(request))
        end,
        nil
    )
end

-- Actual host:port is whatever VLC's own --http-host/--http-port were launched with
-- (see the Security note above) -- this script has no way to know or report that here.
vlc.msg.info("[bookmarkstudio] handlers registered under /bookmarkstudio/v1/ "
    .. "(listening address is controlled by VLC's --http-host/--http-port)")
