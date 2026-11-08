"""Read-only PostgreSQL handoff verification. No JRA requests or DB writes.

Set DATABASE_URL privately; never put credentials in command-line arguments.
Compare --expected with a baseline made before pg_dump. Timestamps and the
PostgreSQL minor version are excluded from the content fingerprint.
"""

import argparse
import gzip
import hashlib
import json
import os
import struct
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, select

from keiba_web.analysis import FIELDS, analyze
from keiba_web.store import SCHEMA_VERSION, encode, metadata


def sha(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return encode(value).encode("utf-8")


def fingerprint(url):
    engine = create_engine(url, hide_parameters=True)
    if engine.dialect.name != "postgresql":
        raise ValueError("PostgreSQL is required.")
    data = {"tables": {}}
    payloads = {}
    latest = []
    original_shas = set()
    fetch_shas = set()
    raw_bytes = 0
    settings = {}
    jobs = []
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
        conn.exec_driver_sql("SET TRANSACTION READ ONLY")
        server = conn.exec_driver_sql("SHOW server_version").scalar_one()
        for table in metadata.sorted_tables:
            digest = hashlib.sha256()
            count = 0
            query = select(table).order_by(*table.primary_key.columns)
            for row in conn.execute(query).mappings():
                value = dict(row)
                if table.name == "source_blobs":
                    compressed = bytes(value["gzip_body"])
                    if len(compressed) != value["stored_bytes"]:
                        raise ValueError("Compressed size mismatch.")
                    body = gzip.decompress(compressed)
                    if sha(body) != value["sha256"]:
                        raise ValueError("Original SHA mismatch.")
                    original_shas.add(value["sha256"])
                    raw_bytes += len(compressed)
                    value["gzip_body"] = {"bytes": len(compressed), "sha256": sha(compressed)}
                elif table.name == "source_fetches":
                    fetch_shas.add(value["sha256"])
                elif table.name == "analysis_snapshots":
                    if sha(value["payload"].encode()) != value["id"]:
                        raise ValueError("Snapshot SHA mismatch.")
                    payload = json.loads(value["payload"])
                    for key in ["race_id", "series_id", "race_date"]:
                        if payload[key] != value[key]:
                            raise ValueError("Snapshot identity mismatch.")
                    payloads[value["id"]] = payload
                elif table.name == "analysis_races":
                    latest.append(value)
                elif table.name == "keiba_settings":
                    settings[value["name"]] = value["value"]
                elif table.name == "acquisition_jobs":
                    plan = json.loads(value["plan"])
                    if (
                        value["state"] in {"queued", "running"}
                        or len(plan) != value["total"]
                        or not 0 <= value["cursor"] <= value["total"]
                    ):
                        raise ValueError("Collector must be stopped with a valid plan.")
                    jobs.append(
                        {
                            "id": value["id"],
                            "state": value["state"],
                            "cursor": value["cursor"],
                            "total": value["total"],
                            "params_sha256": sha(value["params"].encode()),
                            "plan_sha256": sha(value["plan"].encode()),
                            "next_target": plan[value["cursor"]]
                            if value["cursor"] < len(plan)
                            else None,
                        }
                    )
                content = canonical(value)
                digest.update(struct.pack(">Q", len(content)))
                digest.update(content)
                count += 1
            data["tables"][table.name] = {
                "rows": count,
                "content_sha256": digest.hexdigest(),
                "columns": [c.name for c in table.columns],
            }
    engine.dispose()
    if not fetch_shas.issubset(original_shas):
        raise ValueError("Missing original for a fetch.")
    if int(settings["schema_version"]) != SCHEMA_VERSION:
        raise ValueError("Unsupported schema version.")
    if int(settings["raw_bytes"]) != raw_bytes:
        raise ValueError("Original byte counter mismatch.")
    entries, years = [], defaultdict(lambda: {"races": 0, "entries": 0})
    for race in latest:
        payload = payloads.get(race["snapshot_id"])
        if payload is None or any(
            payload[k] != race[column]
            for k, column in [
                ("race_id", "id"),
                ("series_id", "series_id"),
                ("race_date", "race_date"),
            ]
        ):
            raise ValueError("Latest race snapshot mismatch.")
        year = race["race_date"][:4]
        years[year]["races"] += 1
        years[year]["entries"] += len(payload["entries"])
        entries.extend(payload["entries"])
    entries.sort(key=lambda e: e["id"])
    if len({e["id"] for e in entries}) != len(entries):
        raise ValueError("Duplicate current entry ID.")
    cases = [
        ("all", [], None),
        (
            "frame_age_affiliation",
            [
                {"field": "frame", "op": "range", "min": 1, "max": 4},
                {"field": "age", "op": "range", "min": 3, "max": 5},
            ],
            "affiliation",
        ),
        (
            "style_previous_finish_sex",
            [
                {"field": "running_style", "op": "in", "values": ["逃げ", "先行"]},
                {"field": "previous_finish", "op": "range", "min": 1, "max": 3},
            ],
            "sex",
        ),
        (
            "odds_popularity",
            [{"field": "win_odds", "op": "range", "min": 3, "max": 10}],
            "popularity",
        ),
        (
            "previous_class_with_unknowns",
            [{"field": "previous_class", "op": "in", "values": ["未勝利"]}],
            "age",
        ),
    ]
    data.update(
        schema_version=SCHEMA_VERSION,
        originals={"sha_verified": len(original_shas), "compressed_bytes": raw_bytes},
        snapshots_sha_verified=len(payloads),
        latest_races=len(latest),
        latest_entries=len(entries),
        coverage_by_year=dict(sorted(years.items())),
        latest_snapshot_set_sha256=sha(canonical(sorted(r["snapshot_id"] for r in latest))),
        current_entries_sha256=sha(canonical(entries)),
        unknown_feature_counts={
            k: sum(e["features"].get(k) is None for e in entries) for k in FIELDS
        },
        jobs=jobs,
        analysis_cases=[],
    )
    for name, filters, group_by in cases:
        result = analyze(entries, filters, group_by)
        data["analysis_cases"].append(
            {
                "name": name,
                "query": {"filters": filters, "group_by": group_by},
                "population_count": result["population_count"],
                "condition_counts": result["condition_counts"],
                "summary": result["summary"],
                "groups_sha256": sha(canonical(result["groups"])),
                "full_result_sha256": sha(canonical(result)),
            }
        )
    return server, data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    url = os.environ.get("DATABASE_URL")
    if not url:
        parser.error("Set DATABASE_URL in protected local configuration.")
    server, data = fingerprint(url)
    report = {
        "format": "keiba-handoff-verification-v1",
        "verified_at_jst": datetime.now(UTC).astimezone(timezone(timedelta(hours=9))).isoformat(),
        "server_version": server,
        "fingerprint_sha256": sha(canonical(data)),
        "fingerprint": data,
        "expected_matches": None,
    }
    if args.expected:
        expected = json.loads(args.expected.read_text(encoding="utf-8"))
        report["expected_matches"] = expected["fingerprint"] == data
        report["different_sections"] = sorted(
            k
            for k in set(expected["fingerprint"]) | set(data)
            if expected["fingerprint"].get(k) != data.get(k)
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "races": data["latest_races"],
                "entries": data["latest_entries"],
                "originals_verified": data["originals"]["sha_verified"],
                "snapshots_verified": data["snapshots_sha_verified"],
                "analysis_cases": len(data["analysis_cases"]),
                "fingerprint_sha256": report["fingerprint_sha256"],
                "expected_matches": report["expected_matches"],
            },
            ensure_ascii=False,
        )
    )
    return 1 if report["expected_matches"] is False else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        # Do not expose a DB URL/password or row contents in an operational error.
        print(f"DB verification failed ({type(error).__name__}).", file=sys.stderr)
        sys.exit(1)
