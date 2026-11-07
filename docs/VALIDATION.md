# Release validation

## v1.0.1

The user confirmed that loading a save and choosing Exit returns to the PS5 home screen without an error. This fixes a shutdown use-after-free in emulated thread-local storage and uses the PS5 system exit request after normal engine cleanup. The 16-thread regression test passes under AddressSanitizer and UndefinedBehaviorSanitizer; original cleanup timing fails the same test.

Executable SHA-256: `0efcffddf80584063d764e8f22383de0b2895d447a0262d33ba3a267b9daabbf`.

Selection artwork replaces the template background. The same artwork is used for the launch background. Both pass the native-title 4K BC7 DDS validator and are byte-identical. Console installation was verified by raw readback.

## v1.0.0

Tested on PS5 firmware 9.00 with kstuff and ShadowMount, installed on M.2 as `PPSA99630`.

The user confirmed:

- Normal New Game opening and Sony native keyboard name entry.
- The keyboard stays open, accepts the name, and allows continuing character creation.
- Race, face, hair and other appearance controls work with the controller layout.
- Basic save/load and normal visuals after loading the save.
- Larger dialogs at 150% interface scale.
- Visible left-stick pointer in Options and selection of the Controller sub-tab.
- Normal visuals with the separately installed Morrowind Optimization Patch 1.18 core.

Earlier console tests confirmed movement, camera controls, world rendering, inventory, interaction and audio. A complete playthrough, all quest/combat combinations, and other firmware/loader combinations have not been tested.

The executable SHA-256 is `e9daec8edc9402bd72c1df73e64e9358130ecc121b2293f086ede7f3c2d748c2`, matching the console-tested build. The release uses the same GUI settings, with original game data and no enabled optional-mod path in the default configuration.

## Automated checks

Native packaging and SELF integrity checks passed. The IME regression test runs with AddressSanitizer and UndefinedBehaviorSanitizer and SDL's real text conversion. It covers initialization/open failures, duplicate requests, Unicode across event boundaries, cancel/result failures, temporary focus loss and shutdown. These tests supplement console testing.

## Rendering and performance

Original textures, 1080p, 4096-unit view distance, VSync and a 60 FPS engine cap are the defaults. The graphics driver is unchanged from beta.1: SHA-256 `be0d0c3a033043453ad1429d0baee174ebcd275024b863ddbee5097091f17dbb`.

Earlier measurements were approximately 30 FPS in a stationary exterior scene. A previous indoor/outdoor transition ran at about 20 FPS after returning outside; lighter interior views reached about 60 FPS. These measurements do not establish a locked 60 FPS result or a performance improvement from the optional mod.

## Scanout color-order correction

The native runtime now registers the RGBA scanout buffers with VideoOut format `0x8000000022000000`. The source evidence and its limits are in [SCANOUT-COLOR-ORDER.md](SCANOUT-COLOR-ORDER.md). This correction has not been tested on a PS5.

To rebuild the release, follow the full compile and package sequence in [BUILDING.md](BUILDING.md), including `python3 tools/build-draw-batching-driver.py`, `python3 tools/compile-openmw.py`, and `python3 tools/pack-openmw.py --out build/package`. Deploy the rebuilt title to the console and launch it.

Use the same game data and settings for the old and rebuilt titles. Prefer original textures without optional mods.

1. Test on firmware 13.60 if available. Record the firmware, loader, display mode, and title build.
2. Start a new game. Compare character skin and the fire and candles in the first building after leaving the boat.
3. Check the menus, an exterior scene, and a loaded save. Confirm that red and blue are not exchanged.
4. Check neutral white and gray areas. Confirm that lighting and transparency have no new artifacts.
5. Move the camera and change rooms. Check for alternating colors across buffer swaps.

For a separate graphics check, package the existing probe with `python3 tools/pack-openmw.py --probe --out build/color-probe` after rebuilding the driver. The probe replaces the game executable in that package. Its triangle has red at the lower left, green at the lower right, and blue at the top. Compare the display with `/download0/graphics-probe.log`. Readback alone cannot verify VideoOut colors.

Record the results before marking this correction as console-verified. Host checks do not verify the scanout format on PS5 hardware.

## Network benchmark build

The benchmark implementation includes the merged scanout color correction. Capture and network control remain disabled until a valid benchmark config enables them.

The native benchmark title compiles, links, and passes the packager's SELF integrity and presentation-asset checks. The host runtime passes socket scenarios for authentication, capture completion, buffer overflow, replay timing, input release, and normal quit. An AddressSanitizer and UndefinedBehaviorSanitizer smoke run completed both normal and overflow captures without sanitizer reports.

A separate host smoke run used a real FTP server, the compiled C++ runtime, and the production runner. It completed prepare, attach, save-load control, capture, checksum validation, collection, and analysis. It preserved the normal settings and left the manually started runtime running. Its game callbacks were host fixtures, not a PS5 engine execution.

Console lifecycle validation used the installed title and the standalone ELF helper. The production `console-game.py` commands bootstrapped the helper through TCP 9021, launched `PPSA99630` to the main menu, requested normal game quit, confirmed process absence, and launched it again. Authenticated game status confirmed the final main-menu state. No PS5Upload, pairing, forced kill, or ShellUI injection was used.

The helper's process query uses the title ID at app-info offset 16, verified by a read-only probe on this console. A live non-app process returned success with an empty title. A missing PID returned `0x80020003`. Other query failures remain errors rather than evidence that the game is stopped.

The original benchmark save check failed on this console because `canonical()` returned `EPERM` for the sandbox saves directory. Direct entry-status checks now reject symlinks without requiring canonicalization. The socket regression reproduces denied `realpath` and passes with the correction. Console save loading was deferred at the operator's request.

The corrected native title passed SELF integrity checks and full raw FTP readback. Its executable SHA-256 is `aea3d7b0a0bec8bf47ba34b34caa28cf32770bbed8123f0c3642af1abd63586a`. FTP SELF decoding must be disabled on the verification connection to compare stored executable bytes.

All 44 host tests and the C++ socket regression passed after the direct lifecycle migration.

Remaining PS5 validation:

1. Run one idle capture with a benchmark save. Check the frame CSV, manifest, hashes, and collected logs.
2. Run a movement scenario. Check the camera, actions, checkpoints, and input release after disconnect.
3. Run automatic benchmark repeats through the direct helper.
4. Disable the benchmark config and restart. Check that normal saves, settings, and controls remain unchanged.

Host results do not establish PS5 controller correctness, console lifecycle compatibility, GPU timing, or a performance improvement.

## Known limits

- HD texture replacements produced flickering and remain disabled by default.
- Native block-compressed texture sampling remains unresolved; expanded texture data increases memory use.
- The known pre-context `glGetString` warning remains.
- Game data and optional mods must be supplied separately.
