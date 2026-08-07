import importlib.util
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

sys.path.append(os.getcwd())

# Loaded from the file directly — see the note in test_sec_rate_limit.py.
_spec = importlib.util.spec_from_file_location(
    'sec_edgar_provider_shares',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sec_edgar_provider.py'),
)
sec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sec)

OUTSTANDING = 'CommonStockSharesOutstanding'
DEI = 'EntityCommonStockSharesOutstanding'
ISSUED = 'CommonStockSharesIssued'
WEIGHTED = 'WeightedAverageNumberOfSharesOutstandingBasic'


def _facts(entries):
    return {'units': {'shares': entries}}


def _instant(end, val, filed='2026-01-01'):
    return {'end': end, 'val': val, 'filed': filed, 'form': '10-Q'}


def _duration(start, end, val, filed='2026-01-01'):
    return {'start': start, 'end': end, 'val': val, 'filed': filed, 'form': '10-Q'}


def _response(status=200, payload=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload or {}
    r.raise_for_status = MagicMock()
    return r


def _router(by_tag):
    """requests.get stub dispatching on the concept tag in the URL; 404 otherwise."""
    def get(url, **_kwargs):
        for tag, payload in by_tag.items():
            if f'/{tag}.json' in url:
                return _response(200, payload)
        return _response(404)
    return get


def _recent(days_ago):
    return (datetime.now() - timedelta(days=days_ago)).strftime('%Y-%m-%d')


class TestSharesHistory(unittest.TestCase):

    def setUp(self):
        sec._cik_map_cache = {'MA': '0001141391'}

    def _fetch(self, by_tag):
        stub = MagicMock(side_effect=_router(by_tag))
        with patch.object(sec, '_MIN_INTERVAL', 0), patch.object(sec.requests, 'get', stub):
            return sec.fetch_shares_history('MA'), stub

    def test_concepts_merge_instead_of_first_match(self):
        """Each tag covers a different slice of history — first-match truncates it."""
        history, _ = self._fetch({
            OUTSTANDING: _facts([_instant('2015-06-30', 100)]),
            DEI: _facts([_instant(_recent(30), 90)]),
        })
        self.assertEqual([h['date'] for h in history], ['2015-06-30', _recent(30)])

    def test_multi_class_filer_falls_back_to_weighted_average(self):
        """
        MA's real shape: us-gaap 404s, dei stops after 4 facts in 2010 (the rest
        are per-share-class, which the companyconcept API never returns).
        """
        history, _ = self._fetch({
            DEI: _facts([_instant(f'20{y:02d}-10-27', 1_300_000_000) for y in (9, 10)]),
            WEIGHTED: _facts([
                _duration('2025-01-01', '2025-03-31', 920_000_000),
                _duration('2025-04-01', _recent(20), 915_000_000),
            ]),
        })
        self.assertEqual(len(history), 4)
        self.assertEqual(history[-1], {'date': _recent(20), 'shares': 915_000_000})

    def test_dense_series_skips_the_fallback_requests(self):
        """The fallback costs 2 extra SEC calls — only worth it when the real series is unusable."""
        dense = [_instant(_recent(90 * i), 1_000 + i) for i in range(10)]
        _, stub = self._fetch({OUTSTANDING: _facts(dense)})
        self.assertEqual(stub.call_count, len(sec._SHARES_CONCEPTS))

    def test_stale_series_triggers_the_fallback(self):
        """Dense but abandoned years ago — the filer moved to dimensioned facts."""
        stale = [_instant(f'201{i}-12-31', 1_000 + i) for i in range(10)]
        _, stub = self._fetch({OUTSTANDING: _facts(stale)})
        self.assertEqual(stub.call_count, len(sec._SHARES_CONCEPTS) + len(sec._SHARES_FALLBACK_CONCEPTS))

    def test_point_in_time_wins_a_date_conflict_with_the_average(self):
        """A weighted average is close to the real count, never equal to it."""
        day = _recent(15)
        history, _ = self._fetch({
            DEI: _facts([_instant(day, 1_000)]),
            WEIGHTED: _facts([_duration('2026-01-01', day, 999)]),
        })
        self.assertEqual(history[0]['shares'], 1_000)

    def test_quarterly_value_beats_annual_on_a_shared_end_date(self):
        """Q4 and FY share 12-31; deduping on date alone lets the annual figure in."""
        payload = _facts([
            _duration('2025-01-01', '2025-12-31', 950, filed='2026-02-01'),   # FY
            _duration('2025-10-01', '2025-12-31', 900, filed='2026-02-01'),   # Q4
        ])
        with patch.object(sec, '_MIN_INTERVAL', 0), \
             patch.object(sec.requests, 'get', return_value=_response(200, payload)):
            result = sec._fetch_concept_series('0001141391', 'us-gaap', WEIGHTED, prefer_shortest_period=True)
        self.assertEqual([e['value'] for e in result], [900])


if __name__ == '__main__':
    unittest.main()
