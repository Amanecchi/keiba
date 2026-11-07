from datetime import date

import pytest

from keiba_acquisition.catalog import parse_catalog, resolve_series
from keiba_acquisition.html import ParseError


@pytest.mark.parametrize(
    "old,new",
    [
        ("府中牝馬S", "アイルランドT"),
        ("マーメイドS", "府中牝馬S"),
        ("愛知杯", "小倉牝馬S"),
        ("京都牝馬S", "愛知杯"),
        ("東海S", "プロキオンS"),
        ("プロキオンS", "東海S"),
        ("アーリントンC", "チャーチルダウンズC"),
        ("小倉2歳S", "中京2歳S"),
    ],
)
def test_dated_official_successors_are_grouped_instead_of_reused_names(old, new):
    assert resolve_series(old, 2024)["id"] == resolve_series(new, 2025)["id"]
    if old in {"府中牝馬S", "愛知杯", "東海S", "プロキオンS"}:
        assert resolve_series(old, 2024)["id"] != resolve_series(old, 2025)["id"]


def test_unregistered_or_fuzzy_series_is_rejected():
    with pytest.raises(ParseError):
        resolve_series("大阪", 2025)
    assert resolve_series("産経大阪杯", 2016)["id"] == resolve_series("大阪杯", 2017)["id"]


def table(rows):
    return (
        "<table><tr><th>月日</th><th>レース名</th><th>競馬場</th><th>性齢</th><th>コース</th><th>結果</th></tr>"
        + rows
        + "</table>"
    ).encode()


def row(name="大阪杯", day="1月1日", link="/datafile/seiseki/replay/2025/001.html", grade="GⅠ"):
    return f'<tr><td>{day}</td><td><span class="grade_icon">{grade}</span>{name}</td><td>阪神</td><td>4歳以上</td><td>芝2,000メートル</td><td><a href="{link}">結果</a></td></tr>'


def test_catalog_binds_public_result_url_date_venue_and_series():
    result = parse_catalog(
        table(row()), "https://www.jra.go.jp/datafile/seiseki/replay/2025/jyusyo.html"
    )
    race = result["races"][0]
    assert (race["race_date"], race["venue"], race["distance_m"]) == ("2025-01-01", "阪神", 2000)
    assert race["series_id"] == resolve_series("大阪杯", 2025)["id"]
    with pytest.raises(ParseError, match="重複"):
        parse_catalog(
            table(row() + row()), "https://www.jra.go.jp/datafile/seiseki/replay/2025/jyusyo.html"
        )


def test_current_catalog_does_not_treat_future_or_unfinished_races_as_zero_results():
    body = table(
        row()
        + row("京都金杯", "1月2日", "#")
        + row("中山金杯", "1月3日", "/datafile/seiseki/replay/2025/003.html")
    )
    result = parse_catalog(
        body,
        "https://www.jra.go.jp/datafile/seiseki/replay/2025/jyusyo.html",
        as_of=date(2025, 1, 2),
    )
    assert len(result["races"]) == 1
    assert result["pending"] == [{"race_date": "2025-01-02", "name_at_time": "京都金杯"}]
