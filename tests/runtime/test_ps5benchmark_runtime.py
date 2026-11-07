import csv
import hashlib
import json
import pathlib
import secrets
import subprocess
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE = ROOT / "source-overlays/openmw/apps/openmw"


class RuntimeSocketTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="ps5benchmark-build-")
        cls.binary = pathlib.Path(cls.build.name) / "runtime-fixture"
        subprocess.run([
            "c++", "-std=c++17", "-pthread", "-I", str(SOURCE),
            str(SOURCE / "ps5benchmark.cpp"),
            str(ROOT / "tests/runtime/ps5benchmark-runtime-fixture.cpp"),
            "-lyaml-cpp", "-o", str(cls.binary),
        ], check=True)
        import sys
        sys.path.insert(0, str(ROOT / "tools"))
        from benchmark_client import BenchmarkClient, BenchmarkError
        cls.BenchmarkClient = BenchmarkClient
        cls.BenchmarkError = BenchmarkError

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def test_authenticated_capture_and_lifecycle_regressions(self):
        cases = [
            ("complete", 1000, "complete", "", 0.06, 0.15),
            ("overflow", 1, "incomplete", "", 0.06, 0.15),
            ("warmup_checkpoint", 1000, "complete", "warmup_checkpoint", 0.8, 0.15),
            ("manual_lease", 1000, "complete", "manual_lease", 0.06, 0.4),
            ("startup_lifecycle", 1000, "complete", "startup_lifecycle", 0.06, 0.15),
        ]
        for index, (name, capacity, expected, mode, warmup, duration) in enumerate(cases):
            with self.subTest(run=name), tempfile.TemporaryDirectory(prefix="ps5benchmark-run-") as directory:
                root = pathlib.Path(directory)
                (root / "saves").mkdir()
                (root / "scenarios").mkdir()
                (root / "saves/fixture.omwsave").write_bytes(b"fixture save")
                outside_save = root.with_name(root.name + "-outside.omwsave")
                outside_save.write_bytes(b"outside save")
                (root / "saves/escape.omwsave").symlink_to(outside_save)
                token = secrets.token_hex(32)
                port = 19137 + index
                startup = "idle.json" if name == "startup_lifecycle" else ""
                config = {
                    "version": 1, "enabled": True,
                    "control": {"enabled": True, "bind": "127.0.0.1", "port": port, "token": token},
                    "capture": {"enabled": True, "max_frames": capacity},
                    "startup_scenario": startup,
                }
                (root / "benchmark.json").write_text(json.dumps(config))
                events = [{"time": 0, "input": {"forward": 1 if name == "manual_lease" else 0}}]
                checkpoints = ([{"time": 0, "x": 11, "y": 22, "z": 33, "tolerance": 0.1}]
                               if name == "warmup_checkpoint" else [])
                scenario = {
                    "version": 1, "save": "fixture.omwsave", "warmup_seconds": warmup,
                    "duration_seconds": duration, "events": events, "checkpoints": checkpoints,
                }
                (root / "scenarios/idle.json").write_text(json.dumps(scenario))
                command = [str(self.binary), str(root)]
                if mode:
                    command.append(mode)
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                client = None
                try:
                    deadline = time.monotonic() + 10
                    while True:
                        if process.poll() is not None:
                            self.fail("runtime exited before listening")
                        bad = self.BenchmarkClient("127.0.0.1", port, "f" * 64, timeout=1)
                        try:
                            bad.connect()
                            with self.assertRaises(self.BenchmarkError):
                                bad.status()
                            break
                        except OSError:
                            time.sleep(0.05)
                        finally:
                            bad.close()
                        if time.monotonic() >= deadline:
                            self.fail("runtime control socket did not become ready")
                    client = self.BenchmarkClient("127.0.0.1", port, token, timeout=2)
                    client.connect()
                    if name == "complete":
                        before_escape_attempt = client.status()
                        escape_request = client.command("load_save", save="escape.omwsave")
                        rejected_escape = client.wait_for(
                            lambda value: value["last_command_id"] == escape_request["id"],
                            deadline=time.monotonic() + 5, poll_interval=0.02,
                        )
                        self.assertEqual(rejected_escape["last_command_error"], "save_not_found")
                        self.assertEqual(rejected_escape["loaded_save"], before_escape_attempt["loaded_save"])
                        self.assertEqual(rejected_escape["ready"], before_escape_attempt["ready"])

                    if name == "startup_lifecycle":
                        status = client.wait_for(
                            lambda value: value["capture_state"] in ("complete", "incomplete", "error"),
                            deadline=time.monotonic() + 10, poll_interval=0.02,
                        )
                        self.assertEqual(status["capture_state"], "complete", status)
                        startup_run = status["run_id"]
                        client.command("load_save", save="fixture.omwsave")
                        loaded = client.wait_for(
                            lambda value: value["ready"] and value["capture_state"] == "idle",
                            deadline=time.monotonic() + 5, poll_interval=0.02,
                        )
                        self.assertEqual(loaded["loaded_save"], "fixture.omwsave")
                        run_id = startup_run
                    else:
                        self.assertFalse(client.status()["ready"])
                        client.command("load_save", save="fixture.omwsave")
                        client.wait_for(lambda status: status["ready"], deadline=time.monotonic() + 5,
                                        poll_interval=0.02)
                        if name == "manual_lease":
                            client.command("input_state", input={"forward": -1}, lease_ms=80)
                        client.command("run_scenario", scenario="idle.json", run_id="test-" + name)
                        if name == "warmup_checkpoint":
                            (root / "checkpoint-run-start").touch()
                        if name == "manual_lease":
                            (root / "manual-run-start").touch()
                        status = client.wait_for(
                            lambda value: value["capture_state"] in ("complete", "incomplete", "error"),
                            deadline=time.monotonic() + 10, poll_interval=0.02,
                        )
                        self.assertEqual(status["capture_state"], expected, status)
                        run_id = "test-" + name

                    run = root / "runs" / run_id
                    manifest = json.loads((run / "manifest.json").read_text())
                    csv_path = run / "frames.csv"
                    with csv_path.open(newline="") as capture:
                        rows = list(csv.DictReader(capture))
                    self.assertEqual(len(rows), manifest["frame_count"])
                    self.assertEqual(manifest["frame_capacity"], capacity)
                    self.assertEqual(manifest["frames_file"], "frames.csv")
                    self.assertEqual(hashlib.sha256(csv_path.read_bytes()).hexdigest(), manifest["frames_sha256"])
                    self.assertTrue(rows)
                    for row in rows:
                        self.assertGreater(int(row["outer_interval_ns"]), 0)
                        self.assertGreater(int(row["limiter_ns"]), 0)
                        expected_x = 99.0 if name == "warmup_checkpoint" else 11.0
                        self.assertEqual((float(row["player_x"]), float(row["player_y"]), float(row["player_z"])),
                                         (expected_x, 22.0, 33.0))
                    if expected == "complete":
                        self.assertGreaterEqual(len(rows), 5)
                    else:
                        self.assertEqual(len(rows), 1)
                        self.assertIn("overflow", status["error"])
                    client.command("quit")
                    stdout, stderr = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 0, (stdout, stderr))
                finally:
                    if client is not None:
                        client.close()
                    if process.poll() is None:
                        process.terminate()
                        process.communicate(timeout=5)
                    if outside_save.exists():
                        outside_save.unlink()


if __name__ == "__main__":
    unittest.main()
