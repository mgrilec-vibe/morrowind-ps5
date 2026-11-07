#!/usr/bin/env python3
"""Build the persistent benchmark lifecycle payload as a standard SDK ELF."""

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parent.parent
SDK = Path(os.environ.get(
    "PS5_PAYLOAD_SDK",
    Path.home() / "ps5sdk/native-app-boilerplate/.deps/native/ps5-payload-sdk",
)).expanduser().resolve()
SOURCE = ROOT / "platform/benchmark-launcher.c"
OUTPUT = ROOT / "build/benchmark-launcher.elf"
COMPILER = SDK / "bin/prospero-clang"


def main() -> None:
    if not COMPILER.is_file():
        raise SystemExit(f"payload SDK compiler not found: {COMPILER}")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PS5_PAYLOAD_SDK"] = str(SDK)
    subprocess.run(
        [str(COMPILER), "-Wall", "-Werror", "-O2", str(SOURCE), "-o", str(OUTPUT)],
        cwd=ROOT,
        env=env,
        check=True,
    )
    print(OUTPUT)


if __name__ == "__main__":
    main()
