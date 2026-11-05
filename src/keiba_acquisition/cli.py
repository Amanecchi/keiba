"""One-command acquisition probe. Exit status never substitutes for full coverage."""

import argparse
import hashlib
import json
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

from . import __version__
from .html import (
    ParseError,
    discover_results,
    parse_entries,
    parse_odds,
    parse_profile,
    parse_result,
    related_links,
)
from .transport import AcquisitionError, Client, write_json


def years(value: str) -> list[int]:
    try:
        if ":" in value:
            first, last = map(int, value.split(":"))
            selected = list(range(first, last + 1))
        else:
            selected = list(dict.fromkeys(map(int, value.split(","))))
        if not selected or min(selected) < 1986 or max(selected) > date.today().year + 1:
            raise ValueError
        return selected
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "年度は2016,2021,2025または2016:2025と指定してください。"
        ) from error


class Probe:
    def __init__(self, client: Client, *, progress=None):
        self.client = client
        self.progress = progress or (lambda message: print(message, file=sys.stderr, flush=True))
        self.records = []
        self.parsed = []

    def acquire(self, url: str, kind: str, parser=None, *, fresh=False, context=""):
        self.progress(f"取得: {kind} {url}")
        record = {"kind": kind, "url": url, "context": context}
        self.records.append(record)
        try:
            page = self.client.fetch(url, fresh=fresh)
            record.update(page.metadata())
            record["status"] = "downloaded"
            if parser is None:
                return page, None
            result = parser(page.body, page.response_url or page.url)
            result["source"] = page.metadata()
            result["parser_version"] = __version__
            key = hashlib.sha256(f"{kind}|{page.url}|{context}".encode()).hexdigest()
            relative = Path("parsed") / f"{key}.json"
            write_json(self.client.output / relative, result)
            record.update(
                {
                    "status": "parsed" if not result.get("issues") else "partial",
                    "parsed_path": str(relative),
                    "issues": result.get("issues", []),
                }
            )
            self.parsed.append(result)
            return page, result
        except AcquisitionError as error:
            record.update({"status": "error", "error_kind": error.kind, "message": str(error)})
            if error.stop:
                raise
        except ParseError as error:
            record.update({"status": "error", "error_kind": "parse_error", "message": str(error)})
        return None, None

    def result(self, url: str, profile_limit: int):
        _, result = self.acquire(url, "result", parse_result)
        if result is None:
            return
        target_date = date.fromisoformat(result["race_date"]) if result["race_date"] else None
        if target_date is None:
            return
        selected = result["entries"][:profile_limit]
        for entry in selected:
            if entry["horse_url"]:
                self.acquire(
                    entry["horse_url"],
                    "profile",
                    lambda body, base: parse_profile(body, base, target_date),
                    context=target_date.isoformat(),
                )

    def upcoming(self, url: str, odds_url: str | None):
        # Landing pages can link to a JRADB card instead of containing the table.
        page, _ = self.acquire(url, "upcoming_landing", fresh=True)
        if page is None:
            return
        try:
            parse_entries(page.body, page.response_url or page.url)
            entry_url = url
        except ParseError:
            links = related_links(page.body, page.response_url or page.url, "accessD")
            if len(links) != 1:
                self.records[-1].update(
                    {
                        "status": "error",
                        "error_kind": "parse_error",
                        "message": "対象の出馬表リンクを一意に特定できません。対象出馬表URLで再検証してください。",
                    }
                )
                return
            entry_url = links[0]
        # Reuse the just-fetched landing page only inside this call; later runs
        # must request current information again.
        if entry_url == url:
            parsed = parse_entries(page.body, page.response_url or page.url)
            parsed["source"] = page.metadata()
            parsed["parser_version"] = __version__
            key = hashlib.sha256(f"entries|{page.url}".encode()).hexdigest()
            path = Path("parsed") / f"{key}.json"
            write_json(self.client.output / path, parsed)
            self.records.append(
                {
                    "kind": "entries",
                    **page.metadata(),
                    "status": "partial" if parsed["issues"] else "parsed",
                    "issues": parsed["issues"],
                    "parsed_path": str(path),
                }
            )
            self.parsed.append(parsed)
            entry_page = page
        else:
            entry_page, parsed = self.acquire(entry_url, "entries", parse_entries, fresh=True)
        if entry_page and parsed and parsed.get("status") == "parsed":
            base = entry_page.response_url or entry_url
            links = related_links(entry_page.body, base, "accessO", labels=("単勝", "複勝"))
            if not links:
                links = related_links(entry_page.body, base, "accessO")
            if odds_url or len(links) == 1:
                self.acquire(odds_url or links[0], "odds", parse_odds, fresh=True)
            else:
                self.records.append(
                    {
                        "kind": "odds",
                        "url": entry_url,
                        "status": "error",
                        "error_kind": "missing_odds_link",
                        "message": "単複オッズへのリンクを一意に特定できません。対象オッズURLで再検証してください。",
                    }
                )

    def run(self, args) -> tuple[dict, int]:
        started = time.monotonic()
        stopped = None
        discovery = []
        try:
            if args.result_url:
                for url in dict.fromkeys(args.result_url):
                    self.result(url, args.profiles_per_race)
            else:
                for year in args.years or [2016, 2021, 2025]:
                    url = f"https://www.jra.go.jp/datafile/seiseki/replay/{year}/jyusyo.html"
                    page, _ = self.acquire(url, "year_index")
                    if page is None:
                        continue
                    try:
                        links = discover_results(
                            page.body, page.response_url or page.url, args.race_name
                        )
                    except ParseError as error:
                        self.records[-1].update(
                            {"status": "error", "error_kind": "parse_error", "message": str(error)}
                        )
                        continue
                    discovery.append(
                        {
                            "year": year,
                            "matching_results": len(links),
                            "selected_results": min(len(links), args.limit_per_year),
                        }
                    )
                    for link in links[: args.limit_per_year]:
                        self.result(link["url"], args.profiles_per_race)
            if args.upcoming_url:
                self.upcoming(args.upcoming_url, args.odds_url)
            elif args.odds_url:
                self.acquire(args.odds_url, "odds", parse_odds, fresh=True)
        except AcquisitionError as error:
            stopped = error.kind
        except KeyboardInterrupt:
            stopped = "interrupted"

        failures = [record for record in self.records if record["status"] in {"error", "partial"}]
        results = [result for result in self.parsed if result["kind"] == "result"]
        profiles = [result for result in self.parsed if result["kind"] == "profile"]
        if stopped == "proxy_error":
            status, exit_code = "blocked_environment", 3
        elif stopped:
            status, exit_code = "stopped", 2
        elif failures:
            status, exit_code = "partial" if self.parsed else "failed", 2
        elif results:
            status, exit_code = "sample_passed", 0
        else:
            status, exit_code = "no_historical_sample", 2
        report = {
            "started_at": datetime.fromtimestamp(
                time.time() - (time.monotonic() - started), UTC
            ).isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "tool_version": __version__,
            "status": status,
            "stop_reason": stopped,
            "production_ready": False,
            "ten_year_coverage_verified": False,
            "feasibility_decision": "undetermined" if exit_code else "sample_only",
            "requested_scope": {
                "years": None if args.result_url else args.years or [2016, 2021, 2025],
                "race_name": args.race_name,
                "limit_per_year": args.limit_per_year,
                "profiles_per_race": args.profiles_per_race,
                "upcoming_requested": bool(args.upcoming_url),
                "odds_requested": bool(args.odds_url or args.upcoming_url),
            },
            "metrics": {
                "elapsed_seconds": round(time.monotonic() - started, 4),
                "http_attempts": self.client.attempts,
                "cache_hits": self.client.cache_hits,
                "received_bytes": self.client.received_bytes,
                "parsed_results": len(results),
                "parsed_profiles": len(profiles),
                "parsed_entries": sum(len(result["entries"]) for result in results),
            },
            "discovery": discovery,
            "resources": self.records,
            "limits": [
                "少量取得の検証用。全件網羅性・全条件・期待値計算の完成を意味しません。",
                "未取得・欠損・未公表を0や不的中へ変換しません。",
                "手動転記、PDF/OCR、他社データによる補完は行いません。",
                "馬リンクや必要列がない画面は不足として記録します。",
            ],
        }
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        write_json(self.client.output / "reports" / f"{stamp}.json", report)
        write_json(self.client.output / "latest_report.json", report)
        return report, exit_code


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="JRA公式HTMLの自動取得を少量検証します。")
    scope = cli.add_mutually_exclusive_group()
    scope.add_argument("--years", type=years, help="例: 2016,2021,2025 または 2016:2025")
    scope.add_argument(
        "--result-url", action="append", help="公式結果URL。年度一覧を経由しない追加検証用"
    )
    cli.add_argument(
        "--race-name",
        default="アルゼンチン共和国杯",
        help="年度一覧内のレース名部分一致。空文字で全対象",
    )
    cli.add_argument("--limit-per-year", type=int, default=1)
    cli.add_argument("--profiles-per-race", type=int, default=2)
    cli.add_argument("--upcoming-url", help="対象レースの公式出馬表または案内URL")
    cli.add_argument("--odds-url", help="必要に応じて公式単複オッズ画面を直接指定")
    cli.add_argument("--output", type=Path, default=Path("work/acquisition"))
    cli.add_argument("--interval", type=float, default=2.0, help="HTTP要求間の最小秒数。1秒以上")
    cli.add_argument("--timeout", type=float, default=15.0)
    cli.add_argument(
        "--refresh-history", action="store_true", help="過去ページもキャッシュを使わず再取得"
    )
    return cli


def main(argv=None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    if (
        args.interval < 1
        or args.timeout <= 0
        or args.limit_per_year < 1
        or args.profiles_per_race < 1
    ):
        cli.error("intervalは1以上、timeoutは正数、取得件数は1以上を指定してください。")
    client = Client(
        args.output,
        interval=args.interval,
        timeout=args.timeout,
        refresh_history=args.refresh_history,
    )
    report, code = Probe(client).run(args)
    print(
        json.dumps(
            {
                "status": report["status"],
                "report": str(args.output / "latest_report.json"),
                "metrics": report["metrics"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
