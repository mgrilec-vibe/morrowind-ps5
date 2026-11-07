# ELF loader and token-authenticated control

## Historical findings

The research recorded below describes read-only observations made before the direct lifecycle helper was added. At that time, TCP 9021 accepted a connection but received no application bytes, so the listener's identity was not established. The FTP check established anonymous metadata and file retrieval only. Neither check proved loader authentication or FTP write access.

The public `elfldr` protocol accepts raw ELF/SELF payloads and starts them as processes. It does not authenticate uploads. The loader remains separate from title registration, game control, and game readiness. Restrict TCP 9021 to a trusted network. Do not send secrets to it. [elfldr README](https://github.com/ps5-payload-dev/elfldr/blob/master/README.md) · [socket handler](https://github.com/ps5-payload-dev/elfldr/blob/master/socksrv.c)

The old report's statement that automatic title launch was unavailable is historical and superseded by the implementation and console validation below.

## Current direct lifecycle design

`tools/benchmark_launcher.py` uses the raw loader only to start `build/benchmark-launcher.elf` when TCP 9138 refuses a connection. It verifies the ELF magic, streams the file, half-closes the upload connection, and waits for the helper. It does not retry helper `LAUNCH` after an ambiguous response.

The resident helper uses a bounded, newline-delimited ASCII protocol on TCP 9138. It supports `STATUS`, `LAUNCH`, and `EXIT`. Its replies identify service `openmw-launcher`, protocol 1, and fixed title `PPSA99630`. The helper checks process presence before one native launch attempt. `STATUS` reports process presence only after successful, complete process enumeration. Helper protocol is unauthenticated by design and is for a trusted LAN only.

The helper is not the game control service. After launch, the host must use the game's separate token-authenticated TCP service (normally port 9137) to verify game readiness and to request normal quit. It then uses helper `STATUS` to confirm title-process absence. Process presence alone does not prove world readiness. The helper must not force-kill the game, mutate saves/configuration, register or mount titles, or inject into ShellUI.

On the configured console, the direct native launch call succeeded without PS5Upload or ShellUI injection. The production host commands bootstrapped the helper through the ELF loader, launched the title to an authenticated main-menu state, requested normal quit, confirmed process absence, and relaunched the title. Other consoles or firmware may reject the native call because of caller context. See [validation](VALIDATION.md#network-benchmark-build).

## Historical read-only checks

Earlier checks reported a TCP connection to port 9021 without sending application bytes and anonymous FTP metadata/file retrieval on port 2121. These are historical results, not current service identity or write-access guarantees. See [network research](NETWORK-TESTING-RESEARCH.md).

The game's existing control service uses a private token and newline-delimited JSON. It is a distinct protocol and does not encrypt traffic. Keep its token in the private host token file and restrict the control port to a trusted LAN. See [benchmark protocol](BENCHMARK-PROTOCOL.md).

## Limits and safety

- TCP port numbers alone do not identify the service currently listening.
- Raw ELF-loader transport has no authentication. Use it only on a trusted network.
- The lifecycle helper has no authentication. It can start the fixed title, so restrict TCP 9138 to a trusted network.
- Game control requests use a token but travel over unencrypted TCP. Do not expose this service to untrusted networks.
- The implementation is not evidence that native title launch or shutdown works on every console or firmware.
