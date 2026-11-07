"""Dated, exact series mappings and an auditable annual race inventory."""

import json
import re
from copy import deepcopy
from datetime import date
from importlib.resources import files
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .database import VENUES
from .html import ParseError, clean, discover_results, link_url, tables_with_headers, text


def load_registry() -> dict:
    return json.loads(files("keiba_acquisition").joinpath("data/series_registry.json").read_text())


def resolve_series(name: str, year: int, registry: dict | None = None) -> dict:
    registry = registry or load_registry()
    found = [
        item
        for item in registry["series"]
        if any(
            clean(alias["name"]) == clean(name) and alias["from_year"] <= year <= alias["to_year"]
            for alias in item["aliases"]
        )
    ]
    if len(found) != 1:
        raise ParseError(f"{year}年のレース系列を一意に確認できません: {name}")
    return found[0]


def parse_catalog(
    body: bytes, base: str, registry: dict | None = None, *, as_of: date | None = None
) -> dict:
    soup = BeautifulSoup(body, "html.parser")
    match = re.fullmatch(r"/datafile/seiseki/replay/(\d{4})/jyusyo.html", urlsplit(base).path)
    if match:
        year = int(match[1])
    elif urlsplit(base).path == "/datafile/seiseki/replay/jyusyo.html":
        years = [
            m[1]
            for h in soup.find_all(["h1", "h2"])
            if (m := re.fullmatch(r"重賞レース一覧(\d{4})年", clean(text(h))))
        ]
        if len(years) != 1:
            raise ParseError("現行の重賞一覧の年を確認できません。")
        year = int(years[0])
    else:
        raise ParseError("年度別重賞一覧のURLではありません。")
    discovered = {item["url"] for item in discover_results(body, base, flat_only=True)}
    registry = registry or load_registry()
    races, pending, seen_urls, seen_series = [], [], set(), set()
    for rows, mapping in tables_with_headers(soup, {"race"}):
        for row in rows:
            cell = row[mapping["race"]]
            grade_node = cell.select_one(".grade_icon")
            if grade_node is None:
                raise ParseError("年度一覧の格付けを確認できません。")
            grade = clean(text(grade_node))
            if re.fullmatch(r"J[・.]G(?:I{1,3}|[123])", grade):
                continue
            if not re.fullmatch(r"(?:G|Jpn)(?:I{1,3}|[123])|(?:新設)?重賞", grade):
                raise ParseError("年度一覧の未知の格付けを保留します。")
            copied = deepcopy(cell)
            copied.select_one(".grade_icon").decompose()
            name = text(copied, images=False).strip()
            if len(row) < 5:
                raise ParseError("年度一覧の列構成を確認できません。")
            month_day = re.match(r"(\d{1,2})月(\d{1,2})日", clean(text(row[0])))
            venue = clean(text(row[2]))
            course = re.fullmatch(r"(芝|ダート|ダ)([\d,]+)メートル", clean(text(row[4])))
            if month_day is None or venue not in VENUES.values() or course is None:
                raise ParseError("年度一覧の日付・競馬場・コースを確認できません。")
            try:
                race_date = date(year, int(month_day[1]), int(month_day[2])).isoformat()
            except ValueError as error:
                raise ParseError("年度一覧の日付が不正です。") from error
            if as_of is not None and date.fromisoformat(race_date) > as_of:
                continue
            series = resolve_series(name, year, registry)
            if series["id"] in seen_series:
                raise ParseError("同じ年度のレース系列が重複しています。")
            candidates = {
                url
                for node in {id(cell): cell for cell in row}.values()
                for a in node.find_all("a")
                if (url := link_url(a, base)) in discovered
            }
            if not candidates and as_of is not None and race_date == as_of.isoformat():
                pending.append({"race_date": race_date, "name_at_time": name})
                continue
            if len(candidates) != 1:
                raise ParseError("年度一覧の結果リンクを一意に確認できません。")
            archive_url = candidates.pop()
            if archive_url in seen_urls:
                raise ParseError("異なる開催が同じ結果リンクを参照しています。")
            seen_urls.add(archive_url)
            seen_series.add(series["id"])
            races.append(
                {
                    "series_id": series["id"],
                    "series_name": series["display_name"],
                    "year": year,
                    "race_date": race_date,
                    "name_at_time": name,
                    "grade_at_time": grade,
                    "venue": venue,
                    "surface": "ダート" if course[1] == "ダ" else course[1],
                    "distance_m": int(course[2].replace(",", "")),
                    "archive_url": archive_url,
                    "catalog_url": base,
                    "registry_version": registry["version"],
                }
            )
    if (not races and as_of is None) or (as_of is None and seen_urls != discovered):
        raise ParseError("年度一覧と平地重賞の対象件数が一致しません。")
    return {"kind": "race_catalog", "year": year, "races": races, "pending": pending, "issues": []}
