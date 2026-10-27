"""Physical-measure GBM probabilities; explicit discrete averaging, no trading.

Volatility and price drift are annual, with a 365-day year. The caller supplies
the reference and settlement schedule; this module does not interpret live rules.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_HALF_UP, localcontext
import math
import random

from . import __version__
from .numbers import decimal

SECONDS_PER_YEAR = 365 * 24 * 60 * 60
ROUNDING = {"half_up": ROUND_HALF_UP, "half_even": ROUND_HALF_EVEN}


def finite(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


@dataclass(frozen=True)
class SettlementRule:
    reference: Decimal
    comparison: str = "greater_or_equal"
    round_digits: int | None = None
    rounding: str = "half_up"

    def __post_init__(self):
        reference = decimal(self.reference)
        if reference <= 0 or not math.isfinite(float(reference)):
            raise ValueError("reference must be positive and representable as float")
        object.__setattr__(self, "reference", reference)
        if self.comparison not in ("greater_or_equal", "greater"):
            raise ValueError("comparison must be greater_or_equal or greater")
        if self.rounding not in ROUNDING:
            raise ValueError("rounding must be half_up or half_even")
        digits = self.round_digits
        if digits is not None:
            if isinstance(digits, bool) or not isinstance(digits, int) or not 0 <= digits <= 8:
                raise ValueError("round_digits must be null or an integer from 0 to 8")
            if reference != self.rounded(reference):
                raise ValueError("reference must already be rounded to round_digits")

    def rounded(self, value):
        value = decimal(value)
        if self.round_digits is None:
            return value
        with localcontext() as context:
            context.prec = max(28, len(value.as_tuple().digits) + abs(value.adjusted()) + 12)
            return value.quantize(Decimal(1).scaleb(-self.round_digits), rounding=ROUNDING[self.rounding])

    def yes(self, value):
        value = self.rounded(value)
        return value >= self.reference if self.comparison == "greater_or_equal" else value > self.reference

    def continuous_cutoff(self):
        """Rounding-boundary ties have probability zero for nondegenerate GBM."""
        if self.round_digits is None:
            return float(self.reference)
        half_tick = Decimal(5).scaleb(-self.round_digits - 1)
        return float(self.reference + (-half_tick if self.comparison == "greater_or_equal" else half_tick))


def parameters(spot, seconds_to_close, annual_volatility, annual_price_drift):
    spot_decimal = decimal(spot)
    spot = finite(spot_decimal, "spot")
    seconds = finite(seconds_to_close, "seconds_to_close")
    volatility = finite(annual_volatility, "annual_volatility")
    drift = finite(annual_price_drift, "annual_price_drift")
    if spot <= 0 or seconds < 0 or volatility < 0:
        raise ValueError("spot must be positive; time and volatility must be nonnegative")
    return spot_decimal, spot, seconds, volatility, drift


def terminal_probability(spot, rule, seconds_to_close, annual_volatility, annual_price_drift=0):
    """P(rounded I_T compares to K) under dI/I = mu dt + sigma dW.

    This is an outcome probability, not a discounted risk-neutral option price.
    """
    exact_spot, price, seconds, sigma, mu = parameters(
        spot, seconds_to_close, annual_volatility, annual_price_drift)
    tau = seconds / SECONDS_PER_YEAR
    if seconds == 0 or sigma == 0:
        if tau == 0 or mu == 0:
            return float(rule.yes(exact_spot))
        try:
            terminal = price * math.exp(mu * tau)
        except OverflowError:
            raise ValueError("Model parameters overflow the price calculation") from None
        if not math.isfinite(terminal) or terminal <= 0:
            raise ValueError("Model parameters overflow/underflow the price calculation")
        return float(rule.yes(str(terminal)))
    cutoff = rule.continuous_cutoff()
    if cutoff <= 0:
        return 1.0
    scale = sigma * math.sqrt(tau)
    if not math.isfinite(scale) or scale == 0:
        raise ValueError("Model parameters overflow/underflow the probability calculation")
    z = (math.log(price) - math.log(cutoff) + (mu - sigma * sigma / 2) * tau) / scale
    if math.isnan(z):
        raise ValueError("Model parameters overflow the probability calculation")
    return 0.5 * math.erfc(-z / math.sqrt(2))


def binomial_summary(successes, paths):
    p, z = successes / paths, 1.959963984540054
    denominator = 1 + z * z / paths
    center = (p + z * z / (2 * paths)) / denominator
    half = z * math.sqrt(p * (1 - p) / paths + z * z / (4 * paths * paths)) / denominator
    return {"probability": p, "yes_paths": successes, "paths": paths,
            "standard_error": math.sqrt(p * (1 - p) / paths),
            "wilson_95_interval": [max(0.0, center - half), min(1.0, center + half)]}


def simulate(spot, rule, seconds_to_close, annual_volatility, sample_offsets_seconds,
             *, annual_price_drift=0, paths=20000, seed=1729):
    """Exact GBM transitions at supplied offsets, then discrete arithmetic mean.

    Paths are independent, not antithetic. An extra terminal observation is drawn
    when needed, allowing the analytic terminal benchmark to check the sampler.
    No interpolation of historical candles or invented observed samples occurs.
    """
    exact_spot, price, seconds, sigma, mu = parameters(
        spot, seconds_to_close, annual_volatility, annual_price_drift)
    if isinstance(paths, bool) or not isinstance(paths, int) or not 2 <= paths <= 1000000:
        raise ValueError("paths must be an integer from 2 to 1000000")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    if not isinstance(sample_offsets_seconds, (list, tuple)) or not sample_offsets_seconds:
        raise ValueError("Supply a nonempty explicit sample_offsets_seconds schedule")
    offsets = [finite(s, "sample offset") for s in sample_offsets_seconds]
    if (len(offsets) > 3600 or offsets[0] < 0 or offsets[-1] > seconds
            or any(a >= b for a, b in zip(offsets, offsets[1:]))):
        raise ValueError("Sample offsets must be strictly increasing, between 0 and seconds_to_close")
    timeline = sorted(set(offsets + [seconds]))
    if paths * len(timeline) > 5000000:
        raise ValueError("Simulation budget exceeds 5000000 path-observation transitions")
    intervals, previous = [], 0
    for offset in timeline:
        dt = (offset - previous) / SECONDS_PER_YEAR
        intervals.append(((mu - sigma * sigma / 2) * dt, sigma * math.sqrt(dt), offset in offsets))
        previous = offset
    if any(not math.isfinite(a) or not math.isfinite(b) for a, b, _ in intervals):
        raise ValueError("Model parameters overflow the simulation")
    rng = random.Random(seed)
    terminal_yes = average_yes = 0
    for _ in range(paths):
        current, samples = price, []
        for location, scale, take_sample in intervals:
            try:
                current *= math.exp(location + (scale * rng.gauss(0, 1) if scale else 0))
            except OverflowError:
                raise ValueError("Model parameters overflow the simulation") from None
            if not math.isfinite(current) or current <= 0:
                raise ValueError("Model parameters overflow/underflow the simulation")
            if take_sample:
                samples.append(current)
        # Preserve exact input ties in the constant-path case.
        terminal = exact_spot if sigma == mu == 0 or seconds == 0 else Decimal(str(current))
        average = exact_spot if sigma == mu == 0 or seconds == 0 else Decimal(str(math.fsum(samples) / len(samples)))
        terminal_yes += rule.yes(terminal)
        average_yes += rule.yes(average)
    return {"terminal": binomial_summary(terminal_yes, paths),
            "arithmetic_average": binomial_summary(average_yes, paths)}


def benchmark(document):
    required = {"spot", "reference", "seconds_to_close", "annual_volatility", "sample_offsets_seconds"}
    optional = {"annual_price_drift", "comparison", "round_digits", "rounding", "paths", "seed", "input_origin"}
    if not isinstance(document, dict) or not required <= document.keys() or document.keys() - required - optional:
        raise ValueError("Invalid digital input fields; see examples/digital_contract.json")
    rule = SettlementRule(document["reference"], document.get("comparison", "greater_or_equal"),
                          document.get("round_digits"), document.get("rounding", "half_up"))
    mu, paths, seed = document.get("annual_price_drift", 0), document.get("paths", 20000), document.get("seed", 1729)
    analytic = terminal_probability(document["spot"], rule, document["seconds_to_close"], document["annual_volatility"], mu)
    simulated = simulate(document["spot"], rule, document["seconds_to_close"], document["annual_volatility"],
                         document["sample_offsets_seconds"], annual_price_drift=mu, paths=paths, seed=seed)
    return {"schema_version": 1, "package_version": __version__, "model": "physical_gbm_constant_parameters",
            "interpretation": "Model calculation only; not a fitted forecast, live rule certification or trading result.",
            "inputs": document, "seconds_per_year": SECONDS_PER_YEAR, "simulation_seed": seed,
            "settlement_rule": {"reference": str(rule.reference), "comparison": rule.comparison,
                                "round_digits": rule.round_digits, "rounding": rule.rounding},
            "annual_price_drift": finite(mu, "annual_price_drift"),
            "terminal_analytic_probability": analytic, "simulation": simulated,
            "terminal_simulation_minus_analytic": simulated["terminal"]["probability"] - analytic,
            "uncertainty": "Intervals measure independent-path Monte Carlo error only, not model or parameter uncertainty."}
