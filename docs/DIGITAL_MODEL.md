# Digital probability benchmark

## Scope and units

The `digital` command performs an offline calculation using supplied inputs.
It makes no HTTP calls and does not estimate parameters. The example is invented.
Prices and references are exact decimal inputs; path generation uses binary
floating-point arithmetic. Probabilities are physical-model outcome probabilities,
not exchange prices or discounted risk-neutral values.

Use a 365-day year: `seconds_per_year = 31536000`. Annual volatility 0.60 means
60%, not 60. `annual_price_drift` is the drift of price, not log-price. The model is

    dI / I = mu dt + sigma dW

with exact transitions at the requested observation times:

    I(t+dt) = I(t) exp((mu - sigma^2/2) dt + sigma sqrt(dt) Z), Z ~ N(0,1).

Here dt is in years. Zero price drift leaves the Ito log correction in place.
Parameters remain fixed within each forecast. Updating volatility at the next
prediction time is different from modelling stochastic future volatility.

## Analytical reference

For an unrounded terminal payoff, positive time and volatility:

    P(I_T >= K) = Phi((log(I_t/K) + (mu - sigma^2/2) tau) / (sigma sqrt(tau))).

Greater-than versus greater-or-equal does not matter for a continuous distribution,
but does matter on deterministic paths and after rounding. With positive prices,
a reference already on the rounding grid and half-up/half-even rounding, a rounded
value >= K has continuous cutoff K - tick/2. A rounded value > K has cutoff
K + tick/2. Boundary tie conventions affect deterministic values and are handled
with Decimal rounding; their probability is zero in a nondegenerate continuous
GBM distribution. Arbitrary unrounded references with rounding enabled are rejected.

`terminal_probability` supports zero remaining time and zero volatility, returning
the specified deterministic comparison. The formula uses erfc to avoid subtracting
nearly equal normal-tail probabilities. Its payoff is terminal, not averaged.

## Discrete averaged payoff

`sample_offsets_seconds` is an explicit, strictly increasing schedule relative to
the prediction time. All offsets must be between zero and `seconds_to_close`.
The simulator jumps exactly between these observation times. It does not simulate
all intervening seconds unnecessarily or reconstruct an average from minute OHLC.
The arithmetic mean is formed from unrounded simulated observations, then rounded
once using the supplied rule, then compared with the supplied reference.

The synthetic example uses observations at offsets 541..600 inclusive, a 60-point
schedule. This inclusion of the close tick is explicit; it is not automatic rule
approval for KXBTC15M. Archived contract rules, the index feed's convenience average
and historical-data sampling boundaries must be reconciled before live use. The
reference must be the verified opening reference, not the opening spot quote.
No observed partial averaging history is accepted in this milestone. Forecasts
inside the final minute therefore belong to a later implementation.

Terminal and averaged estimates share paths, enabling an analytical sampler check.
When the averaging schedule ends before close, the simulator also samples close for
that check; it does not add this observation to the average. Each path is independent.
There are no antithetic/control-variate draws in the first version.

## Interpreting uncertainty and checking the example

The command prints an estimate, YES count, path count, Bernoulli Monte Carlo
standard error and a 95% Wilson interval for each payoff. Wilson intervals avoid
reporting certainty after zero or all sampled successes. The plug-in standard
error itself can be zero in those cases. Neither interval includes parameter,
index-proxy, model, rounding-approximation or historical-availability uncertainty.

For `examples/digital_contract.json`, the analytical terminal probability is about
0.648257. Simulation results should be nearby within Monte Carlo uncertainty.
A single run need not fall inside a nominal 95% interval; tests use fixed seeds
and a wider tolerance when checking the independently known terminal probability.
Increasing independent paths reduces Monte Carlo standard error at the usual
inverse-square-root rate, not modelling bias. Different payoffs need not agree.

The input JSON explicitly specifies spot, reference, time, volatility and sample
offsets. Optional defaults: annual price drift 0; comparison greater_or_equal;
round_digits null (no rounding); rounding half_up (used only with round_digits);
paths 20000; seed 1729. Unknown fields are rejected. Work is capped at one million
paths and five million path-observation transitions. The CLI never overwrites an
existing output. Simulation seed and actual rule/defaults are included in reports.

The tests also check exact zero-volatility ties, half-up versus half-even rounding,
reference monotonicity with common random paths, deterministic growing-path
averaging, one-sample average equivalence, distributional checks and invalid inputs.
These are mathematical/numerical checks, not evidence of a forecasting advantage.

## Data-access diagnostic

`diagnose` uses allowlisted GET endpoints: BTC market samples, quote candles,
CF `latest_values` and CF `history/values`. The index parser follows the documented
Kalshi data envelope and upstream latest-values/history schemas. Unknown shapes
are blocked rather than recursively searched for arbitrary `value` fields.
Current values must be no more than 30 seconds old or five seconds in the future
relative to receipt; historical samples must fall inside the requested UTC hour.
Duplicates/unordered times and malformed values/flags are rejected. Amendment and
repeat flags are retained, but flagged samples do not make an access probe fail:
research eligibility needs a later explicit policy.

Only selected public contract fields and normalised typed index CSV samples are
persisted. HTTP bodies are hashed in memory but not archived; the digest is an
identity reference, not a reproducible raw archive or authentication guarantee.
No request/response headers, credentials, key paths or arbitrary exception text are
stored. Public rule text is retained for review. Redirects are refused so custom
authentication headers cannot be forwarded to a different URL.

The historical request covers one hour beginning two UTC hours before the current
hour. The provider notes delayed historical publication. A small nonempty response
can pass the *access* check while still having inadequate research coverage; the
report exposes sample counts, times and maximum gaps. Only five recent settled
contracts are sampled, with at most one archived-market fallback. This does not
scan the full universe. An absent active contract near rollover can require rerunning
with a new directory. A settled contract's current final quotes are not old decision
quotes; the separate candle check only tests historical-price retrieval.

The report distinguishes HTTP/network failures, missing credentials, stale values,
upstream errors and empty/unknown payloads. HTTP 503 alone cannot distinguish an
upstream entitlement failure from an upstream outage. Exit 2 indicates a blocked
access sample; local errors use exit 1. No historical features/labels are fitted here.
