"""Verify auxiliary past results and derive features using pre-race records only."""

import re
from datetime import date
from fractions import Fraction
from statistics import median
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from .database import VENUES, checked_result
from .html import ParseError, clean, text

STYLE_METHOD = "prior-three-first-corner-v1"
CLASS_METHOD = "official-condition-v1"
NON_STARTERS = {"取消", "除外", "取止"}
STARTED_WITHOUT_RANK = {"中止", "競走中止", "失格"}
CLASS_CODES = {
    "新馬": "newcomer",
    "未勝利": "maiden",
    "1勝クラス": "allowance_1",
    "500万円以下": "allowance_1",
    "2勝クラス": "allowance_2",
    "1000万円以下": "allowance_2",
    "3勝クラス": "allowance_3",
    "1600万円以下": "allowance_3",
    "オープン": "open",
}


def prior_runs(profile: dict) -> list[dict]:
    """Keep the most recent three actual starts, including DNF/DQ and unknown ranks."""
    target = date.fromisoformat(profile["target_date"])
    selected = [
        run
        for run in profile["history"]
        if date.fromisoformat(run["race_date"]) < target
        and run.get("record_type") == "race"
        and not any(value in (run.get("finish_text") or "") for value in NON_STARTERS)
    ]
    selected.sort(key=lambda run: run["race_date"], reverse=True)
    if len({run["race_date"] for run in selected}) != len(selected):
        raise ParseError("同一日の実出走が重複しており、過去走の順序を確定できません。")
    return selected[:3]


def prior_reference(run: dict) -> dict:
    """Validate a published result token, without constructing or guessing tokens."""
    parts = urlsplit(run.get("source_url") or "")
    if parts.hostname != "www.jra.go.jp" or parts.path != "/JRADB/accessS.html":
        raise ParseError("補助結果の公式詳細リンクを確認できません。")
    cname = parse_qs(parts.query).get("CNAME", [])
    match = re.fullmatch(
        r"pw01sde\d{2}(\d{2})(\d{4})(\d{2})(\d{2})(\d{2})(\d{8})/[0-9A-Fa-f]{2}",
        cname[0] if len(cname) == 1 else "",
    )
    if match is None:
        raise ParseError("補助結果の開催キーをリンクから確認できません。")
    venue, year, meeting, day, number, ymd = match.groups()
    try:
        race_date = date.fromisoformat(f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}").isoformat()
    except ValueError as error:
        raise ParseError("補助結果リンクの日付が不正です。") from error
    if year != ymd[:4] or race_date != run["race_date"] or venue not in VENUES:
        raise ParseError("補助結果リンクと走歴の日付・開催が一致しません。")
    return {
        "race_date": race_date,
        "venue": VENUES[venue],
        "meeting_number": int(meeting),
        "meeting_day": int(day),
        "race_number": int(number),
    }


def race_condition(soup: BeautifulSoup) -> dict:
    """Read explicit condition/grade nodes; never classify from a race-name substring."""
    scope = soup.select_one(".race_header")
    condition = scope.select_one(".type .class") if scope else None
    grade_node = scope.select_one(".race_name .grade_icon") if scope else None
    detail = scope.select_one(".course .detail") if scope else None
    category = scope.select_one(".type .category") if scope else None
    course = scope.select_one(".type .course") if scope else None
    raw_class = text(condition) if condition else None
    raw_grade = text(grade_node) if grade_node else None
    course_text = clean(text(detail)) if detail else ""
    surface = next((value for value in ("障害", "ダート", "芝") if value in course_text), None)
    # Jump courses may say only "芝" in the detail; the category/grade identifies them.
    if (
        "障害" in clean(text(category) if category else "")
        or "障害" in clean(text(course) if course else "")
        or re.match(r"J[・.]G", clean(raw_grade or ""))
    ):
        surface = "障害"
    # 2019/2020 pages may display both old and new official condition names.
    normalized = re.sub(r"[,，]", "", clean(raw_class or ""))
    labels = re.split(r"[()]", normalized)
    codes = {CLASS_CODES[label] for label in labels if label in CLASS_CODES}
    unknown_labels = [label for label in labels if label and label not in CLASS_CODES]
    grade = clean(raw_grade or "")
    code = next(iter(codes)) if len(codes) == 1 and not unknown_labels else None
    issues = []
    if raw_grade:
        if re.fullmatch(r"(?:G|Jpn|J[・.]G)(?:I{1,3}|[123])", grade):
            if code not in {None, "open"}:
                raise ParseError("公式格付けと競走条件の表記が矛盾しています。")
            suffix = re.search(r"(III|II|I|[123])$", grade)[0]
            code = grade[: -len(suffix)] + {"I": "1", "II": "2", "III": "3"}.get(suffix, suffix)
        elif grade == "リステッド":
            if code not in {None, "open"}:
                raise ParseError("リステッドと競走条件の表記が矛盾しています。")
            code = "listed"
        else:
            code = None
            issues.append("unrecognized_grade")
    if code is None:
        issues.append("unknown_race_class")
    return {
        "code": code,
        "raw_class": raw_class,
        "raw_grade": raw_grade,
        "surface": surface,
        "method": CLASS_METHOD,
        "issues": issues,
    }


def parse_prior_result(body: bytes, base: str, run: dict, horse: str, target: str) -> dict:
    if date.fromisoformat(run["race_date"]) >= date.fromisoformat(target):
        raise ParseError("当該レース以後の結果を過去特徴へ使用しません。")
    result = checked_result(body, base, prior_reference(run))
    entries = [entry for entry in result["entries"] if entry.get("horse_id") == horse]
    if not horse or len(entries) != 1:
        raise ParseError("走歴と補助結果の馬IDを一意に照合できません。")
    entry = entries[0]
    if clean(entry["finish_text"] or "") != clean(run.get("finish_text") or ""):
        raise ParseError("走歴と補助結果の着順が一致しません。訂正等を確認してください。")
    condition = race_condition(BeautifulSoup(body, "html.parser"))
    issues = list(condition["issues"])
    starters = 0
    for value in result["entries"]:
        finish = clean(value["finish_text"] or "")
        if finish in NON_STARTERS:
            continue
        if (value["finish"] is None or value["finish"] < 1) and finish not in STARTED_WITHOUT_RANK:
            starters = None
            issues.append("unknown_start_status")
            break
        starters += 1
    raw_positions = entry.get("passing_positions") or ""
    positions = None
    if re.fullmatch(r"\d+(?:\s+\d+){0,3}", raw_positions.strip()):
        values = [int(value) for value in raw_positions.split()]
        if starters and all(1 <= value <= starters for value in values):
            positions = values
    if positions is None:
        issues.append("unknown_passing_positions")
    return {
        "kind": "prior_result",
        "horse_id": horse,
        "target_date": target,
        "run_url": run["source_url"],
        **{field: result[field] for field in prior_reference(run)},
        "horse_number": entry["horse_number"],
        "finish_text": entry["finish_text"],
        "condition": condition,
        "starters": starters,
        "passing_positions": positions,
        "issues": issues,
    }


def derive_features(profile: dict, samples: list[dict]) -> dict:
    selected = prior_runs(profile)
    indexed = {}
    for sample in samples:
        if (sample["horse_id"], sample["target_date"]) != (
            profile["horse_id"],
            profile["target_date"],
        ):
            raise ParseError("異なる馬または対象日の補助結果を結合しません。")
        key = (sample["race_date"], sample["run_url"])
        if key in indexed:
            raise ParseError("同じ過去走の補助結果が重複しています。")
        indexed[key] = sample
    if set(indexed) - {(run["race_date"], run.get("source_url")) for run in selected}:
        raise ParseError("直近3実出走以外の補助結果を脚質へ混ぜません。")
    previous = (
        indexed.get((selected[0]["race_date"], selected[0].get("source_url"))) if selected else None
    )
    previous_class = previous["condition"] if previous else None
    evidence, positions = [], []
    issues = []
    for run in selected:
        sample = indexed.get((run["race_date"], run.get("source_url")))
        evidence.append({"run": run, "result": sample})
        if (
            sample
            and sample["condition"]["surface"] in {"芝", "ダート"}
            and sample["starters"] is not None
            and sample["starters"] >= 2
            and sample["passing_positions"]
        ):
            first = sample["passing_positions"][0]
            positions.append((first, Fraction(first - 1, sample["starters"] - 1)))
    style = None
    if len(positions) >= 2:
        if sum(first == 1 for first, _ in positions) * 2 > len(positions):
            style = "逃げ"
        else:
            middle = median(value for _, value in positions)
            style = (
                "先行"
                if middle <= Fraction(1, 3)
                else ("差し" if middle <= Fraction(2, 3) else "追込")
            )
    else:
        issues.append("insufficient_running_style_evidence")
    if previous_class is None or previous_class["code"] is None:
        issues.append("unknown_previous_class")
    return {
        "kind": "historical_features",
        "horse_id": profile["horse_id"],
        "target_date": profile["target_date"],
        "previous_class": previous_class,
        "running_style": {
            "value": style,
            "method": STYLE_METHOD,
            "candidate_runs": len(selected),
            "usable_runs": len(positions),
            "minimum_runs": 2,
        },
        "prior_runs": evidence,
        "profile_source": profile.get("source"),
        "issues": issues,
    }
