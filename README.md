# Radical Red Pilot

An autopilot that plays [Pokémon Radical Red](https://www.pokecommunity.com/threads/pokemon-radical-red.404332/)
as a Nuzlocke inside mGBA, reports what it is doing, and hands control back the
moment you touch the keyboard.

**Every gameplay decision runs locally.** No API keys, no per-token cost, nothing
to keep topped up — the design constraint was that it should be able to run for
hours without spending anything.

> **You must supply your own ROM.** None is included and none ever will be; see
> [Requirements](#requirements).

---

## The interesting part: what the model is *not* allowed to decide

The obvious design is "show a vision model the screen and let it press buttons."
That design does not work. A 4B local vision model looking at an upscaled 240×160
GBA frame will confidently hallucinate — in testing it decided *"press A to confirm
starter choice"* eight times in a row while standing in an empty room with no
dialogue box on screen.

So the pilot asks the model as little as possible. Anything the game already knows
as an exact number is read out of emulator memory instead:

| Question | Answered by | Why |
|---|---|---|
| Where am I? What's walkable? | `world.py` — collision grid from RAM | exact tiles, no guessing |
| How do I get to another map? | `route.py` — warp + connection tables | the map graph is *in the ROM* |
| What is my party's HP? | `telemetry.py` — party struct decode | a Nuzlocke death must not be a guess |
| Which move should I use? | `battle.py` — type chart × power × accuracy | see below |
| Is this dialogue or a menu? | `dialogue.py` — OCR geometry + confidence | cheap and deterministic |
| Genuinely ambiguous screens | the local vision model | last resort only |

A concrete example of why this split matters. Facing a randomized Dragon/Flying
opponent with a Grass starter, the move scorer ranked the same-type **Absorb** at
`0.25×` and chose Normal-type **Pound** instead. A vision model picking "the move
that matches my type" gets that backwards.

## Architecture

```
mGBA ──bridge.lua──▶ TCP ──▶ server.py ──▶ dashboard (index.html)
        buttons              decision loop
        memory reads         ├── naming.py      keyboard screens
        frame stream         ├── battle.py      move choice by arithmetic
                             ├── route.py       cross-map itineraries
                             ├── navigation.py  tile-by-tile walking
                             ├── dialogue.py    dialogue fast path
                             └── local_driver.py  Ollama vision model (fallback)
```

Supporting modules: `telemetry.py` (party decode), `combat.py` (battle memory +
ROM tables), `nuzlocke.py` (rule ledger), `learning.py` (per-run experience),
`stall.py` (progress watchdog), `monitor.py` (faint detection),
`screen_reader.swift` (offline Apple Vision OCR).

### Safety properties it tries to hold

- **No cloud calls, ever.** `local_driver.py` rejects any non-loopback endpoint
  and any model tag that looks remote.
- **Instant takeover.** `bridge.lua` distinguishes injected input from real host
  input, so touching the keyboard drops the AI out of control immediately.
- **It stops instead of flailing.** `stall.py` counts consecutive no-progress
  decisions and pauses rather than looping forever.
- **No resets, no save-state loads, no cheats**, and no writes to game memory.
- **It pauses rather than risk a life** when HP is low, because a Nuzlocke faint
  is permanent.

## Requirements

- **macOS** (the OCR helper uses Apple Vision; `sips` is used for upscaling)
- **Python 3.11+** — standard library only, no pip dependencies
- **[mGBA](https://mgba.io/) 0.10.5+** with Lua scripting
- **[Ollama](https://ollama.com/)** with a vision model:
  `ollama pull qwen3-vl:4b-instruct-q4_K_M`
- **A Radical Red ROM that you supply yourself.** Place it at
  `runtime/radical-red.gba`. This project does not distribute, link to, or help
  you obtain a ROM. Everything under `runtime/` is gitignored.

The party/battle decoders are fingerprinted against one specific Radical Red
build and will refuse to run against a ROM whose hash does not match, rather
than silently misreading memory.

## Running it

```bash
python3 launcher.py          # starts Ollama, mGBA and the controller
```

Dashboard at `http://127.0.0.1:8765`. There is also a CLI:

```bash
python3 control.py state             # current run state
python3 control.py auto              # hand control to the pilot
python3 control.py pause             # stop AI input
python3 control.py route 3.0 3.19    # walk an itinerary of map ids
python3 control.py press A 4         # manual input
```

## Tests

```bash
python3 -m unittest discover -p 'test_*.py'
```

155 tests, no ROM or emulator required — the decision layers are pure functions
over recorded game state.

## Status

Working: fully autonomous new-game intro (including the naming keyboards),
cross-map routing, party/HP telemetry, deterministic battle move selection,
Nuzlocke rule tracking with evidence.

Not yet: it has not beaten the first gym. The first run died in the mandatory
rival battle to a randomized Level 5 Dragonite. Overworld decisions that fall
through to the vision model are still slow (~9s) and unreliable.

## Licence

MIT — see [LICENSE](LICENSE). This covers the code in this repository only.
Pokémon and Radical Red are the property of their respective owners.
