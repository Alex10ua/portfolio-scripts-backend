import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.append(os.getcwd())

import ecb_provider


def _ecb_payload(rates_by_currency: dict) -> dict:
    """
    SDMX-JSON shaped like the live EXR response (checked 2026-09-23): the series key
    carries one index per dimension, FREQ first and CURRENCY second, and CURRENCY's
    values come back alphabetically — not in the order the URL asked for them.
    """
    codes = sorted(rates_by_currency)
    return {
        'dataSets': [{
            'series': {
                f'0:{i}:0:0:0': {'observations': {'0': [rates_by_currency[code], 0, 0, None, None]}}
                for i, code in enumerate(codes)
            },
        }],
        'structure': {
            'dimensions': {
                'series': [
                    {'id': 'FREQ', 'values': [{'id': 'D'}]},
                    {'id': 'CURRENCY', 'values': [{'id': code} for code in codes]},
                    {'id': 'CURRENCY_DENOM', 'values': [{'id': 'EUR'}]},
                    {'id': 'EXR_TYPE', 'values': [{'id': 'SP00'}]},
                    {'id': 'EXR_SUFFIX', 'values': [{'id': 'A'}]},
                ],
            },
        },
    }


LIVE_LIKE = {'CHF': 0.9321, 'CZK': 24.315, 'GBP': 0.8612, 'PLN': 4.2675, 'USD': 1.1411}


class TestParseEcbResponse(unittest.TestCase):

    def test_every_requested_currency_gets_its_own_rate(self):
        self.assertEqual(ecb_provider._parse_ecb_response(_ecb_payload(LIVE_LIKE)), LIVE_LIKE)

    def test_currency_read_from_its_dimension_not_the_first_key_position(self):
        # FREQ sits at position 0 and is always 0 — keying on it mapped every series
        # onto one currency and only USD survived
        rates = ecb_provider._parse_ecb_response(_ecb_payload(LIVE_LIKE))
        self.assertEqual(rates['GBP'], 0.8612)
        self.assertEqual(rates['CZK'], 24.315)

    def test_last_observation_wins(self):
        payload = _ecb_payload({'USD': 1.10})
        payload['dataSets'][0]['series']['0:0:0:0:0']['observations'] = {
            '0': [1.10], '1': [1.12], '10': [1.15],
        }
        self.assertEqual(ecb_provider._parse_ecb_response(payload), {'USD': 1.15})

    def test_series_without_observations_skipped(self):
        payload = _ecb_payload({'GBP': 0.86, 'USD': 1.14})
        payload['dataSets'][0]['series']['0:0:0:0:0']['observations'] = {}
        self.assertEqual(ecb_provider._parse_ecb_response(payload), {'USD': 1.14})

    def test_malformed_payload_yields_nothing(self):
        self.assertEqual(ecb_provider._parse_ecb_response({'dataSets': []}), {})


class TestFetchAndStoreRates(unittest.TestCase):

    @patch('ecb_provider.requests.get')
    def test_stores_every_currency_plus_eur(self, mock_get):
        mock_get.return_value.json.return_value = _ecb_payload(LIVE_LIKE)
        db = MagicMock()
        rates = ecb_provider.fetch_and_store_rates(db)
        self.assertEqual(set(rates), set(LIVE_LIKE) | {'EUR'})
        stored = {c.args[0]['_id'] for c in db['exchangeRates'].update_one.call_args_list}
        self.assertEqual(stored, set(LIVE_LIKE) | {'EUR'})

    @patch('ecb_provider.requests.get')
    def test_unparsable_response_raises_instead_of_storing_eur_alone(self, mock_get):
        mock_get.return_value.json.return_value = {'dataSets': []}
        db = MagicMock()
        with self.assertRaises(RuntimeError):
            ecb_provider.fetch_and_store_rates(db)
        db['exchangeRates'].update_one.assert_not_called()


if __name__ == '__main__':
    unittest.main()
