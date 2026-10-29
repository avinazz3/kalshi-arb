"""Synthetic diagnostics, independent arithmetic checks and provenance failures."""
import csv
from decimal import Decimal
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_history import FakeHTTP, START, download
from kalshi_lab.analysis import calibration, load_rows, paired_summary, past_features, run_analysis, uncertainty
from kalshi_lab.digital import SECONDS_PER_YEAR
from kalshi_lab.forecast import run_forecast
from kalshi_lab.history import digest


class AnalysisTests(unittest.TestCase):
    def test_exact_average_variance_matches_covariance_sum(self):
        spot, sigma, offsets = 80000, .4, [61, 62, 63]
        average_sd, terminal_sd = uncertainty(spot, sigma, offsets, 64)
        expected = spot**2 * sum(math.expm1(sigma**2 * min(a, b) / SECONDS_PER_YEAR)
                                 for a in offsets for b in offsets) / 9
        self.assertAlmostEqual(average_sd**2, expected, places=10)
        self.assertLess(average_sd, terminal_sd)
        one_sd, terminal_sd = uncertainty(spot, sigma, [64], 64)
        self.assertEqual(one_sd, terminal_sd)
        self.assertEqual(uncertainty(spot, 0, offsets, 64), (0, 0))

    def test_returns_are_past_only_and_flagged_endpoints_are_explicit(self):
        source = 1000
        point = lambda value: dict(value=str(value), amend_time_ms=None, repeat_of_previous_value=False)
        points = {(source - 60 * m) * 1000: point(100) for m in (1, 2, 5)}
        before = past_features(points, source, 101)
        points[(source + 1) * 1000] = point(999999)
        self.assertEqual(before, past_features(points, source, 101))
        self.assertAlmostEqual(before['return_2m_log'], math.log(1.01))
        del points[(source - 60) * 1000]
        points[(source - 120) * 1000]['amend_time_ms'] = 123
        points[(source - 300) * 1000]['repeat_of_previous_value'] = True
        after = past_features(points, source, 101)
        self.assertIsNone(after['return_1m_log'])
        self.assertEqual(after['return_1m_status'], 'missing_sample')
        self.assertEqual(after['return_2m_status'], 'amended_sample')
        self.assertEqual(after['return_5m_status'], 'repeated_sample')

    def test_calibration_endpoints_and_signed_paired_losses(self):
        rows = [dict(ticker=str(i), horizon_minutes=2, outcome=y, rolling=p, ewma=p, market=.5,
                     rolling_excess_brier=(p-y)**2 - (.5-y)**2, rolling_excess_log_loss=0)
                for i, (p, y) in enumerate([(0, 1), (.1, 0), (1, 1)])]
        bins = [b for b in calibration(rows) if b['model'] == 'rolling' and b['horizon_minutes'] == 2]
        self.assertEqual([b['rows'] for b in bins], [1, 1, 0, 0, 0, 0, 0, 0, 0, 1])
        self.assertEqual(bins[-1]['yes_rate'], 1)
        result = paired_summary(rows, 'rolling')
        self.assertAlmostEqual(result['sum_excess_brier'], .75 - .24 - .25)
        self.assertEqual(result['market_better'], 1)
        self.assertEqual(result['model_better'], 2)

    def make_run(self, root):
        history, forecast = root/'history', root/'forecast'
        download(history)
        run_forecast(history, forecast, paths=100, half_life=15.0)
        return history, forecast

    def test_end_to_end_cli_preserves_inputs_and_accepts_existing_v07_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history, forecast = self.make_run(root)
            before = {p: digest(p) for p in [*history.rglob('*'), *forecast.iterdir()] if p.is_file()}
            result = run_analysis(history, forecast, root/'analysis')
            self.assertEqual(result['rows'], 12)
            self.assertEqual(result['contracts'], 4)
            self.assertEqual(result['scores']['overall']['models']['rolling']['brier'], 0)
            self.assertFalse(result['point_in_time_certified'])
            with (root/'analysis'/'rows.csv').open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertTrue(all(r['rolling_gap_z'] == '' for r in rows))
            self.assertTrue(all(r['return_5m_log'] == '0.0' for r in rows))
            with (root/'analysis'/'calibration.csv').open() as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 90)
            self.assertEqual(before, {p: digest(p) for p in before})
            with self.assertRaises(ValueError):
                run_analysis(history, forecast, root/'analysis')
            cli = subprocess.run([sys.executable, '-m', 'kalshi_lab', 'analyze', '--run-dir', str(history),
                                  '--forecast-dir', str(forecast), '--out-dir', str(root/'cli')],
                                 capture_output=True, text=True)
            self.assertEqual(cli.returncode, 0, cli.stderr)
            self.assertEqual(json.loads(cli.stdout)['rows'], 12)

    def test_corruption_is_rejected_before_output_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history, forecast = self.make_run(root)
            path = forecast/'evaluation.csv'
            path.write_text(path.read_text().replace(',0.5', ',0.6', 1))
            with self.assertRaisesRegex(ValueError, 'integrity'):
                run_analysis(history, forecast, root/'analysis')
            self.assertFalse((root/'analysis').exists())
            # Even updating the self-reported hash cannot bypass exact source joins.
            report = json.loads((forecast/'report.json').read_text())
            report['artifacts']['evaluation.csv'] = digest(path)
            (forecast/'report.json').write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, 'probability does not match'):
                load_rows(history, forecast)

    def test_nonconstant_prices_and_incomplete_forecast_are_supported(self):
        class VaryingHTTP(FakeHTTP):
            def get(self, path, params):
                response = super().get(path, params)
                if path == '/cfbenchmarks/history/values':
                    for point in response.data['data']['payload']:
                        seconds = point['time'] // 1000
                        # Every minute still averages exactly 100; source prices
                        # and minute returns vary without changing the labels.
                        shift = Decimal((seconds // 60) % 7 - 3) * Decimal('0.001')
                        point['value'] = str(Decimal(100) + shift * (Decimal(seconds % 60) - Decimal('29.5')))
                        if seconds == int(START.timestamp()) + 49 * 60 + 59:
                            point['repeatOfPreviousValue'] = True
                return response
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history, forecast = root/'history', root/'forecast'
            download(history, VaryingHTTP())
            original = run_forecast(history, forecast, paths=100)
            self.assertEqual(original['status'], 'incomplete')
            result = run_analysis(history, forecast, root/'analysis')
            self.assertEqual(result['source_status'], 'incomplete')
            self.assertEqual(result['rows'], original['paired_rows'])
            rows, _, _ = load_rows(history, forecast)
            self.assertTrue(all(r['rolling_average_sd_dollars'] > 0 for r in rows))
            self.assertTrue(any(r['return_2m_log'] != 0 for r in rows))
            for horizon in (10, 5, 2):
                for model in ('rolling', 'ewma'):
                    panel = result['panels'][f'{horizon}min'][model]
                    self.assertEqual(sum(p['rows'] for p in panel['by_gap_z'].values()), panel['rows'])
                    self.assertAlmostEqual(panel['sum_excess_brier'], panel['worst_three_sum_excess_brier'] +
                                           panel['remaining_rows']['sum_excess_brier'])

    def test_duplicate_or_retimed_predictions_are_rejected(self):
        for mutation in ('duplicate', 'retime'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                history, forecast = self.make_run(root)
                path = forecast/'predictions.jsonl'
                lines = path.read_text().splitlines()
                if mutation == 'duplicate':
                    lines.append(lines[0])
                else:
                    row = json.loads(lines[0])
                    row['source_ts'] += 1
                    lines[0] = json.dumps(row)
                path.write_text('\n'.join(lines) + '\n')
                report = json.loads((forecast/'report.json').read_text())
                report['artifacts']['predictions.jsonl'] = digest(path)
                (forecast/'report.json').write_text(json.dumps(report))
                with self.assertRaises(ValueError):
                    run_analysis(history, forecast, root/'analysis')
                self.assertFalse((root/'analysis').exists())
