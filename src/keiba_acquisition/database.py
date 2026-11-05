"""Public JRADB navigation; tokens come from published links and form choices."""

import re
from datetime import date
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from .html import ParseError, link_url, metadata, parse_result, related_links

VENUES = dict(
    zip(
        (f"{n:02}" for n in range(1, 11)),
        ("札幌", "函館", "福島", "新潟", "東京", "中山", "中京", "京都", "阪神", "小倉"),
    )
)
IDENTITY_FIELDS = ("race_date", "venue", "meeting_number", "meeting_day", "race_number")


def race_reference(body: bytes, base: str) -> dict:
    info = metadata(BeautifulSoup(body, "html.parser"))
    if any(info[field] is None for field in IDENTITY_FIELDS):
        raise ParseError("保存版から開催日・競馬場・開催回日・レース番号を特定できません。")
    return {"kind": "race_reference", **info, "issues": []}


def search_link(body: bytes, base: str) -> str:
    links = related_links(body, base, "accessS", labels=("過去のレース結果",))
    if len(links) != 1:
        raise ParseError("公式の過去結果検索へのリンクを一意に特定できません。")
    return links[0]


def month_link(body: bytes, base: str, race_date: str) -> str:
    target = date.fromisoformat(race_date)
    month = target.strftime("%Y%m")
    # Prefer an explicit published link, when this month is already in navigation.
    for url in related_links(body, base, "accessS"):
        cname = parse_qs(urlsplit(url).query)["CNAME"][0]
        if re.fullmatch(rf"pw01skl(?:00|10){month}/[0-9A-Fa-f]{{2}}", cname):
            return url
    soup = BeautifulSoup(body, "html.parser")
    years = {o.get("value") for o in soup.select("#kaisaiY_list option")}
    months = {o.get("value") for o in soup.select("#kaisaiM_list option")}
    source = "\n".join(script.get_text() for script in soup.find_all("script"))
    token = re.findall(rf'objParam\["{month[2:]}"\]\s*=\s*"([0-9A-Fa-f]{{2}})"', source)
    boundary = re.search(r'var\s+yearMonth\s*=\s*"(\d{6})"', source)
    # Recognize only the observed public form's literal template; never evaluate JS
    # or invent a checksum if the site's choices/template change.
    templates = all(
        re.search(rf"param\s*=\s*'pw01skl{prefix}'\s*\+\s*String\(arg\)\s*\+\s*'/'", source)
        for prefix in ("00", "10")
    )
    if (
        str(target.year) not in years
        or f"{target.month:02}" not in months
        or len(set(token)) != 1
        or boundary is None
        or not templates
        or "Number(arg) >= yearMonth" not in source
    ):
        raise ParseError("過去結果の公開年月選択フォームを認識できません。")
    prefix = "00" if month >= boundary[1] else "10"
    return (
        urljoin(base, "accessS.html")
        + "?"
        + urlencode({"CNAME": f"pw01skl{prefix}{month}/{token[0]}"})
    )


def result_link(body: bytes, base: str, reference: dict) -> str:
    found = []
    for url in related_links(body, base, "accessS"):
        cname = parse_qs(urlsplit(url).query)["CNAME"][0]
        match = re.fullmatch(
            r"pw01sde\d{2}(\d{2})(\d{4})(\d{2})(\d{2})(\d{2})(\d{8})/[0-9A-Fa-f]{2}", cname
        )
        if match is None:
            continue
        venue, year, meeting, day, number, ymd = match.groups()
        identity = {
            "race_date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}",
            "venue": VENUES.get(venue),
            "meeting_number": int(meeting),
            "meeting_day": int(day),
            "race_number": int(number),
        }
        if year == ymd[:4] and all(identity[f] == reference[f] for f in IDENTITY_FIELDS):
            found.append(url)
    if len(found) != 1:
        raise ParseError("保存版と同じ開催の詳細結果リンクを一意に特定できません。")
    return found[0]


def check_identity(result: dict, reference: dict) -> None:
    if any(result.get(f) is None or result[f] != reference[f] for f in IDENTITY_FIELDS):
        raise ParseError("開催日・競馬場・開催回日・レース番号が元のレースと一致しません。")


def checked_result(body: bytes, base: str, reference: dict) -> dict:
    result = parse_result(body, base)
    check_identity(result, reference)
    return result


def odds_link(body: bytes, base: str) -> str:
    soup = BeautifulSoup(body, "html.parser")
    scope = soup.select_one(".race_related_link")
    links = []
    if scope:
        for a in scope.find_all("a"):
            url = link_url(a, base)
            if url and urlsplit(url).path == "/JRADB/accessO.html":
                cname = parse_qs(urlsplit(url).query).get("CNAME", [""])[0]
                if re.match(r"pw151ou", cname):
                    links.append(url)
    if len(set(links)) != 1:
        raise ParseError("対象レースの単複最終オッズリンクを一意に特定できません。")
    return links[0]


def merge_final_odds(result: dict, odds: dict) -> None:
    check_identity(odds, result)
    if odds.get("odds_state") != "final" or odds.get("issues"):
        raise ParseError("確定した単複オッズを確認できません。")
    indexed = {value["horse_number"]: value for value in odds["odds"]}
    if set(indexed) != {entry["horse_number"] for entry in result["entries"]}:
        raise ParseError("結果と最終オッズの馬番集合が一致しません。")
    for entry in result["entries"]:
        value = indexed[entry["horse_number"]]
        if not entry.get("horse_id") or value.get("horse_id") != entry["horse_id"]:
            raise ParseError("結果と最終オッズの馬IDが一致しません。馬名では結合しません。")
        if entry["win_odds"] and entry["win_odds"] != value["win_odds"]:
            raise ParseError("結果と最終オッズの値が矛盾しています。")
    for entry in result["entries"]:
        entry["win_odds"] = indexed[entry["horse_number"]]["win_odds"]
    result["issues"] = [
        issue for issue in result["issues"] if not issue.startswith("missing_win_odds:")
    ]
    missing = [str(e["horse_number"]) for e in result["entries"] if e["win_odds"] is None]
    if missing:
        result["issues"].append("missing_win_odds:" + ",".join(missing))
    result["final_odds_source"] = odds["source"]
