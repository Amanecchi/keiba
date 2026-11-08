"""Offline capacity/query experiment; never contacts JRA or changes the source DB.

KEIBA_ASSESSMENT_SOURCE_URL: read-only use of the existing SQLAlchemy Postgres URL.
KEIBA_ASSESSMENT_BENCH_URL: disposable, distinct Postgres database (required).
Real HTML/payloads stay in the ignored output directory; publish metrics only.
The typed table is a performance prototype, not a production schema migration.
"""

import argparse
import gc
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import statistics
import struct
import time
import tracemalloc
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import psycopg
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from keiba_web.analysis import analyze


def timed(function, repeats=7):
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        value = function()
        samples.append((time.perf_counter() - start) * 1000)
    return {
        "runs": repeats,
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(sorted(samples)[math.ceil(repeats * 0.95) - 1], 3),
        "samples_ms": [round(x, 3) for x in samples],
    }, value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rows", type=int, nargs="+", default=[500_000, 1_000_000])
    args = parser.parse_args()
    ignored_work = Path(__file__).resolve().parents[1] / "work"
    if not args.output.resolve().is_relative_to(ignored_work):
        parser.error("Use an ignored output directory under this repository's work/.")
    if any(count < 1 or count > 2_000_000 for count in args.rows):
        parser.error("Each synthetic row count must be between 1 and 2,000,000.")
    args.output.mkdir(parents=True, exist_ok=True)
    source_url = make_url(os.environ["KEIBA_ASSESSMENT_SOURCE_URL"])
    bench_url = make_url(os.environ["KEIBA_ASSESSMENT_BENCH_URL"])
    if source_url.database == bench_url.database or not bench_url.database.startswith(
        "keiba_capacity_"
    ):
        raise ValueError("Use a separate disposable keiba_capacity_* database.")
    engine = create_engine(source_url, hide_parameters=True)
    result = {
        "checked_at_jst": datetime.now(UTC).astimezone(ZoneInfo("Asia/Tokyo")).isoformat(),
        "network": "source/benchmark Postgres on loopback; no JRA requests",
        "synthetic_warning": "Cyclic real templates, fabricated IDs/dates/classes; not acquired races or production SLA",
    }
    with engine.connect() as c:
        c.exec_driver_sql("SET TRANSACTION READ ONLY")
        result["source_job"] = dict(
            c.exec_driver_sql("SELECT state,cursor,total FROM acquisition_jobs").mappings().one()
        )
        if result["source_job"]["state"] in {"running", "queued"}:
            raise RuntimeError("Collector must remain stopped during assessment.")
        result["postgres_version"] = c.exec_driver_sql("SHOW server_version").scalar_one()
        result["source_table_sizes"] = [
            dict(r)
            for r in c.exec_driver_sql(
                "SELECT relname,pg_relation_size(relid) heap_bytes,"
                "pg_total_relation_size(relid) total_bytes FROM pg_stat_user_tables "
                "ORDER BY relname"
            ).mappings()
        ]
        result["source_database_bytes"] = c.exec_driver_sql(
            "SELECT pg_database_size(current_database())"
        ).scalar_one()
        rows = c.exec_driver_sql(
            "SELECT s.id,s.payload FROM analysis_snapshots s "
            "JOIN analysis_races r ON r.snapshot_id=s.id ORDER BY s.race_date,s.id"
        ).all()
        result["latest_races"] = len(rows)
        result["latest_payload_bytes"] = sum(len(row.payload.encode()) for row in rows)
        result["latest_payload_gzip_bytes"] = sum(
            len(gzip.compress(row.payload.encode())) for row in rows
        )
        originals = c.exec_driver_sql("SELECT sha256,gzip_body FROM source_blobs ORDER BY sha256")
        raw_gzip = raw_bytes = pack_bytes = verified = 0
        buffer = bytearray()
        pack_number = 0

        def save_pack(data):
            nonlocal pack_number, pack_bytes, verified
            compressed = gzip.compress(data, compresslevel=6, mtime=0)
            path = args.output / f"original-pack-{pack_number:03d}.gz"
            path.write_bytes(compressed)
            # Verify exact original bodies and hashes from the newly made copy.
            stream = io.BytesIO(gzip.decompress(path.read_bytes()))
            while header := stream.read(40):
                digest, length = header[:32], struct.unpack(">Q", header[32:])[0]
                body = stream.read(length)
                assert len(body) == length and hashlib.sha256(body).digest() == digest
                verified += 1
            pack_bytes += len(compressed)
            pack_number += 1

        for sha, compressed in originals:
            body = gzip.decompress(compressed)
            assert hashlib.sha256(body).hexdigest() == sha
            raw_gzip += len(compressed)
            raw_bytes += len(body)
            buffer.extend(bytes.fromhex(sha) + struct.pack(">Q", len(body)) + body)
            if len(buffer) >= 16 * 1024 * 1024:
                save_pack(buffer)
                buffer.clear()
        if buffer:
            save_pack(buffer)
        result["originals"] = {
            "count": verified,
            "uncompressed_bytes": raw_bytes,
            "individual_gzip_bytes": raw_gzip,
            "pack_gzip_bytes": pack_bytes,
            "pack_count": pack_number,
            "all_hashes_restored": True,
            "warning": "Offline copy only; 16 MiB packs, SHA-sorted; no production pack index, crash protocol or remote restore yet",
        }
    print("Measured existing DB; packed and verified all saved originals.", flush=True)
    filters = [
        {"field": "frame", "op": "range", "min": 1, "max": 4},
        {"field": "age", "op": "range", "min": 3, "max": 5},
    ]
    texts = [r.payload for r in rows]

    def old_pipeline():
        entries = [e for t in texts for e in json.loads(t)["entries"]]
        return json.dumps(
            analyze(entries, filters, "affiliation"), ensure_ascii=False, separators=(",", ":")
        ).encode()

    result["existing_broad_pipeline_timing"], response = timed(old_pipeline, 5)
    result["existing_broad_response_bytes"] = len(response)
    gc.collect()
    tracemalloc.start()
    response = old_pipeline()
    result["existing_broad_pipeline_peak_traced_bytes"] = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    entries = [e for t in texts for e in json.loads(t)["entries"]]
    result["latest_entries"] = len(entries)
    golden = analyze(entries, filters)
    result["golden_condition_counts"] = golden["condition_counts"]
    result["golden_summary"] = golden["summary"]
    print("Measured current JSON/Python path; preparing isolated typed SQL table.", flush=True)
    dsn = bench_url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute("SET statement_timeout='60s'")
        db.execute("SET max_parallel_workers_per_gather=0")
        name = "probe_" + datetime.now(UTC).strftime("%H%M%S%f")
        db.execute(f"CREATE SCHEMA {name}")
        db.execute(f"SET search_path TO {name}")
        ddl = """(
            id bigint PRIMARY KEY, race_id bigint NOT NULL, horse_id bigint NOT NULL,
            race_date date NOT NULL, venue smallint, surface smallint, distance smallint,
            race_class smallint, frame smallint, running_style text, jockey text, sire text,
            sex text, age smallint, affiliation text, previous_class text,
            previous_distance smallint, interval_days smallint, popularity smallint,
            previous_race text, previous_finish smallint, win_odds numeric(12,2),
            finish smallint, outcome text, win_eligible boolean, win_hit boolean,
            win_payout integer, win_reason text, place_eligible boolean, place_hit boolean,
            place_payout integer, place_reason text, snapshot_sha bytea,
            source_fetch_id bigint, evidence_sha bytea
        )"""
        db.execute("CREATE UNLOGGED TABLE base " + ddl)
        with db.cursor().copy("COPY base FROM STDIN") as cp:
            for n, e in enumerate(entries, 1):
                f, w, p = e["features"], e["bets"]["単勝"], e["bets"]["複勝"]
                cp.write_row(
                    (
                        n,
                        (n - 1) // 15,
                        int(e["horse_id"]),
                        e["race_date"],
                        1,
                        1,
                        2000,
                        8,
                        f.get("frame"),
                        f.get("running_style"),
                        f.get("jockey"),
                        f.get("sire"),
                        f.get("sex"),
                        f.get("age"),
                        f.get("affiliation"),
                        f.get("previous_class"),
                        f.get("previous_distance"),
                        f.get("interval_days"),
                        f.get("popularity"),
                        f.get("previous_race"),
                        f.get("previous_finish"),
                        f.get("win_odds"),
                        e.get("finish"),
                        e["outcome"],
                        w.get("eligible"),
                        w.get("hit"),
                        w.get("payout_yen"),
                        w.get("reason"),
                        p.get("eligible"),
                        p.get("hit"),
                        p.get("payout_yen"),
                        p.get("reason"),
                        hashlib.sha256(e["id"].encode()).digest(),
                        n,
                        bytes.fromhex(e["source"]["sha256"]),
                    )
                )
        predicate = "(frame BETWEEN 1 AND 4 AND age BETWEEN 3 AND 5)"
        started = "outcome IN ('finished','dnf','disqualified')"

        def eligible(kind):
            return (
                f"{started} AND {kind}_eligible IS TRUE "
                f"AND {kind}_hit IS NOT NULL AND {kind}_payout>=0"
            )

        aggregate = f"""SELECT COUNT(*),COUNT(*) FILTER(WHERE {predicate}),
            COUNT(*) FILTER(WHERE NOT {predicate}),COUNT(*) FILTER(WHERE {predicate} IS NULL),
            COUNT(*) FILTER(WHERE {predicate} AND {started}),
            COUNT(*) FILTER(WHERE {predicate} AND finish=1 AND {started}),
            COUNT(*) FILTER(WHERE {predicate} AND {eligible("win")}),
            COALESCE(SUM(win_payout) FILTER(WHERE {predicate} AND {eligible("win")}),0),
            COUNT(*) FILTER(WHERE {predicate} AND {eligible("place")}),
            COALESCE(SUM(place_payout) FILTER(WHERE {predicate} AND {eligible("place")}),0)
            FROM {{table}}"""
        actual = db.execute(aggregate.format(table="base")).fetchone()
        expected = (
            len(entries),
            *[golden["condition_counts"][x] for x in ["match", "nonmatch", "unknown"]],
            golden["summary"]["started_count"],
            golden["summary"]["wins"],
            golden["summary"]["bets"]["単勝"]["confirmed_count"],
            golden["summary"]["bets"]["単勝"]["payout_yen"],
            golden["summary"]["bets"]["複勝"]["confirmed_count"],
            golden["summary"]["bets"]["複勝"]["payout_yen"],
        )
        assert actual == expected, (actual, expected)
        result["typed_sql_golden_agrees"] = True
        db.execute("CREATE UNLOGGED TABLE entries " + ddl)
        columns = [
            r[0]
            for r in db.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name='base' ORDER BY ordinal_position",
                (name,),
            )
        ]
        previous = 0
        result["typed_sql_benchmarks"] = []
        for count in sorted(set(args.rows)):
            expressions = {
                "id": "g",
                "race_id": "(g-1)/15",
                "horse_id": "g % 125000",
                "race_date": "DATE '2006-01-01' + (((g-1)/15) %% 7305)::int",
                "race_class": "(((g-1)/15) % 8 + 1)::smallint",
            }
            # %% is only needed with bind parameters; compose bounded integer literals below.
            expressions["race_date"] = expressions["race_date"].replace("%%", "%")
            selected = ",".join(expressions.get(col, "b." + col) for col in columns)
            db.execute(
                f"INSERT INTO entries SELECT {selected} FROM generate_series({previous + 1},{count}) g JOIN base b ON b.id=((g-1)%{len(entries)})+1"
            )
            if previous == 0:
                for label, cols in [
                    ("date_frame_age", "race_date,frame,age"),
                    ("sire_date", "sire,race_date"),
                    ("jockey_date", "jockey,race_date"),
                    ("class_date", "race_class,race_date"),
                ]:
                    db.execute(f"CREATE INDEX {label} ON entries ({cols})")
            db.execute("VACUUM ANALYZE entries")
            broad = aggregate.format(table="entries")
            selective = f"SELECT COUNT(*),SUM(win_payout),SUM(place_payout) FROM entries WHERE {predicate} AND race_date BETWEEN '2017-01-01' AND '2019-12-31' AND race_class=1"
            detail = f"SELECT id,race_date,horse_id,frame,jockey,age,sire,win_odds,finish FROM entries WHERE {predicate} ORDER BY id LIMIT 25"
            grouped = f"SELECT affiliation,COUNT(*),COUNT(*) FILTER(WHERE finish=1),SUM(win_payout),SUM(place_payout) FROM entries WHERE {predicate} GROUP BY affiliation"
            metrics = {"rows": count}
            for label, query in [
                ("full_summary", broad),
                ("selective_summary", selective),
                ("first_page_25", detail),
                ("grouped", grouped),
            ]:
                metrics[label], value = timed(lambda q=query: db.execute(q).fetchall())
                metrics[label]["returned_bytes_json"] = len(
                    json.dumps(value, default=str, ensure_ascii=False).encode()
                )
            metrics["table_total_bytes"] = db.execute(
                "SELECT pg_total_relation_size('entries')"
            ).fetchone()[0]
            metrics["table_heap_bytes"] = db.execute(
                "SELECT pg_relation_size('entries')"
            ).fetchone()[0]
            metrics["indexes_bytes"] = db.execute("SELECT pg_indexes_size('entries')").fetchone()[0]
            plans = {
                label: db.execute("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + q).fetchone()[0]
                for label, q in [
                    ("full_summary", broad),
                    ("selective_summary", selective),
                    ("first_page_25", detail),
                ]
            }
            (args.output / f"explain-{count}.json").write_text(json.dumps(plans, indent=2))
            result["typed_sql_benchmarks"].append(metrics)
            previous = count
            print(f"Completed {count:,} typed rows and query timings.", flush=True)
        result["benchmark_schema"] = name
    # Publish only counts, timings and derived measurements, never originals/credentials.
    (args.output / "measurements.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
