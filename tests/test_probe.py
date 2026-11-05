import json

import pytest
import requests
from bs4 import BeautifulSoup

from keiba_acquisition.cli import Probe, parser, years
from keiba_acquisition.transport import Client
from test_transport import Response, Session


def result_without_cancelled_runner(body):
    soup = BeautifulSoup(body, "html.parser")
    for row in list(soup.find_all("tr")):
        if "取消" in row.get_text():
            row.decompose()
    # A complete synthetic result includes an explicit historical affiliation.
    header = soup.find("table").find("tr")
    th = soup.new_tag("th")
    th.string = "所属"
    header.append(th)
    for row in soup.find("table").find_all("tr")[1:]:
        td = soup.new_tag("td")
        td.string = "美浦"
        row.append(td)
    return str(soup).encode()


def test_automatic_discovery_result_profile_and_resumption(tmp_path, fixture_bytes):
    result = result_without_cancelled_runner(fixture_bytes("result.html"))
    session = Session(
        [
            Response(fixture_bytes("year_index.html")),
            Response(result),
            Response(fixture_bytes("profile.html")),
            Response(fixture_bytes("profile.html")),
        ]
    )
    args = parser().parse_args(["--years", "2025", "--race-name", "架空記念"])
    client = Client(tmp_path, session=session, sleep=lambda _: None)
    report, code = Probe(client, progress=lambda _: None).run(args)
    assert code == 0 and report["status"] == "sample_passed"
    assert report["metrics"]["parsed_profiles"] == 2
    assert report["metrics"]["parsed_entries"] == 2
    assert len(session.calls) == 4
    assert not report["production_ready"] and not report["ten_year_coverage_verified"]
    assert json.loads((tmp_path / "latest_report.json").read_text())["status"] == "sample_passed"
    second_client = Client(tmp_path, session=session)
    report, code = Probe(second_client, progress=lambda _: None).run(args)
    assert code == 0
    assert report["metrics"]["cache_hits"] == 4
    assert report["metrics"]["http_attempts"] == 0
    assert len(session.calls) == 4


def test_missing_fields_make_probe_partial(tmp_path, fixture_bytes):
    args = parser().parse_args(
        ["--result-url", "https://www.jra.go.jp/datafile/seiseki/replay/2025/001.html"]
    )
    session = Session(
        [
            Response(fixture_bytes("result.html")),
            Response(fixture_bytes("profile.html")),
            Response(fixture_bytes("profile.html")),
        ]
    )
    client = Client(tmp_path, session=session, sleep=lambda _: None)
    report, code = Probe(client, progress=lambda _: None).run(args)
    assert code == 2 and report["status"] == "partial"
    assert report["feasibility_decision"] == "undetermined"
    assert "missing_horse_url:3" in report["resources"][0]["issues"]


def test_proxy_stop_writes_report_and_does_not_try_remaining_years(tmp_path):
    args = parser().parse_args(["--years", "2016,2021,2025"])
    session = Session([requests.exceptions.ProxyError("403 CONNECT")])
    client = Client(tmp_path, session=session)
    report, code = Probe(client, progress=lambda _: None).run(args)
    assert code == 3 and report["status"] == "blocked_environment"
    assert report["feasibility_decision"] == "undetermined"
    assert report["metrics"]["http_attempts"] == 1
    assert report["metrics"]["parsed_results"] == 0
    assert (tmp_path / "latest_report.json").exists()


def test_current_card_and_odds_are_refetched_while_history_resumes(tmp_path, fixture_bytes):
    result = result_without_cancelled_runner(fixture_bytes("result.html"))
    args = parser().parse_args(
        [
            "--years",
            "2025",
            "--race-name",
            "架空記念",
            "--upcoming-url",
            "https://www.jra.go.jp/keiba/race/101/syutsuba.html",
        ]
    )
    session = Session(
        [
            Response(fixture_bytes("year_index.html")),
            Response(result),
            Response(fixture_bytes("profile.html")),
            Response(fixture_bytes("profile.html")),
            Response(fixture_bytes("entries.html")),
            Response(fixture_bytes("odds.html")),
            Response(fixture_bytes("entries.html")),
            Response(fixture_bytes("odds.html")),
        ]
    )
    for expected_attempts in (6, 2):
        client = Client(tmp_path, session=session, sleep=lambda _: None)
        report, code = Probe(client, progress=lambda _: None).run(args)
        assert code == 0
        assert report["metrics"]["http_attempts"] == expected_attempts
        assert [r["kind"] for r in report["resources"]][-2:] == ["entries", "odds"]
    assert len(session.calls) == 8


@pytest.mark.parametrize(
    "value, expected", [("2016,2021,2025", [2016, 2021, 2025]), ("2016:2018", [2016, 2017, 2018])]
)
def test_year_selection(value, expected):
    assert years(value) == expected


def test_relative_horse_links_use_the_final_response_location(tmp_path, fixture_bytes):
    result = result_without_cancelled_runner(fixture_bytes("result.html"))
    result = result.replace(b"/JRADB/accessU.html", b"accessU.html")
    url = "https://www.jra.go.jp/datafile/seiseki/replay/2025/001.html"
    session = Session(
        [
            Response(
                status=302, headers={"Location": "/JRADB/accessS.html?CNAME=sample-result/token"}
            ),
            Response(result),
            Response(fixture_bytes("profile.html")),
            Response(fixture_bytes("profile.html")),
        ]
    )
    args = parser().parse_args(["--result-url", url])
    report, code = Probe(
        Client(tmp_path, session=session, sleep=lambda _: None), progress=lambda _: None
    ).run(args)
    assert code == 0
    assert session.calls[2][0].startswith("https://www.jra.go.jp/JRADB/accessU.html?")
    assert report["resources"][0]["response_url"].startswith(
        "https://www.jra.go.jp/JRADB/accessS.html?"
    )


def test_ambiguous_current_card_links_are_not_guessed(tmp_path):
    body = b"""<html><a href="/JRADB/accessD.html?CNAME=one">one</a>
        <a href="/JRADB/accessD.html?CNAME=two">two</a></html>"""
    session = Session([Response(body)])
    probe = Probe(Client(tmp_path, session=session), progress=lambda _: None)
    probe.upcoming("https://www.jra.go.jp/keiba/race/101/syutsuba.html", None)
    assert len(session.calls) == 1
    assert probe.records[-1]["status"] == "error"


def test_single_and_place_odds_link_is_selected_by_label(tmp_path, fixture_bytes):
    body = fixture_bytes("entries.html").replace(
        b"<table>", '<a href="/JRADB/accessO.html?CNAME=wrong">馬連</a><table>'.encode()
    )
    session = Session([Response(body), Response(fixture_bytes("odds.html"))])
    probe = Probe(Client(tmp_path, session=session, sleep=lambda _: None), progress=lambda _: None)
    probe.upcoming("https://www.jra.go.jp/keiba/race/101/syutsuba.html", None)
    assert session.calls[-1][0].endswith("CNAME=sample-odds%2Ftoken")
