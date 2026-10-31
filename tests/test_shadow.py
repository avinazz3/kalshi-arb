from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from test_history import START, market
from kalshi_lab.diagnostics import ProbeFailure, Response
from kalshi_lab.shadow import ShadowHTTP, live_prediction, normalize_book, run_shadow
from kalshi_lab.forecast import Unavailable


class FakeClock:
    def __init__(self, now):
        self.now, self.elapsed = now, 0.0

    def clock(self):
        return self.now + timedelta(seconds=self.elapsed)

    def monotonic(self):
        return self.elapsed

    def pause(self, seconds):
        self.elapsed += seconds


class LiveHTTP:
    def __init__(self, clock, failure=False):
        self.clock, self.failure, self.calls = clock, failure, []

    def get(self, path, params=None):
        self.calls.append((path, params))
        if self.failure:
            raise ProbeFailure('http_error', 503)
        self.clock.pause(.01)
        now = self.clock.clock()
        if path == '/markets':
            data = {'markets': [{**market(0), 'status': 'active'}]}
        elif path.endswith('/orderbook'):
            data = {'orderbook_fp': {'yes_dollars': [['0.40', '5.00']], 'no_dollars': [['0.40', '6.00']]}}
        elif path.endswith('history/values'):
            lower = int(datetime.fromisoformat(params['timestamp'].replace('Z', '+00:00')).timestamp())
            data = {'data': {'payload': [{'time': t*1000, 'value': '100.00'} for t in range(lower, min(lower+3600, int(now.timestamp())))]}}
        elif path.endswith('latest_values'):
            data = {'data': {'payload': {'latest_values': {'BRTI': {'time': (int(now.timestamp())-1)*1000, 'value': '100.00'}}}}}
        else:
            raise AssertionError(path)
        return Response(data, 'synthetic-response-hash', now.isoformat())


class ShadowTests(unittest.TestCase):
    def test_book_units_complements_sorting_and_empty_depth(self):
        book = normalize_book({'orderbook_fp': {'yes_dollars': [['.4', '2.5'], ['.1', '8']], 'no_dollars': [['.55', '3']]}})
        self.assertEqual(book['yes_bids'][0], ['0.4', '2.5'])
        self.assertEqual(book['yes_ask'], '0.45')
        self.assertEqual(book['no_ask'], '0.6')
        self.assertFalse(normalize_book({'orderbook_fp': {}})['two_sided'])
        for levels in ([['.5', '0']], [['.5', '1'], ['.5', '2']], [['.50001', '2']]):
            with self.assertRaises(ValueError):
                normalize_book({'orderbook_fp': {'yes_dollars': levels}})
        with self.assertRaisesRegex(ValueError, 'Crossed'):
            normalize_book({'orderbook_fp': {'yes_dollars': [['.6','1']], 'no_dollars': [['.5','1']]}})

    def test_transport_allowlist_excludes_accounts_and_order_submission(self):
        http = ShadowHTTP()
        self.assertTrue(http.allowed_path('/markets/KXBTC15M-TEST/orderbook'))
        for path in ('/portfolio/orders', '/markets/OTHER/orderbook', '/cfbenchmarks/secrets', '/markets/KXBTC15M-../orderbook'):
            self.assertFalse(http.allowed_path(path))
            with self.assertRaises(ValueError):
                http.get(path)

    def test_live_inputs_require_receipt_before_freeze_and_ignore_future_observations(self):
        freeze = START + timedelta(minutes=5)
        source = int(freeze.timestamp()) - 1
        points = {t*1000: dict(value='100.00', amend_time_ms=None, repeat_of_previous_value=False,
                              received_at=(freeze-timedelta(seconds=1)).isoformat(), observation_id=0)
                  for t in range(source-3600, source+1, 60)}
        m = {'ticker':'KXBTC15M-TEST','reference':'100.00','comparison':'greater_or_equal',
             'close_time':(START+timedelta(minutes=15)).isoformat()}
        args = (m, 10, int(freeze.timestamp()), freeze)
        before = live_prediction(points, *args, paths=100)
        points[(source+2)*1000] = dict(value='9999',received_at=(freeze+timedelta(seconds=2)).isoformat())
        self.assertEqual(before, live_prediction(points,*args,paths=100))
        points[source*1000]['received_at'] = (freeze+timedelta(seconds=1)).isoformat()
        with self.assertRaises(Unavailable):
            live_prediction(points,*args,paths=100)

    def test_bounded_recording_bootstraps_and_flushes_received_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock(START + timedelta(minutes=4, seconds=55))
            http = LiveHTTP(clock)
            root = Path(tmp)/'shadow'
            result = run_shadow(root, minutes=1, paths=100, bootstrap_history=True, http=http, clock=clock.clock,
                                monotonic=clock.monotonic, pause=clock.pause)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['predictions'], 1)
            decisions = [json.loads(s) for s in (root/'decisions.jsonl').read_text().splitlines()]
            forecast = next(d for d in decisions if d['status']=='forecast')
            self.assertEqual(forecast['horizon_minutes'], 10)
            self.assertTrue(all(p['received_at'] <= forecast['prediction']['calculation_started_at']
                                for p in forecast['prediction']['input_observations']))
            events = [json.loads(s) for s in (root/'observations.jsonl').read_text().splitlines()]
            books = [e for e in events if e['kind']=='book']
            self.assertTrue(all(e['value']['ticker'] == market(0)['ticker'] for e in books))
            self.assertTrue(any(e['request_started_at'] > forecast['available_at'] for e in books))
            self.assertLessEqual(clock.elapsed, 60.1)
            with self.assertRaises(ValueError):
                run_shadow(root, http=http)

    def test_failures_and_interruption_checkpoint_without_sensitive_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock(START + timedelta(minutes=5))
            result = run_shadow(Path(tmp)/'failed', minutes=1, paths=100, bootstrap_history=False,
                                http=LiveHTTP(clock, failure=True), clock=clock.clock,
                                monotonic=clock.monotonic, pause=clock.pause)
            self.assertEqual(result['status'], 'completed_with_gaps')
            self.assertTrue(result['errors'])
            def stop(_):
                raise KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                run_shadow(Path(tmp)/'interrupted', minutes=1, paths=100, bootstrap_history=False,
                           http=LiveHTTP(clock), clock=clock.clock, monotonic=clock.monotonic, pause=stop)
            report=json.loads((Path(tmp)/'interrupted'/'report.json').read_text())
            self.assertEqual(report['status'], 'interrupted')
            self.assertIn('decisions.jsonl',report['artifacts'])

    def test_contract_rollover_is_recorded_without_reusing_the_old_ticker(self):
        class RolloverHTTP(LiveHTTP):
            def get(self,path,params=None):
                response=super().get(path,params)
                if path=='/markets':
                    i=int((self.clock.clock()-START).total_seconds())//900
                    response.data={'markets':[{**market(i),'status':'active'}]}
                return response
        with tempfile.TemporaryDirectory() as tmp:
            clock=FakeClock(START+timedelta(minutes=14,seconds=55))
            root=Path(tmp)/'shadow'
            result=run_shadow(root,minutes=1,paths=100,http=RolloverHTTP(clock),clock=clock.clock,
                              monotonic=clock.monotonic,pause=clock.pause)
            self.assertEqual(result['status'],'completed_with_gaps')
            events=[json.loads(s) for s in (root/'observations.jsonl').read_text().splitlines()]
            self.assertEqual({e['value']['ticker'] for e in events if e['kind']=='book'}, {market(0)['ticker'],market(1)['ticker']})
            self.assertTrue(all('result' not in e['value'] for e in events if e['kind']=='market'))
