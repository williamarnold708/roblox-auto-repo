--!nonstrict
--[[
EventLogger (ModuleScript) - RobloxAutoPromo
Put this ModuleScript in ServerScriptService and start it from a server Script:

    local EventLogger = require(game.ServerScriptService.EventLogger)
    EventLogger.start({
        recordUserIds = {},          -- empty = log every player (fine for solo Studio tests)
        httpUrl = nil,               -- e.g. "http://localhost:8765/events" (Studio only, see README)
        chatCommands = true,         -- "/rec" sync marker, "/stop", "/mark <kind> [detail]"
    })
    -- anywhere in your server code:
    EventLogger.mark("victory", "reached the summit")
    EventLogger.mark("boss_defeat", "Lava King")
    EventLogger.mark("rare_item", "Golden Sword")
    EventLogger.mark("checkpoint", "stage 12")
    EventLogger.mark("unexpected", "physics launch")

Honest limits:
  * Roblox games cannot write files. Output is (a) JSON lines printed to the
    Output window with the prefix [AutoPromoEvent] (copy them into a text file
    next to your recording), and optionally (b) batched HttpService POSTs.
  * HttpService only runs on the server and only if Game Settings > Security >
    "Allow HTTP Requests" is on. Live Roblox servers cannot reach your PC's
    localhost or private IPs, so (b) is only for Studio play-testing on the
    same machine, and even there verify it works on your setup.
  * Timestamps are seconds since EventLogger.start() using
    workspace:GetServerTimeNow(); the "/rec" marker lets the Python side line
    them up with your OBS recording. Nothing here controls or automates players.
]]

local HttpService = game:GetService("HttpService")
local Players = game:GetService("Players")
local RunService = game:GetService("RunService")

local EventLogger = {}
EventLogger.PREFIX = "[AutoPromoEvent]"
EventLogger.VERSION = 1

local KINDS = {
	session_start = true, recording_start = true, recording_stop = true, death = true,
	victory = true, checkpoint = true, rare_item = true, boss_defeat = true,
	high_score = true, unexpected = true, custom = true,
}

local config = {
	recordUserIds = {} :: {number},
	httpUrl = nil :: string?,
	httpBatchSeconds = 5,
	chatCommands = true,
	trackDeaths = true,
	trackLeaderstats = true,
	highScoreCooldown = 3,
}
local started = false
local sessionStart = 0
local buffer: {any} = {}
local lastCommandAt: {[number]: number} = {}
local bestStat: {[string]: number} = {}
local lastHighScoreAt: {[string]: number} = {}

local function now(): number
	return workspace:GetServerTimeNow()
end

local function isTracked(player: Player?): boolean
	if player == nil then return true end
	if #config.recordUserIds == 0 then return true end
	return table.find(config.recordUserIds, player.UserId) ~= nil
end

local function emit(kind: string, detail: string?, extra: {[string]: any}?)
	if not started then
		warn("EventLogger.mark called before EventLogger.start(); ignored")
		return
	end
	if not KINDS[kind] then
		detail = kind .. (detail and (": " .. detail) or "")
		kind = "custom"
	end
	local ev: {[string]: any} = {
		v = EventLogger.VERSION,
		t = math.floor((now() - sessionStart) * 1000 + 0.5) / 1000,
		kind = kind,
		detail = detail and string.sub(tostring(detail), 1, 200) or "",
	}
	if extra then
		for k, v in extra do ev[k] = v end
	end
	local ok, line = pcall(HttpService.JSONEncode, HttpService, ev)
	if ok then
		print(EventLogger.PREFIX .. " " .. line)
		if config.httpUrl then table.insert(buffer, ev) end
	end
end

--- Public API: record a moment. kind is one of the known kinds (others become "custom").
function EventLogger.mark(kind: string, detail: string?)
	emit(string.lower(kind), detail)
end

local function flush()
	if not config.httpUrl or #buffer == 0 then return end
	local batch = buffer
	buffer = {}
	local body = HttpService:JSONEncode({ events = batch })
	local ok, err = pcall(function()
		HttpService:PostAsync(config.httpUrl :: string, body, Enum.HttpContentType.ApplicationJson)
	end)
	if not ok then
		warn("EventLogger HTTP post failed (events are still in Output): " .. tostring(err))
		config.httpUrl = nil -- stop retrying; printed lines remain the source of truth
	end
end

local function watchCharacter(player: Player, character: Model)
	if not config.trackDeaths then return end
	local humanoid = character:WaitForChild("Humanoid", 10) :: Humanoid?
	if humanoid then
		humanoid.Died:Connect(function()
			if isTracked(player) then emit("death", "") end
		end)
	end
end

local function watchLeaderstats(player: Player)
	if not config.trackLeaderstats then return end
	local stats = player:WaitForChild("leaderstats", 30)
	if not stats then return end
	local joinedAt = now()
	local function watchValue(v: Instance)
		if not (v:IsA("IntValue") or v:IsA("NumberValue")) then return end
		local key = tostring(player.UserId) .. ":" .. v.Name
		bestStat[key] = (v :: any).Value
		;(v :: any).Changed:Connect(function(value: number)
			local best = bestStat[key] or 0
			if value > best then
				bestStat[key] = value
				local t = now()
				-- ignore initial data-store load and rapid increments
				if t - joinedAt > 5 and t - (lastHighScoreAt[key] or 0) >= config.highScoreCooldown
					and isTracked(player) then
					lastHighScoreAt[key] = t
					emit("high_score", v.Name .. "=" .. tostring(value))
				end
			end
		end)
	end
	for _, v in stats:GetChildren() do watchValue(v) end
	stats.ChildAdded:Connect(watchValue)
end

local function onCommand(player: Player, message: string)
	if not isTracked(player) then return end
	local t = os.clock()
	if t - (lastCommandAt[player.UserId] or 0) < 0.5 then return end -- de-dupe chat systems
	local msg = string.lower(string.gsub(message, "^%s+", ""))
	if msg == "/rec" then
		lastCommandAt[player.UserId] = t
		emit("recording_start", "sync marker - start OBS recording now")
	elseif msg == "/stop" then
		lastCommandAt[player.UserId] = t
		emit("recording_stop", "")
	else
		local kind, detail = string.match(message, "^%s*/mark%s+(%S+)%s*(.*)$")
		if kind then
			lastCommandAt[player.UserId] = t
			EventLogger.mark(kind, detail ~= "" and detail or nil)
		end
	end
end

local function onPlayer(player: Player)
	player.CharacterAdded:Connect(function(c) watchCharacter(player, c) end)
	if player.Character then task.spawn(watchCharacter, player, player.Character) end
	task.spawn(watchLeaderstats, player)
	if config.chatCommands then
		player.Chatted:Connect(function(message) onCommand(player, message) end)
	end
end

--- Start logging. Call once from a server Script.
function EventLogger.start(opts: {[string]: any}?)
	assert(RunService:IsServer(), "EventLogger must run on the server")
	if started then return end
	for k, v in (opts or {}) do (config :: any)[k] = v end
	started = true
	sessionStart = now()
	emit("session_start", RunService:IsStudio() and "studio" or "live",
		{ utc = DateTime.now():ToIsoDate(), place_id = game.PlaceId })
	if config.httpUrl and not RunService:IsStudio() then
		warn("EventLogger: httpUrl ignored on live servers (Roblox blocks localhost/private IPs)")
		config.httpUrl = nil
	end
	for _, p in Players:GetPlayers() do onPlayer(p) end
	Players.PlayerAdded:Connect(onPlayer)
	if config.httpUrl then
		task.spawn(function()
			while config.httpUrl do
				task.wait(config.httpBatchSeconds)
				flush()
			end
		end)
		game:BindToClose(flush)
	end
end

return EventLogger
