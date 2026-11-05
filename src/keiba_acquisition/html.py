"""Conservative semantic HTML extraction; unrecognized fields remain missing."""

import re
import unicodedata
from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from .transport import AcquisitionError, official_url


class ParseError(Exception):
    pass


def clean(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))


def text(cell: Tag, *, images: bool = True) -> str:
    value = cell.get_text(" ", strip=True)
    if images:
        value += " " + " ".join(image.get("alt", "") for image in cell.find_all("img"))
    return value.strip()


def integer(value: str) -> int | None:
    normalized = clean(value)
    return int(normalized) if re.fullmatch(r"\d+", normalized) else None


def dates(value: str) -> list[date]:
    found = []
    for year, month, day in re.findall(r"(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})日?", value):
        try:
            found.append(date(int(year), int(month), int(day)))
        except ValueError:
            continue
    return found


def link_url(node: Tag, base: str) -> str | None:
    """Read ordinary links and doAction arguments without executing JavaScript."""
    for value in (node.get("href", ""), node.get("onclick", "")):
        action = re.search(r"doAction\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]", value)
        if action:
            candidate = urljoin(base, action[1]) + "?" + urlencode({"CNAME": action[2]})
        elif value and not value.startswith(("javascript:", "#")):
            candidate = urljoin(base, value)
        else:
            continue
        try:
            return official_url(candidate)
        except AcquisitionError:
            continue
    return None


def grid(table: Tag) -> list[list[Tag]]:
    """Expand rowspan/colspan so multi-line headers and payout rows stay aligned."""
    rows, pending = [], {}
    for row in table.find_all("tr"):
        if row.find_parent("table") is not table:
            continue
        occupied = {col: cell for col, (cell, remaining) in pending.items()}
        next_pending = {
            col: (cell, remaining - 1)
            for col, (cell, remaining) in pending.items()
            if remaining > 1
        }
        col = 0
        for cell in row.find_all(["td", "th"], recursive=False):
            while col in occupied:
                col += 1
            try:
                width = min(max(int(cell.get("colspan", 1)), 1), 100)
                height = min(max(int(cell.get("rowspan", 1)), 1), 100)
            except ValueError as error:
                raise ParseError("表の結合セル属性を認識できません。") from error
            for offset in range(width):
                occupied[col + offset] = cell
                if height > 1:
                    next_pending[col + offset] = (cell, height - 1)
            col += width
        if occupied:
            # Empty positions indicate an unknown layout; never shift cells silently.
            if set(occupied) != set(range(max(occupied) + 1)):
                raise ParseError("表の列位置を確定できません。")
            rows.append([occupied[n] for n in range(max(occupied) + 1)])
        pending = next_pending
    return rows


ALIASES = {
    "number": {"馬番"},
    "horse": {"馬名"},
    "finish": {"着順"},
    "frame": {"枠", "枠番"},
    "sex_age": {"性齢"},
    "jockey": {"騎手", "騎手名"},
    "popularity": {"人気", "単勝人気"},
    "win_odds": {"単勝", "単勝オッズ"},
    "place_odds": {"複勝", "複勝オッズ"},
    "corners": {"通過順位", "コーナー通過順位"},
    "trainer": {"調教師", "調教師名"},
    "affiliation": {"所属"},
    "race": {"レース名", "競走名"},
    "date": {"年月日", "開催日", "日付"},
    "distance": {"距離", "コース"},
}


def tables_with_headers(soup: BeautifulSoup, required: set[str]):
    for table in soup.find_all("table"):
        rows = grid(table)
        for index, row in enumerate(rows):
            mapping = {}
            for col, cell in enumerate(row):
                value = clean(text(cell))
                for key, aliases in ALIASES.items():
                    if value in aliases:
                        mapping[key] = col
            if required <= mapping.keys():
                yield rows[index + 1 :], mapping
                break


def cell_value(row: list[Tag], mapping: dict, key: str, *, images: bool = True) -> str | None:
    index = mapping.get(key)
    return text(row[index], images=images) if index is not None and index < len(row) else None


def discover_results(body: bytes, base: str, race_name: str = "") -> list[dict]:
    soup = BeautifulSoup(body, "html.parser")
    found = {}
    for rows, mapping in tables_with_headers(soup, {"race"}):
        for row in rows:
            name = cell_value(row, mapping, "race") or ""
            if race_name and clean(race_name) not in clean(name):
                continue
            for node in {id(cell): cell for cell in row}.values():
                for anchor in node.find_all("a"):
                    url = link_url(anchor, base)
                    if url and (
                        "/JRADB/accessS.html" in url or re.search(r"/replay/\d{4}/\d+\.html", url)
                    ):
                        found.setdefault(url, {"url": url, "race_name": name})
    if not found:
        raise ParseError(
            "年度一覧から対象のHTML結果リンクを取得できません。未開催・形式差を確認してください。"
        )
    return list(found.values())


def metadata(soup: BeautifulSoup) -> dict:
    page_text = text(soup)
    race_dates = dates(page_text)
    title = next(
        (text(h) for h in soup.find_all(["h2", "h3"]) if re.search(r"第\d+回", clean(text(h)))),
        None,
    )
    course = re.search(r"コース[：:]\s*([\d,]+)\s*メートル", page_text)
    return {
        "race_date": race_dates[0].isoformat() if race_dates else None,
        "title": title,
        "distance_m": int(course[1].replace(",", "")) if course else None,
    }


def participants(soup: BeautifulSoup, base: str, *, result: bool) -> list[dict]:
    required = {"number", "horse"} | ({"finish"} if result else set())
    for rows, mapping in tables_with_headers(soup, required):
        entries = []
        for row in rows:
            number = integer(cell_value(row, mapping, "number") or "")
            name = cell_value(row, mapping, "horse", images=False)
            if number is None:
                if name and clean(name) != "馬名":
                    raise ParseError("馬名のある行の馬番を認識できません。")
                continue
            if not name:
                raise ParseError("馬番がある行の馬名を取得できません。")
            horse_cell = row[mapping["horse"]]
            horse_url = next(
                (
                    url
                    for a in horse_cell.find_all("a")
                    if (url := link_url(a, base)) and "/accessU.html" in url
                ),
                None,
            )
            cname = (
                parse_qs(urlsplit(horse_url).query).get("CNAME", [None])[0] if horse_url else None
            )
            finish = cell_value(row, mapping, "finish")
            frame = cell_value(row, mapping, "frame") or ""
            frame_match = re.search(r"枠([1-8])", clean(frame))
            odds_text = cell_value(row, mapping, "win_odds")
            odds_value = clean(odds_text or "")
            entries.append(
                {
                    "horse_number": number,
                    "horse_name": name,
                    "horse_url": horse_url,
                    "horse_source_key": cname,
                    "frame": int(frame_match[1]) if frame_match else integer(frame),
                    "finish": integer(finish or ""),
                    "finish_text": finish,
                    "sex_age": cell_value(row, mapping, "sex_age"),
                    "jockey": cell_value(row, mapping, "jockey"),
                    "trainer": cell_value(row, mapping, "trainer"),
                    "affiliation": cell_value(row, mapping, "affiliation"),
                    "popularity": integer(cell_value(row, mapping, "popularity") or ""),
                    "win_odds": odds_value if re.fullmatch(r"\d+(?:\.\d+)?", odds_value) else None,
                    "win_odds_text": odds_text,
                    "passing_positions": cell_value(row, mapping, "corners"),
                }
            )
        if entries:
            if len({e["horse_number"] for e in entries}) != len(entries):
                raise ParseError("馬番が重複しています。結合セル等の形式を確認してください。")
            return entries
    raise ParseError("出走馬の表を認識できません。")


def payouts(soup: BeautifulSoup) -> list[dict]:
    found = {}

    def add(kind: str, numbers: list, amounts: list):
        if len(numbers) != len(amounts):
            raise ParseError("払戻の馬番と金額の件数が一致しません。")
        for number, amount in zip(numbers, amounts):
            key = (kind, int(number))
            value = {
                "bet_type": kind,
                "horse_number": int(number),
                "yen_per_100": int(amount.replace(",", "")),
            }
            if key in found and found[key] != value:
                raise ParseError("同じ馬の払戻金が矛盾しています。")
            found[key] = value

    for table in soup.find_all("table"):
        for row in grid(table):
            values = [text(cell) for cell in row]
            if not values or clean(values[0]) not in {"単勝", "複勝"}:
                continue
            numbers, amounts = [], []
            for value in values[1:]:
                if "円" in value:
                    amounts += re.findall(r"([\d,]+)\s*円", value)
                elif "人気" not in value:
                    numbers += re.findall(r"\b\d{1,2}\b", value)
            add(clean(values[0]), numbers, amounts)
    # Modern payout blocks can be definition lists rather than tables.
    for label in soup.find_all("dt"):
        kind = clean(text(label))
        if kind not in {"単勝", "複勝"}:
            continue
        definition = label.find_next_sibling("dd")
        if not definition:
            continue
        for number, amount in re.findall(r"(?<![\d,])(\d{1,2})\s+([\d,]+)\s*円", text(definition)):
            add(kind, [number], [amount])
    return list(found.values())


def parse_result(body: bytes, base: str) -> dict:
    soup = BeautifulSoup(body, "html.parser")
    entries = participants(soup, base, result=True)
    payments = payouts(soup)
    info = metadata(soup)
    issues = []
    for field in ("race_date", "title", "distance_m"):
        if info[field] is None:
            issues.append(f"missing_{field}")
    for kind in ("単勝", "複勝"):
        if not any(p["bet_type"] == kind for p in payments):
            issues.append(f"missing_payout_{kind}")
    numbers = {entry["horse_number"] for entry in entries}
    if any(payment["horse_number"] not in numbers for payment in payments):
        raise ParseError("払戻対象の馬番が出走表にありません。")
    winners = {entry["horse_number"] for entry in entries if entry["finish"] == 1}
    paid_winners = {p["horse_number"] for p in payments if p["bet_type"] == "単勝"}
    if winners != paid_winners:
        issues.append("win_payout_finish_mismatch")
    for key in (
        "horse_url",
        "sex_age",
        "jockey",
        "frame",
        "win_odds",
        "popularity",
        "passing_positions",
        "affiliation",
    ):
        missing = [
            entry["horse_number"] for entry in entries if entry[key] is None or entry[key] == ""
        ]
        if missing:
            issues.append(f"missing_{key}:{','.join(map(str, missing))}")
    return {"kind": "result", **info, "entries": entries, "payouts": payments, "issues": issues}


def parse_profile(body: bytes, base: str, target_date: date) -> dict:
    soup = BeautifulSoup(body, "html.parser")
    sire = None
    for label in soup.find_all(["dt", "th", "span"]):
        if clean(text(label)) != "父":
            continue
        value = label.find_next_sibling(["dd", "td", "span"])
        if value:
            sire = text(value, images=False)
            break
    runs = []
    for rows, mapping in tables_with_headers(soup, {"date", "race", "finish"}):
        for row in rows:
            run_dates = dates(cell_value(row, mapping, "date") or "")
            if not run_dates:
                continue
            finish_text = cell_value(row, mapping, "finish") or ""
            race_cell = row[mapping["race"]]
            run_url = next(
                (url for a in race_cell.find_all("a") if (url := link_url(a, base))), None
            )
            runs.append(
                {
                    "race_date": run_dates[0].isoformat(),
                    "race_name": text(race_cell),
                    "distance": cell_value(row, mapping, "distance"),
                    "finish_text": finish_text,
                    "finish": integer(finish_text),
                    "source_url": run_url,
                }
            )
    prior = [
        run
        for run in runs
        if date.fromisoformat(run["race_date"]) < target_date
        and not any(s in run["finish_text"] for s in ("取消", "除外", "取止"))
    ]
    prior.sort(key=lambda run: run["race_date"], reverse=True)
    previous = prior[0] if prior else None
    issues = ([] if sire else ["missing_sire"]) + ([] if previous else ["missing_previous_run"])
    return {
        "kind": "profile",
        "sire": sire,
        "target_date": target_date.isoformat(),
        "previous_run": previous,
        "history": runs,
        "issues": issues,
    }


def publication_state(soup: BeautifulSoup) -> str | None:
    value = clean(text(soup))
    if any(marker in value for marker in ("出馬表は", "出馬表の公開")) and any(
        marker in value for marker in ("公開予定", "掲載予定", "公開いたします", "公開します")
    ):
        return "not_yet_published"
    if any(marker in value for marker in ("発売前", "発売開始前", "発売しておりません")):
        return "not_on_sale"
    return None


def parse_entries(body: bytes, base: str) -> dict:
    soup = BeautifulSoup(body, "html.parser")
    try:
        entries = participants(soup, base, result=False)
    except ParseError:
        if state := publication_state(soup):
            return {"kind": "entries", "status": state, "entries": [], "issues": [state]}
        raise
    return {
        "kind": "entries",
        "status": "parsed",
        **metadata(soup),
        "entries": entries,
        "issues": [],
    }


def parse_odds(body: bytes, base: str) -> dict:
    soup = BeautifulSoup(body, "html.parser")
    for rows, mapping in tables_with_headers(soup, {"number", "win_odds", "place_odds"}):
        values = []
        for row in rows:
            number = integer(cell_value(row, mapping, "number") or "")
            if number is None:
                continue
            win = clean(cell_value(row, mapping, "win_odds") or "")
            place = clean(cell_value(row, mapping, "place_odds") or "")
            win_match = re.fullmatch(r"\d+(?:\.\d+)?", win)
            place_match = re.fullmatch(r"(\d+(?:\.\d+)?)[-~〜](\d+(?:\.\d+)?)", place)
            if win_match and Decimal(win) <= 0:
                win_match = None
            if place_match and not (0 < Decimal(place_match[1]) <= Decimal(place_match[2])):
                place_match = None
            availability = "cancelled" if "取消" in win or "除外" in win else "quoted"
            values.append(
                {
                    "horse_number": number,
                    "win_odds": win if win_match else None,
                    "place_lower": place_match[1] if place_match else None,
                    "place_upper": place_match[2] if place_match else None,
                    "raw_win": win,
                    "raw_place": place,
                    "availability": availability,
                }
            )
        if values:
            if len({value["horse_number"] for value in values}) != len(values):
                raise ParseError("オッズ表の馬番が重複しています。")
            timestamp = re.search(
                r"(?:\d{1,2}月\d{1,2}日\s*)?\d{1,2}(?::|時)\d{2}分?\s*現在", text(soup)
            )
            issues = [] if timestamp else ["missing_provider_timestamp"]
            if any(
                value["availability"] == "quoted"
                and (value["win_odds"] is None or value["place_lower"] is None)
                for value in values
            ):
                issues.append("missing_odds_values")
            return {
                "kind": "odds",
                "status": "parsed",
                "provider_timestamp_text": timestamp[0] if timestamp else None,
                "odds": values,
                "issues": issues,
            }
    if state := publication_state(soup):
        return {"kind": "odds", "status": state, "odds": [], "issues": [state]}
    raise ParseError("単複オッズの表を認識できません。別画面・動的取得等の検証が必要です。")


def related_links(
    body: bytes, base: str, endpoint: str, *, labels: tuple[str, ...] = ()
) -> list[str]:
    soup = BeautifulSoup(body, "html.parser")
    return list(
        dict.fromkeys(
            url
            for anchor in soup.find_all("a")
            if (url := link_url(anchor, base))
            and urlsplit(url).path == f"/JRADB/{endpoint}.html"
            and parse_qs(urlsplit(url).query).get("CNAME")
            and all(label in clean(text(anchor)) for label in labels)
        )
    )
