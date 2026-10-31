# Primary references

## Settlement-probability research

- Archak and Ipeirotis, Modeling Volatility in Prediction Markets (working paper):
  https://archive.nyu.edu/jspui/bitstream/2451/27716/4/CeDER-08-07.pdf
  Connects underlying diffusion models with binary probabilities and contract
  volatility. InTrade evidence is not validation for BTC 15-minute outcomes.
- Bergsli, Lind, Molnar and Polasik, Forecasting Volatility of Bitcoin (2022):
  https://doi.org/10.1016/j.ribaf.2021.101540
  Motivates volatility-model comparisons; daily forecast results do not establish
  performance at our remaining-minute horizons.
- Kemna and Vorst, A Pricing Method for Options Based on Average Asset Values (1990):
  https://doi.org/10.1016/0378-4266(90)90039-5
  Numerical-method reference for averaged payoffs. Their original Asian-option
  payoff differs from a binary indicator. Variance reduction is not implemented
  in this first independent-path simulator.
- Lee, Lee and Lee, Cryptocurrency Prediction Markets Through the Derivatives Lens:
  https://doi.org/10.1016/j.irfa.2026.105433
  Instrument-specific pricing perspective. Risk-neutral pricing and physical
  outcome forecasting are separate objects.
- Gneiting and Raftery, Strictly Proper Scoring Rules, Prediction, and Estimation (2007):
  https://sites.stat.washington.edu/raftery/Research/PDF/Gneiting2007jasa.pdf
  Motivates probability scores. Historical forecast evaluation remains planned.

## Kalshi APIs

- Public metadata: https://docs.kalshi.com/getting_started/quick_start_market_data
- Endpoints: https://docs.kalshi.com/getting_started/api_environments
- Authentication: https://docs.kalshi.com/getting_started/quick_start_authenticated_requests
- Crypto settlement overview: https://help.kalshi.com/en/articles/13823838-crypto-markets
- CF Benchmarks REST access and entitlement: https://docs.kalshi.com/cfbenchmarks/rest-passthrough
- CF Benchmarks index feed: https://docs.kalshi.com/websockets/cfbenchmarks-value
- Historical markets: https://docs.kalshi.com/api-reference/historical/get-historical-markets
- Quote candles: https://docs.kalshi.com/api-reference/market/get-market-candlesticks
- Fixed-point values: https://docs.kalshi.com/getting_started/fixed_point_migration
- Fees: https://kalshi.com/docs/kalshi-fee-schedule.pdf
- Rounding: https://docs.kalshi.com/getting_started/fee_rounding

## CF Benchmarks upstream schemas

- Latest values: https://docs.cfbenchmarks.com/api/rest/latest-values/
- Historical values: https://docs.cfbenchmarks.com/api/rest/historical-values/
- Recent values/revision fields: https://docs.cfbenchmarks.com/api/rest/values/

The diagnostic uses latest_values for its small current-value response and one
history/values hour. It does not retain arbitrary authenticated response bodies.
Access does not establish historical point-in-time availability; amendments and
publication delays require a separate policy in empirical work.

Settlement, authentication and CF data references checked October 27, 2026.
Documentation and series terms can change; archive and review the actual
applicable contract rules. Earlier order-flow references are retained in Git history.


Historical-data milestone (checked 2026-10-27):

- [Recent markets](https://docs.kalshi.com/api-reference/market/get-markets):
  close-time filters are incompatible with status=settled. Local outcome validation
  follows the close-time query.
- [Archived markets](https://docs.kalshi.com/api-reference/historical/get-historical-markets):
  series filter and cursor pagination; no documented time filter.
- [Recent candles](https://docs.kalshi.com/api-reference/market/get-market-candlesticks)
  and [archived candles](https://docs.kalshi.com/api-reference/historical/get-historical-market-candlesticks):
  one-minute periods, inclusive end-period filters; aggregates are not executable depth.
- [CF historical values](https://docs.cfbenchmarks.com/api/rest/historical-values/):
  ascending times, truncated hour requests, possible 15-minute publication delay.
- [CF convenience averages](https://docs.kalshi.com/websockets/cfbenchmarks-value):
  quarter-hour accumulator excludes the start tick and includes close; trailing
  avg_60s_data has different semantics. This documentation does not by itself
  approve a historical settlement-reconstruction convention.
- [General CRYPTO terms](https://assets.kalshi.com/contract_terms/CRYPTO.pdf):
  60-second underlying average and exclusion of post-expiration revisions.
  Applicable market-specific rules must still be reviewed.

The audit's four sampling/rounding cases are engineering sensitivity hypotheses,
not a claimed result from a paper or a certified Kalshi settlement implementation.

## Multi-day and live observation additions — checked 2026-10-29

Live ingestion update checked 2026-10-31:
- https://docs.kalshi.com/websockets/cfbenchmarks-value — BRTI subscription and raw upstream frame.
- https://docs.kalshi.com/getting_started/quick_start_websockets — GET signing of `/trade-api/ws/v2`.
- https://websocket-client.readthedocs.io/en/latest/app.html — library callbacks, fresh handshake headers, reconnect and heartbeat.
The collector uses the raw upstream tick, not the feed's derived average fields.

- [Orderbook responses](https://docs.kalshi.com/getting_started/orderbook_responses):
  fixed-point dollar/count strings; YES/NO bid ladders imply opposite-side asks.
- [Get market orderbook](https://docs.kalshi.com/api-reference/market/get-market-orderbook):
  current REST snapshot and bounded depth parameter. The recorder sorts parsed
  prices explicitly rather than depending on prose descriptions of array ordering.
- [CF REST passthrough](https://docs.kalshi.com/cfbenchmarks/rest-passthrough):
  authenticated entitled access, wrapped CF payloads, history forwarding and higher
  read-budget cost. The recorder reuses the existing tested latest/history schemas.

Day-block bootstrap intervals are exploratory paired resampling diagnostics, with
the exact algorithm, minimum-data guard and limitations described in
[MULTIDAY_AND_SHADOW.md](MULTIDAY_AND_SHADOW.md). No inferential result is claimed
from two days. Paper fills and fee multipliers are explicit scenario assumptions,
not exchange guarantees or empirically established trading performance.
