# Host benchmark guide

The benchmark tools measure OpenMW outer-loop frame intervals. They do not measure GPU execution or display scanout. The runtime is opt-in and must be built with the benchmark patch. See [the runtime protocol](BENCHMARK-PROTOCOL.md) for the JSON and CSV schemas.

## Requirements

- Use your own registered OpenMW title and Morrowind game data. The project does not package game data or saves.
- Keep a local copy of the save that you want to use for the test. The host uploads a unique benchmark copy and does not overwrite the original save.
- Install the packaged title with its benchmark runtime. The disabled example is `assets/benchmark/examples/benchmark-disabled.json`; it does not enable the runtime.
- For automatic start and stop, use a trusted LAN and a working elfldr-compatible listener on TCP 9021. Build the lifecycle ELF with `python3 tools/build-benchmark-launcher.py`. The host streams `build/benchmark-launcher.elf` to the loader only when the lifecycle helper is not reachable. The raw ELF-loader port has no authentication; restrict it to a trusted network.
- The helper exposes the fixed-title lifecycle service on TCP 9138. The game runtime uses its separate authenticated control service, normally TCP 9137. Keep both ports on a trusted LAN. Do not expose them to an untrusted network.
- FTP access must show the active title's writable mount. The runner uses FTP `NLST`; if the server returns `502`, it falls back to `LIST`.

The runner does not assume an M.2 backing path. Its FTP paths default to `/download0`, matching the runtime's workspace layout, but they are configurable. Use only a root and config path observed on the active FTP mount. The runner requires the root's existing `config` directory as a sentinel. It never creates a missing mount root.

## One-time prepare and restart

The runtime reads its config and isolated settings at startup. The first setup cannot enable itself while the feature is disabled, and changing files after startup does not change the running engine. To opt in:

1. Start the installed title normally so its actual writable FTP mount is visible. Do not create a fake sandbox tree while the title is stopped.
2. Run `--prepare` while that mount is visible. This writes only the benchmark config, benchmark-specific settings, scenario, and a unique test-save copy. It copies the settings snapshot before the next startup. It does not change normal game config, saves, caches, or the running title.
3. Normal-exit the title. Use the automatic runner below to start a new process, or start it manually and use `--attach`. The new process reads the enabled config and isolated settings.

Example:

```sh
python3 tools/run-benchmark.py --prepare \
  --host 192.0.2.20 \
  --scenario config/benchmark-example-idle.json \
  --save /path/to/local-test-copy.omwsave \
  --settings config/settings.cfg \
  --output build/benchmark-prepare \
  --remote-root /download0 \
  --remote-config /download0/config/benchmark.json
```

Replace the example host and local save path with your values. If the active FTP view uses another root, set both `--remote-root` and `--remote-config` to the observed paths. The benchmark workspace must match the runtime's `/download0/benchmark` workspace as seen through that FTP view.

Prepare stores a generated hexadecimal token in a private local file (default `~/.config/openmw-ps5-benchmark/token`, mode `0600`). The token is not passed on the command line or printed. Keep it private. The prepare output includes `prepare_summary.json`. If an earlier benchmark config or isolated settings file exists, the runner saves local backups and refuses to overwrite an existing backup. Use a new `--output` directory for a later prepare when the prior backup files already exist. A failed config/settings write restores the previous values where possible; review the error if rollback itself fails.

## Direct game lifecycle

`tools/console-game.py` provides direct `start`, `stop`, and `status` commands for PPSA99630:

```sh
python3 tools/console-game.py start --host 192.0.2.20 --token-file ~/.config/openmw-ps5-benchmark/token
python3 tools/console-game.py status --host 192.0.2.20 --token-file ~/.config/openmw-ps5-benchmark/token
python3 tools/console-game.py stop --host 192.0.2.20 --token-file ~/.config/openmw-ps5-benchmark/token
```

`start` launches through the helper and keeps its first authenticated game-control connection open while it waits for status `menu`, `ready`, or `paused`. `stop` requests normal game quit through the token-authenticated runtime and waits for helper-confirmed process absence. `status` reports helper process state and game status when the authenticated runtime is available. The token file is read locally and is never printed. These commands use `--loader-port`, `--lifecycle-port`, `--launcher-payload`, `--control-port`, `--timeout`, and `--token-file`; defaults match the benchmark runner.

Save progress before using `stop`; the command does not request a save. After a console reboot, the ELF loader must be available again. The next `start` command reloads the helper if its listener is absent.

The production commands completed a loader bootstrap, launch to the main menu, normal shutdown with confirmed process absence, and relaunch on the configured PS5. See [validation](VALIDATION.md#network-benchmark-build). This does not establish compatibility with other consoles or firmware.

## Automatic launch and repeats

After preparation, leave the title stopped, then run:

```sh
python3 tools/run-benchmark.py \
  --host 192.0.2.20 \
  --scenario config/benchmark-example-idle.json \
  --save /path/to/local-test-copy.omwsave \
  --settings config/settings.cfg \
  --output build/benchmark-runs \
  --repeat 5 \
  --build-meta /path/to/PPSA99630/assets/benchmark/buildmeta.json \
  --remote-root /download0 \
  --remote-config /download0/config/benchmark.json
```

The runner starts the fixed title through the resident lifecycle helper, waits for helper-confirmed process presence, then waits for the game's authenticated control service before connecting to FTP. Process presence is not proof that the game is ready. If the helper is absent, the runner verifies the ELF magic and uploads the configured launcher payload through the raw ELF loader once, then waits for the helper. It does not upload PS5Upload or use its HTTP API. The runner checks the active config and settings hashes without rewriting either file. If they do not match the prepared profile, the run fails and asks for another prepare/restart; it does not pretend new settings are effective.

For each repeat, the runner uploads a unique test-save copy and scenario, waits for the requested save to be loaded, then runs the scenario. It matches the asynchronous command ID and exact new `run_id` before accepting completion. It collects the CSV and manifest before requesting normal quit through the authenticated game service, then waits for the lifecycle helper to confirm process absence before starting the next repeat. A failure stops the repeat sequence. Timeouts are bounded by `--operation-timeout`, `--request-timeout`, `--ftp-timeout`, and `--poll-interval`.

Use `--loader-port`, `--lifecycle-port`, and `--launcher-payload` to override the defaults (9021, 9138, and `build/benchmark-launcher.elf`). Other useful options are `--control-port`, `--ftp-port`, `--ftp-user`, `--ftp-password`, and `--max-frames`. Do not place secrets directly in shell history. The FTP password is independent of the runtime token.

## Manual attach

Use `--attach` to run against an already launched title with the configured runtime control service. This mode does not invoke the helper or loader and does not need a launcher ELF. The runner loads the selected benchmark save and scenario, collects the result, then disconnects without quitting the manually started title. It is limited to one run per invocation; `--attach --repeat` greater than one is rejected. If a capture fails, the runner attempts to stop that capture and collect diagnostics, but it still leaves the title running.

## Results and analysis

Each run has a `run-*/` directory with the raw `frames.csv`, runtime `manifest.json`, `source_scenario.json`, uploaded `scenario.json`, `test_save.sha256`, `effective_settings.cfg`, `run_metadata.json`, and `summary.json`. The runner archives save bytes by hash only; it does not copy the local save into the result directory. The `diagnostics/` directory contains available `stderr.log`, `stdout.log`, `config/openmw.log`, `benchmark/config/openmw.log`, and `ps5-opengl.log` files. Its `collection.json` records downloaded log hashes, optional missing files, and retrieval errors. Metadata records hashes of the source and uploaded scenario, test-save bytes, effective settings, and optional packaged build metadata supplied with `--build-meta`. The top-level `repeat_summary.json` reports completed repeats, failures, and across-run summary metrics. Incomplete runs retain diagnostic artifacts when the runtime made them available and are never reported as successful measurements.

Analyze a full capture with:

```sh
python3 tools/analyze-frame-pacing.py build/benchmark-runs/run-*/frames.csv
```

The analyzer validates the manifest checksum and row count when called on a `frames.csv` file. It reports measured outer intervals, interpolated percentiles, and strict `>16.67`, `>33.33`, and `>50 ms` stall counts. Legacy engine-log samples are labeled separately and are not full-frame timing data. Update/render/Lua/limiter durations are CPU-side engine measurements; none is GPU time or physical display timing.

The package created by `tools/pack-openmw.py` includes `assets/benchmark/buildmeta.json` and example configuration/scenario files. Build metadata identifies available source revisions, worktree state, the dependency-lock hash, settings hashes, package options, packaging environment overrides, the driver receipt, the final executable hash, and a build ID. An extracted engine tree without its own Git metadata reports its revision as unknown; it does not inherit the port's revision. Packaging environment overrides are not a substitute for the driver's recorded build flags. Metadata contains no game data or save data.
