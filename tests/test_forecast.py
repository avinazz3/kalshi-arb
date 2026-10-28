import json
import math
from pathlib import Path
import tempfile
import unittest
from test_history import download
from kalshi_lab.forecast import predict, losses, run_forecast, Unavailable

class ForecastTests(unittest.TestCase):
    def test_scores(self):
        self.assertAlmostEqual(losses(.5,1)['brier'],.25)
        self.assertAlmostEqual(losses(.5,1)['log_loss'],math.log(2))
        self.assertTrue(losses(0,1)['log_clipped'])

    def test_future_prices_do_not_change_prediction(self):
        close=100000; decision=close-600
        points={t*1000:dict(value=str(100+((t//60)%7)/100),amend_time_ms=None,repeat_of_previous_value=False)
                for t in range(decision-1-3600,decision,60)}
        args=dict(ticker='TEST',reference='100.00',comparison='greater_or_equal',close_ts=close,horizon=10,paths=100)
        before=predict(points,**args)
        points[decision*1000]=dict(value='1000000',amend_time_ms=None,repeat_of_previous_value=False)
        self.assertEqual(before,predict(points,**args))
        self.assertEqual(before['sample_offsets_seconds'],list(range(541,601)))
        del points[(decision-1)*1000]
        with self.assertRaises(Unavailable):predict(points,**args)

    def test_end_to_end_and_output_preservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'history';out=Path(tmp)/'forecast'
            download(source)
            result=run_forecast(source,out,paths=100)
            self.assertEqual(result['status'],'completed')
            self.assertEqual(result['paired_rows'],12)
            self.assertEqual(result['scores']['overall']['models']['market']['brier'],.25)
            rows=[json.loads(s) for s in (out/'predictions.jsonl').read_text().splitlines()]
            self.assertTrue(all('outcome' not in r for r in rows))
            with self.assertRaises(ValueError):run_forecast(source,out,paths=100)
