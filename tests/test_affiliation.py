import copy

import pytest

from keiba_acquisition.affiliation import affiliation_at, merge_affiliations, parse_trainer_history
from keiba_acquisition.html import ParseError

URL = "https://www.jra.go.jp/JRADB/accessC.html?CNAME=pw05cmk001001/AA"
HEADER = """<meta charset="utf-8"><a onclick="doAction('/JRADB/accessC.html','pw05cjs001001/AB')">重賞成績</a>
<dl><dt>所属</dt><dd>栗東</dd></dl><div class="block_header"><h3>所属歴</h3></div>"""


def history(body):
    return parse_trainer_history((HEADER + body).encode(), URL)


def test_dated_history_overrides_current_facility_for_earlier_race():
    parsed = history("""<ul><li><div class="time">2000年から2019年</div><div class="event">美浦Ｔ・Ｃ</div></li>
      <li><div class="time">2019年から</div><div class="event">栗東Ｔ・Ｃ</div></li></ul>""")
    assert affiliation_at(parsed, "2016-11-06") == "美浦"
    assert affiliation_at(parsed, "2021-11-07") == "栗東"
    assert affiliation_at(parsed, "2019-11-03") is None
    assert affiliation_at(parsed, "2000-11-05") is None
    assert affiliation_at(parsed, "1999-11-07") is None


def test_current_affiliation_alone_is_insufficient():
    with pytest.raises(ParseError):
        parse_trainer_history(HEADER.split('<div class="block_header">')[0].encode(), URL)


def test_trainer_identity_is_checked_against_the_profile_menu():
    body = (
        HEADER
        + '<ul><li><div class="time">2000年から</div><div class="event">美浦Ｔ・Ｃ</div></li></ul>'
    ).replace("pw05cjs001001", "pw05cjs001002")
    with pytest.raises(ParseError, match="ID"):
        parse_trainer_history(body.encode(), URL)


def test_missing_or_unknown_history_is_not_guessed():
    with pytest.raises(ParseError):
        history('<ul><li><div class="time">不明</div><div class="event">美浦Ｔ・Ｃ</div></li></ul>')


def test_closed_period_does_not_establish_its_entire_end_year():
    parsed = history(
        '<ul><li><div class="time">2000年から2019年</div><div class="event">美浦Ｔ・Ｃ</div></li></ul>'
    )
    assert affiliation_at(parsed, "2018-11-04") == "美浦"
    assert affiliation_at(parsed, "2019-01-01") is None
    assert affiliation_at(parsed, "2020-01-01") is None


def test_merge_preserves_other_missing_fields_and_evidence():
    parsed = history(
        '<ul><li><div class="time">2000年から</div><div class="event">美浦Ｔ・Ｃ</div></li></ul>'
    )
    parsed["source"] = {
        "sha256": "synthetic",
        "url": URL,
        "fetched_at": "2026-11-07T05:00:00+00:00",
    }
    result = {
        "race_date": "2016-11-06",
        "entries": [
            {"horse_number": 1, "trainer_id": "001001", "affiliation": None},
            {"horse_number": 2, "trainer_id": "001002", "affiliation": None},
        ],
        "issues": ["missing_affiliation:1,2", "missing_win_odds:1,2"],
    }
    before = copy.deepcopy(result)
    merge_affiliations(result, {"001001": parsed})
    assert result["entries"][0]["affiliation"] == "美浦"
    assert result["entries"][0]["affiliation_evidence"]["trainer_id"] == "001001"
    assert result["entries"][1] == before["entries"][1]
    assert result["issues"] == ["missing_win_odds:1,2", "missing_affiliation:2"]


def test_first_license_year_can_resolve_an_official_result_for_that_trainer():
    body = (
        HEADER
        + '<dl><dt>免許取得年</dt><dd>2019年</dd></dl><ul><li><div class="time">2019年から</div><div class="event">栗東Ｔ・Ｃ</div></li></ul>'
    )
    parsed = parse_trainer_history(body.encode(), URL)
    assert parsed["license_year"] == 2019
    assert affiliation_at(parsed, "2019-11-03") == "栗東"
    assert affiliation_at(parsed, "2018-11-04") is None


def test_open_ended_cached_history_is_not_projected_after_its_observation():
    parsed = history(
        '<ul><li><div class="time">2000年から</div><div class="event">美浦Ｔ・Ｃ</div></li></ul>'
    )
    parsed["source"] = {"fetched_at": "2020-11-07T05:00:00+00:00"}
    result = {
        "race_date": "2025-11-09",
        "entries": [{"horse_number": 1, "trainer_id": "001001", "affiliation": None}],
        "issues": ["missing_affiliation:1"],
    }
    merge_affiliations(result, {"001001": parsed})
    assert result["entries"][0]["affiliation"] is None
    parsed["source"]["fetched_at"] = "2026-11-07T05:00:00+00:00"
    merge_affiliations(result, {"001001": parsed})
    assert result["entries"][0]["affiliation"] == "美浦" and result["issues"] == []
