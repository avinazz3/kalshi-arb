import asyncio
import base64
from contextlib import closing
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
from queue import Full
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from kalshi_lab.capture import kalshi_headers, run_capture, specifications
from kalshi_lab.client import Client
from kalshi_lab.demo import create_demo
from kalshi_lab.discovery import ASSETS, discover, select_active, validate_universe
from kalshi_lab.storage import Store
from kalshi_lab.stream_store import StreamWriter, inspect_stream


def market(ticker='KXBTC15M-TEST', **changes):
    result = {'ticker': ticker, 'event_ticker': 'KXBTC15M-EVENT', 'market_type': 'binary',
              'notional_value_dollars': '1.0000', 'status': 'active', 'exchange_index': 0,
              'open_time': '2020-01-01T00:00:00Z', 'close_time': '2099-01-01T00:00:00Z'}
    return {**result, **changes}


def universe():
    return {'purpose': 'crypto_order_flow_research', 'assets': [{'asset': 'BTC',
            'coinbase_product': 'BTC-USD', 'series': {'ticker': 'KXBTC15M'}, 'market': market()}]}


class DiscoveryTests(unittest.TestCase):
    def test_selects_active_matching_binary_and_rejects_expiry_boundary(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = [market('KXETH15M-OTHER'), market('KXBTC15M-SCALAR', market_type='scalar'),
                market('KXBTC15M-EXPIRED', close_time='2026-01-01T00:00:00Z'), market()]
        self.assertEqual(select_active(rows, 'KXBTC15M', now)['ticker'], 'KXBTC15M-TEST')
        with self.assertRaises(ValueError):
            select_active(rows[:-1], 'KXBTC15M', now)

    def test_wrong_product_duplicate_asset_and_nonbinary_manifest_rejected(self):
        for mutate in [lambda d: d['assets'][0].update(coinbase_product='ETH-USD'),
                       lambda d: d['assets'].append(d['assets'][0]),
                       lambda d: d['assets'][0]['market'].update(market_type='scalar')]:
            d = universe(); mutate(d)
            with self.assertRaises(ValueError): validate_universe(d)

    def test_discovery_archives_explicit_series_and_event(self):
        class FakeClient:
            session = 'metadata'
            def get(self, path, params=None, **kwargs):
                if path.startswith('/series/'): return {'series': {'ticker': 'KXBTC15M'}}
                if path == '/markets':
                    self.assertion = params
                    return {'markets': [market()], 'cursor': ''}
                return {'event': {'event_ticker': 'KXBTC15M-EVENT'}}
        client = FakeClient()
        result = discover(client, ['BTC'])
        self.assertEqual(result['assets'][0]['coinbase_product'], 'BTC-USD')
        self.assertEqual(client.assertion['mve_filter'], 'exclude')
        self.assertEqual(validate_universe(result)[0]['asset'], 'BTC')

    def test_crypto_shard_is_preserved_and_not_filtered_as_shard_zero(self):
        d = universe(); d['assets'][0]['market']['exchange_index'] = 2
        self.assertEqual(validate_universe(d)[0]['market']['exchange_index'], 2)
        self.assertEqual(select_active([d['assets'][0]['market']], 'KXBTC15M', datetime(2026,1,1,tzinfo=timezone.utc))['exchange_index'],2)

    def test_incomplete_discovery_is_not_accepted(self):
        class FakeClient:
            def get(self, path, params=None, **kwargs):
                return {'series': {'ticker': 'KXBTC15M'}} if path.startswith('/series/') else {'markets': [], 'cursor': 'more'}
        with self.assertRaisesRegex(ValueError, 'pagination incomplete'):
            discover(FakeClient(), ['BTC'])


class RawStorageTests(unittest.TestCase):
    def test_synthetic_fixture_integrity_and_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'demo.sqlite'
            result = create_demo(path)
            self.assertTrue(result['synthetic'])
            report = result['quality']
            self.assertEqual(report['body_hash_failures'], 0)
            self.assertEqual(report['sequence_discontinuities'], 0)
            self.assertTrue(report['sessions']['synthetic-demo']['both_book_snapshot_types_present'])
            self.assertEqual(report['sessions']['synthetic-demo']['session_status'], 'synthetic_completed')
            self.assertEqual(report['sessions']['synthetic-demo']['venue_first_frame_wall_ns']['coinbase'],1_001_000_000)
            with self.assertRaises(ValueError): create_demo(path)

    def test_tampered_body_bad_json_and_sequence_gaps_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'frames.sqlite'
            writer = StreamWriter(path, 's', {})
            for i, seq in enumerate([1, 3]):
                writer.record('kalshi', 'c', 'frame', json.dumps({'type': 'orderbook_delta', 'sid': 1, 'seq': seq}),
                              mono_ns=10+i, wall_ns=100+i)
            writer.record('coinbase', 'd', 'frame', 'null', mono_ns=12, wall_ns=102)
            writer.close()
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("UPDATE stream_events SET body=? WHERE id=1", (b'{"type":"orderbook_delta","sid":1,"seq":1,"tampered":true}',))
            report = inspect_stream(path)
            self.assertEqual(report['body_hash_failures'], 1)
            self.assertEqual(report['sequence_discontinuities'], 1)
            self.assertEqual(report['malformed_frames'], 1)

    def test_reconnections_and_distinct_subscriptions_have_separate_sequences(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'frames.sqlite'
            writer = StreamWriter(path, 's', {})
            for i, (connection, sid, seq) in enumerate([('a', 1, 10), ('a', 2, 1), ('a', 1, 11), ('b', 1, 1)]):
                writer.record('kalshi', connection, 'frame', json.dumps({'sid': sid, 'seq': seq}), mono_ns=i, wall_ns=i)
            writer.close()
            self.assertEqual(inspect_stream(path)['sequence_discontinuities'], 0)

    def test_coinbase_sequence_is_shared_across_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'frames.sqlite'; writer = StreamWriter(path, 's', {})
            for i, channel in enumerate(['subscriptions', 'l2_data', 'heartbeats', 'market_trades', 'l2_data']):
                writer.record('coinbase', 'c', 'frame', json.dumps({'channel': channel, 'sequence_num': i}), mono_ns=i, wall_ns=i)
            writer.close()
            self.assertEqual(inspect_stream(path)['sequence_discontinuities'], 0)

    def test_full_queue_aborts_instead_of_dropping(self):
        writer = StreamWriter.__new__(StreamWriter)
        writer.session, writer.error, writer.queue = 's', None, Mock()
        writer.queue.put_nowait.side_effect = Full
        with self.assertRaisesRegex(RuntimeError, 'silently dropping'):
            writer.record('coinbase', 'c', 'frame', '{}')

    def test_duplicate_session_cannot_overwrite_existing_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'frames.sqlite'
            StreamWriter(path, 's', {}).close()
            with self.assertRaises(RuntimeError): StreamWriter(path, 's', {})

    def test_receipt_values_and_raw_bytes_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'frames.sqlite'
            writer = StreamWriter(path, 's', {})
            raw = b'{"value":0.10000000000000001}'
            writer.record('coinbase', 'c', 'frame', raw, mono_ns=11, wall_ns=22)
            writer.close()
            with closing(sqlite3.connect(path)) as db, db:
                row = db.execute('SELECT received_mono_ns,received_wall_ns,body,sha256 FROM stream_events').fetchone()
            self.assertEqual(row, (11, 22, raw, hashlib.sha256(raw).hexdigest()))

    def test_rest_http_failure_and_numeric_precision_are_retained(self):
        class Response:
            status, headers = 200, {}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"value":0.10000000000000001}'
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'metadata.sqlite')
            with patch('kalshi_lab.client.urlopen', return_value=Response()):
                self.assertEqual(Client(store).get('/markets')['value'], '0.10000000000000001')
            with patch('kalshi_lab.client.urlopen', side_effect=HTTPError('test',403,'Forbidden',{},BytesIO(b'blocked'))) as call:
                with self.assertRaises(RuntimeError): Client(store).get('/markets')
                self.assertEqual(call.call_count, 1)
            self.assertEqual(store.db.execute('SELECT status,body FROM responses ORDER BY id DESC LIMIT 1').fetchone(), (403,b'blocked'))
            store.close()


class CaptureTests(unittest.TestCase):
    def test_subscriptions_are_only_public_market_data(self):
        specs = specifications(['BTC', 'ETH'], ['KXBTC15M-X', 'KXETH15M-X'])
        self.assertEqual([s['channel'] for s in specs[0]['subscriptions']], ['heartbeats','level2','market_trades'])
        self.assertEqual(specs[1]['subscriptions'][0]['params']['channels'], ['orderbook_delta'])
        self.assertNotIn('jwt', json.dumps(specs))

    def test_missing_credentials_fail_before_creating_capture(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            path = Path(tmp) / 'frames.sqlite'
            with self.assertRaisesRegex(ValueError, 'KALSHI_API_KEY_ID'):
                asyncio.run(run_capture(path, 1, universe=universe()))
            self.assertFalse(path.exists())

    def test_stale_manifest_rejected(self):
        doc = universe(); doc['assets'][0]['market']['close_time'] = '2021-01-01T00:00:00Z'
        with self.assertRaisesRegex(ValueError, 'expired'):
            asyncio.run(run_capture('unused.sqlite', 1, universe=doc))

    def test_rsa_and_ed25519_sign_expected_path(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import rsa, padding, ed25519
        message = b'123GET/trade-api/ws/v2'
        for key in [rsa.generate_private_key(public_exponent=65537,key_size=2048), ed25519.Ed25519PrivateKey.generate()]:
            headers = kalshi_headers('key-id', key, 123)
            signature = base64.b64decode(headers['KALSHI-ACCESS-SIGNATURE'])
            if isinstance(key, rsa.RSAPrivateKey):
                key.public_key().verify(signature,message,padding.PSS(mgf=padding.MGF1(hashes.SHA256()),salt_length=padding.PSS.DIGEST_LENGTH),hashes.SHA256())
            else: key.public_key().verify(signature,message)

    def test_joint_capture_with_fake_sockets_keeps_headers_out_of_log(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
        class Socket:
            def __init__(self, url): self.url, self.first = url, True
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def send(self, raw): pass
            def __aiter__(self): return self
            async def __anext__(self):
                if self.first:
                    self.first = False
                    return json.dumps({'type':'orderbook_snapshot','sid':1,'seq':1,'msg':{'market_ticker':'KXBTC15M-TEST'}}) if 'kalshi' in self.url else json.dumps({'channel':'l2_data','sequence_num':0,'events':[{'type':'snapshot','product_id':'BTC-USD'}]})
                await asyncio.Event().wait()
        with tempfile.TemporaryDirectory() as tmp:
            key = ed25519.Ed25519PrivateKey.generate()
            key_path = Path(tmp) / 'private.key'
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
            path = Path(tmp) / 'frames.sqlite'
            with patch.dict(os.environ, {'KALSHI_API_KEY_ID':'SECRET-KEY-ID','KALSHI_PRIVATE_KEY_PATH':str(key_path)}), patch('websockets.asyncio.client.connect',side_effect=lambda url,**kwargs:Socket(url)):
                result = asyncio.run(run_capture(path,1,universe=universe()))
            self.assertTrue(result['quality']['sessions'][result['session']]['both_book_snapshot_types_present'])
            self.assertEqual(result['quality']['sessions'][result['session']]['session_status'], 'completed')
            self.assertNotIn(b'SECRET-KEY-ID',path.read_bytes())
            self.assertNotIn(b'PRIVATE KEY',path.read_bytes())

    def test_demo_cli_runs_without_network(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable,'-m','kalshi_lab','demo','--db',str(Path(tmp)/'demo.sqlite')],cwd=root,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertTrue(json.loads(result.stdout)['synthetic'])


if __name__ == '__main__': unittest.main()
