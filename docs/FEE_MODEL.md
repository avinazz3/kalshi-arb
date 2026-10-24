# Fee arithmetic and independent verification

This engine models the published general quadratic fee family for $1 binary
contracts. The caller supplies the effective maker/taker multipliers, ordered
fills and account balance precision. It does not infer a policy from a ticker.

The checked references are the fee schedule effective July 7, 2026 and the
current API fee-rounding documentation, reviewed October 25, 2026:

- https://kalshi.com/docs/kalshi-fee-schedule.pdf
- https://docs.kalshi.com/getting_started/fee_rounding

## Model inputs

The unrounded model amount for one fill is `M * r * q * p * (1-p)`.
`r=0.07` for takers and `r=0.0175` for makers. Each role has its own explicit
multiplier; an absent field is an error, and zero is a valid supplied value.
The model name `quadratic` identifies this calculation family. It is not an
automatic interpretation of the exchange's raw `fee_type` string.

Prices are exact dollar strings in [0,1], with up to four decimal places;
quantities have up to two. Each fill explicitly identifies buy/sell and
maker/taker. For a NO fill, supply the NO instrument's transaction price.

## Rounding state

Each fill's model amount is rounded upward to a micro-dollar. Signed revenue
(negative for buys) minus that amount is aligned downward to the account grid.
The extra rounding charge enters one order's carry. Eligible grid-sized rebates
reduce carry, capped to keep the fill's net fee nonnegative. The engine returns
all components and the final cash change. A new order starts with fresh carry;
maker and taker fills within one order share carry.

This is a documentation-based model. Account fill-ledger reconciliation is still
required before using it for live profitability claims.

## Run the example

```bash
python3 -m kalshi_lab fee-demo
python3 -m kalshi_lab fees --input examples/fee_orders.json --balance-precision 0.0001
python3 -m kalshi_lab fees --input examples/fee_orders.json --balance-precision 0.01
```

`examples/fee_orders.json` supplies hypothetical prices and two separate
single-fill taker orders. Its maker multiplier is an unused example input,
not an assertion about CPI maker fees. Both account-precision scenarios are
shown because the user's account type has not been established.

Independent arithmetic for eight pairs at $0.44 and $0.52:

| Quantity / price | Raw taker model fee, multiplier 1 |
|---|---|
| 8 at $0.44 | $0.137984 |
| 8 at $0.52 | $0.139776 |

| Balance precision | Modelled fees | Minimum ordinary payout minus cost and fees |
|---|---|---|
| $0.0001 | $0.2778 | $0.0422 |
| $0.01 | $0.28 | $0.04 |

Gross purchase cost remains $7.68 and gross surplus $0.32. The original `demo`
still illustrates a deliberately assumed $0.20 reserve; `fee-demo` makes the
specified quadratic model explicit. Neither command uses live quotes.

The tests also reproduce the official buy example (revenue -$0.055000, raw model
fee $0.00363825, cent-grid cash change -$0.06), and exercise the published carry
pattern. They check order separation, mixed liquidity roles, sell proceeds,
fractional quantities, nonuniform fill prices and invalid configurations.

## Limits and next integration

- Unsupported models are rejected. There is no generic fee-free fallback.
- Before calculating a real trade, verify series policy, event overrides,
  effective dates, market waivers and any intermediary charges. This module
  receives the resulting configuration; it does not resolve or archive it.
- Supply fills chronologically. Aggregated depth levels do not reveal the actual
  number/sequence of fills. Applying this engine to guessed fills is a scenario.
- Keep one actual order per input group. Mixed buy/sell directions in one group
  and duplicate order IDs are rejected.
- Rounding applies to complete and partial orders; order identity matters.
- The calculator computes fees and cash changes. Settlement payouts, acquisition
  completeness, funding costs and arbitrage certification belong elsewhere.

No order placement, opportunity scanning or CPI relationship approvals are
added by this change. The rule-review questions in `PRELIMINARY_RULE_REVIEW.md`
remain open.
