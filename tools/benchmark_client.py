#!/usr/bin/env python3
"""Bounded host client and FTP storage for the OpenMW benchmark protocol."""
from __future__ import annotations

import ftplib
import json
import math
import posixpath
import socket
import time
from pathlib import PurePosixPath

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 65_536
CAPTURE_STATES = frozenset(('idle', 'loading', 'waiting_world', 'warming',
                            'capturing', 'complete', 'incomplete', 'error'))


class BenchmarkError(RuntimeError):
    """A remote benchmark operation failed or returned invalid data."""


class ProtocolError(BenchmarkError):
    """A malformed or mismatched control-protocol response was received."""


class BenchmarkClient:
    """One authenticated newline-JSON control connection to the game runtime."""

    def __init__(self, host, port=9137, token=None, timeout=5.0):
        if not token or len(token) < 32 or any(c not in '0123456789abcdefABCDEF' for c in token):
            raise ValueError('benchmark token must contain at least 32 hexadecimal characters')
        if (not 1 <= int(port) <= 65535 or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError('invalid control port or timeout')
        self.host, self.port, self.token = host, int(port), token
        self.timeout = self._configured_timeout = float(timeout)
        self._socket = None
        self._reader = None
        self._writer = None
        self._request_id = 0

    def connect(self):
        if self._socket is not None:
            return
        self._socket = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._socket.settimeout(self.timeout)
        self._reader = self._socket.makefile('rb')
        self._writer = self._socket.makefile('wb')

    def close(self):
        for stream in (self._reader, self._writer):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
        self._socket = self._reader = self._writer = None

    def set_timeout(self, timeout):
        """Set the active socket timeout, for deadline-bounded operations."""
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('control timeout must be positive and finite')
        self.timeout = float(timeout)
        if self._socket is not None:
            self._socket.settimeout(self.timeout)

    def connect_until(self, *, deadline, poll_interval=0.5):
        """Retry a not-yet-started listener until a bounded monotonic deadline."""
        last_error = None
        attempt_timeout = self._configured_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.set_timeout(min(attempt_timeout, remaining))
            try:
                self.connect()
                return
            except OSError as error:
                last_error = error
                self.close()
                time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
        raise TimeoutError('benchmark control service did not become available') from last_error

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def request(self, command, **fields):
        self.connect()
        self._request_id += 1
        request_id = self._request_id
        request = dict(version=PROTOCOL_VERSION, id=request_id, token=self.token,
                       command=command, **fields)
        encoded = (json.dumps(request, separators=(',', ':'), allow_nan=False) + '\n').encode()
        if len(encoded) > MAX_MESSAGE_BYTES:
            raise ValueError('benchmark request exceeds protocol message limit')
        try:
            self._writer.write(encoded)
            self._writer.flush()
            line = self._reader.readline(MAX_MESSAGE_BYTES + 1)
        except (OSError, TimeoutError) as error:
            raise BenchmarkError(f'benchmark {command} request failed: {error}') from error
        if not line:
            raise ProtocolError('benchmark control connection closed before a reply')
        if len(line) > MAX_MESSAGE_BYTES or not line.endswith(b'\n'):
            raise ProtocolError('benchmark response exceeds the line limit or is unterminated')
        try:
            reply = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProtocolError('benchmark response is not valid JSON') from error
        if not isinstance(reply, dict) or reply.get('version') != PROTOCOL_VERSION:
            raise ProtocolError('benchmark response has an unsupported protocol version')
        if reply.get('id') != request_id:
            raise ProtocolError('benchmark response request id does not match')
        if not isinstance(reply.get('accepted'), bool) or not isinstance(reply.get('complete'), bool):
            raise ProtocolError('benchmark response lacks accepted/complete booleans')
        if not reply['accepted'] or reply.get('error'):
            raise BenchmarkError(str(reply.get('error') or f'benchmark command {command} was rejected'))
        return reply

    def status(self):
        reply = self.request('status')
        if not reply['complete']:
            raise ProtocolError('status reply must be complete')
        status = reply.get('status')
        required = ('ready', 'game_state', 'run_id', 'capture_state', 'completed',
                    'manifest', 'frames', 'error', 'loaded_save',
                    'last_command_id', 'last_command_error')
        if not isinstance(status, dict) or any(key not in status for key in required):
            raise ProtocolError('status reply is missing required runtime state fields')
        if not isinstance(status['ready'], bool) or not isinstance(status['completed'], bool):
            raise ProtocolError('status ready/completed fields must be booleans')
        if not isinstance(status['game_state'], str) or status['capture_state'] not in CAPTURE_STATES:
            raise ProtocolError('status has an invalid game_state or capture_state')
        command_id = status['last_command_id']
        if not isinstance(command_id, int) or isinstance(command_id, bool) or command_id < 0:
            raise ProtocolError('status last_command_id must be a nonnegative integer')
        if status['last_command_error'] is not None and not isinstance(status['last_command_error'], str):
            raise ProtocolError('status last_command_error must be a string or null')
        if status['loaded_save'] is not None and not isinstance(status['loaded_save'], str):
            raise ProtocolError('status loaded_save must be a string or null')
        return status

    def command(self, command, **fields):
        """Submit an operation. A queued response is not mistaken for completion."""
        reply = self.request(command, **fields)
        if not reply['complete'] and not reply['accepted']:
            raise ProtocolError(f'{command} was neither accepted nor completed')
        return reply

    def wait_for(self, predicate, *, deadline, poll_interval=0.5, description='runtime state'):
        """Poll authoritative status with a fixed monotonic deadline."""
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f'timed out waiting for {description}')
                self.set_timeout(min(self._configured_timeout, remaining))
                status = self.status()
                if predicate(status):
                    return status
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f'timed out waiting for {description}')
                time.sleep(min(poll_interval, remaining))
        finally:
            self.set_timeout(self._configured_timeout)


class FTPStorage:
    """FTP file operations with NLST-to-LIST fallback for limited servers."""

    def __init__(self, host, port=2121, username='anonymous', password='benchmark', timeout=10.0):
        self.host, self.port = host, int(port)
        self.username, self.password, self.timeout = username, password, float(timeout)
        self.ftp = None

    def connect(self):
        if self.ftp is None:
            self.ftp = ftplib.FTP()
            self.ftp.connect(self.host, self.port, timeout=self.timeout)
            self.ftp.login(self.username, self.password)
            self.ftp.set_pasv(True)
        return self

    def close(self):
        if self.ftp is not None:
            try:
                self.ftp.quit()
            except (OSError, ftplib.Error):
                self.ftp.close()
            self.ftp = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    @staticmethod
    def _remote_path(path):
        text = str(path)
        pure = PurePosixPath(text)
        if not pure.is_absolute() or '..' in pure.parts or any(c in text for c in '\r\n\x00'):
            raise ValueError(f'remote path must be absolute and traversal-free: {text!r}')
        return pure.as_posix()

    def list_names(self, directory):
        path = self._remote_path(directory)
        try:
            return [posixpath.basename(value.rstrip('/')) for value in self.ftp.nlst(path)]
        except ftplib.error_perm as error:
            if not str(error).startswith('502'):
                raise
        lines = []
        self.ftp.retrlines(f'LIST {path}', lines.append)
        names = []
        for line in lines:
            parts = line.split(None, 8)
            if len(parts) == 9 and parts[0][:1] in ('d', '-', 'l'):
                names.append(parts[8].split(' -> ', 1)[0])
            elif line.strip():
                raise BenchmarkError(f'cannot parse FTP LIST entry: {line!r}')
        return names

    def ensure_directory(self, directory):
        path = self._remote_path(directory)
        parts = PurePosixPath(path).parts
        current = ''
        for part in parts:
            if part == '/':
                current = '/'
                continue
            current = posixpath.join(current, part)
            try:
                self.ftp.mkd(current)
            except ftplib.error_perm as error:
                if not str(error).startswith('550'):
                    raise
                original = self.ftp.pwd()
                try:
                    self.ftp.cwd(current)
                except ftplib.error_perm:
                    raise BenchmarkError(f'cannot create or access remote directory {current}') from error
                finally:
                    self.ftp.cwd(original)

    def read_bytes(self, path, *, max_bytes=128 * 1024 * 1024):
        remote = self._remote_path(path)
        chunks = []
        size = 0

        def collect(chunk):
            nonlocal size
            size += len(chunk)
            if size > max_bytes:
                raise BenchmarkError(f'FTP file exceeds the {max_bytes}-byte download limit')
            chunks.append(chunk)

        self.ftp.retrbinary(f'RETR {remote}', collect)
        return b''.join(chunks)

    def write_bytes(self, path, data):
        from io import BytesIO
        remote = self._remote_path(path)
        self.ensure_directory(posixpath.dirname(remote))
        self.ftp.storbinary(f'STOR {remote}', BytesIO(data))

    def upload_file(self, local_path, remote_path):
        remote = self._remote_path(remote_path)
        self.ensure_directory(posixpath.dirname(remote))
        with open(local_path, 'rb') as stream:
            self.ftp.storbinary(f'STOR {remote}', stream)

    def download_file(self, remote_path, local_path, *, max_bytes=128 * 1024 * 1024):
        from pathlib import Path
        data = self.read_bytes(remote_path, max_bytes=max_bytes)
        destination = Path(local_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return data
