import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

import corporate_actions
import updateMarketDataUtilities


class _Timestamp(datetime):
    """Stands in for the tz-aware pandas Timestamp yfinance puts in the index."""


SPGI_SPINOFF_DAY = '2026-07-01'
# read from the table, never hardcoded in logic under test
SPGI_ENTRY = next(e for e in corporate_actions.load_ignored()
                  if e['ticker'] == 'SPGI' and e['action_date'] == SPGI_SPINOFF_DAY)
SPGI_RATIO = SPGI_ENTRY['yf_ratio']


class TestIgnoreList(unittest.TestCase):
    """Yahoo files spin-offs as splits. One applied as a split multiplies every
    share bought before it — SPGI's 1.057 turned a 5-share position into 5.228."""

    def test_seeded_entry_is_a_spinoff_not_a_split(self):
        self.assertEqual(SPGI_ENTRY['action_type'], 'SPINOFF')
        self.assertEqual(SPGI_ENTRY['spun_off_ticker'], 'MBGL')

    def test_spinoff_dropped_whatever_the_date_type(self):
        eastern = timezone(timedelta(hours=-4))
        for date in ('2026-07-01',
                     datetime(2026, 7, 1),
                     _Timestamp(2026, 7, 1, 4, 0, tzinfo=eastern)):
            with self.subTest(date=type(date).__name__):
                kept = corporate_actions.filter_splits(
                    [{'splitDate': date, 'ratioSplit': SPGI_RATIO}], 'SPGI')
                self.assertEqual(kept, [])

    def test_provider_float_noise_still_matches(self):
        kept = corporate_actions.filter_splits(
            [{'splitDate': SPGI_SPINOFF_DAY, 'ratioSplit': SPGI_RATIO + 0.0000003}], 'SPGI')
        self.assertEqual(kept, [])

    def test_genuine_split_on_the_same_ticker_is_kept(self):
        splits = [{'splitDate': '2005-05-18', 'ratioSplit': 2}]
        self.assertEqual(corporate_actions.filter_splits(splits, 'SPGI'), splits)

    def test_different_ratio_on_the_listed_date_is_not_swallowed(self):
        # a new, unreviewed action — only the signed-off ratio is ignored
        splits = [{'splitDate': SPGI_SPINOFF_DAY, 'ratioSplit': 2}]
        self.assertEqual(corporate_actions.filter_splits(splits, 'SPGI'), splits)

    def test_same_ratio_on_another_ticker_is_not_ignored(self):
        splits = [{'splitDate': SPGI_SPINOFF_DAY, 'ratioSplit': SPGI_RATIO}]
        self.assertEqual(corporate_actions.filter_splits(splits, 'MSFT'), splits)

    def test_unlisted_in_band_ratio_passes_through_with_a_warning(self):
        splits = [{'splitDate': '2026-03-04', 'ratioSplit': 1.03}]
        log = io.StringIO()
        with redirect_stdout(log):
            kept = corporate_actions.filter_splits(splits, 'FAKE')
        self.assertEqual(kept, splits)
        output = log.getvalue()
        self.assertIn('WARNING', output)
        for expected in ('FAKE', '2026-03-04', '1.03'):
            self.assertIn(expected, output)

    def test_round_split_ratios_do_not_warn(self):
        log = io.StringIO()
        with redirect_stdout(log):
            corporate_actions.filter_splits(
                [{'splitDate': '2020-08-31', 'ratioSplit': 4},
                 {'splitDate': '2011-06-09', 'ratioSplit': 0.5}], 'FAKE')
        self.assertNotIn('WARNING', log.getvalue())

    def test_suspicious_band_uses_tolerance_not_equality(self):
        self.assertFalse(corporate_actions.is_suspicious(1.0))
        self.assertFalse(corporate_actions.is_suspicious(1.0000001))  # float noise, not an action
        self.assertFalse(corporate_actions.is_suspicious(1.25))
        self.assertFalse(corporate_actions.is_suspicious(0.5))
        self.assertTrue(corporate_actions.is_suspicious(1.057))
        self.assertTrue(corporate_actions.is_suspicious(0.888))

    def test_unreadable_table_ignores_nothing_rather_than_failing(self):
        original = corporate_actions._IGNORE_FILE
        try:
            corporate_actions._IGNORE_FILE = '/nonexistent/ignored_splits.json'
            splits = [{'splitDate': SPGI_SPINOFF_DAY, 'ratioSplit': SPGI_RATIO}]
            with redirect_stdout(io.StringIO()):
                corporate_actions.load_ignored(force_reload=True)  # list is cached per process
                self.assertEqual(
                    corporate_actions.filter_splits(splits, 'SPGI'), splits)
        finally:
            corporate_actions._IGNORE_FILE = original
            corporate_actions.load_ignored(force_reload=True)


class TestGetSplitsIntegration(unittest.TestCase):
    """get_splits is the single funnel every Yahoo write path goes through."""

    def test_spinoff_never_reaches_the_splits_list(self):
        series = {
            datetime(2005, 5, 18): 2.0,
            datetime(2026, 7, 1): SPGI_RATIO,
        }
        with redirect_stdout(io.StringIO()):
            splits = updateMarketDataUtilities.get_splits(series, 'SPGI')
        self.assertEqual([s['ratioSplit'] for s in splits], [2.0])

    def test_series_error_still_degrades_to_empty(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(updateMarketDataUtilities.get_splits(None, 'SPGI'), [])


if __name__ == '__main__':
    unittest.main()
