#!/usr/bin/env python3
"""Install the opt-in OpenMW benchmark overlay and incremental patch safely."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO / "openmw"
PATCH = REPO / "patches/openmw-benchmark.patch"
OVERLAYS = REPO / "source-overlays/openmw"
BASE_PS5PATH_SHA256 = "9e87d7272b697e2abcd7a79b41085b56ec78b8813a7c1aac1cd71fab55211f84"
BASE_PATCH_SENTINELS = {
    "CMakeLists.txt": ("list(APPEND USED_OSG_PLUGINS osgdb_deprecated_osg)",),
    "apps/openmw/engine.cpp": (
        "mViewer->setThreadingModel(osgViewer::Viewer::SingleThreaded)",
        "if (SDL_WasInit(flags) == 0)",
    ),
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_base(source: Path) -> None:
    if not (source / "apps/openmw/engine.cpp").is_file() or not (source / "CMakeLists.txt").is_file():
        raise SystemExit(f"not an OpenMW source root: {source}")
    for relative, sentinels in BASE_PATCH_SENTINELS.items():
        text = (source / relative).read_text(encoding="utf-8")
        missing = [sentinel for sentinel in sentinels if sentinel not in text]
        if missing:
            raise SystemExit(f"base PS5 OpenMW patch is missing from {relative}: {missing[0]!r}")


def overlay_needs_install(source_root: Path, overlay: Path, receipt: dict[str, str]) -> bool:
    relative = overlay.relative_to(OVERLAYS)
    key = relative.as_posix()
    target = source_root / relative
    for parent in target.parents:
        if parent == source_root:
            break
        if parent.is_symlink():
            raise SystemExit(f"refusing to write through symlink OpenMW overlay directory: {parent}")
    if target.is_symlink():
        raise SystemExit(f"refusing to replace symlink OpenMW overlay target: {target}")
    contents = overlay.read_bytes()
    if target.is_file():
        existing = target.read_bytes()
        if existing == contents:
            return False
        if digest(existing) != receipt.get(key) and not (
            key == "components/files/ps5path.hpp" and digest(existing) == BASE_PS5PATH_SHA256
        ):
            raise SystemExit(f"refusing to replace conflicting OpenMW overlay target: {target}")
        return True
    if target.exists():
        raise SystemExit(f"refusing to replace non-file OpenMW overlay target: {target}")
    return True


def install_overlay(source_root: Path, overlay: Path, receipt: dict[str, str]) -> bool:
    if not overlay_needs_install(source_root, overlay, receipt):
        return False
    relative = overlay.relative_to(OVERLAYS)
    target = source_root / relative
    contents = overlay.read_bytes()

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(overlay.stat().st_mode))
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def load_receipt(source: Path) -> tuple[Path, dict[str, str]]:
    receipt_path = source / ".ps5-benchmark-overlays.json"
    if receipt_path.is_symlink():
        raise SystemExit(f"refusing to read symlink overlay receipt: {receipt_path}")
    if not receipt_path.exists():
        return receipt_path, {}
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"invalid OpenMW overlay receipt {receipt_path}: {error}") from error
    if not isinstance(receipt, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in receipt.items()
    ):
        raise SystemExit(f"invalid OpenMW overlay receipt schema: {receipt_path}")
    return receipt_path, receipt


def write_receipt(receipt_path: Path, overlays: list[Path]) -> None:
    contents = {
        overlay.relative_to(OVERLAYS).as_posix(): digest(overlay.read_bytes())
        for overlay in overlays
    }
    serialized = json.dumps(contents, indent=2, sort_keys=True) + "\n"
    if receipt_path.is_file() and receipt_path.read_text(encoding="utf-8") == serialized:
        return
    if receipt_path.is_symlink() or (receipt_path.exists() and not receipt_path.is_file()):
        raise SystemExit(f"refusing to replace non-file OpenMW overlay receipt: {receipt_path}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{receipt_path.name}.", dir=receipt_path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, receipt_path)
    finally:
        temporary.unlink(missing_ok=True)


def patch_check(source: Path, reverse: bool) -> subprocess.CompletedProcess[str]:
    options = ["--reverse"] if reverse else ["--forward"]
    return subprocess.run(
        ["patch", "--directory", str(source), "-p1", *options, "--batch", "--dry-run", "-i", str(PATCH)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def patch_needs_apply(source: Path) -> bool:
    if not PATCH.is_file() or PATCH.stat().st_size == 0:
        raise SystemExit(f"benchmark patch is missing or empty: {PATCH}")

    reverse = patch_check(source, reverse=True)
    reverse_detail = (reverse.stderr + reverse.stdout).lower()
    skip_markers = ("skipping", "ignored", "reversed", "previously applied", "failed")
    if reverse.returncode == 0 and not any(marker in reverse_detail for marker in skip_markers):
        return False

    forward = patch_check(source, reverse=False)
    detail = (forward.stderr + forward.stdout).strip()
    if forward.returncode != 0 or any(marker in detail.lower() for marker in skip_markers):
        raise SystemExit(f"OpenMW tree is neither the benchmark baseline nor fully patched: {detail}")
    return True


def apply_patch(source: Path) -> None:
    result = subprocess.run(
        ["patch", "--directory", str(source), "-p1", "--forward", "--batch", "-i", str(PATCH)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"failed to apply OpenMW benchmark patch: {result.stderr or result.stdout}")



def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                        help="isolated, base-PS5-patched OpenMW source tree")
    args = parser.parse_args()
    source = args.source.resolve()
    if not source.is_dir():
        raise SystemExit(f"OpenMW source directory does not exist: {source}")
    validate_base(source)
    overlays = sorted(path for path in OVERLAYS.rglob("*") if path.is_file())
    if not overlays:
        raise SystemExit(f"OpenMW source overlays are missing: {OVERLAYS}")
    receipt_path, receipt = load_receipt(source)
    overlay_changes = [path for path in overlays if overlay_needs_install(source, path, receipt)]
    patch_pending = patch_needs_apply(source)
    if patch_pending:
        apply_patch(source)
    changed = [path for path in overlay_changes if install_overlay(source, path, receipt)]
    write_receipt(receipt_path, overlays)
    patched = patch_pending

    if changed or patched:
        print(f"Installed {len(changed)} OpenMW overlay file(s); benchmark patch applied: {patched}")
    else:
        print("OpenMW benchmark overlays and patch already match; no files changed")


if __name__ == "__main__":
    main()
