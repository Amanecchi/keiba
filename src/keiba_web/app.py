"""Owner-only web UI. Query routes use SQL only; collection requires an explicit POST."""

import csv
import hmac
import io
import os
import re
import secrets
import threading
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from keiba_acquisition.catalog import load_registry

from .analysis import FIELDS, InvalidQuery, analyze, validate_filters
from .collector import Runner
from .store import JobBusy, Store


def create_app(config=None, *, store=None, runner=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("KEIBA_SESSION_SECRET"),
        OWNER_NAME=os.environ.get("KEIBA_OWNER_NAME", "owner"),
        OWNER_PASSWORD_HASH=os.environ.get("KEIBA_OWNER_PASSWORD_HASH"),
        OWNER_SETUP_TOKEN=os.environ.get("KEIBA_OWNER_SETUP_TOKEN"),
        MAX_CONTENT_LENGTH=256_000,
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
    )
    app.config.update(config or {})
    if not app.config.get("SECRET_KEY") or len(app.config["SECRET_KEY"]) < 32:
        raise RuntimeError("ログイン用の秘密設定が必要です。")
    if store is None:
        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url.startswith(("postgresql://", "postgres://", "postgresql+psycopg://")):
            raise RuntimeError("本番用の永続PostgreSQLの設定が必要です。")
        database_url = re.sub(r"^postgres(?:ql)?://", "postgresql+psycopg://", database_url)
        store = Store(database_url)
    store.initialize()
    if (
        not (app.config.get("OWNER_PASSWORD_HASH") or store.owner_password_hash())
        and len(app.config.get("OWNER_SETUP_TOKEN") or "") < 32
    ):
        raise RuntimeError("本人用ログインの初期設定が必要です。")
    runner = runner or Runner(store)
    app.extensions.update(keiba_store=store, keiba_runner=runner)
    if not app.testing:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)
        hosts = os.environ.get("KEIBA_ALLOWED_HOSTS") or os.environ.get("RENDER_EXTERNAL_HOSTNAME")
        if not hosts:
            raise RuntimeError("Webの接続先ホスト名の設定が必要です。")
        app.config["TRUSTED_HOSTS"] = [value.strip() for value in hosts.split(",")]
    registry = load_registry()
    series_ids = {item["id"] for item in registry["series"]}
    failures, auth_lock = {}, threading.Lock()

    @app.before_request
    def protect():
        if request.endpoint == "health":
            return None
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        if request.endpoint not in {"login", "setup", "static"} and not session.get("owner"):
            return (
                (jsonify(error="ログインしてください。"), 401)
                if request.path.startswith("/api/")
                else redirect(url_for("login"))
            )
        if request.method in {"POST", "PUT", "DELETE"}:
            token = request.headers.get("X-CSRF-Token") or request.form.get("csrf", "")
            if not hmac.compare_digest(token.encode(), session["csrf"].encode()):
                return jsonify(error="画面を再読み込みして操作してください。"), 403
            origin = request.headers.get("Origin")
            if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
                return jsonify(error="操作元を確認できません。"), 403

    @app.after_request
    def headers(response):
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "same-origin",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'",
            }
        )
        return response

    @app.errorhandler(InvalidQuery)
    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(JobBusy)
    def busy(error):
        return jsonify(error=str(error)), 409

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    def owner_hash():
        return app.config.get("OWNER_PASSWORD_HASH") or store.owner_password_hash()

    def throttle_login():
        address = request.remote_addr or "unknown"
        with auth_lock:
            recent = [v for v in failures.get(address, []) if v > time.monotonic() - 60]
            if len(recent) >= 5:
                return False
            failures[address] = recent + [time.monotonic()]
        return True

    def owner_session():
        session.clear()
        session.update(owner=True, csrf=secrets.token_urlsafe(32))
        with auth_lock:
            failures.pop(request.remote_addr or "unknown", None)

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if owner_hash():
            return redirect(url_for("login"))
        error = None
        if request.method == "POST":
            if not throttle_login():
                return render_template("setup.html", error="時間を空けてお試しください。"), 429
            token = request.form.get("setup_token", "")
            password = request.form.get("password", "")
            if not hmac.compare_digest(token.encode(), app.config["OWNER_SETUP_TOKEN"].encode()):
                error = "初期設定コードを確認してください。"
            elif not 12 <= len(password) <= 128 or password != request.form.get("confirmation"):
                error = "12〜128文字の同じパスワードを2回入力してください。"
            elif store.provision_owner(generate_password_hash(password)):
                owner_session()
                return redirect(url_for("index"))
            else:
                return redirect(url_for("login"))
        return render_template("setup.html", error=error), 400 if error else 200

    @app.route("/login", methods=["GET", "POST"])
    def login():
        password_hash = owner_hash()
        if not password_hash:
            return redirect(url_for("setup"))
        error = None
        if request.method == "POST":
            if not throttle_login():
                return render_template("login.html", error="時間を空けてお試しください。"), 429
            password = request.form.get("password", "")
            valid = len(password) <= 256 and check_password_hash(password_hash, password)
            if valid and hmac.compare_digest(
                request.form.get("name", "").encode(), app.config["OWNER_NAME"].encode()
            ):
                owner_session()
                return redirect(url_for("index"))
            error = "ログイン情報を確認してください。"
        return render_template("login.html", error=error), 401 if error else 200

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def index():
        return render_template("index.html", fields=FIELDS)

    def query(value):
        if not isinstance(value, dict) or value.get("series_id") not in series_ids:
            raise InvalidQuery("対象レースを選択してください。")
        try:
            start, end = (
                date.fromisoformat(value.get("start_date", "")),
                date.fromisoformat(value.get("end_date", "")),
            )
        except (ValueError, TypeError) as error:
            raise InvalidQuery("対象期間を確認してください。") from error
        if start > end:
            raise InvalidQuery("対象期間を確認してください。")
        filters = validate_filters(value.get("filters", []))
        grouping = value.get("group_by") or None
        if grouping is not None and grouping not in FIELDS:
            raise InvalidQuery("分類軸を確認してください。")
        ids = value.get("snapshot_ids")
        if ids is not None and (
            not isinstance(ids, list)
            or len(ids) > 500
            or any(not isinstance(i, str) or not re.fullmatch(r"[a-f0-9]{64}", i) for i in ids)
            or len(set(ids)) != len(ids)
        ):
            raise InvalidQuery("保存時のデータ版が不正です。")
        return {
            "series_id": value["series_id"],
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "filters": filters,
            "group_by": grouping,
            **({"snapshot_ids": ids} if ids is not None else {}),
        }

    def run_query(value):
        q = query(value)
        dataset = store.dataset(
            q["series_id"], q["start_date"], q["end_date"], q.get("snapshot_ids")
        )
        result = analyze(dataset["entries"], q["filters"], q["group_by"])
        result.update(
            coverage=dataset["coverage"],
            data_version=dataset["version"],
            snapshot_ids=dataset["snapshot_ids"],
            query=q,
            saved_data="snapshot_ids" in q,
            unknown_features={
                field: sum(e["features"].get(field) is None for e in dataset["entries"])
                for field in FIELDS
            },
            choices={
                field: sorted(
                    {
                        str(e["features"][field])
                        for e in dataset["entries"]
                        if e["features"].get(field) is not None
                    }
                )[:200]
                for field in FIELDS
            },
        )
        return result

    @app.get("/api/meta")
    def meta():
        counts = store.series_counts()
        return {
            "current_year": datetime.now(ZoneInfo("Asia/Tokyo")).year,
            "series": [
                {
                    "id": s["id"],
                    "name": s["display_name"],
                    "imported": counts.get(s["id"], 0),
                    "aliases": s["aliases"],
                }
                for s in registry["series"]
            ],
            "fields": {key: {"label": value[0], "kind": value[1]} for key, value in FIELDS.items()},
        }

    @app.post("/api/analyze")
    def analysis():
        return jsonify(run_query(request.get_json()))

    @app.post("/api/export")
    def export():
        result = run_query(request.get_json())
        stream = io.StringIO()
        writer = csv.writer(stream)
        extra_fields = [k for k in FIELDS if k not in {"frame", "previous_race", "previous_finish"}]
        writer.writerow(
            [
                "開催日",
                "馬名",
                "枠",
                "前走",
                "前走着順",
                "本番着順",
                "単勝払戻",
                "複勝払戻",
                "出典",
                "データ版",
                *[FIELDS[k][0] for k in extra_fields],
                "前走状態",
                "単勝払戻の確認状態",
                "複勝払戻の確認状態",
            ]
        )
        for e in result["entries"]:
            values = [
                e["race_date"],
                e["horse_name"],
                e["features"].get("frame"),
                e["features"].get("previous_race"),
                e["features"].get("previous_finish"),
                e.get("finish_text"),
                e["bets"]["単勝"].get("payout_yen"),
                e["bets"]["複勝"].get("payout_yen"),
                e["source"]["url"],
                result["data_version"],
                *[e["features"].get(k) for k in extra_fields],
                e["features"].get("previous_finish_status"),
                e["bets"]["単勝"].get("reason"),
                e["bets"]["複勝"].get("reason"),
            ]
            writer.writerow(
                [
                    "'" + v
                    if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@"))
                    else v
                    for v in values
                ]
            )
        return Response(
            stream.getvalue().encode("utf-8-sig"),
            content_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="keiba-history.csv"'},
        )

    @app.route("/api/searches", methods=["GET", "POST"])
    def saved_searches():
        if request.method == "GET":
            return {"searches": store.list_searches()}
        value = request.get_json()
        if not isinstance(value, dict):
            raise InvalidQuery("保存する条件を確認してください。")
        name = str(value.get("name", "")).strip()
        if not 1 <= len(name) <= 100:
            raise InvalidQuery("保存名を1〜100文字で指定してください。")
        result = run_query(value.get("query"))
        q = result["query"] | {"snapshot_ids": result["snapshot_ids"]}
        return {"id": store.save_search(name, q)}, 201

    @app.route("/api/jobs", methods=["GET", "POST"])
    def collection_jobs():
        if request.method == "GET":
            return {"jobs": store.list_jobs()}
        value = request.get_json()
        if not isinstance(value, dict):
            raise InvalidQuery("収集する範囲を確認してください。")
        mode = value.get("mode")
        years = value.get("years")
        series = value.get("series_id")
        if mode not in {"history", "latest", "refresh"} or not isinstance(years, list) or not years:
            raise InvalidQuery("収集する期間を確認してください。")
        if (
            any(
                type(y) is not int or not 2016 <= y <= datetime.now(ZoneInfo("Asia/Tokyo")).year
                for y in years
            )
            or len(years) > 30
        ):
            raise InvalidQuery("収集する年度が範囲外です。")
        if series is not None and series not in series_ids:
            raise InvalidQuery("収集する対象レースを確認してください。")
        identity = store.register_job(
            {"mode": mode, "years": sorted(set(years)), "series_id": series}
        )
        runner.start(identity)
        return {"id": identity}, 202

    @app.post("/api/jobs/<identity>/resume")
    def resume(identity):
        if store.get_job(identity) is None:
            return {"error": "収集処理が見つかりません。"}, 404
        return {"started": runner.start(identity)}, 202

    return app
