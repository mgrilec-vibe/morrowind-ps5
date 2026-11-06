import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import benchmark_launcher


class JsonResponse:
    def __init__(self, value):
        self.payload = json.dumps(value).encode('utf-8')

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, limit):
        return self.payload


class QueuedHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request.get_method(), urlsplit(request.full_url).path))
        if not self.responses:
            raise AssertionError('unexpected HTTP request')
        return JsonResponse(self.responses.pop(0))


class LauncherStatusTests(unittest.TestCase):
    def launch(self, http):
        with patch.object(benchmark_launcher.urllib.request, 'urlopen', side_effect=http):
            return benchmark_launcher.launch_title(
                'http://engine', '192.0.2.10', 'PPSA99630',
                timeout_s=2, poll_interval_s=0.001,
            )

    def wait_stopped(self, http):
        with patch.object(benchmark_launcher.urllib.request, 'urlopen', side_effect=http):
            with patch.object(benchmark_launcher.time, 'sleep'):
                return benchmark_launcher.wait_for_title_stopped(
                    'http://engine', '192.0.2.10', 'PPSA99630',
                    timeout_s=2, poll_interval_s=0.001,
                )

    def test_malformed_process_status_never_sends_launch(self):
        malformed = (
            {'processes': []},
            {'processes': {}, 'truncated': False},
            {'processes': [], 'truncated': 0},
            {'processes': [None], 'truncated': False},
            {'processes': [{}], 'truncated': False},
            {'processes': [{'kind': None}], 'truncated': False},
            {'processes': [{'kind': 'app'}], 'truncated': False},
            {'processes': [{'kind': 'app', 'title_id': None}], 'truncated': False},
            {'processes': [{'kind': 'app', 'title_id': 99630}], 'truncated': False},
            {'processes': [], 'truncated': True},
        )
        for status in malformed:
            with self.subTest(status=status):
                http = QueuedHttp([{'version': '8.5.1'}, status])
                with self.assertRaises(benchmark_launcher.LauncherError):
                    self.launch(http)
                self.assertEqual(
                    http.requests,
                    [('GET', '/api/version'), ('GET', '/api/ps5/process/list')],
                )

    def test_non_app_rows_may_omit_title_id_and_launch_succeeds(self):
        status_without_title = {
            'processes': [{'kind': 'system', 'title_id': None}],
            'truncated': False,
        }
        running_title = {
            'processes': [{'kind': 'app', 'title_id': 'PPSA99630'}],
            'truncated': False,
        }
        http = QueuedHttp([
            {'version': '8.5.1'},
            status_without_title,
            {'ok': True},
            running_title,
        ])

        result = self.launch(http)

        self.assertEqual(result['process'], running_title['processes'][0])
        self.assertEqual(
            [method for method, _ in http.requests],
            ['GET', 'GET', 'POST', 'GET'],
        )

    def test_matching_title_in_truncated_list_is_present_not_stopped(self):
        running_but_truncated = {
            'processes': [{'kind': 'app', 'title_id': 'PPSA99630'}],
            'truncated': True,
        }
        stopped = {'processes': [], 'truncated': False}
        http = QueuedHttp([{'version': '8.5.1'}, running_but_truncated, stopped])

        result = self.wait_stopped(http)

        self.assertTrue(result['stopped'])
        self.assertEqual(
            http.requests,
            [
                ('GET', '/api/version'),
                ('GET', '/api/ps5/process/list'),
                ('GET', '/api/ps5/process/list'),
            ],
        )


if __name__ == '__main__':
    unittest.main()
