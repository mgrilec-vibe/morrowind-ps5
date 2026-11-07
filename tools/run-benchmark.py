#!/usr/bin/env python3
"""Prepare, launch, run, collect, and summarize controlled OpenMW benchmarks."""
from __future__ import annotations

import argparse
import ftplib
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import statistics
import sys
import time
import uuid

from benchmark_client import BenchmarkClient, BenchmarkError, FTPStorage

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
TITLE_ID = 'PPSA99630'
SAFE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$')
MAX_SCENARIO_BYTES = 1_048_576
MAX_SCENARIO_SECONDS = 3_600
MAX_OPERATION_TIMEOUT = 7_200
MAX_RUN_TIMEOUT = 14_400


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def safe_name(value, label):
    if not isinstance(value, str) or not SAFE_NAME.fullmatch(value) or value == '.' or '..' in value:
        raise ValueError(f'{label} must be a simple filename or identifier')
    return value


def load_scenario(path):
    raw = Path(path).read_bytes()
    if not raw or len(raw) > MAX_SCENARIO_BYTES:
        raise ValueError('scenario must be non-empty and no larger than 1 MiB')
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get('version') != 1:
        raise ValueError('scenario must be a JSON object with version 1')
    save_name = safe_name(data.get('save'), 'scenario save')
    warmup = data.get('warmup_seconds')
    duration = data.get('duration_seconds')
    if (not isinstance(warmup, (int, float)) or isinstance(warmup, bool) or
            not math.isfinite(warmup) or not 0 <= warmup <= MAX_SCENARIO_SECONDS):
        raise ValueError('warmup_seconds must be between 0 and 3600')
    if (not isinstance(duration, (int, float)) or isinstance(duration, bool) or
            not math.isfinite(duration) or not 0.001 <= duration <= MAX_SCENARIO_SECONDS):
        raise ValueError('duration_seconds must be between 0.001 and 3600')
    events = data.get('events')
    if not isinstance(events, list) or len(events) > 4096:
        raise ValueError('scenario must contain at most 4096 input events')
    previous = -1
    for event in events:
        if (not isinstance(event, dict) or not isinstance(event.get('time'), (int, float)) or
                isinstance(event.get('time'), bool) or not math.isfinite(event.get('time'))):
            raise ValueError('each scenario event needs a finite numeric time')
        if event['time'] < previous or event['time'] < 0 or event['time'] > duration:
            raise ValueError('scenario event times must be monotonic and within duration')
        previous = event['time']
        state = event.get('input')
        if not isinstance(state, dict):
            raise ValueError('each scenario event needs an input object')
        for key in ('forward', 'strafe', 'look_x', 'look_y'):
            value = state.get(key, 0)
            if (not isinstance(value, (int, float)) or isinstance(value, bool) or
                    not math.isfinite(value) or not -1 <= value <= 1):
                raise ValueError(f'scenario input {key} must be between -1 and 1')
        for key in ('jump', 'activate', 'attack', 'run', 'sneak'):
            if not isinstance(state.get(key, False), bool):
                raise ValueError(f'scenario input {key} must be boolean')
    return data, raw, save_name, float(warmup), float(duration)


def _load_analyzer():
    path = Path(__file__).with_name('analyze-frame-pacing.py')
    spec = importlib.util.spec_from_file_location('openmw_frame_pacing', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _remote_join(*parts):
    result = PurePosixPath(parts[0])
    for part in parts[1:]:
        result = result / part
    if not result.is_absolute() or '..' in result.parts:
        raise ValueError('remote paths must be absolute and traversal-free')
    return result.as_posix()


def _read_optional(storage, path):
    try:
        return storage.read_bytes(path)
    except Exception as error:
        import ftplib
        if isinstance(error, ftplib.error_perm) and str(error).startswith('550'):
            return None
        raise

def _delete_optional(storage, path):
    try:
        storage.ftp.delete(path)
    except ftplib.error_perm as error:
        if not str(error).startswith('550'):
            raise



def _verify_scenario_remote_data(scenario, save_name):
    scenario = json.loads(json.dumps(scenario))
    scenario['save'] = save_name
    return scenario, (json.dumps(scenario, indent=2, sort_keys=True, allow_nan=False) + '\n').encode()


def _collect(storage, remote_run_dir, local_dir, run_id, remote_root):
    """Attempt each artifact independently so one missing file cannot hide others."""
    local_dir.mkdir(parents=True, exist_ok=True)
    retrieved = {}
    failures = []
    for name, limit in (('manifest.json', 1024 * 1024), ('frames.csv', 128 * 1024 * 1024)):
        try:
            retrieved[name] = storage.download_file(
                _remote_join(remote_run_dir, name), local_dir / name, max_bytes=limit)
        except Exception as error:
            failures.append(f'{name}: {type(error).__name__}: {error}')
    downloaded_logs = []
    missing_logs = []
    diagnostic_errors = []
    for relative in ('stderr.log', 'stdout.log', 'config/openmw.log',
                     'benchmark/config/openmw.log', 'ps5-opengl.log'):
        remote = _remote_join(remote_root, relative)
        destination = local_dir / 'diagnostics' / relative
        try:
            log_bytes = storage.download_file(remote, destination, max_bytes=16 * 1024 * 1024)
            downloaded_logs.append({
                'path': relative, 'bytes': len(log_bytes), 'sha256': sha256(log_bytes)})
        except ftplib.error_perm as error:
            if str(error).startswith('550'):
                missing_logs.append(relative)
            else:
                diagnostic_errors.append(f'{relative}: {error}')
        except Exception as error:
            diagnostic_errors.append(f'{relative}: {type(error).__name__}: {error}')
    diagnostics_dir = local_dir / 'diagnostics'
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    diagnostics = {
        'downloaded': downloaded_logs, 'missing_optional': missing_logs,
        'errors': diagnostic_errors,
    }
    (diagnostics_dir / 'collection.json').write_text(
        json.dumps(diagnostics, indent=2, sort_keys=True) + '\n')
    if diagnostic_errors:
        (diagnostics_dir / 'collection_errors.txt').write_text('\n'.join(diagnostic_errors) + '\n')
    if failures:
        (local_dir / 'collection_error.txt').write_text('\n'.join(failures) + '\n')
        raise BenchmarkError('failed to download required runtime artifacts: ' + '; '.join(failures))
    manifest_bytes, frames_bytes = retrieved['manifest.json'], retrieved['frames.csv']
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BenchmarkError('runtime manifest is not valid JSON') from error
    if not isinstance(manifest, dict):
        raise BenchmarkError('runtime manifest must be a JSON object')
    if manifest.get('schema_version') != 1 or manifest.get('run_id') != run_id:
        raise BenchmarkError('runtime manifest schema or run ID does not match')
    if manifest.get('status') != 'complete':
        raise BenchmarkError(f"runtime capture is incomplete: {manifest.get('reason') or manifest.get('status')}")
    if manifest.get('frames_file') != 'frames.csv':
        raise BenchmarkError('runtime manifest references an unexpected frame file')
    if manifest.get('frames_sha256') != sha256(frames_bytes):
        raise BenchmarkError('runtime frame CSV checksum does not match its manifest')
    frame_count = manifest.get('frame_count')
    if not isinstance(frame_count, int) or isinstance(frame_count, bool) or frame_count <= 0:
        raise BenchmarkError('runtime manifest has no valid frame count')
    return manifest, manifest_bytes, frames_bytes


def _require_mount(storage, args, *, preparing):
    root = FTPStorage._remote_path(args.remote_root)
    config_dir = _remote_join(root, 'config')
    if PurePosixPath(args.remote_config).parent.as_posix() != config_dir:
        raise BenchmarkError('remote config must be under the configured root/config directory')
    if PurePosixPath(args.remote_config).name != 'benchmark.json':
        raise BenchmarkError('remote config filename must be benchmark.json')
    root_names = {name.casefold() for name in storage.list_names(root)}
    if 'config' not in root_names:
        raise BenchmarkError('FTP root lacks the config-directory sentinel; refusing to create a mount tree')
    config_names = {name.casefold() for name in storage.list_names(config_dir)}
    if preparing:
        return _remote_join(root, 'benchmark'), config_dir, config_names
    workspace = _remote_join(root, 'benchmark')
    if 'benchmark' not in root_names:
        raise BenchmarkError('benchmark workspace is not present on the active mount')
    expected = {'saves', 'scenarios', 'config', 'runs'}
    workspace_names = {name.casefold() for name in storage.list_names(workspace)}
    if not expected.issubset(workspace_names):
        raise BenchmarkError('active benchmark workspace is incomplete; run --prepare while the mount is available')
    return workspace, config_dir, config_names


def _validate_active_config(storage, args, token):
    raw = storage.read_bytes(args.remote_config)
    try:
        config = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BenchmarkError('active benchmark config is not valid JSON') from error
    if not isinstance(config, dict):
        raise BenchmarkError('active benchmark config must be a JSON object')
    control = config.get('control')
    capture = config.get('capture')
    if (config.get('version') != 1 or config.get('enabled') is not True or
            not isinstance(control, dict) or control.get('enabled') is not True or
            control.get('port') != args.control_port or control.get('token') != token or
            not isinstance(capture, dict) or capture.get('enabled') is not True or
            capture.get('max_frames') != args.max_frames):
        raise BenchmarkError('active benchmark config does not match the token/port/capture settings; run --prepare and restart')
    return raw


def _wait_for_mount(args):
    """Wait for FTP plus the existing config sentinel; never create the mount root."""
    deadline = time.monotonic() + args.operation_timeout
    last_error = None
    while time.monotonic() < deadline:
        storage = FTPStorage(args.host, args.ftp_port, args.ftp_user, args.ftp_password,
                             timeout=min(args.ftp_timeout, max(0.1, deadline-time.monotonic())))
        try:
            storage.connect()
            names = {name.casefold() for name in storage.list_names(args.remote_root)}
            if 'config' in names:
                return storage
            last_error = BenchmarkError('FTP-visible root does not contain the expected config sentinel')
        except (ftplib.Error, OSError, TimeoutError) as error:
            last_error = error
        storage.close()
        time.sleep(min(args.poll_interval, max(0.0, deadline-time.monotonic())))
    raise TimeoutError('active FTP mount did not become visible within the operation deadline') from last_error


def _load_token(path, *, create):
    token_path = Path(path)
    if token_path.is_symlink():
        raise ValueError('token file must not be a symbolic link')
    if create and not token_path.exists():
        token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        token = secrets.token_hex(32)
        descriptor = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='ascii') as stream:
            stream.write(token + '\n')
    if not token_path.is_file():
        raise ValueError(f'token file does not exist: {token_path}')
    os.chmod(token_path, 0o600)
    token = token_path.read_text(encoding='ascii').strip()
    if len(token) < 32 or any(char not in '0123456789abcdefABCDEF' for char in token):
        raise ValueError('token file must contain at least 32 hexadecimal characters')
    return token


def _runtime_config(args, token):
    return {
        'version': 1, 'enabled': True,
        'control': {'enabled': True, 'bind': args.bind, 'port': args.control_port,
                    'token': token},
        'capture': {'enabled': True, 'max_frames': args.max_frames},
        'startup_scenario': '',
    }


def _prepare(args, scenario, scenario_bytes, save_path, settings_bytes, token):
    prepare_id = 'prepared-' + uuid.uuid4().hex[:12]
    save_name = prepare_id + (Path(save_path).suffix or '.omwsave')
    scenario_name = 'scenario-' + prepare_id + '.json'
    _, scenario_data = _verify_scenario_remote_data(scenario, save_name)
    args.output.mkdir(parents=True, exist_ok=True)
    config_backup = args.output / 'previous-benchmark-config.json'
    settings_backup = args.output / 'previous-isolated-settings.cfg'
    with _wait_for_mount(args) as storage:
        workspace, _, _ = _require_mount(storage, args, preparing=True)
        old_config = _read_optional(storage, args.remote_config)
        remote_settings = _remote_join(workspace, 'config', 'settings.cfg')
        old_settings = _read_optional(storage, remote_settings)
        if old_config is not None and config_backup.exists():
            raise BenchmarkError(f'config backup already exists; refusing to overwrite it: {config_backup}')
        if old_settings is not None and settings_backup.exists():
            raise BenchmarkError(f'settings backup already exists; refusing to overwrite it: {settings_backup}')
        # The config directory is an observed mount sentinel. Create only below it.
        for directory in (workspace, _remote_join(workspace, 'saves'),
                          _remote_join(workspace, 'scenarios'),
                          _remote_join(workspace, 'config'), _remote_join(workspace, 'runs')):
            storage.ensure_directory(directory)
        if old_config is not None:
            config_backup.write_bytes(old_config)
            os.chmod(config_backup, 0o600)
        if old_settings is not None:
            settings_backup.write_bytes(old_settings)
            os.chmod(settings_backup, 0o600)
        config_bytes = (json.dumps(_runtime_config(args, token), indent=2, sort_keys=True) + '\n').encode()
        try:
            storage.upload_file(save_path, _remote_join(workspace, 'saves', save_name))
            storage.write_bytes(_remote_join(workspace, 'scenarios', scenario_name), scenario_data)
            storage.write_bytes(remote_settings, settings_bytes)
            storage.write_bytes(args.remote_config, config_bytes)
        except Exception as write_error:
            restore_errors = []
            for path, previous in ((remote_settings, old_settings), (args.remote_config, old_config)):
                try:
                    if previous is None:
                        _delete_optional(storage, path)
                    else:
                        storage.write_bytes(path, previous)
                except Exception as restore_error:
                    restore_errors.append(f'{path}: {restore_error}')
            if restore_errors:
                raise BenchmarkError(
                    'provisioning failed and prior benchmark settings/config could not be restored: ' +
                    '; '.join(restore_errors)) from write_error
            raise
    result = {
        'status': 'prepared_for_next_startup',
        'scenario': scenario_name, 'save': save_name,
        'scenario_sha256': sha256(scenario_data),
        'source_scenario_sha256': sha256(scenario_bytes),
        'save_sha256': sha256_file(save_path),
        'settings_sha256': sha256(settings_bytes),
        'previous_config_backup': str(config_backup) if old_config is not None else None,
        'previous_settings_backup': str(settings_backup) if old_settings is not None else None,
        'remote_workspace': workspace,
        'note': 'No title was launched or stopped. Restart normally to load the enabled benchmark config.',
    }
    (args.output / 'prepare_summary.json').write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    return result


def _run_one(args, token, scenario, scenario_bytes, save_path, settings_bytes,
             run_index, analyzer, launch_callback):
    run_id = f"run-{run_index:02d}-{uuid.uuid4().hex[:12]}"
    save_hash = sha256_file(save_path)
    scenario_name = run_id + '.json'
    save_name = run_id + (Path(save_path).suffix or '.omwsave')
    _, scenario_data = _verify_scenario_remote_data(scenario, save_name)
    run_dir = args.output / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / 'source_scenario.json').write_bytes(scenario_bytes)
    (run_dir / 'scenario.json').write_bytes(scenario_data)
    (run_dir / 'test_save.sha256').write_text(save_hash + '\n')
    workspace = None
    remote_run = None
    client = None
    storage = None
    launch = {'mode': 'manual_attach', 'title_id': TITLE_ID} if args.attach else None
    owns_title = False
    collection_attempted = False
    scenario_started = False
    try:
        if not args.attach:
            launch = launch_callback(
                args.host, TITLE_ID, payload_path=args.launcher_payload,
                loader_port=args.loader_port, lifecycle_port=args.lifecycle_port,
                timeout_s=args.operation_timeout, poll_interval_s=args.poll_interval)
            owns_title = True

        client = BenchmarkClient(args.host, args.control_port, token,
                                 timeout=args.request_timeout)
        client.connect_until(deadline=time.monotonic() + args.operation_timeout,
                             poll_interval=args.poll_interval)
        before = client.wait_for(
            lambda status: status['game_state'] in ('menu', 'ready', 'paused'),
            deadline=time.monotonic() + args.operation_timeout,
            poll_interval=args.poll_interval, description='game menu or world readiness')
        if before['capture_state'] not in ('idle', 'complete', 'incomplete'):
            raise BenchmarkError(f"runtime already has an active capture: {before['capture_state']}")

        # FTP is contacted only after the title control service reports its readiness.
        storage = _wait_for_mount(args)
        workspace, _, config_names = _require_mount(storage, args, preparing=False)
        if 'benchmark.json' not in config_names:
            raise BenchmarkError('active benchmark config is missing from the mounted config directory')
        _validate_active_config(storage, args, token)
        remote_settings = _remote_join(workspace, 'config', 'settings.cfg')
        effective_settings = storage.read_bytes(remote_settings)
        (run_dir / 'effective_settings.cfg').write_bytes(effective_settings)
        if sha256(effective_settings) != sha256(settings_bytes):
            raise BenchmarkError('isolated settings differ from --settings; run --prepare and restart before benchmarking')

        remote_save = _remote_join(workspace, 'saves', save_name)
        remote_scenario = _remote_join(workspace, 'scenarios', scenario_name)
        remote_run = _remote_join(workspace, 'runs', run_id)
        storage.upload_file(save_path, remote_save)
        storage.write_bytes(remote_scenario, scenario_data)

        load_reply = client.command('load_save', save=save_name)
        load_deadline = time.monotonic() + args.operation_timeout
        while True:
            state = client.status()
            if state['last_command_id'] == load_reply['id']:
                if state['last_command_error']:
                    raise BenchmarkError(f"save load failed: {state['last_command_error']}")
                if state['ready'] and state['loaded_save'] == save_name:
                    break
            if time.monotonic() >= load_deadline:
                raise TimeoutError('timed out waiting for the requested save to become world-ready')
            time.sleep(min(args.poll_interval, max(0, load_deadline - time.monotonic())))
        started = client.command('run_scenario', scenario=scenario_name, run_id=run_id)
        if not started['accepted']:
            raise BenchmarkError('runtime rejected the scenario')
        scenario_started = True
        run_deadline = time.monotonic() + min(
            MAX_RUN_TIMEOUT, args.operation_timeout + scenario['warmup_seconds'] + scenario['duration_seconds'])
        while True:
            state = client.status()
            result_is_current = (state['run_id'] == run_id and
                                 state['last_command_id'] == started['id'])
            if result_is_current and state['last_command_error']:
                raise BenchmarkError(f"scenario command failed: {state['last_command_error']}")
            if result_is_current and state['capture_state'] in ('incomplete', 'error'):
                raise BenchmarkError(f"runtime capture {state['capture_state']}: {state['error'] or 'no reason reported'}")
            if result_is_current and state['capture_state'] == 'complete' and state['completed']:
                break
            if time.monotonic() >= run_deadline:
                raise TimeoutError('timed out waiting for capture finalization')
            time.sleep(min(args.poll_interval, max(0, run_deadline - time.monotonic())))

        collection_attempted = True
        manifest, manifest_bytes, frames_bytes = _collect(
            storage, remote_run, run_dir, run_id, args.remote_root)

        frame_capacity = manifest.get('frame_capacity')
        if (not isinstance(frame_capacity, int) or isinstance(frame_capacity, bool) or
                frame_capacity <= 0):
            raise BenchmarkError('runtime manifest has no valid frame capacity')
        if manifest['frame_count'] > manifest['frame_capacity']:
            raise BenchmarkError('runtime frame count exceeds the manifest capacity')
        if manifest['frame_count'] > args.max_frames:
            raise BenchmarkError('runtime frame count exceeds the configured capture capacity')
        expected_hashes = {
            'scenario_sha256': sha256(scenario_data),
            'save_sha256': save_hash,
            'settings_sha256': sha256(effective_settings),
        }
        for field, expected in expected_hashes.items():
            if manifest.get(field) is not None and manifest[field] != expected:
                raise BenchmarkError(f'runtime manifest {field} does not match the prepared input')
        analysis = analyzer.analyze_capture(frames_bytes.decode('utf-8'), manifest)
        metadata = {
            'protocol_version': 1, 'run_id': run_id, 'title_id': TITLE_ID,
            'launch': launch, 'scenario_file': scenario_name,
            'scenario_sha256': sha256(scenario_data),
            'source_scenario_sha256': sha256(scenario_bytes),
            'save_file': save_name, 'save_sha256': save_hash,
            'settings_file': 'settings.cfg',
            'settings_sha256': sha256(effective_settings),
            'effective_settings': effective_settings.decode('utf-8', errors='replace'),
            'runtime_manifest': manifest,
            'runtime_manifest_sha256': sha256(manifest_bytes),
            'diagnostics': json.loads((run_dir / 'diagnostics/collection.json').read_text()),
            'capture_complete': True,
            'build_metadata': args.build_metadata,
        }
        (run_dir / 'run_metadata.json').write_text(json.dumps(metadata, indent=2, sort_keys=True) + '\n')
        summary = {
            'run_id': run_id, 'status': 'complete', 'analysis': analysis,
            'source_scenario_sha256': metadata['source_scenario_sha256'],
            'save_sha256': metadata['save_sha256'],
            'settings_sha256': metadata['settings_sha256'],
        }
        (run_dir / 'summary.json').write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n')
        return summary
    except Exception as error:
        error_text = f'{type(error).__name__}: {error}'
        (run_dir / 'run_metadata.json').write_text(json.dumps({
            'protocol_version': 1, 'run_id': run_id, 'status': 'incomplete',
            'error': error_text,
            'scenario_sha256': sha256(scenario_data),
            'source_scenario_sha256': sha256(scenario_bytes),
            'save_sha256': save_hash,
            'settings_sha256': sha256(effective_settings) if 'effective_settings' in locals()
                               else sha256(settings_bytes),
            'effective_settings': (effective_settings.decode('utf-8', errors='replace')
                                   if 'effective_settings' in locals() else None),
            'build_metadata': args.build_metadata,
        }, indent=2, sort_keys=True) + '\n')
        if storage is not None and remote_run is not None and not collection_attempted:
            collection_attempted = True
            try:
                _collect(storage, remote_run, run_dir, run_id, args.remote_root)
            except Exception as collection_error:
                (run_dir / 'collection_error.txt').write_text(str(collection_error) + '\n')
        (run_dir / 'summary.json').write_text(json.dumps({
            'run_id': run_id, 'status': 'incomplete', 'error': error_text,
            'artifact_collection_attempted': collection_attempted,
        }, indent=2, sort_keys=True) + '\n')
        raise
    finally:
        cleanup_errors = []
        capture_stop_requested = False
        if client is not None:
            try:
                state = client.status()
                if scenario_started and state['capture_state'] in (
                        'loading', 'waiting_world', 'warming', 'capturing'):
                    capture_stop_requested = True
                    client.command('stop_capture')
                    deadline = time.monotonic() + args.operation_timeout
                    while time.monotonic() < deadline:
                        state = client.status()
                        if state['capture_state'] not in (
                                'loading', 'waiting_world', 'warming', 'capturing'):
                            break
                        time.sleep(min(args.poll_interval, max(0, deadline - time.monotonic())))
            except Exception as error:
                cleanup_errors.append(f'capture stop/status: {error}')
            if (storage is not None and remote_run is not None and
                    (not collection_attempted or capture_stop_requested)):
                collection_attempted = True
                try:
                    _collect(storage, remote_run, run_dir, run_id, args.remote_root)
                except Exception as error:
                    cleanup_errors.append(f'collect before quit: {error}')
        if client is not None and owns_title:
            try:
                client.command('quit')
                from tools.benchmark_launcher import wait_for_title_stopped
                stopped = wait_for_title_stopped(
                    args.host, TITLE_ID, lifecycle_port=args.lifecycle_port,
                    timeout_s=args.operation_timeout, poll_interval_s=args.poll_interval)
                if stopped.get('stopped') is not True:
                    raise BenchmarkError('lifecycle helper did not confirm title process stopped')
            except Exception as error:
                cleanup_errors.append(f'normal quit/process stop: {error}')
        if storage is not None:
            storage.close()
        if client is not None:
            client.close()
        if cleanup_errors:
            (run_dir / 'cleanup_errors.txt').write_text('\n'.join(cleanup_errors) + '\n')
            metadata_path = run_dir / 'run_metadata.json'
            metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {
                'protocol_version': 1, 'run_id': run_id}
            metadata.update(status='incomplete', capture_complete=False,
                            cleanup_errors=cleanup_errors)
            metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + '\n')
            summary_path = run_dir / 'summary.json'
            if summary_path.exists():
                summary = json.loads(summary_path.read_text())
                summary.update(status='incomplete', cleanup_errors=cleanup_errors)
                summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n')
            raise BenchmarkError('benchmark cleanup failed; see cleanup_errors.txt')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--prepare', action='store_true',
                      help='Provision benchmark-only config, save, scenario, and isolated settings while FTP mount is active')
    mode.add_argument('--attach', action='store_true',
                      help='Run against an already launched title; no title launch is sent')
    parser.add_argument('--host', required=True, help='Console address reachable from this host')
    parser.add_argument('--scenario', required=True, type=Path)
    parser.add_argument('--save', required=True, type=Path, help='Local test-save copy; the original save is not modified')
    parser.add_argument('--settings', required=True, type=Path, help='Settings snapshot staged only by --prepare')
    parser.add_argument('--output', type=Path, default=REPO / 'build/benchmark-runs')
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--token-file', type=Path,
                        default=Path.home() / '.config/openmw-ps5-benchmark/token',
                        help='Private file for the generated 32+ hexadecimal runtime token')
    from benchmark_launcher import DEFAULT_LIFECYCLE_PORT, DEFAULT_LOADER_PORT, DEFAULT_PAYLOAD_PATH
    parser.add_argument('--loader-port', type=int, default=DEFAULT_LOADER_PORT)
    parser.add_argument('--lifecycle-port', type=int, default=DEFAULT_LIFECYCLE_PORT)
    parser.add_argument('--launcher-payload', type=Path, default=DEFAULT_PAYLOAD_PATH)
    parser.add_argument('--ftp-port', type=int, default=2121)
    parser.add_argument('--ftp-user', default='anonymous')
    parser.add_argument('--ftp-password', default='benchmark')
    parser.add_argument('--remote-root', default='/download0', help='FTP-visible active mount root; override for the observed console path')
    parser.add_argument('--remote-config', default='/download0/config/benchmark.json', help='FTP path for the opt-in benchmark config')
    parser.add_argument('--build-meta', type=Path,
                        help='Optional packaged buildmeta.json to archive with each run')
    parser.add_argument('--ftp-timeout', type=float, default=10)
    parser.add_argument('--control-port', type=int, default=9137)
    parser.add_argument('--bind', default='0.0.0.0')
    parser.add_argument('--max-frames', type=int, default=36000)
    parser.add_argument('--request-timeout', type=float, default=5)
    parser.add_argument('--operation-timeout', type=float, default=300)
    parser.add_argument('--poll-interval', type=float, default=0.5)
    args = parser.parse_args(argv)
    if args.repeat < 1 or args.repeat > 100:
        parser.error('--repeat must be between 1 and 100')
    if (args.prepare or args.attach) and args.repeat != 1:
        parser.error('--prepare and --attach support one run')
    if args.max_frames < 1 or args.max_frames > 36000:
        parser.error('--max-frames must be between 1 and 36000')
    if (not 1 <= args.ftp_port <= 65535 or not 1 <= args.control_port <= 65535 or
            not 1 <= args.loader_port <= 65535 or not 1 <= args.lifecycle_port <= 65535):
        parser.error('FTP, control, loader, and lifecycle ports must be between 1 and 65535')
    if (not math.isfinite(args.operation_timeout) or args.operation_timeout <= 0 or
            args.operation_timeout > MAX_OPERATION_TIMEOUT):
        parser.error('--operation-timeout must be between 0 and 7200 seconds')
    for value, label in ((args.request_timeout, 'request timeout'),
                         (args.ftp_timeout, 'FTP timeout'),
                         (args.poll_interval, 'poll interval')):
        if not math.isfinite(value) or value <= 0:
            parser.error(f'{label} must be positive and finite')
    for path, label in ((args.scenario, 'scenario'), (args.save, 'save'), (args.settings, 'settings')):
        if not path.is_file():
            parser.error(f'{label} file does not exist: {path}')
    args.build_metadata = None
    if args.build_meta is not None:
        if not args.build_meta.is_file():
            parser.error(f'build metadata file does not exist: {args.build_meta}')
        try:
            args.build_metadata = json.loads(args.build_meta.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            parser.error(f'build metadata is not valid JSON: {error}')
        if not isinstance(args.build_metadata, dict):
            parser.error('build metadata must be a JSON object')
    if not args.prepare and not args.token_file.is_file():
        parser.error(f'token file does not exist; run --prepare first: {args.token_file}')
    try:
        FTPStorage._remote_path(args.remote_root)
        FTPStorage._remote_path(args.remote_config)
    except ValueError as error:
        parser.error(str(error))
    return args


def main(argv=None):
    args = parse_args(argv)
    scenario, scenario_bytes, _, _, _ = load_scenario(args.scenario)
    settings_bytes = args.settings.read_bytes()
    if not settings_bytes:
        raise SystemExit('settings snapshot cannot be empty')
    args.output.mkdir(parents=True, exist_ok=True)
    token = _load_token(args.token_file, create=args.prepare)
    if args.prepare:
        prepared = _prepare(args, scenario, scenario_bytes, args.save, settings_bytes, token)
        print('Benchmark config and test copies are prepared for the next startup.')
        print('No title was launched or stopped. Normal-exit the current title, then restart it to load the enabled config.')
        print('Preparation summary: ' + str(args.output / 'prepare_summary.json'))
        return prepared

    analyzer = _load_analyzer()
    if not args.attach:
        from tools.benchmark_launcher import launch_title
        launch_callback = launch_title
    else:
        launch_callback = None
    remote_root = args.remote_root.rstrip('/')
    print(f'Host benchmark protocol v1; remote workspace {remote_root}/benchmark')
    failures = []
    summaries = []
    for run_index in range(1, args.repeat + 1):
        try:
            result = _run_one(args, token, scenario, scenario_bytes,
                              args.save, settings_bytes, run_index, analyzer, launch_callback)
            summaries.append(result)
            print(f"[{run_index}/{args.repeat}] complete: {result['run_id']}")
        except Exception as error:
            failures.append({'run_index': run_index,
                             'error': f'{type(error).__name__}: {error}'})
            print(f'[{run_index}/{args.repeat}] incomplete: {type(error).__name__}: {error}',
                  file=sys.stderr)
            break
    metric_values = {}
    for field, extractor in (
            ('mean_fps', lambda result: result['analysis']['mean_fps']),
            ('frame_p95_ms', lambda result: result['analysis']['frame_interval_ms']['p95_ms']),
            ('frame_p99_ms', lambda result: result['analysis']['frame_interval_ms']['p99_ms'])):
        values = [extractor(result) for result in summaries]
        if values:
            metric_values[field] = {
                'median_across_runs': statistics.median(values),
                'mean_across_runs': statistics.fmean(values),
                'minimum': min(values),
                'maximum': max(values),
            }
    aggregate = {
        'protocol_version': 1,
        'status': 'complete' if not failures and len(summaries) == args.repeat else 'incomplete',
        'requested_repeats': args.repeat,
        'completed_repeats': len(summaries),
        'failures': failures,
        'repeat_metrics': metric_values,
        'runs': summaries,
    }
    (args.output / 'repeat_summary.json').write_text(
        json.dumps(aggregate, indent=2, sort_keys=True) + '\n')
    print('Repeat summary: ' + str(args.output / 'repeat_summary.json'))
    if failures:
        raise SystemExit(1)
    return aggregate


if __name__ == '__main__':
    main()