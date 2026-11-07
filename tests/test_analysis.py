import copy

import pytest

from keiba_web.analysis import InvalidQuery, analyze


def entries():
    return [
        {
            "id": str(i),
            "outcome": "finished",
            "finish": finish,
            "features": {"previous_race": previous, "previous_finish": pf, "frame": frame},
            "bets": {
                "単勝": {"eligible": True, "hit": win > 0, "payout_yen": win},
                "複勝": {"eligible": True, "hit": place > 0, "payout_yen": place},
            },
        }
        for i, (previous, pf, frame, finish, win, place) in enumerate(
            [
                ("神戸新聞杯", 1, 1, 1, 600, 200),
                ("神戸新聞杯", 3, 2, 2, 0, 150),
                ("神戸新聞杯", 4, 1, 4, 0, 0),
                ("別レース", 2, 2, 3, 0, 180),
            ]
        )
    ]


def test_historical_manual_example_filter_group_rates_and_returns():
    result = analyze(
        entries(),
        [
            {"field": "previous_race", "op": "in", "values": ["神戸新聞杯"]},
            {"field": "previous_finish", "op": "range", "max": 3},
        ],
        "frame",
    )
    assert result["summary"]["started_count"] == 2
    assert result["summary"]["win_rate"] == "50.0"
    assert result["summary"]["bets"]["単勝"]["return_rate"] == "300.0"
    assert result["summary"]["bets"]["複勝"]["return_rate"] == "175.0"
    assert sum(g["matched_count"] for g in result["groups"]) == len(result["entries"]) == 2


def test_no_matches_is_not_zero_wins_and_conditions_are_not_relaxed():
    zero = analyze(entries(), [{"field": "previous_race", "op": "in", "values": ["存在しない"]}])
    assert zero["summary"]["win_rate"] is None
    losses = entries()[2:3] * 8
    assert analyze(losses)["summary"]["win_rate"] == "0.0"


def test_unknown_filters_and_known_nonmatches_follow_three_valued_logic():
    data = entries()
    data[0]["features"]["previous_finish"] = None
    data[1]["features"]["frame"] = None
    result = analyze(
        data,
        [
            {"field": "previous_finish", "op": "range", "max": 3},
            {"field": "previous_race", "op": "in", "values": ["別レース"]},
        ],
        "frame",
    )
    assert result["condition_counts"] == {"match": 1, "nonmatch": 3, "unknown": 0}
    unknown = analyze(data, [{"field": "previous_finish", "op": "range", "max": 3}], "frame")
    assert unknown["condition_counts"]["unknown"] == 1
    assert any(g["label"] == "不明" for g in unknown["groups"])


def test_dnf_nonstarts_refunds_and_unverified_payouts_use_separate_denominators():
    data = entries()
    dnf = copy.deepcopy(data[2])
    dnf.update(id="dnf", outcome="dnf", finish=None)
    data.append(dnf)
    cancelled = copy.deepcopy(dnf)
    cancelled.update(id="cancelled", outcome="nonstarter")
    cancelled["bets"] = {
        k: {"eligible": False, "reason": "nonstarter", "payout_yen": None, "hit": None}
        for k in ["単勝", "複勝"]
    }
    data.append(cancelled)
    data[3]["bets"]["単勝"] = {"eligible": None, "payout_yen": None, "hit": None}
    result = analyze(data)["summary"]
    assert result["started_count"] == 5 and result["win_rate"] == "20.0"
    assert result["nonstarter_count"] == 1
    assert result["bets"]["単勝"]["purchase_yen"] == 400
    assert result["bets"]["単勝"]["return_rate"] == "150.0"
    assert result["bets"]["単勝"]["unknown_count"] == 1


def test_known_previous_dnf_is_nonmatching_in_a_numeric_finish_filter():
    data = entries()
    data[0]["features"].update(previous_finish=None, previous_finish_status="dnf")
    result = analyze(data, [{"field": "previous_finish", "op": "range", "max": 3}])
    assert result["condition_counts"] == {"match": 2, "nonmatch": 2, "unknown": 0}


@pytest.mark.parametrize(
    "filters",
    [
        [{"field": "unknown", "op": "in", "values": [1]}],
        [{"field": "frame", "op": "range", "min": "NaN"}],
        [{"field": "frame", "op": "range", "min": 3, "max": 1}],
        [{"field": "frame", "op": "in", "values": []}],
        [{"field": "frame", "op": "in", "values": [1]}] * 2,
    ],
)
def test_invalid_conditions_are_rejected_instead_of_silently_ignored(filters):
    with pytest.raises(InvalidQuery):
        analyze(entries(), filters)
