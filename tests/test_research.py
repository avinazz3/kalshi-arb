from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from kalshi_lab.books import Book, Quote, top_of_book_ofi
from kalshi_lab.dataset import Health, Rejected, build_dataset, continuous, export_dataset
from kalshi_lab.replay import Replay, Timeline, load_replay
from kalshi_lab.research_demo import create_research_demo
from kalshi_lab.stream_store import StreamWriter


def ks(ticker="KXBTC15M-SYNTHETIC", **changes):
    return {"market_ticker": ticker, "yes_dollars_fp": [["0.49", "10"]],
            "no_dollars_fp": [["0.50", "12"], ["0.49", "8"]], **changes}


def cs(kind="snapshot", product="BTC-USD", updates=None):
    return {"channel": "l2_data", "events": [{"type": kind, "product_id": product,
        "updates": updates if updates is not None else [
            {"side": "bid", "price_level": "100", "new_quantity": "1"},
            {"side": "offer", "price_level": "101", "new_quantity": "2"}]}]}


class Harness:
    def __init__(self):
        self.replay, self.index = Replay("test", {}), 0

    def send(self, venue, frame=None, *, connection="a", kind="frame", ns=None):
        self.index += 1
        timestamp = self.index if ns is None else ns
        row = {"venue": venue, "connection": connection, "kind": kind, "id": self.index,
               "received_mono_ns": timestamp, "received_wall_ns": timestamp,
               "body": json.dumps(frame or {}).encode()}
        self.replay.consume(row)
        return self.replay

    def k(self, seq=1, sid=1, message=None, snapshot=True, **kwargs):
        return self.send("kalshi", {"type": "orderbook_snapshot" if snapshot else "orderbook_delta",
            "sid": sid, "seq": seq, "msg": message or ks()}, **kwargs)

    def c(self, seq=0, frame=None, **kwargs):
        return self.send("coinbase", {**(frame or cs()), "sequence_num": seq}, **kwargs)


class BookTests(unittest.TestCase):
    def test_kalshi_complement_depth_additive_delta_and_replacement(self):
        book = Book(kalshi=True); book.snapshot_kalshi(ks())
        self.assertEqual(book.quote(), Quote(D('.49'), D('.50'), D(10), D(12)))
        book.delta_kalshi({"side": "no", "price_dollars": ".50", "delta_fp": "-12"})
        self.assertEqual(book.quote().ask, D('.51'))
        self.assertEqual(book.quote().ask_size, D(8))
        book.delta_kalshi({"side": "yes", "price_dollars": ".49", "delta_fp": "2.25"})
        self.assertEqual(book.quote().bid_size, D('12.25'))
        book.snapshot_kalshi(ks(yes_dollars_fp=[[".40", "2"]]))
        self.assertEqual(book.quote().bid, D('.40'))
        self.assertEqual(len(book.bids), 1)

    def test_coinbase_absolute_quantity_delete_and_best_level(self):
        book = Book(); book.update_coinbase(cs()['events'][0]['updates'])
        book.update_coinbase([{"side": "bid", "price_level": "100", "new_quantity": "3"},
                             {"side": "bid", "price_level": "99", "new_quantity": "5"}])
        self.assertEqual(book.quote().bid_size, D(3))
        book.update_coinbase([{"side": "bid", "price_level": "100", "new_quantity": "0"}])
        self.assertEqual(book.quote().bid, D(99))

    def test_invalid_fixed_point_negative_quantity_empty_and_crossed(self):
        for message in [ks(yes_dollars_fp=[[".12345", "1"]]), ks(no_dollars_fp=[["1.01", "2"]]),
                        ks(yes_dollars_fp=[[".49", "1.001"]]), ks(no_dollars_fp=[[".5", "-1"]]),
                        ks(yes_dollars_fp=[[".49", "1"], [".49", "2"]])]:
            with self.assertRaises(ValueError): Book(kalshi=True).snapshot_kalshi(message)
        book = Book(kalshi=True); book.snapshot_kalshi(ks())
        with self.assertRaises(ValueError):
            book.delta_kalshi({"side": "yes", "price_dollars": ".49", "delta_fp": "-11"})
        book.snapshot_kalshi(ks(yes_dollars_fp=[])); self.assertIsNone(book.quote())
        book.snapshot_kalshi(ks(yes_dollars_fp=[[".60", "1"]])); self.assertIsNone(book.quote())

    def test_message_ofi_same_price_and_both_price_directions(self):
        q = Quote(D(100), D(101), D(2), D(3))
        self.assertEqual(top_of_book_ofi(q, Quote(D(100), D(101), D(5), D(1))), D(5))
        self.assertEqual(top_of_book_ofi(q, Quote(D(101), D(102), D(4), D(6))), D(7))
        self.assertEqual(top_of_book_ofi(q, Quote(D(99), D(100), D(4), D(6))), D(-8))


class ReconstructionTests(unittest.TestCase):
    def test_shared_subscription_gap_invalidates_all_markets_individual_snapshots_recover(self):
        h = Harness(); h.k(); h.k(seq=2, message=ks("OTHER"))
        r = h.k(seq=4, snapshot=False, message={"market_ticker": "OTHER", "side": "yes",
                                                 "price_dollars": ".49", "delta_fp": "1"})
        self.assertFalse(r.books)
        self.assertEqual(r.diagnostics['sequence_discontinuities'], 1)
        h.k(seq=5)
        self.assertIn(('kalshi', 'KXBTC15M-SYNTHETIC'), r.books)
        self.assertNotIn(('kalshi', 'OTHER'), r.books)

    def test_separate_subscriptions_and_new_connection_cannot_reuse_state(self):
        h = Harness(); h.k(sid=1); h.k(sid=2, message=ks('OTHER'))
        r = h.k(seq=2, sid=1)
        self.assertEqual(r.diagnostics['sequence_discontinuities'], 0)
        h.k(seq=1, connection='b', snapshot=False, message={"market_ticker": 'OTHER'})
        self.assertFalse(r.books)
        h.k(seq=2, connection='b')
        self.assertEqual(len(r.books), 1)

    def test_duplicate_and_backward_deltas_quarantine_without_double_counting(self):
        h = Harness(); h.k()
        delta = {"market_ticker": "KXBTC15M-SYNTHETIC", "side": "yes", "price_dollars": ".49", "delta_fp": "1"}
        r = h.k(seq=2, snapshot=False, message=delta)
        self.assertEqual(r.books[('kalshi', 'KXBTC15M-SYNTHETIC')].quote().bid_size, D(11))
        h.k(seq=2, snapshot=False, message=delta)
        self.assertFalse(r.books)
        h.k(seq=3, snapshot=False, message=delta)
        self.assertFalse(r.books)

    def test_coinbase_sequence_spans_heartbeats_and_gap_requires_snapshot(self):
        h = Harness(); h.c()
        r = h.c(seq=1, frame={"channel": "heartbeats", "events": []})
        h.c(seq=2, frame=cs('update'))
        self.assertEqual(r.diagnostics['sequence_discontinuities'], 0)
        h.c(seq=4, frame={"channel": "heartbeats", "events": []})
        h.c(seq=5, frame=cs('update'))
        self.assertFalse(r.books)
        h.c(seq=6)
        self.assertIn(('coinbase', 'BTC-USD'), r.books)

    def test_missing_snapshot_empty_crossed_and_malformed_frames_are_quarantined(self):
        h = Harness(); r = h.c(frame=cs('update'))
        self.assertFalse(r.books)
        h.c(seq=1)
        h.c(seq=2, frame=cs('update', updates=[{"side": "bid", "price_level": "102", "new_quantity": "1"}]))
        self.assertFalse(r.books)
        h.c(seq=3); h.c(seq=4, frame={"channel": "l2_data", "events": [{"type": "update"}]})
        self.assertFalse(r.books)
        self.assertEqual(r.diagnostics['malformed_frames'], 1)

    def test_historical_trades_deduplication_maker_side_and_conflicts(self):
        h = Harness()
        trade = {"product_id": "BTC-USD", "trade_id": "1", "price": "100", "size": ".25", "side": "SELL"}
        def frame(kind, trades): return {"channel": "market_trades", "events": [{"type": kind, "trades": trades}]}
        r = h.c(frame=frame('snapshot', [trade]))
        self.assertFalse(r.trades)
        h.c(seq=1, frame=frame('update', [trade, {**trade, 'trade_id': '2'}]))
        self.assertEqual(r.trades['BTC-USD'].values, [(D(25), D(25))])
        h.c(seq=2, frame=frame('update', [{**trade, 'trade_id': '3', 'side': 'BUY'}]))
        self.assertEqual(r.trades['BTC-USD'].values[-1], (D(-25), D(25)))
        h.c(seq=3, frame=frame('update', [{**trade, 'price': '99'}]))
        self.assertEqual(r.diagnostics['malformed_frames'], 1)
        self.assertFalse(r.flow['BTC-USD'].values[-1][1])

    def test_disconnect_and_gap_recovery_inside_interval_cannot_hide_reset(self):
        h = Harness(); r = h.k(ns=10)
        h.send('kalshi', kind='disconnected', ns=15)
        h.k(seq=1, connection='b', ns=20)
        with self.assertRaisesRegex(Rejected, 'book_reset'):
            continuous(r.points[('kalshi', 'KXBTC15M-SYNTHETIC')], 10, 20)

    def test_subscription_removal_invalidates_book_and_trade_coverage(self):
        h = Harness(); r = h.c()
        h.c(seq=1, frame={'channel': 'subscriptions', 'events': [{'subscriptions': {
            'level2': ['BTC-USD'], 'market_trades': ['BTC-USD']}}]})
        h.c(seq=2, frame={'channel': 'subscriptions', 'events': [{'subscriptions': {'heartbeats': ['heartbeats']}}]})
        self.assertFalse(r.books)
        self.assertFalse(r.flow['BTC-USD'].values[-1][1])

    def test_quiet_book_is_valid_with_other_frame_activity_but_silence_is_bounded(self):
        timeline = Timeline()
        for timestamp in [0, 2, 4, 6, 20, 22]: timeline.add(timestamp, None)
        health = Health(timeline, 3)
        health.check(1, 7)
        with self.assertRaises(Rejected): health.check(1, 10)
        with self.assertRaises(Rejected): health.check(1, 22)


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'research.sqlite'
        create_research_demo(self.path)

    def mutate(self, transform):
        with sqlite3.connect(self.path) as db:
            rows = list(db.execute("SELECT id,venue,received_mono_ns,body FROM stream_events WHERE kind='frame' ORDER BY id"))
            for index, venue, timestamp, body in rows:
                value = transform(venue, timestamp, json.loads(body))
                if value is not None:
                    payload = json.dumps(value).encode()
                    db.execute('UPDATE stream_events SET body=?,sha256=? WHERE id=?',
                               (payload, hashlib.sha256(payload).hexdigest(), index))

    def test_hand_calculated_features_labels_and_available_time(self):
        rows, manifest = build_dataset(self.path)
        self.assertEqual(manifest['candidate_rows'], 8)
        self.assertEqual(manifest['accepted_rows'], 7)
        one, two = rows[:2]
        self.assertEqual(D(one['features']['signed_aggressive_notional_usd']), 0)
        self.assertEqual(D(two['features']['signed_aggressive_notional_usd']), D('25.01'))
        self.assertEqual(D(two['features']['coinbase_message_ofi']), 2)
        self.assertEqual(D(two['targets']['midpoint_change_1000ms']), D('.005'))
        self.assertEqual(D(two['features']['coinbase_queue_imbalance']), D('.2'))
        self.assertEqual(D(two['features']['kalshi_midpoint']), D('.495'))
        for row in rows:
            self.assertLessEqual(row['features_available_through_ns'], row['decision_mono_ns'])
            self.assertTrue(all(t > row['decision_mono_ns'] for t in row['label_end_mono_ns'].values()))

    def test_future_frames_and_venue_source_times_cannot_leak_into_features(self):
        before, _ = build_dataset(self.path)
        def change(venue, timestamp, frame):
            frame['timestamp'] = '1900-01-01T00:00:00Z'  # Deliberately misleading source clock.
            if venue == 'coinbase' and timestamp == 2_500_000_000 and frame.get('channel') == 'market_trades':
                frame['events'][0]['trades'][0]['size'] = '1000'
            return frame
        self.mutate(change)
        after, _ = build_dataset(self.path)
        self.assertEqual(before[0]['features'], after[0]['features'])
        self.assertNotEqual(before[1]['features']['signed_aggressive_notional_usd'],
                            after[1]['features']['signed_aggressive_notional_usd'])
        self.assertEqual(before[0]['targets'], after[0]['targets'])

    def test_snapshot_recovery_does_not_validate_rows_crossing_a_gap(self):
        def change(venue, timestamp, frame):
            if venue == 'kalshi' and timestamp >= 3_500_000_000:
                frame['seq'] += 97
                if timestamp == 3_500_000_000:
                    frame['type'] = 'orderbook_snapshot'
                    frame['msg'] = ks(no_dollars_fp=[[".49", "8"]])
            return frame
        self.mutate(change)
        rows, manifest = build_dataset(self.path, horizons_ms=(1000,))
        times = {row['decision_mono_ns'] for row in rows}
        self.assertNotIn(3_000_000_000, times)  # t=2s: reset occurs in the future label interval.
        self.assertNotIn(4_000_000_000, times)  # t=3s: reset occurs in the feature lookback.
        self.assertIn(5_000_000_000, times)
        self.assertEqual(manifest['replay_diagnostics']['sequence_discontinuities'], 1)

    def test_changing_future_quotes_changes_labels_without_changing_features(self):
        before, _ = build_dataset(self.path)
        def change(venue, timestamp, frame):
            if venue == 'kalshi' and timestamp == 3_500_000_000:
                frame['msg'].update(side='yes', price_dollars='.4950', delta_fp='1.00')
            return frame
        self.mutate(change)
        after, _ = build_dataset(self.path)
        self.assertEqual(before[1]['features'], after[1]['features'])
        self.assertNotEqual(before[1]['targets'], after[1]['targets'])

    def test_exact_expiry_boundary_is_rejected(self):
        with sqlite3.connect(self.path) as db:
            config = json.loads(db.execute('SELECT configuration_json FROM sessions').fetchone()[0])
            config['universe']['assets'][0]['market']['close_time'] = '2026-01-01T00:00:07Z'
            db.execute('UPDATE sessions SET configuration_json=?', (json.dumps(config),))
        rows, manifest = build_dataset(self.path)
        self.assertEqual(len(rows), 1)  # t=1 + 5 < 7; t=2 + 5 == 7 is excluded.
        self.assertGreater(manifest['rejected_rows']['outside_contract_window'], 0)

    def test_tampering_clock_backwards_unfinished_and_wall_jump_fail_closed(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE stream_events SET body=? WHERE id=1", (b'{}',))
        with self.assertRaisesRegex(ValueError, 'hash failure'): load_replay(self.path)
        self.path.unlink(); create_research_demo(self.path)
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE stream_events SET received_mono_ns=0 WHERE id=3')
        with self.assertRaisesRegex(ValueError, 'backwards'): load_replay(self.path)
        self.path.unlink(); create_research_demo(self.path)
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM stream_events WHERE kind='finished'")
        with self.assertRaisesRegex(ValueError, 'finish marker'): build_dataset(self.path)
        self.path.unlink(); create_research_demo(self.path)
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE stream_events SET received_wall_ns=received_wall_ns+1000000000 WHERE id=3')
        with self.assertRaisesRegex(ValueError, '250ms'): build_dataset(self.path)

    def test_multiple_sessions_require_explicit_selection_and_never_mix_clocks(self):
        StreamWriter(self.path, 'another', {}).close()
        with self.assertRaisesRegex(ValueError, 'Select --session'): build_dataset(self.path)
        rows, _ = build_dataset(self.path, session='synthetic-research')
        self.assertEqual(len(rows), 7)
        self.assertTrue(all(r['session'] == 'synthetic-research' for r in rows))

    def test_btc_and_eth_books_sequences_and_flow_remain_separate(self):
        dual = Path(self.tmp.name) / 'dual.sqlite'
        with sqlite3.connect(self.path) as db:
            config = json.loads(db.execute('SELECT configuration_json FROM sessions').fetchone()[0])
            eth = json.loads(json.dumps(config['universe']['assets'][0]))
            eth.update(asset='ETH', coinbase_product='ETH-USD', series={'ticker': 'KXETH15M'})
            eth['market']['ticker'] = 'KXETH15M-SYNTHETIC'
            config['universe']['assets'].append(eth)
            writer = StreamWriter(dual, 'dual', config)
            cb_seq = 0
            try:
                for venue, kind, wall, mono, body in db.execute(
                        'SELECT venue,kind,received_wall_ns,received_mono_ns,body FROM stream_events ORDER BY id'):
                    if kind != 'frame':
                        writer.record(venue, 'c', kind, body, wall_ns=wall, mono_ns=mono)
                        continue
                    for asset in ('BTC', 'ETH'):
                        frame = json.loads(body.decode().replace('BTC', asset))
                        if venue == 'kalshi':
                            frame['sid'] = 1 if asset == 'BTC' else 2
                        else:
                            frame['sequence_num'] = cb_seq; cb_seq += 1
                            if frame['channel'] == 'subscriptions':
                                frame['events'][0]['subscriptions'] = {
                                    'level2': ['BTC-USD', 'ETH-USD'], 'market_trades': ['BTC-USD', 'ETH-USD']}
                            if asset == 'ETH' and frame['channel'] == 'market_trades':
                                for trade in frame['events'][0]['trades']: trade['price'] = '10'
                        writer.record(venue, venue + '-c', kind, json.dumps(frame), wall_ns=wall, mono_ns=mono)
            finally:
                writer.close()
        rows, manifest = build_dataset(dual)
        self.assertEqual(len(rows), 14)
        two = {r['asset']: r for r in rows if r['decision_mono_ns'] == 3_000_000_000}
        self.assertEqual(D(two['BTC']['features']['signed_aggressive_notional_usd']), D('25.01'))
        self.assertEqual(D(two['ETH']['features']['signed_aggressive_notional_usd']), D('2.5'))
        self.assertNotIn('sequence_discontinuities', manifest['replay_diagnostics'])

    def test_missing_flow_ack_or_snapshot_is_unknown_rather_than_zero(self):
        def change(venue, timestamp, frame):
            if frame.get('channel') in ('market_trades', 'subscriptions') and timestamp == 1_000_000_000:
                return {'channel': 'heartbeats', 'sequence_num': frame['sequence_num'], 'events': []}
            return frame
        self.mutate(change)
        rows, manifest = build_dataset(self.path)
        self.assertFalse(rows)
        self.assertGreater(manifest['rejected_rows']['trade_flow_unavailable'], 0)

    def test_exports_are_reproducible_hash_checked_and_never_overwritten(self):
        output = Path(self.tmp.name) / 'dataset.jsonl'
        result = export_dataset(self.path, output)
        self.assertEqual(result['dataset_sha256'], hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertEqual(result['source_digest'], load_replay(self.path).source_digest)
        other = Path(self.tmp.name) / 'other.jsonl'; export_dataset(self.path, other)
        self.assertEqual(output.read_bytes(), other.read_bytes())
        with self.assertRaisesRegex(ValueError, 'already exists'): export_dataset(self.path, output)

    def test_invalid_settings_fail_and_zero_row_dataset_is_explicit(self):
        for kwargs in [{'grid_ms': 0}, {'lookback_ms': -1}, {'horizons_ms': ()},
                       {'horizons_ms': (1, 1)}, {'horizons_ms': (True,)}]:
            with self.assertRaises(ValueError): build_dataset(self.path, **kwargs)
        rows, manifest = build_dataset(self.path, horizons_ms=(60000,))
        self.assertEqual(rows, [])
        self.assertEqual(manifest['candidate_rows'], 0)

    def test_installed_style_cli_replay_and_dataset(self):
        output = Path(self.tmp.name) / 'cli.jsonl'
        for arguments in [('replay', '--db', str(self.path)),
                          ('dataset', '--db', str(self.path), '--out', str(output))]:
            result = subprocess.run([sys.executable, '-m', 'kalshi_lab', *arguments], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(output.read_text().splitlines()), 7)
        before = self.path.read_bytes()
        result = subprocess.run([sys.executable, '-m', 'kalshi_lab', 'replay',
                                 '--db', str(self.path), '--out', str(self.path)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
