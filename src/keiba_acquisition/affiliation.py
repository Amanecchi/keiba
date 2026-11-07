"""Historical training centre from the result's trainer ID and published history."""

import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from .html import ParseError, clean, link_url, text, trainer_id


def parse_trainer_history(body: bytes, base: str) -> dict:
    expected = trainer_id(base)
    soup = BeautifulSoup(body, "html.parser")
    identities = set()
    for anchor in soup.find_all("a"):
        url = link_url(anchor, base)
        if not url or urlsplit(url).path != "/JRADB/accessC.html":
            continue
        cname = parse_qs(urlsplit(url).query).get("CNAME", [""])[0]
        match = re.fullmatch(r"pw05c(?:is|js|cl)(\d{6})(?:\d{2})?/[0-9A-Fa-f]{2}", cname)
        if match:
            identities.add(match[1])
    if not expected or identities != {expected}:
        raise ParseError("調教師の所属歴ページのIDが結果の調教師IDと一致しません。")
    heading = next((h for h in soup.find_all(["h2", "h3"]) if clean(text(h)) == "所属歴"), None)
    if heading is None:
        raise ParseError("年付きの所属歴を確認できません。現在の所属だけでは補完しません。")
    block = heading.find_parent(class_="block_header")
    listing = block.find_next_sibling("ul") if block else None
    if listing is None:
        raise ParseError("所属歴の一覧形式を認識できません。")
    periods = []
    for item in listing.find_all("li", recursive=False):
        time = item.select_one(".time")
        event = item.select_one(".event")
        years = re.findall(r"(\d{4})年", clean(text(time))) if time else []
        facility = re.fullmatch(r"(美浦|栗東)T[・.]?C", clean(text(event))) if event else None
        if not facility or not 1 <= len(years) <= 2:
            raise ParseError("所属歴の年・所属先を認識できません。")
        start, end = int(years[0]), int(years[1]) if len(years) == 2 else None
        if end is not None and start > end:
            raise ParseError("所属歴の期間が逆転しています。")
        periods.append({"start_year": start, "end_year": end, "affiliation": facility[1]})
    if not periods:
        raise ParseError("所属歴が空欄です。")
    periods.sort(key=lambda p: p["start_year"])
    for previous, following in zip(periods, periods[1:]):
        if previous["end_year"] is None:
            previous["end_year"] = following["start_year"]
    license_year = None
    for label in soup.find_all("dt"):
        if clean(text(label)) == "免許取得年":
            value = label.find_next_sibling("dd")
            match = re.fullmatch(r"(\d{4})年", clean(text(value))) if value else None
            license_year = int(match[1]) if match else None
    return {
        "kind": "trainer_history",
        "trainer_id": expected,
        "license_year": license_year,
        "periods": periods,
        "issues": [],
    }


def affiliation_at(history: dict, target_date: str) -> str | None:
    year = date.fromisoformat(target_date).year
    periods = history["periods"]
    # Year-only history cannot locate a transition within its boundary year.
    first_year = min(p["start_year"] for p in periods)
    if year < first_year or (year == first_year and history.get("license_year") != first_year):
        return None
    # In the first license year, the historical result itself establishes that
    # this trainer was already licensed. There is no previous licensed period.
    eligible = [
        p
        for p in periods
        if p["start_year"] <= year and (p["end_year"] is None or year <= p["end_year"])
    ]
    if not eligible:
        return None
    # A closed last period cannot establish the remainder of its ending year.
    if all(p["end_year"] == year for p in eligible):
        return None
    facilities = {p["affiliation"] for p in eligible}
    return next(iter(facilities)) if len(facilities) == 1 else None


def history_covers_date(history: dict, target_date: str) -> bool:
    try:
        acquired = datetime.fromisoformat(history["source"]["fetched_at"])
        if acquired.tzinfo is None:
            return False
        observed_day = acquired.astimezone(timezone(timedelta(hours=9))).date()
        return date.fromisoformat(target_date) <= observed_day
    except (KeyError, TypeError, ValueError):
        return False


def merge_affiliations(result: dict, histories: dict[str, dict]) -> None:
    for entry in result["entries"]:
        if entry["affiliation"]:
            continue
        history = histories.get(entry.get("trainer_id"))
        if (
            not history
            or history["trainer_id"] != entry["trainer_id"]
            or not history_covers_date(history, result["race_date"])
        ):
            continue
        value = affiliation_at(history, result["race_date"])
        if value:
            entry["affiliation"] = value
            entry["affiliation_evidence"] = {
                "trainer_id": history["trainer_id"],
                "license_year": history.get("license_year"),
                "target_date": result["race_date"],
                "periods": history["periods"],
                "source": history["source"],
                "rule": "result_trainer_id_and_dated_history; ambiguous_years_excluded",
            }
    result["issues"] = [
        issue for issue in result["issues"] if not issue.startswith("missing_affiliation:")
    ]
    missing = [str(e["horse_number"]) for e in result["entries"] if not e["affiliation"]]
    if missing:
        result["issues"].append("missing_affiliation:" + ",".join(missing))
