from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from kalshi_lab.auth import kalshi_headers, load_kalshi_auth
from kalshi_lab.diagnostics import (MAX_BYTES, NoRedirect, ProbeFailure, ProbeHTTP,
                                    Response, index_points, run_diagnostic)
from kalshi_lab.markets import parse_time, select_active

NOW = datetime(2026, 10, 27, 10, 5, tzinfo=timezone.utc)


def market(*, settled=False):
    opened = NOW.replace(minute=0) - (timedelta(minutes=15) if settled else timedelta())
    return {'ticker': 'KXBTC15M-TEST-' + ('OLD' if settled else 'NEW'),
            'market_type': 'binary', 'notional_value_dollars': '1',
            'open_time': opened.isoformat(), 'close_time': (opened + timedelta(minutes=15)).isoformat(),
            'status': 'settled' if settled else 'active', 'result': 'yes' if settled else '',
            'floor_strike': '100000.00', 'strike_type': 'greater_or_equal',
            'rules_primary': 'SYNTHETIC RULE', 'rules_secondary': 'SYNTHETIC ROUNDING',
            'yes_bid_dollars': '0.60', 'yes_ask_dollars': '0.62',
            'yes_bid_size_fp': '10', 'yes_ask_size_fp': '20'}


def envelope(points, *, latest=False):
    return {'data': {'payload': {'latest_values': {'BRTI': points[0]}} if latest else points}}


class FakeHTTP:
    def __init__(self):
        self.calls, self.failures = [], {}

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path in self.failures:
            value = self.failures[path]
            if isinstance(value, Exception): raise value
            data = value
        elif path in ('/markets', '/historical/markets'):
            data = {'markets': [market(settled=params.get('status') == 'settled' or path.startswith('/historical'))], 'cursor': ''}
        elif path.endswith('/candlesticks'):
            data = {'candlesticks': [{'yes_bid': {'close_dollars': '0.60'}, 'yes_ask': {'close_dollars': '0.62'}}]}
        elif path.endswith('/latest_values'):
            data = envelope([{'time': int(NOW.timestamp() * 1000), 'value': '100100.00'}], latest=True)
        else:
            first = int((NOW - timedelta(hours=2)).replace(minute=0).timestamp() * 1000)
            data = envelope([{'time': first, 'value': '100000.01'}, {'time': first+1000, 'value': '100000.02'}])
        return Response(data, 'test-response-digest', NOW.isoformat())


class DiagnosticTests(unittest.TestCase):
    def test_success_writes_typed_samples_not_credentials_or_raw_headers(self):
        http = FakeHTTP()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'probe'
            result = run_diagnostic(path, http=http, now=NOW)
            self.assertEqual(result['status'], 'access_checks_passed')
            self.assertTrue(result['rule_review_required'])
            self.assertEqual(result['checks']['active_contract']['summary']['reference'], '100000.00')
            self.assertEqual(len(http.calls), 5)
            self.assertEqual(sorted(p.name for p in path.iterdir()), ['diagnostic.json', 'index_current.csv', 'index_history.csv'])
            self.assertIn('100000.01', (path / 'index_history.csv').read_text())
            self.assertNotIn('KALSHI-ACCESS', (path / 'diagnostic.json').read_text())
            with self.assertRaises(ValueError): run_diagnostic(path, http=http, now=NOW)

    def test_entitlement_denial_and_upstream_error_are_not_success(self):
        for failure in (ProbeFailure('http_error', 403), {'data': {'error': 'SECRET echoed upstream', 'payload': []}}, {'data': {'payload': []}}):
            http = FakeHTTP()
            http.failures['/cfbenchmarks/history/values'] = failure
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'probe'
                result = run_diagnostic(path, http=http, now=NOW)
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(result['checks']['historical_index']['status'], 'blocked')
                self.assertFalse((path / 'index_history.csv').exists())
                self.assertNotIn('SECRET', (path / 'diagnostic.json').read_text())

    def test_market_quotes_cannot_be_crossed_or_empty(self):
        for changes in ({'yes_ask_dollars': '0.50'}, {'yes_ask_size_fp': '0'}, {'floor_strike': None}):
            http = FakeHTTP()
            http.failures['/markets'] = {'markets': [{**market(), **changes}], 'cursor': ''}
            with tempfile.TemporaryDirectory() as tmp:
                result = run_diagnostic(Path(tmp) / 'probe', http=http, now=NOW)
                self.assertEqual(result['checks']['active_contract']['status'], 'blocked')

    def test_archived_market_fallback_is_bounded_and_uses_correct_candles(self):
        class Archived(FakeHTTP):
            def get(self, path, params=None):
                if path == '/markets' and params.get('status') == 'settled':
                    self.calls.append((path, params))
                    return Response({'markets': []}, 'digest', NOW.isoformat())
                return super().get(path, params)
        http = Archived()
        with tempfile.TemporaryDirectory() as tmp:
            result = run_diagnostic(Path(tmp) / 'probe', http=http, now=NOW)
        self.assertEqual(result['status'], 'access_checks_passed')
        self.assertLessEqual(len(http.calls), 7)
        self.assertTrue(any(p.startswith('/historical/markets/') and p.endswith('/candlesticks') for p, _ in http.calls))

    def test_current_index_must_be_fresh_at_receipt_not_at_run_start(self):
        class Delayed(FakeHTTP):
            def get(self, path, params=None):
                response = super().get(path, params)
                if path.endswith('/latest_values'):
                    later = NOW + timedelta(seconds=60)
                    response.data = envelope([{'time': int(later.timestamp()*1000), 'value': '100'}], latest=True)
                    response.received_at = later.isoformat()
                return response
        with tempfile.TemporaryDirectory() as tmp:
            result = run_diagnostic(Path(tmp) / 'probe', http=Delayed(), now=NOW)
        self.assertEqual(result['checks']['current_index']['status'], 'passed')
        http = FakeHTTP()
        http.failures['/cfbenchmarks/latest_values'] = envelope([{'time': int((NOW-timedelta(minutes=2)).timestamp()*1000), 'value': '100'}], latest=True)
        with tempfile.TemporaryDirectory() as tmp:
            result = run_diagnostic(Path(tmp) / 'probe', http=http, now=NOW)
        self.assertEqual(result['checks']['current_index']['reason'], 'stale_or_future_index_value')

    def test_index_order_flags_and_history_bounds(self):
        for points in ([{'time': 1, 'value': '100'}, {'time': 1, 'value': '101'}],
                       [{'time': True, 'value': '100'}], [{'time': 1, 'value': 'NaN'}],
                       [{'time': 1, 'value': '100', 'repeatOfPreviousValue': 'false'}]):
            with self.subTest(points=points), self.assertRaises((ProbeFailure, ValueError)):
                index_points(envelope(points))
        point = {'time': 10, 'value': '100', 'amendTime': 20, 'repeatOfPreviousValue': True}
        self.assertTrue(index_points(envelope([point]))[0]['repeat_of_previous_value'])
        http = FakeHTTP()
        http.failures['/cfbenchmarks/history/values'] = envelope([point])
        with tempfile.TemporaryDirectory() as tmp:
            result = run_diagnostic(Path(tmp) / 'probe', http=http, now=NOW)
        self.assertEqual(result['checks']['historical_index']['reason'], 'index_samples_outside_requested_hour')

    def test_invalid_arguments_fail_before_creating_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'probe'
            for timeout in [0, 31, float('nan'), True]:
                with self.assertRaises(ValueError): run_diagnostic(path, timeout=timeout, http=FakeHTTP())
            self.assertFalse(path.exists())


class TransportTests(unittest.TestCase):
    def test_local_key_loader_needs_no_streaming_dependency(self):
        import os
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
        key = ed25519.Ed25519PrivateKey.generate()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'local-private.key'
            path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
            with patch.dict(os.environ, {'KALSHI_API_KEY_ID': 'test-id', 'KALSHI_PRIVATE_KEY_PATH': str(path)}):
                signer = load_kalshi_auth()
                self.assertEqual(signer('/trade-api/v2/cfbenchmarks/latest_values')['KALSHI-ACCESS-KEY'], 'test-id')
            path.write_text('not a PEM key')
            with patch.dict(os.environ, {'KALSHI_API_KEY_ID': 'test-id', 'KALSHI_PRIVATE_KEY_PATH': str(path)}):
                with self.assertRaises(ValueError) as caught: load_kalshi_auth()
                self.assertNotIn(str(path), str(caught.exception))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'KALSHI_API_KEY_ID'): load_kalshi_auth()

    def test_market_selection_keeps_expiry_and_timezone_guards(self):
        valid = market()
        expired = market(settled=True)
        wrong_asset = {**valid, 'ticker': 'KXETH15M-OTHER'}
        wrong_payout = {**valid, 'notional_value_dollars': '2'}
        self.assertEqual(select_active([expired, wrong_asset, wrong_payout, valid], 'KXBTC15M', NOW), valid)
        with self.assertRaises(ValueError): select_active([valid], 'KXBTC15M', parse_time(valid['close_time']))
        with self.assertRaises(ValueError): parse_time('2026-10-27T10:00:00')

    def test_get_only_allowlist_and_query_free_signed_path(self):
        response = BytesIO(b'{"data":{"payload":[]}}')
        response.status = 200
        signer = Mock(return_value={'KALSHI-ACCESS-KEY': 'SECRET'})
        client = ProbeHTTP(signer)
        client.opener = Mock()
        client.opener.open.return_value = response
        client.get('/cfbenchmarks/history/values', {'id': 'BRTI', 'timespan': 'HOUR'})
        signer.assert_called_once_with('/trade-api/v2/cfbenchmarks/history/values')
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertIn('?id=BRTI', request.full_url)
        for path in ['/portfolio/balance', '/portfolio/orders', 'https://example.com', '/cfbenchmarks/latest_values?x=1']:
            with self.assertRaises(ValueError): client.get(path)
        with self.assertRaises(ProbeFailure): ProbeHTTP().get('/cfbenchmarks/latest_values')
        self.assertIsNone(NoRedirect().redirect_request(request, None, 302, 'found', {}, 'https://example.com'))

    def test_http_errors_closed_and_sensitive_response_text_not_exposed(self):
        body = BytesIO(b'SECRET request headers')
        client = ProbeHTTP()
        client.opener = Mock()
        client.opener.open.side_effect = HTTPError('https://example.com', 403, 'SECRET', {}, body)
        with self.assertRaises(ProbeFailure) as caught: client.get('/markets')
        self.assertEqual(caught.exception.status, 403)
        self.assertNotIn('SECRET', str(caught.exception))
        self.assertTrue(body.closed)
        client.opener.open.side_effect = URLError('SECRET')
        with self.assertRaises(ProbeFailure) as caught: client.get('/markets')
        self.assertEqual(str(caught.exception), 'network_error')

    def test_response_size_and_json_validation(self):
        for body, reason in [(b'x'*(MAX_BYTES+1), 'response_size_limit'), (b'[]', 'unexpected_schema'), (b'not json', 'invalid_json')]:
            response = BytesIO(body)
            response.status = 200
            client = ProbeHTTP()
            client.opener = Mock()
            client.opener.open.return_value = response
            with self.subTest(reason=reason), self.assertRaisesRegex(ProbeFailure, reason): client.get('/markets')
            self.assertTrue(response.closed)

    def test_rest_signature_verifies_with_both_key_types(self):
        import base64
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
        path = '/trade-api/v2/cfbenchmarks/history/values'
        for key in [ed25519.Ed25519PrivateKey.generate(), rsa.generate_private_key(public_exponent=65537, key_size=2048)]:
            headers = kalshi_headers('test-id', key, 123, path=path)
            signature = base64.b64decode(headers['KALSHI-ACCESS-SIGNATURE'])
            message = ('123GET'+path).encode()
            if isinstance(key, rsa.RSAPrivateKey):
                key.public_key().verify(signature, message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
            else:
                key.public_key().verify(signature, message)
        with self.assertRaises(ValueError): kalshi_headers('test-id', key, 123, path=path+'?id=BRTI')
