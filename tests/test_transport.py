import hashlib
import json

import pytest
import requests

from keiba_acquisition.transport import AcquisitionError, Client, official_url

URL = "https://www.jra.go.jp/datafile/seiseki/replay/2025/001.html"


class Response:
    def __init__(self, body=b"<html>test</html>", status=200, headers=None):
        self.body = body
        self.status_code = status
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, size):
        yield self.body


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []
        self.methods = []

    def get(self, url, **kwargs):
        self.methods.append("GET")
        return self._response(url, kwargs)

    def post(self, url, **kwargs):
        self.methods.append("POST")
        return self._response(url, kwargs)

    def _response(self, url, kwargs):
        self.calls.append((url, kwargs))
        assert kwargs["allow_redirects"] is False
        assert "verify" not in kwargs  # requests retains TLS verification.
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/test.html",
        "file:///tmp/test.html",
        "https://www.jra.go.jp/a.pdf",
        "https://www.jra.go.jp/datafile/seiseki/test.pdf",
        "https://www.jra.go.jp/dento/",
        "https://user:secret@www.jra.go.jp/JRADB/accessS.html",
        "https://www.jra.go.jp:bad/JRADB/accessS.html",
    ],
)
def test_only_official_target_html_is_accepted(url):
    with pytest.raises(AcquisitionError):
        official_url(url)


def test_hash_verified_cache_reuses_bytes_without_http(tmp_path):
    session = Session([Response()])
    client = Client(tmp_path, session=session)
    first = client.fetch(URL)
    second = client.fetch(URL)
    assert not first.cached and second.cached
    assert first.body == second.body
    assert first.sha256 == hashlib.sha256(first.body).hexdigest()
    assert len(session.calls) == 1
    assert client.cache_hits == 1
    saved = json.loads((tmp_path / first.raw_path).with_suffix(".json").read_text())
    assert saved["sha256"] == first.sha256


def test_corrupt_cache_is_reported(tmp_path):
    client = Client(tmp_path, session=Session([Response()]))
    first = client.fetch(URL)
    (tmp_path / first.raw_path).write_bytes(b"modified")
    with pytest.raises(AcquisitionError) as caught:
        client.fetch(URL)
    assert caught.value.kind == "cache_corrupt"


def test_current_pages_bypass_cache(tmp_path):
    session = Session([Response(b"first"), Response(b"second")])
    client = Client(tmp_path, session=session, sleep=lambda _: None)
    client.fetch(URL)
    second = client.fetch(URL, fresh=True)
    assert second.body == b"second" and not second.cached
    assert len(session.calls) == 2


def test_refetch_preserves_raw_metadata_and_old_report_references(tmp_path):
    session = Session([Response(b"first quote"), Response(b"second quote")])
    client = Client(tmp_path, session=session, sleep=lambda _: None)
    first = client.fetch(URL, fresh=True)
    original_meta = (tmp_path / first.raw_path).with_suffix(".json").read_bytes()
    second = client.fetch(URL, fresh=True)
    assert first.raw_path != second.raw_path
    assert (tmp_path / first.raw_path).read_bytes() == b"first quote"
    assert (tmp_path / first.raw_path).with_suffix(".json").read_bytes() == original_meta
    assert client.fetch(URL).body == b"second quote"
    assert len(session.calls) == 2


def test_legacy_cache_remains_readable_and_unchanged_after_refresh(tmp_path):
    client = Client(tmp_path, session=Session([Response(b"old")]))
    page = client.fetch(URL)
    key = hashlib.sha256(URL.encode()).hexdigest()
    legacy = tmp_path / "raw" / f"{key}.html"
    legacy.write_bytes(page.body)
    meta = {k: v for k, v in page.metadata().items() if k != "cached"}
    meta["raw_path"] = str(legacy.relative_to(tmp_path))
    legacy.with_suffix(".json").write_text(json.dumps(meta))
    (tmp_path / "cache" / f"{key}.json").unlink()
    updated = Client(tmp_path, session=Session([Response(b"new")]))
    assert updated.fetch(URL).body == b"old"
    updated.fetch(URL, fresh=True)
    assert legacy.read_bytes() == b"old"
    assert json.loads(legacy.with_suffix(".json").read_text()) == meta
    assert updated.fetch(URL).body == b"new"


def test_cache_cannot_point_outside_raw_storage(tmp_path):
    client = Client(tmp_path, session=Session([Response()]))
    client.fetch(URL)
    index = next((tmp_path / "cache").glob("*.json"))
    meta = json.loads(index.read_text())
    meta["raw_path"] = "../outside.html"
    index.write_text(json.dumps(meta))
    with pytest.raises(AcquisitionError) as caught:
        client.fetch(URL)
    assert caught.value.kind == "cache_corrupt"


def test_failed_refresh_keeps_previous_snapshot_available(tmp_path):
    client = Client(
        tmp_path,
        session=Session([Response(b"good quote"), Response(status=403)]),
        sleep=lambda _: None,
    )
    previous = client.fetch(URL, fresh=True)
    with pytest.raises(AcquisitionError):
        client.fetch(URL, fresh=True)
    cached = client.fetch(URL)
    assert cached.raw_path == previous.raw_path and cached.body == previous.body
    assert (tmp_path / previous.raw_path).with_suffix(".json").exists()


@pytest.mark.parametrize("status", [403, 429])
def test_blocked_request_is_not_retried_or_cached(tmp_path, status):
    session = Session([Response(status=status)])
    client = Client(tmp_path, session=session)
    with pytest.raises(AcquisitionError) as caught:
        client.fetch(URL)
    assert caught.value.stop
    assert caught.value.kind == "http_blocked"
    assert len(session.calls) == 1
    assert not list(tmp_path.rglob("*.html"))


def test_proxy_failure_is_distinct_from_origin_refusal(tmp_path):
    client = Client(tmp_path, session=Session([requests.exceptions.ProxyError("403 CONNECT")]))
    with pytest.raises(AcquisitionError) as caught:
        client.fetch(URL)
    assert caught.value.kind == "proxy_error"
    assert "JRAへの到達は確認できません" in str(caught.value)


def test_redirect_is_checked_before_requesting_external_host(tmp_path):
    session = Session([Response(status=302, headers={"Location": "https://example.com/data"})])
    with pytest.raises(AcquisitionError) as caught:
        Client(tmp_path, session=session).fetch(URL)
    assert caught.value.kind == "unsupported_url"
    assert len(session.calls) == 1


def test_transient_failure_retries_with_delay_and_bounded_attempts(tmp_path):
    session = Session([Response(status=503), Response()])
    delays = []
    client = Client(tmp_path, session=session, clock=lambda: 0, sleep=delays.append)
    client.fetch(URL)
    assert client.attempts == 2
    assert delays == [2, 2.0]


def test_request_interval_covers_successive_pages(tmp_path):
    session = Session([Response(), Response()])
    delays = []
    client = Client(tmp_path, session=session, clock=lambda: 0, sleep=delays.append)
    client.fetch(URL)
    client.fetch(URL.replace("001", "002"))
    assert delays == [2.0]


def test_non_html_response_is_not_saved(tmp_path):
    client = Client(
        tmp_path, session=Session([Response(headers={"Content-Type": "application/pdf"})])
    )
    with pytest.raises(AcquisitionError) as caught:
        client.fetch(URL)
    assert caught.value.kind == "content_type"
    assert not list(tmp_path.rglob("*.html"))


def test_same_site_redirect_records_the_actual_response_url(tmp_path):
    target = "https://www.jra.go.jp/JRADB/accessS.html?CNAME=sample-race%2Ftoken"
    session = Session([Response(status=302, headers={"Location": target}), Response()])
    page = Client(tmp_path, session=session, sleep=lambda _: None).fetch(URL)
    assert page.url == URL
    assert page.response_url == target
    cached = Client(tmp_path, session=Session([])).fetch(URL)
    assert cached.response_url == target


def test_public_cname_form_preserves_method_and_has_separate_cache(tmp_path):
    target = "https://www.jra.go.jp/JRADB/accessS.html?CNAME=public/token"
    session = Session([Response(b"GET"), Response(b"POST")])
    client = Client(tmp_path, session=session, sleep=lambda _: None)
    get = client.fetch(target)
    post = client.fetch(target, post_cname=True)
    assert session.methods == ["GET", "POST"]
    assert session.calls[1][0] == "https://www.jra.go.jp/JRADB/accessS.html"
    assert session.calls[1][1]["data"] == {"cname": "public/token"}
    assert post.request_method == "POST" and get.raw_path != post.raw_path
    assert client.fetch(target, post_cname=True).body == b"POST"
    assert len(session.calls) == 2


@pytest.mark.parametrize("url", [URL, "https://www.jra.go.jp/JRADB/accessS.html?password=secret"])
def test_only_public_cname_form_is_supported(tmp_path, url):
    session = Session([])
    with pytest.raises(AcquisitionError) as caught:
        Client(tmp_path, session=session).fetch(url, post_cname=True)
    assert caught.value.kind == "unsupported_form"
    assert not session.calls


def test_form_denial_stops_without_get_fallback(tmp_path):
    session = Session([Response(status=403)])
    with pytest.raises(AcquisitionError) as caught:
        Client(tmp_path, session=session).fetch(
            "https://www.jra.go.jp/JRADB/accessS.html?CNAME=public/token", post_cname=True
        )
    assert caught.value.stop and session.methods == ["POST"]
