# Research specification — version 0.1

Working title: **From Price Inconsistency to Executable Arbitrage:
Optimisation and Execution Risk on Kalshi**.

## Research question

How much apparent payoff-consistency arbitrage survives actual order-book depth,
effective fees, receipt-time information and non-atomic execution?

This is a replication-and-extension project. Logical arbitrage detection has
prior literature. Our intended contribution is a reproducible measurement of
the gap between quoted, simulated and executed results under explicit
assumptions. We have not demonstrated that opportunities exist in this universe
or that a strategy is profitable.

## First universe

Begin with greater-than CPI contracts for the **same reference month**. These
provide readable nested thresholds and a link to a recognisable market event.
This is chosen for model validation, not because we have evidence of superior
alpha. Sparse updates or long capital lockup may make another Kalshi family a
better execution experiment later.

Do not compare different months as if their outcomes were logically nested.
Do not treat every market under the same event as mutually exclusive. A
threshold family generally has many YES outcomes simultaneously.

## First payoff proof

Let Y_L = 1{R > L}, Y_H = 1{R > H}, with L < H and exactly the same observed R.
Buy q YES_L and q NO_H. Ordinary binary feasible states and payouts:

| YES_L | YES_H | Total payout |
|---|---|---|
| 0 | 0 | q |
| 1 | 0 | 2q |
| 1 | 1 | q |

The impossible state (0,1) is excluded only if the full contract terms justify
the implication. Acquisition cost is the sum of the consumed ask ladders, not
q times the top quote when depth is insufficient. Minimum conditional surplus
is q minus acquisition cost minus fees and other applicable costs.

Buying YES_H and NO_L can pay zero between thresholds; do not reverse the legs.
Strict inequality and single-decimal published observations matter at equality.

## Contract-review record

For each proposed relationship, retain the exact market and event identifiers,
underlying reference period, official contract-terms links, metadata hashes,
review date and reasoning. Check the source, revisions, rounding, strict
boundaries, publication delay, extension, cancellation, void, nonbinary and
rulebook settlement provisions. Read the linked full terms, not just titles.

The starter proposes relations with review_status=unreviewed. Editing that JSON
does not activate a scanner or approve a trade. A future validator must reject
unknown settlement states and changed rules; at this stage, no live relation is
certified. If unusual resolutions can break the binary implication, add the
valid payout scenarios or exclude the relation from guaranteed-profit claims.

## General mathematical target

For long positions x in YES/NO instruments, let A encode payouts across the
verified feasible states. Let C(x) include full depth-dependent acquisition
cost, fees and other relevant charges. Solve:

    maximise over x in X: min over states s of (A_s x - C(x)).

X incorporates available depth, cash, lot sizes, position limits and optionally
leg-count limits. For n strictly ordered thresholds, the ordinary binary
settlement states form n+1 monotone regions. Enumerating those regions gives a
small master LP for continuous, piecewise-linear costs. Fee rounding, lot sizes
and fixed/leg-count costs can require a discrete formulation. Do not claim the
current starter implements that optimiser: it implements the two-leg accounting
primitive and raw data foundation.

## Fees: verify before measuring net opportunities

Archive effective series fees, event overrides and scheduled changes for the
observation time. The July 7, 2026 published schedule uses a quadratic general
taker model with a multiplier; the API also describes per-fill model rounding,
balance alignment and an order-level rounding accumulator. Member type matters.
Current API metadata is not a historical fee schedule.

Do not hardcode old integer-cent examples or assume one depth level equals one
fill. A verified model must reconcile against demo/actual fill-ledger behaviour
before being used to label net executable opportunities. For now, real capture
reports no net opportunity counts. The synthetic example accepts an explicitly
assumed total fee reserve, not an inferred exchange fee.

## Evaluation design

Pre-register a fixed family and observation window before searching many
alternatives. Keep the first capture as development data; reserve later days
for forward evaluation once the implementation and rules are frozen.

Compare, in order:

1. Apparent inconsistencies in displayed quotes.
2. Full-depth cost, verified fees and capital feasibility.
3. Receipt-time replay with explicit latency and quote consumption.
4. Forward paper orders/signals and operational reconciliation.
5. Small, supervised live execution, if access and risk limits permit.

Report distinct opportunity episodes rather than adding every repeated sampled
quote. Episode merging must account for sampling gaps; polling only identifies
sampled observations, not exact start/end times. Do not repeatedly reuse the
same displayed depth to multiply theoretical profit.

Metrics: valid data coverage, candidate count, distinct sampled episodes,
quantity-dependent minimum surplus, capital locked and capital-days, time to
settlement, quote survival, completed/partial/failed hedges, completion/unwind
cost, realised P&L and differences between stages. Holding-period-normalised
returns must use explicit capital and reuse assumptions; avoid extrapolating
a handful of tiny trades into an annual Sharpe ratio.

## First-week completion criteria

- Run the offline example and explain its three payoff states.
- Discover one event and review at least three candidate relations in its full
  rules. Complete review can legitimately reject all candidates.
- Collect an uninterrupted short sample, then a longer observation period.
- Inspect missing responses, empty sides, timestamps and cross-market skew.
- Record the fee-model assumptions still requiring reconciliation.
- Decide whether to continue this family using observed liquidity and access,
  without requiring that the pilot show positive P&L.

## Division of work

Assistant: implementation, mathematical derivations, meaningful accounting
tests, research critique, data analysis and report preparation.

User: local setup, understanding and independently explaining the payoff proof,
helping review contract terms, running persistent collection, confirming account
eligibility and later controlling credentials, funding and deployment.

Final portfolio evidence should include a clear derivation, a reproducible
capture/replay pipeline, comparison to simple baselines, honest negative results,
and explanation of the largest model-to-execution discrepancies.
