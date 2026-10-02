/* Dashboard de inventario persistido (Fase 4). Vanilla JS sobre la API interna.
 * Todo texto que viene de la nube (nombres, tags, atributos) se escapa antes de insertarse. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const PAGE = 50;
  const state = { clientId: "", accountId: "", offset: 0, total: 0, scans: [], polling: null };

  /* ---------- utilidades ---------- */
  function esc(value) {
    return String(value === undefined || value === null ? "" : value)
      .replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function icons() { if (window.lucide) window.lucide.createIcons(); }
  function fmtDate(value) { return value ? new Date(value).toLocaleString() : "—"; }
  function fmtMs(ms) { return ms === null || ms === undefined ? "—" : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`; }
  const BADGE = {
    completed: "bg-emerald-100 text-emerald-700", completed_with_warnings: "bg-amber-100 text-amber-700",
    completed_with_errors: "bg-orange-100 text-orange-700", failed: "bg-red-100 text-red-700",
    running: "bg-blue-100 text-blue-700", pending: "bg-slate-200 text-slate-700",
    succeeded: "bg-emerald-100 text-emerald-700", partial: "bg-amber-100 text-amber-700",
    denied: "bg-amber-100 text-amber-700", unavailable: "bg-slate-200 text-slate-700", skipped: "bg-slate-200 text-slate-600",
  };
  function badge(status) {
    return `<span class="inline-flex px-2 py-0.5 rounded-full text-[11px] font-semibold ${BADGE[status] || "bg-slate-200 text-slate-700"}">${esc(status || "—")}</span>`;
  }

  function alert(message, tone) {
    const div = document.createElement("div");
    div.className = (tone === "info"
      ? "border-amber-200 bg-amber-50 text-amber-800" : "border-red-200 bg-red-50 text-red-700") +
      " rounded-xl border px-4 py-3 text-sm";
    div.textContent = message;
    $("alerts").appendChild(div);
  }
  function clearAlerts() { $("alerts").innerHTML = ""; }

  async function api(path, options) {
    const response = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, options || {}));
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try { const body = await response.json(); detail = typeof body.detail === "string" ? body.detail : (body.detail && body.detail.mensaje) || detail; } catch (e) {}
      const error = new Error(detail); error.status = response.status; throw error;
    }
    return response.status === 204 ? null : response.json();
  }
  const base = () => `/api/clients/${state.clientId}/accounts/${state.accountId}`;

  /* ---------- tema ---------- */
  function applyTheme(dark) {
    document.documentElement.classList.toggle("dark", dark);
    try { localStorage.setItem("hc_theme", dark ? "dark" : "light"); } catch (e) {}
  }
  try { applyTheme(localStorage.getItem("hc_theme") === "dark"); } catch (e) {}
  $("btnDark").addEventListener("click", () => applyTheme(!document.documentElement.classList.contains("dark")));

  /* ---------- carga de clientes y cuentas ---------- */
  async function loadClients() {
    clearAlerts();
    let clients;
    try { clients = await api("/api/admin/clients"); }
    catch (err) {
      if (err.status === 404 || err.status === 401) {
        alert("La API interna no está disponible. Arranca la aplicación con INVENTORY_ADMIN_API=true y DATABASE_URL configurada.", "info");
      } else { alert(`No se pudieron cargar los clientes: ${err.message}`); }
      return;
    }
    $("clientSelect").innerHTML = clients.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("");
    if (!clients.length) { alert("No hay clientes. Créalos con: python manage.py client create", "info"); return; }
    state.clientId = clients[0].id;
    await loadOverview();
  }

  async function loadOverview() {
    const overview = await api(`/api/clients/${state.clientId}/overview`);
    $("accountSelect").innerHTML = overview.accounts.map((a) => `<option value="${esc(a.id)}">${esc(a.name)} (${esc(a.status)})</option>`).join("");
    if (!overview.accounts.length) {
      alert("Este cliente no tiene cuentas Huawei. Añádelas con: python manage.py account create", "info");
      state.accountId = ""; return;
    }
    if (!overview.accounts.some((a) => a.id === state.accountId)) state.accountId = overview.accounts[0].id;
    $("accountSelect").value = state.accountId;
    const account = overview.accounts.find((a) => a.id === state.accountId);
    $("accountInfo").innerHTML = `<span class="font-semibold">${esc(account.name)}</span>
      <span class="text-slate-500">· ${account.projects} proyecto(s) ·</span>
      ${account.regions.map((r) => `<span class="px-2 py-0.5 rounded-full bg-slate-100 dark:bg-slate-800 text-xs font-mono">${esc(r)}</span>`).join("") || '<span class="text-slate-500">sin regiones</span>'}`;
    await Promise.all([loadStats(), loadScans(), loadResources(), loadSchedules()]);
  }

  /* ---------- KPIs y distribución ---------- */
  function kpi(label, value, icon, extra) {
    return `<div class="bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 p-4">
      <div class="flex items-center gap-2 text-[11px] font-semibold uppercase text-slate-400"><i data-lucide="${icon}" class="w-3.5 h-3.5"></i>${esc(label)}</div>
      <div class="text-xl font-bold mt-1">${value}</div>${extra ? `<div class="text-xs text-slate-500 mt-1">${extra}</div>` : ""}</div>`;
  }
  function bars(target, data) {
    const entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
    const max = Math.max(1, ...entries.map((e) => e[1]));
    $(target).innerHTML = entries.length ? entries.map(([key, value]) => `
      <div class="flex items-center gap-3 text-xs"><span class="w-32 truncate font-mono" title="${esc(key)}">${esc(key || "(vacío)")}</span>
        <div class="flex-1 h-2.5 rounded bg-slate-100 dark:bg-slate-800"><div class="h-2.5 rounded bg-blue-500" style="width:${(100 * value / max).toFixed(1)}%"></div></div>
        <span class="w-10 text-right font-semibold">${value}</span></div>`).join("")
      : '<p class="text-xs text-slate-500">Sin recursos. Lanza un escaneo.</p>';
  }

  async function loadStats() {
    const stats = await api(`${base()}/stats`);
    const last = stats.last_scan;
    const changes = stats.last_scan_changes || {};
    $("kpis").innerHTML = [
      kpi("Recursos activos", stats.total_active, "boxes"),
      kpi("Eliminados", stats.total_deleted, "trash-2"),
      kpi("Último escaneo", last ? badge(last.status) : "—", "radar", last ? fmtDate(last.finished_at || last.created_at) : "nunca"),
      kpi("Nuevos / modif.", `${(changes.created || 0) + (changes.restored || 0)} / ${changes.updated || 0}`, "git-compare", "en el último escaneo"),
      kpi("Eliminados (últ.)", changes.deleted || 0, "minus-circle", "en el último escaneo"),
      kpi("Errores / avisos", last ? `${last.total_errors} / ${last.total_warnings}` : "—", "alert-triangle"),
    ].join("");
    renderCoverage(stats.last_scan_coverage || []);
    bars("byService", stats.by_service);
    bars("byRegion", stats.by_region);
    $("fService").innerHTML = '<option value="">Todos los servicios</option>' + Object.keys(stats.by_service).map((s) => `<option>${esc(s)}</option>`).join("");
    $("fRegion").innerHTML = '<option value="">Todas las regiones</option>' + Object.keys(stats.by_region).map((r) => `<option>${esc(r)}</option>`).join("");
    icons();
  }

  /* ---------- cobertura por servicio ---------- */
  const COVERAGE = {
    succeeded: ["Completo", "Inventario completo."],
    denied: ["Sin permiso", "El usuario IAM de la cuenta no tiene permiso para este servicio. Se conservan los recursos inventariados antes."],
    partial: ["Parcial", "Solo se pudo inventariar una parte (permiso o paginación incompleta en algunas regiones). Lo no visto no se marca como eliminado."],
    unavailable: ["No disponible", "El servicio no existe en las regiones de la cuenta."],
    failed: ["Error", "Fallo de red, de la API o interno. Revisa el detalle del escaneo."],
    skipped: ["Omitido", "No se ejecutó."], running: ["En curso", ""], pending: ["Pendiente", ""],
  };
  const COVERAGE_BADGE = { succeeded: "completed", denied: "denied", partial: "partial", unavailable: "unavailable", failed: "failed" };
  function renderCoverage(items) {
    $("coverage").innerHTML = items.map((c) => {
      const [label, help] = COVERAGE[c.status] || [c.status, ""];
      const extra = [];
      if (c.regions_affected.length && !c.complete) extra.push(`Regiones: ${c.regions_affected.map(esc).join(", ")}`);
      if (c.iam_actions.length) extra.push(`Acción IAM: <span class="font-mono">${c.iam_actions.map(esc).join(", ")}</span>`);
      return `<div class="rounded-xl border ${c.complete ? "border-slate-200 dark:border-slate-800" : "border-amber-200 dark:border-amber-900/50 bg-amber-50/40 dark:bg-amber-950/10"} px-3 py-2"
          title="${esc(c.message || "")}">
        <div class="flex items-center gap-2"><span class="font-mono text-xs font-semibold uppercase">${esc(c.service)}</span>
          <span class="inline-flex px-2 py-0.5 rounded-full text-[11px] font-semibold ${BADGE[COVERAGE_BADGE[c.status] || c.status] || "bg-slate-200 text-slate-700"}">${esc(label)}</span>
          <span class="ml-auto text-xs text-slate-500">${c.resources} recursos</span></div>
        ${c.complete ? "" : `<p class="text-xs text-slate-600 dark:text-slate-400 mt-1">${esc(help)}</p>`}
        ${extra.length ? `<p class="text-[11px] text-slate-500 mt-0.5">${extra.join(" · ")}</p>` : ""}</div>`;
    }).join("") || '<p class="text-xs text-slate-500">Sin escaneos todavía.</p>';
  }

  /* ---------- programaciones ---------- */
  function fmtEvery(minutes) {
    return minutes % 1440 === 0 ? `${minutes / 1440} d` : minutes % 60 === 0 ? `${minutes / 60} h` : `${minutes} min`;
  }
  async function loadSchedules() {
    let items = [];
    try { items = await api(`${base()}/schedules`); } catch (err) { if (err.status !== 403) throw err; }
    $("schedules").innerHTML = items.map((s) => `<tr>
      <td class="px-4 py-2 font-semibold">${esc(s.name)}</td>
      <td class="px-4 py-2">${s.enabled ? '<span class="text-emerald-600 text-xs font-semibold">Sí</span>' : '<span class="text-slate-500 text-xs">No</span>'}</td>
      <td class="px-4 py-2 text-xs">${esc(fmtEvery(s.interval_minutes))}</td>
      <td class="px-4 py-2 text-xs font-mono">${esc(s.services ? s.services.join(", ") : "todos")}</td>
      <td class="px-4 py-2 text-xs whitespace-nowrap">${esc(s.enabled ? fmtDate(s.next_run_at) : "—")}</td>
      <td class="px-4 py-2" title="${esc(s.last_error_safe || "")}">${s.last_status ? badge(s.last_status) : '<span class="text-xs text-slate-500">nunca</span>'}
        ${s.last_triggered_at ? `<span class="text-xs text-slate-500 ml-1">${esc(fmtDate(s.last_triggered_at))}</span>` : ""}</td></tr>`).join("")
      || '<tr><td colspan="6" class="px-4 py-6 text-center text-xs text-slate-500">Sin programaciones. Crea una con: python manage.py schedule create</td></tr>';
  }

  /* ---------- escaneos ---------- */
  async function loadScans() {
    state.scans = await api(`${base()}/scans?limit=20`);
    $("scanHistory").innerHTML = state.scans.map((s, i) => `<tr>
      <td class="px-4 py-2 font-mono">${s.sequence}</td><td class="px-4 py-2">${badge(s.status)}</td>
      <td class="px-4 py-2 whitespace-nowrap">${esc(fmtDate(s.started_at || s.created_at))}</td>
      <td class="px-4 py-2 text-right">${esc(fmtMs(s.duration_ms))}</td><td class="px-4 py-2 text-right">${s.total_resources}</td>
      <td class="px-4 py-2 text-right text-emerald-600">${s.total_created}</td><td class="px-4 py-2 text-right text-blue-600">${s.total_updated}</td>
      <td class="px-4 py-2 text-right text-red-600">${s.total_deleted}</td><td class="px-4 py-2 text-right">${s.total_errors}</td>
      <td class="px-4 py-2 text-right">${s.total_warnings}</td>
      <td class="px-4 py-2 text-right">${state.scans[i + 1] ? `<button data-compare="${i}" class="text-xs font-semibold text-blue-600 hover:underline">Comparar con #${state.scans[i + 1].sequence}</button>` : ""}</td></tr>`).join("")
      || '<tr><td colspan="11" class="px-4 py-6 text-center text-slate-500">Sin escaneos todavía.</td></tr>';
    const active = state.scans.find((s) => s.status === "pending" || s.status === "running");
    if (active) pollScan(active.id);
  }

  $("scanHistory").addEventListener("click", (event) => {
    const button = event.target.closest("[data-compare]");
    if (!button) return;
    const i = Number(button.dataset.compare);
    compare(state.scans[i + 1], state.scans[i]);
  });

  $("btnScan").addEventListener("click", async () => {
    clearAlerts();
    $("btnScan").disabled = true;
    try {
      const run = await api(`${base()}/scans`, { method: "POST", body: JSON.stringify({}) });
      pollScan(run.id);
    } catch (err) { alert(`No se pudo iniciar el escaneo: ${err.message}`); $("btnScan").disabled = false; }
  });

  function pollScan(scanId) {
    if (state.polling) clearInterval(state.polling);
    $("scanProgress").classList.remove("hidden");
    $("btnScan").disabled = true;
    const tick = async () => {
      let scan;
      try { scan = await api(`/api/scans/${scanId}?client_id=${state.clientId}`); }
      catch (err) { clearInterval(state.polling); state.polling = null; $("btnScan").disabled = false; return; }
      $("scanProgressLabel").innerHTML = `Escaneo #${scan.sequence} · ${badge(scan.status)}`;
      $("scanProgressPct").textContent = `${scan.progress.done}/${scan.progress.total} tareas (${scan.progress.percent}%)`;
      $("scanProgressBar").style.width = `${scan.progress.percent}%`;
      $("scanTasks").innerHTML = scan.tasks.map((t) => `<div class="rounded-lg border border-slate-200 dark:border-slate-800 px-2 py-1.5" title="${esc(t.error_message_safe || "")}">
        <div class="font-mono">${esc(t.service)} · ${esc(t.region)}</div><div class="mt-0.5">${badge(t.status)} <span class="text-slate-500">${t.resource_count}</span></div></div>`).join("");
      if (scan.status !== "pending" && scan.status !== "running") {
        clearInterval(state.polling); state.polling = null; $("btnScan").disabled = false;
        await Promise.all([loadStats(), loadScans(), loadResources()]);
      }
    };
    tick();
    state.polling = setInterval(tick, 2000);
  }

  /* ---------- comparación ---------- */
  const CATEGORY = { added: ["Agregados", "text-emerald-700"], removed: ["Eliminados", "text-red-700"],
    modified: ["Modificados", "text-blue-700"], transient: ["Transitorios", "text-slate-500"] };
  async function compare(from, to) {
    const result = await api(`${base()}/scans/compare?from_scan=${from.id}&to_scan=${to.id}&limit=500`);
    $("diffPanel").classList.remove("hidden");
    $("diffTitle").textContent = `Diferencias: escaneo #${from.sequence} → #${to.sequence}`;
    $("diffExport").href = `${base()}/exports/compare?from_scan=${from.id}&to_scan=${to.id}`;
    $("diffSummary").innerHTML = Object.entries(result.summary.counts).map(([k, v]) =>
      `<span class="px-3 py-1 rounded-full bg-slate-100 dark:bg-slate-800 text-xs font-semibold ${CATEGORY[k][1]}">${CATEGORY[k][0]}: ${v}</span>`).join("");
    $("diffItems").innerHTML = result.items.map((item) => `<div class="py-2">
      <span class="text-xs font-semibold ${CATEGORY[item.category][1]}">${CATEGORY[item.category][0]}</span>
      <span class="font-mono text-xs">${esc(item.service)}</span> <span class="font-semibold">${esc(item.name || item.provider_id)}</span>
      <span class="text-xs text-slate-500">${esc(item.region)}</span>
      ${item.changed_fields.length ? `<ul class="mt-1 ml-4 text-xs text-slate-600 dark:text-slate-400">${item.changed_fields.map((c) =>
        `<li><span class="font-mono">${esc(c.field)}</span>: ${esc(JSON.stringify(c.before))} → ${esc(JSON.stringify(c.after))}</li>`).join("")}</ul>` : ""}
    </div>`).join("") || '<p class="text-xs text-slate-500 py-2">Sin diferencias.</p>';
    $("diffPanel").scrollIntoView({ behavior: "smooth" });
  }
  $("diffClose").addEventListener("click", () => $("diffPanel").classList.add("hidden"));

  /* ---------- recursos ---------- */
  function filters() {
    const params = new URLSearchParams();
    if ($("fSearch").value.trim()) params.set("search", $("fSearch").value.trim());
    if ($("fService").value) params.set("service", $("fService").value);
    if ($("fRegion").value) params.set("region", $("fRegion").value);
    if ($("fSort").value) params.set("sort", $("fSort").value);
    if ($("fState").value === "deleted") params.set("only_deleted", "true");
    if ($("fState").value === "all") params.set("include_deleted", "true");
    return params;
  }

  async function loadResources() {
    if (!state.accountId) return;
    const params = filters();
    params.set("limit", PAGE); params.set("offset", state.offset);
    const page = await api(`${base()}/resources?${params}`);
    state.total = page.total;
    $("resources").innerHTML = page.items.map((r) => `<tr class="hover:bg-blue-50/50 dark:hover:bg-blue-400/5 cursor-pointer" data-id="${esc(r.id)}">
      <td class="px-4 py-2 font-semibold">${esc(r.name || r.provider_id)}${r.deleted_at ? ' <span class="text-[10px] text-red-600">eliminado</span>' : ""}</td>
      <td class="px-4 py-2 font-mono text-xs">${esc(r.service)}</td><td class="px-4 py-2 text-xs">${esc(r.resource_type)}</td>
      <td class="px-4 py-2">${esc(r.status || "—")}</td><td class="px-4 py-2 font-mono text-xs">${esc(r.region || "—")}</td>
      <td class="px-4 py-2 text-xs whitespace-nowrap">${esc(fmtDate(r.last_seen))}</td></tr>`).join("")
      || '<tr><td colspan="6" class="px-4 py-6 text-center text-slate-500">No hay recursos con estos filtros.</td></tr>';
    $("pageInfo").textContent = page.total ? `${state.offset + 1}–${Math.min(state.offset + PAGE, page.total)} de ${page.total}` : "";
    $("prevPage").disabled = state.offset === 0;
    $("nextPage").disabled = state.offset + PAGE >= page.total;
    const query = filters();
    $("expXlsx").href = `${base()}/exports/inventory?${query}&format=xlsx`;
    $("expCsv").href = `${base()}/exports/inventory?${query}&format=csv`;
    $("expSummary").href = `${base()}/exports/summary?format=xlsx`;
  }

  let searchTimer = null;
  $("fSearch").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.offset = 0; loadResources(); }, 300); });
  ["fService", "fRegion", "fState", "fSort"].forEach((id) => $(id).addEventListener("change", () => { state.offset = 0; loadResources(); }));
  $("prevPage").addEventListener("click", () => { state.offset = Math.max(0, state.offset - PAGE); loadResources(); });
  $("nextPage").addEventListener("click", () => { state.offset += PAGE; loadResources(); });

  /* ---------- detalle ---------- */
  $("resources").addEventListener("click", async (event) => {
    const row = event.target.closest("tr[data-id]");
    if (!row) return;
    const r = await api(`${base()}/resources/${row.dataset.id}`);
    $("drawerTitle").textContent = r.name || r.provider_id;
    const field = (label, value) => `<div><dt class="text-[10px] font-bold uppercase text-slate-400">${esc(label)}</dt><dd class="font-mono text-xs break-words">${esc(value === null || value === undefined || value === "" ? "—" : value)}</dd></div>`;
    $("drawerBody").innerHTML = `<dl class="grid grid-cols-2 gap-3">
        ${field("Servicio", r.service)}${field("Tipo", r.resource_type)}${field("ID proveedor", r.provider_id)}${field("Estado", r.status)}
        ${field("Región", r.region)}${field("Enterprise Project", r.enterprise_project_id)}${field("Creado en Huawei", fmtDate(r.provider_created_at))}
        ${field("Primera vez visto", fmtDate(r.first_seen))}${field("Última vez visto", fmtDate(r.last_seen))}${field("Eliminado", r.deleted_at ? fmtDate(r.deleted_at) : "")}</dl>
      <div><h4 class="font-bold text-xs mb-1">Tags</h4>${Object.keys(r.tags).length ? Object.entries(r.tags).map(([k, v]) => `<span class="inline-block mr-1 mb-1 px-2 py-0.5 rounded bg-slate-100 dark:bg-slate-800 text-xs font-mono">${esc(k)}=${esc(v)}</span>`).join("") : '<span class="text-xs text-slate-500">Sin tags</span>'}</div>
      <div><h4 class="font-bold text-xs mb-1">Atributos</h4><pre class="text-[11px] bg-slate-50 dark:bg-slate-950 rounded-lg p-3 overflow-x-auto">${esc(JSON.stringify(r.attributes, null, 2))}</pre></div>
      <div><h4 class="font-bold text-xs mb-1">Historial reciente</h4>${r.recent_changes.map((c) => `<div class="text-xs py-1 border-b border-slate-100 dark:border-slate-800">
        <span class="font-semibold">${esc(c.change_type)}</span> · ${esc(fmtDate(c.created_at))}
        ${c.changed_fields.map((f) => `<div class="ml-3 font-mono">${esc(f.field)}: ${esc(JSON.stringify(f.before))} → ${esc(JSON.stringify(f.after))}</div>`).join("")}</div>`).join("") || '<span class="text-xs text-slate-500">Sin cambios registrados</span>'}</div>`;
    $("drawer").classList.remove("translate-x-full");
    $("drawerOverlay").classList.remove("hidden");
  });
  function closeDrawer() { $("drawer").classList.add("translate-x-full"); $("drawerOverlay").classList.add("hidden"); }
  $("drawerClose").addEventListener("click", closeDrawer);
  $("drawerOverlay").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

  /* ---------- selección ---------- */
  $("clientSelect").addEventListener("change", async () => { state.clientId = $("clientSelect").value; state.accountId = ""; state.offset = 0; clearAlerts(); await loadOverview().catch((e) => alert(e.message)); });
  $("accountSelect").addEventListener("change", async () => { state.accountId = $("accountSelect").value; state.offset = 0; clearAlerts(); await loadOverview().catch((e) => alert(e.message)); });
  $("btnRefresh").addEventListener("click", () => (state.clientId ? loadOverview() : loadClients()).catch((e) => alert(e.message)));

  icons();
  loadClients().catch((err) => alert(err.message));
})();
