"""Replicate acquisition data to the owner's independent, private Sites D1 database."""

import base64
import hashlib
import gzip
import io
import json
import os
import threading
from functools import wraps
from datetime import UTC, datetime
from urllib.parse import urlsplit

import requests
from sqlalchemy import func, select

from keiba_acquisition.transport import AcquisitionError, MAX_BYTES

from .store import (
    Store,
    blobs,
    catalog,
    catalog_runs,
    encode,
    fetches,
    jobs,
    races,
    searches,
    settings,
    snapshots,
)

TABLES = (blobs, fetches, catalog, catalog_runs, snapshots, races, searches, jobs)
IMMUTABLE = {blobs.name, fetches.name, snapshots.name}


class RemoteVault:
    def __init__(self, url, token, *, session=None):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not (parsed.hostname or "").endswith(".chatgpt.site")
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or parsed.port not in {None, 443}
            or not token
        ):
            raise ValueError("本人限定の環境外DBの設定を確認してください。")
        self.url = url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update({"OAI-Sites-Authorization": f"Bearer {token}"})
        self.known = {}

    @classmethod
    def from_environment(cls):
        return cls(os.environ.get("KEIBA_VAULT_URL", ""), os.environ.get("KEIBA_VAULT_TOKEN", ""))

    def request(self, method, path, **kwargs):
        try:
            response = self.session.request(
                method, self.url + path, timeout=30, allow_redirects=False, **kwargs
            )
            if response.status_code != 200:
                raise AcquisitionError(
                    "remote_storage_error",
                    f"環境外DBへの保存を確認できないため取得を停止しました（HTTP {response.status_code}）。",
                    stop=True,
                )
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError("Invalid vault response")
            return value
        except (requests.RequestException, ValueError) as error:
            raise AcquisitionError(
                "remote_storage_error",
                "環境外DBとの通信を確認できないため取得を停止しました。",
                stop=True,
            ) from error

    def inventory(self, table):
        if table not in self.known:
            after, found = None, set()
            while True:
                params = {"table": table}
                if after is not None:
                    params["after"] = after
                value = self.request("GET", "/api/inventory", params=params)
                found.update(value["keys"])
                after = value["next"]
                if after is None:
                    break
            self.known[table] = found
        return self.known[table]

    def push(self, table, rows):
        if table not in {t.name for t in TABLES} | {settings.name, "vault_checkpoints"}:
            raise ValueError("Unsupported vault table")
        packed = []
        for original in rows:
            row = dict(original)
            if "gzip_body" in row:
                row["gzip_body_base64"] = base64.b64encode(row.pop("gzip_body")).decode("ascii")
            packed.append(row)
        batch, size = [], 0
        limit = 3 if table == blobs.name else 25
        for row in packed:
            row_size = len(json.dumps(row, ensure_ascii=True))
            if row_size > 1_500_000:
                raise AcquisitionError(
                    "remote_storage_error", "環境外DBの送信上限のため停止しました。", stop=True
                )
            if batch and (len(batch) >= limit or size + row_size > 1_500_000):
                self.request("POST", "/api/replicate", json={"table": table, "rows": batch})
                batch, size = [], 0
            batch.append(row)
            size += row_size
        if batch:
            self.request("POST", "/api/replicate", json={"table": table, "rows": batch})

    def send_query(self, store, table, condition=None):
        with store.engine.connect() as conn:
            query = select(table)
            if condition is not None:
                query = query.where(condition)
            rows = [dict(row._mapping) for row in conn.execute(query)]
        if table.name in IMMUTABLE:
            pk = next(iter(table.primary_key.columns)).name
            known = self.inventory(table.name)
            rows = [row for row in rows if row[pk] not in known]
            self.push(table.name, rows)
            known.update(row[pk] for row in rows)
        else:
            self.push(table.name, rows)

    def backfill(self, store, progress=None):
        for table in TABLES:
            self.send_query(store, table)
            if progress:
                progress(table.name)
        self.send_query(
            store,
            settings,
            settings.c.name.in_(["schema_version", "raw_bytes", "owner_password_hash"]),
        )
        return self.checkpoint(store)

    def progress(self, store, identity):
        job = store.get_job(identity)
        value = {
            key: job[key] for key in ("id", "state", "cursor", "total", "message", "updated_at")
        }
        self.request("POST", "/api/progress", json=value)

    def checkpoint(self, store):
        with store.engine.connect() as conn:
            expected = {
                table.name: conn.scalar(select(func.count()).select_from(table)) for table in TABLES
            }
        actual = self.request("GET", "/api/status")
        if any(actual["counts"].get(name) != count for name, count in expected.items()):
            raise AcquisitionError(
                "remote_storage_error",
                "環境外DBの保存件数が一致しないため停止しました。",
                stop=True,
            )
        value = {"checked_at": datetime.now(UTC).isoformat(), "counts": expected}
        payload = encode(value)
        self.push(
            "vault_checkpoints",
            [
                {
                    "id": hashlib.sha256(payload.encode()).hexdigest(),
                    "payload": payload,
                    "created_at": value["checked_at"],
                }
            ],
        )
        return value

    def restore_to(self, store, progress=None):
        """Restore a consistent idle vault into a separate, empty initialized database."""
        before = self.request("GET", "/api/status")
        with store.engine.connect() as conn:
            if any(conn.scalar(select(func.count()).select_from(t)) for t in TABLES):
                raise ValueError("復元先には空のDBを指定してください。")
        for table in (*TABLES, settings):
            after = None
            while True:
                params = {"table": table.name}
                if after is not None:
                    params["after"] = after
                page = self.request("GET", "/api/rows", params=params)
                rows = []
                for value in page["rows"]:
                    row = dict(value)
                    if table is blobs:
                        compressed = base64.b64decode(row.pop("gzip_body_base64"), validate=True)
                        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
                            body = stream.read(MAX_BYTES + 1)
                        if (
                            len(body) > MAX_BYTES
                            or len(compressed) != row["stored_bytes"]
                            or hashlib.sha256(body).hexdigest() != row["sha256"]
                        ):
                            raise ValueError("復元する原本のSHA・容量が一致しません。")
                        row["gzip_body"] = compressed
                    if (
                        table is snapshots
                        and hashlib.sha256(row["payload"].encode()).hexdigest() != row["id"]
                    ):
                        raise ValueError("復元する解析版のSHAが一致しません。")
                    rows.append(row)
                with store.engine.begin() as conn:
                    for row in rows:
                        if table is settings:
                            found = conn.execute(
                                select(settings.c.name).where(settings.c.name == row["name"])
                            ).first()
                            if found:
                                conn.execute(
                                    settings.update()
                                    .where(settings.c.name == row["name"])
                                    .values(value=row["value"])
                                )
                                continue
                        conn.execute(table.insert().values(**row))
                after = page["next"]
                if after is None:
                    break
            if progress:
                progress(table.name)
        after = self.request("GET", "/api/status")
        if before != after:
            raise ValueError(
                "復元中に環境外DBが更新されました。復元先を採用せず、空DBへ再実行してください。"
            )
        with store.engine.connect() as conn:
            counts = {t.name: conn.scalar(select(func.count()).select_from(t)) for t in TABLES}
        if any(after["counts"][name] != value for name, value in counts.items()):
            raise ValueError("復元件数が一致しません。")
        # A crash can occur after a blob is acknowledged but before its counter.
        with store.engine.begin() as conn:
            raw_bytes = conn.scalar(select(func.sum(blobs.c.stored_bytes))) or 0
            conn.execute(
                settings.update()
                .where(settings.c.name == "raw_bytes")
                .values(value=encode(raw_bytes))
            )
        return counts


def serial_write(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._mirror_lock:
            return method(self, *args, **kwargs)

    return call


class MirroredStore(Store):
    def __init__(self, url, *, vault, **kwargs):
        super().__init__(url, **kwargs)
        self.vault = vault
        self._finishing = False
        self._mirror_lock = threading.RLock()

    @serial_write
    def save_source(self, page):
        super().save_source(page)
        self.vault.send_query(self, blobs, blobs.c.sha256 == page.sha256)
        identity = hashlib.sha256(
            f"{page.url}|{page.request_method}|{page.fetched_at}".encode()
        ).hexdigest()
        self.vault.send_query(self, fetches, fetches.c.id == identity)
        self.vault.send_query(self, settings, settings.c.name == "raw_bytes")

    @serial_write
    def save_catalog(self, items, **kwargs):
        super().save_catalog(items, **kwargs)
        if items:
            self.vault.send_query(
                self, catalog, catalog.c.archive_url.in_([item["archive_url"] for item in items])
            )
        if kwargs.get("year") is not None:
            self.vault.send_query(self, catalog_runs, catalog_runs.c.year == kwargs["year"])

    @serial_write
    def save_race(self, payload):
        key = super().save_race(payload)
        self.vault.send_query(self, snapshots, snapshots.c.id == key)
        self.vault.send_query(self, races, races.c.id == payload["race_id"])
        return key

    @serial_write
    def register_job(self, params):
        identity = super().register_job(params)
        self.vault.send_query(self, jobs, jobs.c.id == identity)
        return identity

    @serial_write
    def claim_job(self, identity, owner):
        claimed = super().claim_job(identity, owner)
        if claimed:
            # Recover a local write whose previous remote acknowledgement failed.
            # Do this before allowing any further JRA requests or cached imports.
            try:
                self.vault.backfill(self, progress=lambda _: Store.heartbeat(self, identity, owner))
            except AcquisitionError as error:
                self.finish_job(identity, owner, "blocked", str(error))
                raise
        return claimed

    @serial_write
    def save_search(self, name, payload):
        identity = super().save_search(name, payload)
        self.vault.send_query(self, searches, searches.c.id == identity)
        return identity

    @serial_write
    def provision_owner(self, password_hash):
        created = super().provision_owner(password_hash)
        if created:
            self.vault.send_query(self, settings, settings.c.name == "owner_password_hash")
        return created

    @serial_write
    def heartbeat(self, identity, owner, **progress):
        super().heartbeat(identity, owner, **progress)
        if self._finishing:
            return
        if "plan" in progress:
            self.vault.send_query(self, jobs, jobs.c.id == identity)
        elif "cursor" in progress or "state" in progress:
            self.vault.progress(self, identity)
            self.vault.checkpoint(self)

    @serial_write
    def finish_job(self, identity, owner, state, message):
        self._finishing = True
        try:
            super().finish_job(identity, owner, state, message)
        finally:
            self._finishing = False
        try:
            self.vault.progress(self, identity)
            self.vault.checkpoint(self)
        except AcquisitionError:
            if state == "completed":
                with self.engine.begin() as conn:
                    conn.execute(
                        jobs.update()
                        .where(jobs.c.id == identity)
                        .values(
                            state="blocked",
                            message="環境外DBへの最終保存確認が必要です。保存済み分は保持しています。",
                        )
                    )
