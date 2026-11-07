import copy
import csv
import gzip
import hashlib
import io
import json
from datetime import UTC, datetime

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import select
from werkzeug.security import generate_password_hash

from keiba_acquisition.catalog import load_registry
from keiba_acquisition.transport import Client, Page
from keiba_web.app import create_app
from keiba_web.collector import PersistentClient
from keiba_web.store import CapacityExceeded, Store, blobs, snapshots
from test_analysis import entries
from test_transport import Session

SERIES = load_registry()["series"][0]["id"]


def payload(win=600):
    data = entries()
    for index, entry in enumerate(data):
        entry.update(
            race_id="race-1",
            race_date="2025-11-02",
            horse_id=f"202000000{index}",
            horse_name=f"架空馬{index}",
            finish_text=str(entry["finish"]),
            source={"url": "https://www.jra.go.jp/JRADB/accessS.html?CNAME=public"},
        )
    data[0]["bets"]["単勝"]["payout_yen"] = win
    return {"race_id": "race-1", "series_id": SERIES, "race_date": "2025-11-02", "entries": data}


class FakeRunner:
    def __init__(self):
        self.calls = []

    def start(self, identity):
        self.calls.append(identity)
        return True


@pytest.fixture
def web(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'test.sqlite'}")
    store.initialize()
    store.save_catalog(
        [
            {
                "archive_url": "https://www.jra.go.jp/datafile/seiseki/replay/2025/001.html",
                "series_id": SERIES,
                "race_date": "2025-11-02",
            }
        ]
    )
    store.save_race(payload())
    runner = FakeRunner()
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-only-session-key-" * 3,
            "OWNER_PASSWORD_HASH": generate_password_hash(
                "test-password", method="pbkdf2:sha256:1000"
            ),
        },
        store=store,
        runner=runner,
    )
    return app, store, runner


def login(client):
    response = client.get("/login", base_url="https://localhost")
    token = BeautifulSoup(response.data, "html.parser").select_one("input[name=csrf]")["value"]
    response = client.post(
        "/login",
        data={"name": "owner", "password": "test-password", "csrf": token},
        base_url="https://localhost",
    )
    assert response.status_code == 302
    with client.session_transaction() as session:
        return session["csrf"]


def post(client, path, token, body):
    return client.post(
        path, json=body, headers={"X-CSRF-Token": token}, base_url="https://localhost"
    )


def query():
    return {
        "series_id": SERIES,
        "start_date": "2016-01-01",
        "end_date": "2025-12-31",
        "filters": [],
        "group_by": "frame",
    }


def test_owner_only_access_and_csrf_protect_data_and_collection(web):
    app, store, runner = web
    client = app.test_client()
    assert client.get("/api/meta").status_code == 401
    assert client.get("/healthz").status_code == 200
    token = login(client)
    assert client.get("/api/meta", base_url="https://localhost").status_code == 200
    assert client.post("/api/jobs", json={}).status_code == 403
    bad = client.post(
        "/api/analyze",
        json=query(),
        headers={"X-CSRF-Token": token, "Origin": "https://example.com"},
        base_url="https://localhost",
    )
    assert bad.status_code == 403
    assert not runner.calls


def test_owner_can_provision_once_in_browser_and_login_after_restart(tmp_path):
    url = f"sqlite:///{tmp_path / 'owner.sqlite'}"
    store = Store(url)
    config = {
        "TESTING": True,
        "SECRET_KEY": "test-only-secret-" * 3,
        "OWNER_PASSWORD_HASH": None,
        "OWNER_SETUP_TOKEN": "test-only-setup-token-" * 3,
    }
    app = create_app(config, store=store, runner=FakeRunner())
    client = app.test_client()
    assert client.get("/login").location == "/setup"
    response = client.get("/setup", base_url="https://localhost")
    csrf = BeautifulSoup(response.data, "html.parser").select_one("input[name=csrf]")["value"]
    assert config["OWNER_SETUP_TOKEN"].encode() not in response.data
    form = {
        "csrf": csrf,
        "setup_token": "wrong",
        "password": "owner-only-password",
        "confirmation": "owner-only-password",
    }
    assert client.post("/setup", data=form, base_url="https://localhost").status_code == 400
    assert store.owner_password_hash() is None
    form["setup_token"] = config["OWNER_SETUP_TOKEN"]
    assert client.post("/setup", data=form, base_url="https://localhost").status_code == 302
    saved = store.owner_password_hash()
    assert saved and saved != form["password"]
    assert not store.provision_owner("replacement-hash")
    assert store.owner_password_hash() == saved
    reopened = Store(url)
    config["OWNER_SETUP_TOKEN"] = None
    second = create_app(config, store=reopened, runner=FakeRunner()).test_client()
    assert second.get("/setup").location == "/login"
    page = second.get("/login", base_url="https://localhost")
    form["csrf"] = BeautifulSoup(page.data, "html.parser").select_one("input[name=csrf]")["value"]
    response = second.post(
        "/login",
        data={"csrf": form["csrf"], "name": "owner", "password": form["password"]},
        base_url="https://localhost",
    )
    assert response.status_code == 302
    assert second.get("/api/meta", base_url="https://localhost").status_code == 200


def test_search_reload_export_and_saved_conditions_never_start_acquisition(web, monkeypatch):
    app, store, runner = web

    def no_http(*args, **kwargs):
        raise AssertionError("A normal DB read attempted an external request")

    monkeypatch.setattr(Client, "_request", no_http)
    client = app.test_client()
    token = login(client)
    for _ in range(2):
        assert client.get("/", base_url="https://localhost").status_code == 200
        result = post(client, "/api/analyze", token, query()).get_json()
        assert result["summary"]["bets"]["単勝"]["return_rate"] == "150.0"
        assert len(result["entries"]) == 4
    response = post(client, "/api/export", token, query())
    rows = list(csv.reader(io.StringIO(response.data.decode("utf-8-sig"))))
    assert len(rows) == 5 and rows[1][6] == "600"
    assert (
        post(client, "/api/searches", token, {"name": "条件A", "query": query()}).status_code == 201
    )
    assert client.get("/api/searches", base_url="https://localhost").get_json()["searches"]
    assert client.get("/api/jobs", base_url="https://localhost").status_code == 200
    assert not runner.calls


def test_explicit_collection_button_registers_one_job_for_duplicate_requests(web):
    app, store, runner = web
    client = app.test_client()
    token = login(client)
    body = {"mode": "history", "years": [2025], "series_id": SERIES}
    first = post(client, "/api/jobs", token, body)
    second = post(client, "/api/jobs", token, body)
    assert first.status_code == second.status_code == 202
    assert first.get_json()["id"] == second.get_json()["id"]
    assert len(store.list_jobs()) == 1
    assert set(runner.calls) == {first.get_json()["id"]}


def test_saved_data_version_survives_correction_without_double_counting(web):
    app, store, runner = web
    client = app.test_client()
    token = login(client)
    post(client, "/api/searches", token, {"name": "訂正前", "query": query()})
    frozen = store.list_searches()[0]["query"]
    old = post(client, "/api/analyze", token, frozen).get_json()
    store.save_race(payload(800))
    current = post(client, "/api/analyze", token, query()).get_json()
    replay = post(client, "/api/analyze", token, frozen).get_json()
    assert current["summary"]["bets"]["単勝"]["return_rate"] == "200.0"
    assert replay["summary"]["bets"]["単勝"]["return_rate"] == "150.0"
    assert replay["data_version"] == old["data_version"] != current["data_version"]
    assert len(replay["entries"]) == len(current["entries"]) == 4
    doubled = query() | {"snapshot_ids": old["snapshot_ids"] + current["snapshot_ids"]}
    assert post(client, "/api/analyze", token, doubled).status_code == 400


def test_database_restart_keeps_raw_snapshots_results_and_saved_conditions(tmp_path):
    url = f"sqlite:///{tmp_path / 'persistent.sqlite'}"
    store = Store(url)
    store.initialize()
    first = store.save_race(payload())
    assert store.save_race(payload()) == first
    store.save_search("保存条件", query())
    body = b"<html>verified source</html>"
    page = Page(
        url="https://www.jra.go.jp/datafile/seiseki/replay/2025/001.html",
        body=body,
        raw_path="raw/sample.html",
        sha256=hashlib.sha256(body).hexdigest(),
        fetched_at=datetime.now(UTC).isoformat(),
        content_type="text/html",
        elapsed_seconds=0,
    )
    store.save_source(page)
    store.engine.dispose()
    reopened = Store(url)
    reopened.initialize()
    assert len(reopened.dataset(SERIES, "2016-01-01", "2025-12-31")["entries"]) == 4
    assert reopened.list_searches()[0]["name"] == "保存条件"
    assert reopened.cached_source(page.url, "GET")[1] == body
    with reopened.engine.connect() as conn:
        assert len(conn.execute(select(snapshots)).all()) == 1


def test_raw_quota_stops_new_writes_preserves_previous_success_and_deduplicates(tmp_path):
    body = b"<html>first</html>"
    store = Store("sqlite://", raw_limit=len(gzip.compress(body, mtime=0)) + 1)
    store.initialize()
    page = Page(
        url="https://www.jra.go.jp/datafile/seiseki/replay/2025/001.html",
        body=body,
        raw_path="raw/source.html",
        sha256=hashlib.sha256(body).hexdigest(),
        fetched_at="2025-01-01T00:00:00+00:00",
        content_type="text/html",
        elapsed_seconds=0,
    )
    store.save_source(page)
    store.save_source(page)
    newer = copy.deepcopy(page)
    newer.body = b"<html>different</html>"
    newer.sha256 = hashlib.sha256(newer.body).hexdigest()
    newer.fetched_at = "2025-01-02T00:00:00+00:00"
    with pytest.raises(CapacityExceeded):
        store.save_source(newer)
    assert store.cached_source(page.url, "GET")[1] == body
    with store.engine.connect() as conn:
        assert len(conn.execute(select(blobs)).all()) == 1


def test_db_raw_cache_restores_without_network_after_temporary_files_are_lost(tmp_path):
    body = b"<html>durable raw</html>"
    page = Page(
        url="https://www.jra.go.jp/datafile/seiseki/replay/2025/001.html",
        body=body,
        raw_path="raw/source.html",
        sha256=hashlib.sha256(body).hexdigest(),
        fetched_at="2025-01-01T00:00:00+00:00",
        content_type="text/html",
        elapsed_seconds=0,
    )
    store = Store("sqlite://")
    store.initialize()
    store.save_source(page)
    session = Session([])
    client = PersistentClient(tmp_path, store, lambda: None, session=session)
    restored = client.fetch(page.url)
    assert restored.body == body and restored.cached
    assert not session.calls


def test_job_cursor_and_fixed_plan_survive_reconnection_and_expired_lease(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'jobs.sqlite'}"
    store = Store(url)
    store.initialize()
    identity = store.register_job({"mode": "history", "years": [2025], "series_id": SERIES})
    assert store.claim_job(identity, "worker-one")
    store.heartbeat(
        identity,
        "worker-one",
        cursor=1,
        total=2,
        plan=json.dumps([{"archive_url": "one"}, {"archive_url": "two"}]),
    )
    assert not store.claim_job(identity, "worker-two")
    import keiba_web.store as module

    original = module.time.time()
    monkeypatch.setattr(module.time, "time", lambda: original + 121)
    reopened = Store(url)
    reopened.initialize()
    assert reopened.claim_job(identity, "worker-two")
    assert reopened.get_job(identity)["cursor"] == 1
    assert reopened.get_job(identity)["plan"][1]["archive_url"] == "two"


def test_missing_year_catalog_cannot_be_mistaken_for_complete_or_zero(web):
    _, store, _ = web
    data = store.dataset(SERIES, "2024-01-01", "2025-12-31")
    assert data["coverage"]["state"] == "partial"
    assert data["coverage"]["expected_races"] is None
    assert data["coverage"]["missing_catalog_years"] == [2024, 2025]
    store.save_catalog([], year=2024, through_date="2024-12-31")
    empty = store.dataset(SERIES, "2024-01-01", "2024-12-31")
    assert empty["coverage"]["state"] == "no_races_in_period"
    assert empty["coverage"]["expected_races"] == 0
    store.save_catalog([], year=2025, through_date="2025-11-01")
    assert store.dataset(SERIES, "2025-01-01", "2025-12-31")["coverage"]["state"] == "partial"
    store.save_catalog([], year=2025, through_date="2025-12-31")
    assert store.dataset(SERIES, "2024-01-01", "2025-12-31")["coverage"]["state"] == "complete"


def test_runner_failure_keeps_last_success_and_resumes_current_race_without_duplication(
    tmp_path, monkeypatch
):
    import keiba_web.collector as collector
    from keiba_acquisition.transport import AcquisitionError
    from keiba_web.store import encode

    store = Store(f"sqlite:///{tmp_path / 'runner.sqlite'}")
    store.initialize()
    plan = [
        {
            "archive_url": f"https://www.jra.go.jp/datafile/seiseki/replay/2025/00{n}.html",
            "race_date": f"2025-11-0{n}",
            "name_at_time": f"架空重賞{n}",
            "series_id": SERIES,
        }
        for n in (1, 2)
    ]
    identity = store.register_job({"mode": "history", "years": [2025], "series_id": SERIES})
    assert store.claim_job(identity, "first")
    store.heartbeat(identity, "first", plan=encode(plan), total=2)
    calls = []
    fail = True

    class Probe:
        def __init__(self, *args, **kwargs):
            self.records = []
            self.parsed = []

        def database_result(self, url, *args, **kwargs):
            calls.append(url)
            if url == plan[1]["archive_url"] and fail:
                raise AcquisitionError("http_error", "HTTP 429", stop=True)

    def make_payload(item, parsed):
        value = payload()
        value.update(race_id=item["archive_url"], race_date=item["race_date"])
        return value

    monkeypatch.setattr(collector, "Probe", Probe)
    monkeypatch.setattr(collector, "make_payload", make_payload)
    collector.Runner(store).run(identity, "first")
    failed = store.get_job(identity)
    assert failed["state"] == "blocked" and failed["cursor"] == 1
    assert store.dataset(SERIES, "2025-01-01", "2025-12-31")["coverage"]["imported_races"] == 1
    fail = False
    reopened = Store(f"sqlite:///{tmp_path / 'runner.sqlite'}")
    reopened.initialize()
    assert reopened.claim_job(identity, "second")
    collector.Runner(reopened).run(identity, "second")
    assert reopened.get_job(identity)["state"] == "completed"
    assert reopened.get_job(identity)["cursor"] == 2
    assert calls == [plan[0]["archive_url"], plan[1]["archive_url"], plan[1]["archive_url"]]
    assert reopened.dataset(SERIES, "2025-01-01", "2025-12-31")["coverage"]["imported_races"] == 2
