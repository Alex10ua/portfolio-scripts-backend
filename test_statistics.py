import importlib.util
import os
import sys
import unittest

sys.path.append(os.getcwd())

# Loaded straight from the file rather than via `import updateMarketDataUtilities`:
# test_update_logic.py puts a MagicMock under that name in sys.modules at import
# time, and pytest imports test modules in command-line order, so a plain import
# here would hand us the mock whenever that file is collected first.
_spec = importlib.util.spec_from_file_location(
    'updateMarketDataUtilities_real',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'updateMarketDataUtilities.py'),
)
utils = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(utils)


class TestGetStatistics(unittest.TestCase):

    def _info(self, **overrides):
        base = {
            'lastFiscalYearEnd': 1751241600,     # 2025-06-30
            'mostRecentQuarter': 1774915200,     # 2026-03-31
            'profitMargins': 0.39344,
            'operatingMargins': 0.46329,
            'returnOnAssets': 0.14811,
            'returnOnEquity': 0.34012,
            'totalRevenue': 318270000000,
            'revenuePerShare': 42.84,
            'revenueGrowth': 0.183,
            'grossProfits': 217410000000,
            'ebitda': 184460000000,
            'netIncomeToCommon': 125220000000,
            'trailingEps': 16.68,
            'earningsQuarterlyGrowth': 0.231,
            'totalCash': 78230000000,
            'totalCashPerShare': 10.53,
            'totalDebt': 125430000000,
            'debtToEquity': 30.27,
            'currentRatio': 1.28,
            'bookValue': 55.78,
            'operatingCashflow': 170140000000,
            'freeCashflow': 37010000000,
            'lastSplitFactor': '2:1',
            'recommendationKey': 'buy',
            'fullTimeEmployees': 228000,
        }
        base.update(overrides)
        return base

    def test_extracts_financial_highlights(self):
        stats = utils.get_statistics(self._info(), 'MSFT')

        self.assertEqual(stats['fiscalYearEnd'], '2025-06-30')
        self.assertEqual(stats['mostRecentQuarter'], '2026-03-31')
        self.assertAlmostEqual(stats['profitMargin'], 0.39344)
        self.assertAlmostEqual(stats['operatingMargin'], 0.46329)
        self.assertAlmostEqual(stats['returnOnAssets'], 0.14811)
        self.assertAlmostEqual(stats['returnOnEquity'], 0.34012)
        self.assertEqual(stats['revenue'], 318270000000)
        self.assertAlmostEqual(stats['revenuePerShare'], 42.84)
        self.assertEqual(stats['grossProfit'], 217410000000)
        self.assertEqual(stats['ebitda'], 184460000000)
        self.assertEqual(stats['netIncomeToCommon'], 125220000000)
        self.assertAlmostEqual(stats['dilutedEps'], 16.68)
        self.assertEqual(stats['totalCash'], 78230000000)
        self.assertAlmostEqual(stats['bookValuePerShare'], 55.78)
        self.assertEqual(stats['operatingCashflow'], 170140000000)
        self.assertEqual(stats['freeCashflow'], 37010000000)
        self.assertEqual(stats['lastSplitFactor'], '2:1')
        self.assertEqual(stats['recommendationKey'], 'buy')
        self.assertEqual(stats['fullTimeEmployees'], 228000)

    def test_ratios_are_not_rescaled(self):
        """UI does the ×100 — storing raw keeps a single source of truth."""
        stats = utils.get_statistics({'profitMargins': 0.39344}, 'X')
        self.assertAlmostEqual(stats['profitMargin'], 0.39344)

    def test_missing_fields_are_omitted_not_nulled(self):
        stats = utils.get_statistics({'trailingPE': 38.2}, 'X')
        self.assertEqual(stats['trailingPE'], 38.2)
        self.assertNotIn('profitMargin', stats)
        self.assertNotIn('marketCap', stats)

    def test_empty_info_returns_none(self):
        self.assertIsNone(utils.get_statistics({}, 'X'))
        self.assertIsNone(utils.get_statistics({'unknownKey': 1}, 'X'))

    def test_blank_and_boolean_values_dropped(self):
        # yfinance uses '' for absent strings; a bool would int() to 0/1 silently
        stats = utils.get_statistics(
            {'lastSplitFactor': '', 'recommendationKey': None, 'fullTimeEmployees': True, 'beta': 1.05}, 'X')
        self.assertEqual(list(k for k in stats if k != 'updatedAt'), ['beta'])

    def test_int_kind_truncates_float_input(self):
        stats = utils.get_statistics({'marketCap': 3.05e12, 'floatShares': 7423000000.0}, 'X')
        self.assertEqual(stats['marketCap'], 3050000000000)
        self.assertEqual(stats['floatShares'], 7423000000)
        self.assertIsInstance(stats['floatShares'], int)

    def test_bad_date_does_not_break_extraction(self):
        stats = utils.get_statistics({'lastFiscalYearEnd': 'not-an-epoch', 'beta': 1.2}, 'X')
        self.assertNotIn('fiscalYearEnd', stats)
        self.assertEqual(stats['beta'], 1.2)

    def test_updated_at_stamped_when_any_field_found(self):
        stats = utils.get_statistics({'beta': 1.0}, 'X')
        self.assertRegex(stats['updatedAt'], r'^\d{4}-\d{2}-\d{2}$')

    def test_field_spec_keys_are_unique(self):
        out_keys = [f[0] for f in utils.STATISTICS_FIELDS]
        self.assertEqual(len(out_keys), len(set(out_keys)))


class TestActionDay(unittest.TestCase):
    """A corporate action is a calendar day, not an instant. yfinance dates one at
    local midnight of its exchange, so storing the raw value put every European and
    UK action a day early — and in the previous month when it fell on the 1st."""

    def test_tz_aware_keeps_its_local_day(self):
        from datetime import datetime, timezone, timedelta
        amsterdam = timezone(timedelta(hours=2))
        self.assertEqual(
            utils.action_day(datetime(2009, 6, 1, 0, 0, tzinfo=amsterdam)), '2009-06-01')

    def test_tz_aware_west_of_utc_keeps_its_local_day(self):
        from datetime import datetime, timezone, timedelta
        new_york = timezone(timedelta(hours=-4))
        self.assertEqual(
            utils.action_day(datetime(2025, 10, 1, 0, 0, tzinfo=new_york)), '2025-10-01')

    def test_naive_datetime(self):
        from datetime import datetime
        self.assertEqual(utils.action_day(datetime(2024, 2, 9)), '2024-02-09')

    def test_string_passes_through_as_a_day(self):
        self.assertEqual(utils.action_day('2024-02-09T00:00:00Z'), '2024-02-09')

    def test_dividends_and_splits_are_dated_by_day(self):
        from datetime import datetime, timezone, timedelta
        frankfurt = timezone(timedelta(hours=2))
        self.assertEqual(
            utils.get_dividends(_Series({datetime(2025, 5, 5, 0, 0, tzinfo=frankfurt): 2.25}), 'BAS.DE'),
            [{'dividendDate': '2025-05-05', 'dividendAmount': 2.25}])
        self.assertEqual(
            utils.get_splits(_Series({datetime(2018, 5, 3, 0, 0, tzinfo=frankfurt): 2.0}), 'BESI.AS'),
            [{'splitDate': '2018-05-03', 'ratioSplit': 2.0}])


class _Series:
    """Just the .items() get_dividends/get_splits use, without pulling in pandas."""

    def __init__(self, mapping):
        self._mapping = mapping

    def items(self):
        return self._mapping.items()


if __name__ == '__main__':
    unittest.main()
