"""Past-only minute-return volatility estimators for retrospective research."""
import math

from .digital import SECONDS_PER_YEAR


def estimate(prices, half_life=15):
    """Return annual rolling and finite-window EWMA second-moment estimates.

    Prices are consecutive one-minute samples, oldest first. EWMA is normalized
    over the supplied finite window; no future observations are accepted here.
    """
    if len(prices) < 3 or isinstance(half_life, bool) or not isinstance(half_life, (int, float)) or not math.isfinite(half_life) or half_life <= 0:
        raise ValueError('Need at least three prices and a positive finite half-life')
    values = [float(x) for x in prices]
    if any(not math.isfinite(x) or x <= 0 for x in values):
        raise ValueError('Prices must be positive and finite')
    returns = [(math.log(b) - math.log(a)) for a, b in zip(values, values[1:])]
    if any(not math.isfinite(r) for r in returns):
        raise ValueError('Returns must be finite')
    weights = [2 ** (-age / half_life) for age in range(len(returns))]
    rolling = math.fsum(r * r for r in returns) / len(returns)
    ewma = math.fsum(w * r * r for w, r in zip(weights, reversed(returns))) / math.fsum(weights)
    annualization = SECONDS_PER_YEAR / 60
    return {'rolling_annual_volatility': math.sqrt(rolling * annualization),
            'ewma_annual_volatility': math.sqrt(ewma * annualization),
            'return_count': len(returns), 'half_life_minutes': half_life}
