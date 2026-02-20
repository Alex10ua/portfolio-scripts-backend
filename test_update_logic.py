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
sys.modules['updateMarketDataUtilies'] = MagicMock()
sys.modules['flask'] = MagicMock()

import updateMarketData

class TestUpdateMarketData(unittest.TestCase):

    def setUp(self):
        # Reset mocks
        updateMarketData.yf.Ticker.reset_mock()
        updateMarketData.collection.reset_mock()
        updateMarketData.tickers_collection.reset_mock()
        updateMarketData.updateMarketDataUtilies.get_company_name.return_value = 'Test Corp'
        updateMarketData.updateMarketDataUtilies.get_current_price.return_value = 100.0

    def test_process_ticker_success(self):
        ticker = 'TEST'
        mock_stock = MagicMock()
        mock_stock.info = {'shortName': 'Test Corp'}
        updateMarketData.yf.Ticker.return_value = mock_stock

        ticker_res, op, error = updateMarketData.process_ticker(ticker)

        self.assertEqual(ticker_res, ticker)
        self.assertIsNotNone(op)
        self.assertIsNone(error)
        updateMarketData.yf.Ticker.assert_called_with(ticker)

    def test_process_ticker_failure(self):
        ticker = 'FAIL'
        updateMarketData.yf.Ticker.side_effect = Exception("Fetch error")

        ticker_res, op, error = updateMarketData.process_ticker(ticker)

        self.assertEqual(ticker_res, ticker)
        self.assertIsNone(op)
        self.assertEqual(error, "Fetch error")

    @patch('concurrent.futures.ThreadPoolExecutor')
    def test_run_task_batch_execution(self, mock_executor_cls):
        # Setup tickers
        mock_cursor = [{'ticker': 'AAPL'}, {'ticker': 'GOOGL'}]
        updateMarketData.tickers_collection.find.return_value = mock_cursor

        # Mock executor context manager
        mock_executor = MagicMock()
        mock_executor_cls.return_value.__enter__.return_value = mock_executor
        
        # Mock futures
        f1 = MagicMock()
        f1.result.return_value = ('AAPL', MagicMock(), None)
        f2 = MagicMock()
        f2.result.return_value = ('GOOGL', MagicMock(), None)
        
        # Map futures to tickers properly mimicking submit return/as_completed
        # But for simpler mocking, we can just mock as_completed
        
        # We need to mock concurrent.futures.as_completed behavior
        with patch('concurrent.futures.as_completed', return_value=[f1, f2]):
             updateMarketData.run_task()
             
        # Check if bulk_write was called
        self.assertTrue(updateMarketData.collection.bulk_write.called)
        self.assertTrue(updateMarketData.tickers_collection.bulk_write.called)
        
        # Check update count (2 successful)
        args, _ = updateMarketData.collection.bulk_write.call_args
        self.assertEqual(len(args[0]), 2)

    def test_update_all_timer(self):
        with patch('threading.Timer') as mock_timer:
            updateMarketData.update_all()
            # Check correctness: threading.Timer(10, run_task) NOT run_task()
            args, _ = mock_timer.call_args
            self.assertEqual(args[0], 10)
            self.assertEqual(args[1], updateMarketData.run_task) # Should be the function object

if __name__ == '__main__':
    unittest.main()
