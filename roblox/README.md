# Roblox EventLogger

`EventLogger.lua` records timestamps of real in-game moments (deaths, wins,
checkpoints, rare items, boss defeats, high scores, surprises) while **you**
play-test your own game and record it with OBS. RobloxAutoPromo uses those
timestamps to pick clips and to write captions that only mention things that
actually happened.

The logger only observes and prints. It does not control, script or
automate any player. You make the recordings yourself.

## Install

1. In Roblox Studio, create a **ModuleScript** in `ServerScriptService` named
   `EventLogger` and paste in `EventLogger.lua`.
2. Create a server **Script** in `ServerScriptService`:

   ```lua
   local EventLogger = require(game.ServerScriptService.EventLogger)
   EventLogger.start({
       recordUserIds = {},   -- optional: only your UserId, e.g. {12345678}
       httpUrl = nil,        -- optional, Studio only (see below)
   })
   ```
3. Add `EventLogger.mark(...)` calls where your game already knows something happened:

   ```lua
   EventLogger.mark("victory", "reached the summit")
   EventLogger.mark("checkpoint", "stage 12")
   EventLogger.mark("rare_item", "Golden Sword")
   EventLogger.mark("boss_defeat", "Lava King")
   EventLogger.mark("unexpected", "physics launch")
   ```

   Detected automatically: `death` (`Humanoid.Died`) and `high_score` (when a
   `leaderstats` IntValue/NumberValue goes above its previous best, ignoring
   the first 5 s after joining so data-store loads don't count).

Kinds: `death victory checkpoint rare_item boss_defeat high_score unexpected`
plus sync markers `session_start recording_start recording_stop`. Any other
kind is logged as `custom`.

## Syncing with the OBS recording

1. Start a Play test. The logger prints `session_start` (with UTC time).
2. **Start OBS recording, then type `/rec` in chat right away.** This logs a
   `recording_start` sync marker. The Python side subtracts its `t` from every
   event, so event time becomes video time. Type `/stop` when you stop.
   `/mark <kind> [detail]` logs a moment by hand.
3. The error is roughly the time between pressing record and sending `/rec`,
   usually under a second. That's fine because clips are 10-30 s. If you forget
   `/rec`, the pipeline falls back to the OBS file name time (for example
   `2026-09-24 18-30-00.mkv`) minus `session_start` UTC. That needs your PC clock
   to be accurate.

Want a hotkey instead of chat? Add a LocalScript that fires a RemoteEvent
on a key press, and have a server Script call `EventLogger.mark("recording_start")`.
This is optional.

## Getting the events to RobloxAutoPromo

**(a) Copy from Output (always works).** After the session, open *View > Output*,
select the lines containing `[AutoPromoEvent]` (other lines are ignored, and
Studio's timestamps and ` - Server` suffixes are fine), and save them as
`<recording name>.events.txt` next to the video, for example
`2026-09-24 18-30-00.events.txt` for `2026-09-24 18-30-00.mkv`, before
dropping both into `inbox/`.
You can also save the canonical JSON form as `<recording name>.events.json`:

```json
{"session_start_utc": "2026-09-24T18:30:00Z",
 "events": [{"t": 12.3, "kind": "death", "detail": ""}]}
```

**(b) HttpService POST (optional, Studio only).** Set `httpUrl`. Events are sent
as `{"events":[...]}` every 5 s. Limits:
* HttpService only works from **server** scripts and only when *Game Settings >
  Security > Allow HTTP Requests* is on.
* **Live Roblox servers can't reach your PC**. Requests to `localhost` and
  private IPs are blocked, so the logger turns `httpUrl` off outside Studio.
  Exposing a public tunnel to your PC is out of scope and not recommended.
* Whether Studio play-tests can reach `localhost` depends on your Studio
  version and settings. Check it yourself. The printed lines stay the
  source of truth, and after a failed POST the logger stops posting.
* RobloxAutoPromo doesn't ship a receiver. A tiny local HTTP server that
  appends the body to a `.events.json` file is enough if you want one.

## Privacy and fair play

* Only events from the players in `recordUserIds` are logged (all players if
  it's empty, which suits solo Studio tests). No usernames are recorded, and death events carry no player identity. The
  only chat text logged is the detail you type after your own `/mark` command.
* Record only your own sessions, or sessions whose players agreed to be
  filmed. Never use bots, fake players or scripted "moments" to make clips
  look better than real gameplay.
