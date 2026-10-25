# Reusable fee model

The engine supports the published quadratic fee family for $1 binary contracts.
It is independent of asset, prediction horizon and settlement relationship.
Callers supply explicit maker/taker multipliers, chronological fills and account
precision. It does not resolve effective series/event fees from a ticker.

Reference schedule (effective July 7, 2026):
https://kalshi.com/docs/kalshi-fee-schedule.pdf

Rounding reference:
https://docs.kalshi.com/getting_started/fee_rounding

The raw model amount is M*r*q*p*(1-p), with r=0.07 for takers and r=0.0175 for
makers. The engine applies per-fill micro-dollar rounding, account-grid balance
alignment and order-level carry/rebates. Roles may change within one order;
directions may not. Separate orders never share carry.

## Check a hypothetical entry and exit

```bash
python3 -m kalshi_lab fees --input examples/fee_orders.json --balance-precision 0.0001
python3 -m kalshi_lab fees --input examples/fee_orders.json --balance-precision 0.01
```

The example buys eight YES contracts at $0.44 and later sells eight at $0.52,
in two separate one-fill taker orders. Both role multipliers are explicit; the
unused maker value is illustrative, not an assertion about a live series.

| Balance precision | Modelled total fees | Net cash change |
|---|---|---|
| $0.0001 | $0.2778 | $0.3622 |
| $0.01 | $0.28 | $0.36 |

The cash change is the supplied round trip's hypothetical result, not a minimum
settlement payout or observed trading profit. Real fills, executable prices and
latency still need measurement. This is a documented model pending actual
account-ledger reconciliation.

Unsupported models and incomplete policies fail explicitly. Future economic
replay must verify effective policies, overrides, waivers, account precision,
intermediary charges and funding costs. Aggregated book levels do not establish
actual fill sequences. Tests include official arithmetic examples and checks
of signed revenues, rebates, order grouping and fractional quantities.
