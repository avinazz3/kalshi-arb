from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_history
from test_history import START, download
from kalshi_lab.analysis import run_analysis
from kalshi_lab.compare import day_bootstrap, load_analysis, run_compare
from kalshi_lab.forecast import run_forecast
from kalshi_lab.history import digest


class CompareTests(unittest.TestCase):
    def make_run(self, root, day=0, paths=100):
        original_market = test_history.market
        def unique_market(i):
            return {**original_market(i), 'ticker': f'KXBTC15M-SYNTHETIC-D{day}-{i}'}
        with patch('test_history.START', START + timedelta(days=day)), patch('test_history.market', unique_market):
            download(root/'history')
        run_forecast(root/'history', root/'forecast', paths=paths)
        run_analysis(root/'history', root/'forecast', root/'analysis')
        return root/'analysis'

    def test_pooling_daily_cohorts_provenance_and_too_few_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = [self.make_run(root/str(i), i) for i in (0, 1)]
            before = [digest(p/'report.json') for p in sources]
            result = run_compare(sources, root/'pooled', samples=100)
            self.assertEqual((result['rows'], result['contracts'], result['utc_days']), (24, 8, 2))
            self.assertEqual(result['scores']['overall']['models']['market']['brier'], .25)
            self.assertEqual(result['bootstrap']['status'], 'unavailable')
            self.assertEqual(before, [digest(p/'report.json') for p in sources])
            self.assertEqual([d['contracts'] for d in result['daily']], [4, 4])
            with self.assertRaises(ValueError):
                run_compare(sources, root/'pooled')

    def test_overlap_configuration_and_corruption_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            a, b = self.make_run(root/'a'), self.make_run(root/'b', 1, paths=101)
            with self.assertRaisesRegex(ValueError, 'overlapping'):
                run_compare([a, a], root/'duplicate')
            with self.assertRaisesRegex(ValueError, 'settings'):
                run_compare([a, b], root/'mixed')
            (a/'rows.csv').write_text((a/'rows.csv').read_text().replace(',0.5,', ',0.6,', 1))
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                load_analysis(a)
            self.assertFalse((root/'duplicate').exists())

    def test_recomputed_scores_reject_forged_artifact_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self.make_run(Path(tmp)/'a')
            path = a/'rows.csv'
            path.write_text(path.read_text().replace(',0.5,', ',0.6,', 1))
            report = json.loads((a/'report.json').read_text())
            report['artifacts']['rows.csv'] = digest(path)
            (a/'report.json').write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, 'score'):
                load_analysis(a)

    def test_day_block_bootstrap_preserves_all_horizons_and_is_reproducible(self):
        rows = []
        for day in range(10):
            for horizon in (10, 5, 2):
                rows.append(dict(utc_day=(START+timedelta(days=day)).date().isoformat(), horizon_minutes=horizon,
                                 rolling_excess_brier=.125, rolling_excess_log_loss=.25,
                                 ewma_excess_brier=.125, ewma_excess_log_loss=.25))
        a = day_bootstrap(rows, block_days=2, samples=100)
        self.assertEqual(a, day_bootstrap(rows, block_days=2, samples=100))
        self.assertEqual(a['status'], 'computed')
        self.assertEqual(a['intervals']['overall']['rolling']['brier']['percentile_95'], [.125, .125])
        self.assertEqual(a['available_blocks'], 9)
        rows = [r for r in rows if r['utc_day'] != (START+timedelta(days=4)).date().isoformat()]
        self.assertEqual(day_bootstrap(rows, samples=100)['available_blocks'], 7)
        with self.assertRaises(ValueError):
            day_bootstrap(rows, block_days=0)
