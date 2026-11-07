"""Conservative per-bet settlement evidence; missing payouts never become losses."""

import re

from bs4 import BeautifulSoup

from .html import clean, integer, text

SCHEMA = "jra-modern-refund-v1"
NONSTARTERS = {"取消", "出走取消", "除外", "競走除外", "取止", "競走取止"}


def outcome(entry: dict) -> str:
    status = clean(entry.get("finish_text") or "")
    if status in NONSTARTERS:
        return "nonstarter"
    if status in {"中止", "競走中止"}:
        return "dnf"
    if status == "失格":
        return "disqualified"
    if isinstance(entry.get("finish"), int) and entry["finish"] > 0:
        return "finished"
    return "unknown"


def audit_settlement(body: bytes, result: dict, *, place_slots: int | None = None) -> dict:
    soup = BeautifulSoup(body, "html.parser")
    regions = soup.select(".refund_area")
    entries = {entry["horse_number"]: entry for entry in result["entries"]}
    global_issues, refunds = [], set()
    region = regions[0] if len(regions) == 1 else None
    if region is None or len(region.select(".refund_unit:not(.restoration)")) != 1:
        global_issues.append("unsupported_payout_region")
    if region is not None:
        refund_sections = [
            dl
            for dl in region.find_all("dl")
            if dl.find("dt") and clean(text(dl.find("dt"))) == "返還"
        ]
        if len(refund_sections) > 1:
            global_issues.append("ambiguous_refund_sections")
        for section in refund_sections:
            content = clean(text(section.find("dd"))) if section.find("dd") else ""
            match = re.fullmatch(
                r"返還馬番((?:\d{1,2}番[、,・]?)+)(?:返還(?:同枠|枠番)(?:\d枠[、,・]?)+)?",
                content,
            )
            if not match:
                global_issues.append("unrecognized_refund_numbers")
                continue
            values = [int(number) for number in re.findall(r"(\d{1,2})番", match[1])]
            if len(values) != len(set(values)) or not set(values) <= entries.keys():
                global_issues.append("invalid_refund_numbers")
            refunds.update(values)
    bets = {}
    for name, css in [("単勝", "win"), ("複勝", "place")]:
        issues = list(global_issues)
        sections = region.select(f".refund_unit:not(.restoration) li.{css}") if region else []
        paid = {}
        status = "unknown"
        if len(sections) != 1:
            issues.append("missing_or_ambiguous_bet_section")
        else:
            section = sections[0]
            label = section.find("dt")
            if not label or clean(text(label)) != name:
                issues.append("unrecognized_bet_label")
            elif "特払" in text(section):
                issues.append("special_payout_requires_review")
            elif section.find("dd") and clean(text(section.find("dd"))) in {
                "発売なし",
                "発売無し",
                "発売されません",
            }:
                status = "not_sold"
            else:
                lines = section.select("dd .line")
                if not lines:
                    issues.append("missing_payout_lines")
                for line in lines:
                    numbers, amounts = line.select(".num"), line.select(".yen")
                    number = integer(text(numbers[0])) if len(numbers) == 1 else None
                    raw_amount = clean(text(amounts[0])) if len(amounts) == 1 else ""
                    amount = re.fullmatch(r"([\d,]+)円", raw_amount)
                    if number not in entries or amount is None or number in paid:
                        issues.append("invalid_payout_line")
                        continue
                    yen = int(amount[1].replace(",", ""))
                    if yen < 100 or outcome(entries[number]) != "finished" or number in refunds:
                        issues.append("inconsistent_payout_entry")
                        continue
                    paid[number] = yen
                if name == "単勝":
                    winners = {n for n, e in entries.items() if e.get("finish") == 1}
                    if not winners or set(paid) != winners:
                        issues.append("winning_rank_mismatch")
                else:
                    starters = sum(
                        outcome(e) in {"finished", "dnf", "disqualified"} for e in entries.values()
                    )
                    slots = place_slots
                    if slots is None:
                        if starters >= 8:
                            slots = 3
                        elif 5 <= len(entries) <= 7:
                            slots = 2
                    if slots not in {2, 3}:
                        issues.append("unknown_place_terms")
                    else:
                        expected_place = {
                            n
                            for n, e in entries.items()
                            if outcome(e) == "finished" and 1 <= e["finish"] <= slots
                        }
                        if not expected_place or set(paid) != expected_place:
                            issues.append("place_rank_mismatch")
                expected = {
                    p["horse_number"]: p["yen_per_100"]
                    for p in result["payouts"]
                    if p["bet_type"] == name
                }
                if paid != expected:
                    issues.append("parsed_payout_mismatch")
                if not issues:
                    status = "verified"
        bets[name] = {
            "status": status if not issues else "unknown",
            "payouts": [
                {"horse_number": number, "yen_per_100": yen} for number, yen in paid.items()
            ],
            "refunded_numbers": sorted(refunds),
            "issues": sorted(set(issues)),
        }
    values = []
    for number, entry in entries.items():
        state = outcome(entry)
        item = {"horse_number": number, "outcome": state, "bets": {}}
        for name, bet in bets.items():
            paid = {p["horse_number"]: p["yen_per_100"] for p in bet["payouts"]}
            known = bet["status"] == "verified"
            if number in refunds and not global_issues:
                eligible, reason = False, "refunded"
            elif state == "nonstarter":
                eligible, reason = False, "nonstarter"
            elif bet["status"] == "not_sold":
                eligible, reason = False, "not_sold"
            elif state == "unknown" or not known:
                eligible, reason = None, "unverified"
            else:
                eligible, reason = True, "verified"
            item["bets"][name] = {
                "eligible": eligible,
                "reason": reason,
                "hit": number in paid if eligible is True else None,
                "payout_yen": paid.get(number, 0) if eligible is True else None,
            }
        values.append(item)
    notes = []
    for node in soup.select("td.margin, .race_result .caution li, .result_area .caution li"):
        value = text(node)
        if any(word in value for word in ("降着", "訂正", "失格")):
            notes.append(value[:1000])
    return {
        "schema": SCHEMA,
        "bets": bets,
        "entries": values,
        "official_notes": notes,
        "issues": sorted(
            {f"{name}:{issue}" for name, bet in bets.items() for issue in bet["issues"]}
        ),
    }
