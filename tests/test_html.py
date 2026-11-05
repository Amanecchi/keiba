from datetime import date

import pytest

from keiba_acquisition.html import (
    ParseError,
    discover_results,
    parse_entries,
    parse_odds,
    parse_profile,
    parse_result,
)

BASE = "https://www.jra.go.jp/datafile/seiseki/replay/2025/001.html"


def test_discovery_filters_name_and_skips_pdf_and_external_links(fixture_bytes):
    found = discover_results(fixture_bytes("year_index.html"), BASE, "架空記念")
    assert found == [{"url": BASE, "race_name": "架空記念 GⅡ"}]
    all_links = discover_results(fixture_bytes("year_index.html"), BASE)
    assert len(all_links) == 2
    assert all_links[1]["url"].endswith("accessS.html?CNAME=sample-race%2Ftoken")


def test_no_result_is_not_a_successful_empty_import(fixture_bytes):
    with pytest.raises(ParseError):
        discover_results(fixture_bytes("year_index.html"), BASE, "存在しない杯")


def test_result_images_rowspan_and_missing_values_are_preserved(fixture_bytes):
    parsed = parse_result(fixture_bytes("result.html"), BASE)
    assert parsed["race_date"] == "2025-11-02"
    assert parsed["distance_m"] == 2500
    assert parsed["title"] == "第1回架空記念 GⅡ"
    assert parsed["entries"][0]["frame"] == 1
    assert parsed["entries"][0]["horse_name"] == "架空馬A"
    assert parsed["entries"][0]["horse_source_key"] == "sample-horse-a/token"
    assert parsed["entries"][2]["finish"] is None
    assert parsed["entries"][2]["finish_text"] == "取消"
    assert parsed["entries"][2]["popularity"] is None
    assert parsed["payouts"] == [
        {"bet_type": "単勝", "horse_number": 1, "yen_per_100": 500},
        {"bet_type": "複勝", "horse_number": 1, "yen_per_100": 150},
        {"bet_type": "複勝", "horse_number": 2, "yen_per_100": 180},
    ]
    assert "missing_horse_url:3" in parsed["issues"]


def test_missing_odds_are_not_derived_from_winner_payout(fixture_bytes):
    body = fixture_bytes("result.html").replace(
        b"<th>" + "単勝オッズ".encode() + b"</th>", b"<th>unavailable</th>"
    )
    parsed = parse_result(body, BASE)
    assert parsed["entries"][0]["win_odds"] is None
    assert "missing_win_odds:1,2,3" in parsed["issues"]


def test_definition_list_payouts_and_dead_heat():
    from bs4 import BeautifulSoup
    from keiba_acquisition.html import payouts

    soup = BeautifulSoup(
        "<dl><dt>単勝</dt><dd><ul><li>1 500円 1番人気</li><li>2 600円 2番人気</li></ul></dd><dt>複勝</dt><dd>1 150円 2 180円</dd></dl>",
        "html.parser",
    )
    result = payouts(soup)
    assert [(r["bet_type"], r["horse_number"], r["yen_per_100"]) for r in result] == [
        ("単勝", 1, 500),
        ("単勝", 2, 600),
        ("複勝", 1, 150),
        ("複勝", 2, 180),
    ]


def test_duplicate_horse_number_is_rejected(fixture_bytes):
    body = fixture_bytes("result.html").replace(b"<td>2</td><td><a", b"<td>1</td><td><a")
    with pytest.raises(ParseError, match="重複"):
        parse_result(body, BASE)


def test_unknown_layout_is_rejected_instead_of_zero_runners():
    with pytest.raises(ParseError):
        parse_result(b"<html><p>Maintenance</p></html>", BASE)


def test_shift_jis_encoding(fixture_bytes):
    body = (
        fixture_bytes("result.html")
        .decode()
        .replace('charset="utf-8"', 'charset="shift_jis"')
        .encode("cp932")
    )
    assert parse_result(body, BASE)["entries"][0]["horse_name"] == "架空馬A"


def test_previous_run_excludes_future_current_and_cancelled_runs(fixture_bytes):
    parsed = parse_profile(fixture_bytes("profile.html"), BASE, date(2025, 11, 2))
    assert parsed["sire"] == "架空種牡馬A"
    assert parsed["previous_run"]["race_date"] == "2025-10-05"
    assert parsed["previous_run"]["distance"] == "芝2400"
    assert parsed["previous_run"]["finish"] == 3
    assert len(parsed["history"]) == 5
    assert parsed["issues"] == []


def test_missing_previous_run_is_not_zero(fixture_bytes):
    parsed = parse_profile(fixture_bytes("profile.html"), BASE, date(2020, 1, 1))
    assert parsed["previous_run"] is None
    assert "missing_previous_run" in parsed["issues"]


def test_unpublished_and_not_on_sale_are_distinct():
    pending = "<html><p>出馬表は11月5日に公開予定です。</p></html>".encode()
    entries = parse_entries(pending, BASE)
    assert entries["status"] == "not_yet_published"
    odds = parse_odds("<html><p>発売開始前です。</p></html>".encode(), BASE)
    assert odds["status"] == "not_on_sale"


def test_live_odds_range_time_and_cancellation(fixture_bytes):
    parsed = parse_odds(fixture_bytes("odds.html"), BASE)
    assert parsed["provider_timestamp_text"] == "11月9日 14時05分現在"
    assert parsed["odds"][0]["win_odds"] == "12.0"
    assert parsed["odds"][0]["place_lower"] == "2.0"
    assert parsed["odds"][0]["place_upper"] == "3.0"
    assert parsed["odds"][1]["win_odds"] is None
    assert parsed["odds"][1]["availability"] == "cancelled"
    assert parsed["issues"] == []


def test_provider_time_is_not_substituted_with_download_time(fixture_bytes):
    body = fixture_bytes("odds.html").replace("11月9日 14時05分現在".encode(), b"")
    parsed = parse_odds(body, BASE)
    assert parsed["provider_timestamp_text"] is None
    assert "missing_provider_timestamp" in parsed["issues"]


def test_unreadable_runner_number_is_not_silently_dropped(fixture_bytes):
    body = fixture_bytes("result.html").replace(b"<td>2</td><td><a", b"<td>unknown</td><td><a")
    with pytest.raises(ParseError, match="馬番を認識"):
        parse_result(body, BASE)


def test_payout_count_mismatch_is_rejected(fixture_bytes):
    body = fixture_bytes("result.html").replace(b"<td>1</td><td>500", b"<td>1 2</td><td>500")
    with pytest.raises(ParseError, match="件数が一致"):
        parse_result(body, BASE)


def test_win_payout_for_another_runner_is_reported(fixture_bytes):
    body = fixture_bytes("result.html").replace(b"<td>1</td><td>500", b"<td>2</td><td>500")
    assert "win_payout_finish_mismatch" in parse_result(body, BASE)["issues"]


def test_inverted_place_odds_are_not_accepted(fixture_bytes):
    body = fixture_bytes("odds.html").replace(b"2.0 - 3.0", b"3.0 - 2.0")
    result = parse_odds(body, BASE)
    assert result["odds"][0]["place_lower"] is None
    assert "missing_odds_values" in result["issues"]


def test_legacy_side_by_side_payouts_never_include_other_bets():
    from bs4 import BeautifulSoup
    from keiba_acquisition.html import payouts

    body = """<table>
      <tr><td>単勝</td><td>1</td><td>500円</td><td>1番人気</td>
          <td>馬連</td><td>1-2</td><td>900円</td><td>2番人気</td></tr>
      <tr><td rowspan="2">複勝</td><td rowspan="2">1<br>2</td>
          <td rowspan="2">150円<br>180円</td><td rowspan="2">1番人気<br>2番人気</td>
          <td>ワイド</td><td>1-2</td><td>300円</td></tr>
      <tr><td>3連単</td><td>1-2-3</td><td>2000円</td></tr>
    </table>"""
    assert payouts(BeautifulSoup(body, "html.parser")) == [
        {"bet_type": "単勝", "horse_number": 1, "yen_per_100": 500},
        {"bet_type": "複勝", "horse_number": 1, "yen_per_100": 150},
        {"bet_type": "複勝", "horse_number": 2, "yen_per_100": 180},
    ]


def test_legacy_race_identity_and_distance_do_not_require_modern_headers():
    from bs4 import BeautifulSoup
    from keiba_acquisition.html import metadata

    body = """<table><tr><th class="header3">第５回 架空記念</th></tr></table>
      <p>2016年11月6日（日） 5回東京2日</p><p class="specialRace">11R 第5回 架空記念</p>
      <table><tr><td>2500m</td><td>芝・左</td></tr></table>"""
    parsed = metadata(BeautifulSoup(body, "html.parser"))
    assert parsed["race_date"] == "2016-11-06" and parsed["distance_m"] == 2500
    assert parsed["venue"] == "東京" and parsed["race_number"] == 11
    assert parsed["meeting_number"] == 5 and parsed["meeting_day"] == 2


def test_detailed_card_before_number_assignment_preserves_horse_identity():
    body = """<meta charset="utf-8"><table><thead><tr><th>枠</th><th>馬番</th>
      <th>馬名<br>調教師名<br>血統</th><th>性齢/毛色<br>負担重量<br>騎手名</th></tr></thead>
      <tbody><tr><td></td><td><img alt="ブリンカー着用"></td><td class="horse">
      <div class="name"><a href="/JRADB/accessU.html?CNAME=pw01dud002020000001/AA">架空馬A</a></div>
      <p class="trainer"><a>架空調教師</a><span class="division">(美浦)</span></p>
      <ul><li class="sire"><span>父：</span>架空種牡馬</li></ul></td>
      <td><p class="age">牡5/鹿</p><p class="jockey"><a>架空騎手</a></p></td></tr></tbody></table>""".encode()
    parsed = parse_entries(body, BASE)
    entry = parsed["entries"][0]
    assert entry["horse_number"] is None and entry["frame"] is None
    assert entry["horse_name"] == "架空馬A" and entry["horse_id"] == "2020000001"
    assert entry["sire"] == "架空種牡馬" and entry["affiliation"] == "美浦"
    assert entry["jockey"] == "架空騎手" and entry["sex_age"] == "牡5"
    assert parsed["issues"] == ["unassigned_horse_number", "unassigned_frame"]


def test_final_odds_are_distinguished_from_live_quotes(fixture_bytes):
    body = fixture_bytes("odds.html").replace(
        "11月9日 14時05分現在".encode(), "最終オッズ".encode()
    )
    body = body.replace("複勝オッズ".encode(), "複勝（3着払い）".encode())
    parsed = parse_odds(body, BASE)
    assert parsed["odds_state"] == "final" and parsed["provider_timestamp_text"] is None
    assert parsed["issues"] == [] and parsed["odds"][0]["win_odds"] == "12.0"


def test_unpublished_odds_are_not_final_or_zero_quotes():
    parsed = parse_odds("<p>今週のオッズは未発表です。</p>".encode(), BASE)
    assert parsed["status"] == "not_yet_published" and parsed["odds"] == []


def test_registration_transfer_is_not_a_previous_race(fixture_bytes):
    body = fixture_bytes("profile.html").replace(
        b"</table>",
        "<tr><td>2025.10.12</td><td>JRAへ転入</td><td></td><td></td></tr></table>".encode(),
    )
    parsed = parse_profile(body, BASE, date(2025, 11, 2))
    assert parsed["previous_run"]["race_date"] == "2025-10-05"
    assert any(run["record_type"] == "administrative" for run in parsed["history"])
    assert parsed["issues"] == []


def test_did_not_finish_is_a_real_previous_start(fixture_bytes):
    body = fixture_bytes("profile.html").replace(b"<td>3</td>", "<td>中止</td>".encode())
    parsed = parse_profile(body, BASE, date(2025, 11, 2))
    assert parsed["previous_run"]["race_date"] == "2025-10-05"
    assert parsed["previous_run"]["finish"] is None
    assert parsed["previous_run"]["finish_text"] == "中止" and parsed["issues"] == []
