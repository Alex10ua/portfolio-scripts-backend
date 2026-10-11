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

OUTSTANDING = ('us-gaap', 'CommonStockSharesOutstanding')
DEI = ('dei', 'EntityCommonStockSharesOutstanding')
ISSUED = ('us-gaap', 'CommonStockSharesIssued')
WEIGHTED = ('us-gaap', 'WeightedAverageNumberOfSharesOutstandingBasic')
REVENUES = ('us-gaap', 'Revenues')
ASSETS = ('us-gaap', 'Assets')


def _facts(entries, unit='shares'):
    return {'units': {unit: entries}}


def _company(by_concept):
    """companyfacts payload: {(taxonomy, tag): concept body} -> SEC's nesting."""
    facts = {}
    for (taxonomy, tag), body in by_concept.items():
        facts.setdefault(taxonomy, {})[tag] = body
    return {'cik': 1, 'entityName': 'Test Co', 'facts': facts}


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


def _recent(days_ago):
    return (datetime.now() - timedelta(days=days_ago)).strftime('%Y-%m-%d')


class TestSharesHistory(unittest.TestCase):

    def setUp(self):
        sec._cik_map_cache = {'MA': '0001141391'}
        sec._facts_cache = None

    def _fetch(self, by_concept, status=200):
        stub = MagicMock(return_value=_response(status, _company(by_concept)))
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
        MA's real shape: no us-gaap series, dei stops after 4 facts in 2010 (the
        rest are per-share-class, which SEC's XBRL JSON APIs never return).
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

    def test_dense_series_leaves_the_averages_out(self):
        """Period averages slotted between real counts would make the series wobble."""
        dense = [_instant(_recent(90 * i), 1_000 + i) for i in range(10)]
        history, _ = self._fetch({
            OUTSTANDING: _facts(dense),
            WEIGHTED: _facts([_duration('2026-01-01', _recent(45), 999)]),
        })
        self.assertNotIn(_recent(45), [h['date'] for h in history])

    def test_stale_series_takes_the_fallback(self):
        """Dense but abandoned years ago — the filer moved to dimensioned facts."""
        stale = [_instant(f'201{i}-12-31', 1_000 + i) for i in range(10)]
        history, _ = self._fetch({
            OUTSTANDING: _facts(stale),
            WEIGHTED: _facts([_duration('2026-01-01', _recent(45), 999)]),
        })
        self.assertEqual(history[-1], {'date': _recent(45), 'shares': 999})

    def test_issued_shares_are_never_a_share_count(self):
        """Issued includes treasury stock — KO issued 7.0B against 4.3B outstanding."""
        history, _ = self._fetch({ISSUED: _facts([_instant(_recent(30), 7_040_000_000)])})
        self.assertEqual(history, [])

    def test_point_in_time_wins_a_date_conflict_with_the_average(self):
        """A weighted average is close to the real count, never equal to it."""
        day = _recent(15)
        history, _ = self._fetch({
            DEI: _facts([_instant(day, 1_000)]),
            WEIGHTED: _facts([_duration('2026-01-01', day, 999)]),
        })
        self.assertEqual(history[0]['shares'], 1_000)

    def test_empty_units_object_reads_as_no_facts(self):
        """
        KO's real companyconcept answer (2026-10): 200 with "units": {"shares": {}}
        — a dict where a list belongs. It must not crash or count as data.
        """
        history, _ = self._fetch({DEI: {'units': {'shares': {}}}})
        self.assertEqual(history, [])

    def test_no_xbrl_filings_is_empty_not_an_error(self):
        history, _ = self._fetch({}, status=404)
        self.assertEqual(history, [])

    def test_quarterly_value_beats_annual_on_a_shared_end_date(self):
        """Q4 and FY share 12-31; deduping on date alone lets the annual figure in."""
        facts = _company({WEIGHTED: _facts([
            _duration('2025-01-01', '2025-12-31', 950, filed='2026-02-01'),   # FY
            _duration('2025-10-01', '2025-12-31', 900, filed='2026-02-01'),   # Q4
        ])})['facts']
        result = sec._concept_series(facts, *WEIGHTED, prefer_shortest_period=True)
        self.assertEqual([e['value'] for e in result], [900])

    def test_untagged_concept_is_none_not_empty(self):
        """None lets callers fall through to the next tag candidate."""
        self.assertIsNone(sec._concept_series({'us-gaap': {}}, *ASSETS))
        self.assertIsNone(sec._concept_series(None, *ASSETS))


class TestCompanyFactsRequests(unittest.TestCase):

    def setUp(self):
        sec._cik_map_cache = {'KO': '0000021344', 'MSFT': '0000789019'}
        sec._facts_cache = None

    def test_fundamentals_and_shares_share_one_request(self):
        """A SEC refresh runs both back to back — one companyfacts download covers it."""
        payload = _company({DEI: _facts([_instant(_recent(30), 4_300_000_000)]),
                            ASSETS: _facts([_instant(_recent(30), 1)], unit='USD')})
        stub = MagicMock(return_value=_response(200, payload))
        with patch.object(sec, '_MIN_INTERVAL', 0), patch.object(sec.requests, 'get', stub):
            sec.fetch_fundamentals('KO')
            sec.fetch_shares_history('KO')
        self.assertEqual(stub.call_count, 1)
        self.assertIn('/companyfacts/CIK0000021344.json', stub.call_args.args[0])

    def test_another_ticker_is_not_served_from_the_cache(self):
        stub = MagicMock(return_value=_response(200, _company({})))
        with patch.object(sec, '_MIN_INTERVAL', 0), patch.object(sec.requests, 'get', stub):
            sec.fetch_fundamentals('KO')
            sec.fetch_fundamentals('MSFT')
        self.assertEqual(stub.call_count, 2)

    def test_cache_expires(self):
        stub = MagicMock(return_value=_response(200, _company({})))
        with patch.object(sec, '_MIN_INTERVAL', 0), patch.object(sec, '_FACTS_CACHE_TTL', 0), \
             patch.object(sec.requests, 'get', stub):
            sec.fetch_fundamentals('KO')
            sec.fetch_fundamentals('KO')
        self.assertEqual(stub.call_count, 2)


class TestFundamentalsPeriods(unittest.TestCase):

    def setUp(self):
        sec._cik_map_cache = {'MSFT': '0000789019'}
        sec._facts_cache = None

    def _fundamentals(self, by_concept):
        stub = MagicMock(return_value=_response(200, _company(by_concept)))
        with patch.object(sec, '_MIN_INTERVAL', 0), patch.object(sec.requests, 'get', stub):
            return sec.fetch_fundamentals('MSFT')

    def test_quarter_beats_year_to_date_on_a_shared_end_date(self):
        """A 10-Q files the quarter and the half-year with one 'end' and one 'filed'."""
        result = self._fundamentals({REVENUES: _facts([
            _duration('2025-01-01', '2025-06-30', 200, filed='2025-07-30'),   # 6-month YTD
            _duration('2025-04-01', '2025-06-30', 110, filed='2025-07-30'),   # Q2
            _duration('2024-07-01', '2025-06-30', 400, filed='2025-07-30'),   # trailing year
        ], unit='USD')})
        self.assertEqual([e['value'] for e in result['revenue']], [110])

    def test_instant_concepts_still_take_the_latest_filing(self):
        result = self._fundamentals({ASSETS: _facts([
            _instant('2025-06-30', 1_000, filed='2025-07-30'),
            _instant('2025-06-30', 1_050, filed='2026-01-30'),   # restated
        ], unit='USD')})
        self.assertEqual([e['value'] for e in result['assets']], [1_050])

    def test_tag_candidates_merge(self):
        """ASC 606 renamed revenue tags — the old and new tag each cover a slice."""
        result = self._fundamentals({
            REVENUES: _facts([_duration('2015-04-01', '2015-06-30', 90)], unit='USD'),
            ('us-gaap', 'RevenueFromContractWithCustomerExcludingAssessedTax'):
                _facts([_duration('2025-04-01', '2025-06-30', 110)], unit='USD'),
        })
        self.assertEqual([e['value'] for e in result['revenue']], [90, 110])

    def test_untagged_concepts_are_omitted(self):
        result = self._fundamentals({ASSETS: _facts([_instant('2025-06-30', 1)], unit='USD')})
        self.assertEqual(list(result), ['assets'])


if __name__ == '__main__':
    unittest.main()
