import unittest
from unittest.mock import MagicMock, patch
import sys
import os

sys.path.append(os.getcwd())

import massive_provider


class TestMassiveProvider(unittest.TestCase):

    def _sample_response(self):
        return {
            'name': 'Apple Inc.',
            'price': 175.5,
            'priceYesterday': 172.0,
            'yearlyDividend': 0.96,
            'lastDividendPayment': 0.24,
            'dividends': [
                {'dividendDate': '2024-02-09', 'dividendAmount': 0.24},
            ],
            'splits': [
                {'splitDate': '2020-08-31', 'ratioSplit': 4.0},
            ],
            'country': 'United States',
            'sector': 'Technology',
            'industry': 'Consumer Electronics',
        }

    @patch.dict(os.environ, {'MASSIVE_API_KEY': 'test-key', 'MASSIVE_BASE_URL': 'https://api.massive.com/v1'})
    @patch('massive_provider.requests.get')
    def test_fetch_market_data_correct_url_and_header(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = self._sample_response()
        mock_get.return_value = mock_response

        massive_provider.fetch_market_data('AAPL')

        mock_get.assert_called_once_with(
            'https://api.massive.com/v1/quotes/AAPL',
            headers={'X-API-Key': 'test-key'},
            timeout=10,
        )
        mock_response.raise_for_status.assert_called_once()

    @patch.dict(os.environ, {'MASSIVE_API_KEY': 'test-key', 'MASSIVE_BASE_URL': 'https://api.massive.com/v1'})
    @patch('massive_provider.requests.get')
    def test_fetch_market_data_field_mapping(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = self._sample_response()
        mock_get.return_value = mock_response

        result = massive_provider.fetch_market_data('AAPL')

        self.assertEqual(result['name'], 'Apple Inc.')
        self.assertEqual(result['price'], 175.5)
        self.assertEqual(result['priceYesterday'], 172.0)
        self.assertEqual(result['yearlyDividend'], 0.96)
        self.assertEqual(result['lastDividendPayment'], 0.24)
        self.assertEqual(len(result['dividends']), 1)
        self.assertEqual(result['dividends'][0]['dividendAmount'], 0.24)
        self.assertEqual(len(result['splits']), 1)
        self.assertEqual(result['splits'][0]['ratioSplit'], 4.0)
        self.assertEqual(result['country'], 'United States')
        self.assertEqual(result['sector'], 'Technology')
        self.assertEqual(result['industry'], 'Consumer Electronics')
        self.assertIn('updatedAt', result)

    @patch.dict(os.environ, {'MASSIVE_API_KEY': 'test-key', 'MASSIVE_BASE_URL': 'https://api.massive.com/v1'})
    @patch('massive_provider.requests.get')
    def test_fetch_market_data_uses_companyName_fallback(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = {'companyName': 'Fallback Corp'}
        mock_get.return_value = mock_response

        result = massive_provider.fetch_market_data('TEST')
        self.assertEqual(result['name'], 'Fallback Corp')

    @patch.dict(os.environ, {'MASSIVE_API_KEY': '', 'MASSIVE_BASE_URL': 'https://api.massive.com/v1'})
    def test_fetch_market_data_raises_when_api_key_missing(self):
        with self.assertRaises(RuntimeError) as ctx:
            massive_provider.fetch_market_data('AAPL')
        self.assertIn('MASSIVE_API_KEY', str(ctx.exception))

    @patch.dict(os.environ, {'MASSIVE_API_KEY': 'test-key'}, clear=True)
    def test_fetch_market_data_raises_when_base_url_missing(self):
        # Ensure MASSIVE_BASE_URL is not set
        os.environ.pop('MASSIVE_BASE_URL', None)
        with self.assertRaises(RuntimeError) as ctx:
            massive_provider.fetch_market_data('AAPL')
        self.assertIn('MASSIVE_BASE_URL', str(ctx.exception))

    @patch.dict(os.environ, {'MASSIVE_API_KEY': 'test-key', 'MASSIVE_BASE_URL': 'https://api.massive.com/v1'})
    @patch('massive_provider.requests.get')
    def test_fetch_market_data_surfaces_http_error(self, mock_get):
        import requests
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = requests.HTTPError("404 Not Found")
        mock_get.return_value = mock_response

        with self.assertRaises(requests.HTTPError):
            massive_provider.fetch_market_data('INVALID')

    def test_map_response_empty_dividends_and_splits(self):
        data = {'name': 'Test Co', 'price': 50.0}
        result = massive_provider._map_response('TEST', data)
        self.assertEqual(result['dividends'], [])
        self.assertEqual(result['splits'], [])


if __name__ == '__main__':
    unittest.main()
