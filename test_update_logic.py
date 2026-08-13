import unittest
from unittest.mock import MagicMock, patch
import sys
import os
import concurrent.futures

# Add current directory to path to import the module
sys.path.append(os.getcwd())

# Mock imports before importing the module
sys.modules['pymongo'] = MagicMock()
sys.modules['yfinance'] = MagicMock()
sys.modules['updateMarketDataUtilities'] = MagicMock()
sys.modules['massive_provider'] = MagicMock()
mock_flask = MagicMock()
def mock_route(*args, **kwargs):
    def decorator(f):
        return f
    return decorator
mock_flask.Flask.return_value.route = mock_route
mock_flask.jsonify = MagicMock(side_effect=lambda x: x)
mock_flask.request = MagicMock()
sys.modules['flask'] = mock_flask

import updateMarketData


class TestUpdateMarketData(unittest.TestCase):

    def setUp(self):
        updateMarketData.collection.reset_mock(side_effect=True, return_value=True)
        updateMarketData.tickers_collection.reset_mock(side_effect=True, return_value=True)
        updateMarketData.shares_history_collection.reset_mock(side_effect=True, return_value=True)
        # Reset debounce timers
        updateMarketData.debounce_timers = {"yahoo": None, "massive": None}

    def _make_mock_provider(self, name='mock_name', price=100.0):
        def provider(ticker):
            return {
                'name': name,
                'price': price,
                'priceYesterday': price,
                'yearlyDividend': '',
                'lastDividendPayment': '',
                'dividends': [],
                'splits': [],
                'country': '',
                'sector': '',
                'industry': '',
                'updatedAt': None,
            }
        return provider

    def test_process_ticker_success(self):
        provider = self._make_mock_provider()
        ticker_res, op, error = updateMarketData.process_ticker('TEST', provider)

        self.assertEqual(ticker_res, 'TEST')
        self.assertIsNone(error)
        self.assertIsNotNone(op)

    def test_process_ticker_failure(self):
        def failing_provider(ticker):
            raise Exception("Fetch error")

        ticker_res, op, error = updateMarketData.process_ticker('FAIL', failing_provider)

        self.assertEqual(ticker_res, 'FAIL')
        self.assertIsNone(op)
        self.assertEqual(error, "Fetch error")

    @patch('concurrent.futures.ThreadPoolExecutor')
    def test_run_task_batch_execution(self, mock_executor_cls):
        mock_cursor = [{'ticker': 'AAPL'}, {'ticker': 'GOOGL'}]
        updateMarketData.tickers_collection.find.return_value = mock_cursor

        mock_executor = MagicMock()
        mock_executor_cls.return_value.__enter__.return_value = mock_executor

        f1 = MagicMock()
        f1.result.return_value = ('AAPL', MagicMock(), None)
        f2 = MagicMock()
        f2.result.return_value = ('GOOGL', MagicMock(), None)

        mock_executor.submit.side_effect = [f1, f2]

        provider = self._make_mock_provider()
        with patch('concurrent.futures.as_completed', return_value=[f1, f2]):
            updateMarketData.run_task(provider, 'yahoo')

        self.assertTrue(updateMarketData.collection.bulk_write.called)
        self.assertTrue(updateMarketData.tickers_collection.bulk_write.called)

        args, _ = updateMarketData.collection.bulk_write.call_args
        self.assertEqual(len(args[0]), 2)

    def test_schedule_batch_yahoo_timer(self):
        provider = self._make_mock_provider()
        with patch('threading.Timer') as mock_timer:
            mock_timer_instance = MagicMock()
            mock_timer.return_value = mock_timer_instance
            updateMarketData.schedule_batch('yahoo', provider)
            args, _ = mock_timer.call_args
            self.assertEqual(args[0], 10)
            self.assertEqual(args[1], updateMarketData.run_task)
            mock_timer_instance.start.assert_called_once()

    def test_schedule_batch_massive_timer(self):
        provider = self._make_mock_provider()
        with patch('threading.Timer') as mock_timer:
            mock_timer_instance = MagicMock()
            mock_timer.return_value = mock_timer_instance
            updateMarketData.schedule_batch('massive', provider)
            args, _ = mock_timer.call_args
            self.assertEqual(args[0], 10)
            self.assertEqual(args[1], updateMarketData.run_task)
            mock_timer_instance.start.assert_called_once()

    def test_schedule_batch_providers_independent(self):
        """Cancelling yahoo timer must not affect massive timer."""
        yahoo_provider = self._make_mock_provider()
        massive_provider = self._make_mock_provider()

        yahoo_timer = MagicMock()
        massive_timer = MagicMock()
        timer_instances = [yahoo_timer, massive_timer]

        with patch('threading.Timer') as mock_timer:
            mock_timer.side_effect = timer_instances
            updateMarketData.schedule_batch('yahoo', yahoo_provider)
            updateMarketData.schedule_batch('massive', massive_provider)

        # Both timers started
        yahoo_timer.start.assert_called_once()
        massive_timer.start.assert_called_once()
        # Yahoo timer was not cancelled (no prior yahoo timer existed)
        yahoo_timer.cancel.assert_not_called()


class TestMergeList(unittest.TestCase):
    """Dedup must be by calendar day: Mongo returns naive datetimes, yfinance
    yields tz-aware Timestamps, Finnhub/Massive use strings — raw values never
    compare equal, which used to double every dividend/split on each update."""

    def test_naive_vs_tz_aware_same_day_dedups(self):
        from datetime import datetime, timezone, timedelta
        ny = timezone(timedelta(hours=-4))
        existing = [{'dividendDate': datetime(2024, 3, 14, 4, 0), 'dividendAmount': 0.485}]
        new = [{'dividendDate': datetime(2024, 3, 14, 0, 0, tzinfo=ny), 'dividendAmount': 0.485}]
        merged = updateMarketData._merge_list(existing, new, 'dividendDate')
        self.assertEqual(len(merged), 1)
        # new entry wins
        self.assertEqual(merged[0]['dividendDate'].tzinfo, ny)

    def test_string_vs_datetime_same_day_dedups(self):
        from datetime import datetime
        existing = [{'dividendDate': datetime(2024, 2, 9), 'dividendAmount': 0.24}]
        new = [{'dividendDate': '2024-02-09', 'dividendAmount': 0.25}]
        merged = updateMarketData._merge_list(existing, new, 'dividendDate')
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['dividendAmount'], 0.25)

    def test_existing_duplicates_collapse(self):
        from datetime import datetime
        existing = [
            {'dividendDate': datetime(2024, 3, 14, 4, 0), 'dividendAmount': 0.485},
            {'dividendDate': datetime(2024, 3, 14, 4, 0), 'dividendAmount': 0.485},
        ]
        merged = updateMarketData._merge_list(existing, [], 'dividendDate')
        self.assertEqual(len(merged), 1)

    def test_different_days_kept(self):
        existing = [{'splitDate': '2020-08-31', 'ratioSplit': 4}]
        new = [{'splitDate': '2024-06-10', 'ratioSplit': 10}]
        merged = updateMarketData._merge_list(existing, new, 'splitDate')
        self.assertEqual(len(merged), 2)

    def test_missing_key_skipped(self):
        merged = updateMarketData._merge_list(
            [{'dividendAmount': 1}], [{'dividendDate': '2024-01-02', 'dividendAmount': 2}], 'dividendDate')
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['dividendAmount'], 2)


class _FakeFrame:
    """Minimal stand-in for a yfinance history frame: columns + iterrows()."""

    def __init__(self, columns, rows):
        self.columns = columns
        self._rows = rows

    def iterrows(self):
        return iter(self._rows)


class TestPriceHistoryEntries(unittest.TestCase):
    """rawPrice is the close as quoted; price stays the total-return series every
    existing consumer reads. Yield history divides nominal dividends by rawPrice —
    using the adjusted close there reads past yields far too rich."""

    def test_adjusted_frame_splits_the_two_closes(self):
        from datetime import datetime
        frame = _FakeFrame(['Close', 'Adj Close'], [
            (datetime(2024, 1, 31), {'Close': 100.0, 'Adj Close': 92.5}),
        ])
        entries = updateMarketData.history_entries(frame)
        self.assertEqual(entries, [{'date': '2024-01-31', 'price': 92.5, 'rawPrice': 100.0}])

    def test_auto_adjusted_frame_carries_the_same_value_in_both(self):
        from datetime import datetime
        frame = _FakeFrame(['Close'], [(datetime(2024, 1, 31), {'Close': 92.5})])
        entries = updateMarketData.history_entries(frame)
        self.assertEqual(entries, [{'date': '2024-01-31', 'price': 92.5, 'rawPrice': 92.5}])

    def test_monthly_fold_keeps_last_close_of_month_with_raw(self):
        daily = [
            {'date': '2024-01-30', 'price': 90.0, 'rawPrice': 98.0},
            {'date': '2024-01-31', 'price': 92.5, 'rawPrice': 100.0},
            {'date': '2024-02-29', 'price': 95.0, 'rawPrice': 103.0},
        ]
        self.assertEqual(updateMarketData.monthly_from_daily(daily), [
            {'date': '2024-01', 'price': 92.5, 'rawPrice': 100.0},
            {'date': '2024-02', 'price': 95.0, 'rawPrice': 103.0},
        ])

    def test_nan_close_is_dropped(self):
        # Yahoo returns a NaN bar for a session with no close yet. A NaN reaches
        # Mongo as a Double, and BigDecimal has no NaN — the Java side then fails
        # to read the whole document, not just that point.
        from datetime import datetime
        frame = _FakeFrame(['Close', 'Adj Close'], [
            (datetime(2026, 8, 11), {'Close': 385.04, 'Adj Close': 385.04}),
            (datetime(2026, 8, 12), {'Close': float('nan'), 'Adj Close': float('nan')}),
        ])
        entries = updateMarketData.history_entries(frame)
        self.assertEqual([e['date'] for e in entries], ['2026-08-11'])

    def test_monthly_fold_drops_nan_prices(self):
        daily = [
            {'date': '2026-08-11', 'price': 385.04, 'rawPrice': 385.04},
            {'date': '2026-08-12', 'price': float('nan'), 'rawPrice': float('nan')},
        ]
        self.assertEqual(updateMarketData.monthly_from_daily(daily),
                         [{'date': '2026-08', 'price': 385.04, 'rawPrice': 385.04}])

    def test_monthly_fold_of_legacy_entries_has_no_raw_key(self):
        daily = [{'date': '2019-06-28', 'price': 41.2}]
        self.assertEqual(updateMarketData.monthly_from_daily(daily), [{'date': '2019-06', 'price': 41.2}])


class TestRecordSharesHistory(unittest.TestCase):

    def setUp(self):
        self.coll = updateMarketData.shares_history_collection
        self.coll.reset_mock(side_effect=True, return_value=True)

    def test_none_shares_skips(self):
        updateMarketData.record_shares_history('AAPL', None)
        self.coll.find_one.assert_not_called()
        self.coll.update_one.assert_not_called()

    def test_first_entry_appended(self):
        self.coll.find_one.return_value = None
        updateMarketData.record_shares_history('AAPL', 1000)
        args, kwargs = self.coll.update_one.call_args
        self.assertEqual(args[0], {'_id': 'AAPL'})
        self.assertEqual(args[1]['$push']['history']['shares'], 1000)
        self.assertTrue(kwargs.get('upsert'))

    def test_unchanged_value_not_appended(self):
        self.coll.find_one.return_value = {'history': [{'date': '2026-01-01', 'shares': 1000}]}
        updateMarketData.record_shares_history('AAPL', 1000)
        self.coll.update_one.assert_not_called()

    def test_changed_value_appended(self):
        self.coll.find_one.return_value = {'history': [{'date': '2026-01-01', 'shares': 1000}]}
        updateMarketData.record_shares_history('AAPL', 900)
        args, _ = self.coll.update_one.call_args
        self.assertEqual(args[1]['$push']['history']['shares'], 900)

    def test_same_day_change_replaces_entry(self):
        from datetime import datetime
        today = datetime.now().strftime('%Y-%m-%d')
        self.coll.find_one.return_value = {'history': [{'date': today, 'shares': 1000}]}
        updateMarketData.record_shares_history('AAPL', 900)
        calls = self.coll.update_one.call_args_list
        self.assertEqual(len(calls), 2)
        # first call pops today's entry, second pushes the corrected value
        self.assertEqual(calls[0].args[1], {'$pop': {'history': 1}})
        self.assertEqual(calls[1].args[1]['$push']['history'], {'date': today, 'shares': 900})

    def test_db_error_swallowed(self):
        self.coll.find_one.side_effect = Exception('mongo down')
        # must not raise — history is best-effort
        updateMarketData.record_shares_history('AAPL', 1000)


if __name__ == '__main__':
    unittest.main()
