# coc-farming-2

Phased Clash of Clans farming over a USB-connected Android phone:

1. Replay a recorded **start search** flow (home → Find a Match)
2. **Loot gate** — wait for match UI (not clouds), OCR loot, tap **Next** until thresholds
3. Pick a random **attack template** (2–3 recorded deploys) with Gaussian coordinate/timing jitter
4. Detect the **Return** screen, go home
5. If gold or elixir reserve is full → **terminal bell** and wait for you; otherwise loop

## Account risk

**Using this tool can get the Clash of Clans account permanently banned.** Supercell’s
Safe and Fair Play policy and Terms of Service prohibit bots and automation. Random
template selection and Gaussian jitter are **not** ban protection — they only reduce
identical input replay. Local use and short supervised runs do not remove the risk.

## Prerequisites

- macOS (terminal bell + browser calibration)
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Android Platform Tools (`adb` on `PATH`)
- USB debugging authorized for this Mac
- For pinch replay: JDK (`javac`) + Android SDK (`d8`, `android.jar`)
- For loot OCR: `brew install tesseract` + `uv sync --extra ocr`
  (Tesseract is preferred; EasyOCR is slower and does not use Apple MPS)

Default device profile matches a Samsung SM-A145F landscape layout
(serial `RF8WA0586AB`). Adjust `DoctorExpectations` in code if your device differs.

## Install

```sh
uv sync --dev
uv sync --extra ocr   # optional until you need ocr-test / loot gate
adb devices
```

## Setup workflow

```sh
# 1. Open CoC, unlocked, landscape, home village
uv run coc-farm2 doctor
uv run coc-farm2 status

# 2. Pixel probes (browser click unless auto home)
uv run coc-farm2 calibrate home
uv run coc-farm2 calibrate gold-full      # bar full
uv run coc-farm2 calibrate elixir-full
uv run coc-farm2 calibrate match_ready_a  # on a found base UI chrome
uv run coc-farm2 calibrate match_ready_b
uv run coc-farm2 calibrate return_ready_a # end-of-attack Return button
uv run coc-farm2 calibrate return_ready_b
uv run coc-farm2 calibrate next_button
uv run coc-farm2 calibrate return_tap

# 3. OCR crops on match screen (two corners each)
uv run coc-farm2 calibrate-ocr gold
uv run coc-farm2 calibrate-ocr elixir
# optional:
uv run coc-farm2 calibrate-ocr dark
uv run coc-farm2 set-threshold --gold 400000 --elixir 400000 --mode all
# wall farming: attack when known gold + elixir reaches 2M
uv run coc-farm2 set-threshold --sum 2000000 --mode sum

# 4. Record phases
uv run coc-farm2 record start_search   # home → find match
# Manually find a base, frame camera (pinch/pan OK), then:
uv run coc-farm2 record attack         # deploy only — repeat 2–3 times

# 5. Checkpoints for long delays, then approve
uv run coc-farm2 status
uv run coc-farm2 checkpoint start_search --before-gesture N --probe-name home
uv run coc-farm2 preview start_search
# optional: inject gestures on the phone (type PLAY)
uv run coc-farm2 preview start_search --live
uv run coc-farm2 preview attack:01 --live --no-overlay
uv run coc-farm2 preview attack:01 --live --jitter --seed 1   # Gaussian like run
# after parser fixes, rebuild macros from saved getevent traces:
# uv run coc-farm2 recompile attack:01
# delete a bad take:
# uv run coc-farm2 delete attack:01
uv run coc-farm2 approve start_search
uv run coc-farm2 approve attack:01
uv run coc-farm2 approve attack:02

# 6. Smoke
uv run coc-farm2 ocr-test
uv run coc-farm2 test-once             # type RUN
uv run coc-farm2 run --max-cycles 3    # type START
uv run coc-farm2 run
```

Runtime data lives in `.coc-farm2/` (gitignored).

### Operator controls while running

- `p` — pause before next input check  
- `q` — stop  
- Ctrl-C — interrupt  
- Full reserve: terminal bell (`BEL`); Enter to recheck / resume, `q` to quit  

### Replay pacing (timing)

Recorded thinking gaps are compressed so attacks feel closer to real play:

| Config (`.coc-farm2/config.json` → `timing`) | Default | Meaning |
| --- | --- | --- |
| `delay_scale` | `0.4` | Multiply gaps between gestures (finger-up → next down) |
| `duration_scale` | `1.0` | Path / pinch motion speed |
| `tap_max_ms` | `100` | Cap single-tap holds |
| `trailing_hold_ms` | `80` | Drop resting finger after a drag ends |

Set `delay_scale` to `1.0` for exact recorded pauses; lower (e.g. `0.3`) for snappier farming.

## Architecture (short)

| Phase | Source |
| --- | --- |
| `start_search` | One recorded macro |
| loot gate | Built-in: `match_ready` probes + OCR + Next |
| attack | Random approved template + Gaussian variation |
| return home | `return_ready` probes + calibrated tap |

Prefer **screen-fixed UI chrome** for probes (buttons, bars), never village buildings.

## Development

```sh
uv run ruff format .
uv run ruff check .
uv run mypy src tests
uv run pytest
```

## Non-goals

Login, dialog dismissal, upgrades, troop training, iOS, resolution scaling, ban evasion
beyond requested multi-template + jitter.
