# Network control research: PS5 title launch and status

## Current lifecycle design

The current host tools do not depend on PS5Upload. `tools/benchmark_launcher.py` contacts the lifecycle helper on TCP 9138. If and only if that connection is refused, it verifies and uploads the standard ELF payload through the raw loader on TCP 9021. The resident helper serves bounded `STATUS`, `LAUNCH`, and `EXIT` requests for PPSA99630. The host uses the game's separate token-authenticated control service on TCP 9137 for readiness and normal quit.

The raw loader and lifecycle helper are unauthenticated. Use them only on a trusted LAN. The helper checks process presence before a single native launch call and reports the real error. Process presence is not game readiness. On the configured PS5, the production CLI completed helper bootstrap, startup to an authenticated main-menu state, normal shutdown with process-absence confirmation, and relaunch. See [validation](VALIDATION.md#network-benchmark-build).

The rest of this document records historical PS5Upload/FTX2 research and read-only observations. Those findings are not current instructions for `run-benchmark.py` or `console-game.py`.

Historical research recorded no payload upload or console mutation at that time. That statement does not describe later direct lifecycle implementation or prove its hardware behavior.

## Read-only console observations

The following checks were made against `192.168.18.218`:

| Service/check | Result | What it does and does not establish |
| --- | --- | --- |
| FTP TCP 2121, anonymous | Login and passive `LIST` succeeded. | Read-only metadata access is available. `NLST` is unsupported by this server; use `LIST`/`RETR`. |
| `/data/ps5upload/` | Contains `runtime/active_instance.txt`, `startup.log`, `installer.log`, and `ava/` metadata. | Evidence of PS5Upload data/runtime footprint; file presence alone does not identify its release. |
| `/data/ps5upload/runtime/active_instance.txt` | Records `runtime_port=0`, `startup_reason=2`, a start timestamp, and PID 120. | Runtime ownership metadata, not a semantic version or proof that TCP 9120 belongs to this process. Do not publish its instance identifier. |
| `/data/ps5upload/startup.log` | Recent startup sequence includes `AVA1_STARTED` and `WEBUI_STARTED`. | Strong evidence an AVA1-capable PS5Upload instance has run. The log does not report a release version. |
| `/data/ps5upload/installer.log` | Contains `ps5upload installer 1.5.0` start messages. | This is the companion package installer version only, not the main PS5Upload payload version. |
| `/data/pldmgr/payloads/repo/ps5upload@e8cce5d1/meta.json` | Names cached payload file `ps5upload_v8.5.1.elf`, version `v8.5.1`. | Identifies a repository-cache payload, not by itself the active version. |
| `/data/pldmgr/payloads/local/ps5upload-helper/meta.json` | Names `ps5upload-helper.elf` but contains no version. The local file and cached v8.5.1 file both list 6,614,240 bytes and the same Nov 5 06:48 modification time. | Strong candidate that the installed helper was copied from cached v8.5.1. Metadata does not prove byte identity or the running process version. Binary contents were not downloaded or inspected. |
| `/data/pldmgr/autoload.txt` | Lists FTP, kstuff and shadowmountplus; no PS5Upload entry. | Does not identify the mechanism that started PS5Upload. |
| `/mnt/sandbox/PPSA99630_000/app0` | Mounted title tree contains `eboot.bin` and `assets/`; `eboot.bin` is 114,510,152 bytes. | Confirms the title is mounted in this sandbox view; does not reveal its physical backing install path or prove it is currently running. The older `/mnt/ext1/etaHEN/games/PPSA99630` path was denied by FTP and was not visible in `/mnt/ext1/`. |
| `/mnt/sandbox/PPSA99630_000/download0` | Writable app sandbox with `config`, `user`, `cache`, and application logs. Saves directory contains two `.omwsave` files; names are retained only in private session metadata. | This is the observed writable mount for this title. It is a title sandbox, not the physical installed-title path. No save contents were read. Recent stdout/config log modification times make an active title plausible, but do not prove current process state. Treat it as possibly active and do not interrupt it. |
| TCP 9114 | Connection refused. | The repository's historical FTX2 endpoint is unavailable now. |
| TCP 9021 | TCP connection accepted, no bytes sent. | Listener protocol and identity remain unknown. |
| TCP 9120 | TCP connection accepted, no bytes sent. | Matches current upstream PS5Upload's documented AVA1 port, but neither protocol nor version was verified on-console. |
| Local engine / web UI | `127.0.0.1:19113` and console `192.168.18.218:9180` refused connections. | No authenticated PS5Upload client or process-status API was available during these checks. Current game-process status remains unknown. |

FTP allowed anonymous `LIST` and file `RETR`; `NLST` returned `502 Command not recognized`. The repository cache is v8.5.1, but its local installed-helper manifest has no semantic version; size/time equality is suggestive but not conclusive. The startup log proves startup milestones only. The installer log's v1.5.0 marker identifies the installer companion, not PS5Upload itself. The latest public PS5Upload release observed was v11.2.6.

At the time of this research, `tools/console_control.py` defaulted to `127.0.0.1:9114` and framed requests with `struct.Struct("<IHHIQQ")`, magic `FTX2`, version 1, and JSON payloads. Its matching `tools/deploy.py` used operation 56 with `{"src_path": ...}` for registration, 60 with `{"title_id": ...}` for launch, and 162 for process listing. Those were the old project FTX2 contract only. Since 9114 refused a connection, none of those commands were sent or verified against this console. Do not send those frames to 9120.

## Historical PS5Upload protocol findings

The upstream [v8.5.1 engine README](https://github.com/phantomptr/ps5upload/blob/v8.5.1/engine/README.md) documents TCP 9120 as AVA1, Noise XX and sealed frames. The [AVA1 protocol README](https://github.com/phantomptr/ava1/blob/main/README.md) specifies ChaCha20-Poly1305 per frame, a six-digit pairing code shown by the receiver, CPace, and identity/peer keys stored on both sides. The earlier proposal was to use the PS5Upload engine as the client. The current tools do not implement AVA1 framing or use that engine.

The matching v8.5.1 PS5Upload engine exposes an HTTP API on the engine computer, separate from console port 9120. Its source establishes this fixed contract:

- `GET /api/version` returns `{"version":"8.5.1", ...}` for the v8.5.1 engine.
- This v8.5.1 tag has no `/api/openapi.json` endpoint. Its route contract must be pinned to the tagged source, not guessed from current main or a different engine release.
- `POST /api/ps5/app/launch` accepts JSON `{"addr":"<console-ip>","title_id":"<title-id>"}`. The source's `addr` field is optional and defaults to `PS5_ADDR`, but the benchmark client sends the explicit address. It returns `{"ok":true}` when the engine's launch operation succeeds. The title must already be registered in the console's app database. A successful response is not proof that the title process is present or game-ready.
- `GET /api/ps5/process/list?addr=<console-ip>` returns `{"processes":[...],"truncated":false}`. Each row includes `pid`, `name`, `comm`, `title_id`, `kind`, and resource fields. A native game process has `kind:"app"` and its title ID. If `truncated:true` and the target title is absent, status is unknown.
- The earlier `tools/benchmark_launcher.py` adapter checked `/api/version`, queried process state before launch, refused an already-running title, sent one launch request, and polled for process presence. The current adapter replaces this HTTP path with the direct lifecycle helper.

At the time of the earlier integration plan, the v8.5.1 engine HTTP API had no password and accepted loopback by default; `PS5UPLOAD_ALLOW_IP` allowed remote clients. Its HTTP guard was not AVA1 authentication. Its API could read, write, and delete console files, so access had to remain restricted. These facts describe that old engine API, not the current host workflow.

If the engine has no existing AVA1 trust relationship, v8.5.1 supports human pairing. `GET /api/ava1/pairing?addr=<console-ip>` only reports known state. `POST /api/ava1/pairing/start` with `{"addr":"<console-ip>"}` starts/re-reads the handshake and makes the PS5 show a six-digit code. A human must enter that code with `POST /api/ava1/pairing/confirm` and `{"addr":"<console-ip>","code":"<six digits>"}`. This setup has console-visible effects and is not an agent-doable step without the operator. Do not automate guessed codes or print the code.

For a headless matching engine, check out the public PS5Upload `v8.5.1` source workspace with Rust/Cargo installed, then run `PS5_ADDR=<ip> cargo run -p ps5upload-engine` from its `engine/` directory. The v8.5.1 README gives the default port as 19113. The binary's `run_cli` source binds `0.0.0.0:<port>`, but the loopback guard rejects non-loopback `/api/*` requests unless `PS5UPLOAD_ALLOW_IP` is set; leave that variable unset and use local firewall rules if strict local-only exposure is required. Setting `PS5_ADDR` only sets the engine's default console address: startup registers the AVA1 transport and local workers but does not connect to, deploy to, or upgrade the console. `GET /api/version` is static engine metadata; `GET /api/ava1/pairing?addr=` only reads in-memory pairing state. `POST /api/ava1/pairing/start` initiates a handshake, and `GET /api/ps5/process/list?addr=` opens a read-only AVA1 process-list request. The source stores AVA1 state under `PS5UPLOAD_DATA_DIR` when set, otherwise `~/.ps5upload`; preserve it across runs to retain paired-console trust. The initial local probe found no engine or default state directory. The parent later started the verified v8.5.1 standalone engine in a Docker service mapped to host loopback and observed `/api/version` return `{"caps":{"rar":true},"version":"8.5.1"}` and `/api/ava1/pairing?addr=192.168.18.218` return `{"state":"none"}`. The pairing-state read does not prove that the console is unpaired or reveal installed payload version. No title process-list request was made. Do not change the console's installed payload to make this host adapter work.

## Historical PS5Upload integration plan (superseded)

The instructions in this section describe the earlier PS5Upload adapter plan. They are not current requirements. Current host lifecycle tools use the direct lifecycle helper described at the start of this document.

1. Configure the engine URL explicitly (default `http://127.0.0.1:19113`) and the console address explicitly (`192.168.18.218` only for this observed setup; do not bake it into the tool).
2. Run only the version-matched v8.5.1 engine for this adapter. Before any launch, call `GET /api/version` and require exactly `8.5.1`. The v8 tag does not provide OpenAPI; use the exact source contract above. Fail closed on any other engine version.
3. Query `GET /api/ps5/process/list?addr=...` before launch. If `PPSA99630` is already present, use attach mode and do not start another copy. If the list is truncated without the title, status is unknown and launch must not proceed.
4. Use `POST /api/ps5/app/launch` once with explicit `addr` and title ID `PPSA99630`. Do not retry after an ambiguous timeout; query process status first.
5. Poll `GET /api/ps5/process/list?addr=...` until its `processes` list contains `kind:"app"` and `title_id:"PPSA99630"`, or until a bounded deadline expires. Require an in-game ready response before starting a benchmark; process presence is not scene readiness.
6. For benchmark shutdown, ask the game-side command interface to finalize, clear injected input, and request normal game exit. In automatic mode, call `wait_for_title_stopped(...)` and wait for the process-list route to confirm the title is absent before the next launch. A truncated list or timeout is a failed/incomplete stop, not confirmation. Do not use a force-kill call as normal quit.
7. Keep one controlling client, bounded deadlines, and explicit run IDs. On ambiguous timeout, mark the run incomplete and query status; do not blindly repeat a potentially non-idempotent launch.

Historical session note: The v8.5.1 engine was reachable on host loopback, and its version endpoint matched the then-pinned adapter. The pairing-state endpoint returned `none`; no pairing handshake or console process request was started. The FTP cache identified v8.5.1 as the installed-helper candidate, but the local helper manifest did not establish the active binary version. These observations do not describe the current host workflow or prove title launch.

## Historical ELF-loader assessment

The first-party [`elfldr` README](https://github.com/ps5-payload-dev/elfldr/blob/master/README.md) documents a two-stage process: an exploit-provided loader is needed on 9020 to bootstrap `elfldr-ps5.elf`; after that, `elfldr` listens on 9021. The socket implementation [`socksrv.c`](https://github.com/ps5-payload-dev/elfldr/blob/master/socksrv.c) accepts ELF and SELF payload data, file/HTTP URI lines, and HTTP GET requests, then spawns the supplied payload via `elfldr_spawn`/`selfldr_spawn`. The README notes payloads run in individual processes and the loader stays resident.

The ELF loader starts payload processes; it does not itself register or mount a native folder title, identify a title's process state, or provide a clean game-exit operation. The current host design adds a separate helper ELF that uses process enumeration and the native launch API. That design does not turn the loader into a title manager. Direct launch succeeded on the configured console without ShellUI injection. The host still reports real native errors and does not assume compatibility with other firmware.

## Source links

- [This repository's console setup](CONSOLE-SETUP.md): folder-title nature, historical FTX2 deployment, firmware qualification, and data paths.
- [This repository's FTX2 client](../tools/console_control.py): FTX2 request framing and defaults.
- [This repository's deploy helper](../tools/deploy.py): legacy FTX2 operation IDs and payloads.
- [PS5Upload payload README](https://github.com/phantomptr/ps5upload/blob/main/payload/README.md): current 9120 AVA1 service, module capabilities and firmware statement.
- [PS5Upload API reference](https://github.com/phantomptr/ps5upload/blob/main/API.md): HTTP engine endpoint, version/schema discovery, access guards, and route families.
- [PS5Upload v11.2.6 release](https://github.com/phantomptr/ps5upload/releases/tag/v11.2.6): current release observed on 2026-11-06; not evidence of installed console version.
- [PS5Upload source tree](https://github.com/phantomptr/ps5upload/tree/main/payload/src): app registration, process management, and AVA1 RPC source modules.
- [PS5Upload v8.5.1 engine README](https://github.com/phantomptr/ps5upload/blob/v8.5.1/engine/README.md): matched headless command, engine port, unauthenticated API, and AVA1 transport.
- [PS5Upload v8.5.1 engine startup and routes](https://github.com/phantomptr/ps5upload/blob/v8.5.1/engine/crates/ps5upload-engine/src/lib.rs): `/api/version`, launch/process route registration, and `run_cli` binding/startup behavior.
- [PS5Upload v8.5.1 app launch handler](https://github.com/phantomptr/ps5upload/blob/v8.5.1/engine/crates/ps5upload-engine/src/lib.rs): `AppLaunchReq` and `ps5_app_launch`.
- [PS5Upload v8.5.1 process response types](https://github.com/phantomptr/ps5upload/blob/v8.5.1/engine/crates/ps5upload-core/src/process_mgr.rs): process row fields, title ID, `kind`, and `truncated`.
- [PS5Upload v8.5.1 AVA1 pairing handlers](https://github.com/phantomptr/ps5upload/blob/v8.5.1/engine/crates/ps5upload-engine/src/ava1_api.rs): read-only pairing state and explicit start/confirm routes.
- [PS5Upload v8.5.1 engine state path](https://github.com/phantomptr/ps5upload/blob/v8.5.1/engine/crates/ps5upload-engine/src/remote/store.rs): `PS5UPLOAD_DATA_DIR` and default `~/.ps5upload`.
- [AVA1 protocol README](https://github.com/phantomptr/ava1/blob/main/README.md): Noise XX, ChaCha20-Poly1305, CPace pairing, and identity/peer stores.
- [PS5Upload v8.5.1 release](https://github.com/phantomptr/ps5upload/releases/tag/v8.5.1): version-matched payload and engine release.
- [`elfldr` socket protocol implementation](https://github.com/ps5-payload-dev/elfldr/blob/master/socksrv.c): accepted wire formats and payload spawning behavior.
- [etaHEN launch declarations](https://github.com/etaHEN/etaHEN/blob/main/Source%20Code/daemon/include/launcher.hpp): launch parameter field types and native function declarations.
- [Public PS5 payload SDK](https://github.com/ps5-payload-dev/sdk): standalone payload build, process enumeration, dynamic module loading, and UserService initialization examples.
- [Current lifecycle helper](../platform/benchmark-launcher.c): fixed-title commands and the app-info layout verified on the configured console.
