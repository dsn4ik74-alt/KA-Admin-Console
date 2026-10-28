"""Run unit tests with an in-memory clipboard; never access the OS clipboard."""
from pathlib import Path
import sys
import unittest
import os
import tempfile

root = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(root / 'tests'), str(root)]
from clipboard_sandbox import isolated_test_clipboards

suite = unittest.defaultTestLoader.discover(str(root / 'tests'), pattern=sys.argv[1] if len(sys.argv) > 1 else 'test_*.py')
with tempfile.TemporaryDirectory(prefix='ka-tests-scheduler-') as scheduler_temp, isolated_test_clipboards():
    os.environ['KA_SCHEDULER_DIR'] = scheduler_temp
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
