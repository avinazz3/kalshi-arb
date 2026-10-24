# Recorded connectivity pilot

This directory contains actual public Kalshi responses, separate from the
synthetic arithmetic demonstration. Observation times are inside each JSON/SQLite
record. Do not reuse this dated watchlist as a permanent research universe.

The construction run retrieved 30 open CPI contracts across three reference
months, with complete pagination, and proposed 27 adjacent threshold relations.
All proposals remain unreviewed. The selected watchlist was four thresholds
from `KXCPI-26OCT`: 0.1%, 0.2%, 0.3%, 0.4%.

Two sequential sweeps yielded eight schema-valid books with no failed HTTP
attempts and no empty bid sides. Median successful book-request duration was
about 7.13 seconds; the maximum within-sweep receipt span was about 22.16 seconds.
The whole sweep took about 29.5 seconds. These timings describe this environment,
not typical Kalshi matching-engine latency or your future local connection.

This pilot validates plumbing only. It is too short and asynchronous to estimate
opportunity frequency, quote survival, profitability or capital efficiency.
No real-market net-profit calculation has been performed.

Inspect it offline from the project root:

```bash
python3 -m kalshi_lab inspect --db examples/pilot/orderbooks.sqlite
```

The source passed 18 offline tests covering payoff/cost arithmetic, depth,
fractional quantities, wrong units, event separation and raw-response handling.
The observed CPI terms/metadata discrepancy is documented in
`docs/PRELIMINARY_RULE_REVIEW.md`.
