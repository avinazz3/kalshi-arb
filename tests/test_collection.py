import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from kalshi_lab.capture import AuthenticationRejected, CaptureIncomplete
from kalshi_lab.client import Client, RequestDeadline, RequestError
from kalshi_lab.collection import (CollectionFailed, WindowNotReady, _discover_archived,
                                   archived_discovery, run_collection, window_plan)
from kalshi_lab.dataset import build_dataset
from kalshi_lab.discovery import ASSETS, NoActiveMarket
from kalshi_lab.replay import load_replay
from kalshi_lab.storage import Store
from kalshi_lab.stream_store import StreamWriter, inspect_stream


class FakeClock:
    def __init__(self, elapsed=0):
        self.elapsed, self.sleeps = elapsed, []
        self.origin = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def now(self): return self.origin + timedelta(seconds=self.elapsed)
    def monotonic(self): return self.elapsed
    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.elapsed += seconds
        await asyncio.sleep(0)


def manifest(clock, assets=('BTC', 'ETH'), window=None):
    window = int(clock.elapsed // 900) if window is None else window
    records = []
    for asset in assets:
        series, product = ASSETS[asset]
        records.append({'asset': asset, 'coinbase_product': product, 'series': {'ticker': series},
            'market': {'ticker': series + f'-SYNTHETIC-{window}', 'market_type': 'binary',
                'status': 'active', 'notional_value_dollars': '1.0000', 'exchange_index': 2,
                'open_time': (clock.origin + timedelta(seconds=window * 900)).isoformat(),
                'close_time': (clock.origin + timedelta(seconds=(window + 1) * 900)).isoformat()}})
    return {'purpose': 'crypto_order_flow_research', 'assets': records, 'synthetic': True}


def fake_capture(clock, calls):
    async def capture(path, seconds, *, universe):
        calls.append((str(path), seconds, universe))
        session = f'synthetic-{len(calls)}'
        writer = StreamWriter(path, session, {'synthetic': True, 'reference_only': False, 'universe': universe})
        mono, wall = int(clock.elapsed * 1e9), int(clock.now().timestamp() * 1e9)
        cb_seq = 0
        try:
            for r in universe['assets']:
                writer.record('kalshi', 'k', 'frame', json.dumps({'type': 'orderbook_snapshot',
                    'sid': universe['assets'].index(r) + 1, 'seq': 1, 'msg': {'market_ticker': r['market']['ticker'],
                    'yes_dollars_fp': [['.49', '10']], 'no_dollars_fp': [['.50', '12']]}}), wall_ns=wall, mono_ns=mono)
                writer.record('coinbase', 'c', 'frame', json.dumps({'channel': 'l2_data', 'sequence_num': cb_seq,
                    'events': [{'type': 'snapshot', 'product_id': r['coinbase_product'], 'updates': [
                        {'side': 'bid', 'price_level': '100', 'new_quantity': '1'},
                        {'side': 'offer', 'price_level': '101', 'new_quantity': '2'}]}]}), wall_ns=wall, mono_ns=mono)
                cb_seq += 1
            writer.record('coinbase', 'c', 'frame', json.dumps({'channel': 'subscriptions', 'sequence_num': cb_seq,
                'events': [{'subscriptions': {'level2': [r['coinbase_product'] for r in universe['assets']],
                    'market_trades': [r['coinbase_product'] for r in universe['assets']]}}]}), wall_ns=wall, mono_ns=mono)
            cb_seq += 1
            # Deliberately emit only 12s; the run report must not confuse this
            # observation span with the scheduler's much longer requested time.
            for second in range(1, 13):
                writer.record('coinbase', 'c', 'frame', json.dumps({'channel': 'heartbeats',
                    'sequence_num': cb_seq, 'events': []}), wall_ns=wall + second * 10**9, mono_ns=mono + second * 10**9)
                cb_seq += 1
                for index, r in enumerate(universe['assets']):
                    writer.record('kalshi', 'k', 'frame', json.dumps({'type': 'orderbook_delta',
                        'sid': index + 1, 'seq': second + 1, 'msg': {'market_ticker': r['market']['ticker'],
                            'side': 'yes', 'price_dollars': '.49', 'delta_fp': '1'}}),
                        wall_ns=wall + second * 10**9, mono_ns=mono + second * 10**9)
            writer.record('collector', 'x', 'finished', '{"status":"synthetic_completed"}',
                wall_ns=wall + int(seconds * 1e9), mono_ns=mono + int(seconds * 1e9))
        finally:
            writer.close()
        clock.elapsed += seconds
        return {'session': session, 'quality': inspect_stream(path)}
    return capture


class WindowPlanTests(unittest.TestCase):
    def test_metadata_drives_close_no_ticker_string_schedule(self):
        clock = FakeClock(123)
        doc = manifest(clock)
        for record in doc['assets']: record['market']['ticker'] += '-UNUSUAL-SUFFIX'
        records, close = window_plan(doc, ['BTC', 'ETH'], clock.now(), set(), 30)
        self.assertEqual(close, clock.origin + timedelta(seconds=900))
        self.assertEqual(len(records), 2)

    def test_near_expiry_mismatch_stale_inactive_and_repeated_markets(self):
        clock = FakeClock(880); doc = manifest(clock)
        with self.assertRaisesRegex(WindowNotReady, 'near_expiry'):
            window_plan(doc, ['BTC', 'ETH'], clock.now(), set(), 30)
        clock.elapsed = 0
        doc['assets'][1]['market']['close_time'] = (clock.origin + timedelta(seconds=1800)).isoformat()
        with self.assertRaisesRegex(WindowNotReady, 'mismatch'):
            window_plan(doc, ['BTC', 'ETH'], clock.now(), set(), 30)
        doc = manifest(clock)
        with self.assertRaisesRegex(WindowNotReady, 'already_attempted'):
            window_plan(doc, ['BTC', 'ETH'], clock.now(), {doc['assets'][0]['market']['ticker']}, 30)
        with self.assertRaisesRegex(WindowNotReady, 'stale'):
            window_plan(doc, ['BTC', 'ETH'], clock.origin + timedelta(seconds=900), set(), 30)
        doc['assets'][0]['market']['status'] = 'closed'
        with self.assertRaisesRegex(WindowNotReady, 'inactive'):
            window_plan(doc, ['BTC', 'ETH'], clock.now(), set(), 30)

    def test_missing_requested_asset_is_fatal_instead_of_silent_subset(self):
        clock = FakeClock()
        with self.assertRaisesRegex(ValueError, 'exactly'):
            window_plan(manifest(clock, ['BTC']), ['BTC', 'ETH'], clock.now(), set(), 30)

    def test_unexpected_contract_duration_is_not_silently_treated_as_fifteen_minutes(self):
        clock = FakeClock(); doc = manifest(clock)
        for record in doc['assets']:
            record['market']['close_time'] = (clock.origin + timedelta(seconds=1800)).isoformat()
        with self.assertRaisesRegex(ValueError, '900-second'):
            window_plan(doc, ['BTC', 'ETH'], clock.now(), set(), 30)


class CollectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / 'run'
        self.clock, self.calls = FakeClock(), []

    async def discover(self, directory, assets, deadline):
        return manifest(self.clock, assets)

    async def run_it(self, **kwargs):
        settings = dict(seconds=1802, clock=self.clock, discover_window=self.discover,
                        capture_window=fake_capture(self.clock, self.calls), preflight=lambda: None)
        settings.update(kwargs)
        return await run_collection(self.directory, **settings)

    def read_report(self): return json.loads((self.directory / 'run.json').read_text())

    async def test_two_windows_have_independent_sessions_replay_and_datasets(self):
        result = await self.run_it()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['captured_windows'], 2)
        first, second = result['attempts']
        self.assertNotEqual(first['markets'], second['markets'])
        self.assertNotEqual(first['session'], second['session'])
        for attempt in result['attempts']:
            directory = self.directory / attempt['directory']
            self.assertTrue((directory / 'universe.json').exists())
            replay = load_replay(directory / 'capture.sqlite')
            self.assertEqual(replay.diagnostics['sequence_discontinuities'], 0)
            rows, quality = build_dataset(directory / 'capture.sqlite')
            self.assertTrue(rows)
            self.assertTrue(all(r['market_ticker'] in attempt['markets'].values() for r in rows))
            self.assertTrue(quality['synthetic'])
        self.assertEqual(first['coverage']['kalshi']['observed_span_seconds'], 12)
        self.assertGreater(first['requested_seconds'], 800)
        self.assertAlmostEqual(second['coverage']['kalshi']['gap_since_previous_window_seconds'], 888.25)
        self.assertLessEqual(max(self.clock.sleeps), 30)
        self.assertLessEqual(self.clock.elapsed, 1802)

    async def test_final_window_is_clipped_to_monotonic_run_budget(self):
        result = await self.run_it(seconds=960)
        self.assertEqual(result['captured_windows'], 2)
        self.assertAlmostEqual(self.calls[-1][1], 59.75)
        self.assertEqual(result['attempts'][-1]['planned_stop_reason'], 'run_budget')
        self.assertAlmostEqual(self.clock.elapsed, 960)

    async def test_near_expiry_waits_and_never_starts_old_tickers(self):
        self.clock.elapsed = 880
        result = await self.run_it(seconds=100)
        self.assertEqual(result['attempts'][0]['reason'], 'near_expiry')
        self.assertEqual(result['captured_windows'], 1)
        self.assertTrue(all(r['market']['ticker'].endswith('-1') for r in self.calls[0][2]['assets']))

    async def test_no_active_market_is_retried_without_counting_transport_failure(self):
        count = 0
        async def discover(*args):
            nonlocal count
            count += 1
            if count == 1: raise NoActiveMarket('temporary absence')
            return await self.discover(*args)
        result = await self.run_it(seconds=60, discover_window=discover)
        self.assertEqual(result['attempts'][0]['reason'], 'no_active_market')
        self.assertEqual(result['captured_windows'], 1)

    async def test_transient_metadata_failures_back_off_honor_retry_after_then_recover(self):
        count = 0
        async def discover(*args):
            nonlocal count
            count += 1
            if count < 3: raise RequestError('sensitive detail', status=429, retryable=True, retry_after=7)
            return await self.discover(*args)
        result = await self.run_it(seconds=100, discover_window=discover)
        self.assertEqual([a['retry_delay_seconds'] for a in result['attempts'][:2]], [7, 10])
        self.assertEqual(result['captured_windows'], 1)
        self.assertNotIn('sensitive detail', (self.directory / 'run.json').read_text())

    async def test_permanent_metadata_access_failure_stops_immediately(self):
        async def discover(*args): raise RequestError('SECRET', status=403, retryable=False)
        with self.assertRaises(CollectionFailed): await self.run_it(discover_window=discover)
        report = self.read_report()
        self.assertEqual(len(report['attempts']), 1)
        self.assertEqual(report['attempts'][0]['http_status'], 403)
        self.assertFalse(self.calls)
        self.assertNotIn('SECRET', json.dumps(report))

    async def test_transient_failure_limit_is_bounded(self):
        async def discover(*args): raise RequestError('outage', status=503, retryable=True)
        with self.assertRaises(CollectionFailed):
            await self.run_it(discover_window=discover, max_discovery_failures=2)
        self.assertEqual(len(self.read_report()['attempts']), 2)
        self.assertEqual(self.clock.elapsed, 5)

    async def test_unavailable_markets_end_with_explicit_no_windows_failure(self):
        async def discover(*args): raise NoActiveMarket('none')
        with self.assertRaisesRegex(CollectionFailed, 'No window'):
            await self.run_it(seconds=30, minimum_window_seconds=1, discover_window=discover)
        self.assertEqual(self.read_report()['status'], 'no_complete_windows')
        self.assertEqual(self.clock.elapsed, 30)

    async def test_metadata_deadline_does_not_start_capture(self):
        async def discover(*args): raise RequestDeadline('budget')
        with self.assertRaises(CollectionFailed): await self.run_it(discover_window=discover)
        self.assertEqual(self.read_report()['attempts'][0]['reason'], 'run_budget_exhausted')
        self.assertFalse(self.calls)

    async def test_repeated_stale_metadata_never_recaptures_same_market(self):
        async def discover(directory, assets, deadline): return manifest(self.clock, assets, window=0)
        result = await self.run_it(seconds=1000, discover_window=discover)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result['captured_windows'], 1)
        self.assertTrue(any(a.get('reason') == 'stale_or_not_open' for a in result['attempts']))

    async def test_mismatched_asset_windows_are_retried_as_a_bundle(self):
        count = 0
        async def discover(*args):
            nonlocal count
            count += 1
            doc = await self.discover(*args)
            if count == 1:
                doc['assets'][1]['market']['close_time'] = (self.clock.origin + timedelta(seconds=1800)).isoformat()
            return doc
        result = await self.run_it(seconds=60, discover_window=discover)
        self.assertEqual(result['attempts'][0]['reason'], 'asset_window_mismatch')
        self.assertEqual(len(self.calls), 1)

    async def test_incomplete_capture_preserves_quality_and_advances_to_next_window(self):
        capture = fake_capture(self.clock, self.calls)
        async def incomplete(*args, **kwargs):
            result = await capture(*args, **kwargs)
            if len(self.calls) == 1: raise CaptureIncomplete(result['session'], result['quality'])
            return result
        result = await self.run_it(capture_window=incomplete)
        self.assertEqual(result['incomplete_windows'], 1)
        self.assertEqual(result['captured_windows'], 1)
        self.assertEqual(result['attempts'][0]['status'], 'incomplete')
        self.assertTrue((self.directory / 'attempt-00001' / 'quality.json').exists())

    async def test_authentication_and_storage_errors_are_fatal_and_do_not_leak_messages(self):
        async def broken(*args, **kwargs): raise AuthenticationRejected('PRIVATE-SECRET')
        with self.assertRaises(CollectionFailed) as failure: await self.run_it(capture_window=broken)
        self.assertEqual(self.read_report()['status'], 'failed')
        self.assertNotIn('PRIVATE-SECRET', str(failure.exception))
        self.assertNotIn('PRIVATE-SECRET', (self.directory / 'run.json').read_text())
        self.assertEqual(len(self.read_report()['attempts']), 1)
        self.directory = Path(self.tmp.name) / 'disk-failure'
        async def disk(*args, **kwargs): raise OSError('disk full')
        with self.assertRaises(CollectionFailed): await self.run_it(capture_window=disk)
        self.assertEqual(self.read_report()['error_type'], 'OSError')

    async def test_cancellation_preserves_partial_capture_and_marks_interrupted(self):
        capture = fake_capture(self.clock, self.calls)
        async def cancelled(*args, **kwargs):
            await capture(*args, **kwargs)
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError): await self.run_it(capture_window=cancelled)
        report = self.read_report()
        self.assertEqual(report['status'], 'interrupted')
        self.assertEqual(report['attempts'][0]['status'], 'interrupted')
        self.assertTrue((self.directory / 'attempt-00001' / 'quality.json').exists())

    async def test_invalid_settings_preflight_and_existing_directory_do_not_write(self):
        for kwargs in [{'seconds': 0}, {'seconds': 86401}, {'retry_seconds': False},
                       {'seconds': 5}, {'assets': []}, {'assets': ['DOGE']}]:
            callback = Mock()
            with self.assertRaises(ValueError): await self.run_it(preflight=callback, **kwargs)
            callback.assert_not_called()
        def bad_key(): raise ValueError('invalid local key')
        with self.assertRaises(ValueError): await self.run_it(preflight=bad_key)
        self.assertFalse(self.directory.exists())
        self.directory.mkdir(); marker = self.directory / 'keep'; marker.write_text('keep')
        with self.assertRaisesRegex(ValueError, 'already exists'): await self.run_it()
        self.assertEqual(marker.read_text(), 'keep')

    async def test_cancelling_metadata_signals_and_joins_the_worker_before_returning(self):
        started, finished = Event(), Event()
        def worker(directory, assets, deadline, stop_event):
            started.set()
            signalled = stop_event.wait(2)
            finished.set()
            if not signalled: raise RuntimeError('worker was not cancelled')
            raise RequestDeadline('cancelled')
        with self.assertNoLogs('asyncio', level='ERROR'), patch('kalshi_lab.collection._discover_archived', side_effect=worker):
            task = asyncio.create_task(archived_discovery(self.directory, ['BTC'], 100))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            await asyncio.sleep(0)  # Drain worker-completion callbacks before checking logs.
        self.assertTrue(finished.is_set())

    async def test_repeated_cancellation_still_waits_for_metadata_worker_cleanup(self):
        started, stopping, release, finished = Event(), Event(), Event(), Event()
        def worker(directory, assets, deadline, stop_event):
            started.set()
            if not stop_event.wait(2): raise RuntimeError('worker was not cancelled')
            stopping.set()
            release.wait(2)
            finished.set()
            raise RequestDeadline('cancelled')
        with self.assertNoLogs('asyncio', level='ERROR'), patch('kalshi_lab.collection._discover_archived', side_effect=worker):
            task = asyncio.create_task(archived_discovery(self.directory, ['BTC'], 100))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                task.cancel()
                self.assertTrue(await asyncio.to_thread(stopping.wait, 1))
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError): await task
                await asyncio.sleep(0)
        self.assertTrue(finished.is_set())

    async def test_metadata_success_and_uncancelled_failure_are_propagated(self):
        expected = {'metadata': 'ok'}
        with patch('kalshi_lab.collection._discover_archived', return_value=expected):
            self.assertIs(await archived_discovery(self.directory, ['BTC'], 100), expected)
        failure = RequestError('test failure', status=503, retryable=True)
        with patch('kalshi_lab.collection._discover_archived', side_effect=failure):
            with self.assertRaises(RequestError) as caught:
                await archived_discovery(self.directory, ['BTC'], 100)
        self.assertIs(caught.exception, failure)

    async def test_fatal_second_window_keeps_first_window_and_its_quality(self):
        capture = fake_capture(self.clock, self.calls)
        async def fail_later(*args, **kwargs):
            if self.calls: raise OSError('disk full')
            return await capture(*args, **kwargs)
        with self.assertRaises(CollectionFailed): await self.run_it(capture_window=fail_later)
        report = self.read_report()
        self.assertEqual(report['captured_windows'], 1)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['attempts'][0]['status'], 'captured')
        self.assertTrue((self.directory / report['attempts'][0]['directory'] / 'quality.json').exists())


class MetadataBudgetTests(unittest.TestCase):
    def test_client_checks_deadline_and_cancellation_before_request(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'metadata.sqlite')
            try:
                with patch('kalshi_lab.client.urlopen') as call:
                    with self.assertRaises(RequestDeadline): Client(store, deadline=time.monotonic()-1).get('/markets')
                    stop = Event(); stop.set()
                    with self.assertRaises(RequestDeadline): Client(store, stop_event=stop).get('/markets')
                    call.assert_not_called()
            finally: store.close()

    def test_archived_discovery_links_full_metadata_to_raw_http_session(self):
        class Response:
            status, headers = 200, {}
            def __init__(self, payload): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(self.payload).encode()
        clock = FakeClock(); doc = manifest(clock, ['BTC'])
        market = doc['assets'][0]['market']
        market.update(open_time='2020-01-01T00:00:00Z', close_time='2099-01-01T00:00:00Z', event_ticker='EVENT')
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            with patch('kalshi_lab.client.urlopen', side_effect=[Response({'series': {'ticker': 'KXBTC15M'}}),
                    Response({'markets': [market]}), Response({'event': {'event_ticker': 'EVENT'}})]) as call:
                result = _discover_archived(directory, ['BTC'], time.monotonic()+5, Event())
            with closing(sqlite3.connect(directory / 'metadata.sqlite')) as db, db:
                rows = db.execute('SELECT session,status FROM responses').fetchall()
            self.assertEqual(len(rows), 3)
            self.assertTrue(all(s == result['metadata_session'] and status == 200 for s, status in rows))
            self.assertTrue(all(0 < kwargs['timeout'] <= 5 for _, kwargs in call.call_args_list))

    def test_typed_http_error_preserves_status_retryability_and_retry_after(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'metadata.sqlite')
            try:
                body = BytesIO(b'limit')
                with patch('kalshi_lab.client.urlopen', side_effect=HTTPError('x',429,'limit',{'retry-after':'12'},body)):
                    with self.assertRaises(RequestError) as caught: Client(store, retries=0).get('/markets')
                self.assertEqual(caught.exception.status, 429)
                self.assertTrue(caught.exception.retryable)
                self.assertEqual(caught.exception.retry_after, 12)
                self.assertTrue(body.closed)
            finally: store.close()

    def test_collect_cli_missing_credentials_fails_before_creating_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'run'
            env = dict(os.environ)
            env.pop('KALSHI_API_KEY_ID', None); env.pop('KALSHI_PRIVATE_KEY_PATH', None)
            result = subprocess.run([sys.executable, '-m', 'kalshi_lab', 'collect', '--run-dir', str(path)],
                                    capture_output=True, text=True, env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('KALSHI_API_KEY_ID', result.stderr)
            self.assertFalse(path.exists())


if __name__ == '__main__': unittest.main()
