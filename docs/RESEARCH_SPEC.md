# Settlement-aware Bitcoin contract probabilities

## Question and first scope

Do time-varying volatility, distribution assumptions and accurate settlement
averaging improve forecasts of BTC 15-minute contract outcomes relative to a
simple diffusion benchmark and Kalshi's contemporaneous midpoint?

Start with BTC, series KXBTC15M. Forecast the real-world probability of YES at
10, 5 and 2 minutes before close. This is a settlement target, replacing the
previous active 1s/5s midpoint-change experiment. The earlier specification and
commands remain in [ORDER_FLOW_SPEC.md](ORDER_FLOW_SPEC.md) and
[ORDER_FLOW.md](ORDER_FLOW.md); existing raw captures and exports are preserved.

## Payoff and information set

Represent the contract by its actual rule version: opening reference K, index ID,
opening/closing sampling timestamps, arithmetic averaging, rounding and equality
conditions. The schematic target is P(rounded closing average >= K | G_t).
The comparison must instead be strict if the particular rules require it.
Do not infer those conditions from a ticker, a chart or prose similarities.
The active market's observed reference must be available before the prediction.

G_t contains contract metadata and index observations received by decision time t,
including earlier price history used to estimate volatility. Store source and
receipt timestamps and revision flags. Select a stale-data limit before testing.
A source timestamp before t does not make a later-received observation available
at t. Retrospective index history may be amended and cannot prove old receipt-time
availability; distinguish retrospective modelling from prospective validation.
A minute OHLC candle does not reconstruct the sixty settlement observations.
Coinbase prices can only be labelled proxies, with index-basis error assessed.

Exclude final-minute decision times initially. When adding them later, preserve
observed closing-window samples and simulate only the remaining samples; missing
observed values must not be treated as uncertain future values.

## Implemented milestone (0.5.0)

- GET-only diagnostic for a bounded BTC metadata/quote/outcome sample, latest BRTI
  and one historical BRTI hour. Historical access requires account entitlement.
- Physical-measure geometric Brownian motion with supplied annual volatility and
  annual price drift, using a 365-day year.
- Analytical terminal binary probability, including explicit rounding cutoffs and
  deterministic cases, plus independent-path Monte Carlo on explicit observation
  schedules for terminal and arithmetic-average payoffs.
- Comparison/rounding fixtures, analytical versus simulated probability checks,
  random-seed reproducibility and Monte Carlo uncertainty reporting.

These are conditional model calculations with explicit inputs. They do not fit
volatility or drift, certify live settlement rules, export a historical settlement
forecast dataset, evaluate calibration, or simulate executable trades. There are
no claimed historical forecasting results. See [DIGITAL_MODEL.md](DIGITAL_MODEL.md).

## Planned model comparison

Keep the payoff calculation and evaluation observations consistent:

1. Constant-parameter diffusion with a rolling historical volatility estimate.
2. The same diffusion with EWMA volatility, fixed during each individual forecast.
3. Conditional volatility/distribution alternatives, such as GARCH with Gaussian
   versus heavy-tailed innovations. Specify a coherent path model for the closing
   average, rather than assuming a multi-step return is a one-step Student-t draw.
4. HAR-based alternatives only if the data and earlier results justify them.

Initial annual *price* drift is zero. This means log-price drift is -sigma^2/2,
not zero. Drift changes require their own chronological validation; minute-level
sample means are noisy. Historical volatility estimates describe a physical
forecast. A Black-Scholes option price uses a pricing measure and discounting;
these are separate experimental objects, not interchangeable probabilities.

Record training period, initialization, lookbacks, volatility units, parameter
estimation, random seed/path count and the actual averaging rule for every forecast.
Estimation history can precede the 15-minute contract. Never substitute invented
example volatility for fitted parameters when reporting empirical performance.

## Evaluation

Use chronological train/validation/test periods. Keep every observation for a
contract in one split and group inference by contract/day. Three predictions
per contract share one outcome; they are not three independent bets. Purge training
labels that would not yet have resolved at the first validation/test decision.
Fix model settings before the final untouched period. Report missing observations,
coverage, stale-index/quote exclusions and rule/reference failures by horizon.

Use Brier score and log loss against realised settled YES/NO outcomes. Include the
contemporaneous market midpoint and a simple diffusion benchmark on identical
rows. Assess calibration by prediction horizon and volatility/reference-distance
regime. Score the full return distribution when relevant; matching volatility
alone need not produce good binary probabilities. Numerical simulation error and
parameter/model uncertainty are different and must be reported separately.

For prospective evidence, archive the inputs as they arrive and only later attach
outcomes. Confirm exact reference data access before building around it. Start
with an access/quality sample; multiple days/regimes and an untouched future period
are necessary before drawing forecasting conclusions.

## Economics and research contribution

A discrepancy from the market is a candidate signal, not proof of mispricing.
Any execution study must use available asks/bids, observed size and plausible
arrival delays, with the actual fee policy and rounding. Historical quote candles
are not proof that a hypothetical order could fill. Settlement timing and holding
risk matter even when no early exit is planned.

The contribution is a reproducible test of settlement-aware probability forecasts
under limited data requirements. Established digital-option mathematics alone is
not novel, and a null forecasting or trading result is a valid research outcome.
