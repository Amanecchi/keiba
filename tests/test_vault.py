import gzip
import hashlib
import base64
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from keiba_acquisition.transport import AcquisitionError, Page
from keiba_web.store import Store, blobs, settings
from keiba_web.vault import MirroredStore, RemoteVault, TABLES
from test_web import payload


class HTTP:
    def __init__(self, status=200):
        self.headers = {}
        self.calls = []
        self.status = status

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        parent = self

        class Response:
            status_code = parent.status

            def json(self):
                return {"ok": True}

        return Response()


def test_vault_credentials_remain_on_exact_https_site_and_redirects_stop():
    http = HTTP(status=302)
    vault = RemoteVault("https://vault-test.chatgpt.site", "test-secret", session=http)
    with pytest.raises(AcquisitionError) as error:
        vault.request("GET", "/api/status")
    assert error.value.stop and "test-secret" not in str(error.value)
    assert http.calls[0][2]["allow_redirects"] is False
    assert http.headers["OAI-Sites-Authorization"] == "Bearer test-secret"
    for url in [
        "http://vault.chatgpt.site",
        "https://vault.chatgpt.site.evil",
        "https://vault.chatgpt.site/?token=a",
        "https://vault.chatgpt.site/other",
    ]:
        with pytest.raises(ValueError):
            RemoteVault(url, "test-secret", session=http)


class RecordingVault:
    def __init__(self):
        self.events = []
        self.fail = False

    def send_query(self, store, table, condition=None):
        self.events.append(table.name)
        if self.fail:
            raise AcquisitionError("remote_storage_error", "Remote unavailable", stop=True)

    def progress(self, store, identity):
        self.events.append("progress")
        if self.fail:
            raise AcquisitionError("remote_storage_error", "Remote unavailable", stop=True)

    def checkpoint(self, store):
        self.events.append("checkpoint")

    def backfill(self, store, progress=None):
        self.events.append("backfill-before-JRA")
        if self.fail:
            raise AcquisitionError("remote_storage_error", "Remote unavailable", stop=True)
        if progress:
            progress("source_blobs")


def page():
    body = b"<html>durable official source</html>"
    return Page(
        url="https://www.jra.go.jp/datafile/seiseki/replay/2025/001.html",
        body=body,
        raw_path="raw/source.html",
        sha256=hashlib.sha256(body).hexdigest(),
        fetched_at=datetime.now(UTC).isoformat(),
        content_type="text/html",
        elapsed_seconds=0,
    )


def test_acquired_original_snapshot_search_and_owner_are_replicated_in_dependency_order():
    remote = RecordingVault()
    store = MirroredStore("sqlite://", vault=remote)
    store.initialize()
    store.save_source(page())
    assert remote.events == ["source_blobs", "source_fetches", "keiba_settings"]
    store.save_race(payload())
    assert remote.events[-2:] == ["analysis_snapshots", "analysis_races"]
    store.save_search("条件", {})
    assert remote.events[-1] == "saved_searches"
    assert store.provision_owner("test-password-hash")
    assert remote.events[-1] == "keiba_settings"


def test_remote_failure_stops_collection_and_preserves_the_local_write_and_failed_job():
    remote = RecordingVault()
    store = MirroredStore("sqlite://", vault=remote)
    store.initialize()
    identity = store.register_job({"mode": "history", "years": [2025]})
    assert store.claim_job(identity, "test-worker")
    assert "backfill-before-JRA" in remote.events
    remote.fail = True
    with pytest.raises(AcquisitionError) as error:
        store.save_source(page())
    assert error.value.stop
    with store.engine.connect() as conn:
        assert len(conn.execute(select(blobs)).all()) == 1
    store.finish_job(identity, "test-worker", "blocked", "Remote storage unavailable")
    assert store.get_job(identity)["state"] == "blocked"
    assert store.get_job(identity)["cursor"] == 0


def test_restore_checks_all_original_hashes_and_rejects_corruption():
    body = b"official source"
    packed = gzip.compress(body, mtime=0)
    row = {
        "sha256": "0" * 64,
        "gzip_body_base64": __import__("base64").b64encode(packed).decode(),
        "stored_bytes": len(packed),
    }
    store = Store("sqlite://")
    store.initialize()
    vault = RemoteVault("https://vault-test.chatgpt.site", "test-secret", session=HTTP())

    def request(method, path, **kwargs):
        if path == "/api/status":
            return {"counts": {}}
        return {"rows": [row], "next": None}

    vault.request = request
    with pytest.raises(ValueError, match="SHA"):
        vault.restore_to(store)
    with store.engine.connect() as conn:
        assert not conn.execute(select(blobs)).all()


def test_failed_resume_preflight_releases_the_lease_and_keeps_the_cursor():
    remote = RecordingVault()
    store = MirroredStore("sqlite://", vault=remote)
    store.initialize()
    identity = store.register_job({"mode": "history", "years": [2025]})
    remote.fail = True
    with pytest.raises(AcquisitionError):
        store.claim_job(identity, "first")
    assert store.get_job(identity)["state"] == "blocked"
    assert store.get_job(identity)["cursor"] == 0
    remote.fail = False
    assert store.claim_job(identity, "second")


def test_restore_recounts_an_original_saved_before_a_crash_updated_its_counter():
    body = b"official original"
    packed = gzip.compress(body, mtime=0)
    row = {
        "sha256": hashlib.sha256(body).hexdigest(),
        "gzip_body_base64": base64.b64encode(packed).decode(),
        "stored_bytes": len(packed),
    }
    counts = {table.name: int(table is blobs) for table in TABLES}
    vault = RemoteVault("https://vault-test.chatgpt.site", "test-secret", session=HTTP())

    def request(method, path, **kwargs):
        if path == "/api/status":
            return {"counts": counts}
        table = kwargs["params"]["table"]
        return {"rows": [row] if table == "source_blobs" else [], "next": None}

    vault.request = request
    store = Store("sqlite://")
    store.initialize()
    vault.restore_to(store)
    with store.engine.connect() as conn:
        counter = conn.scalar(select(settings.c.value).where(settings.c.name == "raw_bytes"))
    assert int(counter) == len(packed)
