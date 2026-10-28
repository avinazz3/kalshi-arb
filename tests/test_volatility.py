import math
import unittest
from kalshi_lab.digital import SECONDS_PER_YEAR
from kalshi_lab.volatility import estimate


class VolatilityTests(unittest.TestCase):
    def test_constant_log_returns_have_same_annual_volatility(self):
        prices = [100 * math.exp(i * .001) for i in range(61)]
        result = estimate(prices)
        expected = .001 * math.sqrt(SECONDS_PER_YEAR / 60)
        self.assertAlmostEqual(result['rolling_annual_volatility'], expected)
        self.assertAlmostEqual(result['ewma_annual_volatility'], expected)

    def test_recent_shock_has_more_weight(self):
        self.assertGreater(estimate([100, 100, 100, 110], half_life=1)['ewma_annual_volatility'],
                           estimate([100, 110, 110, 110], half_life=1)['ewma_annual_volatility'])

    def test_invalid_prices_rejected(self):
        for prices in ([1], [1, 0, 2], [1, float('nan'), 2]):
            with self.assertRaises(ValueError):
                estimate(prices)
