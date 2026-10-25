# Primary references

## Research

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

Protocol references checked October 25, 2026. Preserve the raw payloads actually
received; documentation and series terms can change. The two venues have
different timestamp, quantity-update and sequence semantics.
