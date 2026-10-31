"""One BRTI subscription; websocket-client owns framing, ping/pong and reconnects."""
from datetime import datetime, timezone
import hashlib
import json
from queue import Empty, Full, Queue
from threading import Thread

from .diagnostics import ProbeFailure, index_points

URL = 'wss://external-api-ws.kalshi.com/trade-api/ws/v2'


class IndexStream:
    def __init__(self, signer, app_factory=None):
        if app_factory is None:
            try:
                from websocket import WebSocketApp
            except ImportError:
                raise RuntimeError('Install the [data] optional dependency') from None
            app_factory = WebSocketApp
        self.queue = Queue(maxsize=10000)
        self.overflow = False
        self.stopping = False
        self.app = app_factory(
            URL, header=lambda: signer('/trade-api/ws/v2'),
            on_open=self.subscribe, on_reconnect=self.subscribe,
            on_message=self.message, on_error=lambda *_: self.error('connection_error'),
            on_close=lambda *_: self.error('disconnected'))
        self.thread = Thread(target=self.run, name='brti-stream', daemon=True)

    def put(self, item):
        try:
            self.queue.put_nowait(item)
        except Full:
            self.overflow = True

    def error(self, reason):
        if not self.stopping:
            self.put({'error': reason})

    def subscribe(self, app):
        app.send(json.dumps({'id': 1, 'cmd': 'subscribe', 'params': {
            'channels': ['cfbenchmarks_value'], 'index_ids': ['BRTI']}}))

    def message(self, app, raw):
        received = datetime.now(timezone.utc).isoformat()
        try:
            if len(raw) > 65536:
                raise ValueError('Frame too large')
            frame = json.loads(raw)
            if frame['type'] == 'error':
                self.error('subscription_error')
            elif frame['type'] == 'cfbenchmarks_value':
                if frame['msg']['index_id'] != 'BRTI':
                    raise ValueError('Unexpected index')
                point = json.loads(frame['msg']['data'])
                if point['id'] != 'BRTI' or point['type'] != 'value':
                    raise ValueError('Unexpected upstream frame')
                values = index_points({'data': {'payload': [point]}})
                self.put({'value': values, 'stream_received_at': received,
                          'response_sha256': hashlib.sha256(raw.encode()).hexdigest()})
        except (ValueError, KeyError, TypeError, ProbeFailure):
            self.error('invalid_frame')

    def run(self):
        try:
            self.app.run_forever(ping_interval=20, ping_timeout=10, reconnect=5)
        except Exception:
            self.error('connection_error')
        finally:
            self.error('stream_stopped')

    def start(self):
        self.thread.start()

    def drain(self):
        if self.overflow:
            self.overflow = False
            yield {'error': 'queue_overflow'}
        while True:
            try:
                yield self.queue.get_nowait()
            except Empty:
                return

    def close(self):
        self.stopping = True
        self.app.close()
        self.thread.join(timeout=2)
