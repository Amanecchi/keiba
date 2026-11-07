"""Synthetic regressions for historical evidence, identities, and feature boundaries."""

import copy
import json

import pytest
from bs4 import BeautifulSoup

from keiba_acquisition.cli import Probe, main
from keiba_acquisition.history import (
    derive_features,
    parse_prior_result,
    prior_reference,
    prior_runs,
    race_condition,
)
from keiba_acquisition.html import ParseError
from keiba_acquisition.transport import Client
from test_transport import Response, Session

HORSE = "2020000001"
BASE = "https://www.jra.go.jp/JRADB/accessS.html"


def run(day, finish="1", **extra):
    return {
        "race_date": f"2025-10-{day:02}",
        "race_name": "架空競走",
        "record_type": "race",
        "finish_text": finish,
        "source_url": f"{BASE}?CNAME=pw01sde10052025040110202510{day:02}/AB",
        **extra,
    }


def profile():
    return {
        "kind": "profile",
        "horse_id": HORSE,
        "target_date": "2025-11-02",
        "history": [run(1), run(5), run(10)],
        "source": {"url": f"https://www.jra.go.jp/JRADB/accessU.html?CNAME=pw01dud10{HORSE}/AA"},
    }


def condition_html(label="オープン", grade=None, surface="芝"):
    icon = f'<span class="grade_icon"><img alt="{grade}"></span>' if grade else ""
    return (
        '<div class="race_header"><div>2025年10月10日 4回東京1日</div>'
        '<div class="race_number"><img alt="10レース"></div>'
        f'<h2>第1回<span class="race_name">架空競走{icon}</span></h2>'
        f'<div class="type"><div class="class">{label}</div>'
        f'<div class="course">コース：2,000メートル<span class="detail">（{surface}・左）</span>'
        "</div></div></div>"
    )


def result_html(*, horse=HORSE, finish="1", positions="1 2 3 4", cancelled=True):
    return (
        condition_html("3勝クラス")
        + "<table><tr><th>馬番</th><th>馬名</th><th>着順</th><th>通過順位</th></tr>"
        + f'<tr><td>1</td><td><a href="/JRADB/accessU.html?CNAME=pw01dud10{horse}/AB">'
        + f"架空馬A</a></td><td>{finish}</td><td>{positions}</td></tr>"
        + "".join(
            f"<tr><td>{n}</td><td>架空馬{n}</td><td>{n}</td><td>2 3</td></tr>" for n in range(2, 6)
        )
        + ("<tr><td>6</td><td>架空馬B</td><td>取消</td><td></td></tr>" if cancelled else "")
        + "</table><dl><dt>単勝</dt><dd>1 500円</dd><dt>複勝</dt><dd>1 150円 2 180円</dd></dl>"
    ).encode()


def samples(p, first_positions):
    return [
        {
            "horse_id": HORSE,
            "target_date": p["target_date"],
            "race_date": r["race_date"],
            "run_url": r["source_url"],
            "condition": {"code": "allowance_3", "surface": "芝"},
            "starters": 7,
            "passing_positions": [position],
        }
        for r, position in zip(prior_runs(p), first_positions)
    ]


@pytest.mark.parametrize(
    "label,grade,expected",
    [
        ("1600万円以下", None, "allowance_3"),
        ("３勝クラス（１，６００万円以下）", None, "allowance_3"),
        ("3勝クラス", None, "allowance_3"),
        ("1勝クラス（500万円以下）", None, "allowance_1"),
        ("1000万円以下", None, "allowance_2"),
        ("オープン", "GⅠ", "G1"),
        ("オープン", "GIII", "G3"),
        ("オープン", "JpnⅡ", "Jpn2"),
        ("オープン", "リステッド", "listed"),
        ("オープン", None, "open"),
    ],
)
def test_official_condition_labels_are_normalized_with_raw_evidence(label, grade, expected):
    value = race_condition(BeautifulSoup(condition_html(label, grade), "html.parser"))
    assert value["code"] == expected
    assert value["raw_class"] == label
    assert value["surface"] == "芝"


def test_missing_class_is_not_inferred_from_name_or_prize_money():
    value = race_condition(
        BeautifulSoup("<h2>架空GⅠ</h2><p>1600万円以下 1着賞金</p>", "html.parser")
    )
    assert value["code"] is None
    assert value["issues"] == ["unknown_race_class"]
    conflicting = race_condition(
        BeautifulSoup(condition_html("3勝クラス（500万円以下）"), "html.parser")
    )
    assert conflicting["code"] is None
    with pytest.raises(ParseError, match="矛盾"):
        race_condition(BeautifulSoup(condition_html("3勝クラス", "GⅠ"), "html.parser"))
    jump_grade = race_condition(
        BeautifulSoup(condition_html("オープン", "J・GⅠ", surface="芝"), "html.parser")
    )
    assert jump_grade["surface"] == "障害"
    jump_maiden = condition_html("未勝利").replace(
        '<div class="type">', '<div class="type"><div class="category">障害3歳以上</div>'
    )
    assert race_condition(BeautifulSoup(jump_maiden, "html.parser"))["surface"] == "障害"


def test_history_selection_ignores_future_registration_and_nonstarts_but_retains_dnf():
    p = profile()
    p["history"] += [
        run(20, "取消"),
        run(19, "除外"),
        run(18, "取止"),
        run(17, "", record_type="administrative"),
        run(16, "中止"),
        run(15, "失格"),
        run(30, race_date="2025-11-02"),
        run(31, race_date="2025-11-03"),
    ]
    assert [(r["race_date"], r["finish_text"]) for r in prior_runs(p)] == [
        ("2025-10-16", "中止"),
        ("2025-10-15", "失格"),
        ("2025-10-10", "1"),
    ]
    p["history"].append(run(16, "中止"))
    with pytest.raises(ParseError, match="重複"):
        prior_runs(p)


def test_supplementary_reference_checks_published_date_and_all_race_identity_fields():
    r = run(10)
    assert prior_reference(r) == {
        "race_date": "2025-10-10",
        "venue": "東京",
        "meeting_number": 4,
        "meeting_day": 1,
        "race_number": 10,
    }
    for bad in [
        {**r, "race_date": "2025-10-09"},
        {**r, "source_url": r["source_url"].replace("20250401", "20240401")},
        {**r, "source_url": r["source_url"].replace("www.jra.go.jp", "example.com")},
    ]:
        with pytest.raises(ParseError):
            prior_reference(bad)


def test_supplementary_join_excludes_cancellations_from_field_size():
    value = parse_prior_result(result_html(), BASE, run(10), HORSE, "2025-11-02")
    assert value["starters"] == 5
    assert value["passing_positions"] == [1, 2, 3, 4]
    assert value["condition"]["code"] == "allowance_3"
    assert value["issues"] == []


@pytest.mark.parametrize("failure", ["horse", "date", "venue", "rank", "future"])
def test_wrong_auxiliary_result_cannot_contribute_to_features(failure):
    body, target = result_html(), "2025-11-02"
    if failure == "horse":
        body = result_html(horse="2020000999")
    elif failure == "date":
        body = body.replace("10月10日".encode(), "10月9日".encode())
    elif failure == "venue":
        body = body.replace("4回東京1日".encode(), "4回京都1日".encode())
    elif failure == "rank":
        body = result_html(finish="2")
    else:
        target = "2025-10-10"
    with pytest.raises(ParseError):
        parse_prior_result(body, BASE, run(10), HORSE, target)


def test_unknown_positions_are_not_zero_or_a_fake_front_runner():
    value = parse_prior_result(result_html(positions="不明"), BASE, run(10), HORSE, "2025-11-02")
    assert value["passing_positions"] is None
    assert "unknown_passing_positions" in value["issues"]
    invalid = parse_prior_result(result_html(positions="0 8"), BASE, run(10), HORSE, "2025-11-02")
    assert invalid["passing_positions"] is None
    invalid_finish = parse_prior_result(
        result_html(finish="0"), BASE, run(10, finish="0"), HORSE, "2025-11-02"
    )
    assert invalid_finish["starters"] is None
    assert "unknown_start_status" in invalid_finish["issues"]


@pytest.mark.parametrize(
    "positions,expected",
    [([1, 1, 7], "逃げ"), ([1, 3, 3], "先行"), ([5, 5, 7], "差し"), ([6, 7, 7], "追込")],
)
def test_running_style_uses_prior_first_corner_and_exact_third_boundaries(positions, expected):
    p = profile()
    result = derive_features(p, samples(p, positions))
    assert result["running_style"]["value"] == expected
    assert result["running_style"]["usable_runs"] == 3
    assert result["previous_class"]["code"] == "allowance_3"


def test_missing_or_unsupported_runs_do_not_trigger_selection_of_older_history():
    p = profile()
    p["history"].append(run(2))
    data = samples(p, [1, 1, 1])
    data[1]["condition"]["surface"] = "障害"
    data[2]["passing_positions"] = None
    result = derive_features(p, data)
    assert result["running_style"]["value"] is None
    assert result["running_style"]["usable_runs"] == 1
    assert result["running_style"]["candidate_runs"] == 3
    assert "insufficient_running_style_evidence" in result["issues"]


def test_other_horse_date_and_duplicate_evidence_are_rejected_atomically():
    p = profile()
    data = samples(p, [1, 2, 3])
    original = copy.deepcopy(data)
    for field, bad in [("horse_id", "2020000999"), ("target_date", "2025-11-03")]:
        changed = copy.deepcopy(data)
        changed[0][field] = bad
        with pytest.raises(ParseError):
            derive_features(p, changed)
    with pytest.raises(ParseError, match="重複"):
        derive_features(p, data + [data[0]])
    assert data == original


def test_supplement_acquisition_is_serial_resumable_and_does_not_fetch_future(tmp_path):
    p = profile()
    p["history"] = [run(10), run(9)]
    p["history"].append(run(30, race_date="2026-10-30"))
    session = Session(
        [
            Response(result_html()),
            Response(result_html().replace("10月10日".encode(), "10月9日".encode())),
        ]
    )
    for attempts in (2, 0):
        client = Client(tmp_path, session=session, sleep=lambda _: None)
        probe = Probe(client, progress=lambda _: None)
        probe.history_features(p)
        assert client.attempts == attempts
        result = probe.parsed[-1]
        assert result["kind"] == "historical_features"
        assert result["running_style"]["value"] == "逃げ"
        path = tmp_path / probe.records[-1]["parsed_path"]
        assert json.loads(path.read_text())["prior_runs"][0]["result"]["source"]["sha256"]
    assert len(session.calls) == 2


def test_auxiliary_http_block_preserves_previous_raw_and_stops_remaining_fetches(tmp_path):
    p = profile()
    session = Session([Response(result_html()), Response(b"", status=403)])
    probe = Probe(Client(tmp_path, session=session, sleep=lambda _: None), progress=lambda _: None)
    from keiba_acquisition.transport import AcquisitionError

    with pytest.raises(AcquisitionError) as raised:
        probe.history_features(p)
    assert raised.value.stop
    assert len(session.calls) == 2
    assert len(list((tmp_path / "raw").rglob("*.html"))) == 1


def test_current_only_rejects_historical_feature_flag_before_network():
    with pytest.raises(SystemExit):
        main(["--current-only", "--history-features", "--odds-url", BASE])
