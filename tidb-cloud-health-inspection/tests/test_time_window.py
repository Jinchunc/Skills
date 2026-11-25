import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dedicated_health_inspection.py"
sys.path.insert(0, str(SCRIPT.parent))
# Load under a private module name so this test never clobbers the canonical
# `dedicated_health_inspection` entry in sys.modules (which other tests patch).
spec = importlib.util.spec_from_file_location("dedicated_health_inspection_timewindow", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class TimeWindowTest(unittest.TestCase):
    def test_explicit_window(self):
        window = module.resolve_window(
            "2026-11-19T00:00:00+00:00",
            "2026-11-19T06:00:00+00:00",
            None,
            "UTC",
        )
        self.assertEqual(window.seconds, 6 * 60 * 60)

    def test_one_day_is_a_relative_window(self):
        window = module.resolve_window(
            None,
            "2026-11-20T00:00:00+00:00",
            "1d",
            "UTC",
        )
        self.assertEqual(window.seconds, 24 * 60 * 60)
        self.assertEqual(window.start.isoformat(), "2026-11-19T00:00:00+00:00")

    def test_window_is_required(self):
        with self.assertRaisesRegex(ValueError, "provide either"):
            module.resolve_window(None, None, None, "UTC")

    def test_end_must_follow_start(self):
        with self.assertRaisesRegex(ValueError, "later than"):
            module.resolve_window(
                "2026-11-20T00:00:00+00:00",
                "2026-11-19T00:00:00+00:00",
                None,
                "UTC",
            )


if __name__ == "__main__":
    unittest.main()
