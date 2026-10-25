"""Run offline tests and fail on resource leaks or unexpected asyncio errors."""
import gc
import logging
from pathlib import Path
import sys
import unittest
import warnings


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run_checks(suite):
    errors = []

    class AsyncErrors(logging.Handler):
        def emit(self, record):
            errors.append(self.format(record))

    logger = logging.getLogger("asyncio")
    handler = AsyncErrors(level=logging.ERROR)
    logger.addHandler(handler)
    try:
        with warnings.catch_warnings(record=True) as observed:
            warnings.simplefilter("always", ResourceWarning)
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            # sqlite connections and HTTPError objects may only warn when collected.
            gc.collect()
    finally:
        logger.removeHandler(handler)
    resources = [w for w in observed if issubclass(w.category, ResourceWarning)]
    for warning in resources:
        sys.stderr.write(warnings.formatwarning(warning.message, warning.category, warning.filename, warning.lineno))
    for error in errors:
        print("Unexpected asyncio error:", error, file=sys.stderr)
    if resources or errors:
        print(f"Cleanup checks failed: {len(resources)} resource warnings, {len(errors)} asyncio errors", file=sys.stderr)
    else:
        print("Cleanup checks passed: no resource warnings or unexpected asyncio errors", file=sys.stderr)
    return result.wasSuccessful() and not resources and not errors


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    raise SystemExit(0 if run_checks(suite) else 1)
