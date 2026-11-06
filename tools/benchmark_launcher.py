"""Version-checked title launcher through the PS5Upload engine HTTP API."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request


class LauncherError(RuntimeError):
    """The engine could not safely launch or verify the requested title."""


def _request(engine_url, method, path, *, body=None, timeout_s):
    url = engine_url.rstrip("/") + path
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise LauncherError(f"PS5Upload response too large for {method} {path}")
    except urllib.error.HTTPError as error:
        raise LauncherError(f"PS5Upload returned HTTP {error.code} for {method} {path}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise LauncherError(f"PS5Upload request failed for {method} {path}") from error
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LauncherError(f"PS5Upload returned invalid JSON for {method} {path}") from error


def _check_engine_api(engine_url, remaining_timeout):
    version = _request(
        engine_url, "GET", "/api/version", timeout_s=remaining_timeout()
    )
    if not isinstance(version, dict) or version.get("version") != "8.5.1":
        actual = version.get("version") if isinstance(version, dict) else None
        raise LauncherError(
            f"PS5Upload engine version {actual!r} is unsupported; require 8.5.1"
        )
    return version


def _app_process(status, title_id):
    if not isinstance(status, dict) or not isinstance(status.get("processes"), list):
        raise LauncherError("PS5Upload process list has no processes array")
    if not isinstance(status.get("truncated"), bool):
        raise LauncherError("PS5Upload process list has no valid truncated flag")

    matching = None
    for process in status["processes"]:
        if not isinstance(process, dict):
            raise LauncherError("PS5Upload process list contains a malformed row")
        kind = process.get("kind")
        if not isinstance(kind, str):
            raise LauncherError("PS5Upload process row has no valid kind")
        if kind != "app":
            continue
        process_title_id = process.get("title_id")
        if not isinstance(process_title_id, str):
            raise LauncherError("PS5Upload app process row has no valid title_id")
        if process_title_id.casefold() == title_id.casefold():
            matching = process

    if matching is None and status["truncated"]:
        raise LauncherError("PS5Upload process list was truncated; title status is unknown")
    return matching


def launch_title(engine_url, console_addr, title_id, *, timeout_s, poll_interval_s):
    """Launch a registered native title and wait until its process is visible.

    Returns a JSON-compatible dict with ``engine_version``, ``title_id``,
    ``launch_response``, and the matching ``process`` row. Raises LauncherError
    on API/schema failure, launch refusal, malformed status, or timeout. It
    refuses to launch an already-running title and never retries the launch call.
    """
    if not engine_url or not console_addr or not title_id:
        raise LauncherError("engine URL, console address, and title ID are required")
    if timeout_s <= 0 or poll_interval_s <= 0:
        raise LauncherError("timeout and poll interval must be positive")

    deadline = time.monotonic() + timeout_s

    def request_timeout():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LauncherError("launcher deadline expired")
        return min(remaining, 10.0)

    engine_version = _check_engine_api(engine_url, request_timeout)
    query = urllib.parse.urlencode({"addr": console_addr})
    status_path = "/api/ps5/process/list?" + query

    status = _request(engine_url, "GET", status_path, timeout_s=request_timeout())
    existing = _app_process(status, title_id)
    if existing is not None:
        raise LauncherError(
            f"title {title_id} is already running; use attach mode instead of launching it"
        )

    launched = _request(
        engine_url,
        "POST",
        "/api/ps5/app/launch",
        body={"addr": console_addr, "title_id": title_id},
        timeout_s=request_timeout(),
    )
    if not isinstance(launched, dict) or launched.get("ok") is not True:
        raise LauncherError("PS5Upload did not confirm title launch")

    while True:
        status = _request(engine_url, "GET", status_path, timeout_s=request_timeout())
        matching = _app_process(status, title_id)
        if matching is not None:
            return {
                "engine_version": engine_version,
                "title_id": title_id,
                "launch_response": launched,
                "process": matching,
            }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LauncherError(
                f"title {title_id} was not visible in PS5Upload process list before timeout"
            )
        time.sleep(min(poll_interval_s, remaining))


def wait_for_title_stopped(
    engine_url, console_addr, title_id, *, timeout_s, poll_interval_s
):
    """Wait until a title has no matching app process in the v8.5.1 process list.

    Returns a JSON-compatible dict with ``engine_version``, ``title_id``,
    ``stopped: True``, and ``process: None``. Raises LauncherError if status is
    malformed, the process list is truncated, the engine is incompatible, or
    the deadline expires. This function only reads process status.
    """
    if not engine_url or not console_addr or not title_id:
        raise LauncherError("engine URL, console address, and title ID are required")
    if timeout_s <= 0 or poll_interval_s <= 0:
        raise LauncherError("timeout and poll interval must be positive")

    deadline = time.monotonic() + timeout_s

    def request_timeout():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LauncherError("launcher deadline expired")
        return min(remaining, 10.0)

    engine_version = _check_engine_api(engine_url, request_timeout)
    query = urllib.parse.urlencode({"addr": console_addr})
    status_path = "/api/ps5/process/list?" + query

    while True:
        status = _request(engine_url, "GET", status_path, timeout_s=request_timeout())
        matching = _app_process(status, title_id)
        if matching is None:
            return {
                "engine_version": engine_version,
                "title_id": title_id,
                "stopped": True,
                "process": None,
            }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LauncherError(
                f"title {title_id} remained visible in PS5Upload process list before timeout"
            )
        time.sleep(min(poll_interval_s, remaining))
