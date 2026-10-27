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

## Earlier order-flow research

- Gregory Young, OpenMarket: A Synchronized Polymarket–Binance Dataset for
  High-Frequency Prediction-Market Research: https://arxiv.org/abs/2607.26245
- Cont, Kukanov and Stoikov, The Price Impact of Order Book Events:
  https://arxiv.org/abs/1011.6402
- Gould and Bonart, Queue Imbalance as a One-Tick-Ahead Price Predictor:
  https://arxiv.org/abs/1512.03492

The latter two study equity order books. They motivate features and tests; their
findings are not evidence of a profitable Kalshi/crypto strategy.

## Kalshi APIs

- Public metadata: https://docs.kalshi.com/getting_started/quick_start_market_data
- Endpoints: https://docs.kalshi.com/getting_started/api_environments
- Authentication: https://docs.kalshi.com/getting_started/quick_start_authenticated_requests
- Crypto settlement overview: https://help.kalshi.com/en/articles/13823838-crypto-markets
- CF Benchmarks REST access and entitlement: https://docs.kalshi.com/cfbenchmarks/rest-passthrough
- CF Benchmarks index feed: https://docs.kalshi.com/websockets/cfbenchmarks-value
- Historical markets: https://docs.kalshi.com/api-reference/historical/get-historical-markets
- Quote candles: https://docs.kalshi.com/api-reference/market/get-market-candlesticks
- Streaming: https://docs.kalshi.com/getting_started/quick_start_websockets
- Snapshot/delta schema: https://docs.kalshi.com/websockets/orderbook-updates
- Exchange index: https://docs.kalshi.com/getting_started/exchange_sharding
- Fixed-point values: https://docs.kalshi.com/getting_started/fixed_point_migration
- Fees: https://kalshi.com/docs/kalshi-fee-schedule.pdf
- Rounding: https://docs.kalshi.com/getting_started/fee_rounding

## Coinbase APIs

- Public feeds and authentication:
  https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/websocket/websocket-endpoints
- Level2: https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/websocket/level2
- Trades: https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/websocket/market-trades
- Machine-readable schemas, maker-side semantics and connection sequence numbers:
  https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/advanced-trade-asyncapi.json

## CF Benchmarks upstream schemas

- Latest values: https://docs.cfbenchmarks.com/api/rest/latest-values/
- Historical values: https://docs.cfbenchmarks.com/api/rest/historical-values/
- Recent values/revision fields: https://docs.cfbenchmarks.com/api/rest/values/

The diagnostic uses latest_values for its small current-value response and one
history/values hour. It does not retain arbitrary authenticated response bodies.
Access does not establish historical point-in-time availability; amendments and
publication delays require a separate policy in empirical work.

Settlement, authentication and CF data references checked October 27, 2026.
Earlier order-flow protocol references checked October 25, 2026. Documentation
and series terms can change; archive and review the actual applicable contract
rules. The two venues have different timestamp and sequence semantics.
