from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import main
from scripts import ocr_pages as ocr
from scripts import performance as perf
from scripts import pipeline
from test_ocr_pages import line


def run(stage='ocr', workers=2, pages=100, seconds=110, **values):
    return perf.Run(stage, perf.machine(stage), perf.options(stage), workers, pages, seconds, **values)


class PredictionTests(unittest.TestCase):
    def test_initial_ocr_estimate_accounts_for_workers_and_incomplete_waves(self):
        self.assertEqual(perf.initial_ocr_seconds(10, 1, 3, .2), 30)
        self.assertEqual(perf.initial_ocr_seconds(10, 6, 3, .2), 8)
        self.assertEqual(perf.initial_ocr_seconds(12, 6, 3, .2), 8.4)
        self.assertEqual(perf.initial_ocr_seconds(2, 6, 3, .2), 3.4)
        self.assertEqual(perf.initial_ocr_seconds(0, 6, 3, .2), 0)

    def test_initial_ocr_estimate_skips_recognition_for_cache_hits(self):
        self.assertEqual(perf.initial_ocr_seconds(10, 6, 3, .2, hit_share=.5), 5)
        self.assertEqual(perf.initial_ocr_seconds(10, 0, 3, .2, hit_share=1), 2)
        self.assertEqual(perf.initial_ocr_seconds(10, 1, 3, .2, hit_share=.5), 16)

    def test_learns_saturation_from_actual_worker_profiles(self):
        samples = [run(workers=workers, pages=pages, seconds=20 + rate * pages, setup_seconds=5)
                   for workers, rate in ((1, 4), (2, 2.5), (4, 2.3), (8, 3))
                   for pages in (100, 200, 400)]
        predictions = {workers: perf.predict(samples, pages=300, workers=workers, fallback=99)
                       for workers in (1, 2, 4, 8)}
        self.assertAlmostEqual(predictions[2].seconds, 770)
        self.assertLess(predictions[2].seconds, predictions[1].seconds)
        self.assertGreater(predictions[8].seconds, predictions[4].seconds)
        self.assertTrue(all(cost.calibrated for cost in predictions.values()))
        # Missing profiles use the supplied fallback rather than extrapolating measurements.
        unseen = perf.predict(samples, pages=300, workers=3, fallback=4)
        self.assertFalse(unseen.calibrated)
        self.assertEqual(unseen.seconds, 1200)

    def test_fixed_startup_is_not_divided_by_the_worker_count(self):
        samples = [run(pages=pages, seconds=60 + pages * .5, setup_seconds=10)
                   for pages in (10, 20, 40)]
        estimate = perf.predict(samples, pages=30, workers=2, fallback=3)
        self.assertAlmostEqual(estimate.seconds, 75)
        self.assertLess(estimate.low, estimate.seconds)
        self.assertGreater(estimate.high, estimate.seconds)

    def test_cold_and_cached_runs_are_not_mixed(self):
        samples = [run(workers=2, pages=pages, seconds=20 + pages * 2)
                   for pages in (100, 200, 400)]
        samples += [run(workers=0, pages=pages, seconds=2 + pages * .1, cache_hits=pages)
                    for pages in (100, 200, 400)]
        cold = perf.predict(samples, pages=200, workers=2, fallback=3)
        warm = perf.predict(samples, pages=200, workers=0, fallback=3, hit_share=1)
        self.assertEqual(cold.seconds, 420)
        self.assertEqual(warm.seconds, 22)
        self.assertLess(warm.seconds, cold.seconds)
        mismatch = perf.predict(samples, pages=200, workers=2, fallback=3, hit_share=.5)
        self.assertFalse(mismatch.calibrated)

    def test_lot_shape_and_extrapolation_reduce_confidence(self):
        samples = [run(stage='barcode', pages=pages, seconds=10 + .3 * pages, lots=1)
                   for pages in (100, 200, 400)]
        tiny_lots = perf.predict(samples, pages=300, workers=2, fallback=.3, lots=100)
        huge_batch = perf.predict(samples, pages=10000, workers=2, fallback=.3, lots=1)
        self.assertFalse(tiny_lots.calibrated)
        self.assertFalse(huge_batch.calibrated)

    def test_copy_uses_bytes_not_the_number_of_pages(self):
        samples = [run(stage='import', workers=1, pages=10, seconds=2 + size / 100,
                       input_bytes=size) for size in (100, 200, 400)]
        small = perf.predict(samples, pages=10, workers=1, fallback=1, input_bytes=100)
        large = perf.predict(samples, pages=10, workers=1, fallback=1, input_bytes=400)
        self.assertEqual(small.seconds, 3)
        self.assertEqual(large.seconds, 6)

    def test_outlier_does_not_define_the_prediction(self):
        samples = [run(pages=100, seconds=seconds, setup_seconds=10) for seconds in (100, 105, 110, 115, 2000)]
        cost = perf.predict(samples, pages=100, workers=2, fallback=99)
        self.assertEqual(cost.seconds, 110)
        self.assertGreater(cost.high, cost.seconds)


class HistoryFixture:
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def save(self, name, value):
        directory = self.root / 'Reports' / 'ocr' / name
        directory.mkdir(parents=True)
        (directory / 'performance.json').write_text(json.dumps(value))


class HistoryTests(HistoryFixture, unittest.TestCase):
    def test_only_valid_current_host_and_settings_are_used(self):
        valid = {'schema': perf.SCHEMA, **asdict(run())}
        self.save('01', valid)
        for name, overrides in [('02', {'machine': 'another PC'}), ('03', {'options': {'dpi': 72}}),
                                ('04', {'seconds': float('nan')}), ('05', {'pages': -1}),
                                ('06', {'setup_seconds': 999}), ('07', {'cache_hits': 999})]:
            self.save(name, dict(valid, **overrides))
        for index, malformed in enumerate((None, [], 'garbage', {'schema': 99}), 8):
            self.save(str(index), malformed)
        self.assertEqual(perf.history(self.root, 'ocr'), [run()])

    def test_recorded_settings_match_cli_defaults(self):
        for stage, module in (('ocr', ocr), ('review', __import__('scripts.review_pages', fromlist=['parse_args']))):
            args = module.parse_args(['-d', str(self.root)])
            self.assertEqual(perf.options(stage, args), perf.options(stage))

    def test_history_write_failure_is_nonfatal(self):
        with patch('scripts.script.atomic_text_write', side_effect=OSError('read-only drive')), \
                self.assertLogs('scripts.performance', level='WARNING') as logs:
            perf.record(self.root, 'ocr', workers=2, pages=100, seconds=10)
        self.assertIn('could not be saved', logs.output[0])

    def test_shared_launcher_adds_startup_and_shutdown_to_the_observation(self):
        directory = self.root / 'Reports' / 'ocr' / '01'
        def finish():
            directory.mkdir(parents=True)
            perf.record(directory, 'ocr', workers=2, pages=100, seconds=20, setup_seconds=3)
            return 0
        command = main.build_command('ocr_pages.py', self.root, workers=2)
        with patch.object(main.subprocess, 'Popen') as popen, \
                patch.object(main.time, 'perf_counter', side_effect=[100, 125]):
            popen.return_value.__enter__.return_value.wait.side_effect = finish
            self.assertEqual(main.execute_command(command), 0)
        sample = perf.history(self.root, 'ocr')[0]
        self.assertEqual((sample.seconds, sample.setup_seconds), (25, 8))


class CacheSamplingTests(HistoryFixture, unittest.TestCase):
    def make_files(self, count):
        folder = self.root / 'OCR'
        folder.mkdir()
        files = []
        for i in range(count):
            path = folder / f'1_{i}.pdf'
            path.write_bytes(f'page-{i}'.encode())
            files.append(path)
        return files

    def test_sampling_reads_cache_without_models_or_new_entries(self):
        files = self.make_files(6)
        path = self.root / 'Reports' / '.ocr_cache.sqlite3'
        cache = ocr.OcrCache(path, ocr.ocr_settings(200, 'cpu'))
        for file in files[:3]:
            cache.put(ocr.file_digest(file), [line('known')])
        cache.close()
        before = path.read_bytes()
        with patch.object(ocr, 'PaddleEngine', side_effect=AssertionError('No inference during estimate')):
            sample = perf.cache_sample(self.root, 'ocr', files)
        self.assertEqual((sample.hits, sample.checked, sample.total), (3, 6, 6))
        self.assertTrue(sample.exact)
        self.assertEqual(path.read_bytes(), before)

    def test_missing_cache_is_not_created(self):
        files = self.make_files(3)
        sample = perf.cache_sample(self.root, 'ocr', files)
        self.assertEqual(sample.checked, 0)
        self.assertFalse((self.root / 'Reports').exists())

    def test_large_backlog_has_a_bounded_sample(self):
        files = self.make_files(200)
        cache = ocr.OcrCache(self.root / 'Reports' / '.ocr_cache.sqlite3', ocr.ocr_settings(200, 'cpu'))
        cache.close()
        sample = perf.cache_sample(self.root, 'ocr', files)
        self.assertEqual(sample.checked, perf.CACHE_SAMPLE_LIMIT)
        self.assertFalse(sample.exact)
        self.assertGreater(sample.bounds[1], sample.share)

    def test_all_sampled_cache_hits_do_not_imply_every_page_is_cached(self):
        sample = perf.CacheSample(24, 24, 1000)
        self.assertEqual(sample.share, 1)
        self.assertLess(sample.bounds[0], 1)
        self.assertEqual(perf.CacheSample(5, 5, 5).bounds, (1, 1))


class PipelineEstimateTests(HistoryFixture, unittest.TestCase):
    def test_measured_branching_and_sequential_final_stage_are_included(self):
        config = pipeline.Config(source=self.root / 'source', root=self.root, workers=2, ocr_workers=4)
        plan = pipeline.Plan(waiting=[pipeline.LotInfo('1', Path('unused'), 1000, 1000)], pending_files=10)
        barcode = [run(stage='barcode', workers=2, pages=n, seconds=10 + n * .2,
                       lots=1, unresolved=n // 4) for n in (500, 1000, 2000)]
        recognition = [run(workers=4, pages=n, seconds=20 + n,
                           unresolved=n // 5) for n in (100, 200, 400)]
        review = [run(stage='review', workers=1, pages=n, seconds=5 + n * .1,
                      cache_hits=n) for n in (30, 60, 120)]
        def history(root, stage, settings=None):
            return {'barcode': barcode, 'ocr': recognition, 'review': review, 'import': []}[stage]
        with patch.object(perf, 'history', side_effect=history), \
                patch.object(perf, 'cache_sample', return_value=perf.CacheSample(10, 10, 10)):
            cost = perf.estimate(plan, config, lambda path: [])
        # 1000 barcode pages -> 250 OCR pages -> 50 review pages + 10 already pending.
        self.assertAlmostEqual(cost.seconds, (10 + 200) + (20 + 250) + (5 + 6))
        self.assertLess(cost.low, cost.seconds)
        self.assertGreater(cost.high, cost.seconds)

    def test_pending_only_run_is_not_reported_as_zero_time(self):
        config = pipeline.Config(source=self.root, root=self.root)
        estimate = perf.estimate(pipeline.Plan(pending_files=300), config, lambda path: [])
        self.assertEqual(estimate.seconds, 60)
        self.assertTrue(any('classification finale' in note for note in estimate.notes))

    def test_final_stage_estimate_uses_its_own_worker_profile(self):
        config = pipeline.Config(source=self.root, root=self.root, review_workers=3)
        runs = [run(stage='review', workers=workers, pages=pages, seconds=2 + pages * rate)
                for workers, rate in ((1, .4), (3, .2)) for pages in (100, 200, 400)]
        with patch.object(perf, 'history', side_effect=lambda root, stage, settings=None: runs if stage == 'review' else []):
            estimate = perf.estimate(pipeline.Plan(pending_files=200), config, lambda path: [])
        self.assertEqual(estimate.seconds, 42)

    def test_duration_display_stays_on_one_line(self):
        config = pipeline.Config(source=self.root, root=self.root, ocr_workers=6)
        text = pipeline.format_plan(pipeline.Plan(ocr_files=10), config)
        self.assertEqual(text.splitlines()[-1], 'Durée estimée : environ 10 s.')
        self.assertEqual(len(text.splitlines()), 4)

    def test_first_pipeline_estimate_uses_parallel_ocr_then_adapts_to_measurements(self):
        config = pipeline.Config(source=self.root, root=self.root, ocr_workers=6)
        plan = pipeline.Plan(ocr_files=10)
        self.assertEqual(perf.estimate(plan, config, lambda path: []).seconds, 10)  # 8 OCR + 2 final stage
        directory = self.root / 'Reports' / 'ocr' / '01'
        directory.mkdir(parents=True)
        perf.record(directory, 'ocr', workers=6, pages=10, seconds=12, setup_seconds=2, unresolved=10)
        self.assertEqual(perf.estimate(plan, config, lambda path: []).seconds, 14)  # measured 12 OCR + 2 final

    def test_estimation_config_rejects_nonfinite_rates_and_invalid_shares(self):
        for key, value in [('ocr', 'nan'), ('ocr', 'inf'), ('part_ocr', '1.1'), ('copie_mo_par_seconde', '0'),
                           ('ocr_surcout_par_page', '-0.1'), ('ocr_surcout_par_page', 'nan')]:
            config = self.root / 'config.toml'
            config.write_text(f'[dossiers]\nsource = "/a"\nracine = "/b"\n[estimation]\n{key} = {value}\n')
            with self.subTest(key=key, value=value), self.assertRaises(pipeline.PipelineError):
                pipeline.load_config(config)

    def test_ocr_overhead_baseline_can_be_configured(self):
        path = self.root / 'config.toml'
        path.write_text(f'[dossiers]\nsource = "{self.root}"\nracine = "{self.root}"\n'
                        '[traitement]\nprocessus_ocr = 6\n'
                        '[estimation]\nocr = 3.5\nocr_surcout_par_page = 0.3\n')
        config = pipeline.load_config(path)
        self.assertEqual(config.ocr_overhead_seconds_per_page, .3)
        self.assertEqual(perf.estimate(pipeline.Plan(ocr_files=10), config, lambda path: []).seconds, 12)
