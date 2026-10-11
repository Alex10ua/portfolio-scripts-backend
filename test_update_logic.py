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
        # Mocked pymongo hands back one MagicMock for every collection, so without
        # this the queue cursor doubles as customAssets and every ticker is skipped.
        with patch.object(updateMarketData, 'get_custom_tickers', return_value=set()), \
             patch('concurrent.futures.as_completed', return_value=[f1, f2]):
            updateMarketData.run_task(provider, 'yahoo')

        # marketData write, then the queue delete (one mock serves both collections)
        self.assertEqual(updateMarketData.collection.bulk_write.call_count, 2)
        args, _ = updateMarketData.collection.bulk_write.call_args_list[0]
        self.assertEqual(len(args[0]), 2)

    @patch('concurrent.futures.ThreadPoolExecutor')
    def test_run_task_keeps_queue_when_write_fails(self, mock_executor_cls):
        updateMarketData.tickers_collection.find.return_value = [{'ticker': 'AAPL'}]
        updateMarketData.collection.bulk_write.side_effect = Exception('mongo down')
        mock_executor = MagicMock()
        mock_executor_cls.return_value.__enter__.return_value = mock_executor
        f1 = MagicMock()
        f1.result.return_value = ('AAPL', MagicMock(), None)
        mock_executor.submit.side_effect = [f1]

        with patch.object(updateMarketData, 'get_custom_tickers', return_value=set()), \
             patch('concurrent.futures.as_completed', return_value=[f1]):
            updateMarketData.run_task(self._make_mock_provider(), 'yahoo')

        # the failed marketData write only — no queue delete after it
        self.assertEqual(updateMarketData.collection.bulk_write.call_count, 1)

    def _set_of(self, ticker, provider):
        """The $set document process_ticker built for `ticker`."""
        with patch.object(updateMarketData, 'UpdateOne') as update_one, \
             patch.object(updateMarketData, 'get_price_history') as history:
            _, op, error = updateMarketData.process_ticker(ticker, provider)
        self.assertIsNone(error)
        return update_one.call_args.args[1]['$set'], history

    def test_unknown_symbol_is_a_failure_not_a_stub(self):
        # Yahoo's answer to a symbol it does not know: empty .info, empty history
        def provider(ticker):
            return {'name': '', 'price': '', 'priceYesterday': None, 'currency': None,
                    'dividends': [], 'splits': [], 'updatedAt': 'now'}

        with patch.object(updateMarketData, 'UpdateOne') as update_one:
            ticker, op, error = updateMarketData.process_ticker('UKW.GB', provider)
        self.assertIsNone(op)
        self.assertIn('no price and no name', error)
        update_one.assert_not_called()

    def test_missing_price_drops_previous_close_and_freshness_stamp(self):
        def provider(ticker):
            return {'name': 'Ethereum USD', 'price': '', 'priceYesterday': 2775.96,
                    'dividends': [], 'splits': [], 'updatedAt': 'now'}

        updateMarketData.collection.find_one.return_value = None
        fields, _ = self._set_of('ETH', provider)
        self.assertEqual(fields['name'], 'Ethereum USD')
        self.assertNotIn('priceYesterday', fields)
        self.assertNotIn('updatedAt', fields)

    def test_stored_ignored_split_is_purged(self):
        from datetime import datetime
        updateMarketData.collection.find_one.return_value = {'splits': [
            {'splitDate': '2005-05-18', 'ratioSplit': 2.0},
            # SPGI's spin-off, stored before ignored_splits.json listed it — legacy
            # instant form, 04:00Z being New York midnight
            {'splitDate': datetime(2026, 7, 1, 4, 0), 'ratioSplit': 1.057},
        ]}
        fields, _ = self._set_of('SPGI', self._make_mock_provider())
        self.assertEqual([s['ratioSplit'] for s in fields['splits']], [2.0])

    def test_crypto_provider_history_uses_the_pair_symbol(self):
        def provider(ticker):
            return {'name': 'Bitcoin', 'price': 65000.0, 'dividends': [], 'splits': [], '_crypto': True}

        updateMarketData.collection.find_one.return_value = None
        fields, history = self._set_of('BTC', provider)
        self.assertNotIn('_crypto', fields)
        history.assert_called_once_with('BTC', crypto=True)

    def test_yahoo_symbol_when_crypto_is_known(self):
        updateMarketData.holdings_collection.find_one.reset_mock()
        self.assertEqual(updateMarketData.yahoo_crypto_symbol('BTC', crypto=True), 'BTC-USD')
        self.assertEqual(updateMarketData.yahoo_crypto_symbol('BTC-EUR', crypto=True), 'BTC-EUR')
        self.assertEqual(updateMarketData.yahoo_crypto_symbol('AAPL', crypto=False), 'AAPL')
        updateMarketData.holdings_collection.find_one.assert_not_called()

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

    def test_legacy_utc_instant_collapses_into_new_string_date(self):
        """The case the NY test cannot catch: an exchange *ahead* of UTC.

        Documents written before action_day() hold the raw instant, which Mongo
        reads back naive and in UTC — Frankfurt local midnight as 22:00 the day
        before. The new write is the string '2025-05-05', so without the legacy
        bridge in _day_key the two never match and every European/UK ticker
        re-appends its whole dividend and split history on each update."""
        from datetime import datetime
        stored = [{'dividendDate': datetime(2025, 5, 4, 22, 0), 'dividendAmount': 2.25}]
        fetched = [{'dividendDate': '2025-05-05', 'dividendAmount': 2.25}]
        merged = updateMarketData._merge_list(stored, fetched, 'dividendDate')
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['dividendDate'], '2025-05-05')

    def test_legacy_london_split_instant_collapses(self):
        from datetime import datetime
        stored = [{'splitDate': datetime(2013, 8, 27, 23, 0), 'ratioSplit': 2}]
        fetched = [{'splitDate': '2013-08-28', 'ratioSplit': 2}]
        merged = updateMarketData._merge_list(stored, fetched, 'splitDate')
        self.assertEqual(len(merged), 1)

    def test_us_legacy_instant_keeps_its_own_day(self):
        """New York's local midnight is 04:00 UTC on the same day — the bridge
        must not roll those forward."""
        from datetime import datetime
        stored = [{'dividendDate': datetime(2025, 10, 1, 4, 0), 'dividendAmount': 0.27}]
        fetched = [{'dividendDate': '2025-10-01', 'dividendAmount': 0.27}]
        merged = updateMarketData._merge_list(stored, fetched, 'dividendDate')
        self.assertEqual(len(merged), 1)

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


class TestReplaceSharesHistory(unittest.TestCase):
    """
    A stock's share-count history is SEC filings only. Mocked pymongo hands back
    one MagicMock for every collection, so find_one also answers is_crypto_ticker:
    None = not crypto.
    """

    def setUp(self):
        self.coll = updateMarketData.shares_history_collection
        self.coll.reset_mock(side_effect=True, return_value=True)
        self.coll.find_one.return_value = None

    def test_series_replaces_the_doc_sorted(self):
        stored = updateMarketData.replace_shares_history('KO', [
            {'date': '2026-04-28', 'shares': 4302482418},
            {'date': '2009-07-24', 'shares': 2317441658},
        ])
        self.assertEqual(stored, 2)
        args, kwargs = self.coll.update_one.call_args
        self.assertEqual(args[1]['$set']['history'], [
            {'date': '2009-07-24', 'shares': 2317441658},
            {'date': '2026-04-28', 'shares': 4302482418},
        ])
        self.assertEqual(args[1]['$set']['lastUpdated'], '2026-04-28')
        self.assertTrue(kwargs.get('upsert'))

    def test_nothing_is_merged_from_the_stored_doc(self):
        """Yahoo snapshots used to survive every backfill — GOOGL's class-A-only count among them."""
        self.coll.find_one.side_effect = [None, {'history': [{'date': '2026-07-31', 'shares': 5_867_000_000}]}]
        updateMarketData.replace_shares_history('GOOGL', [{'date': '2026-06-30', 'shares': 12_100_000_000}])
        args, _ = self.coll.update_one.call_args
        self.assertEqual(args[1]['$set']['history'], [{'date': '2026-06-30', 'shares': 12_100_000_000}])

    def test_no_filings_removes_the_doc(self):
        self.assertEqual(updateMarketData.replace_shares_history('NSRGY', []), 0)
        self.coll.delete_one.assert_called_once_with({'_id': 'NSRGY'})
        self.coll.update_one.assert_not_called()

    def test_crypto_supply_history_is_never_touched(self):
        self.coll.find_one.return_value = {'ticker': 'ETH', 'assetType': 'CRYPTO'}
        self.assertEqual(updateMarketData.replace_shares_history('ETH', []), 0)
        self.coll.delete_one.assert_not_called()
        self.coll.update_one.assert_not_called()

    def test_db_error_swallowed(self):
        self.coll.update_one.side_effect = Exception('mongo down')
        self.assertEqual(updateMarketData.replace_shares_history('KO', [{'date': '2026-01-01', 'shares': 1}]), 0)


class TestThrottledRun(unittest.TestCase):
    """
    Pause/resume, per-ticker failure reasons and the streamed event feed. The
    worker runs in a thread with pauseSeconds=0 so only the cooperative checks
    (not the sleeps) decide timing.
    """

    def setUp(self):
        import threading
        self.threading = threading
        updateMarketData.collection.reset_mock(side_effect=True, return_value=True)
        updateMarketData.tickers_collection.reset_mock(side_effect=True, return_value=True)
        # no holding at all — a bare MagicMock would read as a CRYPTO holding
        updateMarketData.holdings_collection.find_one.return_value = None
        with updateMarketData.throttled_lock:
            updateMarketData.throttled_status.update({
                "running": False, "scope": None, "total": 0, "processed": 0,
                "updated": 0, "failed": [], "pauseSeconds": None,
                "currentTicker": None, "currentStage": None, "lastError": None,
                "cancelRequested": False, "paused": False, "pausedAt": None,
                "pausedSeconds": 0.0, "abortedReason": None,
                "startedAt": None, "finishedAt": None, "events": [],
            })

    def _status(self, key):
        with updateMarketData.throttled_lock:
            return updateMarketData.throttled_status[key]

    def _start(self, tickers):
        """Start the worker the way the endpoint does: slot claimed, then thread."""
        with updateMarketData.throttled_lock:
            updateMarketData.throttled_status["running"] = True
        t = self.threading.Thread(
            target=updateMarketData.run_throttled_task, args=[tickers, 0, 'queue'], daemon=True)
        t.start()
        return t

    def _wait_for(self, predicate, timeout=5.0):
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_fetch_failure_records_reason_and_stage(self):
        def fake_process(ticker, provider_fn, request_pause=0):
            if ticker == 'BAD':
                return ticker, None, 'Yahoo said no'
            return ticker, MagicMock(), None

        with patch.object(updateMarketData, 'process_ticker', side_effect=fake_process):
            self._start(['GOOD', 'BAD']).join(timeout=5)

        failed = self._status('failed')
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]['ticker'], 'BAD')
        self.assertEqual(failed[0]['stage'], 'fetch')
        self.assertEqual(failed[0]['error'], 'Yahoo said no')
        self.assertIn('Yahoo said no', self._status('lastError'))
        self.assertEqual(self._status('updated'), 1)

    def test_db_failure_recorded_as_db_stage(self):
        updateMarketData.collection.bulk_write.side_effect = Exception('mongo down')
        with patch.object(updateMarketData, 'process_ticker',
                          side_effect=lambda t, fn, request_pause=0: (t, MagicMock(), None)):
            self._start(['AAPL']).join(timeout=5)

        failed = self._status('failed')
        self.assertEqual(failed[0]['stage'], 'db')
        self.assertEqual(failed[0]['error'], 'mongo down')
        self.assertEqual(self._status('updated'), 0)

    def test_worker_crash_recorded_as_aborted_reason(self):
        def boom(ticker, provider_fn, request_pause=0):
            raise RuntimeError('worker exploded')

        with patch.object(updateMarketData, 'process_ticker', side_effect=boom):
            self._start(['AAPL']).join(timeout=5)

        self.assertIn('worker exploded', self._status('abortedReason'))
        self.assertFalse(self._status('running'))

    def test_pause_holds_then_resume_finishes(self):
        seen = []

        def fake_process(ticker, provider_fn, request_pause=0):
            seen.append(ticker)
            if ticker == 'A':
                updateMarketData.update_throttled_pause()  # pause mid-ticker
            return ticker, MagicMock(), None

        with patch.object(updateMarketData, 'process_ticker', side_effect=fake_process):
            thread = self._start(['A', 'B', 'C'])
            # The ticker in flight always finishes; the hold lands before the next one.
            self.assertTrue(self._wait_for(lambda: self._status('currentStage') == 'paused'))
            self.assertEqual(seen, ['A'])
            self.assertEqual(self._status('processed'), 1)
            self.assertTrue(self._status('running'))

            updateMarketData.update_throttled_resume()
            thread.join(timeout=5)

        self.assertEqual(seen, ['A', 'B', 'C'])
        self.assertEqual(self._status('processed'), 3)
        self.assertIsNone(self._status('abortedReason'))
        self.assertFalse(self._status('paused'))

    def test_cancel_beats_pause(self):
        def fake_process(ticker, provider_fn, request_pause=0):
            if ticker == 'A':
                updateMarketData.update_throttled_pause()
            return ticker, MagicMock(), None

        with patch.object(updateMarketData, 'process_ticker', side_effect=fake_process):
            thread = self._start(['A', 'B'])
            self.assertTrue(self._wait_for(lambda: self._status('currentStage') == 'paused'))
            # A stop must not wait for a resume that may never come.
            updateMarketData.update_throttled_cancel()
            thread.join(timeout=5)

        self.assertEqual(self._status('processed'), 1)
        self.assertEqual(self._status('abortedReason'), 'cancelled by user')
        self.assertFalse(self._status('paused'))
        self.assertFalse(self._status('running'))

    def test_pause_endpoint_409_when_not_running(self):
        body, code = updateMarketData.update_throttled_pause()
        self.assertEqual(code, 409)
        self.assertEqual(body['status'], 'not_running')

        body, code = updateMarketData.update_throttled_resume()
        self.assertEqual(code, 409)
        self.assertEqual(body['status'], 'not_running')

    def test_resume_409_when_running_but_not_paused(self):
        with updateMarketData.throttled_lock:
            updateMarketData.throttled_status["running"] = True
        try:
            body, code = updateMarketData.update_throttled_resume()
        finally:
            with updateMarketData.throttled_lock:
                updateMarketData.throttled_status["running"] = False
        self.assertEqual(code, 409)
        self.assertEqual(body['status'], 'not_paused')

    def test_events_carry_every_ticker_and_are_cursorable(self):
        with patch.object(updateMarketData, 'process_ticker',
                          side_effect=lambda t, fn, request_pause=0: (t, MagicMock(), None)):
            self._start(['AAPL', 'MSFT']).join(timeout=5)

        events = self._status('events')
        messages = ' | '.join(e['message'] for e in events)
        self.assertIn('AAPL: fetching from Yahoo', messages)
        self.assertIn('AAPL: updated', messages)
        self.assertIn('MSFT: updated', messages)
        seqs = [e['seq'] for e in events]
        self.assertEqual(seqs, sorted(seqs))

        # sinceSeq is a cursor: everything up to it is already logged by the client.
        cutoff = seqs[len(seqs) // 2]
        with patch.object(updateMarketData, 'request') as req:
            req.args.get.return_value = str(cutoff)
            body, code = updateMarketData.update_throttled_status()
        self.assertEqual(code, 200)
        self.assertTrue(all(e['seq'] > cutoff for e in body['events']))
        self.assertEqual(body['eventSeq'], seqs[-1])

    def test_crypto_goes_to_coingecko_not_yahoo(self):
        # Yahoo's .info has no currentPrice for a crypto pair: a Yahoo pass left
        # ETH's price stale while refreshing its previous close
        updateMarketData.holdings_collection.find_one.side_effect = (
            lambda query, *a, **k: {'ticker': 'ETH'} if query.get('ticker') == 'ETH' else None)
        used = {}

        def fake_process(ticker, provider_fn, request_pause=0):
            used[ticker] = provider_fn
            return ticker, MagicMock(), None

        with patch.object(updateMarketData, 'process_ticker', side_effect=fake_process):
            self._start(['AAPL', 'ETH']).join(timeout=5)

        self.assertIs(used['ETH'], updateMarketData.crypto_provider.fetch_market_data)
        self.assertIsNot(used['AAPL'], updateMarketData.crypto_provider.fetch_market_data)
        messages = ' | '.join(e['message'] for e in self._status('events'))
        self.assertIn('ETH: fetching from CoinGecko', messages)
        self.assertIn('AAPL: fetching from Yahoo', messages)

    def test_cancel_does_not_clobber_a_crash_reason(self):
        def boom(ticker, provider_fn, request_pause=0):
            with updateMarketData.throttled_lock:
                updateMarketData.throttled_status["cancelRequested"] = True
            raise RuntimeError('worker exploded')

        with patch.object(updateMarketData, 'process_ticker', side_effect=boom):
            self._start(['AAPL']).join(timeout=5)

        self.assertIn('worker exploded', self._status('abortedReason'))

    def test_event_feed_is_capped(self):
        with updateMarketData.throttled_lock:
            updateMarketData.throttled_status["events"] = []
        for i in range(updateMarketData.THROTTLED_EVENT_CAP + 25):
            updateMarketData.throttled_event('info', f'line {i}')
        events = self._status('events')
        self.assertEqual(len(events), updateMarketData.THROTTLED_EVENT_CAP)
        self.assertEqual(events[-1]['message'], f'line {updateMarketData.THROTTLED_EVENT_CAP + 24}')


class TestBulkRun(unittest.TestCase):
    """
    POST /update/bulk's worker: per-ticker outcomes land in the right counter, and
    the run stops on a provider rate limit or a streak of failures instead of
    pushing on. Run inline with pauseSeconds=0 and a fake job.
    """

    def setUp(self):
        with updateMarketData.bulk_lock:
            updateMarketData.bulk_status.update({
                "running": True, "job": None, "total": 0, "processed": 0, "updated": 0,
                "noData": [], "failed": [], "cancelRequested": False, "abortedReason": None,
                "events": [],
            })

    def _run(self, outcomes: dict, tickers: list):
        calls = []

        def fake_job(ticker):
            calls.append(ticker)
            return outcomes.get(ticker, ('success', {'fields': 3}))

        with patch.dict(updateMarketData.BULK_JOBS, {'fake': (fake_job, 'fake job')}):
            updateMarketData.run_bulk_task('fake', tickers, 0, 'holdings')
        return calls

    def _status(self, key):
        with updateMarketData.bulk_lock:
            return updateMarketData.bulk_status[key]

    def test_outcomes_counted_per_kind(self):
        calls = self._run({
            'ETF': ('no_data', {'reason': 'ETF has no financial statements'}),
            'BAD': ('error', {'error': 'Yahoo said no'}),
        }, ['AAPL', 'ETF', 'BAD', 'MSFT'])

        self.assertEqual(calls, ['AAPL', 'ETF', 'BAD', 'MSFT'])
        self.assertEqual(self._status('processed'), 4)
        self.assertEqual(self._status('updated'), 2)
        self.assertEqual(self._status('noData'), ['ETF'])
        failed = self._status('failed')
        self.assertEqual([(f['ticker'], f['stage'], f['error']) for f in failed], [('BAD', 'error', 'Yahoo said no')])
        self.assertIsNone(self._status('abortedReason'))
        self.assertFalse(self._status('running'))

    def test_rate_limit_stops_the_run(self):
        calls = self._run({'B': ('rate_limited', {'error': 'SEC 429'})}, ['A', 'B', 'C'])
        self.assertEqual(calls, ['A', 'B'])
        self.assertIn('rate-limited at B', self._status('abortedReason'))

    def test_failure_streak_stops_the_run(self):
        n = updateMarketData.BULK_MAX_CONSECUTIVE_FAILURES
        tickers = [f'T{i}' for i in range(n + 3)]
        calls = self._run({t: ('error', {'error': 'timeout'}) for t in tickers}, tickers)
        self.assertEqual(len(calls), n)
        self.assertIn('failures in a row', self._status('abortedReason'))

    def test_success_or_no_data_resets_the_streak(self):
        n = updateMarketData.BULK_MAX_CONSECUTIVE_FAILURES
        bad = ('error', {'error': 'timeout'})
        # n-1 failures, a no_data, n-1 failures: never n in a row
        tickers = [f'E{i}' for i in range(n - 1)] + ['ND'] + [f'F{i}' for i in range(n - 1)]
        outcomes = {t: bad for t in tickers}
        outcomes['ND'] = ('no_data', {'reason': 'none'})
        calls = self._run(outcomes, tickers)
        self.assertEqual(len(calls), len(tickers))
        self.assertIsNone(self._status('abortedReason'))

    def test_cancel_ends_after_the_ticker_in_flight(self):
        def fake_job(ticker):
            with updateMarketData.bulk_lock:
                updateMarketData.bulk_status["cancelRequested"] = True
            return 'success', {}

        with patch.dict(updateMarketData.BULK_JOBS, {'fake': (fake_job, 'fake job')}):
            updateMarketData.run_bulk_task('fake', ['A', 'B', 'C'], 0, 'all')

        self.assertEqual(self._status('processed'), 1)
        self.assertEqual(self._status('abortedReason'), 'cancelled by user')
        self.assertFalse(self._status('cancelRequested'))

    def test_bulk_offers_only_what_a_throttled_run_does_not_cover(self):
        # sharesOutstanding duplicated the throttled run; the SEC jobs live in the SEC backfill
        self.assertEqual(set(updateMarketData.BULK_JOBS), {'statistics', 'financials'})


class TestSecBackfill(unittest.TestCase):
    """
    The SEC backfill: sec_job reads fundamentals and shares history off one
    companyfacts download, runs through the same worker as a bulk Yahoo job in its
    own slot, and takes a holdings/all scope or an explicit list.
    """

    def setUp(self):
        with updateMarketData.sec_bulk_lock:
            updateMarketData.sec_bulk_status.update({
                "running": False, "scope": None, "total": 0, "processed": 0, "updated": 0,
                "noData": [], "failed": [], "cancelRequested": False, "abortedReason": None,
                "events": [],
            })
        updateMarketData.fundamentals_collection.reset_mock(side_effect=True, return_value=True)
        self.sec = updateMarketData.sec_edgar_provider

    def tearDown(self):
        with updateMarketData.sec_bulk_lock:
            updateMarketData.sec_bulk_status["running"] = False

    def _status(self, key):
        with updateMarketData.sec_bulk_lock:
            return updateMarketData.sec_bulk_status[key]

    def test_sec_job_writes_both_from_one_fetch(self):
        concepts = {'Revenue': [{'date': '2025-12-31', 'value': 1}], 'Assets': [{'date': '2025-12-31', 'value': 2}]}
        entries = [{'date': '2024-12-31', 'shares': 10}, {'date': '2025-12-31', 'shares': 9}]
        with patch.object(self.sec, 'fetch_fundamentals', return_value=concepts), \
                patch.object(self.sec, 'fetch_shares_history', return_value=entries), \
                patch.object(updateMarketData, 'replace_shares_history', return_value=2) as replace:
            status, details = updateMarketData.sec_job('KO')

        self.assertEqual((status, details), ('success', {'concepts': 2, 'sharesPoints': 2}))
        replace.assert_called_once_with('KO', entries)
        updateMarketData.fundamentals_collection.update_one.assert_called_once()

    def test_sec_job_without_filings_is_no_data_and_still_clears_history(self):
        with patch.object(self.sec, 'fetch_fundamentals', return_value={}), \
                patch.object(self.sec, 'fetch_shares_history', return_value=[]), \
                patch.object(self.sec, 'get_cik', return_value=None), \
                patch.object(updateMarketData, 'replace_shares_history', return_value=0) as replace:
            status, details = updateMarketData.sec_job('ULVR.L')

        self.assertEqual(status, 'no_data')
        self.assertIn('no CIK', details['reason'])
        replace.assert_called_once_with('ULVR.L', [])
        updateMarketData.fundamentals_collection.update_one.assert_not_called()

    def test_sec_job_reports_a_throttle(self):
        with patch.object(self.sec, 'fetch_fundamentals', side_effect=self.sec.SecThrottled('SEC 429')):
            status, details = updateMarketData.sec_job('MSFT')
        self.assertEqual(status, 'rate_limited')
        self.assertIn('429', details['error'])

    def test_run_goes_through_the_shared_worker(self):
        outcomes = {
            'ASML': ('no_data', {'reason': 'no CIK found for ticker (not SEC-registered?)'}),
            'BAD': ('error', {'error': 'boom'}),
        }
        with patch.object(updateMarketData, 'sec_job',
                          side_effect=lambda t: outcomes.get(t, ('success', {'concepts': 3, 'sharesPoints': 40}))):
            updateMarketData.run_sec_bulk_task(['ASML', 'BAD', 'KO'], 0, 'holdings')

        self.assertEqual(self._status('scope'), 'holdings')
        self.assertEqual(self._status('processed'), 3)
        self.assertEqual(self._status('updated'), 1)
        self.assertEqual(self._status('noData'), ['ASML'])
        self.assertEqual([f['ticker'] for f in self._status('failed')], ['BAD'])
        self.assertFalse(self._status('running'))
        self.assertTrue(any('KO: updated' in e['message'] for e in self._status('events')))

    def test_rate_limit_aborts_the_run(self):
        with patch.object(updateMarketData, 'sec_job', return_value=('rate_limited', {'error': 'SEC 403'})) as job:
            updateMarketData.run_sec_bulk_task(['A', 'B'], 0, 'all')
        self.assertEqual(job.call_count, 1)
        self.assertIn('rate-limited at A', self._status('abortedReason'))

    def _start(self, body):
        with patch.object(updateMarketData.request, 'get_json', return_value=body), \
                patch.object(updateMarketData.threading, 'Thread') as thread:
            response = updateMarketData.update_sec_all()
        return response, thread

    def test_explicit_list_overrides_scope(self):
        with patch.object(updateMarketData, 'get_custom_tickers', return_value={'S'}):
            (body, code), thread = self._start({'tickers': ['msft', ' ko ', 'MSFT', 'S'], 'scope': 'holdings'})
        self.assertEqual(code, 202)
        self.assertEqual((body['scope'], body['tickers'], body['skippedCustom']), ('list', 2, ['S']))
        self.assertEqual(thread.call_args.kwargs['args'], [['KO', 'MSFT'], 1.0, 'list'])

    def test_scope_resolves_like_a_throttled_run(self):
        with patch.object(updateMarketData, 'resolve_throttled_tickers', return_value=(['KO', 'MSFT'], [])) as resolve:
            (body, code), thread = self._start({'scope': 'holdings', 'pauseSeconds': 0})
        resolve.assert_called_once_with('holdings')
        self.assertEqual((code, body['scope'], body['tickers']), (202, 'holdings', 2))
        self.assertEqual(thread.call_args.kwargs['args'], [['KO', 'MSFT'], 0.0, 'holdings'])

    def test_unknown_scope_is_rejected(self):
        (body, code), thread = self._start({'scope': 'queue'})
        self.assertEqual(code, 400)
        thread.assert_not_called()

    def test_refused_while_a_backfill_runs(self):
        with updateMarketData.sec_bulk_lock:
            updateMarketData.sec_bulk_status["running"] = True
        with patch.object(updateMarketData, 'resolve_throttled_tickers', return_value=(['KO'], [])):
            (body, code), thread = self._start({'scope': 'all'})
        self.assertEqual((code, body['status']), (409, 'already_running'))
        thread.assert_not_called()


if __name__ == '__main__':
    unittest.main()
