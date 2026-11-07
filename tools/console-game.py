#!/usr/bin/env python3
"""Start, stop, or inspect the registered Morrowind title over the trusted LAN."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from benchmark_client import BenchmarkClient, BenchmarkError
from benchmark_launcher import (
    DEFAULT_LIFECYCLE_PORT,
    DEFAULT_LOADER_PORT,
    DEFAULT_PAYLOAD_PATH,
    LauncherError,
    launch_title,
    title_status,
    wait_for_title_stopped,
)

DEFAULT_TOKEN_FILE = Path.home() / ".config/openmw-ps5-benchmark/token"
TOKEN_PATTERN = re.compile(r"^[0-9a-fA-F]{32,}$")


def _load_token(path):
    try:
        token = Path(path).read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as error:
        raise LauncherError(f"cannot read benchmark token file {path}: {error}") from error
    if not TOKEN_PATTERN.fullmatch(token):
        raise LauncherError(f"benchmark token file {path} must contain at least 32 hexadecimal characters")
    return token


def _client(args, token, timeout):
    return BenchmarkClient(args.host, args.control_port, token, timeout=timeout)


def _wait_game_ready(args, token, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LauncherError("timed out waiting for authenticated game readiness")
    client = _client(args, token, min(args.timeout, remaining))
    try:
        client.connect_until(deadline=deadline, poll_interval=args.poll_interval)
        return client.wait_for(
            lambda status: status["game_state"] in ("menu", "ready", "paused"),
            deadline=deadline, poll_interval=args.poll_interval,
            description="authenticated game menu or world readiness",
        )
    finally:
        client.close()


def _start(args, token):
    deadline = time.monotonic() + args.timeout
    launch = launch_title(
        args.host, "PPSA99630", payload_path=args.launcher_payload,
        loader_port=args.loader_port, lifecycle_port=args.lifecycle_port,
        timeout_s=args.timeout, poll_interval_s=args.poll_interval,
    )
    game = _wait_game_ready(args, token, deadline)
    print(json.dumps({"action": "start", "title_id": "PPSA99630",
                      "launch": launch, "game_status": game}, sort_keys=True))


def _stop(args, token):
    deadline = time.monotonic() + args.timeout
    process = title_status(args.host, lifecycle_port=args.lifecycle_port,
                           timeout_s=min(args.timeout, 10.0))
    if process["running"]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LauncherError("stop deadline expired before authenticated quit")
        client = _client(args, token, min(args.timeout, remaining))
        try:
            client.connect_until(deadline=deadline, poll_interval=args.poll_interval)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LauncherError("stop deadline expired before authenticated quit")
            client.set_timeout(remaining)
            client.command("quit")
        finally:
            client.close()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LauncherError("stop deadline expired before process absence was confirmed")
        stopped = wait_for_title_stopped(
            args.host, "PPSA99630", lifecycle_port=args.lifecycle_port,
            timeout_s=remaining, poll_interval_s=args.poll_interval,
        )
    else:
        stopped = {"title_id": "PPSA99630", "stopped": True, "process": None}
    print(json.dumps({"action": "stop", "process": stopped}, sort_keys=True))


def _status(args, token):
    try:
        process = title_status(args.host, lifecycle_port=args.lifecycle_port,
                               timeout_s=min(args.timeout, 10.0))
    except LauncherError as error:
        process = {"unavailable": f"{type(error).__name__}: {error}"}
    if not token:
        game = {"unavailable": "private token file is not available"}
    else:
        try:
            with _client(args, token, min(args.timeout, 5.0)) as client:
                game = client.status()
        except Exception as error:
            game = {"unavailable": f"{type(error).__name__}: {error}"}
    print(json.dumps({"action": "status", "process": process, "game_status": game}, sort_keys=True))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "stop", "status"))
    parser.add_argument("--host", required=True, help="Console address reachable from this host")
    parser.add_argument("--loader-port", type=int, default=DEFAULT_LOADER_PORT)
    parser.add_argument("--lifecycle-port", type=int, default=DEFAULT_LIFECYCLE_PORT)
    parser.add_argument("--launcher-payload", type=Path, default=DEFAULT_PAYLOAD_PATH)
    parser.add_argument("--control-port", type=int, default=9137)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    args = parser.parse_args(argv)
    for name, port in (("loader", args.loader_port), ("lifecycle", args.lifecycle_port),
                       ("control", args.control_port)):
        if not 1 <= port <= 65535:
            parser.error(f"--{name}-port must be between 1 and 65535")
    for name, value in (("timeout", args.timeout), ("poll-interval", args.poll_interval)):
        if not math.isfinite(value) or value <= 0:
            parser.error(f"--{name} must be positive and finite")
    if args.command != "status" and not args.token_file.is_file():
        parser.error(f"token file does not exist: {args.token_file}")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        try:
            token = _load_token(args.token_file) if args.token_file.is_file() else None
        except LauncherError:
            if args.command != "status":
                raise
            token = None
        if args.command != "status" and token is None:
            raise LauncherError(f"token file does not exist: {args.token_file}")
        {"start": _start, "stop": _stop, "status": _status}[args.command](args, token)
    except (LauncherError, BenchmarkError, TimeoutError) as error:
        print(f"console-game: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
