"""Explicit, resumable collection jobs. Ordinary reads never construct this client."""

import hashlib
import re
import tempfile
import threading
import uuid
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from keiba_acquisition.catalog import parse_catalog
from keiba_acquisition.cli import Probe
from keiba_acquisition.html import ParseError, clean
from keiba_acquisition.transport import (
    AcquisitionError,
    Client,
    atomic_write,
    official_url,
    write_json,
)

from .store import CapacityExceeded, JobBusy, encode


def source_ref(source):
    return {
        key: source[key]
        for key in ("url", "sha256", "fetched_at", "request_method")
        if key in source
    }


def make_payload(item, parsed):
    result = next((p for p in parsed if p["kind"] == "result"), None)
    if result is None or any(
        result[field] != item[field] for field in ("race_date", "venue", "distance_m")
    ):
        raise ParseError("結果と年度一覧の日付・開催場・距離が一致しません。")
    profiles = {
        p["horse_id"]: p
        for p in parsed
        if p["kind"] == "profile" and p["target_date"] == result["race_date"]
    }
    features = {
        p["horse_id"]: p
        for p in parsed
        if p["kind"] == "historical_features" and p["target_date"] == result["race_date"]
    }
    ledger = {p["horse_number"]: p for p in result["settlement"]["entries"]}
    official_key = ":".join(
        str(result[key])
        for key in ("race_date", "venue", "meeting_number", "meeting_day", "race_number")
    )
    race_id = "jra:" + hashlib.sha256(official_url(item["archive_url"]).encode()).hexdigest()
    entries = []
    for entry in result["entries"]:
        horse = entry.get("horse_id")
        settlement = ledger[entry["horse_number"]]
        if settlement["outcome"] == "unknown":
            raise ParseError("結果が確定した出走状態を確認できません。")
        if settlement["outcome"] != "nonstarter" and not horse:
            raise ParseError("実出走の馬IDを確認できません。")
        profile = profiles.get(horse, {})
        feature = features.get(horse, {})
        previous = profile.get("previous_run") or {}
        sex_age = re.fullmatch(r"(牡|牝|せん|セ|騸)(\d+)", clean(entry.get("sex_age") or ""))
        previous_distance = re.fullmatch(
            r"(?:芝|ダート|ダ)(\d{3,4})", clean(previous.get("distance") or "")
        )
        previous_state = (
            "dnf"
            if "中止" in previous.get("finish_text", "")
            else ("disqualified" if "失格" in previous.get("finish_text", "") else None)
        )
        values = {
            "frame": entry.get("frame"),
            "running_style": feature.get("running_style", {}).get("value"),
            "jockey": entry.get("jockey"),
            "sire": profile.get("sire") or entry.get("sire"),
            "sex": sex_age[1] if sex_age else None,
            "age": int(sex_age[2]) if sex_age else None,
            "affiliation": entry.get("affiliation"),
            "previous_class": (feature.get("previous_class") or {}).get("code"),
            "previous_distance": int(previous_distance[1]) if previous_distance else None,
            "interval_days": (
                date.fromisoformat(result["race_date"]) - date.fromisoformat(previous["race_date"])
            ).days
            if previous.get("race_date")
            else None,
            "popularity": entry.get("popularity"),
            "previous_race": previous.get("race_name"),
            "previous_finish": previous.get("finish"),
            "previous_finish_status": previous_state,
            "win_odds": entry.get("win_odds"),
        }
        entries.append(
            {
                "id": f"{race_id}:{entry['horse_number']}",
                "race_id": race_id,
                "race_date": result["race_date"],
                "horse_id": horse,
                "horse_name": entry["horse_name"],
                "horse_number": entry["horse_number"],
                "finish": entry.get("finish"),
                "finish_text": entry.get("finish_text"),
                "outcome": settlement["outcome"],
                "features": values,
                "affiliation_evidence": entry.get("affiliation_evidence"),
                "bets": settlement["bets"],
                "source": source_ref(result["source"]),
            }
        )
    return {
        "schema": "historical-analysis-v1",
        "race_id": race_id,
        "official_race_key": official_key,
        "series_id": item["series_id"],
        "race_date": result["race_date"],
        "catalog": item,
        "entries": entries,
        "source": source_ref(result["source"]),
        "settlement_issues": result["settlement"]["issues"],
        "official_notes": result["settlement"]["official_notes"],
        "source_refs": [
            source_ref(p["source"]) | {"kind": p["kind"]} for p in parsed if p.get("source")
        ],
        "feature_evidence": [p for p in parsed if p["kind"] == "historical_features"],
    }


class PersistentClient(Client):
    def __init__(self, output, store, heartbeat, **kwargs):
        super().__init__(output, **kwargs)
        self.store, self.heartbeat = store, heartbeat

    def fetch(self, url, *, fresh=False, post_cname=False):
        url = official_url(url)
        method = "POST" if post_cname else "GET"
        if not fresh and not self.refresh_history:
            saved = self.store.cached_source(url, method)
            if saved:
                meta, body = saved
                raw = (self.output / meta["raw_path"]).resolve()
                if not raw.is_relative_to((self.output / "raw").resolve()):
                    raise AcquisitionError("cache_corrupt", "保存先の参照が不正です。")
                atomic_write(raw, body)
                key = hashlib.sha256((f"POST|{url}" if post_cname else url).encode()).hexdigest()
                write_json(self.output / "cache" / f"{key}.json", meta)
        page = super().fetch(url, fresh=fresh, post_cname=post_cname)
        if not page.cached:
            self.store.save_source(page)
        self.heartbeat()
        return page


class Runner:
    def __init__(self, store):
        self.store = store

    def start(self, identity):
        owner = uuid.uuid4().hex
        if not self.store.claim_job(identity, owner):
            return False
        threading.Thread(target=self.run, args=(identity, owner), daemon=True).start()
        return True

    def run(self, identity, owner):
        job = self.store.get_job(identity)
        params = job["params"]
        try:
            with tempfile.TemporaryDirectory(prefix="keiba-job-") as temporary:
                client = PersistentClient(
                    Path(temporary),
                    self.store,
                    lambda: self.store.heartbeat(identity, owner),
                    refresh_history=params["mode"] == "refresh",
                )
                targets = job["plan"]
                if not targets:
                    for year in params["years"]:
                        page = client.fetch(
                            f"https://www.jra.go.jp/datafile/seiseki/replay/{year}/jyusyo.html",
                            fresh=params["mode"] == "latest",
                        )
                        value = parse_catalog(
                            page.body,
                            page.response_url or page.url,
                            as_of=datetime.now(ZoneInfo("Asia/Tokyo")).date(),
                        )
                        cutoff = datetime.now(ZoneInfo("Asia/Tokyo")).date()
                        if value["pending"]:
                            from datetime import timedelta

                            cutoff -= timedelta(days=1)
                        self.store.save_catalog(
                            value["races"],
                            year=value["year"],
                            through_date=min(cutoff.isoformat(), f"{year}-12-31"),
                            source_meta=source_ref(page.metadata()),
                        )
                        targets.extend(
                            r
                            for r in value["races"]
                            if params.get("series_id") in {None, r["series_id"]}
                        )
                    targets.sort(key=lambda item: (item["race_date"], item["archive_url"]))
                    self.store.heartbeat(identity, owner, total=len(targets), plan=encode(targets))
                for index, item in enumerate(targets):
                    if index < job["cursor"]:
                        continue
                    previous = self.store.current_race(item)
                    if previous and params["mode"] == "history":
                        self.store.heartbeat(identity, owner, cursor=index + 1)
                        continue
                    if previous and params["mode"] == "latest":
                        age = (
                            datetime.now(ZoneInfo("Asia/Tokyo")).date()
                            - date.fromisoformat(item["race_date"])
                        ).days
                        if age > 14:
                            self.store.heartbeat(identity, owner, cursor=index + 1)
                            continue
                    self.store.heartbeat(
                        identity,
                        owner,
                        message=f"{item['race_date']} {item['name_at_time']} を収集中",
                    )
                    probe = Probe(client, progress=lambda _: None)
                    probe.database_result(
                        item["archive_url"],
                        24,
                        trainer_history=True,
                        history_features=True,
                        fresh_result=params["mode"] == "latest",
                    )
                    failed = [r for r in probe.records if r["status"] == "error"]
                    if failed:
                        raise AcquisitionError(
                            "resource_failed",
                            "一部のデータを確認できません。前の成功データを保持して停止しました。",
                        )
                    self.store.save_race(make_payload(item, probe.parsed))
                    self.store.heartbeat(identity, owner, cursor=index + 1)
                self.store.finish_job(identity, owner, "completed", "収集が完了しました。")
        except (AcquisitionError, ParseError, CapacityExceeded) as error:
            self.store.finish_job(
                identity,
                owner,
                "blocked" if getattr(error, "stop", False) else "failed",
                str(error),
            )
        except JobBusy:
            return
        except Exception:
            # Operational errors must not expose connection strings or credentials in the UI.
            self.store.finish_job(
                identity,
                owner,
                "failed",
                "収集を完了できませんでした。保存済みデータは保持しています。",
            )
