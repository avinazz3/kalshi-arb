"""Run offline tests and fail on resource leaks."""
import gc
from pathlib import Path
import sys
import unittest
import warnings


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run_checks(suite):
    with warnings.catch_warnings(record=True) as observed:
        warnings.simplefilter("always", ResourceWarning)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        gc.collect()
    resources = [w for w in observed if issubclass(w.category, ResourceWarning)]
    for warning in resources:
        sys.stderr.write(warnings.formatwarning(warning.message, warning.category, warning.filename, warning.lineno))
    if resources:
        print(f"Cleanup checks failed: {len(resources)} resource warnings", file=sys.stderr)
    else:
        print("Cleanup checks passed: no resource warnings", file=sys.stderr)
    return result.wasSuccessful() and not resources


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    raise SystemExit(0 if run_checks(suite) else 1)
