const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const api = async (path, opts = {}) => {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) {
    const raw = await r.text();
    let msg = raw;
    try { msg = JSON.parse(raw).detail ?? raw; } catch { /* not JSON */ }
    throw new Error(String(msg).slice(0, 400));
  }
  return r.status === 204 ? null : r.json();
};
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* ---------------------------------------------------------------- navigation */
$$(".tab").forEach((t) => t.onclick = () => {
  $$(".tab").forEach((x) => x.classList.toggle("active", x === t));
  $$(".view").forEach((v) => v.classList.add("hidden"));
  $(`#view-${t.dataset.view}`).classList.remove("hidden");
  ({ run: loadJobs, review: loadDrafts, recaps: loadRecaps, history: loadRuns, guide: loadGuide, settings: loadSettings })[t.dataset.view]();
});

/* ------------------------------------------------------------------- theme */
function applyTheme(t) {
  if (!["auto", "light", "dark"].includes(t)) t = "auto";
  document.documentElement.dataset.theme = t;
  $$("[data-theme-set]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.themeSet === t)));
  try { localStorage.setItem("mailroom-theme", t); } catch (e) { /* storage may be off in the app window */ }
}
$$("[data-theme-set]").forEach((b) => b.onclick = () => {
  applyTheme(b.dataset.themeSet);
  api("/api/settings/ui", { method: "PUT", body: JSON.stringify({ theme: b.dataset.themeSet }) }).catch(() => {});
});
applyTheme(document.documentElement.dataset.theme);
api("/api/settings/ui").then((u) => applyTheme(u.theme)).catch(() => {});

/* ---------------------------------------------------------------------- run */
const ENGINE = {
  claude_cli: { key: "claude", logo: "claude-color.svg", label: "Claude", desc: "Claude CLI · your Claude plan" },
  codex_cli: { key: "chatgpt", logo: "openai.svg", mono: true, label: "ChatGPT", desc: "Codex CLI · your ChatGPT plan" },
  gemini_cli: { key: "gemini", logo: "gemini-color.svg", label: "Gemini", desc: "Antigravity CLI · your Google plan" },
  ollama: { key: "ollama", logo: "ollama.svg", mono: true, label: "Ollama", desc: "Ollama · local, on this Mac" },
};
const N8N_ENGINE = { key: "n8n", logo: "n8n-color.svg", label: "n8n", desc: "n8n workflow · webhook" };
function engineOf(j) {
  if (j.runner === "n8n") return N8N_ENGINE;
  return ENGINE[j.provider] || { key: "default", text: (j.name || "?").slice(0, 2), label: j.name || "", desc: j.runner + (j.provider ? " · " + j.provider : "") };
}
/* Official logo in a tile. Monochrome logos (OpenAI, Ollama) are drawn with a CSS mask so they follow the theme's text color. */
function logoTile(e, size = "") {
  if (!e.logo) return `<span class="logo ${size}"><span class="txt">${esc(e.text || "?")}</span></span>`;
  const src = `/static/logos/${e.logo}`;
  return e.mono
    ? `<span class="logo ${size}" role="img" aria-label="${esc(e.label)}"><i class="mask" style="--src:url('${src}')"></i></span>`
    : `<span class="logo ${size}"><img src="${src}" alt="${esc(e.label)}"></span>`;
}
function ago(iso) {
  if (!iso) return "";
  const t = new Date(/[zZ+]/.test(iso.slice(19)) ? iso : iso + "Z").getTime();
  const m = Math.round((Date.now() - t) / 60000);
  if (m < 1) return "just now";
  if (m < 60) return m + " min ago";
  if (m < 48 * 60) return Math.round(m / 60) + " h ago";
  return Math.round(m / 1440) + " days ago";
}
let runningJob = null;
/* "cancelled" is what a run records when you press Stop */
const stLabel = (s) => ({ cancelled: "stopped" }[s] || s);

async function loadJobs() {
  const [jobs, runs] = await Promise.all([api("/api/jobs"), api("/api/runs?limit=100").catch(() => [])]);
  const last = {};
  runs.forEach((r) => { if (!last[r.job_id]) last[r.job_id] = r; });
  $("#jobs").innerHTML = jobs.map((j) => {
    const e = engineOf(j), r = last[j.job_id];
    const cls = r ? (r.status === "succeeded" ? "ok-t" : r.status === "running" ? "run-t" : r.status === "cancelled" ? "" : "bad-t") : "";
    const lastTxt = r ? `last run <b class="${cls}">${esc(stLabel(r.status))}</b> · ${esc(ago(r.started_at))}` : "never run";
    return `
    <div class="card${runningJob === j.job_id ? " running" : ""}" data-engine="${e.key}" data-jobcard="${esc(j.job_id)}">
      <div class="top">${logoTile(e)}
        <div class="tt"><h3>${esc(j.name)}</h3><div class="meta">${esc(e.desc)}</div></div></div>
      <div class="model" title="${esc(j.effective_model || "")}">${esc(j.effective_model || "—")}</div>
      <div class="last">${lastTxt}</div>
      <button class="primary" data-job="${esc(j.job_id)}">${runningJob === j.job_id ? "Running…" : "Run ▸"}</button>
    </div>`;
  }).join("");
  $$("#jobs button").forEach((b) => b.onclick = () => runJob(b.dataset.job, b));
  if (runningJob) $$("#jobs button").forEach((b) => b.disabled = true);
}

function setBusy(busy) {
  $("#busy").classList.toggle("hidden", !busy);
  $("#idle").classList.toggle("hidden", busy);
  $("#btn-stop").classList.toggle("hidden", !busy);
}

const PHASES = ["drafting", "events", "recap"];
function renderProgress(d, busy) {
  const log = d.log || [];
  const isAssistant = d.runner === "assistant";
  $("#phases").classList.toggle("off", !isAssistant);
  let cur = -1, done = 0, total = 0;
  const failed = new Set();
  log.forEach((l) => {
    const m = l.match(/— (DRAFTING|EVENTS|RECAP) —/);
    if (m) { cur = PHASES.indexOf(m[1].toLowerCase()); done = 0; total = 0; }
    const f = l.match(/(DRAFTING|EVENTS|RECAP) FAILED/);
    if (f) failed.add(f[1].toLowerCase());
    const n = l.match(/\[(\d+)\/(\d+)\]|ranking batch (\d+)\/(\d+)/);
    if (n) { done = +(n[1] || n[3]) - 1; total = +(n[2] || n[4]); }
  });
  $$("#phases span").forEach((s, i) => {
    s.className = "";
    if (failed.has(s.dataset.phase)) s.classList.add("failed");
    else if (!busy && cur >= 0 && i <= cur) s.classList.add("done");
    else if (i < cur) s.classList.add("done");
    else if (i === cur && busy) s.classList.add("active");
  });
  const bar = $("#progress-bar"), wrap = bar.parentElement;
  if (!busy) { wrap.classList.remove("indeterminate"); bar.style.width = "100%"; return; }
  if (!isAssistant || cur < 0) { wrap.classList.add("indeterminate"); return; }
  wrap.classList.remove("indeterminate");
  const span = [[0, 50], [50, 80], [80, 100]][cur];
  const frac = total ? Math.min(1, done / total) : 0.05;
  bar.style.width = (span[0] + (span[1] - span[0]) * frac).toFixed(1) + "%";
}

/* Look-back window (one value for replies, events and recap); remembered in data/ui.json. */
let lookback = 0;
let stdWin = { d: 48, e: 24, r: 24 };     // Standard windows, from Settings → Look-back windows and limits
const stdText = () => stdWin.e === stdWin.r ? `Replies ${lbLabel(stdWin.d)}, events and recap ${lbLabel(stdWin.e)}`
  : `Replies ${lbLabel(stdWin.d)}, events ${lbLabel(stdWin.e)}, recap ${lbLabel(stdWin.r)}`;
function lookbackHours() {
  if (lookback !== "custom") return lookback || null;
  const v = parseInt($("#lookback-hours").value, 10);
  return v >= 1 && v <= 168 ? v : null;
}
function lbLabel(h) { return h % 24 === 0 && h > 48 ? `${h / 24} days` : `${h} h`; }
function showLookback() {
  $$("#lookback button").forEach((b) => b.classList.toggle("on", String(b.dataset.lb) === String(lookback)));
  $(".lb-custom").hidden = lookback !== "custom";
  const h = lookbackHours();
  $("[data-lb=\"0\"]").title = stdText();
  $("#lookback-note").textContent = lookback === 0 ? stdText()
    : h ? `Replies, events and recap: last ${lbLabel(h)}` + (h > 72 ? " · a run reads at most the max-emails setting for replies" : "")
    : "Type 1 to 168 hours";
}
function saveLookback() {
  const h = lookbackHours();
  if (lookback === "custom" && !h) return;
  api("/api/settings/ui", { method: "PUT", body: JSON.stringify({ lookbackHours: h || 0 }) }).catch(() => {});
}
$$("#lookback button").forEach((b) => b.onclick = () => {
  lookback = b.dataset.lb === "custom" ? "custom" : parseInt(b.dataset.lb, 10);
  showLookback();
  if (lookback === "custom") $("#lookback-hours").focus(); else saveLookback();
});
$("#lookback-hours").oninput = () => { showLookback(); saveLookback(); };
api("/api/settings/ui").then((u) => {
  const h = u.lookbackHours || 0;
  if ([0, 24, 48, 72, 168].includes(h)) lookback = h; else { lookback = "custom"; $("#lookback-hours").value = h; }
  showLookback();
}).catch(() => {});

async function runJob(id, btn) {
  if (lookback === "custom" && !lookbackHours()) { showLookback(); $("#lookback-hours").focus(); return; }
  runningJob = id;
  $$("#jobs button").forEach((b) => b.disabled = true);
  btn.textContent = "running…";
  $(`[data-jobcard="${id}"]`)?.classList.add("running");
  $("#run-panel").classList.remove("hidden");
  $("#run-log").classList.remove("hidden");
  $("#progress-bar").style.width = "0";
  $("#progress-bar").parentElement.classList.add("indeterminate");
  $$("#phases span").forEach((s) => s.className = "");
  setBusy(true);
  $("#run-log").textContent = "queued…\n";
  try {
    const hours = lookbackHours();
    await api(`/api/jobs/${id}/run`, { method: "POST", body: JSON.stringify(hours ? { hours } : {}) });
    await pollRun();
  } catch (e) {
    $("#run-log").textContent += "\nERROR: " + e.message;
    runningJob = null; setBusy(false); loadJobs();
  }
}

$("#btn-stop").onclick = async () => {
  const b = $("#btn-stop");
  if (!confirm("Stop the running job? Drafts already created stay in Gmail Drafts; nothing is sent.")) return;
  b.disabled = true; b.textContent = "stopping…";
  try {
    await api("/api/jobs/stop", { method: "POST" });
    $("#stop-note").textContent = "Stop requested — the running step is being stopped.";
  } catch (e) {
    $("#stop-note").textContent = e.message;
    b.disabled = false; b.textContent = "Stop";
  }
};

async function pollRun() {
  const tick = async () => {
    let busy = false;
    try {
      busy = (await api("/api/jobs/status")).busy;
      const runs = await api("/api/runs?limit=1");
      if (runs[0]) {
        const d = await api(`/api/runs/${runs[0].id}`);
        const when = d.started_at ? new Date(d.started_at + (/[zZ+]/.test((d.started_at || "").slice(19)) ? "" : "Z")).toLocaleString() : "";
        const box = $("#run-log");
        const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
        box.textContent = `── run #${d.id} · ${d.job_name} · ${when} · ${d.status} ──\n\n` +
          (d.log || []).join("\n") + (d.error ? `\n\nERROR: ${d.error}` : "");
        if (stick) box.scrollTop = box.scrollHeight;
        renderProgress(d, busy);
        if (!runningJob && busy) runningJob = d.job_id;
      }
    } catch (e) { /* transient: keep polling */ busy = true; }
    setBusy(busy);
    if (busy) return setTimeout(tick, 1200);
    $("#stop-note").textContent = "";
    $("#btn-stop").disabled = false; $("#btn-stop").textContent = "Stop";
    runningJob = null;
    loadJobs();
    refreshBadge();
    if (auto) api("/api/schedule").then(renderAutoState).catch(() => {});
  };
  setTimeout(tick, 600);
}

async function resumeIfRunning() {
  const { busy } = await api("/api/jobs/status").catch(() => ({ busy: false }));
  setBusy(busy);
  if (!busy) return;
  const runs = await api("/api/runs?limit=1").catch(() => []);
  runningJob = runs[0]?.job_id || null;
  $("#run-panel").classList.remove("hidden");
  $("#run-log").classList.remove("hidden");
  loadJobs();
  pollRun();
}

/* ---------------------------------------------------------- automatic runs
   Run tab → Automatic runs. Saved in data/schedule.json; Save with Automatic installs a
   LaunchAgent so macOS starts the run at the chosen times, even with the window closed. */
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
let auto = null, autoSaved = "", autoJobs = [];
/* Time pickers are two menus (hour, minutes), not <input type="time">: the Mac app's web view
   leaves a time field empty until every part is filled, so Add time read nothing. */
const pad2 = (n) => String(n).padStart(2, "0");
function tpBuild(el, value) {
  const lab = el.dataset.label || "Time";
  el.innerHTML = `<select data-h aria-label="${esc(lab)} hour">${Array.from({ length: 24 }, (_, h) => `<option>${pad2(h)}</option>`).join("")}</select>`
    + `<span class="tp-c">:</span><select data-m aria-label="${esc(lab)} minutes">${Array.from({ length: 12 }, (_, i) => `<option>${pad2(i * 5)}</option>`).join("")}</select>`;
  tpSet(el, value);
}
function tpSet(el, v) {
  const [h, m] = String(v || "08:00").split(":");
  const ms = $("[data-m]", el);
  if (![...ms.options].some((o) => o.value === m)) {             // a saved minute off the 5-minute grid
    ms.add(new Option(m, m)); [...ms.options].sort((a, b) => a.value - b.value).forEach((o) => ms.add(o));
  }
  $("[data-h]", el).value = h; ms.value = m;
}
const tpGet = (el) => `${$("[data-h]", el).value}:${$("[data-m]", el).value}`;
const tpDisable = (el, off) => $$("select", el).forEach((x) => x.disabled = off);
tpBuild($("#auto-time-new"), "08:00"); tpBuild($("#auto-win-s"), "07:00"); tpBuild($("#auto-win-e"), "21:00");
const hhmm = (iso) => new Date(iso).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
function autoCfg() {
  const lbSel = $("#auto-lb").value;
  const lb = lbSel === "custom" ? parseInt($("#auto-lb-h").value, 10) || 0 : parseInt(lbSel, 10);
  return {
    enabled: $("#auto-mode .on").dataset.mode === "auto",
    job_id: $("#auto-job").value,
    lookbackHours: lb,
    days: $$("#auto-days button.on").map((b) => +b.dataset.d),
    times: auto.times.slice().sort(),
    window: { enabled: $("#auto-win").checked, start: tpGet($("#auto-win-s")), end: tpGet($("#auto-win-e")) },
    notify: $("#auto-notify").checked,
  };
}
function autoDirty() {
  const now = JSON.stringify(autoCfg());
  $("#auto-save").disabled = now === autoSaved;
  $("#auto-err").hidden = true;
  const c = autoCfg();
  $("#auto-body").hidden = !c.enabled;
  $("#auto-lb-note").textContent = c.lookbackHours ? `Replies, events and recap: last ${lbLabel(c.lookbackHours)}` : stdText();
  $(".lb-c").hidden = $("#auto-lb").value !== "custom";
  $$("#auto-win-s, #auto-win-e").forEach((el) => tpDisable(el, !c.window.enabled));
}
function renderTimes() {
  $("#auto-times").innerHTML = auto.times.slice().sort().map((t) =>
    `<span class="tm">${esc(t)}<button type="button" data-rm="${esc(t)}" aria-label="Remove ${esc(t)}">×</button></span>`).join("");
  $$("#auto-times [data-rm]").forEach((b) => b.onclick = () => { auto.times = auto.times.filter((x) => x !== b.dataset.rm); renderTimes(); autoDirty(); });
}
function renderAutoState(st) {
  const c = st.config, ins = st.installed, j = autoJobs.find((x) => x.job_id === c.job_id);
  const name = j ? j.name : "—";
  const when = c.days.length === 7 ? "every day" : c.days.join() === "1,2,3,4,5" ? "weekdays" : c.days.map((d) => DAYS[d - 1]).join(", ");
  $("#auto-summary").textContent = c.enabled
    ? `On: ${name}, ${when} at ${c.times.join(", ")}${c.window.enabled ? ` (only ${c.window.start}-${c.window.end})` : ""}.`
    : "Off: runs start only when you press Run.";
  const rows = [];
  if (c.enabled) {
    if (!st.platform_ok) rows.push(`<span class="bad-t">Background runs need macOS.</span>`);
    else if (!ins.file || !ins.loaded) rows.push(`<span class="bad-t">The background job is not active: press Save again.</span>`);
    else if (!ins.current) rows.push(`<span class="warn-t">The background job is out of date (the project folder moved?): press Save again.</span>`);
    else rows.push(`<span><b>Background job</b> <span class="ok-t">active</span> · runs with the window closed</span>`);
    if (st.next.length) rows.push(`<span><b>Next</b> ${st.next.map(hhmm).map(esc).join(" · ")}</span>`);
  }
  $("#auto-state").innerHTML = rows.join("");
  $("#auto-kick").hidden = !(c.enabled && ins.file && ins.loaded);
  const hist = st.history || [];
  $("#auto-hist").hidden = !hist.length;
  $("#auto-hist ul").innerHTML = hist.map(autoRowHtml).join("");
  wireOpenRun($("#auto-hist"));
  showAutoNews(st.unseen);
}

/* Recent automatic runs: the list in the panel and the in-app notification (until you press Got it) */
const AUTO_ST = { succeeded: "ok", failed: "bad", interrupted: "bad", cancelled: "skip", skipped: "skip" };
function autoRowHtml(h) {
  const j = autoJobs.find((x) => x.job_id === h.job);
  const when = new Date(h.at).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  const what = h.status === "succeeded" ? (h.summary || "") : (h.error || h.reason || h.summary || "");
  return `<li class="ah ${AUTO_ST[h.status] || "skip"}"><span class="ah-dot" aria-hidden="true"></span>
    <span class="ah-when">${esc(when)}</span><span class="ah-eng">${esc(j ? j.name : h.job || "")}</span>
    <b class="ah-st">${esc(stLabel(h.status))}</b><span class="ah-what" title="${esc(what)}">${esc(what)}</span>
    ${h.run_id ? `<button type="button" class="linkbtn" data-open-run="${esc(h.run_id)}">Open</button>` : "<span></span>"}</li>`;
}
function wireOpenRun(root, after) {
  $$("[data-open-run]", root).forEach((b) => b.onclick = async () => {
    if (after) after();
    $('.tab[data-view="history"]').click();
    await showRun(b.dataset.openRun);
    $("#run-detail").scrollIntoView({ behavior: "smooth", block: "start" });
  });
}
let autoNewsKey = "";
function showAutoNews(u) {
  const box = $("#toasts");
  if (!u || !u.count) { box.innerHTML = ""; autoNewsKey = ""; return; }
  const key = u.items.map((x) => x.at + x.status).join();
  if (key === autoNewsKey) return;
  autoNewsKey = key;
  const n = (s) => u.items.filter((x) => x.status === s).length;
  const bad = n("failed") + n("interrupted");
  const cls = bad ? "bad" : n("succeeded") ? "ok" : "skip";
  const title = u.count === 1 ? `Automatic run ${stLabel(u.items[0].status)}` : `${u.count} automatic runs since you last looked`
    + (bad ? ` · ${bad} failed or interrupted` : "");
  box.innerHTML = `<div class="toast ${cls}" role="status">
    <div class="t-head"><b>${esc(title)}</b><button type="button" class="t-x" aria-label="Dismiss">×</button></div>
    <ul class="ah-list">${u.items.map(autoRowHtml).join("")}</ul>
    ${u.count > u.items.length ? `<small class="hint">and ${u.count - u.items.length} older: see Run → Automatic runs</small>` : ""}
    <div class="t-foot"><span class="hint">Drafts only, nothing is sent.</span><button type="button" class="primary small" data-seen>Got it</button></div></div>`;
  const done = () => { api("/api/schedule/seen", { method: "POST" }).catch(() => {}); box.innerHTML = ""; autoNewsKey = ""; };
  wireOpenRun(box, done);
  $("[data-seen]", box).onclick = done; $(".t-x", box).onclick = done;
}
function fillAuto(st) {
  const c = st.config;
  auto = { times: c.times.slice() };
  $$("#auto-mode button").forEach((b) => b.classList.toggle("on", b.dataset.mode === (c.enabled ? "auto" : "manual")));
  $("#auto-job").value = c.job_id || (autoJobs[0] && autoJobs[0].job_id) || "";
  const preset = ["0", "24", "48", "72", "168"].includes(String(c.lookbackHours));
  $("#auto-lb").value = preset ? String(c.lookbackHours) : "custom";
  $("#auto-lb-h").value = preset ? "" : c.lookbackHours;
  $$("#auto-days button").forEach((b) => b.classList.toggle("on", c.days.includes(+b.dataset.d)));
  $("#auto-win").checked = c.window.enabled;
  tpSet($("#auto-win-s"), c.window.start); tpSet($("#auto-win-e"), c.window.end);
  $("#auto-notify").checked = c.notify !== false;
  renderTimes();
  autoSaved = JSON.stringify(autoCfg());
  renderAutoState(st);
  autoDirty();
}
async function loadAuto() {
  try {
    autoJobs = await api("/api/jobs");
    $("#auto-job").innerHTML = autoJobs.map((j) => `<option value="${esc(j.job_id)}">${esc(j.name)} · ${esc(engineOf(j).desc)}</option>`).join("");
    fillAuto(await api("/api/schedule"));
  } catch (e) { $("#auto-summary").textContent = "Automatic runs: " + e.message; }
}
$("#auto-days").innerHTML = DAYS.map((d, i) => `<button type="button" data-d="${i + 1}" aria-pressed="false">${d}</button>`).join("");
$$("#auto-days button").forEach((b) => b.onclick = () => { b.classList.toggle("on"); b.setAttribute("aria-pressed", b.classList.contains("on")); autoDirty(); });
$$("[data-days]").forEach((b) => b.onclick = () => {
  const on = b.dataset.days.split(",");
  $$("#auto-days button").forEach((x) => x.classList.toggle("on", on.includes(x.dataset.d)));
  autoDirty();
});
$$("#auto-mode button").forEach((b) => b.onclick = () => {
  $$("#auto-mode button").forEach((x) => x.classList.toggle("on", x === b));
  autoDirty();
});
$("#auto-time-add").onclick = () => {
  const v = tpGet($("#auto-time-new")), note = $("#auto-time-note");
  if (auto.times.includes(v)) { note.textContent = `${v} is already in the list`; return; }
  auto.times.push(v); note.textContent = "";
  renderTimes(); autoDirty();
};
$("#auto-time-new").onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); $("#auto-time-add").click(); } };
$("#auto-time-new").onchange = () => { $("#auto-time-note").textContent = ""; };
["#auto-job", "#auto-lb", "#auto-lb-h", "#auto-win", "#auto-win-s", "#auto-win-e", "#auto-notify"].forEach((s) => {
  $(s).oninput = autoDirty; $(s).onchange = autoDirty;
});
$("#auto-save").onclick = async () => {
  const c = autoCfg(), was = JSON.parse(autoSaved || "{}");
  if (c.enabled && !was.enabled && !confirm("Turn on automatic runs?\n\nThis adds a background job to your Mac (a LaunchAgent in your user Library) that starts the run at the times you chose, even when MailRoom is closed. Drafts only, nothing is sent.\n\nChoosing Manual and Save removes it."))
    return;
  if (!c.enabled && was.enabled && !confirm("Turn off automatic runs? The background job is removed from your Mac.")) return;
  const b = $("#auto-save"); b.disabled = true; b.textContent = "saving…";
  try {
    fillAuto(await api("/api/schedule", { method: "PUT", body: JSON.stringify(c) }));
  } catch (e) {
    $("#auto-err").textContent = e.message; $("#auto-err").hidden = false; b.disabled = false;
  } finally { b.textContent = "Save"; }
};
$("#auto-kick").onclick = async () => {
  if (!confirm("Start the automatic run once now, in the background? It is the same run as pressing Run: drafts only, nothing is sent.")) return;
  try { await api("/api/schedule/kick", { method: "POST" }); setTimeout(resumeIfRunning, 2500); }
  catch (e) { $("#auto-err").textContent = e.message; $("#auto-err").hidden = false; }
};
wireInfo($("#auto"));
loadAuto();
/* an automatic run can start while the window is open: pick it up and show its progress */
setInterval(async () => {
  if (document.hidden) return;
  api("/api/schedule/news").then(showAutoNews).catch(() => {});
  if (runningJob) return;
  const s = await api("/api/jobs/status").catch(() => null);
  if (s && s.busy) { await resumeIfRunning(); }
}, 20000);

/* ------------------------------------------------------------------- review */
/* Quick links: the tiles at the top of Review. Edited in Settings → Quick links;
   {account} in an address becomes your main Gmail address (opens the right Google account). */
const QL_ICON = {
  mail: '<path d="M4 6h16v12H4z"/><path d="m4 7 8 6 8-6"/>',
  calendar: '<rect x="4" y="5" width="16" height="15" rx="2"/><path d="M4 10h16M9 3v4M15 3v4"/>',
  drive: '<path d="M8 4h8l5 9-4 7H7l-4-7z"/><path d="M8 4l9 16M16 4 7 20M3 13h18"/>',
  doc: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4M9 12h6M9 16h6"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  n8n: '<circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="7" r="2"/><circle cx="19" cy="17" r="2"/><path d="M7 12h3M14 12l3-4M14 12l3 4"/>',
};
let quickLinks = [], qlAccount = "", qlFolder = "";
function fillAccount(url) {
  if (url.includes("{recapFolder}")) {
    url = qlFolder ? url.split("{recapFolder}").join(encodeURIComponent(qlFolder))
      : "https://docs.google.com/document/u/0/?authuser={account}";         // no folder set: Google Docs home
  }
  if (qlAccount) return url.split("{account}").join(encodeURIComponent(qlAccount));
  return url.replace(/[?&]authuser=\{account\}/g, "").replace("{account}", "").replace(/\?#/, "#").replace(/\?$/, "");
}
function renderQuickLinks() {
  const nav = $("#quick-links");
  if (!nav) return;
  nav.hidden = !quickLinks.length;
  nav.innerHTML = quickLinks.map((l, i) => `
    <a class="ql" style="--q:${["#ea4335", "#4285f4", "#34a853", "#fbbc05", "#a78bfa", "#ff6d8a"][i % 6]}" href="${esc(fillAccount(l.url))}" target="_blank" rel="noopener">
      <span class="ql-ico"><svg viewBox="0 0 24 24" aria-hidden="true">${QL_ICON[l.icon] || QL_ICON.link}</svg></span>
      <span class="ql-tx"><b>${esc(l.label)}</b><small>${esc(l.sub || "")}${l.url.includes("{account}") && qlAccount && l.icon !== "doc" ? (l.sub ? " · " : "") + esc(qlAccount) : ""}</small></span>
      <span class="ql-go">↗</span>
    </a>`).join("");
}
function loadQuickLinks() {
  api("/api/settings/app").then((r) => {
    const f = r.sections.flatMap((s) => s.fields).find((x) => x.key === "quickLinks");
    const v = (k) => (r.sections.flatMap((s) => s.fields).find((x) => x.key === k) || {}).value;
    qlFolder = v("recapFolderId") || "";
    quickLinks = (f && f.value) || []; renderQuickLinks();
    stdWin = { d: v("lookbackDrafting") || 48, e: v("lookbackEvents") || 24, r: v("lookbackRecap") || 24 };
    showLookback();
  }).catch(() => {});
}
loadQuickLinks();
api("/api/settings/addresses").then((r) => { qlAccount = (r && r.primary) || ""; renderQuickLinks(); }).catch(() => {});
const KIND_LABEL = { reply: "Reply draft", booking: "BOOK? note", "event-free": "Event · free", "event-conflict": "Event · conflict",
  "event-check": "Event · check", note: "Note" };
let reviewItems = [], reviewFilter = "all";
function whenMs(ms) { const d = new Date(+ms); return isNaN(d) || !+ms ? "" : d.toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }); }

async function loadDrafts(fresh = true) {
  const box = $("#drafts");
  box.innerHTML = `<p class="hint">Reading your Gmail drafts…</p>`;
  try {
    const r = await api(`/api/review?fresh=${fresh}`);
    reviewItems = r.items || [];
    setBadge(reviewItems.length);
  } catch (e) { box.innerHTML = `<p class="hint bad">${esc(e.message)}</p>`; return; }
  renderReview();
}
function renderReview() {
  const box = $("#drafts");
  const notes = reviewItems.filter((i) => i.kind !== "reply").length;
  $("#rf-all").textContent = reviewItems.length; $("#rf-reply").textContent = reviewItems.length - notes; $("#rf-note").textContent = notes;
  $$("[data-rf]").forEach((c) => c.classList.toggle("on", c.dataset.rf === reviewFilter));
  const list = reviewItems.filter((i) => reviewFilter === "all" || (reviewFilter === "reply" ? i.kind === "reply" : i.kind !== "reply"));
  box.innerHTML = list.length ? list.map((i) => {
    const o = i.original;
    const orig = o ? `<div class="orig"><b>${esc(o.from)}</b> · ${esc(o.subject)} · ${esc(whenMs(o.internalDate))}<div class="snip">${esc(o.snippet)}</div></div>` : "";
    const slots = i.slots && i.slots.length && i.status === "pending" ? `<div class="slots">${i.slots.map((x) => `<span>${esc(x)}</span>`).join("")}</div>` : "";
    const content = i.kind === "reply"
      ? `<input class="subj" data-f="subject" value="${esc(i.subject)}"><textarea data-f="body">${esc(i.body)}</textarea>`
      : `<div class="note-body">${esc(i.body)}</div>`;
    const acts = (i.actions || []).map((a) => `<a class="act ${esc(a.action)}" href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.label)} ↗</a>`).join("");
    return `<div class="dr" data-draft="${esc(i.draftId)}">
      <div class="dr-head"><span class="kind ${esc(i.kind)}">${esc(KIND_LABEL[i.kind] || i.kind)}</span>
        <span class="dr-to">${i.to ? "to " + esc(i.to) : "no recipient"}</span>
        <span class="when">${esc(whenMs(i.updated))}</span></div>
      ${orig}${slots}${content}
      <div class="dr-actions">
        ${i.kind === "reply" ? `<button class="primary" data-a="save">Save edits</button>` : acts}
        <a class="act" href="${esc(i.gmailUrl)}" target="_blank" rel="noopener">Open in Gmail ↗</a>
        <span class="saved"></span><span class="sp"></span>
        <button class="ghost danger" data-a="discard">Discard draft</button>
      </div></div>`;
  }).join("") : `<p class="hint">Nothing to review. New drafts and notes appear here after the next run.</p>`;
  $$(".dr").forEach((el) => {
    const id = el.dataset.draft;
    $$("button[data-a]", el).forEach((b) => b.onclick = async () => {
      const done = $(".saved", el);
      if (b.dataset.a === "discard") {
        if (!confirm("Delete this draft from Gmail? Only the draft goes; your emails are untouched.")) return;
        b.disabled = true;
        try { await api(`/api/review/${encodeURIComponent(id)}`, { method: "DELETE" });
          reviewItems = reviewItems.filter((x) => x.draftId !== id); setBadge(reviewItems.length); renderReview(); }
        catch (e) { alert(e.message); b.disabled = false; }
        return;
      }
      b.disabled = true; done.textContent = "saving…";
      try {
        const subject = $('[data-f="subject"]', el).value, body = $('[data-f="body"]', el).value;
        await api(`/api/review/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify({ subject, body }) });
        const it = reviewItems.find((x) => x.draftId === id); if (it) { it.subject = subject; it.body = body; }
        done.textContent = "saved in Gmail";
      } catch (e) { done.textContent = ""; alert(e.message); }
      b.disabled = false;
    });
  });
}
$$("[data-rf]").forEach((c) => c.onclick = () => { reviewFilter = c.dataset.rf; renderReview(); });
$("#btn-review-refresh").onclick = () => loadDrafts(true);

function setBadge(n) {
  const b = $("#pending-badge");
  b.textContent = n || "";
  b.hidden = !n;
}
async function refreshBadge() {
  try { const r = await api("/api/review?fresh=true"); setBadge((r.items || []).length); } catch (e) { setBadge(0); }
}

/* ------------------------------------------------------------------- recaps */
const LV = { 5: ["CRITICAL", "var(--p1)"], 4: ["HIGH", "var(--p2)"], 3: ["MEDIUM", "var(--p3)"], 2: ["LOW", "#8b92a3"], 1: ["IGNORE", "var(--line-2)"] };
let recapKey = null, recapData = null, recapFilter = { levels: new Set([5, 4, 3, 2, 1]), q: "" };
function fmtWhen(iso) {
  const d = new Date(iso);
  return isNaN(d) ? (iso || "") : d.toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}
function lvBar(levels, total) {
  const n = Math.max(1, total || Object.values(levels).reduce((a, b) => a + b, 0));
  return `<div class="lvbar">${[5, 4, 3, 2, 1].map((p) => levels[p] ? `<i class="lv${p}" style="width:${(100 * levels[p] / n).toFixed(1)}%"></i>` : "").join("")}</div>`;
}
function runCard(r) {
  if (!r || !Object.keys(r).length) return "";
  const cell = (n, label, cls = "") => `<div class="rs ${cls}"><b>${n}</b><span>${label}</span></div>`;
  return `<div class="run-card">
    <div class="rh">Run summary${r.engine ? " · " + esc(r.engine) : ""}${r.newEmails === 0 ? ' <span class="nn">nothing new since the last run</span>' : ""}</div>
    <div class="rgrid">
      ${cell(r.newEmails, "new emails handled")}
      ${cell(r.draftsCreated, "reply drafts created")}
      ${cell(r.meetingProposals, "with proposed times")}
      ${cell(r.bookingNotes + r.eventNotes, "notes created")}
    </div>
    <div class="rh2">Still open (last ${esc(String(r.days || 7))} days)</div>
    <div class="rgrid">
      ${cell(r.draftsReady, "drafts ready, not sent", r.draftsReady ? "warn" : "")}
      ${cell(r.draftsSent, "drafts sent", "ok")}
      ${cell(r.draftsDeleted, "deleted, not sent", r.draftsDeleted ? "bad" : "")}
      ${cell(r.notesWaiting, "notes waiting for you", r.notesWaiting ? "warn" : "")}
    </div></div>`;
}
async function loadRecaps() {
  const box = $("#recap-list");
  box.innerHTML = `<p class="hint">Loading…</p>`;
  let r;
  try { r = await api("/api/recaps"); } catch (e) { box.innerHTML = `<p class="hint bad">${esc(e.message)}</p>`; return; }
  const list = r.recaps || [];
  box.innerHTML = (r.error ? `<p class="hint bad">n8n not reachable: ${esc(r.error)}</p>` : "") + (list.length ? list.map((x) => `
    <button class="rc${x.key === recapKey ? " active" : ""}" data-recap="${esc(x.key)}">
      <div class="t"><span>${esc(fmtWhen(x.generatedAt))}</span><span class="eng" data-engine="${esc(x.source)}">${esc(x.engine)}</span></div>
      <div class="m">${x.run && x.run.newEmails === 0 ? "nothing new · " : x.run && x.run.newEmails ? x.run.newEmails + " new · " : ""}${x.emailCount} emails · ${x.levels[5]} critical · ${x.levels[4]} high</div>
      ${lvBar(x.levels, x.emailCount)}
    </button>`).join("") : `<p class="hint">No recaps yet. They appear here after the next run (n8n or any engine).</p>`);
  $$("[data-recap]").forEach((b) => b.onclick = () => openRecap(b.dataset.recap));
  if (list.length && (!recapKey || !list.some((x) => x.key === recapKey))) openRecap(list[0].key);
  else if (recapKey) openRecap(recapKey);
}
async function openRecap(key) {
  recapKey = key;
  $$("[data-recap]").forEach((b) => b.classList.toggle("active", b.dataset.recap === key));
  const view = $("#recap-view");
  view.innerHTML = `<p class="hint">Loading…</p>`;
  try { recapData = await api(`/api/recaps/${encodeURIComponent(key)}`); }
  catch (e) { view.innerHTML = `<p class="hint bad">${esc(e.message)}</p>`; return; }
  renderRecap();
}
function renderRecap() {
  const d = recapData, view = $("#recap-view");
  const q = recapFilter.q.toLowerCase();
  const shown = d.items.filter((i) => recapFilter.levels.has(+i.priority || 1) &&
    (!q || [i.subject, i.from, i.summary, i.topic].join(" ").toLowerCase().includes(q)));
  const groups = [5, 4, 3, 2, 1].map((p) => [p, shown.filter((i) => (+i.priority || 1) === p)]).filter(([, g]) => g.length);
  view.innerHTML = `
    <div class="rv-head">
      <div><h3>${esc(d.title)}</h3><div class="m">${esc(fmtWhen(d.generatedAt))} · <span class="eng" data-engine="${esc(d.source)}">${esc(d.engine)}</span> · ${d.emailCount} emails · ${d.drafted} drafted</div></div>
      ${d.docUrl ? `<a class="linkbtn" href="${esc(d.docUrl)}" target="_blank" rel="noopener">Open Google Doc ↗</a>` : ""}
    </div>
    ${runCard(d.run)}
    <div class="rv-tools">
      ${[5, 4, 3, 2, 1].map((p) => `<button class="chip${recapFilter.levels.has(p) ? " on" : ""}" data-lv="${p}">${LV[p][0]} · ${d.levels[p] || 0}</button>`).join("")}
      <input id="rv-q" type="search" placeholder="Search subject, sender, summary…" value="${esc(recapFilter.q)}">
    </div>
    ${groups.length ? groups.map(([p, g]) => `
      <div class="lvl-h"><span class="dot" style="background:${LV[p][1]}"></span>${LV[p][0]} <span class="hint" style="margin:0">${g.length}</span></div>
      ${g.map((i) => `<div class="ri" style="--lv:${LV[p][1]}">
        <div class="s">${i.gmailId ? `<a href="https://mail.google.com/mail/u/0/#all/${esc(i.gmailId)}" target="_blank" rel="noopener">${esc(i.subject)}</a>` : esc(i.subject)}
          <span class="topic">${esc(i.topic)}</span></div>
        <div class="sum">${esc(i.summary)}</div>
        <div class="meta">${esc(i.from)} · ${esc(i.date)}${i.priorityReason ? ` · <i>${esc(i.priorityReason)}</i>` : ""}</div>
        ${(i.replyStatus && i.replyStatus !== "Not tracked") || (i.scheduleStatus && i.scheduleStatus !== "N/A") ? `<div class="st">
          ${i.replyStatus && i.replyStatus !== "Not tracked" ? `<span class="${/^Sent/.test(i.replyStatus) ? "dr" : /ready|Drafted/.test(i.replyStatus) ? "rdy" : /deleted/.test(i.replyStatus) ? "del" : ""}">Reply: ${esc(i.replyStatus)}</span>` : ""}
          ${i.scheduleStatus && i.scheduleStatus !== "N/A" ? `<span class="sc">${esc(i.scheduleStatus)}</span>` : ""}</div>` : ""}
      </div>`).join("")}`).join("") : `<p class="hint">Nothing matches the filter.</p>`}`;
  $$("[data-lv]", view).forEach((c) => c.onclick = () => {
    const p = +c.dataset.lv;
    recapFilter.levels.has(p) ? recapFilter.levels.delete(p) : recapFilter.levels.add(p);
    renderRecap();
  });
  const inp = $("#rv-q");
  inp.oninput = () => { recapFilter.q = inp.value; const pos = inp.selectionStart; renderRecap(); const n = $("#rv-q"); n.focus(); n.setSelectionRange(pos, pos); };
}

/* -------------------------------------------------------------------- guide */
let guideLoaded = false;
async function loadGuide() {
  if (guideLoaded) return;
  const box = $("#guide-body");
  try {
    const r = await fetch("/static/guide.html", { cache: "no-store" });
    if (!r.ok) throw new Error("guide not found (" + r.status + ")");
    box.innerHTML = await r.text();
    guideLoaded = true;
    $$(".g-toc a, .g-body a[href^='#']", box).forEach((a) => a.onclick = (ev) => {
      ev.preventDefault();
      box.querySelector(a.getAttribute("href"))?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  } catch (e) { box.innerHTML = `<p class="hint bad">${esc(e.message)}</p>`; }
}

/* ------------------------------------------------------------------ history */
/* Result column: readable key/value chips instead of one long "k=v, k=v" string. */
const RUN_HIDE = new Set(["engine", "model", "execution_id"]);
const RUN_LABEL = { n8n_status: "n8n", llm_calls: "model calls", emails_checked: "emails checked",
  reply_drafts: "reply drafts", meeting_proposals: "with times", booking_notes: "BOOK notes",
  event_notes: "event notes", model_errors: "model errors", tokens_in: "tokens in", tokens_out: "tokens out",
  served_model: "ran on", references_found: "earlier items found", references_missing: "earlier items missing" };
function runResult(r) {
  const s = r.stats || {};
  const chips = [];
  if (s.execution_id) chips.push(`<span class="kv"><b>n8n run</b> #${esc(s.execution_id)}</span>`);
  for (const [k, v] of Object.entries(s)) {
    if (RUN_HIDE.has(k) || v === "" || v === null || v === undefined) continue;
    if (v === 0 && !/reply/.test(k)) continue;              // zero counts are noise; reply drafts 0 is an answer
    if (k === "recap_url") {
      if (/^https?:/.test(String(v))) chips.push(`<a class="kv link" href="${esc(v)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">Recap doc ↗</a>`);
      continue;
    }
    const bad = (k === "errors" || k === "model_errors") && Number(v) > 0;
    chips.push(`<span class="kv${bad ? " warn" : ""}"><b>${esc(RUN_LABEL[k] || k.replace(/_/g, " "))}</b> ${esc(v)}</span>`);
  }
  const err = r.error ? `<div class="run-err" title="${esc(r.error)}">${esc(r.error)}</div>` : "";
  return `<div class="kvs">${chips.join("")}</div>${err}`;
}

async function loadRuns() {
  const rs = await api("/api/runs");
  $("#runs tbody").innerHTML = rs.map((r) => `
    <tr class="clickable" data-id="${r.id}">
      <td class="num">${r.id}</td><td>${esc(r.job_name)}</td><td>${esc(r.runner)}</td>
      <td class="mono-cell" title="${esc(r.model || "")}">${esc(r.model || "—")}</td>
      <td class="${r.status === "succeeded" ? "ok" : ["failed", "interrupted"].includes(r.status) ? "bad" : ""}">${esc(stLabel(r.status))}</td>
      <td class="when">${esc((r.started_at || "").replace("T", " ").slice(0, 16))}</td>
      <td class="result">${runResult(r)}</td>
    </tr>`).join("");
  $$("#runs tbody tr").forEach((tr) => tr.onclick = () => showRun(tr.dataset.id));
}

async function showRun(id) {
  const d = await api(`/api/runs/${id}`);
  $("#run-detail").innerHTML = `
    <h2>Run ${d.id} — ${esc(d.job_name)}</h2>
    <table><thead><tr><th>Pri</th><th>From</th><th>Subject</th><th>Category</th><th>Summary</th><th>Draft</th></tr></thead>
    <tbody>${d.emails.map((e) => `<tr>
      <td><span class="tag ${esc(e.priority)}">${esc(e.priority)}</span></td>
      <td>${esc(e.sender)}</td>
      <td><a href="${esc(e.web_link || "#")}" target="_blank" rel="noopener">${esc(e.subject)}</a></td>
      <td>${esc(e.category)}</td><td>${esc(e.summary)}</td>
      <td>${esc(e.draft_status || "—")}</td></tr>`).join("")}</tbody></table>
    <pre class="log">${esc((d.log || []).join("\n"))}</pre>`;
}

/* ----------------------------------------------------------------- settings */
function setAdvanced(open, save) {
  const body = $("#adv-body"), b = $("#btn-adv");
  if (!body) return;
  body.hidden = !open;
  b.textContent = open ? "Hide" : "Show";
  b.setAttribute("aria-expanded", String(open));
  if (save) api("/api/settings/ui", { method: "PUT", body: JSON.stringify({ showAdvanced: open }) }).catch(() => {});
}
if ($("#btn-adv")) $("#btn-adv").onclick = () => setAdvanced($("#adv-body").hidden, true);
api("/api/settings/ui").then((u) => setAdvanced(!!u.showAdvanced, false)).catch(() => {});
$$(".subnav a, .adv-index a").forEach((a) => a.onclick = (ev) => {
  ev.preventDefault();
  const target = document.querySelector(a.getAttribute("href"));
  if (!target) return;
  if (target.closest("#adv-body") && $("#adv-body").hidden) setAdvanced(true, true);
  if (target.id === "s-advgroup" && $("#adv-body").hidden) setAdvanced(true, true);
  target.scrollIntoView({ behavior: "smooth", block: "start" });
});
const SECRET_GROUPS = [
  ["AI engines", ["anthropic_api_key", "openai_api_key", "gemini_api_key"]],
  ["n8n", ["n8n_api_key"]],
  ["Google", ["google_client_secret_json", "google_token_json"]],
];
/* ------------------------------------------------------- engine sign-in */
const AUTH_PILL = { ok: "signed in", signed_out: "not signed in", missing: "not installed", error: "not working", wait: "checking…" };
async function loadSignin() {
  const box = $("#signin");
  if (!box) return;
  let list;
  try { list = await api("/api/settings/engine-auth"); } catch (e) { box.innerHTML = `<p class="bad">${esc(e.message)}</p>`; return; }
  box.innerHTML = list.map((e) => `
    <div class="auth-row" data-auth="${esc(e.id)}">
      ${logoTile(ENGINE_BY_ID[e.id] || { text: e.label.slice(0, 2), label: e.label }, "sm")}
      <div class="aname">${esc(e.label)}</div>
      <div class="astate"><span class="apill wait">${AUTH_PILL.wait}</span><span class="detail"></span></div>
      <div class="aact"><button class="ghost" data-asignin>Sign in</button><button class="primary" data-averify>Verify</button></div>
      <div class="ares" data-ares></div>
    </div>`).join("");
  wireInfo($("#s-signin"));
  list.forEach((e) => {
    const row = $(`[data-auth="${e.id}"]`), res = $("[data-ares]", row);
    const show = (st) => {
      const pill = $(".apill", row);
      pill.className = "apill " + st.state; pill.textContent = AUTH_PILL[st.state] || st.state;
      if (st.detail != null) $(".detail", $(".astate", row)).textContent = st.detail || "";
    };
    const refresh = () => api(`/api/settings/engine-auth/${e.id}`).then(show).catch((err) => show({ state: "error", detail: err.message }));
    refresh();
    $("[data-asignin]", row).onclick = async (ev) => {
      const b = ev.currentTarget; b.disabled = true;
      try {
        const r = await api(`/api/settings/engine-auth/${e.id}/signin`, { method: "POST", body: "{}" });
        res.innerHTML = `<span class="ok">Terminal opened.</span> ${esc(r.steps)}`;
        let n = 0;                                   // watch for the sign-in to finish (up to 5 minutes)
        const t = setInterval(async () => {
          n += 1;
          try { const st = await api(`/api/settings/engine-auth/${e.id}`); show(st); if (st.state === "ok" || n > 60) clearInterval(t); }
          catch { if (n > 60) clearInterval(t); }
        }, 5000);
      } catch (err) { res.innerHTML = `<span class="bad">${esc(err.message)}</span>`; }
      b.disabled = false;
    };
    $("[data-averify]", row).onclick = async (ev) => {
      const b = ev.currentTarget; b.disabled = true; b.textContent = "verifying…";
      res.innerHTML = `<span class="muted">checking the sign-in and sending one tiny prompt…</span>`;
      try {
        const r = await api(`/api/settings/engine-auth/${e.id}/verify`, { method: "POST", body: "{}" });
        show({ state: r.ok ? "ok" : r.state, detail: null });      // the result line below carries the detail
        res.innerHTML = r.ok ? `<span class="ok">✓ Works.</span> ${esc(r.detail)}` : `<span class="bad">✗ ${esc(r.detail)}</span>`;
      } catch (err) { res.innerHTML = `<span class="bad">✗ ${esc(err.message)}</span>`; }
      b.disabled = false; b.textContent = "Verify";
    };
  });
}

async function loadSettings() {
  loadSignin();
  loadAppSettings();
  loadRulesYaml();
  loadWebhookKey();
  loadDrafting();
  loadAddresses();
  const s = await api("/api/settings/status");
  const g = s.google;
  $("#google-status").innerHTML = g.connected
    ? `<span class="ok">connected</span> — refresh token: ${g.has_refresh_token ? "yes" : "no"}<br><small>${esc((g.scopes || []).join(", "))}</small>`
    : `<span class="bad">not connected</span> — ${esc(g.reason || "")}`;

  const secRow = (k) => `
    <div class="sec-row">
      <span class="sec-name">${esc(s.secret_labels[k])}</span>
      <span class="sec-st ${s.secrets[k] ? "ok" : "bad"}">${s.secrets[k] ? "set" : "not set"}</span>
      <div class="sec-in">
        <input type="password" autocomplete="off" placeholder="${k === "google_client_secret_json" ? "paste the whole JSON, or choose the file" : "paste value"}" data-k="${esc(k)}">
        ${k === "google_client_secret_json" ? `<label class="filebtn ghost">Choose file…<input type="file" accept=".json,application/json" data-file="${esc(k)}" hidden></label>` : ""}
      </div>
      <button class="ghost" data-save="${esc(k)}">Save</button>
    </div>`;
  const groups = SECRET_GROUPS.map(([title, keys]) => [title, keys.filter((k) => k in s.secret_labels)])
    .concat([["Other", Object.keys(s.secret_labels).filter((k) => !SECRET_GROUPS.some(([, ks]) => ks.includes(k)))]])
    .filter(([, keys]) => keys.length);
  $("#secrets").innerHTML = groups.map(([title, keys]) => `
    <div class="sec-group"><h3>${esc(title)}</h3>${keys.map(secRow).join("")}</div>`).join("");

  $$("[data-file]").forEach((inp) => inp.onchange = async () => {
    const f = inp.files?.[0];
    if (!f) return;
    const text = await f.text();
    try { JSON.parse(text); } catch { alert("That file is not valid JSON."); return; }
    await api("/api/settings/secrets", { method: "POST", body: JSON.stringify({ key: inp.dataset.file, value: text }) });
    loadSettings();
  });
  $$("[data-save]").forEach((b) => b.onclick = async () => {
    const inp = $(`[data-k="${b.dataset.save}"]`);
    if (!inp.value) return;
    await api("/api/settings/secrets", { method: "POST", body: JSON.stringify({ key: b.dataset.save, value: inp.value }) });
    inp.value = ""; loadSettings();
  });

  loadModels().catch(() => {});

  const PROV = { claude_cli: "Claude (Claude Code)", codex_cli: "ChatGPT (Codex)", gemini_cli: "Gemini (Antigravity)",
    ollama: "Ollama", anthropic: "Anthropic API", openai: "OpenAI API", gemini: "Google AI Studio API" };
  const RUN = ["claude_cli", "codex_cli", "gemini_cli", "ollama"];
  const card = (p) => `
    <div class="card"><h3>${esc(PROV[p] || p)}</h3>
    <div class="meta" id="h-${esc(p)}">—</div>
    <button class="ghost" data-health="${esc(p)}">Test</button></div>`;
  const api_ = s.providers.filter((p) => !RUN.includes(p));
  $("#providers").innerHTML = `<h3 class="ph">Run engines</h3><div class="cards">${RUN.filter((p) => s.providers.includes(p)).map(card).join("")}</div>`
    + (api_.length ? `<h3 class="ph">API keys <small>(used only by legacy "pipeline" jobs in jobs.yaml, billed per use)</small></h3>
      <div class="cards">${api_.map(card).join("")}</div>` : "");
  $$("[data-health]").forEach((b) => b.onclick = async () => {
    const p = b.dataset.health, cell = $(`#h-${p}`);
    cell.textContent = "testing…";
    try {
      const r = await api(`/api/settings/providers/${p}/health`);
      cell.innerHTML = r.ok ? `<span class="ok">ok</span> · ${esc(r.model)}` : `<span class="bad">${esc(r.detail)}</span>`;
    } catch (e) { cell.innerHTML = `<span class="bad">${esc(e.message)}</span>`; }
  });
}

const ENGINE_BY_ID = { ...ENGINE, n8n: N8N_ENGINE };

async function loadModels() {
  const out = $("#models");
  out.innerHTML = `<p class="hint">Loading…</p>`;
  const r = await api("/api/settings/engine-models");
  renderModels(r.engines);
}

const EFFORT_LABEL = { off: "Off", on: "On", none: "None", minimal: "Minimal", low: "Low", medium: "Medium",
  high: "High", xhigh: "Extra high", max: "Max", ultra: "Ultra" };

function renderModels(engines) {
  const out = $("#models");
  out.innerHTML = engines.map((e) => {
    const ids = e.models.map((m) => m.id);
    const cur = e.chosen || "";
    const isCustom = cur && !ids.includes(cur);
    // One name per option: the model's name; the exact ID is in the "in use" line and the tooltip.
    const dflt = e.id === "n8n" ? "Workflow defaults" : `Default (${e.defaultLabel})`;
    const opts = [`<option value="">${esc(dflt)}</option>`]
      .concat(e.models.map((m) => `<option value="${esc(m.id)}" title="${esc(m.id)}"${m.id === cur ? " selected" : ""}>${esc(m.label || m.id)}</option>`))
      .concat([`<option value="__custom"${isCustom ? " selected" : ""}>Custom…</option>`]).join("");
    const src = e.live ? esc(e.source)
      : `list checked ${esc(e.asOf)} · ${esc(e.source).split(" · ").map((s) => /^https?:/.test(s) ? `<a href="${s}" target="_blank" rel="noopener">source</a>` : s).join(" · ")}`;
    const sv = e.served || {};
    const served = sv.model ? `<span class="served">reported by ${esc(e.label)}: <b>${esc(sv.model)}</b>${sv.effort ? " · effort " + esc(sv.effort) : ""}${sv.at ? " · " + esc(fmtWhen(sv.at)) : ""}</span>` : (e.testable ? `<span class="served muted">exact model confirmed after a Test or a run</span>` : "");
    return `<div class="erow" data-erow="${esc(e.id)}">
      ${logoTile(ENGINE_BY_ID[e.id] || { text: e.label.slice(0, 2), label: e.label }, "sm")}
      <div class="ename">${esc(e.label)}</div>
      <div class="epick">
        <select data-esel aria-label="${esc(e.label)} model">${opts}</select>
        <input data-ecustom placeholder="exact model ID" value="${isCustom ? esc(cur) : ""}"${isCustom ? "" : " hidden"}>
        <select data-eeff class="eeff" aria-label="${esc(e.label)} effort"></select>
        <button class="ghost" data-esave>Save</button>
        ${e.testable ? `<button class="ghost" data-etest>Test</button>` : ""}
      </div>
      <div class="einfo">
        <span>in use: <b>${esc(e.effective)}</b>${e.effort ? ` · effort <b>${esc(e.effort)}</b>` : ""}</span>
        <span class="enote" data-enote></span>
        ${served}
        <span class="esrc">${src}</span>
        ${e.error ? `<span class="bad">${esc(e.error)}</span>` : ""}
        ${e.id === "n8n" ? `<span class="enote">Applies to the n8n Drafting, Events and Recap workflows from their next run.</span>` : ""}
        <span data-eresult></span>
      </div>
    </div>`;
  }).join("");

  engines.forEach((e) => {
    const row = $(`[data-erow="${e.id}"]`);
    const sel = $("[data-esel]", row), custom = $("[data-ecustom]", row), eff = $("[data-eeff]", row);
    const res = $("[data-eresult]", row);
    const value = () => (sel.value === "__custom" ? custom.value.trim() : sel.value);
    // The model the row would run with: the picked one, or the default when "Default setting" is picked.
    const modelId = () => value() || e.default;
    const savedEffort = (mid) => (e.id === "n8n" ? (mid === e.chosen ? e.effort : "") : ((e.efforts || {})[mid] || ""));
    const fillEffort = () => {
      const mid = modelId();
      const m = e.models.find((x) => x.id === mid);
      const levels = m ? m.efforts : (sel.value === "__custom" ? e.customEfforts : []);
      if (!levels || !levels.length) {
        eff.innerHTML = `<option value="">Effort: n/a</option>`; eff.disabled = true; return;
      }
      const dflt = e.id === "n8n" ? "Effort: Off"
        : m && m.defaultEffort ? `Effort: default (${EFFORT_LABEL[m.defaultEffort] || m.defaultEffort})` : "Effort: default";
      const want = savedEffort(mid);
      eff.disabled = false;
      eff.innerHTML = [`<option value="">${esc(dflt)}</option>`].concat(levels.map((l) =>
        `<option value="${esc(l)}"${l === want ? " selected" : ""}>Effort: ${esc(EFFORT_LABEL[l] || l)}</option>`)).join("");
    };
    const note = () => {
      const m = e.models.find((x) => x.id === modelId());
      $("[data-enote]", row).textContent = m && m.note ? m.note : "";
    };
    fillEffort(); note();
    sel.onchange = () => {
      custom.hidden = sel.value !== "__custom"; if (!custom.hidden) custom.focus();
      fillEffort(); note(); res.textContent = "";
    };
    custom.oninput = () => fillEffort();
    $("[data-esave]", row).onclick = async (ev) => {
      const b = ev.currentTarget;
      if (sel.value === "__custom" && !custom.value.trim()) { res.innerHTML = `<span class="bad">type a model ID</span>`; return; }
      b.disabled = true; b.textContent = "saving…";
      try {
        const r = await api(`/api/settings/engine-models/${e.id}`, { method: "PUT", body: JSON.stringify({ model: value(), effort: eff.disabled ? "" : eff.value }) });
        renderModels(r.engines);
        const nrow = $(`[data-erow="${e.id}"] [data-eresult]`);
        if (nrow) nrow.innerHTML = `<span class="ok">saved</span>`;
        loadJobs().catch(() => {});
      } catch (err) { res.innerHTML = `<span class="bad">${esc(err.message)}</span>`; b.disabled = false; b.textContent = "Save"; }
    };
    const tb = $("[data-etest]", row);
    if (tb) tb.onclick = async () => {
      const m = modelId(), ef = eff.disabled ? "" : eff.value;
      tb.disabled = true; res.innerHTML = `<span class="muted">testing ${esc(m)}${ef ? " · " + esc(ef) : ""}…</span>`;
      try {
        const r = await api(`/api/settings/providers/${e.id}/health?model=${encodeURIComponent(m)}${ef ? "&effort=" + encodeURIComponent(ef) : ""}`);
        const sv = r.served || {};
        res.innerHTML = r.ok
          ? `<span class="ok">✓ answered · ran on <b>${esc(sv.model || r.model)}</b>${sv.effort ? " · effort " + esc(sv.effort) : ""}</span>`
          : `<span class="bad">✗ ${esc(r.detail)}</span>`;
      } catch (err) { res.innerHTML = `<span class="bad">${esc(err.message)}</span>`; }
      tb.disabled = false;
    };
  });
}

$("#btn-models").onclick = async () => {
  const b = $("#btn-models");
  b.disabled = true; b.textContent = "refreshing…";
  try { await loadModels(); } catch (e) { $("#models").innerHTML = `<p class="hint bad">${esc(e.message)}</p>`; }
  b.disabled = false; b.textContent = "Refresh";
};

/* ----------------------------------------------------------- your addresses */
const KIND = { main: "main", "send-as": "send-as", forwarded: "forwarded", "received-as": "received as" };
let addrState = null;
function renderAddresses(d) {
  addrState = d;
  const list = d.addresses || [];
  const off = new Set(d.excluded || []);
  const own = list.filter((a) => ["main", "send-as"].includes(a.kinds[0]));
  const other = list.filter((a) => !["main", "send-as"].includes(a.kinds[0]));
  const item = (a) => `<label class="ad-item" title="${esc(a.kinds.map((k) => KIND[k] || k).join(", "))}">
      <input type="checkbox" data-addr="${esc(a.address)}" ${off.has(a.address) ? "" : "checked"}>
      <span class="addr">${esc(a.address)}</span>
      <span class="kind ${esc(a.kinds[0])}">${esc(KIND[a.kinds[0]] || a.kinds[0])}</span>
      <span class="n">${a.count}</span></label>`;
  $("#ad-list").innerHTML = list.length
    ? own.map(item).join("") +
      (other.length ? `<p class="ad-sub">Other addresses that look like yours, inferred from the headers (not verified): forwarding traces of other accounts, and addresses that were the only recipient of at least 2 emails that reached you. Recipients of group emails and mailing lists are not listed. Untick one to ignore mail addressed to it.</p>` + other.map(item).join("") : "")
    : `<p class="hint">No scan yet. Press "Scan my mailbox" (needs Google connected in this app).</p>`;
  const tracked = list.length - off.size;
  $("#ad-summary").textContent = list.length
    ? (off.size ? `${tracked} of ${list.length} tracked` : `all ${list.length} tracked`) +
      (d.scannedAt ? ` · scanned ${ago(d.scannedAt)}` : "") + " · counts are emails in the last 30 days"
    : "";
  const q = $("#ad-query");
  q.classList.toggle("hidden", !d.query);
  q.textContent = d.query ? "Gmail filter added to every search: " + d.query : "";
}
async function loadAddresses() {
  try { renderAddresses(await api("/api/settings/addresses")); }
  catch (e) { $("#ad-list").innerHTML = `<p class="hint bad">${esc(e.message)}</p>`; }
}
$("#btn-ad-scan").onclick = async () => {
  const b = $("#btn-ad-scan");
  b.disabled = true; b.textContent = "scanning…"; $("#ad-note").textContent = "";
  try {
    const d = await api("/api/settings/addresses/scan", { method: "POST" });
    renderAddresses(d);
    $("#ad-note").textContent = `read ${d.scanned} emails`;
  } catch (e) { $("#ad-note").innerHTML = `<span class="bad">${esc(e.message)}</span>`; }
  b.disabled = false; b.textContent = scanLabel;
};
$("#btn-ad-all").onclick = () => $$("#ad-list input[data-addr]").forEach((c) => c.checked = true);
$("#btn-ad-save").onclick = async () => {
  const excluded = $$("#ad-list input[data-addr]").filter((c) => !c.checked).map((c) => c.dataset.addr);
  $("#ad-note").textContent = "saving…";
  try {
    renderAddresses(await api("/api/settings/addresses", { method: "PUT", body: JSON.stringify({ excluded }) }));
    $("#ad-note").textContent = "saved — used from the next run";
  } catch (e) { $("#ad-note").innerHTML = `<span class="bad">${esc(e.message)}</span>`; }
};

/* ----------------------------------------------------------- reply drafting */
const CAT_LABEL = { fulltime: "Full-time job", career: "Professional career", anytime: "Any time" };
function calOptions(cals, selected, emptyLabel) {
  const known = cals.some((c) => c.id === selected);
  return (emptyLabel !== undefined ? `<option value="">${esc(emptyLabel)}</option>` : "") +
    cals.map((c) => `<option value="${esc(c.id)}" ${c.id === selected ? "selected" : ""}>${esc(c.name)}</option>`).join("") +
    (selected && !known ? `<option value="${esc(selected)}" selected>${esc(selected)} (unknown)</option>` : "");
}
function catOptions(selected, emptyLabel) {
  return (emptyLabel !== undefined ? `<option value="">${esc(emptyLabel)}</option>` : "") +
    Object.entries(CAT_LABEL).map(([k, v]) => `<option value="${k}" ${k === selected ? "selected" : ""}>${esc(v)}</option>`).join("");
}
let drafting = null;

function renderDrafting(d) {
  drafting = d;
  $("#rd-error").textContent = "";
  const cals = d.calendars || [];
  const defcat = CAT_LABEL[d.defaultCategory] || "Professional career";
  $("#rd-busy").innerHTML = cals.map((c) => `<label><input type="checkbox" value="${esc(c.id)}" ${d.busyCalendars.includes(c.id) ? "checked" : ""}> ${esc(c.name)}</label>`).join("")
    || `<span class="hint">No calendar list yet — press "Refresh calendar list from Google".</span>`;
  ["fulltime", "career", "anytime"].forEach((k) => {
    $(`#rd-h-${k}`).value = (d.hours || {})[k] || "";
    $(`#rd-book-${k}`).innerHTML = calOptions(cals, (d.bookingCalendars || {})[k] || "", "— choose —");
  });
  $("#rd-workcal").innerHTML = calOptions(cals, d.workCalendar || "", "— none —");
  $("#rd-presence").value = d.presenceMinHours || 4;
  $("#rd-defcat").innerHTML = catOptions(d.defaultCategory || "career");
  $("#rd-new-cat").innerHTML = catOptions("", `Category: ${defcat} (default)`);
  $("#rd-new-cal").innerHTML = calOptions(cals, "", "Calendar: category default");
  $$('input[name="rd-scope"]').forEach((r) => r.checked = r.value === d.draftScope);
  $("#rd-signature").value = d.signatureName || "";
  $("#rd-max").value = d.maxEmails || 25;
  $("#rd-notes").value = d.defaultNotes || "";
  $("#rd-count").textContent = `${d.active_senders} active of ${d.senders.length}` +
    (d.draftScope === "senders" && !d.active_senders ? " — no active sender: no reply drafts will be made" : "");
  $("#rd-senders tbody").innerHTML = d.senders.map((s) => `
    <tr data-id="${s.id}" class="${s.active ? "" : "off"}">
      <td><input type="checkbox" data-f="active" ${s.active ? "checked" : ""}></td>
      <td><input type="text" data-f="matchPatterns" value="${esc(s.matchPatterns)}"></td>
      <td><input type="text" data-f="senderName" value="${esc(s.senderName)}"></td>
      <td><select data-f="category">${catOptions(s.category, `Default (${defcat})`)}</select></td>
      <td><select data-f="calendarId">${calOptions(cals, s.calendarId, "Category default")}</select></td>
      <td><input type="text" data-f="notes" value="${esc(s.notes)}"></td>
      <td><button class="ghost" data-del="${s.id}">Remove</button></td>
    </tr>`).join("") || `<tr><td colspan="7" class="hint">No senders yet.</td></tr>`;
  $$("#rd-senders [data-f]").forEach((inp) => inp.onchange = async () => {
    const id = inp.closest("tr").dataset.id, f = inp.dataset.f;
    const body = { [f]: inp.type === "checkbox" ? inp.checked : inp.value };
    await draftingCall(`/api/settings/drafting/senders/${id}`, "PATCH", body);
  });
  $$("#rd-senders [data-del]").forEach((b) => b.onclick = async () => {
    const row = b.closest("tr"), who = row.querySelector('[data-f="matchPatterns"]').value;
    if (!confirm(`Stop tracking ${who}?`)) return;
    await draftingCall(`/api/settings/drafting/senders/${b.dataset.del}`, "DELETE");
  });
}

async function draftingCall(path, method, body) {
  try {
    const d = await api(path, { method, body: body ? JSON.stringify(body) : undefined });
    renderDrafting(d);
    return d;
  } catch (e) {
    $("#rd-error").textContent = e.message;
    return null;
  }
}

async function loadDrafting() {
  $("#rd-error").textContent = "loading from n8n…";
  await draftingCall("/api/settings/drafting", "GET");
}

$("#btn-rd-save").onclick = async () => {
  const scope = ($$('input[name="rd-scope"]').find((r) => r.checked) || {}).value;
  const booking = {};
  ["fulltime", "career", "anytime"].forEach((k) => { const v = $(`#rd-book-${k}`).value; if (v) booking[k] = v; });
  const body = {
    draftScope: scope, signatureName: $("#rd-signature").value,
    defaultNotes: $("#rd-notes").value, maxEmails: Number($("#rd-max").value) || 25,
    defaultCategory: $("#rd-defcat").value, workCalendar: $("#rd-workcal").value,
    presenceMinHours: Number($("#rd-presence").value) || 4,
    bookingCalendars: booking,
    hours: { fulltime: $("#rd-h-fulltime").value, career: $("#rd-h-career").value, anytime: $("#rd-h-anytime").value },
  };
  const busy = $$("#rd-busy input:checked").map((i) => i.value);
  if ($$("#rd-busy input").length) body.busyCalendars = busy;
  const d = await draftingCall("/api/settings/drafting", "PUT", body);
  if (d) { $("#rd-saved").textContent = "saved " + new Date().toLocaleTimeString(); }
};

$("#btn-rd-add").onclick = async () => {
  const d = await draftingCall("/api/settings/drafting/senders", "POST", {
    matchPatterns: $("#rd-new-match").value, senderName: $("#rd-new-name").value,
    notes: $("#rd-new-notes").value, category: $("#rd-new-cat").value, calendarId: $("#rd-new-cal").value,
  });
  if (d) ["#rd-new-match", "#rd-new-name", "#rd-new-notes"].forEach((k) => $(k).value = "");
};

$("#btn-rd-cals").onclick = async () => {
  const b = $("#btn-rd-cals"); b.disabled = true; b.textContent = "reading Google…";
  await draftingCall("/api/settings/drafting/calendars/refresh", "POST");
  b.disabled = false; b.textContent = "Refresh calendar list from Google";
};

/* -------------------------------------------------------------- webhook key */
async function loadWebhookKey() {
  try {
    const k = await api("/api/settings/n8n/webhook-key");
    const when = k.rotated_at ? new Date(k.rotated_at).toLocaleString() : "";
    $("#wk-status").innerHTML = k.key_stored
      ? `<span class="ok">key set</span> — sent as <code>${esc(k.header)}</code>${when ? ", created " + esc(when) : ""}` +
        (k.job_auth && k.job_auth !== "key" ? ` · <span class="bad">job '${esc(k.job_id)}' does not send it (auth: ${esc(k.job_auth)})</span>` : "")
      : `<span class="bad">no key</span> — anyone who knows the webhook URL can start a run`;
    $("#btn-wk").textContent = k.key_stored ? "Rotate key" : "Create key";
  } catch (e) {
    $("#wk-status").textContent = "Webhook key status unavailable: " + e.message;
  }
}

$("#btn-wk").onclick = async () => {
  const b = $("#btn-wk"), out = $("#wk-result");
  b.disabled = true; const label = b.textContent; b.textContent = "working…";
  out.innerHTML = "";
  try {
    let r;
    try {
      r = await api("/api/settings/n8n/webhook-key", { method: "POST", body: "{}" });
    } catch (e) {
      if (!e.message.startsWith("UNPUBLISHED:")) throw e;
      if (!confirm(e.message.replace("UNPUBLISHED: ", "") + "\n\nPublish them together with the key?")) {
        throw new Error("Cancelled. Nothing was changed.");
      }
      r = await api("/api/settings/n8n/webhook-key", { method: "POST", body: JSON.stringify({ allow_unpublished: true }) });
    }
    out.innerHTML = `<pre class="log">${esc([
      r.ok ? "Done: n8n now requires the key on this webhook." : "Key stored, but verification did not pass:",
      `workflow: ${r.workflow.name} (${r.workflow.id})`,
      `n8n credential: ${r.credential_id}`,
      `workflow updated and re-published: ${r.workflow_changed ? "yes" : "no (already protected)"}`,
      `published version requires the key: ${r.published_requires_key ? "yes" : "NO"}`,
      `request with a wrong key → HTTP ${r.wrong_key_probe_status ?? "not tested"}`,
      r.note ? "\n" + r.note : "",
      `backup of the workflow: ${r.backup}`,
    ].join("\n"))}</pre>`;
  } catch (e) {
    out.innerHTML = `<pre class="log">ERROR: ${esc(e.message)}</pre>`;
  }
  b.disabled = false; b.textContent = label;
  loadWebhookKey();
};

$("#btn-n8n").onclick = async () => {
  const b = $("#btn-n8n"), out = $("#n8n-list");
  b.disabled = true; b.textContent = "looking…";
  out.innerHTML = `<p class="hint">Probing n8n…</p>`;
  try {
    const r = await api("/api/settings/n8n/workflows");
    out.innerHTML = `<div class="n8n-head">${esc(r.detail)} · ${r.count} workflows · ${r.with_webhook} with a webhook</div>` +
      (r.with_webhook ? "" : `<p class="hint bad">No workflow has a Webhook trigger.
        n8n's public API has no "run workflow" endpoint, so add a Webhook node to the
        workflow you want this app to start, activate it, then re-run Discover.</p>`) +
      r.workflows.map((w) => `
        <div class="wf">
          <h4>${esc(w.name)}
            <span class="state ${w.active ? "on" : "off"}">${w.active ? "active" : "inactive"}</span>
          </h4>
          ${w.webhooks.length ? w.webhooks.map((k) => `
            <div class="hook">
              <span class="meth">${esc(k.method)}</span>
              <span class="url">${esc(k.production_url)}</span>
              <button class="ghost" data-copy="${esc(k.production_url)}">Copy</button>
              ${k.connected === false ? `<span class="tag P2">not wired</span>
                <button class="ghost" data-addhook="${esc(w.id)}" data-wfname="${esc(w.name)}">Fix</button>` : ""}
            </div>`).join("")
            : `<div class="none">no webhook · triggers: ${esc((w.triggers || []).join(", "))}
                 <button class="ghost" data-addhook="${esc(w.id)}" data-wfname="${esc(w.name)}">Add webhook trigger</button>
               </div>`}
        </div>`).join("");
    $$("[data-addhook]").forEach((btn) => btn.onclick = async () => {
      const id = btn.dataset.addhook;
      const suggested = btn.dataset.wfname.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
      const path = prompt("Webhook path (the URL segment after /webhook/):", suggested);
      if (!path) return;
      btn.disabled = true;
      try {
        const plan = await api("/api/settings/n8n/plan-webhook", {
          method: "POST", body: JSON.stringify({ workflow: id, path }) });
        const verb = plan.action === "adopt"
          ? `Reuse the existing webhook node "${plan.adopt_node}" on "${plan.workflow.name}"?`
          : `Add a Webhook trigger to "${plan.workflow.name}"?`;
        const msg = plan.already_present
          ? `"${plan.workflow.name}" already serves path "${path}" and is connected. Nothing to do.`
          : [verb, "",
             plan.changes.length ? `changes: ${plan.changes.join(" · ")}` : "",
             `existing triggers: ${plan.existing_triggers.join(", ") || "none"}`,
             `it will feed into: ${plan.connect_to.join(", ") || "nothing (wire it up manually)"}`,
             `URL: ${plan.production_url}`,
             plan.workflow.active
               ? "The workflow is active, so it will be re-activated to register the new path."
               : "NOTE: the workflow is inactive — the URL only responds once you activate it.",
             "", "The current version is backed up before any change."].filter(Boolean).join("\n");
        if (plan.already_present) { alert(msg); btn.disabled = false; return; }
        if (!confirm(msg)) { btn.disabled = false; return; }
        const res = await api("/api/settings/n8n/add-webhook", {
          method: "POST", body: JSON.stringify({ workflow: id, path, activate: false }) });
        alert(res.changed
          ? `${res.adopted_existing ? `Reused "${res.adopted_existing}"` : `Added "${res.node_added}"`}` +
            `\nconnected to: ${(res.connected_to || []).join(", ") || "—"}` +
            `${res.reregistered === true ? "\nre-activated to register the path" : ""}` +
            `\n\nURL: ${res.production_url}\nBackup: ${res.backup}`
          : `No change: ${res.reason}`);
        $("#btn-n8n").click();
      } catch (e) { alert(e.message); btn.disabled = false; }
    });

    $$("[data-copy]").forEach((btn) => btn.onclick = async () => {
      try { await navigator.clipboard.writeText(btn.dataset.copy); btn.textContent = "copied"; }
      catch { btn.textContent = "copy failed"; }
      setTimeout(() => (btn.textContent = "Copy"), 1500);
    });
  } catch (e) { out.innerHTML = `<p class="hint bad">${esc(e.message)}</p>`; }
  b.disabled = false; b.textContent = "Discover workflows";
};

$("#btn-checks").onclick = async () => {
  const b = $("#btn-checks"), out = $("#checks");
  b.disabled = true; b.textContent = "checking…";
  out.innerHTML = `<p class="hint">Contacting each service…</p>`;
  try {
    const r = await api("/api/settings/checks");
    const groups = {};
    r.checks.forEach((c) => (groups[c.group] ||= []).push(c));
    out.innerHTML = Object.entries(groups).map(([g, cs]) => `
      <div class="chk-group">${esc(g)}</div>` + cs.map((c) => `
      <div class="chk ${c.ok ? "pass" : "fail"}">
        <span class="dot"></span>
        <span class="nm">${esc(c.name)}</span>
        <span><span class="dt">${esc(c.detail)}</span>${c.ok || !c.hint ? "" : `<div class="ht">${esc(c.hint)}</div>`}</span>
      </div>`).join("")).join("") +
      `<p class="chk-score">${r.ok} of ${r.total} passing</p>`;
  } catch (e) { out.innerHTML = `<p class="hint bad">${esc(e.message)}</p>`; }
  b.disabled = false; b.textContent = "Run all checks";
};

$("#btn-google").onclick = async () => {
  const b = $("#btn-google"), box = $("#google-status");
  const s = await api("/api/settings/status").catch(() => null);
  if (s && !s.secrets.google_client_secret_json) {
    box.innerHTML = `<span class="bad">No OAuth client yet.</span> Create a
      <b>Desktop app</b> OAuth client in Google Cloud, then load its JSON with
      <b>Choose file…</b> next to <em>Google OAuth desktop client JSON</em> below.
      See <code>docs/CONNECTING.md</code> for the click path.`;
    return;
  }
  b.disabled = true;
  b.textContent = "waiting for consent in your browser…";
  try {
    await api("/api/settings/google/authorize", { method: "POST" });
  } catch (e) {
    box.innerHTML = `<span class="bad">${esc(e.message)}</span>`;
  }
  b.disabled = false;
  b.textContent = "Connect / re-authorize Google";
  loadSettings();
};

loadJobs(); refreshBadge(); resumeIfRunning();


/* -------------------------------------------- settings: every formerly hard-coded value */
const infoBtn = (id) => `<button type="button" class="info" data-info="${id}" aria-label="How to set this" aria-expanded="false">i</button>`;
function wireInfo(root) {
  $$("[data-info]", root).forEach((b) => b.onclick = (ev) => {
    ev.preventDefault();
    const box = $(`#help-${b.dataset.info}`);
    const open = box.hidden;
    box.hidden = !open; b.setAttribute("aria-expanded", String(open)); b.classList.toggle("on", open);
  });
}
const fmtDefault = (f) => Array.isArray(f.default)
  ? (f.kind === "links" ? `${f.default.length} links` : `${f.default.length} keywords`)
  : `${f.default}${f.unit ? " " + f.unit : ""}`;

function fieldInput(f) {
  const id = `as-${f.key}`;
  if (f.kind === "prompt") return `<textarea id="${id}" rows="${f.multiline ? 5 : 2}" data-as="${f.key}" maxlength="${f.max || ""}">${esc(f.value)}</textarea>`;
  if (f.kind === "keywords") return `<textarea id="${id}" rows="7" data-as="${f.key}" spellcheck="false">${esc(f.value.join("\n"))}</textarea>`;
  if (f.kind === "links") return `<div class="ql-editor" id="${id}" data-as="${f.key}"></div>
    <button type="button" class="ghost small" data-ql-add>+ Add link</button>`;
  const num = f.kind === "int" || f.kind === "float";
  return `<span class="as-in"><input id="${id}" data-as="${f.key}" type="${num ? "number" : "text"}" value="${esc(f.value)}"
    ${num ? `min="${f.min}" max="${f.max}" step="${f.step || 1}"` : 'spellcheck="false"'}>${f.unit ? `<span class="unit">${esc(f.unit)}</span>` : ""}</span>`;
}

let appIcons = [];
let scanLabel = "Scan my mailbox";
function linkRow(l) {
  return `<div class="ql-row">
    <input data-l="label" placeholder="Name" value="${esc(l.label || "")}">
    <input data-l="sub" placeholder="Short line (optional)" value="${esc(l.sub || "")}">
    <select data-l="icon">${appIcons.map((i) => `<option${i === (l.icon || "link") ? " selected" : ""}>${i}</option>`).join("")}</select>
    <input data-l="url" class="url" placeholder="https://…  ({account} = your Gmail address)" value="${esc(l.url || "")}" spellcheck="false">
    <button type="button" class="ghost small" data-ql-del title="Remove">✕</button></div>`;
}
function wireLinks(box) {
  $$("[data-ql-del]", box).forEach((b) => b.onclick = () => b.closest(".ql-row").remove());
}

async function loadAppSettings() {
  const host = $("#app-settings");
  if (!host) return;
  let r;
  try { r = await api("/api/settings/app"); } catch (e) { host.innerHTML = `<p class="bad">${esc(e.message)}</p>`; return; }
  appIcons = r.icons || [];
  const root = $("#view-settings");
  host.innerHTML = "";
  r.sections.forEach((s) => {
    const scope = s.fields[0] && s.fields[0].scope === "shared";
    const html = `<section class="panel as-sec" id="s-${s.id}">
      <h2>${esc(s.title)} <span class="scope ${scope ? "shared" : "local"}">${scope ? "shared with n8n" : "this Mac"}</span></h2>
      <p class="hint">${esc(s.intro)}</p>
      ${scope && r.sharedError ? `<p class="bad">n8n not reachable, showing defaults: ${esc(r.sharedError)}</p>` : ""}
      <div class="as-grid${["events", "links", "priority"].includes(s.id) ? " one" : ""}">
      ${s.fields.map((f) => `<div class="as-field" data-field="${f.key}">
        <div class="as-label"><label for="as-${f.key}">${esc(f.label)}</label>${infoBtn(f.key)}
          <span class="as-def">${f.isDefault ? "default" : f.kind === "prompt"
            ? `edited · <button type="button" class="linkbtn" data-reset="${f.key}">reset to default</button>`
            : `default: ${esc(fmtDefault(f))} <button type="button" class="linkbtn" data-reset="${f.key}">reset</button>`}</span></div>
        <div class="as-help" id="help-${f.key}" hidden>${esc(f.help).replace(/\n/g, "<br>")}</div>
        ${fieldInput(f)}
      </div>`).join("")}
      </div>
      <div class="btn-row"><button class="primary" data-as-save="${s.id}">Save</button><span class="hint" data-as-note="${s.id}"></span></div>
    </section>`;
    const slot = $(`[data-as-slot="${s.id}"]`);
    if (slot) slot.innerHTML = html; else host.insertAdjacentHTML("beforeend", html);
  });
  wireInfo(root);
  const fields = Object.fromEntries(r.sections.flatMap((s) => s.fields).map((f) => [f.key, f]));
  scanLabel = `Scan my mailbox (last ${fields.scanDays ? fields.scanDays.value : 30} days)`;
  if (!$("#btn-ad-scan").disabled) $("#btn-ad-scan").textContent = scanLabel;
  const ed = $("#as-quickLinks");
  if (ed) {
    ed.innerHTML = fields.quickLinks.value.map(linkRow).join("");
    wireLinks(ed);
    $("[data-ql-add]").onclick = () => { ed.insertAdjacentHTML("beforeend", linkRow({ icon: "link" })); wireLinks(ed); };
  }
  $$("[data-reset]", root).forEach((b) => b.onclick = () => {
    const f = fields[b.dataset.reset];
    if (f.kind === "keywords") $(`#as-${f.key}`).value = f.default.join("\n");
    else if (f.kind === "links") { ed.innerHTML = f.default.map(linkRow).join(""); wireLinks(ed); }
    else $(`#as-${f.key}`).value = f.default;
  });
  $$("[data-as-save]", root).forEach((b) => b.onclick = async () => {
    const sid = b.dataset.asSave, note = $(`[data-as-note="${sid}"]`);
    const values = {};
    r.sections.find((s) => s.id === sid).fields.forEach((f) => {
      if (f.kind === "keywords") values[f.key] = $(`#as-${f.key}`).value.split("\n").map((x) => x.trim()).filter(Boolean);
      else if (f.kind === "links") values[f.key] = $$(".ql-row", ed).map((row) => Object.fromEntries(
        $$("[data-l]", row).map((i) => [i.dataset.l, i.value.trim()])));
      else values[f.key] = $(`#as-${f.key}`).value.trim();
    });
    b.disabled = true; note.className = "hint"; note.textContent = "saving…";
    try {
      await api("/api/settings/app", { method: "PUT", body: JSON.stringify({ values }) });
      await loadAppSettings();
      const n = $(`[data-as-note="${sid}"]`); n.className = "hint ok"; n.textContent = "saved";
      if (sid === "links") loadQuickLinks();
    } catch (e) { note.className = "hint bad"; note.textContent = e.message; b.disabled = false; }
  });
}

async function loadRulesYaml() {
  const ta = $("#rules-yaml");
  if (!ta) return;
  try { const r = await api("/api/settings/rules-yaml"); ta.value = r.text; } catch (e) { $("#rules-note").textContent = e.message; }
}
if ($("#btn-rules-save")) $("#btn-rules-save").onclick = async () => {
  const note = $("#rules-note");
  note.className = "hint"; note.textContent = "checking…";
  try {
    await api("/api/settings/rules-yaml", { method: "PUT", body: JSON.stringify({ text: $("#rules-yaml").value }) });
    note.className = "hint ok"; note.textContent = "saved (the previous version is kept in data/ as a backup)";
  } catch (e) { note.className = "hint bad"; note.textContent = e.message; }
};
