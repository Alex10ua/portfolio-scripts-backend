import importlib.util
import os
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.append(os.getcwd())

# Loaded from the file directly: test_update_logic.py stubs sibling modules in
# sys.modules at import time, and pytest imports test files in command-line
# order, so a plain import could hand us a MagicMock instead of the real module.
_spec = importlib.util.spec_from_file_location(
    'sec_edgar_provider_real',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sec_edgar_provider.py'),
)
sec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sec)


def _response(status=200, payload=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload or {}
    r.raise_for_status = MagicMock()
    return r


class TestRateLimit(unittest.TestCase):

    def setUp(self):
        sec._last_request_at = 0.0

    def test_requests_are_spaced_by_the_minimum_interval(self):
        with patch.object(sec, '_MIN_INTERVAL', 0.05), \
             patch.object(sec.requests, 'get', return_value=_response()):
            start = time.monotonic()
            for _ in range(4):
                sec._sec_get('https://data.sec.gov/x')
            elapsed = time.monotonic() - start
        # first call is free (last_request_at = 0), the other three each wait
        self.assertGreaterEqual(elapsed, 0.15 - 0.01)

    def test_concurrent_threads_share_one_global_rate(self):
        """Per-thread pacing would let N workers run at N× the intended rate."""
        with patch.object(sec, '_MIN_INTERVAL', 0.04), \
             patch.object(sec.requests, 'get', return_value=_response()):
            start = time.monotonic()
            threads = [threading.Thread(target=lambda: sec._sec_get('https://data.sec.gov/x')) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.16 - 0.01)

    def test_zero_interval_disables_throttle(self):
        with patch.object(sec, '_MIN_INTERVAL', 0):
            start = time.monotonic()
            for _ in range(50):
                sec._throttle()
            self.assertLess(time.monotonic() - start, 0.05)

    def test_403_raises_sec_throttled(self):
        with patch.object(sec, '_MIN_INTERVAL', 0), \
             patch.object(sec.requests, 'get', return_value=_response(403)):
            with self.assertRaises(sec.SecThrottled):
                sec._sec_get('https://data.sec.gov/x')

    def test_429_raises_sec_throttled(self):
        with patch.object(sec, '_MIN_INTERVAL', 0), \
             patch.object(sec.requests, 'get', return_value=_response(429)):
            with self.assertRaises(sec.SecThrottled):
                sec._sec_get('https://data.sec.gov/x')

    def test_404_is_not_an_error(self):
        """A concept the filer never tagged — callers fall through to the next tag."""
        with patch.object(sec, '_MIN_INTERVAL', 0), \
             patch.object(sec.requests, 'get', return_value=_response(404)):
            self.assertIsNone(sec._fetch_concept_series('0000320193', 'us-gaap', 'Assets'))

    def test_throttle_propagates_out_of_fetch_fundamentals(self):
        """Bulk runs rely on this bubbling up so they can abort the whole run."""
        sec._cik_map_cache = {'MSFT': '0000789019'}
        with patch.object(sec, '_MIN_INTERVAL', 0), \
             patch.object(sec.requests, 'get', return_value=_response(429)):
            with self.assertRaises(sec.SecThrottled):
                sec.fetch_fundamentals('MSFT')

    def test_default_spacing_is_well_below_sec_ceiling(self):
        """SEC blocks sustained traffic above 10 req/s — the default leaves wide headroom."""
        self.assertGreaterEqual(sec.SECONDS_PER_REQUEST, 0.1)
        self.assertEqual(sec._MIN_INTERVAL, sec.SECONDS_PER_REQUEST)
        self.assertAlmostEqual(sec.MAX_REQUESTS_PER_SEC, 1.0 / sec.SECONDS_PER_REQUEST)
        self.assertLessEqual(sec.MAX_REQUESTS_PER_SEC, 10)


if __name__ == '__main__':
    unittest.main()
