"""Offline historical-data fixtures. All prices and rules below are synthetic."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from kalshi_lab.diagnostics import ProbeFailure, Response
from kalshi_lab.history import digest, fetch_history, quote_rows, save_json
from kalshi_lab.settlement import audit_dataset, minute

START = datetime(2026, 10, 26, tzinfo=timezone.utc)
NOW = START + timedelta(days=2)


def market(i):
    opened = START + timedelta(minutes=15*i)
    return {'ticker': f'KXBTC15M-SYNTHETIC-{i}', 'market_type': 'binary', 'notional_value_dollars': '1',
            'open_time': opened.isoformat(), 'close_time': (opened+timedelta(minutes=15)).isoformat(),
            'floor_strike': '100.00', 'strike_type': 'greater_or_equal', 'result': 'yes',
            'expiration_value': '100.00', 'rules_primary': 'SYNTHETIC test: final minute average >= opening average.',
            'rules_secondary': 'SYNTHETIC test, nearest 2 decimals.', 'is_provisional': False}


class FakeHTTP:
    def __init__(self, *, archived=False, failure=None, missing_second=False, revised=False):
        self.calls = []
        self.archived, self.failure = archived, failure
        self.missing_second, self.revised = missing_second, revised

    def get(self, path, params):
        self.calls.append((path, dict(params)))
        if self.failure and self.failure[0] in path:
            raise self.failure[1]
        if path in ('/markets', '/historical/markets'):
            present = path == ('/historical/markets' if self.archived else '/markets')
            data = {'markets': [market(i) for i in range(4)] if present else [], 'cursor': ''}
        elif path.endswith('/candlesticks'):
            data = {'candlesticks': [{'end_period_ts': t, 'yes_bid': {'close_dollars': '0.49'},
                                     'yes_ask': {'close_dollars': '0.51'}}
                                    for t in range(params['start_ts'], params['end_ts']+1, 60)]}
        elif path == '/cfbenchmarks/history/values':
            lower = int(datetime.fromisoformat(params['timestamp'].replace('Z', '+00:00')).timestamp()*1000)
            points = [{'time': t, 'value': '100.00'} for t in range(lower, lower+3600000, 1000)]
            if self.missing_second:
                points = [p for p in points if p['time'] != int(START.timestamp()*1000)+899000]
            if self.revised:
                for p in points:
                    p.update(amendTime=p['time']+100000, repeatOfPreviousValue=True)
            data = {'data': {'payload': points}}
        else:
            raise AssertionError(path)
        return Response(data, 'synthetic-response-digest', NOW.isoformat())


def download(path, http=None, **kwargs):
    return fetch_history(path, start=START.isoformat(), hours=1, http=http or FakeHTTP(),
                         now=NOW, pause=lambda _: None, **kwargs)


class HistoryTests(unittest.TestCase):
    def test_small_download_and_independent_offline_audit(self):
        http = FakeHTTP()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'history'
            report = download(path, http)
            self.assertEqual(report['status'], 'downloaded')
            self.assertEqual(report['contracts'], 4)
            self.assertEqual(report['quote_rows'], 60)
            self.assertEqual(len(report['index_hours']), 3)  # warm-up, requested, final-close hour
            self.assertEqual(len(http.calls), 8)
            self.assertNotIn('status', http.calls[0][1])  # close filters incompatible with settled status
            self.assertEqual(report['index_hours'][-1]['hour'], (START+timedelta(hours=1)).isoformat())
            for artifact in report['files']:
                self.assertEqual(digest(path/artifact['path']), artifact['sha256'])
            result = audit_dataset(path)
            self.assertEqual(result['status'], 'computed')
            self.assertIsNone(result['selected_candidate'])
            self.assertTrue(result['rule_review_required'])
            self.assertFalse(result['point_in_time_certified'])
            for candidate in result['candidates']:
                self.assertEqual(candidate['counts']['opening_matches_reference']['matches'], 4)
                self.assertEqual(candidate['counts']['result_matches']['matches'], 4)
                self.assertEqual(candidate['counts']['expiration_value_matches']['matches'], 4)
            before = (path/'manifest.json').read_bytes()
            with self.assertRaises(ValueError): download(path, http)
            self.assertEqual(before, (path/'manifest.json').read_bytes())
            self.assertEqual(len(http.calls), 8)

    def test_missing_second_is_not_filled_or_silently_averaged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'history'
            report = download(path, FakeHTTP(missing_second=True))
            self.assertEqual(report['status'], 'incomplete')
            result = audit_dataset(path)
            self.assertEqual(result['status'], 'incomplete')
            for candidate in result['candidates']:
                closing = candidate['contracts'][0]['closing']
                self.assertEqual(closing['missing'], 1)
                self.assertNotIn('mean', closing)
                self.assertIsNone(candidate['contracts'][0]['result_matches'])

    def test_archived_fallback_uses_only_supported_filters_and_correct_candles(self):
        http = FakeHTTP(archived=True)
        with tempfile.TemporaryDirectory() as tmp:
            report = download(Path(tmp)/'history', http)
        self.assertEqual(report['status'], 'downloaded')
        historical = [params for path, params in http.calls if path == '/historical/markets']
        self.assertEqual(historical, [{'series_ticker': 'KXBTC15M', 'limit': 1000}])
        self.assertTrue(all(path.startswith('/historical/') for path, _ in http.calls if path.endswith('/candlesticks')))

    def test_pagination_cursor_loop_and_page_cap_are_explicit_failures(self):
        for repeated in (True, False):
            class Pages(FakeHTTP):
                def get(self, path, params):
                    self.calls.append((path, params))
                    return Response({'markets': [], 'cursor': 'same' if repeated else str(len(self.calls))}, 'digest', NOW.isoformat())
            http = Pages()
            with tempfile.TemporaryDirectory() as tmp:
                report = download(Path(tmp)/'history', http)
            self.assertEqual(report['status'], 'blocked')
            self.assertEqual(report['failure']['reason'], 'repeated_pagination_cursor' if repeated else 'market_page_limit')
            self.assertLessEqual(len(http.calls), 4)

    def test_gaps_in_listing_and_duplicate_windows_are_visible(self):
        for duplicate in (False, True):
            class Changed(FakeHTTP):
                def get(self, path, params):
                    r = super().get(path, params)
                    if path == '/markets':
                        r.data['markets'] = [market(0), market(1), market(2)]
                        if duplicate:
                            r.data['markets'].append({**market(2), 'ticker': 'KXBTC15M-SYNTHETIC-DUPLICATE'})
                    return r
            with tempfile.TemporaryDirectory() as tmp:
                report = download(Path(tmp)/'history', Changed())
            self.assertEqual(report['status'], 'blocked' if duplicate else 'incomplete')
            if duplicate:
                self.assertEqual(report['failure']['reason'], 'duplicate_contract_window')
            else:
                self.assertEqual(len(report['missing_contract_opens']), 1)

    def test_outcome_and_reference_conflicts_are_not_hidden_by_rule_selection(self):
        class Wrong(FakeHTTP):
            def get(self, path, params):
                r = super().get(path, params)
                if path == '/markets':
                    r.data['markets'][0].update(floor_strike='101.00', expiration_value='102.00')
                return r
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'history'
            download(path, Wrong())
            result = audit_dataset(path)
            for candidate in result['candidates']:
                row = candidate['contracts'][0]
                self.assertFalse(row['opening_matches_reference'])
                self.assertFalse(row['result_matches'])
                self.assertFalse(row['expiration_value_matches'])
            self.assertIsNone(result['selected_candidate'])

    def test_revision_flags_preserved_without_point_in_time_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'history'
            report = download(path, FakeHTTP(revised=True))
            self.assertEqual(report['index_hours'][0]['amended_samples'], 3600)
            result = audit_dataset(path)
            opening = result['candidates'][0]['contracts'][0]['opening']
            self.assertEqual(opening['amended_samples'], 60)
            self.assertEqual(opening['repeated_samples'], 60)
            self.assertFalse(result['point_in_time_certified'])

    def test_entitlement_and_schema_failures_checkpoint_without_secret_text(self):
        for failure in (ProbeFailure('http_error', 403), ProbeFailure('http_error', 429)):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)/'history'
                report = download(path, FakeHTTP(failure=('/cfbenchmarks/', failure)))
                self.assertEqual(report['status'], 'blocked')
                self.assertEqual(report['failure']['http_status'], failure.status)
                self.assertEqual(json.loads((path/'manifest.json').read_text())['status'], 'blocked')
                self.assertEqual(report['index_hours'], [])
        class BadSchema(FakeHTTP):
            def get(self, path, params):
                return Response({'markets': 'SECRET upstream echo'}, 'digest', NOW.isoformat())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'history'
            self.assertEqual(download(path, BadSchema())['status'], 'blocked')
            self.assertNotIn('SECRET', (path/'manifest.json').read_text())

    def test_validation_happens_before_network_or_directory_creation(self):
        changes = [{'hours': 0}, {'hours': 25}, {'hours': True}, {'warmup_hours': 0}, {'timeout': float('nan')}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'history'
            for change in changes:
                http = FakeHTTP()
                with self.assertRaises(ValueError):
                    fetch_history(path, start=START.isoformat(), hours=change.get('hours', 1),
                                  warmup_hours=change.get('warmup_hours', 1), timeout=change.get('timeout', 10), http=http, now=NOW)
                self.assertFalse(path.exists())
                self.assertFalse(http.calls)
            for stamp in ['2026-10-26T00:01:00Z', '2026-10-26T00:00:00', NOW.isoformat()]:
                with self.assertRaises(ValueError): fetch_history(path, start=stamp, hours=1, http=FakeHTTP(), now=NOW)
            self.assertFalse(path.exists())

    def test_integrity_verification_rejects_modified_and_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'history'
            download(path)
            (path/'contracts.json').write_text('[]')
            with self.assertRaisesRegex(ValueError, 'integrity'): audit_dataset(path)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'history'
            download(path)
            manifest = json.loads((path/'manifest.json').read_text())
            manifest['files'][0]['path'] = '../elsewhere.json'
            save_json(path/'manifest.json', manifest)
            with self.assertRaisesRegex(ValueError, 'filename'): audit_dataset(path)

    def test_five_hz_history_is_preserved_but_audit_uses_exact_seconds(self):
        import gzip
        class FiveHz(FakeHTTP):
            def get(self, path, params):
                if path == '/cfbenchmarks/history/values':
                    self.calls.append((path, params))
                    lower = int(datetime.fromisoformat(params['timestamp'].replace('Z', '+00:00')).timestamp()*1000)
                    points = [{'time': t, 'value': '100.00' if t % 1000 == 0 else '900.00'}
                              for t in range(lower, lower+3600000, 200)]
                    return Response({'data': {'payload': points}}, 'synthetic', NOW.isoformat())
                return super().get(path, params)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'history'
            report = download(path, FiveHz())
            self.assertEqual(report['index_hours'][0]['samples'], 18000)
            self.assertEqual(report['index_hours'][0]['whole_second_samples'], 3600)
            with gzip.open(path/report['index_hours'][0]['file'], 'rt') as handle:
                self.assertIn('900.00', handle.read())
            result = audit_dataset(path)
            self.assertEqual(result['candidates'][0]['contracts'][0]['opening']['rounded'], '100.00')

    def test_successful_pagination_and_archive_coverage_can_stop_early(self):
        class Pages(FakeHTTP):
            def get(self, path, params):
                if path == '/markets':
                    self.calls.append((path, params))
                    offset = 2 if params.get('cursor') else 0
                    return Response({'markets': [market(i) for i in range(offset, offset+2)],
                                     'cursor': '' if offset else 'next-page'}, 'synthetic', NOW.isoformat())
                return super().get(path, params)
        http = Pages()
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(download(Path(tmp)/'history', http)['status'], 'downloaded')
        self.assertEqual(http.calls[1][1]['cursor'], 'next-page')
        self.assertFalse(any(path == '/historical/markets' for path, _ in http.calls))
        class Archive(FakeHTTP):
            def get(self, path, params):
                r = super().get(path, params)
                if path == '/historical/markets':
                    r.data['cursor'] = 'older-unneeded-page'
                return r
        http = Archive(archived=True)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(download(Path(tmp)/'history', http)['status'], 'downloaded')
        self.assertEqual(sum(path == '/historical/markets' for path, _ in http.calls), 1)

    def test_quote_coverage_and_provisional_outcomes_block_clean_download(self):
        class Partial(FakeHTTP):
            def get(self, path, params):
                r = super().get(path, params)
                if path.endswith('/candlesticks'):
                    r.data['candlesticks'] = r.data['candlesticks'][:5]
                return r
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'history'
            report = download(path, Partial())
            self.assertEqual(report['status'], 'incomplete')
            self.assertTrue(any(i.startswith('incomplete_quotes:') for i in report['issues']))
            self.assertEqual(audit_dataset(path)['status'], 'incomplete')
        class Provisional(FakeHTTP):
            def get(self, path, params):
                r = super().get(path, params)
                if path == '/markets':
                    r.data['markets'][0]['is_provisional'] = True
                return r
        with tempfile.TemporaryDirectory() as tmp:
            report = download(Path(tmp)/'history', Provisional())
        self.assertEqual(report['failure']['reason'], 'provisional_outcome')

    def test_cli_offline_audit_and_existing_output_preservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'history'
            download(path)
            command = [sys.executable, '-m', 'kalshi_lab', 'audit', '--run-dir', str(path)]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            out = path/'settlement_audit.json'
            before = out.read_bytes()
            self.assertEqual(json.loads(before)['contracts'], 4)
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 1)
            self.assertEqual(out.read_bytes(), before)

    def test_interruption_is_checkpointed(self):
        class Interrupted(FakeHTTP):
            def get(self, path, params):
                if path.endswith('/history/values'):
                    raise KeyboardInterrupt()
                return super().get(path, params)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'history'
            with self.assertRaises(KeyboardInterrupt): download(path, Interrupted())
            self.assertEqual(json.loads((path/'manifest.json').read_text())['status'], 'interrupted')


class SamplingTests(unittest.TestCase):
    def test_five_hz_ticks_are_not_all_averaged_and_boundaries_differ(self):
        end = 120000
        points = {t: {'value': '999', 'amend_time_ms': None, 'repeat_of_previous_value': False}
                  for t in range(60000, 120001, 200)}
        for t in range(60000, 120001, 1000):
            points[t]['value'] = '100'
        points[60000]['value'], points[120000]['value'] = '40', '160'
        left = minute(points, end, 'left_closed', 'half_up')
        right = minute(points, end, 'right_closed', 'half_up')
        self.assertEqual(Decimal(left['mean']), 99)
        self.assertEqual(Decimal(right['mean']), 101)
        self.assertEqual(right['samples'], 60)

    def test_rounding_tie_uses_mean_then_single_round(self):
        points = {t: {'value': '100.005', 'amend_time_ms': None, 'repeat_of_previous_value': False}
                  for t in range(1000, 61000, 1000)}
        self.assertEqual(minute(points, 60000, 'right_closed', 'half_up')['rounded'], '100.01')
        self.assertEqual(minute(points, 60000, 'right_closed', 'half_even')['rounded'], '100.00')

    def test_candle_null_crossed_and_wrong_timestamps(self):
        lower = int(START.timestamp())
        data = {'candlesticks': [{'end_period_ts': lower+60, 'yes_bid': {'close_dollars': None}, 'yes_ask': {'close_dollars': '0.5'}}]}
        self.assertFalse(quote_rows(data, market(0))[0]['two_sided'])
        data['candlesticks'][0]['yes_bid']['close_dollars'] = '0.6'
        self.assertFalse(quote_rows(data, market(0))[0]['two_sided'])
        data['candlesticks'][0]['end_period_ts'] += 1
        with self.assertRaises(ProbeFailure): quote_rows(data, market(0))
