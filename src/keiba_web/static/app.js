"use strict";
const $ = (id) => document.getElementById(id);
const csrf = document.querySelector('meta[name="csrf-token"]').content;
let meta,
  choices = {},
  filterRows = [],
  frozen = null,
  lastResult = null,
  saved = [],
  poll = null,
  entryPage = 0;
const classNames = {
  allowance_1: "1勝クラス（旧500万円以下）",
  allowance_2: "2勝クラス（旧1000万円以下）",
  allowance_3: "3勝クラス（旧1600万円以下）",
  open: "オープン",
  listed: "リステッド",
  newcomer: "新馬",
  maiden: "未勝利",
  G1: "GⅠ",
  G2: "GⅡ",
  G3: "GⅢ",
  Jpn1: "JpnⅠ",
  Jpn2: "JpnⅡ",
  Jpn3: "JpnⅢ",
};
const fmt = (value) => (value === null || value === undefined ? "—" : value);
const pct = (value) =>
  value === null || value === undefined ? "—" : `${value}%`;
const label = (field, value) =>
  value == null
    ? "不明"
    : field === "previous_class"
      ? classNames[value] || value
      : value;
function error(message) {
  $("error").textContent = message;
  $("error").hidden = !message;
}
async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    credentials: "same-origin",
    headers:
      body === undefined
        ? {}
        : { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (response.status === 401) {
    location.assign("/login");
    throw Error("ログインしてください。");
  }
  if (!response.ok) {
    let message = "操作を完了できませんでした。";
    try {
      message = (await response.json()).error || message;
    } catch {}
    throw Error(message);
  }
  return response;
}
async function json(path, body) {
  return (await api(path, body)).json();
}
function option(value, name) {
  const node = document.createElement("option");
  node.value = value;
  node.textContent = name;
  return node;
}
function addFilter(condition) {
  const field =
    condition?.field ||
    Object.keys(meta.fields).find(
      (key) => !filterRows.some((row) => row.field === key),
    );
  if (!field) return;
  filterRows.push({
    field,
    op:
      condition?.op || (meta.fields[field].kind === "number" ? "range" : "in"),
    values: condition?.values || [],
    min: condition?.min ?? "",
    max: condition?.max ?? "",
  });
  renderFilters();
}
function renderFilters() {
  const parent = $("filters");
  parent.replaceChildren();
  filterRows.forEach((row, index) => {
    const box = document.createElement("div");
    box.className = "filter-row";
    const head = document.createElement("div");
    head.className = "filter-head";
    const select = document.createElement("select");
    select.setAttribute("aria-label", "絞り込み項目");
    Object.entries(meta.fields).forEach(([key, value]) => {
      const o = option(key, value.label);
      o.disabled = filterRows.some((r, i) => i !== index && r.field === key);
      select.append(o);
    });
    select.value = row.field;
    select.onchange = () => {
      row.field = select.value;
      row.op = meta.fields[row.field].kind === "number" ? "range" : "in";
      row.values = [];
      row.min = row.max = "";
      renderFilters();
    };
    const remove = document.createElement("button");
    remove.textContent = "×";
    remove.className = "remove";
    remove.setAttribute("aria-label", "この条件を解除");
    remove.onclick = () => {
      filterRows.splice(index, 1);
      renderFilters();
    };
    head.append(select, remove);
    box.append(head);
    if (meta.fields[row.field].kind === "number") {
      const mode = document.createElement("select");
      mode.setAttribute("aria-label", "指定方法");
      mode.append(
        option("range", "範囲で指定"),
        option("in", "複数の値を選択"),
      );
      mode.value = row.op;
      mode.onchange = () => {
        row.op = mode.value;
        renderFilters();
      };
      box.append(mode);
    }
    if (row.op === "range") {
      const inputs = document.createElement("div");
      inputs.className = "range-fields";
      ["min", "max"].forEach((key, i) => {
        const input = document.createElement("input");
        input.type = "number";
        input.step = "any";
        input.placeholder = i ? "上限" : "下限";
        input.setAttribute(
          "aria-label",
          `${meta.fields[row.field].label}の${i ? "上限" : "下限"}`,
        );
        input.value = row[key];
        input.oninput = () => (row[key] = input.value);
        inputs.append(input);
      });
      box.append(inputs);
    } else {
      const values = document.createElement("select");
      values.multiple = true;
      values.setAttribute(
        "aria-label",
        `${meta.fields[row.field].label}の選択値`,
      );
      const available = [
        ...new Set([...(choices[row.field] || []), ...row.values.map(String)]),
      ];
      available.forEach((value) => {
        const o = option(value, label(row.field, value));
        o.selected = row.values.map(String).includes(value);
        values.append(o);
      });
      values.onchange = () =>
        (row.values = Array.from(values.selectedOptions).map((o) => o.value));
      box.append(values);
      const note = document.createElement("p");
      note.className = "hint";
      note.textContent = available.length
        ? "複数の値を選択できます。"
        : "この対象に取得済みの値がありません。";
      box.append(note);
    }
    parent.append(box);
  });
  $("add-filter").disabled =
    filterRows.length >= Object.keys(meta.fields).length;
}
function query() {
  return {
    series_id: $("series").value,
    start_date: $("start").value,
    end_date: $("end").value,
    group_by: $("group").value || null,
    filters: filterRows.map((row) =>
      row.op === "range"
        ? {
            field: row.field,
            op: "range",
            min: row.min === "" ? null : row.min,
            max: row.max === "" ? null : row.max,
          }
        : { field: row.field, op: "in", values: row.values },
    ),
    ...(frozen === null ? {} : { snapshot_ids: frozen }),
  };
}
function cells(parent, values) {
  const tr = document.createElement("tr");
  values.forEach((value) => {
    const td = document.createElement("td");
    if (value instanceof Node) td.append(value);
    else td.textContent = fmt(value);
    tr.append(td);
  });
  parent.append(tr);
}
function empty(parent, span, message) {
  parent.replaceChildren();
  const tr = document.createElement("tr"),
    td = document.createElement("td");
  td.colSpan = span;
  td.className = "empty";
  td.textContent = message;
  tr.append(td);
  parent.append(tr);
}
function render(result) {
  lastResult = result;
  choices = result.choices;
  const s = result.summary,
    available = result.coverage.imported_races > 0;
  $("result-title").textContent =
    meta.series.find((v) => v.id === $("series").value)?.name || "分析結果";
  const c = result.coverage;
  $("coverage").textContent =
    `${$("start").value} 〜 ${$("end").value} ／ 保存済み ${c.imported_races}開催${c.expected_races === null ? "（対象一覧は未整備）" : ` / 対象 ${c.expected_races}開催`}`;
  $("metric-count").textContent = available ? `${s.matched_count}頭` : "—";
  $("metric-starts").textContent =
    `実出走 ${available ? s.started_count : "—"}頭`;
  $("metric-win").textContent = pct(s.win_rate);
  $("metric-wins").textContent =
    `1着 ${available ? s.wins : "—"} / 実出走 ${available ? s.started_count : "—"}`;
  $("metric-place").textContent = pct(s.bets["複勝"].hit_rate);
  $("metric-place-count").textContent =
    `的中 ${available ? s.bets["複勝"].hits : "—"} / 確認済み ${available ? s.bets["複勝"].confirmed_count : "—"}`;
  $("metric-win-return").textContent = pct(s.bets["単勝"].return_rate);
  $("metric-place-return").textContent = pct(s.bets["複勝"].return_rate);
  $("data-mode").hidden = !result.saved_data;
  const unknown = result.condition_counts.unknown;
  const notes = [];
  if (available && Object.values(result.unknown_features).some((n) => n > 0))
    notes.push("一部の条件項目に未取得・不明な値があります。");
  if (result.coverage.state === "no_races_in_period")
    notes.push("対象期間に対象レースの開催はありません。");
  else if (!available)
    notes.push(
      "このレースのデータはまだ保存されていません。データ収集から対象を選択してください。",
    );
  else if (c.state !== "complete")
    notes.push(
      "対象期間の収集が一部未完了です。保存済みの範囲で集計しています。",
    );
  if (unknown)
    notes.push(`条件判定に必要な値が不明な出走が${unknown}件あります。`);
  if (s.bets["単勝"].unknown_count || s.bets["複勝"].unknown_count)
    notes.push("払戻未確認の出走を回収率の計算から除いています。");
  $("quality").textContent = notes.join(" ");
  $("quality").hidden = !notes.length;
  $("groups").replaceChildren();
  const grouped = $("group").value;
  $("group-caption").textContent = grouped
    ? `${meta.fields[grouped].label}別`
    : "全体";
  const groups = grouped ? result.groups : [{ label: "全体", ...s }];
  groups.forEach((g) =>
    cells($("groups"), [
      grouped ? label(grouped, g.value) : g.label,
      `${g.matched_count} / ${g.started_count}`,
      pct(g.win_rate),
      pct(g.top2_rate),
      pct(g.top3_rate),
      pct(g.bets["複勝"].hit_rate),
      pct(g.bets["単勝"].return_rate),
      pct(g.bets["複勝"].return_rate),
    ]),
  );
  if (!groups.length)
    empty(
      $("groups"),
      8,
      available ? "条件に合う出走は0頭です。" : "対象データが未収集です。",
    );
  entryPage = 0;
  renderEntries();
  $("export").disabled = !available;
}
function renderEntries() {
  const data = lastResult.entries;
  const first = entryPage * 25;
  $("entries").replaceChildren();
  data.slice(first, first + 25).forEach((e) => {
    const facts = document.createElement("details");
    facts.className = "horse-facts";
    const heading = document.createElement("summary");
    heading.textContent = e.horse_name;
    const values = document.createElement("dl");
    Object.keys(meta.fields).forEach((field) => {
      const title = document.createElement("dt"),
        value = document.createElement("dd");
      title.textContent = meta.fields[field].label;
      value.textContent =
        field === "previous_finish" && e.features.previous_finish_status
          ? { dnf: "競走中止", disqualified: "失格" }[
              e.features.previous_finish_status
            ]
          : label(field, e.features[field]);
      values.append(title, value);
    });
    facts.append(heading, values);
    const link = document.createElement("a");
    link.href = e.source.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "JRA ↗";
    cells($("entries"), [
      e.race_date,
      facts,
      e.features.frame,
      e.features.previous_race,
      e.features.previous_finish,
      e.finish_text,
      e.bets["単勝"].payout_yen,
      e.bets["複勝"].payout_yen,
      link,
    ]);
  });
  if (!data.length)
    empty(
      $("entries"),
      9,
      lastResult.coverage.state === "no_races_in_period"
        ? "対象期間に開催はありません。"
        : lastResult.coverage.imported_races
          ? "条件に合う出走は0頭です。"
          : "対象データが未収集です。",
    );
  $("detail-count").textContent = data.length
    ? `${first + 1}〜${Math.min(first + 25, data.length)} / ${data.length}件`
    : "0件";
  $("entries-prev").disabled = entryPage === 0;
  $("entries-next").disabled = first + 25 >= data.length;
}
async function analyze() {
  error("");
  $("analyze").disabled = true;
  try {
    render(await json("/api/analyze", query()));
  } catch (e) {
    error(e.message);
  } finally {
    $("analyze").disabled = false;
  }
}
async function loadSaved() {
  saved = (await json("/api/searches")).searches;
  $("saved").replaceChildren(option("", "選択してください"));
  saved.forEach((s) => $("saved").append(option(s.id, s.name)));
}
async function jobs() {
  clearTimeout(poll);
  const data = await json("/api/jobs");
  $("jobs").replaceChildren();
  let active = false;
  data.jobs.forEach((job) => {
    const stale =
      job.state === "running" &&
      Date.now() - Date.parse(job.updated_at) > 150000;
    const row = document.createElement("div");
    row.className = "job";
    const info = document.createElement("div"),
      title = document.createElement("strong"),
      message = document.createElement("p"),
      updated = document.createElement("small");
    const names = {
      queued: "収集待ち",
      running: "収集中",
      completed: "完了",
      failed: "停止・要確認",
      blocked: "取得を停止",
    };
    title.textContent = `${stale ? "再開待ち" : names[job.state] || job.state}　${job.cursor} / ${job.total || "準備中"}`;
    message.textContent = job.message;
    updated.textContent = `更新：${new Date(job.updated_at).toLocaleString("ja-JP", { timeZone: "Asia/Tokyo" })}（日本時間）`;
    info.append(title, message, updated);
    row.append(info);
    if (stale || ["queued", "failed", "blocked"].includes(job.state)) {
      const resume = document.createElement("button");
      resume.textContent = "再開";
      resume.onclick = async () => {
        try {
          await json(`/api/jobs/${job.id}/resume`, {});
          await jobs();
        } catch (e) {
          error(e.message);
        }
      };
      row.append(resume);
    }
    $("jobs").append(row);
    if (["queued", "running"].includes(job.state) && !stale) active = true;
  });
  if (!data.jobs.length) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = "収集はまだ実行されていません。";
    $("jobs").append(p);
  }
  if (active)
    poll = setTimeout(() => jobs().catch((e) => error(e.message)), 3000);
}
async function collect(latest, selectedOnly = false) {
  error("");
  const year = meta.current_year;
  const first = Number($("first-year").value),
    last = Number($("last-year").value);
  if (!latest && (first > last || last - first > 29)) {
    error("収集する年度を確認してください。");
    return;
  }
  const series = selectedOnly
    ? $("series").value
    : $("collection-scope").value === "all"
      ? null
      : $("series").value;
  if (series === "") {
    error("対象レースを選択してください。");
    return;
  }
  const years = latest
    ? [year - 1, year]
    : Array.from({ length: last - first + 1 }, (_, i) => first + i);
  $("collect-history").disabled = $("collect-latest").disabled = true;
  try {
    await json("/api/jobs", {
      years,
      series_id: series,
      mode: latest
        ? "latest"
        : $("refresh-history").checked
          ? "refresh"
          : "history",
    });
    await jobs();
  } catch (e) {
    error(e.message);
  } finally {
    $("collect-history").disabled = $("collect-latest").disabled = false;
  }
}
async function boot() {
  try {
    meta = await json("/api/meta");
    $("series").replaceChildren(option("", "レースを選択"));
    meta.series
      .sort(
        (a, b) =>
          (b.imported > 0) - (a.imported > 0) ||
          a.name.localeCompare(b.name, "ja"),
      )
      .forEach((s) => {
        const former =
          s.aliases.length > 1 ? `（旧：${s.aliases[0].name}）` : "";
        $("series").append(
          option(
            s.id,
            `${s.name}${former} · ${s.imported ? s.imported + "開催保存済み" : "未収集"}`,
          ),
        );
      });
    const ready = meta.series.find((s) => s.imported > 0);
    if (ready) {
      $("series").value = ready.id;
      await analyze();
    }
    await Promise.all([loadSaved(), jobs()]);
  } catch (e) {
    error(e.message);
  }
}
$("analyze").onclick = analyze;
$("add-filter").onclick = () => addFilter();
$("current-data").onclick = () => {
  frozen = null;
  analyze();
};
["series", "start", "end"].forEach(
  (id) =>
    ($(id).onchange = () => {
      frozen = null;
      filterRows = [];
      renderFilters();
      if (id === "series") analyze();
    }),
);
$("save").onclick = () => $("save-dialog").showModal();
$("cancel-save").onclick = () => $("save-dialog").close();
$("save-form").onsubmit = async (event) => {
  event.preventDefault();
  try {
    await json("/api/searches", { name: $("save-name").value, query: query() });
    $("save-dialog").close();
    await loadSaved();
  } catch (e) {
    error(e.message);
  }
};
$("load").onclick = async () => {
  const s = saved.find((v) => v.id === $("saved").value);
  if (!s) return;
  $("series").value = s.query.series_id;
  $("start").value = s.query.start_date;
  $("end").value = s.query.end_date;
  $("group").value = s.query.group_by || "";
  frozen = s.query.snapshot_ids ?? null;
  filterRows = [];
  (s.query.filters || []).forEach(addFilter);
  renderFilters();
  await analyze();
};
$("export").onclick = async () => {
  try {
    const response = await api("/api/export", {
      ...lastResult.query,
      snapshot_ids: lastResult.snapshot_ids,
    });
    const url = URL.createObjectURL(await response.blob()),
      anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "keiba-history.csv";
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (e) {
    error(e.message);
  }
};
$("collect-history").onclick = () => collect(false);
$("collect-latest").onclick = () => collect(true);
$("quick-latest").onclick = () => collect(true, true);
$("entries-prev").onclick = () => {
  entryPage--;
  renderEntries();
};
$("entries-next").onclick = () => {
  entryPage++;
  renderEntries();
};
boot();
