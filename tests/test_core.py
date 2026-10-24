from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from unittest.mock import patch
from urllib.error import HTTPError
from io import BytesIO, StringIO

from kalshi_lab.books import asks, bids, buy_cost, decimal, threshold_pair_quote
from kalshi_lab.client import Client
from kalshi_lab.discovery import candidates, choose_watchlist
from kalshi_lab.storage import Store
from kalshi_lab.__main__ import inspect as inspect_capture


def book(yes=(), no=()):
    return {"orderbook_fp": {"yes_dollars": list(yes), "no_dollars": list(no)}}


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.lower = book(no=[['0.5600', '20.00']])
        self.higher = book(yes=[['0.4800', '8.00'], ['0.4300', '30.00']])

    def test_yes_ask_uses_no_bid_and_keeps_fractional_size(self):
        self.assertEqual(asks(book(no=[['0.5678', '1.25']]), 'yes'), [(D('.4322'), D('1.25'))])

    def test_no_ask_uses_yes_bid(self):
        self.assertEqual(asks(self.higher, 'no')[0], (D('.52'), D('8')))

    def test_unsorted_duplicate_levels_do_not_change_execution(self):
        payload = book(no=[['.40', '3'], ['.60', '1.25'], ['.60', '2.75']])
        self.assertEqual(buy_cost(payload, 'yes', '5')['cost_dollars'], D('2.20'))

    def test_empty_side_is_not_free_liquidity(self):
        with self.assertRaisesRegex(ValueError, 'Insufficient depth'):
            buy_cost(book(), 'yes', '1')

    def test_no_partial_fill_is_reported_as_full_execution(self):
        with self.assertRaisesRegex(ValueError, 'Insufficient depth'):
            buy_cost(self.lower, 'yes', '21')

    def test_tiny_subcent_fractional_cost_is_exact(self):
        result = buy_cost(book(no=[['.9999', '.01']]), 'yes', '.01')
        self.assertEqual(result['cost_dollars'], D('.000001'))

    def test_visible_pair_gross_payoff(self):
        result = threshold_pair_quote(self.lower, self.higher, '8')
        self.assertEqual(result['quoted_cost_dollars'], D('7.68'))
        self.assertEqual(result['gross_surplus_dollars'], D('.32'))
        self.assertIsNone(result['surplus_after_assumed_reserve_dollars'])

    def test_depth_reduces_total_surplus(self):
        result = threshold_pair_quote(self.lower, self.higher, '20')
        self.assertEqual(result['quoted_cost_dollars'], D('19.80'))
        self.assertEqual(result['gross_surplus_dollars'], D('.20'))
        # Beyond the first 8, each pair costs 1.01: incremental loss .01.
        self.assertEqual(result['gross_surplus_dollars'] - D('.32'), D('-.12'))

    def test_fee_reserve_can_reverse_a_positive_gross_result(self):
        result = threshold_pair_quote(self.lower, self.higher, '8', '.40')
        self.assertEqual(result['surplus_after_assumed_reserve_dollars'], D('-.08'))

    def test_invalid_numeric_and_unit_inputs_fail(self):
        for value in ['NaN', 'Infinity', 0.1, True, None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                decimal(value)
        for payload in [book(yes=[['1.1', '1']]), book(no=[['.4', '-1']]), {'orderbook': {'yes': [[50, 1]]}}]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                bids(payload, 'yes') if 'orderbook_fp' not in payload or payload['orderbook_fp']['yes_dollars'] else bids(payload, 'no')

    def test_invalid_quantity_side_or_fee_fails(self):
        for q in ['0', '-1']:
            with self.assertRaises(ValueError):
                buy_cost(self.lower, 'yes', q)
        with self.assertRaises(ValueError):
            asks(self.lower, 'maybe')
        with self.assertRaises(ValueError):
            threshold_pair_quote(self.lower, self.higher, '8', '-.01')

    def test_nonobject_orderbooks_fail_with_a_schema_error(self):
        for payload in [None, [], 'unexpected']:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                bids(payload, 'yes')


class DiscoveryTests(unittest.TestCase):
    def market(self, ticker, strike, event='E', kind='greater'):
        return {'ticker': ticker, 'event_ticker': event, 'market_type': 'binary',
                'strike_type': kind, 'floor_strike': strike, 'volume_fp': '10.25',
                'yes_bid_dollars': '.40', 'yes_ask_dollars': '.60'}

    def test_candidates_do_not_cross_events_or_approve_themselves(self):
        result = candidates([self.market('A', '.1'), self.market('B', '.2'),
                             self.market('C', '.3', 'OTHER')])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['lower_ticker'], 'A')
        self.assertEqual(result[0]['review_status'], 'unreviewed')
        self.assertFalse(any(result[0]['rule_checks'].values()))

    def test_interval_markets_are_not_assumed_nested(self):
        self.assertEqual(candidates([self.market('A', '.1'), self.market('B', '.2', kind='between')]), [])

    def test_equal_thresholds_are_not_assumed_strictly_nested(self):
        self.assertEqual(candidates([self.market('A', '.1'), self.market('B', '.10')]), [])

    def test_watchlist_selects_one_event(self):
        watch = choose_watchlist([self.market('B', '.2'), self.market('A', '.1'),
                                  self.market('C', '.3', 'OTHER')])
        self.assertEqual(watch['tickers'], ['A', 'B'])
        self.assertFalse(watch['relations_reviewed'])


class CaptureTests(unittest.TestCase):
    def test_quality_report_counts_bad_schema_without_carrying_it_as_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'capture.sqlite'
            store = Store(path)
            common = dict(session='pilot', sweep=1, kind='orderbook', ticker='TEST',
                          url='https://example.com/book', duration_ms=10,
                          status=200, headers={}, error=None)
            store.append(**common, requested_at='2026-10-24T00:00:00+00:00',
                         received_at='2026-10-24T00:00:01+00:00',
                         body=json.dumps(book(no=[['.60', '2']])).encode())
            store.append(**common, requested_at='2026-10-24T00:00:02+00:00',
                         received_at='2026-10-24T00:00:03+00:00', body=b'null')
            store.close()
            output = StringIO()
            with redirect_stdout(output):
                inspect_capture(Namespace(db=str(path), out=None))
            report = json.loads(output.getvalue())['markets']['TEST']
            self.assertEqual(report['valid_books'], 1)
            self.assertEqual(report['invalid_or_failed'], 1)
            self.assertEqual(report['last_received_at'], '2026-10-24T00:00:01+00:00')
            self.assertEqual(report['latest_yes_ask'], '0.40')

    def test_raw_bytes_preserved_and_bad_json_is_retained(self):
        class Response:
            status = 200
            headers = {'Date': 'test'}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{bad-json}'
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'capture.sqlite')
            with patch('kalshi_lab.client.urlopen', return_value=Response()):
                with self.assertRaises(ValueError):
                    Client(store).get('/markets')
            body, digest = store.db.execute('SELECT body,sha256 FROM responses').fetchone()
            self.assertEqual(body, b'{bad-json}')
            self.assertEqual(digest, hashlib.sha256(body).hexdigest())
            store.close()

    def test_numeric_json_decimal_is_not_rounded_to_binary_float(self):
        class Response:
            status = 200
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"floor_strike": 0.10000000000000001}'
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'capture.sqlite')
            with patch('kalshi_lab.client.urlopen', return_value=Response()):
                result = Client(store).get('/markets')
            self.assertEqual(result['floor_strike'], '0.10000000000000001')
            store.close()

    def test_http_failure_is_retained_and_not_retried_for_403(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'capture.sqlite')
            failure = HTTPError('test', 403, 'Forbidden', {}, BytesIO(b'blocked'))
            with patch('kalshi_lab.client.urlopen', side_effect=failure) as call:
                with self.assertRaises(RuntimeError):
                    Client(store).get('/markets')
            self.assertEqual(call.call_count, 1)
            self.assertEqual(store.db.execute('SELECT status,body FROM responses').fetchone(), (403, b'blocked'))
            store.close()


if __name__ == '__main__':
    unittest.main()
