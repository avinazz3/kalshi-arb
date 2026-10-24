# Sources and reading order

Official documentation checked during starter construction, October 2026.
Recheck live documentation before implementing later trading milestones.

1. Public market data and production URL:
   https://docs.kalshi.com/getting_started/quick_start_market_data
2. Order-book response fields and opposite-side ask construction:
   https://docs.kalshi.com/getting_started/orderbook_responses
3. Market pagination, filtering and metadata:
   https://docs.kalshi.com/api-reference/market/get-markets
4. Series rule links and fee metadata:
   https://docs.kalshi.com/api-reference/market/get-series
5. Event metadata and fee overrides:
   https://docs.kalshi.com/api-reference/events/get-event
6. Fixed-point dollar prices and fractional quantities:
   https://docs.kalshi.com/getting_started/fixed_point_migration
7. Fee rounding and the order-level accumulator:
   https://docs.kalshi.com/getting_started/fee_rounding
8. Fee schedule (published effective date July 7, 2026 when checked):
   https://kalshi.com/docs/kalshi-fee-schedule.pdf
9. Current scheduled fee information:
   https://kalshi.com/fee-schedule
10. CPI contract terms, as linked by KXCPI series metadata:
    https://assets.kalshi.com/contract_terms/CPI.pdf

The URLs above can change. Every discovery/capture run records the series and
event metadata it actually retrieved; its timestamp is the relevant observation
date. Terms must be reviewed in full. This package does not distribute copies of
the official contract PDF or claim it has certified all exceptional resolutions.

Research papers selected in the project discussion:

- Hongjian Shi and Daojing Zhai, *Combinatorial Arbitrage in Prediction Markets*,
  arXiv:2610.23517v3. Read feasible payoff states and optimisation first, then the
  data/execution limitations. The research contribution should distinguish
  retrospective simulated fills from actual simultaneous execution.
- Saguillo, Ghafouri, Kiffer and Suarez-Tangil, *Unravelling the Probabilistic
  Forest: Arbitrage in Prediction Markets*. Focus on relation discovery and
  validation, then the limitations of inferring executable prices from trades.

These papers motivate this project; neither establishes profitable trading in
our selected Kalshi family. The starter has no optimiser or LLM relation miner.
