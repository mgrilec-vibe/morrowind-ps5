#!/usr/bin/env python3
"""Analyze sampled engine timings and driver profile windows without console access."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import statistics

FIELDS = re.compile(r'([A-Za-z_][A-Za-z_0-9]*)=([-+]?\d+(?:\.\d+)?)')
ENGINE = re.compile(r'PS5 frame timing ([0-9a-fA-F]+)\s+(.*)')


def percentile(values, fraction):
    values = sorted(values)
    index = (len(values)-1)*fraction
    low, high = math.floor(index), math.ceil(index)
    return values[low] + (values[high]-values[low])*(index-low)


def analyze_legacy(text):
    """Analyze sparse logs. These records are samples/windows, not full frame data."""
    samples, steady_samples, windows = [], [], []
    rejection_signatures = set()
    native = None
    light_latest, light_resets, light_records = {}, {}, {}
    for line in text.splitlines():
        for marker in ('ps5-present-diag', 'ps5-batch-breaks'):
            if '['+marker+']' not in line:
                continue
            fields = FIELDS.findall(line)
            if any('.' in value or value.startswith('-') for key,value in fields):
                continue
            counters = {key:int(value) for key,value in fields}
            if counters.get('cumulative') != 1 or 'present' not in counters:
                continue
            previous = light_latest.get(marker)
            if previous and counters['present'] < previous['present']:
                light_resets[marker] = light_resets.get(marker,0)+1
            light_latest[marker] = counters
            light_records[marker] = light_records.get(marker,0)+1
        engine = ENGINE.search(line)
        if engine:
            values = {k: float(v) for k,v in FIELDS.findall(engine[2])}
            if all(k in values and math.isfinite(values[k]) and values[k]>=0
                   for k in ('update_ms','render_ms','lua_wait_ms')):
                total = sum(values[k] for k in ('update_ms','render_ms','lua_wait_ms'))
                samples.append(total)
                if engine[1] not in ('1','2','3'):
                    steady_samples.append(total)
        if '[ps5-native-profile]' in line:
            native = {k: float(v) for k,v in FIELDS.findall(line)}
        if '[ps5-cpu-profile]' in line:
            values = {k: float(v) for k,v in FIELDS.findall(line)}
            present = values.get('present',0)
            wall = values.get('wall_ms',0)
            # Reporter emits every frame through120, then every60; wall_ms is
            # since its last emitted report, not the last line available in a file.
            frames = 1 if 1 <= present <=120 else 60 if present>120 and present%60==0 else 0
            if frames and wall>0 and math.isfinite(wall):
                item = dict(present=int(present), frames=frames, wall_ms=wall,
                            interval_mean_ms=wall/frames, interval_fps=frames*1000/wall)
                item['cpu_per_frame']={k:v/frames for k,v in values.items() if k not in ('present','wall_ms')}
                if native and native.get('present')==present:
                    item['native_per_frame']={k:v/frames for k,v in native.items() if k!='present'}
                windows.append(item)
            native = None
        if '[ps5-outer-batch-reject]' in line or '[ps5-batch-texture-reject]' in line:
            rejection_signatures.add(line[line.index('[ps5-'):])
    result = dict(record_kind='legacy_sparse_log_samples',
        limitations=['Legacy engine timing percentiles describe logged sample frames only, not all frames.',
                     'Legacy driver windows are interval averages; they cannot measure individual-frame jitter.',
                     'CPU/native stage times overlap; do not add nested stage totals.',
                     'Rejection receipts are bounded signatures, not occurrence counts.',
                     'Light diagnostics are cumulative counters, not timing windows. Present-counter decreases mark reset boundaries; earlier discarded resets cannot be recovered.',
                     'Screen present counts queue-present boundaries, not successful native presents; queued boundaries are eligibility before queue outcome. Batch counts are nonempty submit attempts before retirement, not guaranteed successful completions. Native counters reset with video release.'],
        engine_sample_count=len(samples), driver_window_count=len(windows),
        rejection_signatures=sorted(rejection_signatures),
        latest_light_diagnostics=light_latest,
        light_diagnostic_reset_boundaries=light_resets,
        light_diagnostic_record_counts=light_records)
    if samples:
        result['sampled_engine_ms']=dict(median=statistics.median(samples),p95=percentile(samples,.95),maximum=max(samples))
    if steady_samples:
        result['sampled_engine_after_first_three_frames_ms']=dict(
            count=len(steady_samples),median=statistics.median(steady_samples),
            p95=percentile(steady_samples,.95),maximum=max(steady_samples))
    if windows:
        frames=sum(w['frames'] for w in windows);wall=sum(w['wall_ms'] for w in windows)
        result['driver_interval_summary']=dict(frames=frames,wall_ms=wall,weighted_fps=frames*1000/wall,
                                               maximum_interval_mean_ms=max(w['interval_mean_ms'] for w in windows))
        result['latest_driver_window']=windows[-1]
        steady = [w for w in windows if w['present']>120]
        if steady:
            frames=sum(w['frames'] for w in steady);wall=sum(w['wall_ms'] for w in steady)
            result['driver_after_present_120_summary']=dict(
                frames=frames,wall_ms=wall,weighted_fps=frames*1000/wall,
                maximum_interval_mean_ms=max(w['interval_mean_ms'] for w in steady))
    return result


FRAME_FIELDS = (
    'frame_id', 'phase', 'scenario_time_ns', 'outer_interval_ns', 'update_ns',
    'render_ns', 'lua_wait_ns', 'limiter_ns', 'checkpoint_index', 'paused',
    'loading', 'cell_id', 'player_x', 'player_y', 'player_z',
    'player_rot_x', 'player_rot_y', 'player_rot_z')
STALL_NS = (16_670_000, 33_330_000, 50_000_000)


def _distribution(values_ns):
    values_ms = [value / 1_000_000 for value in values_ns]
    return dict(count=len(values_ms), mean_ms=statistics.fmean(values_ms),
                median_ms=statistics.median(values_ms),
                p95_ms=percentile(values_ms, .95), p99_ms=percentile(values_ms, .99),
                maximum_ms=max(values_ms))


def analyze_capture(text, manifest=None):
    """Analyze validated full-frame CSV records; warmup frames are excluded."""
    import csv
    import io

    if manifest is not None:
        if not isinstance(manifest, dict):
            raise ValueError('capture manifest must be a JSON object')
        if manifest.get('status') != 'complete':
            raise ValueError('capture manifest is not complete')
        if manifest.get('frames_file') != 'frames.csv':
            raise ValueError('manifest does not identify frames.csv')
        if manifest.get('frames_sha256') is not None:
            digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
            if manifest['frames_sha256'] != digest:
                raise ValueError('manifest frame CSV checksum does not match')
    reader = csv.DictReader(io.StringIO(text, newline=''))
    if tuple(reader.fieldnames or ()) != FRAME_FIELDS:
        raise ValueError('unexpected frame CSV header')
    previous_id = 0
    row_count = 0
    capture = []
    for row in reader:
        try:
            if None in row or any(row[name] is None for name in FRAME_FIELDS):
                raise ValueError('malformed frame CSV row')
            frame_id = int(row['frame_id'])
            if frame_id != previous_id + 1:
                raise ValueError('frame_id values must be consecutive and start at 1')
            previous_id = frame_id
            row_count += 1
            if row['phase'] not in ('warmup', 'capture'):
                raise ValueError('invalid frame phase')
            numeric = {name: int(row[name]) for name in (
                'scenario_time_ns', 'outer_interval_ns', 'update_ns', 'render_ns',
                'lua_wait_ns', 'limiter_ns', 'checkpoint_index', 'paused', 'loading', 'cell_id')}
            if any(numeric[name] < 0 for name in (
                    'scenario_time_ns', 'outer_interval_ns', 'update_ns', 'render_ns',
                    'lua_wait_ns', 'limiter_ns', 'cell_id')):
                raise ValueError('frame durations and IDs cannot be negative')
            if numeric['paused'] not in (0, 1) or numeric['loading'] not in (0, 1):
                raise ValueError('paused/loading must be 0 or 1')
            pose = [float(row[name]) for name in (
                'player_x', 'player_y', 'player_z', 'player_rot_x', 'player_rot_y', 'player_rot_z')]
            if not all(math.isfinite(value) for value in pose):
                raise ValueError('actor position and rotation values must be finite')
            if row['phase'] == 'capture':
                capture.append((numeric, pose))
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError('invalid frame CSV value') from error
    if not capture:
        raise ValueError('capture has no measured frames')
    intervals = [row[0]['outer_interval_ns'] for row in capture]
    if any(interval <= 0 for interval in intervals):
        raise ValueError('capture outer intervals must be positive')
    if manifest is not None:
        frame_count = manifest.get('frame_count')
        if (not isinstance(frame_count, int) or isinstance(frame_count, bool) or
                frame_count != row_count):
            raise ValueError('manifest frame_count does not match CSV data rows')
    total_interval = sum(intervals)
    result = {
        'record_kind': 'full_frame_capture',
        'csv_record_count': row_count,
        'elapsed_interval_ms': total_interval / 1_000_000,
        'mean_fps': len(intervals) * 1_000_000_000 / total_interval,
        'frame_interval_ms': _distribution(intervals),
        'stall_counts': {
            'over_16_67_ms': sum(value > STALL_NS[0] for value in intervals),
            'over_33_33_ms': sum(value > STALL_NS[1] for value in intervals),
            'over_50_ms': sum(value > STALL_NS[2] for value in intervals),
        },
        'engine_phase_ms': {
            name: _distribution([row[0][field] for row in capture])
            for name, field in (('update', 'update_ns'), ('render', 'render_ns'),
                                ('lua_wait', 'lua_wait_ns'), ('limiter', 'limiter_ns'))
        },
        'state_counts': {
            'paused': sum(row[0]['paused'] for row in capture),
            'loading': sum(row[0]['loading'] for row in capture),
            'checkpoints': sorted({row[0]['checkpoint_index'] for row in capture
                                   if row[0]['checkpoint_index'] >= 0}),
            'cell_ids': len({row[0]['cell_id'] for row in capture}),
            'actor_rotations_are_camera_orientation': False,
        },
        'limitations': [
            'Outer intervals are full start-to-start frame times and include pacing waits.',
            'Update, render, Lua-wait, and limiter fields are CPU-side durations; they are not GPU execution or scanout times.',
            'Legacy sparse engine samples and driver interval windows are analyzed separately and cannot describe all-frame jitter.',
        ],
    }
    if manifest is not None and manifest.get('frames_sha256'):
        result['manifest_frames_sha256'] = manifest['frames_sha256']
    return result


def analyze(text, manifest=None):
    """Keep complete captures and legacy sampled logs in separate report sections."""
    if text.splitlines() and text.splitlines()[0].split(',') == list(FRAME_FIELDS):
        return {'full_frame_capture': analyze_capture(text, manifest)}
    return analyze_legacy(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('captures', nargs='+', type=Path)
    args = parser.parse_args()
    reports = {}
    for path in args.captures:
        text = path.read_bytes().decode('utf-8', errors='replace')
        manifest_path = path.with_name('manifest.json')
        manifest = None
        if path.name == 'frames.csv':
            if not manifest_path.is_file():
                raise ValueError(f'{path}: capture manifest is missing')
            manifest = json.loads(manifest_path.read_text())
        reports[str(path)] = analyze(text, manifest)
    print(json.dumps(reports, indent=2))


if __name__=='__main__':main()
