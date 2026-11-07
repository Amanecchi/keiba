"""Durable SQL storage. SQLite is supported for tests; hosted configuration requires Postgres."""

import gzip
import hashlib
import io
import json
import time
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Column,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    func,
    select,
)
from sqlalchemy.pool import StaticPool

from keiba_acquisition.transport import AcquisitionError, MAX_BYTES

SCHEMA_VERSION = 1
metadata = MetaData()
settings = Table(
    "keiba_settings",
    metadata,
    Column("name", String(80), primary_key=True),
    Column("value", Text, nullable=False),
)
blobs = Table(
    "source_blobs",
    metadata,
    Column("sha256", String(64), primary_key=True),
    Column("gzip_body", LargeBinary, nullable=False),
    Column("stored_bytes", Integer, nullable=False),
)
fetches = Table(
    "source_fetches",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("url", Text, nullable=False, index=True),
    Column("method", String(8), nullable=False),
    Column("fetched_at", String(40), nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("page_meta", Text, nullable=False),
)
catalog = Table(
    "race_catalog",
    metadata,
    Column("archive_url", Text, primary_key=True),
    Column("series_id", String(80), nullable=False, index=True),
    Column("race_date", String(10), nullable=False, index=True),
    Column("payload", Text, nullable=False),
)
catalog_runs = Table(
    "catalog_runs",
    metadata,
    Column("year", Integer, primary_key=True),
    Column("through_date", String(10), nullable=False),
    Column("source_meta", Text, nullable=False),
)
races = Table(
    "analysis_races",
    metadata,
    Column("id", String(100), primary_key=True),
    Column("series_id", String(80), nullable=False, index=True),
    Column("race_date", String(10), nullable=False, index=True),
    Column("snapshot_id", String(64), nullable=False),
    Column("updated_at", String(40), nullable=False),
)
snapshots = Table(
    "analysis_snapshots",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("race_id", String(100), nullable=False, index=True),
    Column("series_id", String(80), nullable=False, index=True),
    Column("race_date", String(10), nullable=False, index=True),
    Column("payload", Text, nullable=False),
    Column("created_at", String(40), nullable=False),
)
searches = Table(
    "saved_searches",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(100), nullable=False),
    Column("payload", Text, nullable=False),
    Column("updated_at", String(40), nullable=False),
)
jobs = Table(
    "acquisition_jobs",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("params", Text, nullable=False),
    Column("state", String(30), nullable=False),
    Column("plan", Text, nullable=False),
    Column("cursor", Integer, nullable=False),
    Column("total", Integer, nullable=False),
    Column("message", Text, nullable=False),
    Column("updated_at", String(40), nullable=False),
)


class CapacityExceeded(RuntimeError):
    pass


class JobBusy(RuntimeError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def now():
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, url, *, raw_limit=350_000_000):
        options = {"pool_pre_ping": True, "hide_parameters": True}
        if url.startswith("sqlite"):
            options.update(connect_args={"check_same_thread": False})
            if url in {"sqlite://", "sqlite:///:memory:"}:
                options["poolclass"] = StaticPool
        else:
            options.update(pool_size=2, max_overflow=2)
        self.engine = create_engine(url, **options)
        self.raw_limit = raw_limit

    def initialize(self):
        with self.engine.begin() as conn:
            if conn.dialect.name == "postgresql":
                conn.exec_driver_sql("SELECT pg_advisory_xact_lock(747662169)")
            metadata.create_all(conn)
            existing = conn.execute(
                select(settings.c.value).where(settings.c.name == "schema_version")
            ).scalar()
            if existing is not None and int(existing) != SCHEMA_VERSION:
                raise RuntimeError("DBスキーマの版が一致しません。")
            for key, value in [
                ("schema_version", SCHEMA_VERSION),
                ("raw_bytes", 0),
                ("collector_lease", {}),
            ]:
                if (
                    conn.execute(select(settings.c.name).where(settings.c.name == key)).first()
                    is None
                ):
                    conn.execute(settings.insert().values(name=key, value=encode(value)))

    def save_source(self, page):
        if len(page.body) > MAX_BYTES or hashlib.sha256(page.body).hexdigest() != page.sha256:
            raise AcquisitionError("cache_corrupt", "原本のハッシュが一致しません。")
        compressed = gzip.compress(page.body, mtime=0)
        with self.engine.begin() as conn:
            counter = int(
                conn.execute(
                    select(settings.c.value).where(settings.c.name == "raw_bytes").with_for_update()
                ).scalar_one()
            )
            exists = conn.execute(
                select(blobs.c.sha256).where(blobs.c.sha256 == page.sha256)
            ).first()
            if exists is None:
                if counter + len(compressed) > self.raw_limit:
                    raise CapacityExceeded("無料保存枠の上限に達するため収集を停止しました。")
                if conn.dialect.name == "postgresql":
                    used = conn.exec_driver_sql(
                        "SELECT pg_database_size(current_database())"
                    ).scalar_one()
                    if used + len(compressed) > 750_000_000:
                        raise CapacityExceeded("DBの保存上限に近付いたため収集を停止しました。")
                conn.execute(
                    blobs.insert().values(
                        sha256=page.sha256, gzip_body=compressed, stored_bytes=len(compressed)
                    )
                )
                conn.execute(
                    settings.update()
                    .where(settings.c.name == "raw_bytes")
                    .values(value=encode(counter + len(compressed)))
                )
            identity = hashlib.sha256(
                f"{page.url}|{page.request_method}|{page.fetched_at}".encode()
            ).hexdigest()
            if conn.execute(select(fetches.c.id).where(fetches.c.id == identity)).first() is None:
                conn.execute(
                    fetches.insert().values(
                        id=identity,
                        url=page.url,
                        method=page.request_method,
                        fetched_at=page.fetched_at,
                        sha256=page.sha256,
                        page_meta=encode(page.metadata()),
                    )
                )

    def owner_password_hash(self):
        with self.engine.connect() as conn:
            return conn.execute(
                select(settings.c.value).where(settings.c.name == "owner_password_hash")
            ).scalar()

    def provision_owner(self, password_hash):
        with self.engine.begin() as conn:
            conn.execute(
                select(settings.c.value)
                .where(settings.c.name == "schema_version")
                .with_for_update()
            ).scalar_one()
            if conn.execute(
                select(settings.c.name).where(settings.c.name == "owner_password_hash")
            ).first():
                return False
            conn.execute(settings.insert().values(name="owner_password_hash", value=password_hash))
        return True

    def cached_source(self, url, method):
        with self.engine.connect() as conn:
            row = conn.execute(
                select(fetches.c.page_meta, blobs.c.gzip_body)
                .join(blobs, blobs.c.sha256 == fetches.c.sha256)
                .where(fetches.c.url == url, fetches.c.method == method)
                .order_by(fetches.c.fetched_at.desc())
                .limit(1)
            ).first()
        if row is None:
            return None
        meta = json.loads(row.page_meta)
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(row.gzip_body)) as stream:
                body = stream.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES or hashlib.sha256(body).hexdigest() != meta["sha256"]:
                raise ValueError("hash mismatch")
        except (OSError, EOFError, ValueError) as error:
            raise AcquisitionError("cache_corrupt", "DBに保存した原本を検証できません。") from error
        return meta, body

    def save_catalog(self, items, *, year=None, through_date=None, source_meta=None):
        with self.engine.begin() as conn:
            for item in items:
                values = dict(
                    series_id=item["series_id"], race_date=item["race_date"], payload=encode(item)
                )
                found = conn.execute(
                    select(catalog.c.archive_url).where(
                        catalog.c.archive_url == item["archive_url"]
                    )
                ).first()
                if found:
                    conn.execute(
                        catalog.update()
                        .where(catalog.c.archive_url == item["archive_url"])
                        .values(**values)
                    )
                else:
                    conn.execute(catalog.insert().values(archive_url=item["archive_url"], **values))
            if year is not None and through_date:
                values = dict(through_date=through_date, source_meta=encode(source_meta or {}))
                if conn.execute(
                    select(catalog_runs.c.year).where(catalog_runs.c.year == year)
                ).first():
                    conn.execute(
                        catalog_runs.update().where(catalog_runs.c.year == year).values(**values)
                    )
                else:
                    conn.execute(catalog_runs.insert().values(year=year, **values))

    def save_race(self, payload):
        key = hashlib.sha256(encode(payload).encode()).hexdigest()
        race_id = payload["race_id"]
        with self.engine.begin() as conn:
            if conn.execute(select(snapshots.c.id).where(snapshots.c.id == key)).first() is None:
                conn.execute(
                    snapshots.insert().values(
                        id=key,
                        race_id=race_id,
                        series_id=payload["series_id"],
                        race_date=payload["race_date"],
                        payload=encode(payload),
                        created_at=now(),
                    )
                )
            value = dict(
                series_id=payload["series_id"],
                race_date=payload["race_date"],
                snapshot_id=key,
                updated_at=now(),
            )
            if conn.execute(select(races.c.id).where(races.c.id == race_id)).first():
                conn.execute(races.update().where(races.c.id == race_id).values(**value))
            else:
                conn.execute(races.insert().values(id=race_id, **value))
        return key

    def dataset(self, series_id, start, end, snapshot_ids=None):
        with self.engine.connect() as conn:
            query = select(snapshots.c.id, snapshots.c.payload)
            if snapshot_ids is None:
                query = query.join(races, races.c.snapshot_id == snapshots.c.id)
            else:
                query = query.where(snapshots.c.id.in_(snapshot_ids))
            rows = conn.execute(
                query.where(
                    snapshots.c.series_id == series_id,
                    snapshots.c.race_date >= start,
                    snapshots.c.race_date <= end,
                ).order_by(snapshots.c.race_date)
            ).all()
            expected = conn.execute(
                select(catalog.c.archive_url).where(
                    catalog.c.series_id == series_id,
                    catalog.c.race_date >= start,
                    catalog.c.race_date <= end,
                )
            ).all()
            checked = dict(
                conn.execute(select(catalog_runs.c.year, catalog_runs.c.through_date)).all()
            )
        required = range(int(start[:4]), int(end[:4]) + 1)
        missing_years = [
            year for year in required if checked.get(year, "") < min(end, f"{year}-12-31")
        ]
        catalog_complete = not missing_years
        payloads = [json.loads(row.payload) for row in rows]
        if snapshot_ids is not None and (
            len(rows) != len(snapshot_ids)
            or len({payload["race_id"] for payload in payloads}) != len(payloads)
        ):
            raise ValueError("保存時のデータ版を確認できません。")
        return {
            "entries": [entry for payload in payloads for entry in payload["entries"]],
            "snapshot_ids": [row.id for row in rows],
            "version": hashlib.sha256("|".join(row.id for row in rows).encode()).hexdigest(),
            "coverage": {
                "expected_races": len(expected) if catalog_complete else None,
                "known_expected_races": len(expected),
                "missing_catalog_years": missing_years,
                "imported_races": len(rows),
                "state": "no_races_in_period"
                if not rows and catalog_complete and not expected
                else "not_collected"
                if not rows
                else ("complete" if catalog_complete and len(expected) == len(rows) else "partial"),
            },
        }

    def series_counts(self):
        with self.engine.connect() as conn:
            return dict(
                conn.execute(
                    select(races.c.series_id, func.count()).group_by(races.c.series_id)
                ).all()
            )

    def current_race(self, item):
        with self.engine.connect() as conn:
            row = conn.execute(
                select(snapshots.c.payload)
                .join(races, races.c.snapshot_id == snapshots.c.id)
                .where(
                    races.c.series_id == item["series_id"], races.c.race_date == item["race_date"]
                )
            ).first()
        return json.loads(row.payload) if row else None

    def save_search(self, name, payload):
        identity = uuid.uuid4().hex
        with self.engine.begin() as conn:
            conn.execute(
                searches.insert().values(
                    id=identity, name=name, payload=encode(payload), updated_at=now()
                )
            )
        return identity

    def list_searches(self):
        with self.engine.connect() as conn:
            return [
                {"id": row.id, "name": row.name, "query": json.loads(row.payload)}
                for row in conn.execute(select(searches).order_by(searches.c.updated_at.desc()))
            ]

    def register_job(self, params):
        with self.engine.begin() as conn:
            conn.execute(
                select(settings.c.value)
                .where(settings.c.name == "collector_lease")
                .with_for_update()
            ).scalar_one()
            active = conn.execute(
                select(jobs).where(jobs.c.state.in_(["queued", "running"]))
            ).first()
            if active:
                if json.loads(active.params) == params:
                    return active.id
                raise JobBusy("収集処理が登録済みです。進捗を確認してください。")
            identity = uuid.uuid4().hex
            conn.execute(
                jobs.insert().values(
                    id=identity,
                    params=encode(params),
                    state="queued",
                    plan="[]",
                    cursor=0,
                    total=0,
                    message="収集待ち",
                    updated_at=now(),
                )
            )
        return identity

    def list_jobs(self):
        with self.engine.connect() as conn:
            return [
                dict(row._mapping) | {"params": json.loads(row.params)}
                for row in conn.execute(
                    select(*[c for c in jobs.c if c.name != "plan"])
                    .order_by(jobs.c.updated_at.desc())
                    .limit(20)
                )
            ]

    def get_job(self, identity):
        with self.engine.connect() as conn:
            row = conn.execute(select(jobs).where(jobs.c.id == identity)).first()
        return (
            dict(row._mapping) | {"params": json.loads(row.params), "plan": json.loads(row.plan)}
            if row
            else None
        )

    def claim_job(self, identity, owner):
        with self.engine.begin() as conn:
            lease = json.loads(
                conn.execute(
                    select(settings.c.value)
                    .where(settings.c.name == "collector_lease")
                    .with_for_update()
                ).scalar_one()
            )
            if lease.get("until", 0) > time.time():
                return False
            job = conn.execute(select(jobs).where(jobs.c.id == identity)).first()
            if job is None or job.state == "completed":
                return False
            conn.execute(
                settings.update()
                .where(settings.c.name == "collector_lease")
                .values(value=encode({"job": identity, "owner": owner, "until": time.time() + 120}))
            )
            conn.execute(
                jobs.update().where(jobs.c.id == identity).values(state="running", updated_at=now())
            )
        return True

    def heartbeat(self, identity, owner, **progress):
        with self.engine.begin() as conn:
            lease = json.loads(
                conn.execute(
                    select(settings.c.value)
                    .where(settings.c.name == "collector_lease")
                    .with_for_update()
                ).scalar_one()
            )
            if lease.get("owner") != owner or lease.get("job") != identity:
                raise JobBusy("収集処理の実行権を確認できません。")
            conn.execute(
                settings.update()
                .where(settings.c.name == "collector_lease")
                .values(value=encode({"job": identity, "owner": owner, "until": time.time() + 120}))
            )
            conn.execute(
                jobs.update().where(jobs.c.id == identity).values(updated_at=now(), **progress)
            )

    def finish_job(self, identity, owner, state, message):
        self.heartbeat(identity, owner, state=state, message=message)
        with self.engine.begin() as conn:
            lease = json.loads(
                conn.execute(
                    select(settings.c.value)
                    .where(settings.c.name == "collector_lease")
                    .with_for_update()
                ).scalar_one()
            )
            if lease.get("owner") == owner:
                conn.execute(
                    settings.update().where(settings.c.name == "collector_lease").values(value="{}")
                )
