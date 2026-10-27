from decimal import Decimal
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from kalshi_lab.digital import (SECONDS_PER_YEAR, SettlementRule, benchmark,
                                binomial_summary, simulate, terminal_probability)


class DigitalTests(unittest.TestCase):
    def test_independent_normal_tail_and_time_units(self):
        # Set log(I/K)=sigma^2*tau/2: the formula's normal threshold is zero.
        tau, sigma = 600 / SECONDS_PER_YEAR, 0.6
        spot = str(100000 * math.exp(sigma * sigma * tau / 2))
        self.assertAlmostEqual(terminal_probability(spot, SettlementRule('100000'), 600, sigma), 0.5, places=10)
        self.assertAlmostEqual(terminal_probability('100', SettlementRule('100'), SECONDS_PER_YEAR, 0.2),
                               0.460172162722971, places=12)

    def test_rounded_threshold_and_comparison(self):
        up = SettlementRule('100.00', round_digits=2, rounding='half_up')
        even = SettlementRule('100.00', round_digits=2, rounding='half_even')
        self.assertTrue(up.yes('99.995'))
        self.assertFalse(up.yes('99.9949'))
        self.assertFalse(SettlementRule('100.00', 'greater', 2).yes('100.0049'))
        self.assertTrue(SettlementRule('100.00', 'greater', 2).yes('100.005'))
        self.assertFalse(SettlementRule('100.00', 'greater', 2, 'half_even').yes('100.005'))
        self.assertEqual(up.continuous_cutoff(), 99.995)
        self.assertEqual(up.continuous_cutoff(), even.continuous_cutoff())
        for rule in (up, even):
            self.assertEqual(terminal_probability('100.00', rule, 0, 0.6), 1)

    def test_zero_volatility_and_exact_equality(self):
        for comparison, expected in [('greater_or_equal', 1), ('greater', 0)]:
            rule = SettlementRule('100000.00', comparison, 2)
            self.assertEqual(terminal_probability('100000.00', rule, 600, 0), expected)
            result = simulate('100000.00', rule, 600, 0, [541, 600], paths=100)
            self.assertEqual(result['arithmetic_average']['probability'], expected)
        self.assertEqual(terminal_probability('99', SettlementRule('100'), 600, 0), 0)

    def test_zero_volatility_nonzero_drift_and_averaging(self):
        # Future values are exactly 100*exp(t). Mean at t=.25 and 1 is below
        # terminal 100*e; this catches accidentally settling on the terminal.
        rule = SettlementRule('250')
        result = simulate('100', rule, SECONDS_PER_YEAR, 0,
                          [SECONDS_PER_YEAR / 4, SECONDS_PER_YEAR], annual_price_drift=1, paths=100)
        self.assertEqual(result['terminal']['probability'], 1)
        self.assertEqual(result['arithmetic_average']['probability'], 0)

    def test_terminal_monte_carlo_agrees_with_analytic_and_single_sample_average(self):
        rule = SettlementRule('100000', round_digits=2)
        result = simulate('100100', rule, 600, 0.6, [600], paths=30000, seed=91)
        p = terminal_probability('100100', rule, 600, 0.6)
        terminal = result['terminal']
        self.assertLess(abs(terminal['probability'] - p), 5 * math.sqrt(p * (1-p) / terminal['paths']))
        self.assertEqual(terminal, result['arithmetic_average'])

    def test_multiple_observations_preserve_terminal_distribution(self):
        result = simulate('100100', SettlementRule('100000'), 600, 0.6, [541, 570, 600], paths=20000)
        p = terminal_probability('100100', SettlementRule('100000'), 600, 0.6)
        self.assertLess(abs(result['terminal']['probability'] - p), 5 * math.sqrt(p * (1-p) / 20000))

    def test_wilson_interval_does_not_claim_certainty_after_zero_or_all_successes(self):
        self.assertGreater(binomial_summary(0, 100)['wilson_95_interval'][1], 0)
        self.assertLess(binomial_summary(100, 100)['wilson_95_interval'][0], 1)

    def test_low_volatility_average_matches_correlated_brownian_limit(self):
        # At this tiny horizon-scaled volatility GBM is locally arithmetic
        # Brownian motion. Var(mean W_i) = sum_ij min(i,j)/n^2, not 1/n times
        # terminal variance. This independently checks within-path correlation.
        n, spot, sigma, reference = 60, 100000, 0.01, 100000.50
        sum_covariance = n * (n + 1) * (2*n + 1) / 6
        sd = spot * sigma * math.sqrt(sum_covariance / (n*n) / SECONDS_PER_YEAR)
        expected = 0.5 * math.erfc((reference - spot) / sd / math.sqrt(2))
        result = simulate(str(spot), SettlementRule(str(reference)), n, sigma,
                          list(range(1, n+1)), paths=20000, seed=74)
        tolerance = 5 * math.sqrt(expected * (1-expected) / 20000)
        self.assertLess(abs(result['arithmetic_average']['probability'] - expected), tolerance)

    def test_reference_monotonicity_and_seed_reproducibility(self):
        inputs = dict(spot='100', seconds_to_close=600, annual_volatility=0.6,
                      sample_offsets_seconds=[541, 600], paths=1000, seed=7)
        low = simulate(rule=SettlementRule('99.9'), **inputs)
        high = simulate(rule=SettlementRule('100.1'), **inputs)
        self.assertEqual(low, simulate(rule=SettlementRule('99.9'), **inputs))
        self.assertGreaterEqual(low['arithmetic_average']['yes_paths'], high['arithmetic_average']['yes_paths'])

    def test_invalid_inputs_and_work_budgets(self):
        for value in ['NaN', 'Infinity', '-1', True]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                terminal_probability('100', SettlementRule('100'), 600, value)
        for offsets in [[], [601], [-1], [10, 10], [20, 10], [True], [float('nan')]]:
            with self.subTest(offsets=offsets), self.assertRaises(ValueError):
                simulate('100', SettlementRule('100'), 600, 0.6, offsets)
        for rule in [('100.001', 'greater', 2), ('0', 'greater', None), ('100', 'less', None)]:
            with self.assertRaises(ValueError): SettlementRule(*rule)
        with self.assertRaisesRegex(ValueError, 'budget'):
            simulate('100', SettlementRule('100'), 600, 0.6, list(range(600)), paths=10000)
        with self.assertRaisesRegex(ValueError, 'overflow'):
            simulate('100', SettlementRule('100'), SECONDS_PER_YEAR, 0, [SECONDS_PER_YEAR], annual_price_drift=1e10, paths=2)

    def test_cli_example_and_existing_output_preserved(self):
        root = Path(__file__).resolve().parents[1]
        document = json.loads((root / 'examples/digital_contract.json').read_text())
        document['paths'] = 200
        with tempfile.TemporaryDirectory() as tmp:
            source, out = Path(tmp) / 'input.json', Path(tmp) / 'output.json'
            source.write_text(json.dumps(document))
            command = [sys.executable, '-m', 'kalshi_lab', 'digital', '--input', str(source), '--out', str(out)]
            run = subprocess.run(command, cwd=root, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            data = json.loads(out.read_text())
            self.assertEqual(data['model'], 'physical_gbm_constant_parameters')
            before = out.read_bytes()
            self.assertNotEqual(subprocess.run(command, cwd=root, capture_output=True).returncode, 0)
            self.assertEqual(out.read_bytes(), before)
        with self.assertRaises(ValueError): benchmark({**document, 'typo': 1})
