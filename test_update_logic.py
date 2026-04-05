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


if __name__ == '__main__':
    unittest.main()
