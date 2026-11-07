"""Historical filters and realized statistics, independent of HTTP and acquisition."""

from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

FIELDS = {
    "frame": ("枠順", "number"),
    "running_style": ("脚質", "text"),
    "jockey": ("騎手", "text"),
    "sire": ("種牡馬", "text"),
    "sex": ("性別", "text"),
    "age": ("年齢", "number"),
    "affiliation": ("所属", "text"),
    "previous_class": ("前走クラス", "text"),
    "previous_distance": ("前走距離", "number"),
    "interval_days": ("レース間隔（日）", "number"),
    "popularity": ("単勝人気", "number"),
    "previous_race": ("前走レース名", "text"),
    "previous_finish": ("前走着順", "number"),
    "win_odds": ("単勝確定オッズ", "number"),
}


class InvalidQuery(ValueError):
    pass


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise InvalidQuery("数値の形式が不正です。")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise InvalidQuery("数値の形式が不正です。") from error
    if not result.is_finite():
        raise InvalidQuery("有限の数値を指定してください。")
    return result


def validate_filters(filters):
    if not isinstance(filters, list) or len(filters) > 20:
        raise InvalidQuery("条件は20件以内で指定してください。")
    seen = set()
    for condition in filters:
        if not isinstance(condition, dict) or condition.get("field") not in FIELDS:
            raise InvalidQuery("未対応の分析条件です。")
        if condition["field"] in seen:
            raise InvalidQuery("同じ項目の条件は、1行にまとめて複数選択してください。")
        seen.add(condition["field"])
        kind = FIELDS[condition["field"]][1]
        op = condition.get("op")
        if op == "in":
            values = condition.get("values")
            if not isinstance(values, list) or not 1 <= len(values) <= 200:
                raise InvalidQuery("選択値を1〜200件指定してください。")
            for value in values:
                if kind == "number":
                    number(value)
                elif not isinstance(value, str) or not value or len(value) > 200:
                    raise InvalidQuery("条件の文字列が不正です。")
        elif op == "range" and kind == "number":
            if condition.get("min") is None and condition.get("max") is None:
                raise InvalidQuery("範囲の下限または上限を指定してください。")
            low = number(condition["min"]) if condition.get("min") is not None else None
            high = number(condition["max"]) if condition.get("max") is not None else None
            if low is not None and high is not None and low > high:
                raise InvalidQuery("範囲の下限が上限を超えています。")
        else:
            raise InvalidQuery("未対応の条件指定です。")
    return filters


def match_entry(entry, filters):
    unknown = False
    for condition in filters:
        field = condition["field"]
        value = entry["features"].get(field)
        if field == "previous_finish" and entry["features"].get("previous_finish_status") in {
            "dnf",
            "disqualified",
        }:
            return "nonmatch"
        if value is None:
            unknown = True
            continue
        if FIELDS[field][1] == "number":
            value = number(value)
        if condition["op"] == "in":
            choices = condition["values"]
            if FIELDS[field][1] == "number":
                choices = [number(item) for item in choices]
            if value not in choices:
                return "nonmatch"
        else:
            if condition.get("min") is not None and value < number(condition["min"]):
                return "nonmatch"
            if condition.get("max") is not None and value > number(condition["max"]):
                return "nonmatch"
    return "unknown" if unknown else "match"


def percent(numerator, denominator):
    if not denominator:
        return None
    return str(
        (Decimal(numerator) / denominator * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    )


def summarize(entries):
    starts = [e for e in entries if e["outcome"] in {"finished", "dnf", "disqualified"}]
    ranked = [e for e in starts if e["outcome"] == "finished" and e.get("finish")]
    wins = sum(e["finish"] == 1 for e in ranked)
    seconds = sum(e["finish"] <= 2 for e in ranked)
    thirds = sum(e["finish"] <= 3 for e in ranked)
    bets = {}
    for name in ("単勝", "複勝"):
        confirmed, payout, hits, refunded, unknown = 0, 0, 0, 0, 0
        for entry in entries:
            value = entry.get("bets", {}).get(name, {})
            if value.get("reason") == "refunded":
                refunded += 1
            if value.get("eligible") is False:
                continue
            amount = value.get("payout_yen")
            if (
                entry["outcome"] not in {"finished", "dnf", "disqualified"}
                or value.get("eligible") is not True
                or not isinstance(amount, int)
                or isinstance(amount, bool)
                or amount < 0
                or not isinstance(value.get("hit"), bool)
            ):
                unknown += 1
                continue
            confirmed += 1
            payout += amount
            hits += value["hit"]
        bets[name] = {
            "confirmed_count": confirmed,
            "purchase_yen": confirmed * 100,
            "payout_yen": payout,
            "hits": hits,
            "hit_rate": percent(hits, confirmed),
            "return_rate": percent(payout, confirmed * 100),
            "refunded_count": refunded,
            "unknown_count": unknown,
        }
    return {
        "matched_count": len(entries),
        "started_count": len(starts),
        "wins": wins,
        "top2_count": seconds,
        "top3_count": thirds,
        "win_rate": percent(wins, len(starts)),
        "top2_rate": percent(seconds, len(starts)),
        "top3_rate": percent(thirds, len(starts)),
        "nonstarter_count": sum(e["outcome"] == "nonstarter" for e in entries),
        "unknown_outcome_count": sum(e["outcome"] == "unknown" for e in entries),
        "bets": bets,
    }


def analyze(entries, filters=None, group_by=None):
    filters = validate_filters([] if filters is None else filters)
    if group_by is not None and group_by not in FIELDS:
        raise InvalidQuery("未対応の分類軸です。")
    matches, counts = [], {"match": 0, "nonmatch": 0, "unknown": 0}
    for entry in entries:
        state = match_entry(entry, filters)
        counts[state] += 1
        if state == "match":
            matches.append(entry)
    groups = defaultdict(list)
    if group_by:
        for entry in matches:
            value = entry["features"].get(group_by)
            groups[str(value) if value is not None else None].append(entry)
    return {
        "population_count": len(entries),
        "condition_counts": counts,
        "summary": summarize(matches),
        "groups": [
            {"value": key, "label": "不明" if key is None else key, **summarize(values)}
            for key, values in sorted(
                groups.items(),
                key=lambda item: (
                    item[0] is None,
                    number(item[0])
                    if item[0] is not None and FIELDS[group_by][1] == "number"
                    else item[0] or "",
                ),
            )
        ],
        "entries": matches,
    }
