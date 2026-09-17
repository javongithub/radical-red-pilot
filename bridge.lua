-- Radical Red Pilot: bounded local control for mGBA 0.10.5 / 0.11.
-- The supplied development build auto-loads this through --script.
-- Uses the official API: https://mgba.io/docs/scripting.html
-- No game-memory writes, cheats, save-state loads, or save-file operations.

if RR_PILOT_BRIDGE and RR_PILOT_BRIDGE.stop then
    RR_PILOT_BRIDGE.stop()
end

local scriptDirectory = debug and debug.getinfo(1, "S").source:match("^@(.+)/[^/]+$")
-- Set RR_PILOT_RUNTIME to override; otherwise this resolves next to the script,
-- falling back to a relative path when mGBA does not expose the script source.
local runtime = RR_PILOT_RUNTIME or (scriptDirectory and scriptDirectory .. "/runtime")
    or "runtime"
local bridge = {sock = nil, epoch = 0, mask = 0, remaining = 0,
    rx = "", tx = "", ticks = 0, callbackIds = {}, stopped = false, manualLatched = false}
RR_PILOT_BRIDGE = bridge

local function clean(value)
    return tostring(value):gsub("[\r\n\t]", " ")
end

local function diagnostic(message)
    local line = tostring(os.time()) .. " frame=" .. tostring(emu and emu:currentFrame() or -1)
        .. " epoch=" .. tostring(bridge.epoch) .. " " .. clean(message)
    local handle = io.open(runtime .. "/bridge.log", "a")
    if handle then handle:write(line .. "\n"); handle:close() end
    console:log("Pilot: " .. clean(message))
end

local function release()
    if emu and bridge.mask ~= 0 then emu:setKeys(0) end
    bridge.mask = 0
    bridge.remaining = 0
end

local function disconnect(reason)
    if bridge.sock then diagnostic("disconnect: " .. tostring(reason or "lifecycle")) end
    release()
    if bridge.sock then
        local old = bridge.sock
        bridge.sock = nil
        pcall(function() old:close() end)
    end
    bridge.rx = ""
    bridge.tx = ""
end

local function flush()
    if not bridge.sock or #bridge.tx == 0 then return end
    local last, err = bridge.sock:send(bridge.tx)
    if last then
        bridge.tx = bridge.tx:sub(last + 1)
    elseif err ~= socket.ERRORS.AGAIN then
        disconnect("send error " .. tostring(err))
    end
end

local function send(line)
    if not bridge.sock then return end
    if #bridge.tx + #line > 32768 then disconnect("transmit buffer overflow " .. #bridge.tx); return end
    bridge.tx = bridge.tx .. line .. "\n"
    flush()
end

local function picture()
    if not emu then return end
    local temp = runtime .. "/frame-next.png"
    local ok, err = pcall(function()
        emu:screenshot(temp)
        local moved, why = os.rename(temp, runtime .. "/frame.png")
        if not moved then error(why or "screenshot rename failed") end
    end)
    if not ok then send("ERROR\tSCREEN " .. clean(err)) end
end

local function integer(value, low, high)
    local n = tonumber(value)
    if n and n == math.floor(n) and n >= low and n <= high then return n end
end

local function readable(address, count)
    local last = address + count
    return (address >= 0x02000000 and last <= 0x02040000)
        or (address >= 0x03000000 and last <= 0x03008000)
        or (address >= 0x08000000 and last <= 0x08000000 + math.min(emu:romSize(), 0x02000000))
end

local function command(line)
    local args = {}
    for field in (line .. "\t"):gmatch("([^\t]*)\t") do args[#args + 1] = field end
    local op = args[1]
    if op == "KEYS" then
        local mask = integer(args[2], 0, 1023)
        local frames = integer(args[3], 1, 120)
        local epoch = integer(args[4], 0, 9007199254740991)
        if #args ~= 4 or not mask or not frames or not epoch then
            send("ERROR\tInvalid KEYS"); return
        end
        if epoch < bridge.epoch then send("ERROR\tStale epoch"); return end
        release()
        bridge.epoch = epoch
        bridge.manualLatched = false
        bridge.mask = mask
        bridge.remaining = frames
        emu:setKeys(mask)
        send("ACK\t" .. epoch .. "\tKEYS")
    elseif op == "RELEASE" then
        local epoch = integer(args[2], 0, 9007199254740991)
        if #args ~= 2 or not epoch then send("ERROR\tInvalid RELEASE"); return end
        if epoch < bridge.epoch then send("ERROR\tStale epoch"); return end
        release()
        bridge.epoch = epoch
        send("ACK\t" .. epoch .. "\tRELEASE")
    elseif op == "SCREEN" and #args == 1 then
        picture()
        send("FRAME\t" .. emu:currentFrame() .. "\t" .. bridge.epoch .. "\t" .. bridge.mask)
    elseif op == "READ" then
        local address = integer(args[2], 0, 0xFFFFFFFF)
        local count = integer(args[3], 1, 1024)
        local id = args[4]
        if #args ~= 4 or not address or not count or not id or #id > 64
            or not id:match("^[%w_.-]+$") or not readable(address, count) then
            send("ERROR\tInvalid READ"); return
        end
        local bytes = emu:readRange(address, count)
        local hex = bytes:gsub(".", function(c) return string.format("%02x", string.byte(c)) end)
        send("DATA\t" .. id .. "\t" .. hex)
    else
        send("ERROR\tUnknown command")
    end
end

local function receive()
    if not bridge.sock then return end
    -- Bounded work per callback, even if a local client floods the socket.
    for _ = 1, 8 do
        local packet, err = bridge.sock:receive(4096)
        if not packet or #packet == 0 then
            if err ~= socket.ERRORS.AGAIN then disconnect("receive " .. tostring(err) .. " packet=" .. tostring(packet)) end
            return
        end
        bridge.rx = bridge.rx .. packet
        if #bridge.rx > 32768 then disconnect("receive buffer overflow"); return end
        while true do
            local boundary = bridge.rx:find("\n", 1, true)
            if not boundary then break end
            local line = bridge.rx:sub(1, boundary - 1):gsub("\r$", "")
            bridge.rx = bridge.rx:sub(boundary + 1)
            if #line > 1024 then disconnect("command too long"); return end
            local ok, why = pcall(command, line)
            if not ok then
                release()
                send("ERROR\tCommand failed " .. clean(why))
            end
            if not bridge.sock then return end
        end
    end
end

local function connect()
    if bridge.stopped or bridge.sock or not emu then return end
    local sock = socket.tcp()
    local ok = sock:connect("127.0.0.1", 8766)
    if not ok then sock:close(); return end
    bridge.sock = sock
    bridge.rx = ""
    bridge.tx = ""
    sock:add("received", receive)
    sock:add("error", function(_, err) disconnect("socket event " .. tostring(err)) end)
    send("HELLO\t1\t" .. clean(emu:getGameCode()) .. "\t" .. clean(emu:getGameTitle()))
    picture()
    send("FRAME\t" .. emu:currentFrame() .. "\t" .. bridge.epoch .. "\t0")
    diagnostic("connected to localhost:8766")
end

local function frame()
    bridge.ticks = bridge.ticks + 1
    -- Wall-clock polling can miss an entire faint animation in fast-forward.
    -- Observe HP each emulated frame and send the actual zero-HP snapshot.
    bridge.partyHP = bridge.partyHP or {}
    local count = emu:read8(0x02024029)
    if count > 0 and count <= 6 then
        for slot = 0, count - 1 do
            local address = 0x02024284 + slot * 100
            local species = emu:read16(address + 32)
            local hp, maximum = emu:read16(address + 86), emu:read16(address + 88)
            if species > 0 and species < 1376 and maximum > 0 and maximum <= 999 and hp <= maximum then
                local identity = tostring(emu:read32(address)) .. ':' .. tostring(emu:read32(address + 4))
                if hp == 0 and bridge.partyHP[identity] ~= 0 then
                    local bytes = emu:readRange(0x02024284, 600)
                    local hex = bytes:gsub('.', function(c) return string.format('%02x', string.byte(c)) end)
                    bridge.pendingFaint = bridge.pendingFaint or {}
                    bridge.pendingFaint[identity] = 'PARTY_ZERO\t' .. count .. '\t' .. hex
                end
                bridge.partyHP[identity] = hp
            end
        end
    end
    if bridge.sock and bridge.pendingFaint then
        for identity, packet in pairs(bridge.pendingFaint) do
            send(packet)
            bridge.pendingFaint[identity] = nil
        end
    end
    if bridge.remaining > 0 then
        bridge.remaining = bridge.remaining - 1
        if bridge.remaining == 0 then
            release()
            send("DONE\t" .. bridge.epoch .. "\t" .. emu:currentFrame())
        end
    end
    if not bridge.sock then
        if bridge.ticks % 120 == 0 then connect() end
    else
        flush()
    end
    if bridge.ticks % 30 == 0 then
        picture()
        send("FRAME\t" .. emu:currentFrame() .. "\t" .. bridge.epoch .. "\t" .. bridge.mask)
    end
end

local function manual(source)
    if bridge.manualLatched then return end
    release()
    bridge.epoch = bridge.epoch + 1
    bridge.manualLatched = true
    send("MANUAL\t" .. bridge.epoch .. "\t" .. source)
    diagnostic("native " .. source .. " input took control")
end

function bridge.stop()
    bridge.stopped = true
    disconnect()
    for _, id in ipairs(bridge.callbackIds) do callbacks:remove(id) end
    bridge.callbackIds = {}
end

local function add(name, fn)
    bridge.callbackIds[#bridge.callbackIds + 1] = callbacks:add(name, fn)
end
add("frame", frame)
add("keysRead", function()
    if bridge.remaining > 0 then emu:setKeys(bridge.mask) end
end)
add("start", connect)
add("reset", function() disconnect(); connect() end)
add("stop", disconnect)
add("crashed", disconnect)
add("shutdown", disconnect)
-- mGBA 0.11 exposes actual host input separately from emulated keys.
-- emu:setKeys does not emit these events, so AI presses never cause takeover.
if input and C and C.INPUT_STATE then
    add("key", function(event)
        if event.state == C.INPUT_STATE.DOWN then manual("keyboard") end
    end)
    add("gamepadButton", function(event)
        if event.state == C.INPUT_STATE.DOWN then manual("gamepad") end
    end)
    add("gamepadHat", function(event)
        if event.direction ~= 0 then manual("gamepad") end
    end)
    add("frame", function()
        local pad = input.activeGamepad
        if not pad or bridge.manualLatched then return end
        -- The first two axes are the left stick; a generous dead zone avoids drift.
        for index = 1, math.min(#pad.axes, 2) do
            if math.abs(pad.axes[index]) > 16000 then manual("gamepad"); return end
        end
    end)
end
connect()
console:log("Radical Red Pilot bridge loaded. Button presses are limited to 120 frames.")
