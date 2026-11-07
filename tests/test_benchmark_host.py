import ftplib
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / 'tools'
sys.path.insert(0, str(TOOLS))
from benchmark_client import BenchmarkClient, BenchmarkError, FTPStorage, ProtocolError


def load_script(name, module_name):
    spec = importlib.util.spec_from_file_location(module_name, TOOLS / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = load_script('run-benchmark.py', 'run_benchmark')
console_game = load_script('console-game.py', 'console_game')
pacing = load_script('analyze-frame-pacing.py', 'pacing_for_host_test')
packager = load_script('pack-openmw.py', 'pack_for_host_test')


class BuildProvenanceTests(unittest.TestCase):
    def test_extracted_engine_does_not_inherit_enclosing_port_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '--quiet', str(root)], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture',
                            '-c', 'user.email=fixture@example.invalid',
                            'commit', '--quiet', '--allow-empty', '-m', 'fixture'],
                           check=True)
            engine = root / 'openmw'
            engine.mkdir()
            self.assertEqual(packager.git_identity(engine),
                             {'revision': None, 'worktree': 'unknown'})
class ReplyHandler(socketserver.StreamRequestHandler):
    response_id_delta = 0
    reject = False
    oversize = False
    delay_s = 0
    requests = []

    def handle(self):
        request = json.loads(self.rfile.readline())
        type(self).requests.append(request)
        if type(self).oversize:
            self.wfile.write(b'x' * 65_537 + b'\n')
            return
        reply = {'version': 1, 'id': request['id'] + type(self).response_id_delta,
                 'accepted': not type(self).reject, 'complete': True}
        if type(self).reject:
            reply['error'] = 'rejected by fixture'
        if request['command'] == 'status':
            reply['status'] = {
                'ready': True, 'game_state': 'ready', 'run_id': None,
                'capture_state': 'idle', 'completed': False,
                'manifest': None, 'frames': None, 'error': None, 'loaded_save': None,
                'last_command_id': 0, 'last_command_error': '',
            }
        if type(self).delay_s:
            time.sleep(type(self).delay_s)
            return
        self.wfile.write((json.dumps(reply) + '\n').encode())


class ControlSocketFixture:
    def __init__(self, states, events, *, port=0):
        self.states = list(states)
        self.events = events
        self.requests = []
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                owner.events.append(('control_connected',))
                while line := self.rfile.readline():
                    request = json.loads(line)
                    owner.requests.append(request)
                    reply = {'version': 1, 'id': request['id'],
                             'accepted': True, 'complete': True}
                    if request['command'] == 'status':
                        state = owner.states.pop(0) if len(owner.states) > 1 else owner.states[0]
                        owner.events.append(('game_state', state['game_state']))
                        reply['status'] = {
                            'ready': state['ready'], 'game_state': state['game_state'],
                            'run_id': None, 'capture_state': 'idle', 'completed': False,
                            'manifest': None, 'frames': None, 'error': None,
                            'loaded_save': None, 'last_command_id': 0,
                            'last_command_error': None,
                        }
                    else:
                        owner.events.append(('command', request['command']))
                    self.wfile.write(json.dumps(reply).encode() + bytes((10,)))
                    self.wfile.flush()

        self.server = socketserver.ThreadingTCPServer(('127.0.0.1', port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class ClientProtocolTests(unittest.TestCase):
    def setUp(self):
        ReplyHandler.response_id_delta = 0
        ReplyHandler.reject = False
        ReplyHandler.oversize = False
        ReplyHandler.delay_s = 0
        self.server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), ReplyHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.token = 'a' * 32

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def client(self):
        return BenchmarkClient('127.0.0.1', self.server.server_address[1], self.token)

    def test_response_id_mismatch_and_oversize_reply_fail_closed(self):
        ReplyHandler.response_id_delta = 1
        with self.client() as client, self.assertRaises(ProtocolError):
            client.request('status')
        ReplyHandler.response_id_delta = 0
        ReplyHandler.oversize = True
        with self.client() as client, self.assertRaises(ProtocolError):
            client.request('status')

    def test_remote_command_rejection_is_not_reported_as_success(self):
        ReplyHandler.reject = True
        with self.client() as client, self.assertRaisesRegex(BenchmarkError, 'rejected by fixture'):
            client.command('load_save', save='fixture.omwsave')

    def test_wait_for_restores_configured_timeout_after_success(self):
        with self.client() as client:
            status = client.wait_for(
                lambda value: value['game_state'] == 'ready',
                deadline=time.monotonic() + 1, poll_interval=0.001,
            )
            self.assertEqual(status['game_state'], 'ready')
            self.assertEqual(client.timeout, 5.0)
    def test_wait_for_bounds_status_read_by_remaining_deadline(self):
        ReplyHandler.delay_s = 0.1
        with self.client() as client:
            with self.assertRaises(BenchmarkError):
                client.wait_for(
                    lambda status: False, deadline=time.monotonic() + 0.02,
                    poll_interval=0.001, description='delayed status',
                )


class ScenarioContractTests(unittest.TestCase):
    def test_boolean_axes_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'scenario.json'
            scenario = {'version': 1, 'save': 'fixture.omwsave',
                        'warmup_seconds': 0, 'duration_seconds': 1,
                        'events': [{'time': 0, 'input': {'forward': True}}]}
            path.write_text(json.dumps(scenario))
            with self.assertRaisesRegex(ValueError, 'forward'):
                runner.load_scenario(path)

    def test_runtime_event_capacity_is_enforced_before_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'scenario.json'
            path.write_text(json.dumps({
                'version': 1, 'save': 'fixture.omwsave',
                'warmup_seconds': 0, 'duration_seconds': 1,
                'events': [{'time': 0, 'input': {}}] * 4097,
            }))
            with self.assertRaisesRegex(ValueError, '4096'):
                runner.load_scenario(path)


class FakeFTP:
    def __init__(self, listing=None):
        self.listing = listing or []
        self.commands = []

    def nlst(self, path):
        self.commands.append(('NLST', path))
        raise ftplib.error_perm('502 Command not recognized')

    def retrlines(self, command, callback):
        self.commands.append(('LIST', command))
        for line in self.listing:
            callback(line)


class FTPCompatibilityTests(unittest.TestCase):
    def test_list_502_falls_back_to_list_and_keeps_names_with_spaces(self):
        storage = FTPStorage('unused')
        storage.ftp = FakeFTP(['drwxr-xr-x 2 user group 4096 Jan 01 00:00 benchmark data',
                               '-rw-r--r-- 1 user group 12 Jan 01 00:00 config.json'])
        self.assertEqual(storage.list_names('/download0'), ['benchmark data', 'config.json'])
        self.assertEqual([command[0] for command in storage.ftp.commands], ['NLST', 'LIST'])

    def test_remote_paths_reject_relative_and_parent_traversal(self):
        for path in ('download0/benchmark', '/download0/../save', '/download0/line\nbreak'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                FTPStorage._remote_path(path)


class MemoryStorage:
    def __init__(self, events=None, files=None):
        self.events = events if events is not None else []
        self.files = dict(files or {})
        self.ftp = self
        self.fail_config_write_once = False

    def connect(self):
        self.events.append(('ftp_connect',))
        return self

    def close(self):
        self.events.append(('ftp_close',))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False

    def list_names(self, path):
        self.events.append(('ftp_list', path))
        if path == '/download0':
            return ['config', 'benchmark']
        if path == '/download0/config':
            return ['benchmark.json'] if '/download0/config/benchmark.json' in self.files else []
        if path == '/download0/benchmark':
            return ['saves', 'scenarios', 'config', 'runs']
        return []

    def ensure_directory(self, path):
        self.events.append(('ftp_mkdir_under_sentinel', path))

    def upload_file(self, local, remote):
        self.events.append(('ftp_upload', remote))
        self.files[remote] = Path(local).read_bytes()

    def write_bytes(self, remote, data):
        self.events.append(('ftp_write', remote))
        if remote == '/download0/config/benchmark.json' and self.fail_config_write_once:
            self.fail_config_write_once = False
            raise OSError('fixture config write failure')
        self.files[remote] = data

    def read_bytes(self, remote):
        if remote not in self.files:
            raise ftplib.error_perm('550 not found')
        return self.files[remote]

    def download_file(self, remote, local, *, max_bytes=128 * 1024 * 1024):
        self.events.append(('ftp_download', remote))
        data = self.read_bytes(remote)
        if len(data) > max_bytes:
            raise BenchmarkError('fixture exceeds download limit')
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        Path(local).write_bytes(data)
        return data

    def delete(self, remote):
        if remote not in self.files:
            raise ftplib.error_perm('550 not found')
        del self.files[remote]


class StorageFactory:
    _remote_path = staticmethod(FTPStorage._remote_path)

    def __init__(self, storage):
        self.storage = storage

    def __call__(self, *args, **kwargs):
        return self.storage


class RuntimeFixture:
    def __init__(self, storage, events, frame_csv):
        self.storage, self.events, self.frame_csv = storage, events, frame_csv
        self.loaded_save = None
        self.run_id = None
        self.capture_state = 'idle'
        self.last_command_id = 0
        self.last_command_error = ''

    def __call__(self, host, port, token, timeout=5.0):
        return RuntimeConnection(self)


class RuntimeConnection:
    def __init__(self, fixture):
        self.fixture = fixture

    def connect_until(self, **kwargs):
        return None

    def close(self):
        return None

    def wait_for(self, predicate, **kwargs):
        status = self.status()
        if not predicate(status):
            raise AssertionError('fixture did not reach requested status')
        return status

    def status(self):
        return {
            'ready': self.fixture.loaded_save is not None,
            'game_state': 'menu' if self.fixture.loaded_save is None else 'ready',
            'run_id': self.fixture.run_id, 'capture_state': self.fixture.capture_state,
            'completed': self.fixture.capture_state == 'complete',
            'manifest': 'manifest.json' if self.fixture.capture_state == 'complete' else None,
            'frames': 'frames.csv' if self.fixture.capture_state == 'complete' else None,
            'error': None, 'loaded_save': self.fixture.loaded_save,
            'last_command_id': self.fixture.last_command_id,
            'last_command_error': self.fixture.last_command_error,
        }

    def command(self, command, **fields):
        self.fixture.events.append(('command', command))
        self.fixture.last_command_id += 1
        request_id = self.fixture.last_command_id
        if command == 'load_save':
            self.fixture.loaded_save = fields['save']
        elif command == 'run_scenario':
            self.fixture.run_id = fields['run_id']
            self.fixture.capture_state = 'complete'
            remote = f'/download0/benchmark/runs/{fields["run_id"]}'
            manifest = {
                'schema_version': 1, 'run_id': fields['run_id'], 'status': 'complete',
                'frame_count': 1, 'frame_capacity': 36000, 'reason': '',
                'frames_file': 'frames.csv',
                'frames_sha256': hashlib.sha256(self.fixture.frame_csv).hexdigest(),
            }
            self.fixture.storage.files[f'{remote}/manifest.json'] = json.dumps(manifest).encode()
            self.fixture.storage.files[f'{remote}/frames.csv'] = self.fixture.frame_csv
        return {'id': request_id, 'accepted': True, 'complete': command == 'load_save'}


class HostLifecycleTests(unittest.TestCase):
    def fixture_inputs(self, root):
        scenario = ROOT / 'config/benchmark-example-idle.json'
        save = root / 'fixture.omwsave'
        save.write_bytes(b'test save copy')
        settings = root / 'settings.cfg'
        settings.write_text('video\n    resolution x = 1920\n')
        return scenario, save, settings

    def test_runner_uses_direct_lifecycle_options_without_engine_url(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scenario, save, settings = self.fixture_inputs(root)
            token_file = root / 'token'
            token_file.write_text('a' * 64)
            args = ['--host', '127.0.0.1', '--scenario', str(scenario),
                    '--save', str(save), '--settings', str(settings),
                    '--token-file', str(token_file)]
            defaults = runner.parse_args(args)
            self.assertEqual(defaults.loader_port, 9021)
            self.assertEqual(defaults.lifecycle_port, 9138)
            self.assertEqual(defaults.launcher_payload.name, 'benchmark-launcher.elf')
            overridden = runner.parse_args(args + [
                '--loader-port', '10021', '--lifecycle-port', '10138',
                '--launcher-payload', str(root / 'custom.elf'),
            ])
            self.assertEqual((overridden.loader_port, overridden.lifecycle_port),
                             (10021, 10138))
            with self.assertRaises(SystemExit):
                runner.parse_args(args + ['--engine-url', 'http://localhost'])
    def test_console_game_start_waits_for_authenticated_runtime_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            token_file = root / 'token'
            token_file.write_text('a' * 64)
            events = []
            servers = []
            delayed_start = []
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                control_port = probe.getsockname()[1]

            def launch(*args, **kwargs):
                events.append(('launch',))
                def start_control_service():
                    time.sleep(0.05)
                    servers.append(ControlSocketFixture([
                        {'ready': False, 'game_state': 'starting'},
                        {'ready': False, 'game_state': 'menu'},
                    ], events, port=control_port))
                thread = threading.Thread(target=start_control_service)
                thread.start()
                delayed_start.append(thread)
                return {'title_id': 'PPSA99630',
                        'launch_response': {'launch_result': 0},
                        'process': {'running': True, 'pid': 81}}

            try:
                with patch.object(console_game, 'launch_title', side_effect=launch):
                    result = console_game.main([
                        'start', '--host', '127.0.0.1', '--token-file', str(token_file),
                        '--control-port', str(control_port), '--timeout', '2',
                        '--poll-interval', '0.005',
                    ])
            finally:
                for thread in delayed_start:
                    thread.join(timeout=2)
                for server in servers:
                    server.close()
            self.assertEqual(result, 0)
            self.assertLess(events.index(('launch',)), events.index(('game_state', 'menu')))
            self.assertEqual(
                [event for event in events if event[0] == 'game_state'],
                [('game_state', 'starting'), ('game_state', 'menu')],
            )
            self.assertEqual(events.count(('control_connected',)), 1)
            self.assertEqual([request['command'] for request in servers[0].requests],
                             ['status', 'status'])

    def test_console_game_stop_sends_normal_quit_before_waiting_for_absence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            token_file = root / 'token'
            token_file.write_text('a' * 64)
            events = []
            server = ControlSocketFixture(
                [{'ready': False, 'game_state': 'menu'}], events)

            def wait_stopped(*args, **kwargs):
                events.append(('stopped',))
                return {'title_id': 'PPSA99630', 'stopped': True, 'process': None}

            try:
                with patch.object(console_game, 'title_status',
                                  return_value={'running': True, 'pid': 81}), \
                        patch.object(console_game, 'wait_for_title_stopped',
                                     side_effect=wait_stopped):
                    result = console_game.main([
                        'stop', '--host', '127.0.0.1', '--token-file', str(token_file),
                        '--control-port', str(server.port), '--timeout', '2',
                    ])
            finally:
                server.close()
            self.assertEqual(result, 0)
            self.assertLess(events.index(('command', 'quit')), events.index(('stopped',)))
            self.assertEqual(server.requests[0]['command'], 'quit')
            self.assertEqual(server.requests[0]['token'], 'a' * 64)
    def frame_csv(self):
        header = ','.join(pacing.FRAME_FIELDS)
        row = '1,capture,1000000,20000000,3000000,5000000,100000,11900000,-1,0,0,1,1,2,3,0.1,0.2,0.3'
        return (header + '\n' + row + '\n').encode()

    def test_missing_manifest_does_not_prevent_frame_or_log_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary) / 'run'
            remote_run = '/download0/benchmark/runs/missing-manifest'
            storage = MemoryStorage(files={
                remote_run + '/frames.csv': self.frame_csv(),
                '/download0/stderr.log': b'engine diagnostic',
            })
            with self.assertRaisesRegex(BenchmarkError, 'required runtime artifacts'):
                runner._collect(storage, remote_run, local, 'missing-manifest', '/download0')
            self.assertEqual((local / 'frames.csv').read_bytes(), self.frame_csv())
            self.assertEqual((local / 'diagnostics/stderr.log').read_bytes(), b'engine diagnostic')
            collection = json.loads((local / 'diagnostics/collection.json').read_text())
            self.assertEqual(collection['downloaded'][0]['path'], 'stderr.log')
            self.assertIn('stdout.log', collection['missing_optional'])
            downloaded = [event[1] for event in storage.events if event[0] == 'ftp_download']
            self.assertIn(remote_run + '/manifest.json', downloaded)
            self.assertIn(remote_run + '/frames.csv', downloaded)

    def run_cli(self, root, attach, repeat=1):
        scenario, save, settings = self.fixture_inputs(root)
        output = root / 'runs'
        events = []
        token = 'b' * 64
        settings_bytes = settings.read_bytes()
        active_config = {
            'version': 1, 'enabled': True,
            'control': {'enabled': True, 'bind': '0.0.0.0', 'port': 9137, 'token': token},
            'capture': {'enabled': True, 'max_frames': 36000}, 'startup_scenario': '',
        }
        storage = MemoryStorage(events, {
            '/download0/config/benchmark.json': json.dumps(active_config).encode(),
            '/download0/benchmark/config/settings.cfg': settings_bytes,
        })
        runtime = RuntimeFixture(storage, events, self.frame_csv())
        def launch_title(host, title_id, **kwargs):
            events.append(('launch', host, title_id, kwargs['loader_port'],
                           kwargs['lifecycle_port'], Path(kwargs['payload_path']).name))
            events.append(('launch',))
            return {'title_id': 'PPSA99630',
                    'launch_response': {'launch_result': 0},
                    'process': {'running': True, 'pid': 81}}
        def wait_for_title_stopped(host, title_id, **kwargs):
            events.append(('process_stopped',))
            return {'title_id': 'PPSA99630', 'stopped': True, 'process': None}
        adapter = types.SimpleNamespace(launch_title=launch_title,
                                        wait_for_title_stopped=wait_for_title_stopped)
        token_file = root / 'private-token'
        token_file.write_text(token + '\n')
        arguments = ['--host', '127.0.0.1', '--scenario', str(scenario), '--save', str(save),
                     '--settings', str(settings), '--output', str(output), '--remote-root', '/download0',
                     '--remote-config', '/download0/config/benchmark.json', '--token-file', str(token_file),
                     '--poll-interval', '0.001', '--repeat', str(repeat)]
        if attach:
            arguments.append('--attach')
        with patch.object(runner, 'FTPStorage', StorageFactory(storage)), \
             patch.object(runner, 'BenchmarkClient', runtime), \
             patch.dict(sys.modules, {'tools.benchmark_launcher': adapter}):
            runner.main(arguments)
        return storage, events, output, save

    def test_automatic_launch_waits_for_mount_collects_quits_and_confirms_stop(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage, events, output, save = self.run_cli(Path(temporary), attach=False)
            self.assertLess(events.index(('launch',)), events.index(('ftp_connect',)))
            self.assertLess(events.index(('command', 'run_scenario')),
                            events.index(('command', 'quit')))
            self.assertLess(events.index(('command', 'quit')), events.index(('process_stopped',)))
            run_dirs = [path for path in output.iterdir() if path.is_dir()]
            self.assertEqual(len(run_dirs), 1)
            self.assertEqual(json.loads((run_dirs[0] / 'summary.json').read_text())['status'], 'complete')
            self.assertEqual(hashlib.sha256(save.read_bytes()).hexdigest(),
                             json.loads((run_dirs[0] / 'run_metadata.json').read_text())['save_sha256'])
            metadata = json.loads((run_dirs[0] / 'run_metadata.json').read_text())
            self.assertEqual(metadata['runtime_manifest_sha256'],
                             hashlib.sha256((run_dirs[0] / 'manifest.json').read_bytes()).hexdigest())
            self.assertTrue((run_dirs[0] / 'source_scenario.json').is_file())
            self.assertTrue((run_dirs[0] / 'scenario.json').is_file())
            self.assertEqual((run_dirs[0] / 'test_save.sha256').read_text().strip(),
                             hashlib.sha256(save.read_bytes()).hexdigest())
            self.assertEqual((run_dirs[0] / 'effective_settings.cfg').read_text(),
                             'video\n    resolution x = 1920\n')
            self.assertEqual(storage.files['/download0/benchmark/config/settings.cfg'],
                             b'video\n    resolution x = 1920\n')
            self.assertEqual(json.loads(storage.files['/download0/config/benchmark.json'])
                             ['control']['token'], 'b' * 64)
            launch_args = next(event for event in events
                               if isinstance(event, tuple) and len(event) > 2 and event[0] == 'launch')
            self.assertEqual(launch_args[1:5], ('127.0.0.1', 'PPSA99630', 9021, 9138))
            self.assertEqual(launch_args[5], 'benchmark-launcher.elf')

    def test_automatic_repeats_wait_for_prior_title_stop(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, events, output, _ = self.run_cli(Path(temporary), attach=False, repeat=2)
            launches = [index for index, event in enumerate(events) if event == ('launch',)]
            stops = [index for index, event in enumerate(events) if event == ('process_stopped',)]
            self.assertEqual(len(launches), 2)
            self.assertEqual(len(stops), 2)
            self.assertLess(stops[0], launches[1])
            self.assertEqual(json.loads((output / 'repeat_summary.json').read_text())['completed_repeats'], 2)

    def test_manual_attach_skips_launch_and_leaves_title_running(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage, events, output, _ = self.run_cli(Path(temporary), attach=True)
            self.assertNotIn(('launch',), events)
            self.assertIn(('command', 'load_save'), events)
            self.assertIn(('command', 'run_scenario'), events)
            self.assertNotIn(('command', 'quit'), events)
            self.assertNotIn(('process_stopped',), events)
            run_dir = next(path for path in output.iterdir() if path.is_dir())
            metadata = json.loads((run_dir / 'run_metadata.json').read_text())
            self.assertEqual(metadata['launch']['mode'], 'manual_attach')

    def test_prepare_backs_up_old_benchmark_config_and_isolated_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scenario, save, settings = self.fixture_inputs(root)
            output = root / 'prepare'
            old_config = b'{"version":1,"enabled":false}\n'
            old_settings = b'old isolated settings'
            storage = MemoryStorage(files={
                '/download0/config/benchmark.json': old_config,
                '/download0/benchmark/config/settings.cfg': old_settings,
            })
            token_file = root / 'private-token'
            arguments = ['--prepare', '--host', '127.0.0.1', '--scenario', str(scenario),
                         '--save', str(save), '--settings', str(settings), '--output', str(output),
                         '--remote-root', '/download0', '--remote-config', '/download0/config/benchmark.json',
                         '--token-file', str(token_file)]
            with patch.object(runner, 'FTPStorage', StorageFactory(storage)):
                runner.main(arguments)
            self.assertEqual((output / 'previous-benchmark-config.json').read_bytes(), old_config)
            self.assertEqual((output / 'previous-isolated-settings.cfg').read_bytes(), old_settings)
            self.assertEqual(storage.files['/download0/benchmark/config/settings.cfg'], settings.read_bytes())
            prepared_config = json.loads(storage.files['/download0/config/benchmark.json'])
            self.assertTrue(prepared_config['enabled'])
            self.assertEqual(prepared_config['control']['token'], token_file.read_text().strip())
            self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)

    def test_prepare_write_failure_restores_previous_config_and_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scenario, save, settings = self.fixture_inputs(root)
            old_config = b'prior config'
            old_settings = b'prior isolated settings'
            storage = MemoryStorage(files={
                '/download0/config/benchmark.json': old_config,
                '/download0/benchmark/config/settings.cfg': old_settings,
            })
            storage.fail_config_write_once = True
            arguments = ['--prepare', '--host', '127.0.0.1', '--scenario', str(scenario),
                         '--save', str(save), '--settings', str(settings), '--output', str(root / 'prepare'),
                         '--remote-root', '/download0', '--remote-config', '/download0/config/benchmark.json',
                         '--token-file', str(root / 'private-token')]
            with patch.object(runner, 'FTPStorage', StorageFactory(storage)), \
                 self.assertRaisesRegex(OSError, 'fixture config write failure'):
                runner.main(arguments)
            self.assertEqual(storage.files['/download0/config/benchmark.json'], old_config)
            self.assertEqual(storage.files['/download0/benchmark/config/settings.cfg'], old_settings)


if __name__ == '__main__':
    unittest.main()
