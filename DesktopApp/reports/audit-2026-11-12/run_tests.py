"""Run the audit's selected offline suites and keep a UTF-8 review log."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
modules = ["test_ui", "test_review", "test_report", "test_storage", "test_views",
           "test_metrics", "test_app_logging", "test_ux"]
suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
with (Path(__file__).parent / "tests-after.txt").open("w", encoding="utf-8") as stream:
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
print(f"Tests: {result.testsRun}; failures: {len(result.failures)}; errors: {len(result.errors)}")
sys.exit(0 if result.wasSuccessful() else 1)
