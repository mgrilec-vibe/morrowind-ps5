# Benchmark runtime protocol

The benchmark runtime is opt-in. It reads one JSON configuration during startup. The document must be strict JSON (not general YAML), at most 64 KiB, and must not use duplicate keys. A missing file leaves benchmarking disabled. Invalid configuration disables the feature. Scenario files and network requests are also strict JSON and have bounded sizes.

## Configuration

```json
{
  "version": 1,
  "enabled": true,
  "control": {
    "enabled": true,
    "bind": "127.0.0.1",
    "port": 9137,
    "token": "0123456789abcdef0123456789abcdef"
  },
  "capture": { "enabled": true, "max_frames": 36000 },
  "startup_scenario": "route.json"
}
```

`version` must be `1`. `enabled` is required. When `enabled` is false, no control worker or capture timing runs. `control` and `capture` are optional; if present, their `enabled` fields are required. `max_frames` is from 1 through 1,000,000. When control is enabled, `token` is required and must be 32–128 hexadecimal characters. The token is a shared secret; keep the control socket on a trusted interface and do not expose it to an untrusted network. `startup_scenario` is optional. The runtime uses a fixed workspace root (`/download0/benchmark` on PS5); host test code can pass a local root through the non-PS5 C++ API, but no network command can select a path.

With a valid enabled configuration, the runtime creates `config`, `user`, `cache`, `saves`, `scenarios`, and `runs` below the workspace and publishes the isolated-path flag before engine configuration is read. Put save files in `saves/` and scenarios in `scenarios/`. A save name is a single filename, not a path.

## Scenario

A scenario file is strict JSON, at most 1 MiB:

```json
{
  "version": 1,
  "save": "route.ess",
  "warmup_seconds": 5,
  "duration_seconds": 120,
  "events": [
    { "time": 0, "input": { "forward": 1, "strafe": 0 } },
    { "time": 10, "input": { "forward": 0, "strafe": 1, "run": true } }
  ],
  "checkpoints": [
    { "time": 15, "cell": "Balmora", "x": 100, "y": 200, "z": 10, "tolerance": 5 }
  ]
}
```

`save`, `warmup_seconds`, `duration_seconds`, and `events` are required. Duration must be positive and is capped at one hour; warmup may be zero and is capped at one hour. Event times are nondecreasing simulation seconds from the scenario-ready origin and must be within the capture duration. Events set the complete input state; omitted axes default to zero and omitted buttons to false. `forward`, `strafe`, `look_x`, and `look_y` are finite numbers in [-1, 1]. Buttons are `jump`, `activate`, `attack`, `run`, and `sneak` booleans.

`checkpoints` is optional and has nondecreasing simulation-second times. Each checkpoint must specify a `cell`, a complete `x/y/z` position plus `tolerance`, or a complete `rot_x/rot_y/rot_z` plus `rotation_tolerance`. A checkpoint mismatch invalidates the capture. Position uses the player actor reference position; rotation uses its Euler rotation. These fields do not describe camera orientation.

Warmup and capture duration are measured with a monotonic wall clock. Event and checkpoint schedules use engine simulation seconds. This separates stable route scheduling from the wall-clock capture window. Loading, paused or skipped outer frames, physical input during a run, and failed checkpoints invalidate a run. Cell streaming stalls remain part of the measured route.

## TCP control protocol

The server accepts one client at a time. Each message is one UTF-8 JSON object followed by `\n`; each request must be at most 65,536 bytes. A request has `version: 1`, a positive integer `id`, the configured `token`, and a `command`. Replies are newline-delimited JSON with the same `id`, `accepted`, `complete`, optional `error`, and a `status` snapshot. A command reply with `accepted: true` means queued, not completed. Poll `status` until the requested state is reached. For asynchronous command outcomes, match `status.last_command_id` to the request id and inspect `status.last_command_error`.

Example request:

```json
{"version":1,"id":1,"token":"0123456789abcdef0123456789abcdef","command":"status"}
```

Commands:

- `status`: return the latest published status.
- `load_save`: provide `save`, a filename below `saves/`. The engine must finish loading before the runtime reports the world ready.
- `run_scenario`: provide `scenario` and unique `run_id`. The scenario save must be the currently loaded save. Existing run paths are never overwritten.
- `stop_capture`: stop an active warmup or capture and finalize it as incomplete.
- `input_state`: provide an `input` object using the scenario axis/button names and `lease_ms` from 1 to 1000. A new update refreshes the bounded manual-input lease. Physical input clears the lease; manual input during capture invalidates the run.
- `quit`: request engine shutdown.

Status includes `ready`, `game_state`, `run_id`, `capture_state`, `completed`, `manifest`, `frames`, `loaded_save`, `error`, `last_command_id`, and `last_command_error`. `capture_state` is `idle`, `loading`, `waiting_world`, `warming`, `capturing`, `complete`, `incomplete`, or `error`. `completed` is true only after the CSV and manifest have been finalized successfully. A client disconnect releases that client's input lease. An unauthenticated client cannot interrupt a different authenticated run.

## Output and timing

Each run writes `runs/<run_id>/frames.csv` and `runs/<run_id>/manifest.json` after measurement, not during measured frame callbacks. `manifest.frames_file` is the basename `frames.csv`, relative to the run directory. An incomplete run is still finalized with `status: "incomplete"` and an explanatory `reason`; consumers must reject it rather than report zero FPS. `frame_count` is the number of CSV data rows. `frame_capacity` is only the preallocated row limit, not an expected frame count. The manifest includes a SHA-256 hash of the CSV.

CSV columns are:

`frame_id,phase,scenario_time_ns,outer_interval_ns,update_ns,render_ns,lua_wait_ns,limiter_ns,checkpoint_index,paused,loading,cell_id,player_x,player_y,player_z,player_rot_x,player_rot_y,player_rot_z`

`frame_id` is the benchmark outer-loop identifier, not an OSG, native-present, or displayed-frame count. `outer_interval_ns` is wall time from one captured outer-loop start to the next and includes limiter time; the first captured record begins at the capture onset within its outer iteration. Component times are the supplied update, render, Lua-wait, and limiter durations for that captured interval. `scenario_time_ns` is simulation time relative to scenario readiness. `cell_id` identifies a cell-name transition. Position and Euler rotation are player actor reference-data values. No GPU or physical display timing is claimed.
