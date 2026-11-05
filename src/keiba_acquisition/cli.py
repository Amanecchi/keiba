"""One-command acquisition probe. Exit status never substitutes for full coverage."""

import argparse
import hashlib
import json
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .database import (
    check_identity,
    checked_result,
    merge_final_odds,
    month_link,
    odds_link,
    race_reference,
    result_link,
    search_link,
)
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

    def acquire(
        self, url: str, kind: str, parser=None, *, fresh=False, context="", post_cname=False
    ):
        self.progress(f"取得: {kind} {url}")
        record = {"kind": kind, "url": url, "context": context}
        self.records.append(record)
        try:
            page = self.client.fetch(url, fresh=fresh, post_cname=post_cname)
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

    def result(self, url: str, profile_limit: int, *, reference=None, final_odds=False):
        parse = (
            parse_result
            if reference is None
            else (lambda body, base: checked_result(body, base, reference))
        )
        page, result = self.acquire(url, "result", parse)
        if result is None:
            return
        record = self.records[-1]
        if final_odds:
            try:
                target = odds_link(page.body, page.response_url or page.url)
                _, odds = self.acquire(target, "historical_odds", parse_odds, post_cname=True)
                if odds is not None:
                    merge_final_odds(result, odds)
                    write_json(self.client.output / record["parsed_path"], result)
                    record.update(
                        status="partial" if result["issues"] else "parsed", issues=result["issues"]
                    )
            except ParseError as error:
                self.records.append(
                    {
                        "kind": "historical_odds_validation",
                        "url": url,
                        "status": "error",
                        "error_kind": "parse_error",
                        "message": str(error),
                    }
                )
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

    def database_result(self, archive_url: str, profile_limit: int):
        page, reference = self.acquire(archive_url, "race_reference", race_reference)
        if reference is None:
            return
        try:
            search_url = search_link(page.body, page.response_url or page.url)
            search, _ = self.acquire(search_url, "database_search", post_cname=True)
            if search is None:
                return
            month_url = month_link(
                search.body, search.response_url or search.url, reference["race_date"]
            )
            month, _ = self.acquire(month_url, "database_month", post_cname=True)
            if month is None:
                return
            target = result_link(month.body, month.response_url or month.url, reference)
            self.result(target, profile_limit, reference=reference, final_odds=True)
        except ParseError as error:
            self.records.append(
                {
                    "kind": "database_discovery",
                    "url": archive_url,
                    "status": "error",
                    "error_kind": "parse_error",
                    "message": str(error),
                }
            )

    def upcoming(self, url: str, odds_url: str | None):
        # Landing pages can link to a JRADB card instead of containing the table.
        page, _ = self.acquire(url, "upcoming_landing", fresh=True)
        if page is None:
            return
        try:
            initial = parse_entries(page.body, page.response_url or page.url)
            entry_url = url
            if initial["status"] != "parsed":
                self.records[-1]["publication_state"] = initial["status"]
                links = related_links(page.body, page.response_url or page.url, "accessD")
                detailed = [
                    link
                    for link in links
                    if parse_qs(urlsplit(link).query)["CNAME"][0].startswith("pw01dde")
                ]
                # A dated detail link can already be available while the article
                # still displays its publication notice. Never choose a menu.
                if len(detailed) == 1:
                    entry_url = detailed[0]
                elif len(detailed) > 1:
                    raise ParseError("対象の詳細出馬表が複数あります。")
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
            # The global odds menu is not an odds page for this race.
            links = [
                link
                for link in links
                if not parse_qs(urlsplit(link).query)["CNAME"][0].startswith("pw15oli")
            ]
            if odds_url or len(links) == 1:

                def current_odds(body, base):
                    value = parse_odds(body, base)
                    if value["status"] == "parsed":
                        check_identity(value, parsed)
                        if value["odds_state"] != "live":
                            raise ParseError(
                                "過去の最終オッズを今回の発売中オッズとして採用しません。"
                            )
                    return value

                target = odds_url or links[0]
                self.acquire(
                    target,
                    "odds",
                    current_odds,
                    fresh=True,
                    post_cname=urlsplit(target).path == "/JRADB/accessO.html",
                )
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
                    self.result(url, args.profiles_per_race, final_odds=args.database)
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
                        if args.database:
                            self.database_result(link["url"], args.profiles_per_race)
                        else:
                            self.result(link["url"], args.profiles_per_race)
            if args.upcoming_url:
                self.upcoming(args.upcoming_url, args.odds_url)
            elif args.odds_url:
                self.acquire(
                    args.odds_url,
                    "odds",
                    parse_odds,
                    fresh=True,
                    post_cname=urlsplit(args.odds_url).path == "/JRADB/accessO.html",
                )
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
                "database_requested": args.database,
                "historical_odds_requested": args.database,
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
                "parsed_historical_odds": sum(
                    r["kind"] == "odds" and r.get("odds_state") == "final" for r in self.parsed
                ),
                "parsed_current_entries": sum(
                    len(r["entries"]) for r in self.parsed if r["kind"] == "entries"
                ),
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
    cli.add_argument(
        "--database",
        action="store_true",
        help="年度一覧の保存版を開催キーでJRADB詳細結果・最終単複オッズへ対応付けて検証",
    )
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
