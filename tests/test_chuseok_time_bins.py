"""CTM windows ending at midnight belong to the preceding observed hour."""
import sys
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from validate_ctm_speed import record_end_hours


def test_record_end_hours_covers_each_hour_without_midnight_wrap():
    ends = np.arange(5, 1441, 5, dtype=float)
    hours = record_end_hours(ends)
    np.testing.assert_array_equal(hours, np.repeat(np.arange(24), 12))
    np.testing.assert_array_equal(record_end_hours([5, 60, 65, 120, 1440]), [0, 0, 1, 1, 23])
