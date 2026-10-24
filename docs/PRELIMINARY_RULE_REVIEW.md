# Preliminary CPI review — not a certified trading relation

The construction run selected event `KXCPI-26OCT` and four threshold contracts:
`T0.1`, `T0.2`, `T0.3`, `T0.4`. These identifiers are dated examples; rerun
discovery before starting a new collection window.

Three proposals to review first:

| Lower threshold | Higher threshold | Proposed implication |
|---|---|---|
| 0.1% | 0.2% | YES at 0.2 implies YES at 0.1 |
| 0.2% | 0.3% | YES at 0.3 implies YES at 0.2 |
| 0.3% | 0.4% | YES at 0.4 implies YES at 0.3 |

Under ordinary published-data settlement, the market metadata and linked terms
support the same reference month, strict greater-than comparisons and a
single-decimal monthly CPI observation. The linked terms identify seasonally
adjusted CPI-U from BLS and exclude revisions released after expiration. This
supports the ordinary binary payoff argument; it is not a complete exceptional
settlement review.

An unresolved detail illustrates why this step matters: the retrieved listing
metadata describes a government-shutdown extension of up to six months after
the shutdown ends, while the linked generic contract-terms PDF describes a
different latest-expiration framework and a fallback calculation. The linked
original product-certification filing dates to 2021. We have not reconciled
the applicable amendments or current rulebook contingencies. None of these
three proposals is marked approved.

Next review step: identify the effective amendments/rulebook and verify that
the same observation and exceptional payout treatment applies to each selected
contract. Record any possible nonbinary payout states explicitly rather than
assuming ordinary threshold outcomes cover every legal resolution.

Fee review is also incomplete. KXCPI series metadata returned a maker-fee-bearing
quadratic fee type and multiplier 1. This alone does not validate a complete
effective fee model: event overrides, scheduled changes, membership precision
and per-fill rounding/accumulation still need to be handled.

Evidence: the public responses in `examples/pilot/discovery/requests.sqlite`,
the exported market/event JSON in that directory, and the official terms and
fee URLs in `SOURCES.md`. The arithmetic demo is synthetic and separate from
this recorded evidence.
