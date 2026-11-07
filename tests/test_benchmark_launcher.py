import json
from pathlib import Path
import socket
import socketserver
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import benchmark_launcher


def response(command, *, running=False, pid=0):
    result = {
        'service': 'openmw-launcher', 'protocol': 1,
        'title_id': 'PPSA99630', 'ok': True,
    }
    if command == 'STATUS':
        result.update(running=running, pid=pid if running else 0)
    elif command == 'LAUNCH':
        result['launch_result'] = 0
    return result


class LifecycleServer:
    def __init__(self, commands, *, malformed=None):
        self.commands = list(commands)
        self.requests = []
        self.malformed = malformed
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                line = self.rfile.readline(128)
                command = line.decode('ascii').rstrip('\n')
                owner.requests.append(command)
                if owner.malformed is not None:
                    payload = owner.malformed
                else:
                    if not owner.commands:
                        raise AssertionError('unexpected lifecycle request')
                    queued = owner.commands.pop(0)
                    if queued is None:
                        return
                    payload = json.dumps(queued).encode('ascii') + b'\n'
                self.wfile.write(payload)

        self.server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class LauncherStatusTests(unittest.TestCase):
    def test_launch_checks_process_then_launches_once_and_waits_for_visibility(self):
        with LifecycleServer([
            response('STATUS'), response('LAUNCH'), response('STATUS', running=True, pid=81),
        ]) as helper:
            result = benchmark_launcher.launch_title(
                helper.host, 'PPSA99630', timeout_s=2, poll_interval_s=0.001,
                lifecycle_port=helper.port,
            )
        self.assertEqual(helper.requests, ['STATUS', 'LAUNCH', 'STATUS'])
        self.assertEqual(result['process']['pid'], 81)
        self.assertEqual(result['launch_response']['launch_result'], 0)

    def test_launch_refuses_running_title_without_launching(self):
        with LifecycleServer([response('STATUS', running=True, pid=81)]) as helper:
            with self.assertRaisesRegex(benchmark_launcher.LauncherError, 'already running'):
                benchmark_launcher.launch_title(
                    helper.host, 'PPSA99630', timeout_s=2, poll_interval_s=0.001,
                    lifecycle_port=helper.port,
                )
        self.assertEqual(helper.requests, ['STATUS'])

    def test_status_rejects_malformed_helper_identity_and_fields(self):
        cases = (
            {'service': 'other', 'protocol': 1, 'title_id': 'PPSA99630', 'ok': True,
             'running': False, 'pid': 0},
            {'service': 'openmw-launcher', 'protocol': 2, 'title_id': 'PPSA99630', 'ok': True,
             'running': False, 'pid': 0},
            {'service': 'openmw-launcher', 'protocol': 1, 'title_id': 'OTHER', 'ok': True,
             'running': False, 'pid': 0},
            {'service': 'openmw-launcher', 'protocol': 1, 'title_id': 'PPSA99630', 'ok': True,
             'running': 0, 'pid': 0},
            {'service': 'openmw-launcher', 'protocol': 1, 'title_id': 'PPSA99630', 'ok': True,
             'running': False, 'pid': True},
        )
        for malformed_reply in cases:
            with self.subTest(reply=malformed_reply):
                with LifecycleServer([], malformed=json.dumps(malformed_reply).encode() + b'\n') as helper:
                    with self.assertRaises(benchmark_launcher.LauncherError):
                        benchmark_launcher.title_status(
                            helper.host, lifecycle_port=helper.port, timeout_s=2,
                        )

    def test_title_status_rejects_unterminated_reply(self):
        with LifecycleServer([], malformed=b'{"ok":true}') as helper:
            with self.assertRaisesRegex(benchmark_launcher.LauncherError, 'unterminated'):
                benchmark_launcher.title_status(
                    helper.host, lifecycle_port=helper.port, timeout_s=2,
                )

    def test_wait_stopped_requires_authoritative_process_absence(self):
        with LifecycleServer([
            response('STATUS', running=True, pid=81), response('STATUS'),
        ]) as helper:
            result = benchmark_launcher.wait_for_title_stopped(
                helper.host, 'PPSA99630', lifecycle_port=helper.port,
                timeout_s=2, poll_interval_s=0.001,
            )
        self.assertEqual(helper.requests, ['STATUS', 'STATUS'])
        self.assertTrue(result['stopped'])

    def test_ensure_helper_uploads_only_when_helper_connection_is_refused(self):
        # A reachable helper is never re-executed from the loader.
        with LifecycleServer([response('STATUS')]) as helper:
            status = benchmark_launcher.ensure_helper(
                helper.host, lifecycle_port=helper.port, timeout_s=2,
                poll_interval_s=0.001,
            )
        self.assertFalse(status['running'])

        with tempfile.TemporaryDirectory() as temporary:
            payload = Path(temporary) / 'launcher.elf'
            payload.write_bytes(b'not an elf')
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                lifecycle_port = probe.getsockname()[1]
            with self.assertRaisesRegex(benchmark_launcher.LauncherError, 'not an ELF'):
                benchmark_launcher.ensure_helper(
                    '127.0.0.1', payload_path=payload, loader_port=1,
                    lifecycle_port=lifecycle_port, timeout_s=1, poll_interval_s=0.001,
                )

            payload_bytes = bytes((0x7f,)) + b'ELFpayload bytes'
            payload.write_bytes(payload_bytes)
            received = []

            class LoaderHandler(socketserver.BaseRequestHandler):
                def handle(self):
                    chunks = []
                    while True:
                        chunk = self.request.recv(4096)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    received.append(b''.join(chunks))

            loader = socketserver.TCPServer(('127.0.0.1', 0), LoaderHandler)
            loader_thread = threading.Thread(target=loader.serve_forever, daemon=True)
            loader_thread.start()
            if lifecycle_port == loader.server_address[1]:
                with socket.socket() as probe:
                    probe.bind(('127.0.0.1', 0))
                    lifecycle_port = probe.getsockname()[1]
            try:
                with self.assertRaisesRegex(benchmark_launcher.LauncherError, 'deadline'):
                    benchmark_launcher.ensure_helper(
                        '127.0.0.1', payload_path=payload,
                        loader_port=loader.server_address[1], lifecycle_port=lifecycle_port,
                        timeout_s=0.05, poll_interval_s=0.001,
                    )
            finally:
                loader.shutdown()
                loader.server_close()
                loader_thread.join(timeout=2)
            self.assertEqual(received, [payload_bytes])


if __name__ == '__main__':
    unittest.main()
