import importlib.util
from pathlib import Path
import unittest
p=Path(__file__).resolve().parents[1]/'tools/analyze-frame-pacing.py'
spec=importlib.util.spec_from_file_location('pacing',p);pacing=importlib.util.module_from_spec(spec);spec.loader.exec_module(pacing)


class PacingTests(unittest.TestCase):
 def test_missing_interval_lines_keep_actual_reporter_cadence(self):
  result=pacing.analyze('[ps5-cpu-profile] present=180 wall_ms=2000 draws=600\n'
                        '[ps5-cpu-profile] present=300 wall_ms=1000 draws=1200')
  self.assertEqual(result['driver_interval_summary']['frames'],120)
  self.assertEqual(result['driver_interval_summary']['weighted_fps'],40)
  self.assertEqual(result['latest_driver_window']['cpu_per_frame']['draws'],20)
 def test_native_join_and_single_frame_warmup(self):
  result=pacing.analyze('[ps5-cpu-profile] present=1 wall_ms=0\n'
                       '[ps5-native-profile] present=2 present_vblank_ms=10\n'
                       '[ps5-cpu-profile] present=2 wall_ms=20')
  self.assertEqual(result['driver_window_count'],1)
  self.assertEqual(result['latest_driver_window']['native_per_frame']['present_vblank_ms'],10)
 def test_engine_hex_ids_and_sample_only_stats(self):
  result=pacing.analyze('PS5 frame timing 3c update_ms=2 render_ms=30 lua_wait_ms=0.1\n'
                       'PS5 frame timing 78 update_ms=3 render_ms=40 lua_wait_ms=0.2')
  self.assertEqual(result['engine_sample_count'],2)
  self.assertAlmostEqual(result['sampled_engine_ms']['median'],37.65)
  self.assertEqual(result['driver_window_count'],0)
 def test_light_cumulative_counters_are_not_timing_windows(self):
  result=pacing.analyze('[ps5-present-diag] present=120 cumulative=1 cpu_flips=100 marker_waits=3\n'
                       '[ps5-batch-breaks] present=120 cumulative=1 capacity_batches=8 capacity_draws=1024\n'
                       '[ps5-present-diag] present=240 cumulative=1 cpu_flips=200 marker_waits=5\n'
                       '[ps5-present-diag] present=120 cumulative=1 cpu_flips=2 marker_waits=1')
  self.assertEqual(result['driver_window_count'],0)
  self.assertEqual(result['latest_light_diagnostics']['ps5-present-diag']['cpu_flips'],2)
  self.assertEqual(result['latest_light_diagnostics']['ps5-batch-breaks']['capacity_draws'],1024)
  self.assertEqual(result['light_diagnostic_reset_boundaries']['ps5-present-diag'],1)
  self.assertEqual(result['light_diagnostic_record_counts']['ps5-present-diag'],3)
 def test_light_integer_counters_preserve_precision(self):
  result=pacing.analyze('[ps5-batch-breaks] present=120 cumulative=1 resource_hazard_draws=9007199254740993')
  self.assertEqual(result['latest_light_diagnostics']['ps5-batch-breaks']['resource_hazard_draws'],9007199254740993)
 def test_startup_separated_from_steady_samples(self):
  result=pacing.analyze('PS5 frame timing 1 update_ms=100 render_ms=900 lua_wait_ms=0\n'
                       'PS5 frame timing 3c update_ms=2 render_ms=30 lua_wait_ms=0')
  self.assertEqual(result['sampled_engine_ms']['maximum'],1000)
  self.assertEqual(result['sampled_engine_after_first_three_frames_ms']['maximum'],32)
 def test_signatures_not_frequencies_and_malformed_records(self):
  result=pacing.analyze('[ps5-outer-batch-reject] mode=6\n[ps5-outer-batch-reject] mode=6\n'
                       '[ps5-cpu-profile] present=150 wall_ms=20\nPS5 frame timing 3c render_ms=30')
  self.assertEqual(len(result['rejection_signatures']),1)
  self.assertEqual(result['driver_window_count'],0)
  self.assertEqual(result['engine_sample_count'],0)
 def test_full_frame_intervals_percentiles_and_strict_stall_thresholds(self):
  intervals=[10_000_000,16_670_000,33_330_000,50_000_000,50_010_000]
  header=','.join(pacing.FRAME_FIELDS)
  rows=[header]
  for frame,interval in enumerate(intervals,1):
   rows.append(','.join(map(str,[frame,'capture',frame*1_000_000,interval,
    frame*1_000_000,2_000_000,100_000,interval-3_100_000,-1,0,0,1,
    10.0,20.0,30.0,0.0,0.5,1.0])))
  manifest={'status':'complete','frames_file':'frames.csv','frame_count':5}
  capture='\n'.join(rows)+'\n'
  report=pacing.analyze_capture(capture,manifest)
  with self.assertRaisesRegex(ValueError,'checksum'):
   pacing.analyze_capture(capture,{**manifest,'frames_sha256':'bad'})
  self.assertEqual(report['record_kind'],'full_frame_capture')
  self.assertEqual(report['frame_interval_ms']['median_ms'],33.33)
  self.assertAlmostEqual(report['frame_interval_ms']['p95_ms'],50.008)
  self.assertEqual(report['stall_counts'],{
   'over_16_67_ms':3,'over_33_33_ms':2,'over_50_ms':1})
  self.assertAlmostEqual(report['mean_fps'],5_000_000_000/160_010_000)
  self.assertFalse(report['state_counts']['actor_rotations_are_camera_orientation'])
 def test_full_frame_capture_rejects_incomplete_and_missing_records(self):
  header=','.join(pacing.FRAME_FIELDS)
  row=','.join(map(str,[1,'capture',0,20_000_000,2_000_000,3_000_000,
   100_000,14_900_000,-1,0,0,1,0,0,0,0,0,0]))
  with self.assertRaises(ValueError):
   pacing.analyze_capture(header+'\n'+row+'\n',{'status':'incomplete','frames_file':'frames.csv'})
  with self.assertRaises(ValueError):
   pacing.analyze_capture(header+'\n'+row+'\n'+row+'\n',
    {'status':'complete','frames_file':'frames.csv','frame_count':2})
 def test_legacy_records_are_explicitly_sampled(self):
  result=pacing.analyze('PS5 frame timing 3c update_ms=2 render_ms=30 lua_wait_ms=0.1')
  self.assertEqual(result['record_kind'],'legacy_sparse_log_samples')

if __name__=='__main__':unittest.main()
