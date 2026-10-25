from decimal import Decimal as D
from pathlib import Path
import json
import subprocess
import sys
import unittest

from kalshi_lab.fees import (
    FeePolicy, Fill, OrderFeeAccumulator, calculate_order, calculate_orders,
)


def example_document():
    path = Path(__file__).resolve().parents[1] / 'examples' / 'fee_orders.json'
    return json.loads(path.read_text())


class RoundingTests(unittest.TestCase):
    def test_official_fcm_buy_example(self):
        # Published example: revenue -.055, raw model fee .00363825.
        result = OrderFeeAccumulator('0.01').apply('-.055000', '.00363825')
        self.assertEqual(result['trade_fee_dollars'], D('.003639'))
        self.assertEqual(result['rounding_fee_dollars'], D('.001361'))
        self.assertEqual(result['cash_change_dollars'], D('-.060000'))
        self.assertEqual(result['net_fee_dollars'], D('.005'))

    def test_same_fill_under_direct_member_precision(self):
        result = OrderFeeAccumulator('0.0001').apply('-.055', '.00363825')
        self.assertEqual(result['net_fee_dollars'], D('.0037'))
        self.assertEqual(result['cash_change_dollars'], D('-.0587'))

    def test_documented_accumulator_pattern(self):
        ledger = OrderFeeAccumulator('0.01')
        # These revenue/fee inputs generate the documented .004 carry per fill.
        results = [ledger.apply('-.99', '.006') for _ in range(3)]
        self.assertEqual([r['rebate_dollars'] for r in results], [D('0'), D('0'), D('.01')])
        self.assertEqual(ledger.rounding_carry, D('.002'))
        self.assertEqual(sum(r['net_fee_dollars'] for r in results), D('.02'))

    def test_rebate_cap_prevents_negative_fee(self):
        ledger = OrderFeeAccumulator('0.01')
        results = [ledger.apply('-.006', '0') for _ in range(4)]
        self.assertEqual(ledger.rounding_carry, D('.016'))
        self.assertTrue(all(r['rebate_dollars'] == 0 for r in results))
        self.assertTrue(all(r['net_fee_dollars'] == D('.004') for r in results))

    def test_signed_sell_revenue_rounds_towards_negative_infinity(self):
        result = OrderFeeAccumulator('0.01').apply('.055', '.00363825')
        self.assertEqual(result['cash_change_dollars'], D('.05'))
        self.assertEqual(result['net_fee_dollars'], D('.005'))

    def test_equivalent_precision_strings_are_canonical(self):
        a = OrderFeeAccumulator('0.010').apply('-.055', '.00363825')
        b = OrderFeeAccumulator('0.01').apply('-.055', '.00363825')
        self.assertEqual(a, b)

    def test_invalid_precision_revenue_and_model_fee_fail(self):
        for precision in ['0', '.001', 'NaN']:
            with self.subTest(precision=precision), self.assertRaises(ValueError):
                OrderFeeAccumulator(precision)
        for revenue, fee in [('.0000001', '0'), ('0', '-.01'), ('NaN', '0')]:
            with self.subTest(revenue=revenue), self.assertRaises(ValueError):
                OrderFeeAccumulator('.01').apply(revenue, fee)


class PolicyAndOrderTests(unittest.TestCase):
    def setUp(self):
        self.policy = FeePolicy('quadratic', '1', '1')

    def test_published_half_dollar_taker_fee(self):
        order = calculate_order([Fill('.5', '100', 'taker', 'buy')], self.policy, '.01')
        self.assertEqual(order['net_fee_dollars'], D('1.75'))
        self.assertEqual(order['cash_change_dollars'], D('-51.75'))

    def test_maker_rate_and_zero_maker_multiplier(self):
        fill = Fill('.5', '100', 'maker', 'buy')
        self.assertEqual(self.policy.model_fee(fill), D('.4375'))
        waived = FeePolicy('quadratic', '1', '0')
        self.assertEqual(calculate_order([fill], waived, '.01')['net_fee_dollars'], 0)

    def test_multiplier_two_and_zero_are_respected(self):
        fill = Fill('.5', '100', 'taker', 'buy')
        self.assertEqual(FeePolicy('quadratic', '2', '0').model_fee(fill), D('3.50'))
        self.assertEqual(FeePolicy('quadratic', '0', '0').model_fee(fill), 0)

    def test_mixed_maker_and_taker_fills_share_one_accumulator(self):
        fills = [Fill('.1', '1', 'taker', 'buy'), Fill('.1', '1', 'maker', 'buy'),
                 Fill('.1', '1', 'taker', 'buy')]
        order = calculate_order(fills, self.policy, '.01')
        self.assertEqual(order['net_fee_dollars'], D('.02'))
        # .0037 carry from taker + .008425 from maker triggers the second-fill rebate.
        self.assertEqual(order['fills'][1]['rebate_dollars'], D('.01'))
        self.assertEqual(order['fills'][-1]['rebate_dollars'], 0)
        self.assertEqual(order['rounding_carry_dollars'], D('.005825'))

    def test_separate_orders_do_not_share_rounding(self):
        fill = Fill('.1', '1', 'taker', 'buy')
        joined = calculate_order([fill] * 3, self.policy, '.01')
        separate = [calculate_order([fill], self.policy, '.01') for _ in range(3)]
        self.assertEqual(joined['net_fee_dollars'], D('.02'))
        self.assertEqual(sum(o['net_fee_dollars'] for o in separate), D('.03'))

    def test_buy_and_sell_fills_cannot_be_combined_as_one_order(self):
        fills = [Fill('.1', '1', 'taker', 'buy'), Fill('.1', '1', 'maker', 'sell')]
        with self.assertRaisesRegex(ValueError, 'both buy and sell'):
            calculate_order(fills, self.policy, '.01')

    def test_price_levels_cannot_be_replaced_by_average_price(self):
        fills = [Fill('.1', '1', 'taker', 'buy'), Fill('.9', '1', 'taker', 'buy')]
        result = calculate_order(fills, self.policy, '.0001')
        self.assertEqual(result['net_fee_dollars'], D('.0126'))
        average = calculate_order([Fill('.5', '2', 'taker', 'buy')], self.policy, '.0001')
        self.assertEqual(average['net_fee_dollars'], D('.035'))

    def test_subcent_price_and_fractional_quantity(self):
        fill = Fill('.4321', '.25', 'taker', 'buy')
        result = calculate_order([fill], self.policy, '.0001')
        self.assertEqual(result['revenue_dollars'], D('-.108025'))
        self.assertEqual(result['cash_change_dollars'] % D('.0001'), 0)
        self.assertEqual(result['revenue_dollars'] - result['net_fee_dollars'], result['cash_change_dollars'])

    def test_unsupported_models_missing_and_invalid_multipliers_fail(self):
        for model, taker, maker in [('flat', '1', '0'), ('quadratic', '-1', '0'),
                                     ('quadratic', '1', 'NaN'), ('quadratic', 0.1, '0')]:
            with self.subTest(model=model, taker=taker), self.assertRaises(ValueError):
                FeePolicy(model, taker, maker)
        with self.assertRaises(TypeError):
            FeePolicy('quadratic', '1')

    def test_invalid_fill_fields_and_empty_orders_fail(self):
        invalid = [('-0.1', '1', 'taker', 'buy'), ('1.1', '1', 'taker', 'buy'),
                   ('.00001', '1', 'taker', 'buy'), ('.5', '.001', 'taker', 'buy'),
                   ('.5', '0', 'taker', 'buy'), ('.5', '1', 'unknown', 'buy'),
                   ('.5', '1', 'taker', 'unknown'), (0.5, '1', 'taker', 'buy')]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                Fill(*values)
        with self.assertRaises(ValueError):
            calculate_order([], self.policy, '.01')


class FeeInputAndDemoTests(unittest.TestCase):
    def test_hypothetical_round_trip_at_both_precisions(self):
        for precision, expected_fee, expected_cash in [
            ('.0001', '.2778', '.3622'), ('.01', '.28', '.36')]:
            with self.subTest(precision=precision):
                result = calculate_orders(example_document(), precision)
                self.assertEqual(result['total_fee_dollars'], D(expected_fee))
                self.assertEqual(result['total_cash_change_dollars'], D(expected_cash))

    def test_duplicate_order_ids_and_missing_configuration_fail(self):
        duplicate = example_document()
        duplicate['orders'][1]['order_id'] = duplicate['orders'][0]['order_id']
        with self.assertRaises(ValueError):
            calculate_orders(duplicate, '.01')
        missing = example_document()
        del missing['policy']['maker_multiplier']
        with self.assertRaises(TypeError):
            calculate_orders(missing, '.01')

    def test_invalid_document_shapes_fail(self):
        invalid = [None, [], {}, {'policy': {}, 'orders': []},
                   {'policy': [], 'orders': []}, {'policy': {}, 'orders': [], 'extra': True}]
        for document in invalid:
            with self.subTest(document=document), self.assertRaises((ValueError, TypeError)):
                calculate_orders(document, '.01')
        for fills in [None, {}, [None]]:
            document = example_document()
            document['orders'][0]['fills'] = fills
            with self.subTest(fills=fills), self.assertRaises(ValueError):
                calculate_orders(document, '.01')

    def test_cli_explicit_input(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, '-m', 'kalshi_lab', 'fees', '--input',
                                 str(root / 'examples/fee_orders.json'), '--balance-precision', '0.0001'],
                                cwd=root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(D(json.loads(result.stdout)['total_fee_dollars']), D('.2778'))


if __name__ == '__main__':
    unittest.main()
