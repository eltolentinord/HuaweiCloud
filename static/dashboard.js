/* Dashboard de inventario persistido. Vanilla JS sobre la API interna.
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
  function fmtMs(ms) { return ms === null || ms === undefined ? "—" : ms < 1000 ? `${ms} ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms / 60000)} min`; }
  function fmtAgo(value) {
    if (!value) return "nunca";
    const minutes = Math.round((Date.now() - new Date(value).getTime()) / 60000);
    if (minutes < 1) return "hace un momento";
    if (minutes < 60) return `hace ${minutes} min`;
    if (minutes < 1440) return `hace ${Math.round(minutes / 60)} h`;
    return `hace ${Math.round(minutes / 1440)} días`;
  }
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

  /* Nombres legibles de servicios y estados */
  const SERVICE = {
    ecs: "Servidores (ECS)", evs: "Discos (EVS)", vpc: "Redes (VPC)", eip: "IPs públicas (EIP)",
    obs: "Almacenamiento (OBS)", rds: "Bases de datos (RDS)", elb: "Balanceadores (ELB)", nat: "NAT Gateway",
    vpn: "VPN", cbr: "Backups (CBR)", ces: "Alarmas (CES)", cfw: "Firewall (CFW)", dcs: "Redis (DCS)",
    hss: "Seguridad de hosts (HSS)", waf: "WAF",
  };
  const serviceName = (s) => SERVICE[s] || String(s || "").toUpperCase();

  const STATUS = {
    completed: ["Completado", "green"], completed_with_warnings: ["Con avisos", "amber"],
    completed_with_errors: ["Con errores", "orange"], failed: ["Fallido", "red"],
    running: ["En curso", "blue"], pending: ["En cola", "slate"],
    succeeded: ["Completo", "green"], partial: ["Parcial", "amber"], denied: ["Sin permiso", "amber"],
    unavailable: ["No disponible", "slate"], skipped: ["Omitido", "slate"],
  };
  const TONE = {
    green: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300",
    amber: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
    orange: "bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300",
    red: "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300",
    blue: "bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300",
    slate: "bg-slate-200 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
  };
  function badge(status) {
    const [label, tone] = STATUS[status] || [status || "—", "slate"];
    return `<span class="inline-flex px-2 py-0.5 rounded-full text-[11px] font-semibold whitespace-nowrap ${TONE[tone]}">${esc(label)}</span>`;
  }

  function alert(message, tone) {
    const div = document.createElement("div");
    div.className = (tone === "info"
      ? "border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300"
      : "border-red-200 bg-red-50 text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300") +
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

  /* ---------- pestañas (también enlazables: /dashboard#recursos) ---------- */
  const TAB_HASH = { summary: "resumen", resources: "recursos", history: "historial", schedules: "programaciones" };
  function showTab(name) {
    document.querySelectorAll("#tabs [data-tab]").forEach((b) => {
      const active = b.dataset.tab === name;
      b.classList.toggle("tab-active", active);
      b.setAttribute("aria-selected", active ? "true" : "false");
    });
    document.querySelectorAll("[data-panel]").forEach((p) => p.classList.toggle("hidden", p.dataset.panel !== name));
    try { localStorage.setItem("hc_tab", name); history.replaceState(null, "", `#${TAB_HASH[name]}`); } catch (e) {}
  }
  $("tabs").addEventListener("click", (e) => { const b = e.target.closest("[data-tab]"); if (b) showTab(b.dataset.tab); });
  let initialTab = "summary";
  try { initialTab = localStorage.getItem("hc_tab") || "summary"; } catch (e) {}
  const fromHash = Object.keys(TAB_HASH).find((k) => `#${TAB_HASH[k]}` === location.hash);
  if (fromHash) initialTab = fromHash;
  showTab(document.querySelector(`[data-panel="${initialTab}"]`) ? initialTab : "summary");

  /* ---------- carga de clientes y cuentas ---------- */
  async function loadClients() {
    clearAlerts();
    let clients;
    try { clients = await api("/api/admin/clients"); }
    catch (err) {
      if (err.status === 404 || err.status === 401) {
        setHealth("slate", "info", "La API interna no está activa", "Arranca la aplicación con INVENTORY_ADMIN_API=true y DATABASE_URL configurada.");
      } else { alert(`No se pudieron cargar los clientes: ${err.message}`); }
      return;
    }
    $("clientSelect").innerHTML = clients.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("");
    if (!clients.length) { setHealth("slate", "info", "Todavía no hay clientes", "Créalos con: python manage.py client create"); return; }
    if (!clients.some((c) => c.id === state.clientId)) state.clientId = clients[0].id;
    $("clientSelect").value = state.clientId;
    await loadOverview();
  }

  async function loadOverview() {
    const overview = await api(`/api/clients/${state.clientId}/overview`);
    $("accountSelect").innerHTML = overview.accounts.map((a) => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join("");
    if (!overview.accounts.length) {
      setHealth("slate", "info", "Este cliente no tiene cuentas de Huawei Cloud", "Añádelas con: python manage.py account create");
      state.accountId = ""; $("btnScan").disabled = true; return;
    }
    if (!overview.accounts.some((a) => a.id === state.accountId)) state.accountId = overview.accounts[0].id;
    $("accountSelect").value = state.accountId;
    const account = overview.accounts.find((a) => a.id === state.accountId);
    $("accountInfo").innerHTML = `<i data-lucide="map-pin" class="w-3.5 h-3.5"></i>
      ${plural(account.projects, "proyecto", "proyectos")} en
      ${account.regions.map((r) => `<span class="px-2 py-0.5 rounded-full bg-slate-100 dark:bg-slate-800 font-mono">${esc(r)}</span>`).join("") || "ninguna región"}`;
    $("btnScan").disabled = false;
    await Promise.all([loadStats(), loadScans(), loadResources(), loadSchedules()]);
    icons();
  }

  /* ---------- estado general ---------- */
  const HEALTH = {
    green: ["bg-emerald-50 border-emerald-200 dark:bg-emerald-950/20 dark:border-emerald-900/60", "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/50 dark:text-emerald-300", "check-circle-2"],
    amber: ["bg-amber-50 border-amber-200 dark:bg-amber-950/20 dark:border-amber-900/60", "bg-amber-100 text-amber-700 dark:bg-amber-900/50 dark:text-amber-300", "alert-triangle"],
    red: ["bg-red-50 border-red-200 dark:bg-red-950/20 dark:border-red-900/60", "bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300", "x-circle"],
    blue: ["bg-blue-50 border-blue-200 dark:bg-blue-950/20 dark:border-blue-900/60", "bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300", "loader"],
    slate: ["bg-white border-slate-200 dark:bg-slate-900 dark:border-slate-800", "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300", "info"],
  };
  function setHealth(tone, icon, title, text) {
    const [box, iconBox, defaultIcon] = HEALTH[tone];
    $("health").className = `rounded-2xl border p-4 md:p-5 flex items-start gap-4 ${box}`;
    $("healthIcon").className = `shrink-0 w-11 h-11 rounded-xl grid place-items-center ${iconBox}`;
    $("healthIcon").innerHTML = `<i data-lucide="${icon === "info" ? defaultIcon : icon || defaultIcon}" class="w-5 h-5"></i>`;
    $("healthTitle").textContent = title;
    $("healthText").textContent = text;
    icons();
  }

  function renderHealth(stats) {
    const last = stats.last_scan;
    const coverage = stats.last_scan_coverage || [];
    if (!last) {
      setHealth("slate", "radar", "Esta cuenta aún no se ha escaneado", "Pulsa «Escanear ahora» para obtener el primer inventario. Solo se leen datos; no se modifica nada en Huawei Cloud.");
      return;
    }
    const when = `Último escaneo ${fmtAgo(last.finished_at || last.created_at)} (#${last.sequence}).`;
    if (last.status === "pending" || last.status === "running") {
      setHealth("blue", "loader", "Escaneo en curso…", "El inventario se actualizará al terminar.");
      return;
    }
    const ok = coverage.filter((c) => c.complete).length;
    const denied = coverage.filter((c) => c.status === "denied").map((c) => serviceName(c.service));
    const partial = coverage.filter((c) => c.status === "partial").map((c) => serviceName(c.service));
    const failed = coverage.filter((c) => c.status === "failed").map((c) => serviceName(c.service));
    const parts = [`${ok} de ${coverage.length} servicios inventariados por completo.`];
    if (denied.length) parts.push(`Sin permiso: ${denied.join(", ")}.`);
    if (partial.length) parts.push(`Parciales: ${partial.join(", ")}.`);
    if (failed.length) parts.push(`Con error: ${failed.join(", ")}.`);
    if (last.status === "failed") setHealth("red", "x-circle", "El último escaneo falló", `${when} ${parts.join(" ")}`);
    else if (failed.length) setHealth("red", "x-circle", "Inventario con errores", `${when} ${parts.join(" ")} Revisa el detalle abajo.`);
    else if (denied.length || partial.length) setHealth("amber", "alert-triangle", "Inventario correcto, con servicios sin acceso", `${when} ${parts.join(" ")} Lo ya inventariado de esos servicios se conserva.`);
    else setHealth("green", "check-circle-2", "Inventario al día", `${when} ${parts.join(" ")}`);
  }

  /* ---------- cifras clave ---------- */
  function kpi(label, value, icon, hint) {
    return `<div class="bg-white dark:bg-slate-900 rounded-2xl border border-slate-200 dark:border-slate-800 p-4">
      <div class="flex items-center gap-2 text-xs font-medium text-slate-500"><i data-lucide="${icon}" class="w-4 h-4"></i>${esc(label)}</div>
      <div class="text-2xl font-bold mt-1">${value}</div><div class="text-xs text-slate-500 mt-0.5">${hint}</div></div>`;
  }

  async function loadStats() {
    const stats = await api(`${base()}/stats`);
    const last = stats.last_scan;
    const c = stats.last_scan_changes || {};
    const coverage = stats.last_scan_coverage || [];
    const added = (c.created || 0) + (c.restored || 0);
    $("kpis").innerHTML = [
      kpi("Recursos en la nube", stats.total_active, "boxes",
        stats.total_deleted ? `${stats.total_deleted} eliminados en el historial` : "inventario actual"),
      kpi("Servicios cubiertos", coverage.length ? `${coverage.filter((x) => x.complete).length}<span class="text-base text-slate-400"> / ${coverage.length}</span>` : "—",
        "shield-check", "inventariados por completo"),
      kpi("Cambios en el último escaneo", last ? `<span class="text-emerald-600">+${added}</span> <span class="text-blue-600">~${c.updated || 0}</span> <span class="text-red-600">−${c.deleted || 0}</span>` : "—",
        "git-compare", "nuevos · modificados · eliminados"),
      kpi("Último escaneo", last ? badge(last.status) : "—", "clock", last ? esc(fmtAgo(last.finished_at || last.created_at)) : "nunca"),
    ].join("");
    renderHealth(stats);
    renderCoverage(coverage);
    bars("byService", stats.by_service, serviceName, "fService");
    bars("byRegion", stats.by_region, (r) => r, "fRegion");
    $("fService").innerHTML = '<option value="">Todos los servicios</option>' + Object.keys(stats.by_service).sort().map((s) => `<option value="${esc(s)}">${esc(serviceName(s))}</option>`).join("");
    $("fRegion").innerHTML = '<option value="">Todas las regiones</option>' + Object.keys(stats.by_region).sort().map((r) => `<option>${esc(r)}</option>`).join("");
    icons();
  }

  function bars(target, data, label, filterId) {
    const entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
    const max = Math.max(1, ...entries.map((e) => e[1]));
    $(target).innerHTML = entries.length ? entries.map(([key, value]) => `
      <button data-filter="${esc(filterId)}" data-value="${esc(key)}" class="w-full flex items-center gap-3 text-xs rounded-lg px-1 py-0.5 hover:bg-slate-50 dark:hover:bg-slate-800/60 text-left">
        <span class="w-40 truncate" title="${esc(key)}">${esc(label(key) || "(sin valor)")}</span>
        <span class="flex-1 h-2.5 rounded-full bg-slate-100 dark:bg-slate-800"><span class="block h-2.5 rounded-full bg-blue-500" style="width:${(100 * value / max).toFixed(1)}%"></span></span>
        <span class="w-10 text-right font-semibold">${value}</span></button>`).join("")
      : '<p class="text-xs text-slate-500">Sin recursos todavía.</p>';
  }
  ["byService", "byRegion"].forEach((id) => $(id).addEventListener("click", (e) => {
    const b = e.target.closest("[data-filter]");
    if (!b) return;
    resetFilters();
    $(b.dataset.filter).value = b.dataset.value;
    showTab("resources");
    loadResources();
  }));

  /* ---------- cobertura por servicio ---------- */
  const EXPLAIN = {
    denied: "El usuario IAM de esta cuenta no tiene permiso para leer este servicio. Lo inventariado antes se conserva (no se marca como eliminado).",
    partial: "Solo se pudo leer una parte (por permisos en algunas regiones o una lista incompleta). Lo que no se vio no se marca como eliminado.",
    failed: "Falló por un problema de red, de la API de Huawei o interno. Suele resolverse volviendo a escanear.",
    running: "Todavía se está consultando.", pending: "Pendiente de consultar.", skipped: "No se consultó en este escaneo.",
  };
  function renderCoverage(items) {
    const problems = items.filter((c) => !c.complete);
    const ok = items.filter((c) => c.complete);
    $("coverageProblems").innerHTML = problems.map((c) => {
      const tone = c.status === "failed" ? "border-red-200 bg-red-50/60 dark:border-red-900/60 dark:bg-red-950/20" : "border-amber-200 bg-amber-50/60 dark:border-amber-900/60 dark:bg-amber-950/20";
      const extra = [];
      if (c.regions_affected.length) extra.push(`Región: <span class="font-mono">${c.regions_affected.map(esc).join(", ")}</span>`);
      if (c.iam_actions.length) extra.push(`Permiso necesario: <span class="font-mono">${c.iam_actions.map(esc).join(", ")}</span>`);
      if (c.message) extra.push(`<span title="${esc(c.message)}">Detalle: ${esc(c.message.length > 90 ? c.message.slice(0, 90) + "…" : c.message)}</span>`);
      return `<div class="rounded-xl border ${tone} px-4 py-3">
        <div class="flex flex-wrap items-center gap-2"><span class="font-semibold">${esc(serviceName(c.service))}</span>${badge(c.status)}
          ${c.resources ? `<span class="text-xs text-slate-500">${plural(c.resources, "recurso conservado", "recursos conservados")}</span>` : ""}</div>
        <p class="text-sm text-slate-700 dark:text-slate-300 mt-1">${esc(EXPLAIN[c.status] || "")}</p>
        ${extra.length ? `<p class="text-xs text-slate-500 mt-1 break-words">${extra.join(" · ")}</p>` : ""}</div>`;
    }).join("");
    $("coverageOk").innerHTML = !items.length ? '<p class="text-sm text-slate-500">Aún no hay escaneos.</p>'
      : ok.length ? `<p class="text-xs font-semibold text-slate-500 mb-2 flex items-center gap-1"><i data-lucide="check" class="w-3.5 h-3.5 text-emerald-600"></i>Inventariados por completo (${ok.length})</p>
        <div class="flex flex-wrap gap-1.5">${ok.map((c) => `<span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-slate-50 dark:bg-slate-800/60 border border-slate-200 dark:border-slate-700 text-xs"
          title="${c.status === "unavailable" ? "No existe en estas regiones" : ""}">${esc(serviceName(c.service))}
          <span class="font-semibold ${c.resources ? "text-slate-800 dark:text-slate-100" : "text-slate-400"}">${c.status === "unavailable" ? "n/d" : c.resources}</span></span>`).join("")}</div>`
      : "";
  }

  /* ---------- programaciones ---------- */
  function fmtEvery(minutes) {
    return minutes % 1440 === 0 ? `cada ${plural(minutes / 1440, "día", "días")}` : minutes % 60 === 0 ? `cada ${plural(minutes / 60, "hora", "horas")}` : `cada ${minutes} min`;
  }
  async function loadSchedules() {
    let items = [];
    try { items = await api(`${base()}/schedules`); } catch (err) { if (err.status !== 403) throw err; }
    $("tabCountSchedules").textContent = items.length || "";
    $("schedules").innerHTML = items.map((s) => `<tr>
      <td class="px-4 py-2.5 font-semibold">${esc(s.name)}</td>
      <td class="px-4 py-2.5">${s.enabled ? '<span class="text-emerald-600 text-xs font-semibold">Activa</span>' : '<span class="text-slate-500 text-xs">Pausada</span>'}</td>
      <td class="px-4 py-2.5 text-xs">${esc(fmtEvery(s.interval_minutes))}</td>
      <td class="px-4 py-2.5 text-xs">${esc(s.services ? s.services.map(serviceName).join(", ") : "Todos")}</td>
      <td class="px-4 py-2.5 text-xs whitespace-nowrap">${esc(s.enabled ? fmtDate(s.next_run_at) : "—")}</td>
      <td class="px-4 py-2.5" title="${esc(s.last_error_safe || "")}">${s.last_status ? badge(s.last_status) : '<span class="text-xs text-slate-500">Aún no se ha ejecutado</span>'}
        ${s.last_triggered_at ? `<span class="text-xs text-slate-500 ml-1">${esc(fmtAgo(s.last_triggered_at))}</span>` : ""}</td></tr>`).join("")
      || '<tr><td colspan="6" class="px-4 py-8 text-center text-sm text-slate-500">No hay escaneos automáticos.<br><span class="text-xs">Crea uno con <code class="font-mono">python manage.py schedule create</code></span></td></tr>';
  }

  /* ---------- escaneos ---------- */
  async function loadScans() {
    state.scans = await api(`${base()}/scans?limit=20`);
    $("scanHistory").innerHTML = state.scans.map((s, i) => {
      const changes = s.total_created + s.total_updated + s.total_deleted;
      return `<tr>
      <td class="px-4 py-2.5 font-mono text-xs text-slate-500">${s.sequence}</td><td class="px-4 py-2.5">${badge(s.status)}</td>
      <td class="px-4 py-2.5 whitespace-nowrap text-xs" title="${esc(fmtDate(s.started_at || s.created_at))}">${esc(fmtAgo(s.started_at || s.created_at))}</td>
      <td class="px-4 py-2.5 text-right text-xs">${esc(fmtMs(s.duration_ms))}</td><td class="px-4 py-2.5 text-right font-semibold">${s.total_resources}</td>
      <td class="px-4 py-2.5 text-xs whitespace-nowrap">${changes ? `<span class="text-emerald-600">+${s.total_created}</span> <span class="text-blue-600">~${s.total_updated}</span> <span class="text-red-600">−${s.total_deleted}</span>` : '<span class="text-slate-400">sin cambios</span>'}</td>
      <td class="px-4 py-2.5 text-xs whitespace-nowrap">${s.total_errors ? `<span class="text-red-600">${plural(s.total_errors, "error", "errores")}</span> ` : ""}${s.total_warnings ? `<span class="text-amber-600">${plural(s.total_warnings, "aviso", "avisos")}</span>` : ""}${!s.total_errors && !s.total_warnings ? '<span class="text-slate-400">ninguna</span>' : ""}</td>
      <td class="px-4 py-2.5 text-right">${state.scans[i + 1] ? `<button data-compare="${i}" class="text-xs font-semibold text-blue-600 hover:underline whitespace-nowrap">Ver cambios</button>` : ""}</td></tr>`;
    }).join("") || '<tr><td colspan="8" class="px-4 py-8 text-center text-sm text-slate-500">Todavía no hay escaneos.</td></tr>';
    const active = state.scans.find((s) => s.status === "pending" || s.status === "running");
    if (active) pollScan(active.id);
  }

  $("scanHistory").addEventListener("click", (event) => {
    const button = event.target.closest("[data-compare]");
    if (!button) return;
    const i = Number(button.dataset.compare);
    compare(state.scans[i + 1], state.scans[i]).catch((e) => alert(e.message));
  });

  $("btnScan").addEventListener("click", async () => {
    if (!state.accountId) return;
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
      $("scanProgressLabel").innerHTML = `Escaneo #${scan.sequence} ${badge(scan.status)}`;
      $("scanProgressPct").textContent = `${scan.progress.done} de ${scan.progress.total} consultas (${scan.progress.percent}%)`;
      $("scanProgressBar").style.width = `${scan.progress.percent}%`;
      $("scanTasks").innerHTML = scan.tasks.map((t) => `<div class="rounded-lg border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 px-2 py-1.5" title="${esc(t.error_message_safe || "")}">
        <div class="truncate">${esc(serviceName(t.service))}</div><div class="text-[10px] font-mono text-slate-500">${esc(t.region)}</div>
        <div class="mt-0.5">${badge(t.status)} <span class="text-slate-500">${t.resource_count}</span></div></div>`).join("");
      if (scan.status !== "pending" && scan.status !== "running") {
        clearInterval(state.polling); state.polling = null; $("btnScan").disabled = false;
        $("scanProgress").classList.add("hidden");
        await Promise.all([loadStats(), loadScans(), loadResources()]);
      }
    };
    tick();
    state.polling = setInterval(tick, 2000);
  }

  /* ---------- comparación ---------- */
  const CATEGORY = { added: ["Nuevos", "text-emerald-700 dark:text-emerald-400"], removed: ["Eliminados", "text-red-700 dark:text-red-400"],
    modified: ["Modificados", "text-blue-700 dark:text-blue-400"], transient: ["Aparecieron y desaparecieron", "text-slate-500"] };
  async function compare(from, to) {
    const result = await api(`${base()}/scans/compare?from_scan=${from.id}&to_scan=${to.id}&limit=500`);
    $("diffPanel").classList.remove("hidden");
    $("diffTitle").textContent = `Qué cambió entre el escaneo #${from.sequence} y el #${to.sequence}`;
    $("diffExport").href = `${base()}/exports/compare?from_scan=${from.id}&to_scan=${to.id}`;
    $("diffSummary").innerHTML = Object.entries(result.summary.counts).map(([k, v]) =>
      `<span class="px-3 py-1 rounded-full bg-slate-100 dark:bg-slate-800 text-xs font-semibold ${CATEGORY[k][1]}">${CATEGORY[k][0]}: ${v}</span>`).join("");
    $("diffItems").innerHTML = result.items.map((item) => `<div class="py-2.5">
      <div class="flex flex-wrap items-baseline gap-2"><span class="text-xs font-semibold ${CATEGORY[item.category][1]}">${CATEGORY[item.category][0]}</span>
      <span class="font-semibold">${esc(item.name || item.provider_id)}</span>
      <span class="text-xs text-slate-500">${esc(serviceName(item.service))} · ${esc(item.region)}</span></div>
      ${item.changed_fields.length ? `<ul class="mt-1 ml-4 text-xs text-slate-600 dark:text-slate-400 space-y-0.5">${item.changed_fields.map((c) =>
        `<li><span class="font-mono">${esc(c.field)}</span>: <span class="line-through opacity-70">${esc(JSON.stringify(c.before))}</span> → ${esc(JSON.stringify(c.after))}</li>`).join("")}</ul>` : ""}
    </div>`).join("") || '<p class="text-sm text-slate-500 py-3">No hubo cambios entre estos dos escaneos.</p>';
    icons();
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
  function resetFilters() {
    $("fSearch").value = ""; $("fService").value = ""; $("fRegion").value = ""; $("fState").value = "active"; $("fSort").value = "";
    state.offset = 0;
  }

  async function loadResources() {
    if (!state.accountId) return;
    const params = filters();
    params.set("limit", PAGE); params.set("offset", state.offset);
    const page = await api(`${base()}/resources?${params}`);
    state.total = page.total;
    $("tabCountResources").textContent = page.total;
    $("resources").innerHTML = page.items.map((r) => `<tr class="hover:bg-blue-50/50 dark:hover:bg-blue-400/5 cursor-pointer" data-id="${esc(r.id)}">
      <td class="px-4 py-2.5"><div class="font-semibold">${esc(r.name || r.provider_id)}${r.deleted_at ? ' <span class="ml-1 px-1.5 py-0.5 rounded bg-red-100 dark:bg-red-900/40 text-[10px] text-red-700 dark:text-red-300">eliminado</span>' : ""}</div>
        <div class="text-[11px] text-slate-500 font-mono truncate max-w-xs">${esc(r.resource_type)}</div></td>
      <td class="px-4 py-2.5 text-xs">${esc(serviceName(r.service))}</td>
      <td class="px-4 py-2.5 text-xs">${esc(r.status || "—")}</td><td class="px-4 py-2.5 font-mono text-xs">${esc(r.region || "—")}</td>
      <td class="px-4 py-2.5 text-xs whitespace-nowrap" title="${esc(fmtDate(r.last_seen))}">${esc(fmtAgo(r.last_seen))}</td></tr>`).join("")
      || '<tr><td colspan="5" class="px-4 py-8 text-center text-sm text-slate-500">No hay recursos con estos filtros.</td></tr>';
    $("pageInfo").textContent = page.total ? `Mostrando ${state.offset + 1}–${Math.min(state.offset + PAGE, page.total)} de ${page.total}` : "";
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
  $("fClear").addEventListener("click", () => { resetFilters(); loadResources(); });
  $("prevPage").addEventListener("click", () => { state.offset = Math.max(0, state.offset - PAGE); loadResources(); });
  $("nextPage").addEventListener("click", () => { state.offset += PAGE; loadResources(); });

  /* ---------- detalle ---------- */
  const CHANGE = { created: "Apareció", updated: "Cambió", deleted: "Desapareció", restored: "Reapareció" };
  $("resources").addEventListener("click", async (event) => {
    const row = event.target.closest("tr[data-id]");
    if (!row) return;
    const r = await api(`${base()}/resources/${row.dataset.id}`);
    $("drawerTitle").textContent = r.name || r.provider_id;
    const field = (label, value) => `<div><dt class="text-[10px] font-bold uppercase tracking-wide text-slate-400">${esc(label)}</dt><dd class="text-xs break-words mt-0.5">${esc(value === null || value === undefined || value === "" ? "—" : value)}</dd></div>`;
    $("drawerBody").innerHTML = `
      <section><h4 class="font-bold text-xs uppercase tracking-wide text-slate-500 mb-2">General</h4><dl class="grid grid-cols-2 gap-3">
        ${field("Servicio", serviceName(r.service))}${field("Tipo", r.resource_type)}${field("Estado", r.status)}${field("Región", r.region)}
        ${field("ID en Huawei", r.provider_id)}${field("Enterprise Project", r.enterprise_project_id)}</dl></section>
      <section><h4 class="font-bold text-xs uppercase tracking-wide text-slate-500 mb-2">Fechas</h4><dl class="grid grid-cols-2 gap-3">
        ${field("Creado en Huawei", fmtDate(r.provider_created_at))}${field("Visto por primera vez", fmtDate(r.first_seen))}
        ${field("Visto por última vez", fmtDate(r.last_seen))}${field("Eliminado", r.deleted_at ? fmtDate(r.deleted_at) : "")}</dl></section>
      <section><h4 class="font-bold text-xs uppercase tracking-wide text-slate-500 mb-2">Etiquetas</h4>${Object.keys(r.tags).length ? Object.entries(r.tags).map(([k, v]) => `<span class="inline-block mr-1 mb-1 px-2 py-0.5 rounded bg-slate-100 dark:bg-slate-800 text-xs font-mono">${esc(k)}=${esc(v)}</span>`).join("") : '<span class="text-xs text-slate-500">Sin etiquetas</span>'}</section>
      <section><h4 class="font-bold text-xs uppercase tracking-wide text-slate-500 mb-2">Historial</h4>${r.recent_changes.map((c) => `<div class="text-xs py-1.5 border-b border-slate-100 dark:border-slate-800">
        <span class="font-semibold">${esc(CHANGE[c.change_type] || c.change_type)}</span> <span class="text-slate-500">· ${esc(fmtDate(c.created_at))}</span>
        ${c.changed_fields.map((f) => `<div class="ml-3 font-mono">${esc(f.field)}: ${esc(JSON.stringify(f.before))} → ${esc(JSON.stringify(f.after))}</div>`).join("")}</div>`).join("") || '<span class="text-xs text-slate-500">Sin cambios registrados</span>'}</section>
      <details><summary class="font-bold text-xs uppercase tracking-wide text-slate-500 cursor-pointer">Datos técnicos</summary>
        <pre class="mt-2 text-[11px] bg-slate-50 dark:bg-slate-950 rounded-lg p-3 overflow-x-auto">${esc(JSON.stringify(r.attributes, null, 2))}</pre></details>`;
    $("drawer").classList.remove("translate-x-full");
    $("drawerOverlay").classList.remove("hidden");
  });
  function closeDrawer() { $("drawer").classList.add("translate-x-full"); $("drawerOverlay").classList.add("hidden"); }
  $("drawerClose").addEventListener("click", closeDrawer);
  $("drawerOverlay").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

  /* ---------- selección ---------- */
  function reload() { $("diffPanel").classList.add("hidden"); clearAlerts(); return loadOverview().catch((e) => alert(e.message)); }
  $("clientSelect").addEventListener("change", () => { state.clientId = $("clientSelect").value; state.accountId = ""; resetFilters(); reload(); });
  $("accountSelect").addEventListener("change", () => { state.accountId = $("accountSelect").value; resetFilters(); reload(); });
  $("btnRefresh").addEventListener("click", () => (state.clientId ? reload() : loadClients()).catch((e) => alert(e.message)));

  icons();
  loadClients().catch((err) => alert(err.message));
})();
