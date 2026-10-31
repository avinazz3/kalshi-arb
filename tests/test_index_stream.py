from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest

from kalshi_lab.index_stream import IndexStream, URL
from kalshi_lab.shadow import run_shadow
from test_history import START
from test_shadow import FakeClock, LiveHTTP


class FakeApp:
    def __init__(self, url, **kwargs):
        self.url, self.callbacks, self.sent = url, kwargs, []

    def send(self, value):
        self.sent.append(json.loads(value))

    def close(self):
        pass


class StreamTests(unittest.TestCase):
    def test_documented_frame_reuses_index_validation_and_does_not_leak_errors(self):
        paths = []
        stream = IndexStream(lambda path: paths.append(path) or {}, FakeApp)
        self.assertEqual(stream.app.url, URL)
        stream.app.callbacks['header']()
        self.assertEqual(paths, ['/trade-api/ws/v2'])
        stream.subscribe(stream.app)
        self.assertEqual(stream.app.sent[0]['params'],
                         {'channels': ['cfbenchmarks_value'], 'index_ids': ['BRTI']})
        point = {'type': 'value', 'id': 'BRTI', 'time': 1793336400000, 'value': '100.12'}
        raw = json.dumps({'type': 'cfbenchmarks_value', 'msg': {
            'index_id': 'BRTI', 'data': json.dumps(point)}})
        stream.message(stream.app, raw)
        event = list(stream.drain())[0]
        self.assertEqual(event['value'][0]['value'], '100.12')
        self.assertEqual(len(event['response_sha256']), 64)
        for bad in ('SECRET malformed', json.dumps({'type': 'error', 'msg': 'SECRET'})):
            stream.message(stream.app, bad)
        self.assertNotIn('SECRET', json.dumps(list(stream.drain())))

    def test_bounded_queue_reports_loss(self):
        stream = IndexStream(lambda _: {}, FakeApp)
        for _ in range(10001):
            stream.put({'error': 'test'})
        self.assertEqual(next(stream.drain()), {'error': 'queue_overflow'})

    def test_stream_collects_seconds_while_rest_book_is_slow(self):
        clock = FakeClock(START + timedelta(minutes=4, seconds=55))
        class Stream:
            closed = False
            last = int(clock.clock().timestamp())-1
            def start(self): pass
            def close(self): self.closed = True
            def drain(self):
                now = int(clock.clock().timestamp())-1
                for t in range(self.last+1, now+1):
                    yield {'value': [{'time_ms':t*1000, 'value':'100.00',
                                     'amend_time_ms':None, 'repeat_of_previous_value':False}],
                           'stream_received_at': clock.clock().isoformat(), 'response_sha256':'fake'}
                self.last = now
        class SlowBooks(LiveHTTP):
            def get(self, path, params=None):
                if path.endswith('/orderbook'):
                    clock.pause(1.7)
                return super().get(path, params)
        stream = Stream()
        http = SlowBooks(clock)
        with tempfile.TemporaryDirectory() as tmp:
            report = run_shadow(Path(tmp)/'run', minutes=1, paths=100, stream=stream,
                                bootstrap_history=True, http=http,
                                clock=clock.clock, monotonic=clock.monotonic, pause=clock.pause)
            self.assertEqual(report['predictions'], 1)
            events = [json.loads(s) for s in (Path(tmp)/'run'/'observations.jsonl').read_text().splitlines()]
            self.assertFalse(any(path.endswith('latest_values') for path, _ in http.calls))
            self.assertTrue(any(e.get('stream_received_at') for e in events))
            self.assertEqual([e['received_at'] for e in events], sorted(e['received_at'] for e in events))
            self.assertTrue(stream.closed)

    def test_wall_deadline_stops_after_suspension_without_late_forecast(self):
        clock = FakeClock(START+timedelta(minutes=5))
        class SleepingHTTP(LiveHTTP):
            def get(self, path, params=None):
                # Simulate Mac sleep: wall clock advances, monotonic does not.
                clock.now += timedelta(minutes=15)
                return super().get(path, params)
        http = SleepingHTTP(clock)
        with tempfile.TemporaryDirectory() as tmp:
            report = run_shadow(Path(tmp)/'run', minutes=1, paths=100, http=http,
                                clock=clock.clock, monotonic=clock.monotonic, pause=clock.pause)
            self.assertEqual(len(http.calls), 1)
            self.assertEqual(report['predictions'], 0)
            self.assertGreater(report['wall_elapsed_seconds'], 60)
            self.assertEqual(report['status'], 'completed_with_gaps')

    def test_empty_stream_fails_early_and_closes(self):
        clock = FakeClock(START)
        class EmptyStream:
            closed = False
            def start(self): pass
            def drain(self): return iter(())
            def close(self): self.closed = True
        stream = EmptyStream()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'run'
            with self.assertRaisesRegex(ValueError, 'No BRTI stream ticks'):
                run_shadow(root, minutes=120, paths=100, stream=stream, http=LiveHTTP(clock),
                           clock=clock.clock, monotonic=clock.monotonic, pause=clock.pause)
            self.assertLess(clock.elapsed, 32)
            self.assertTrue(stream.closed)
            self.assertEqual(json.loads((root/'report.json').read_text())['status'], 'failed')
