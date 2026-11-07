"""Host-side client for the direct PS5 title lifecycle helper."""
from __future__ import annotations

import errno
import json
import math
import socket
import time
from pathlib import Path

SERVICE = "openmw-launcher"
PROTOCOL_VERSION = 1
TITLE_ID = "PPSA99630"
DEFAULT_LOADER_PORT = 9021
DEFAULT_LIFECYCLE_PORT = 9138
DEFAULT_PAYLOAD_PATH = Path(__file__).resolve().parents[1] / "build" / "benchmark-launcher.elf"
MAX_REPLY_BYTES = 4096
UPLOAD_CHUNK_BYTES = 64 * 1024


class LauncherError(RuntimeError):
    """The direct lifecycle helper could not safely service the request."""

def _positive_timeout(timeout_s, poll_interval_s=None):
    if (not math.isfinite(timeout_s) or timeout_s <= 0 or
            (poll_interval_s is not None and
             (not math.isfinite(poll_interval_s) or poll_interval_s <= 0))):
        raise LauncherError("timeout and poll interval must be positive and finite")


def _request(console_addr, lifecycle_port, command, timeout_s):
    request = (command + "\n").encode("ascii")
    deadline = time.monotonic() + timeout_s
    try:
        with socket.create_connection((console_addr, lifecycle_port),
                                      timeout=_remaining(deadline)) as conn:
            conn.settimeout(_remaining(deadline))
            conn.sendall(request)
            raw = bytearray()
            while len(raw) <= MAX_REPLY_BYTES:
                conn.settimeout(_remaining(deadline))
                part = conn.recv(min(1024, MAX_REPLY_BYTES + 1 - len(raw)))
                if not part:
                    break
                raw.extend(part)
                if b"\n" in part:
                    break
    except OSError as error:
        raise LauncherError(f"lifecycle helper {command} request failed: {error}") from error
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1 or len(raw) > MAX_REPLY_BYTES:
        raise LauncherError("lifecycle helper returned an unterminated or oversized reply")
    try:
        text = raw[:-1].decode("ascii")
        reply = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LauncherError("lifecycle helper returned invalid ASCII JSON") from error
    if not isinstance(reply, dict):
        raise LauncherError("lifecycle helper reply must be a JSON object")
    if reply.get("service") != SERVICE:
        raise LauncherError("unexpected lifecycle helper service")
    if type(reply.get("protocol")) is not int or reply["protocol"] != PROTOCOL_VERSION:
        raise LauncherError("unsupported lifecycle helper protocol")
    if reply.get("title_id") != TITLE_ID:
        raise LauncherError("lifecycle helper reported an unexpected title ID")
    if type(reply.get("ok")) is not bool:
        raise LauncherError("lifecycle helper reply lacks an ok boolean")
    if not reply["ok"]:
        message = reply.get("error")
        code = reply.get("code")
        if not isinstance(message, str):
            message = "request was rejected"
        if code is not None and (type(code) is not int):
            raise LauncherError("lifecycle helper returned an invalid error code")
        raise LauncherError(f"lifecycle helper rejected {command}: {message}")
    if command == "STATUS":
        if type(reply.get("running")) is not bool:
            raise LauncherError("lifecycle helper status lacks a running boolean")
        if type(reply.get("pid")) is not int or reply["pid"] < 0:
            raise LauncherError("lifecycle helper status has an invalid process ID")
        if not reply["running"] and reply["pid"] != 0:
            raise LauncherError("lifecycle helper reports a PID for an absent title")
        if reply["running"] and reply["pid"] == 0:
            raise LauncherError("lifecycle helper reports a running title without a process ID")
    elif command == "LAUNCH":
        if type(reply.get("launch_result")) is not int:
            raise LauncherError("lifecycle helper launch reply lacks an integer result")
    return reply


def _is_connection_refused(error):
    cause = error.__cause__
    return isinstance(cause, OSError) and cause.errno == errno.ECONNREFUSED


def _upload_payload(console_addr, payload_path, loader_port, timeout_s):
    path = Path(payload_path)
    deadline = time.monotonic() + timeout_s
    try:
        with path.open("rb") as source:
            if source.read(4) != b"\x7fELF":
                raise LauncherError(f"launcher payload is not an ELF file: {path}")
            source.seek(0)
            with socket.create_connection(
                (console_addr, loader_port), timeout=_remaining(deadline)
            ) as conn:
                while True:
                    chunk = source.read(UPLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    conn.settimeout(_remaining(deadline))
                    conn.sendall(chunk)
                conn.shutdown(socket.SHUT_WR)
    except LauncherError:
        raise
    except OSError as error:
        raise LauncherError(f"could not execute launcher ELF through the loader: {error}") from error


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LauncherError("lifecycle deadline expired")
    return remaining


def ensure_helper(
    console_addr,
    *,
    payload_path=DEFAULT_PAYLOAD_PATH,
    loader_port=DEFAULT_LOADER_PORT,
    lifecycle_port=DEFAULT_LIFECYCLE_PORT,
    timeout_s,
    poll_interval_s,
):
    """Return helper process status, bootstrapping only when its port is refused."""
    if not console_addr:
        raise LauncherError("console address is required")
    _positive_timeout(timeout_s, poll_interval_s)
    deadline = time.monotonic() + timeout_s
    try:
        return _request(console_addr, lifecycle_port, "STATUS", _remaining(deadline))
    except LauncherError as error:
        if not _is_connection_refused(error):
            raise
    _upload_payload(console_addr, payload_path, loader_port, _remaining(deadline))
    while True:
        try:
            return _request(console_addr, lifecycle_port, "STATUS", _remaining(deadline))
        except LauncherError as error:
            if not _is_connection_refused(error):
                raise
        remaining = _remaining(deadline)
        time.sleep(min(poll_interval_s, remaining))


def title_status(console_addr, *, lifecycle_port=DEFAULT_LIFECYCLE_PORT, timeout_s):
    """Read authoritative process status from an already-running helper."""
    if not console_addr:
        raise LauncherError("console address is required")
    _positive_timeout(timeout_s)
    return _request(console_addr, lifecycle_port, "STATUS", timeout_s)


def launch_title(
    console_addr,
    title_id,
    *,
    payload_path=DEFAULT_PAYLOAD_PATH,
    loader_port=DEFAULT_LOADER_PORT,
    lifecycle_port=DEFAULT_LIFECYCLE_PORT,
    timeout_s,
    poll_interval_s,
):
    """Launch once through the resident helper and wait for process visibility."""
    if title_id != TITLE_ID:
        raise LauncherError(f"this lifecycle helper supports only {TITLE_ID}")
    _positive_timeout(timeout_s, poll_interval_s)
    deadline = time.monotonic() + timeout_s
    status = ensure_helper(
        console_addr, payload_path=payload_path, loader_port=loader_port,
        lifecycle_port=lifecycle_port, timeout_s=_remaining(deadline),
        poll_interval_s=poll_interval_s,
    )
    if status["running"]:
        raise LauncherError(f"lifecycle helper is already running as PID {status['pid']}")
    # Do not retry this request: a lost reply leaves launch outcome ambiguous.
    launched = _request(console_addr, lifecycle_port, "LAUNCH", _remaining(deadline))
    while True:
        status = _request(console_addr, lifecycle_port, "STATUS", _remaining(deadline))
        if status["running"]:
            return {"title_id": TITLE_ID, "launch_response": launched, "process": status}
        remaining = _remaining(deadline)
        time.sleep(min(poll_interval_s, remaining))


def wait_for_title_stopped(
    console_addr,
    title_id,
    *,
    lifecycle_port=DEFAULT_LIFECYCLE_PORT,
    timeout_s,
    poll_interval_s,
):
    """Wait for helper-confirmed absence of the fixed title process."""
    if title_id != TITLE_ID:
        raise LauncherError(f"this lifecycle helper supports only {TITLE_ID}")
    if not console_addr:
        raise LauncherError("console address is required")
    _positive_timeout(timeout_s, poll_interval_s)
    deadline = time.monotonic() + timeout_s
    while True:
        status = _request(console_addr, lifecycle_port, "STATUS", _remaining(deadline))
        if not status["running"]:
            return {"title_id": TITLE_ID, "stopped": True, "process": None}
        remaining = _remaining(deadline)
        time.sleep(min(poll_interval_s, remaining))
