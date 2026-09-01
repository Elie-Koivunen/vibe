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

local function send_json(client, status_code, body_table)
    local body = json_encode(body_table)
    client:set_status(status_code)
    client:add_header("Content-Type", "application/json")
    client:add_header("Cache-Control", "no-store")
    return body
end

local function ok_json(client, body_table)
    body_table = body_table or {}
    body_table.ok = true
    return send_json(client, 200, body_table)
end

local function error_json(client, http_status, code, message)
    return send_json(client, http_status, { ok = false, error = { code = code, message = message } })
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

local function handle_health(client, _query)
    return ok_json(client, {
        protocol_version = PROTOCOL_VERSION,
        vlc_version = vlc.misc.version(),
        bridge_version = BRIDGE_VERSION,
    })
end

local function handle_status(client, _query)
    local input = vlc.object.input()
    if input == nil then
        return error_json(client, 200, "NO_MEDIA", "no media is currently loaded")
    end

    local item = vlc.input.item()
    local state = vlc.playlist.status()
    local time_us = vlc.var.get(input, "time")       -- microseconds (spec #24)
    local length_us = vlc.var.get(input, "length")
    local position = vlc.var.get(input, "position")
    local rate = vlc.var.get(input, "rate")
    local playlist_status = vlc.playlist.get("playlist", false)
    local current_id = playlist_status and playlist_status.current or nil

    return ok_json(client, {
        state = state,
        time_us = time_us or 0,
        position = position or 0.0,
        rate = rate or 1.0,
        current_playlist_item_id = current_id,
        duration_us = (length_us and length_us > 0) and length_us or nil,
        media_uri = item and item:uri() or nil,
    })
end

local function handle_playlist(client, _query)
    local pl = vlc.playlist.get("playlist", false)
    local items = {}
    if pl and pl.children then
        for _, node in ipairs(pl.children) do
            if node.item then
                -- ':duration and :duration()' is invalid Lua (colon-call syntax needs
                -- immediate parens) and pcall guards against it not existing/erroring
                -- on every VLC build regardless -- verified live against VLC 3.0.23,
                -- which rejected the naive version with a parse error at load time.
                local duration_s = nil
                local called_ok, duration_value = pcall(function() return node.item:duration() end)
                if called_ok then
                    duration_s = duration_value
                end
                table.insert(items, {
                    vlc_id = node.id,
                    uri = node.item:uri() or "",
                    name = node.item:name() or "",
                    duration_s = duration_s,
                })
            end
        end
    end
    return ok_json(client, { current_id = pl and pl.current or nil, items = items })
end

local function handle_control(client, query)
    local command = query["command"]
    if command == nil or not ALLOWED_COMMANDS[command] then
        return error_json(client, 400, "INVALID_REQUEST", "unknown or missing command")
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
            return error_json(client, 400, "INVALID_ITEM", "id must be a non-negative integer")
        end
        vlc.playlist.goto(id)
    end

    return ok_json(client)
end

local function handle_seek(client, query)
    local time_us = parse_int(query["time_us"])
    if time_us == nil or time_us < 0 then
        return error_json(client, 400, "INVALID_TIME", "time_us must be a non-negative integer")
    end

    local input = vlc.object.input()
    if input == nil then
        return error_json(client, 200, "NO_MEDIA", "no media is currently loaded")
    end

    -- Clamp to known duration if available (spec #159), rather than passing an
    -- out-of-range value straight to VLC.
    local length_us = vlc.var.get(input, "length")
    if length_us and length_us > 0 and time_us > length_us then
        time_us = length_us
    end

    vlc.player.seek_by_time_absolute(time_us)
    return ok_json(client)
end

local function handle_rate(client, query)
    local value = tonumber(query["value"])
    if value == nil or value <= 0 then
        return error_json(client, 400, "INVALID_REQUEST", "value must be a positive number")
    end
    local input = vlc.object.input()
    if input == nil then
        return error_json(client, 200, "NO_MEDIA", "no media is currently loaded")
    end
    vlc.var.set(input, "rate", value)
    return ok_json(client)
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

local function dispatch(client, url, query)
    local handler = ROUTES[url]
    if handler == nil then
        return error_json(client, 404, "INVALID_REQUEST", "unknown endpoint: " .. tostring(url))
    end
    local success, result = pcall(handler, client, query or {})
    if not success then
        return error_json(client, 500, "INTERNAL_ERROR", tostring(result))
    end
    return result
end

-- vlc.httpd()'s handler callback signature: (data, url, request, type, in_data)
-- request/in_data query-string parsing is handled by VLC's own request object; if
-- that facility isn't available in a given VLC build, query values simply come back
-- nil and every handler's own validation already rejects nil/missing parameters.
for url, _ in pairs(ROUTES) do
    httpd:handler(
        url,
        USERNAME,
        TOKEN,
        function(data, request_url, request, request_type, in_data)
            local query = {}
            if request and request.psz_args then
                for key, value in string.gmatch(request.psz_args, "([^&=?]+)=([^&]*)") do
                    query[key] = value
                end
            end
            return dispatch(request.client, request_url, query)
        end,
        nil
    )
end

-- Actual host:port is whatever VLC's own --http-host/--http-port were launched with
-- (see the Security note above) -- this script has no way to know or report that here.
vlc.msg.info("[bookmarkstudio] handlers registered under /bookmarkstudio/v1/ "
    .. "(listening address is controlled by VLC's --http-host/--http-port)")
