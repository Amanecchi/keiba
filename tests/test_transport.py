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

    def get(self, url, **kwargs):
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
