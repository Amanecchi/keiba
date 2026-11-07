"""Synthetic public-navigation regressions; no official HTML is redistributed."""

import copy
from urllib.parse import parse_qs, urlsplit

import pytest

from keiba_acquisition.cli import Probe, parser
from keiba_acquisition.database import (
    check_identity,
    merge_final_odds,
    month_link,
    result_link,
)
from keiba_acquisition.html import ParseError
from keiba_acquisition.transport import Client
from test_transport import Response, Session

BASE = "https://www.jra.go.jp/JRADB/accessS.html"
REFERENCE = {
    "race_date": "2025-11-02",
    "venue": "東京",
    "meeting_number": 5,
    "meeting_day": 2,
    "race_number": 11,
}
HEADER = """<meta charset="utf-8"><div class="race_header">
  <div class="date">2025年11月2日 5回東京2日</div>
  <div class="race_number"><img alt="11レース"></div>
  <h2>第1回 架空記念</h2><div>コース：2,500メートル（芝・左）</div></div>"""
SEARCH_LINK = (
    "<a onclick=\"doAction('/JRADB/accessS.html','pw01skl00999999/B3')\">過去のレース結果</a>"
)
SEARCH = """<select id="kaisaiY_list"><option value="2025">2025</option></select>
  <select id="kaisaiM_list"><option value="11">11</option></select>
  <script>objParam["2511"]="AB"; var yearMonth = "202611";
  if(Number(arg) >= yearMonth){param = 'pw01skl00' + String(arg) + '/';}
  else{param = 'pw01skl10' + String(arg) + '/';}</script>"""
MONTH = '<a href="/JRADB/accessS.html?CNAME=pw01sde1005202505021120251102/AB">架空記念</a>'


def test_month_selection_uses_published_options_template_and_checksum():
    url = month_link(SEARCH.encode(), BASE, REFERENCE["race_date"])
    assert parse_qs(urlsplit(url).query) == {"CNAME": ["pw01skl10202511/AB"]}
    with pytest.raises(ParseError):
        month_link(SEARCH.replace('objParam["2511"]="AB";', "").encode(), BASE, "2025-11-02")
    with pytest.raises(ParseError):
        month_link(SEARCH.encode(), BASE, "2016-11-06")


def test_result_selection_checks_date_venue_meeting_and_race_number():
    assert "pw01sde10052025050211" in result_link(MONTH.encode(), BASE, REFERENCE)
    for field, value in (("venue", "京都"), ("race_number", 10), ("meeting_day", 3)):
        with pytest.raises(ParseError):
            result_link(MONTH.encode(), BASE, {**REFERENCE, field: value})
    with pytest.raises(ParseError):
        check_identity({**REFERENCE, "race_date": "2025-11-09"}, REFERENCE)


def test_final_odds_join_uses_horse_id_and_is_atomic_on_mismatch():
    result = {
        **REFERENCE,
        "entries": [
            {"horse_number": 1, "horse_id": "2020000001", "win_odds": None},
            {"horse_number": 2, "horse_id": "2020000002", "win_odds": None},
        ],
        "issues": ["missing_win_odds:1,2", "missing_affiliation:1,2"],
    }
    odds = {
        **REFERENCE,
        "odds_state": "final",
        "issues": [],
        "source": {"sha256": "sample"},
        "odds": [
            {"horse_number": 1, "horse_id": "2020000001", "win_odds": "5.0"},
            {"horse_number": 2, "horse_id": "2020000999", "win_odds": "8.0"},
        ],
    }
    original = copy.deepcopy(result)
    with pytest.raises(ParseError, match="馬ID"):
        merge_final_odds(result, odds)
    assert result == original
    odds["odds"][1]["horse_id"] = "2020000002"
    merge_final_odds(result, odds)
    assert result["entries"][0]["win_odds"] == "5.0"
    assert result["issues"] == ["missing_affiliation:1,2"]


def test_database_probe_discovers_supplements_and_resumes_without_network(tmp_path, fixture_bytes):
    result = (
        HEADER
        + """<div class="race_related_link"><a
      onclick="doAction('/JRADB/accessO.html','pw151ou1005202505021120251102Z/AA')">オッズ</a></div>
      <table><tr><th>着順</th><th>枠</th><th>馬番</th><th>馬名</th><th>性齢</th>
      <th>騎手</th><th>人気</th><th>通過順位</th></tr>"""
    )
    odds = (
        HEADER
        + '<div class="refresh_line"><div class="time">最終オッズ</div></div><table><tr><th>馬番</th><th>馬名</th><th>単勝</th><th>複勝（3着払い）</th></tr>'
    )
    for number in (1, 2):
        anchor = (
            f'<a href="/JRADB/accessU.html?CNAME=pw01dud10202000000{number}/AA">架空馬{number}</a>'
        )
        result += f"<tr><td>{number}</td><td>1</td><td>{number}</td><td>{anchor}</td><td>牡5</td><td>架空騎手</td><td>{number}</td><td>1 2</td></tr>"
        odds += f"<tr><td>{number}</td><td>{anchor}</td><td>5.0</td><td>1.5 - 2.0</td></tr>"
    result += "</table><dl><dt>単勝</dt><dd>1 500円</dd><dt>複勝</dt><dd>1 150円 2 180円</dd></dl>"
    odds += "</table>"
    session = Session(
        [
            Response(b)
            for b in (
                fixture_bytes("year_index.html"),
                (HEADER + SEARCH_LINK).encode(),
                SEARCH.encode(),
                MONTH.encode(),
                result.encode(),
                odds.encode(),
                fixture_bytes("profile.html"),
                fixture_bytes("profile.html"),
            )
        ]
    )
    args = parser().parse_args(["--database", "--years", "2025", "--race-name", "架空記念"])
    for expected_attempts in (8, 0):
        probe = Probe(
            Client(tmp_path, session=session, sleep=lambda _: None), progress=lambda _: None
        )
        report, code = probe.run(args)
        assert code == 2 and report["status"] == "partial"  # Historical affiliation is absent.
        assert report["metrics"]["http_attempts"] == expected_attempts
        assert report["metrics"]["parsed_results"] == 1
        assert report["metrics"]["parsed_historical_odds"] == 1
        parsed_result = next(r for r in probe.parsed if r["kind"] == "result")
        assert parsed_result["issues"] == ["missing_affiliation:1,2"]
        assert all(e["win_odds"] == "5.0" for e in parsed_result["entries"])
    assert session.methods == ["GET", "GET", "POST", "POST", "GET", "POST", "GET", "GET"]
