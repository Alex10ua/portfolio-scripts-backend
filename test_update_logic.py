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

    def test_event_feed_is_capped(self):
        with updateMarketData.throttled_lock:
            updateMarketData.throttled_status["events"] = []
        for i in range(updateMarketData.THROTTLED_EVENT_CAP + 25):
            updateMarketData.throttled_event('info', f'line {i}')
        events = self._status('events')
        self.assertEqual(len(events), updateMarketData.THROTTLED_EVENT_CAP)
        self.assertEqual(events[-1]['message'], f'line {updateMarketData.THROTTLED_EVENT_CAP + 24}')


if __name__ == '__main__':
    unittest.main()
