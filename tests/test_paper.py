from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from test_history import START, download
from test_shadow import FakeClock, LiveHTTP
from kalshi_lab.fees import FeePolicy
from kalshi_lab.history import digest
from kalshi_lab.paper import run_paper, walk_asks
from kalshi_lab.shadow import normalize_book, run_shadow
from datetime import timedelta


class PaperTests(unittest.TestCase):
    def fixture(self, root):
        download(root/'history')
        clock=FakeClock(START+timedelta(minutes=4,seconds=55))
        run_shadow(root/'shadow',minutes=1,paths=100,http=LiveHTTP(clock),clock=clock.clock,
                   monotonic=clock.monotonic,pause=clock.pause)
        policy = json.loads(Path('examples/paper_policy.json').read_text())
        (root/'policy.json').write_text(json.dumps(policy))
        return policy

    def test_depth_walk_complement_fee_rounding_and_price_limit(self):
        book=normalize_book({'orderbook_fp':{'yes_dollars':[['.4','10']], 'no_dollars':[['.4','1'],['.35','2']]}})
        policy=FeePolicy('quadratic',Decimal(1),Decimal(0))
        result=walk_asks(book,'yes',Decimal(2),policy,Decimal('.0001'))
        self.assertEqual(result['cost'],Decimal('1.25'))
        self.assertEqual(result['fee'],Decimal('.032800'))
        # Balance alignment uses the configured account precision.
        self.assertEqual(result['cash_cost'],Decimal('1.2828'))
        self.assertIsNone(walk_asks(book,'yes',Decimal(4),policy,Decimal('.0001')))
        self.assertIsNone(walk_asks(book,'yes',Decimal(2),policy,Decimal('.0001'),limit=Decimal('.60')))

    def test_end_to_end_post_latency_book_and_separate_model_pnl(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.fixture(root)
            result=run_paper(root/'shadow',[root/'history'],root/'policy.json',root/'paper')
            for model in ('rolling','ewma'):
                self.assertEqual(result['models'][model]['scored_positions'],1)
                self.assertEqual(Decimal(result['models'][model]['net_profit_dollars']),Decimal('.3832'))
            with self.assertRaises(ValueError):
                run_paper(root/'shadow',[root/'history'],root/'policy.json',root/'paper')

    def test_no_later_snapshot_means_no_assumed_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);policy=self.fixture(root)
            policy.update(latency_ms=60000,max_wait_ms=0)
            (root/'policy.json').write_text(json.dumps(policy))
            result=run_paper(root/'shadow',[root/'history'],root/'policy.json',root/'paper')
            self.assertEqual(result['counts']['rolling:no_post_latency_book'],1)
            self.assertEqual(result['models']['rolling']['scored_positions'],0)

    def test_insufficient_execution_depth_and_unknown_fee_settings_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);policy=self.fixture(root)
            policy['quantity']='100.00'
            (root/'policy.json').write_text(json.dumps(policy))
            result=run_paper(root/'shadow',[root/'history'],root/'policy.json',root/'paper')
            self.assertEqual(result['models']['rolling']['attempts'],0)
            del policy['policy']['taker_multiplier']
            (root/'policy.json').write_text(json.dumps(policy))
            with self.assertRaises(TypeError):
                run_paper(root/'shadow',[root/'history'],root/'policy.json',root/'invalid')
            self.assertFalse((root/'invalid').exists())

    def test_modified_or_future_input_record_is_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);self.fixture(root)
            path=root/'shadow'/'decisions.jsonl'
            rows=[json.loads(line) for line in path.read_text().splitlines()]
            decision=next(d for d in rows if d['status']=='forecast')
            decision['prediction']['calculation_started_at']=(START-timedelta(days=1)).isoformat()
            path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            report=json.loads((root/'shadow'/'report.json').read_text())
            report['artifacts']['decisions.jsonl']=digest(path)
            (root/'shadow'/'report.json').write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError,'alignment|did not exist'):
                run_paper(root/'shadow',[root/'history'],root/'policy.json',root/'paper')
            self.assertFalse((root/'paper').exists())
