from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_history
from test_history import START, FakeHTTP, NOW, market
from kalshi_lab.batch import run_batch
from kalshi_lab.history import fetch_history, digest


class FullDayHTTP(FakeHTTP):
    def get(self,path,params):
        response=super().get(path,params)
        if path=='/markets':
            response.data['markets']=[market(i) for i in range(96)]
        return response


class BatchTests(unittest.TestCase):
    def test_complete_day_and_verified_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            transports=[]
            def downloader(path,**kwargs):
                http=FullDayHTTP();transports.append(http)
                return fetch_history(path,http=http,now=NOW,pause=lambda _:None,**kwargs)
            result=run_batch(START.isoformat(),1,root/'data',root/'batch1',paths=100,downloader=downloader,now=NOW)
            self.assertEqual(result['status'],'completed')
            self.assertEqual(result['rows'],288)
            self.assertFalse(result['runs'][0]['forecast_reused'])
            prediction=Path(result['runs'][0]['forecast'])/'predictions.jsonl'
            before=digest(prediction)
            # Existing user runs may use names such as btc-day-001.
            for kind in ('history','forecasts','analysis'):
                (root/'data'/kind/'btc-20261026').rename(root/'data'/kind/'btc-day-001')
            prediction=root/'data'/'forecasts'/'btc-day-001'/'predictions.jsonl'
            result2=run_batch(START.isoformat(),1,root/'data',root/'batch2',paths=100,downloader=downloader,now=NOW)
            self.assertTrue(result2['runs'][0]['forecast_reused'])
            self.assertTrue(result2['runs'][0]['analysis_reused'])
            self.assertEqual(len(transports),1)
            self.assertTrue(result2['runs'][0]['history_reused'])
            self.assertEqual(before,digest(prediction))
            with self.assertRaisesRegex(ValueError,'settings differ'):
                run_batch(START.isoformat(),1,root/'data',root/'mixed',paths=101,downloader=downloader,now=NOW)

    def test_invalid_dates_fail_before_downloading_or_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            for start,days in [(START.isoformat(),0),((START+timedelta(hours=1)).isoformat(),1),(NOW.isoformat(),1)]:
                with self.assertRaises(ValueError):
                    run_batch(start,days,Path(tmp)/'data',Path(tmp)/'batch',now=NOW)
                self.assertFalse((Path(tmp)/'batch').exists())
