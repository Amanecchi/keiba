"""Serial official-site HTTP acquisition with resumable, verified raw storage."""

import hashlib
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from . import __version__

ALLOWED_HOSTS = {"www.jra.go.jp", "jra.go.jp"}
RETRY_STATUSES = {500, 502, 503, 504}
MAX_BYTES = 8 * 1024 * 1024


class AcquisitionError(Exception):
    def __init__(self, kind: str, message: str, *, stop: bool = False):
        super().__init__(message)
        self.kind = kind
        self.stop = stop


def official_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise AcquisitionError("unsupported_url", "URLの形式を認識できません。") from error
    if (
        parts.scheme not in {"https", "http"}
        or parts.hostname not in ALLOWED_HOSTS
        or parts.username
        or parts.password
        or port not in {None, 80, 443}
        or not parts.path.startswith(("/datafile/seiseki/", "/JRADB/", "/keiba/race/"))
        or parts.path.lower().endswith((".pdf", ".zip"))
    ):
        raise AcquisitionError("unsupported_url", "JRA公式の対象HTMLページ以外は取得しません。")
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit(("https", parts.hostname, parts.path, query, ""))


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, data: dict) -> None:
    atomic_write(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode())


@dataclass
class Page:
    url: str
    body: bytes
    raw_path: str
    sha256: str
    fetched_at: str
    content_type: str
    elapsed_seconds: float
    response_url: str | None = None
    cached: bool = False
    request_method: str = "GET"

    def metadata(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k != "body"}


class Client:
    def __init__(
        self,
        output: Path,
        *,
        interval: float = 2.0,
        timeout: float = 15.0,
        retries: int = 1,
        refresh_history: bool = False,
        session=None,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        self.output = output
        self.interval = interval
        self.timeout = timeout
        self.retries = retries
        self.refresh_history = refresh_history
        self.session = session or requests.Session()
        self.session.headers.update(
            {"User-Agent": f"KeibaLocalAcquisition/{__version__} (personal research)"}
        )
        self.clock, self.sleep = clock, sleep
        self.last_request = None
        self.attempts = 0
        self.cache_hits = 0
        self.received_bytes = 0

    def _request(self, url: str, *, data: dict | None = None):
        if self.last_request is not None:
            self.sleep(max(0.0, self.interval - (self.clock() - self.last_request)))
        self.last_request = self.clock()
        self.attempts += 1
        try:
            request = self.session.get if data is None else self.session.post
            kwargs = {} if data is None else {"data": data}
            return request(url, timeout=self.timeout, allow_redirects=False, stream=True, **kwargs)
        except requests.exceptions.ProxyError as error:
            raise AcquisitionError(
                "proxy_error",
                "接続プロキシで失敗しました。JRAへの到達は確認できません。",
                stop=True,
            ) from error
        except requests.exceptions.SSLError as error:
            raise AcquisitionError(
                "tls_error", "TLS証明書の検証に失敗しました。", stop=True
            ) from error
        except requests.exceptions.Timeout as error:
            raise AcquisitionError("timeout", "HTTP取得がタイムアウトしました。") from error
        except requests.exceptions.RequestException as error:
            raise AcquisitionError("network_error", "HTTP接続に失敗しました。") from error

    def fetch(self, url: str, *, fresh: bool = False, post_cname: bool = False) -> Page:
        from urllib.parse import urljoin

        url = official_url(url)
        data = None
        current_url = url
        if post_cname:
            parts = urlsplit(url)
            query = parse_qs(parts.query)
            if (
                parts.path not in {f"/JRADB/access{name}.html" for name in ("S", "D", "O", "U")}
                or set(query) != {"CNAME"}
                or len(query["CNAME"]) != 1
            ):
                raise AcquisitionError("unsupported_form", "公開画面のCNAME送信だけに対応します。")
            data = {"cname": query["CNAME"][0]}
            current_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        method = "POST" if post_cname else "GET"
        key = hashlib.sha256((f"POST|{url}" if post_cname else url).encode()).hexdigest()
        raw = self.output / "raw" / f"{key}.html"
        metadata = raw.with_suffix(".json")
        if not (fresh or self.refresh_history) and raw.exists() and metadata.exists():
            try:
                meta = json.loads(metadata.read_text(encoding="utf-8"))
                body = raw.read_bytes()
                if (
                    meta["url"] != url
                    or meta.get("request_method", "GET") != method
                    or hashlib.sha256(body).hexdigest() != meta["sha256"]
                ):
                    raise ValueError("cache identity mismatch")
                page = Page(body=body, **meta)
            except (ValueError, KeyError, TypeError) as error:
                raise AcquisitionError(
                    "cache_corrupt", "保存原本とハッシュが一致しません。"
                ) from error
            self.cache_hits += 1
            page.cached = True
            return page

        started = self.clock()
        redirects = 0
        attempts = 0
        while True:
            try:
                response = self._request(current_url, data=data)
            except AcquisitionError as error:
                if error.kind in {"timeout", "network_error"} and attempts < self.retries:
                    attempts += 1
                    self.sleep(2**attempts)
                    continue
                raise
            with response:
                status = response.status_code
                if status in {301, 302, 303, 307, 308}:
                    redirects += 1
                    if redirects > 5 or not response.headers.get("Location"):
                        raise AcquisitionError("redirect_error", "転送先を確定できません。")
                    # Validate BEFORE sending a request to the redirect target.
                    current_url = official_url(urljoin(current_url, response.headers["Location"]))
                    if status in {301, 302, 303}:
                        data = None
                    continue
                if status in {403, 429}:
                    raise AcquisitionError(
                        "http_blocked",
                        f"HTTP {status}。自動再試行せず取得を停止します。",
                        stop=True,
                    )
                if status in RETRY_STATUSES and attempts < self.retries:
                    attempts += 1
                    self.sleep(2**attempts)
                    continue
                if status != 200:
                    raise AcquisitionError("http_error", f"HTTP {status}。原本は正常保存しません。")
                content_type = response.headers.get("Content-Type", "")
                if not any(
                    t in content_type.lower() for t in ("text/html", "application/xhtml+xml")
                ):
                    raise AcquisitionError("content_type", "応答は対象のHTML形式ではありません。")
                chunks, length = [], 0
                try:
                    for chunk in response.iter_content(65536):
                        length += len(chunk)
                        if length > MAX_BYTES:
                            raise AcquisitionError(
                                "response_too_large", "HTMLのサイズ上限を超えました。"
                            )
                        chunks.append(chunk)
                except requests.exceptions.RequestException as error:
                    raise AcquisitionError(
                        "network_error", "応答本文の受信に失敗しました。"
                    ) from error
                body = b"".join(chunks)
                self.received_bytes += length
                page = Page(
                    url=url,
                    body=body,
                    raw_path=str(raw.relative_to(self.output)),
                    sha256=hashlib.sha256(body).hexdigest(),
                    fetched_at=datetime.now(UTC).isoformat(),
                    content_type=content_type,
                    elapsed_seconds=round(self.clock() - started, 4),
                    response_url=current_url,
                    request_method=method,
                )
                atomic_write(raw, body)
                write_json(metadata, {k: v for k, v in page.metadata().items() if k != "cached"})
                return page
