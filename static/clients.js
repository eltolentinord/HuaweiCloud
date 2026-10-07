/* Mis clientes (tenants de la plataforma) y su entorno Huawei Cloud.
 * - Cliente/Tenant = ESTA plataforma. Cuenta, IAM, Projects, Regions y Enterprise Projects = HUAWEI CLOUD.
 * - Solo lectura sobre Huawei Cloud: conectar (validar IAM), descubrir y escanear con el motor existente.
 * - Todo texto que viene de la nube o de la base se escapa. AK/SK solo se envían al crear la
 *   cuenta y nunca se muestran ni se guardan en el navegador. Ninguna cifra se inventa: si un dato
 *   no existe se muestra "Sin datos" / "nunca". */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const SERVICE = {
    ecs: "Servidores (ECS)", evs: "Discos (EVS)", vpc: "Redes (VPC)", eip: "IPs públicas (EIP)",
    obs: "Almacenamiento (OBS)", rds: "Bases de datos (RDS)", elb: "Balanceadores (ELB)", nat: "NAT Gateway",
    vpn: "VPN", cbr: "Backups (CBR)", ces: "Alarmas (CES)", cfw: "Firewall (CFW)", dcs: "Redis (DCS)",
    hss: "Seguridad de hosts (HSS)", waf: "WAF",
  };
  const PERM = {
    read: ["✓", "Lectura", "text-emerald-700 bg-emerald-50 dark:bg-emerald-950/30 dark:text-emerald-300"],
    denied: ["⚠", "Permiso denegado", "text-amber-800 bg-amber-50 dark:bg-amber-950/30 dark:text-amber-300"],
    partial: ["◐", "Parcial", "text-amber-800 bg-amber-50 dark:bg-amber-950/30 dark:text-amber-300"],
    unavailable: ["○", "No disponible en la región", "text-slate-600 bg-slate-100 dark:bg-slate-800 dark:text-slate-300"],
    error: ["✕", "Error al consultar", "text-red-700 bg-red-50 dark:bg-red-950/30 dark:text-red-300"],
    pending: ["…", "En curso", "text-blue-700 bg-blue-50 dark:bg-blue-950/30 dark:text-blue-300"],
    not_checked: ["—", "Sin comprobar", "text-slate-500 bg-slate-50 dark:bg-slate-800/60 dark:text-slate-400"],
  };
  const PERM_ORDER = { denied: 0, partial: 1, error: 2, pending: 3, unavailable: 4, read: 5, not_checked: 6 };
  const STEP = { ok: ["Encontrados", "text-emerald-700 dark:text-emerald-400"], denied: ["Permiso insuficiente", "text-amber-700 dark:text-amber-400"],
    unavailable: ["No disponible", "text-slate-500"], failed: ["Error", "text-red-700 dark:text-red-400"], skipped: ["No intentado", "text-slate-500"] };
  const SCAN_STATUS = { completed: "completado", completed_with_warnings: "completado con avisos", completed_with_errors: "completado con errores",
    failed: "fallido", running: "en curso", pending: "en cola" };
  const TONE = {
    green: ["bg-emerald-500", "bg-emerald-50 text-emerald-700 ring-emerald-200 dark:bg-emerald-950/40 dark:text-emerald-300 dark:ring-emerald-900"],
    amber: ["bg-amber-500", "bg-amber-50 text-amber-800 ring-amber-200 dark:bg-amber-950/40 dark:text-amber-300 dark:ring-amber-900"],
    red: ["bg-red-500", "bg-red-50 text-red-700 ring-red-200 dark:bg-red-950/40 dark:text-red-300 dark:ring-red-900"],
    blue: ["bg-blue-500", "bg-blue-50 text-blue-700 ring-blue-200 dark:bg-blue-950/40 dark:text-blue-300 dark:ring-blue-900"],
    slate: ["bg-slate-400", "bg-slate-100 text-slate-600 ring-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:ring-slate-700"],
  };
  const SEVERITY = { red: 4, amber: 3, blue: 2, slate: 1, green: 0 };
  const state = { clients: [], client: null, accounts: [], accountId: "", projects: [], regions: [], eps: [],
    permissions: null, selection: { kind: "all" }, summary: null, busy: null, filter: "", showNewClient: false,
    service: "", resources: null, tab: (location.hash || "#resumen").slice(1), confirmDelete: false };
  const LIST_LIMIT = 25;

  /* ---------- utilidades ---------- */
  function esc(value) {
    return String(value === undefined || value === null ? "" : value)
      .replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function icons() { if (window.lucide) window.lucide.createIcons(); }
  function fmtDate(value) { return value ? new Date(value).toLocaleString() : "—"; }
  function fmtAgo(value) {
    if (!value) return "nunca";
    const minutes = Math.round((Date.now() - new Date(value).getTime()) / 60000);
    if (minutes < 1) return "hace un momento";
    if (minutes < 60) return `hace ${minutes} min`;
    if (minutes < 1440) return `hace ${Math.round(minutes / 60)} h`;
    return `hace ${Math.round(minutes / 1440)} días`;
  }
  const serviceName = (s) => SERVICE[s] || String(s || "").toUpperCase();
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  function initials(name) {
    const words = String(name || "?").split(/\s+/).filter((w) => w && !/^cliente$/i.test(w));
    const text = words.length > 1 ? words[0][0] + words[1][0] : (words[0] || "?").slice(0, 2);
    return text.toUpperCase();
  }
  const shortId = (id) => { const s = String(id || ""); return s.length > 14 ? `${s.slice(0, 8)}…` : s; };

  /** ID técnico discreto: abreviado, completo en el tooltip y con botón de copiar. Nunca secretos. */
  function techId(label, id) {
    if (!id && id !== 0) return "";
    return `<span class="inline-flex items-center gap-1 max-w-full text-[11px] text-slate-500 dark:text-slate-400">
      ${label ? `<span>${esc(label)}</span>` : ""}<code class="font-mono px-1.5 py-0.5 rounded bg-slate-100 dark:bg-slate-800 truncate" title="${esc(id)}">${esc(shortId(id))}</code>
      <button type="button" data-copy="${esc(id)}" title="Copiar ${esc(label || "ID")}" class="p-0.5 rounded hover:bg-slate-100 dark:hover:bg-slate-800"><i data-lucide="copy" class="w-3 h-3"></i></button></span>`;
  }
  const regionCode = (code) => `<code class="font-mono text-[11px] px-1.5 py-0.5 rounded bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300">${esc(code)}</code>`;
  function pill(tone, label) {
    const cls = { green: "badge-green", amber: "badge-amber", red: "badge-red", blue: "badge-blue", slate: "badge-slate" }[tone] || "badge-slate";
    return `<span class="badge ${cls}"><span class="dot ${(TONE[tone] || TONE.slate)[0]}"></span>${esc(label)}</span>`;
  }

  function notify(message, tone) {
    const map = {
      error: ["badge-red", "alert-circle", "border-red-200 dark:border-red-900"],
      info: ["badge-amber", "info", "border-amber-200 dark:border-amber-900"],
      ok: ["badge-green", "check-circle-2", "border-emerald-200 dark:border-emerald-900"],
    };
    const [badgeCls, icon, border] = map[tone || "error"];
    const div = document.createElement("div");
    div.setAttribute("role", tone === "error" ? "alert" : "status");
    div.className = `card ${border} flex items-start gap-3 px-4 py-3 text-sm`;
    div.innerHTML = `<span class="badge ${badgeCls} !px-1.5"><i data-lucide="${icon}" class="w-4 h-4"></i></span><span class="pt-0.5 min-w-0 break-words"></span>
      <button type="button" class="ml-auto text-slate-400 hover:text-slate-600" title="Cerrar" aria-label="Cerrar">&times;</button>`;
    div.querySelector("span.pt-0\\.5").textContent = message;
    div.querySelector("button").addEventListener("click", () => div.remove());
    $("alerts").appendChild(div);
    icons();
  }

  function clearAlerts() { $("alerts").innerHTML = ""; }

  async function api(path, options) {
    const response = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, options || {}));
    const body = response.status === 204 ? null : await response.json().catch(() => null);
    if (!response.ok) {
      const detail = body && body.detail;
      const message = typeof detail === "string" ? detail
        : (detail && (detail.mensaje || (Array.isArray(detail) && detail.map((d) => d.msg).join("; ")))) || `HTTP ${response.status}`;
      const error = new Error(message); error.status = response.status; throw error;
    }
    return body;
  }

  /* ---------- tema ---------- */
  function applyTheme(dark) {
    document.documentElement.classList.toggle("dark", dark);
    try { localStorage.setItem("hc_theme", dark ? "dark" : "light"); } catch (e) {}
  }
  try { applyTheme(localStorage.getItem("hc_theme") === "dark"); } catch (e) {}
  $("btnDark").addEventListener("click", () => applyTheme(!document.documentElement.classList.contains("dark")));

  /* ---------- estados (solo con datos reales de la API) ---------- */
  function accountState(acc) {
    if (!acc) return { tone: "slate", label: "Sin cuenta Huawei", detail: "Agrega una cuenta Huawei Cloud para empezar." };
    const steps = Object.values(acc.discovery_steps || {});
    if (acc.status === "invalid") return { tone: "red", label: "Credenciales rechazadas", detail: acc.last_validation_error || "Huawei Cloud no aceptó la AK/SK." };
    if (acc.last_scan_auth_failed) return { tone: "red", label: "Credenciales rechazadas", detail: "Huawei Cloud las rechazó en el último escaneo. Reemplaza la AK/SK y pulsa «Conectar»." };
    if (acc.status === "disabled") return { tone: "slate", label: "Deshabilitada", detail: "" };
    if (steps.length && steps.every((s) => s.status === "failed" || s.status === "skipped")) {
      return { tone: "red", label: "Discovery con errores", detail: "El último descubrimiento no pudo completarse. Vuelve a intentarlo." };
    }
    if (acc.status !== "active") return { tone: "amber", label: "Credenciales configuradas", detail: "Sin validar todavía: pulsa «Conectar Huawei Cloud»." };
    if (steps.some((s) => s.status === "failed")) return { tone: "amber", label: "Discovery con errores", detail: "Parte del descubrimiento falló." };
    if (steps.some((s) => s.status === "denied") || acc.limited_services.length) {
      return { tone: "amber", label: "Permisos limitados", detail: "Conectada; la identidad IAM no puede leer todo." };
    }
    return { tone: "green", label: "Conectada", detail: acc.last_validated_at ? `Validada ${fmtAgo(acc.last_validated_at)}` : "" };
  }
  function clientState(client) {
    if (!client.accounts.length) return accountState(null);
    return client.accounts.map(accountState).sort((a, b) => SEVERITY[b.tone] - SEVERITY[a.tone])[0];
  }
  const latest = (values) => values.filter(Boolean).sort().slice(-1)[0] || null;

  /* ---------- vistas de carga y error ---------- */
  function loadingView(kind) {
    const line = (w, h) => `<div class="skeleton ${h || "h-3"}" style="width:${w}"></div>`;
    const card = (h) => `<div class="card p-5 space-y-3 ${h}">${line("35%")}${line("70%", "h-6")}${line("50%")}</div>`;
    $("view").innerHTML = `<p class="sr-only" role="status">Cargando…</p>${kind === "client"
      ? `<div class="card p-5 flex items-center gap-4"><div class="skeleton w-12 h-12 !rounded-xl"></div><div class="flex-1 space-y-2">${line("20%")}${line("40%", "h-5")}</div></div>
         <div class="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">${card("").repeat(6)}</div>`
      : `<div class="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">${card("").repeat(6)}</div>
         <div class="grid grid-cols-1 md:grid-cols-2 2xl:grid-cols-3 gap-4">${card("h-52").repeat(3)}</div>`}`;
  }

  function errorView(title, message) {
    $("view").innerHTML = `<div class="card empty-state state-error">
      <div class="empty-icon"><i data-lucide="cloud-off" class="w-5 h-5"></i></div>
      <h2 class="font-bold text-slate-900 dark:text-white">${esc(title)}</h2><p class="mt-1 text-sm">${esc(message)}</p>
      <div class="mt-4 flex flex-wrap justify-center gap-2">
        <button type="button" data-retry class="btn-secondary"><i data-lucide="refresh-cw" class="w-4 h-4"></i>Reintentar</button>
        <a href="/clientes" class="btn-ghost"><i data-lucide="arrow-left" class="w-4 h-4"></i>Volver al inicio</a></div></div>`;
    icons();
  }

  /* =====================================================================
   * PORTAFOLIO
   * ===================================================================== */
  async function loadPortfolio() {
    $("breadcrumb").innerHTML = `<span>Huawei Cloud Inventory</span>`;
    $("title").textContent = "Inicio";
    $("clientNav").classList.add("hidden");
    loadingView("list");
    try { state.clients = await api("/api/clients"); }
    catch (err) {
      errorView("No se pudieron cargar los clientes", err.status === 404 || err.status === 401
        ? "La API interna no está activa. Arranca la aplicación con INVENTORY_ADMIN_API=true y DATABASE_URL configurada."
        : err.message);
      return;
    }
    renderPortfolio();
  }

  function kpi(label, value, hint, icon) {
    return `<div class="kpi"><span class="kpi-icon"><i data-lucide="${icon || "circle"}" class="w-5 h-5"></i></span>
      <div class="min-w-0"><div class="kpi-label">${esc(label)}</div><div class="kpi-value">${value}</div>${hint ? `<div class="kpi-hint">${hint}</div>` : ""}</div></div>`;
  }

  function clientCard(c) {
    const st = clientState(c);
    const limited = c.accounts.reduce((n, a) => n + a.limited_services.length, 0);
    const hasAccount = c.accounts.length > 0;
    const num = (n) => (hasAccount ? esc(n) : '<span class="text-slate-300 dark:text-slate-600">—</span>');
    const accountLine = !hasAccount ? "Sin cuenta Huawei Cloud"
      : c.accounts.length === 1 ? `Cuenta: ${c.accounts[0].name}` : `${c.accounts.length} cuentas Huawei Cloud`;
    const scan = c.last_scan;
    const stat = (label, value, title) => `<div class="min-w-0" title="${title || label}"><dt class="text-[11px] text-slate-500 truncate">${label}</dt><dd class="text-lg font-bold tabular-nums">${value}</dd></div>`;
    return `<article class="card card-hover flex flex-col p-5 min-w-0">
      <div class="flex items-start gap-3 min-w-0">
        <div class="shrink-0 w-11 h-11 rounded-xl bg-blue-50 dark:bg-blue-950/40 text-blue-700 dark:text-blue-300 grid place-items-center font-bold">${esc(initials(c.name))}</div>
        <div class="min-w-0 flex-1">
          <h3 class="font-bold leading-snug line-clamp-2 break-words text-slate-900 dark:text-white" title="${esc(c.name)}">${esc(c.name)}</h3>
          <p class="text-xs text-slate-500 truncate flex items-center gap-1"><i data-lucide="cloud" class="w-3.5 h-3.5 shrink-0"></i><span class="truncate">${esc(accountLine)}</span></p>
        </div>
      </div>
      <div class="mt-3 flex flex-wrap gap-1.5">${pill(st.tone, st.label)}${limited ? `<span class="badge badge-amber">${plural(limited, "servicio sin permiso", "servicios sin permiso")}</span>` : ""}</div>
      <dl class="mt-4 grid grid-cols-4 gap-2 rounded-xl bg-slate-50 dark:bg-slate-800/40 px-3 py-2.5">
        ${stat("Projects", num(c.projects))}${stat("EPs", num(c.enterprise_projects), "Enterprise Projects")}${stat("Regiones", num(c.regions.length))}${stat("Recursos", num(c.resources))}
      </dl>
      <div class="mt-4 flex items-center gap-3 text-xs text-slate-500">
        <span class="min-w-0 truncate"><i data-lucide="scan-search" class="inline w-3.5 h-3.5 -mt-0.5"></i> Último escaneo: ${esc(scan ? fmtAgo(scan.finished_at || scan.created_at) : "nunca")}</span>
        <a href="?client=${encodeURIComponent(c.id)}" data-open="${esc(c.id)}" class="ml-auto shrink-0 btn-primary btn-sm">Entrar<i data-lucide="arrow-right" class="w-3.5 h-3.5"></i></a>
      </div>
    </article>`;
  }

  function activityFeed() {
    // Solo eventos reales que la API ya devuelve: último escaneo y último discovery de cada cuenta.
    const events = [];
    state.clients.forEach((c) => c.accounts.forEach((a) => {
      if (a.last_scan) {
        const sc = a.last_scan, when = sc.finished_at || sc.created_at;
        const tone = sc.status === "completed" ? "green" : sc.status === "failed" || sc.status === "completed_with_errors" ? "red"
          : sc.status === "completed_with_warnings" ? "amber" : "blue";
        events.push({ when, icon: "scan-search", tone, title: `Escaneo #${sc.sequence} ${SCAN_STATUS[sc.status] || sc.status}`, who: `${c.name} · ${a.name}`, client: c.id });
      }
      if (a.last_discovery_at) {
        const steps = Object.values(a.discovery_steps || {});
        const tone = steps.every((x) => x.status === "ok") ? "green" : steps.some((x) => x.status === "ok" || x.status === "denied") ? "amber" : "red";
        events.push({ when: a.last_discovery_at, icon: "radar", tone, title: `Discovery ${tone === "green" ? "completo" : tone === "amber" ? "parcial" : "con errores"}`,
          who: `${c.name} · ${a.name}`, client: c.id });
      }
    }));
    events.sort((x, y) => String(y.when).localeCompare(String(x.when)));
    if (!events.length) return `<div class="empty-state !border-0 !py-6"><div class="empty-icon"><i data-lucide="activity" class="w-5 h-5"></i></div><p class="text-sm">Sin actividad todavía.</p></div>`;
    return `<ol class="divide-y divide-slate-100 dark:divide-slate-800">${events.slice(0, 8).map((e) => `<li>
      <a href="?client=${encodeURIComponent(e.client)}" data-open="${esc(e.client)}" class="flex items-start gap-3 px-4 py-3 hover:bg-slate-50 dark:hover:bg-slate-800/40">
        <span class="mt-0.5 shrink-0 w-8 h-8 rounded-lg grid place-items-center ${{ green: "bg-emerald-50 text-emerald-600", amber: "bg-amber-50 text-amber-600", red: "bg-red-50 text-red-600", blue: "bg-blue-50 text-blue-600" }[e.tone]} dark:bg-slate-800">
          <i data-lucide="${e.icon}" class="w-4 h-4"></i></span>
        <span class="min-w-0 flex-1"><span class="block text-sm font-semibold leading-snug line-clamp-2">${esc(e.title)}</span>
          <span class="block text-xs text-slate-500 truncate">${esc(e.who)}</span></span>
        <span class="shrink-0 text-[11px] text-slate-400 whitespace-nowrap">${esc(fmtAgo(e.when))}</span></a></li>`).join("")}</ol>`;
  }

  function renderPortfolio() {
    const all = state.clients;
    const accounts = all.flatMap((c) => c.accounts);
    const connected = accounts.filter((a) => accountState(a).tone === "green" || accountState(a).label === "Permisos limitados").length;
    const regions = new Set(all.flatMap((c) => c.regions));
    const query = state.filter.trim().toLowerCase();
    const shown = query ? all.filter((c) => c.name.toLowerCase().includes(query)) : all;
    $("view").innerHTML = `
      <section class="flex flex-wrap items-end gap-3">
        <div class="mr-auto min-w-0">
          <p class="eyebrow">Plataforma multi-cliente</p>
          <h2 class="page-title">Inicio</h2>
          <p class="page-sub">Estado de tus clientes y de sus entornos Huawei Cloud. Cada cliente (tenant) solo ve sus propios datos.</p>
        </div>
        <button type="button" data-new-client class="btn-primary"><i data-lucide="plus" class="w-4 h-4"></i>Nuevo cliente</button>
      </section>
      <form id="newClient" class="${state.showNewClient ? "" : "hidden"} card p-4 flex flex-wrap items-end gap-3">
        <label class="field flex-1 min-w-[12rem]">Nombre del cliente<input name="name" required maxlength="200" placeholder="p. ej. Cliente ABC" class="input" autocomplete="off" /></label>
        <button class="btn-primary">Crear cliente</button>
        <button type="button" data-new-client class="btn-secondary">Cancelar</button>
      </form>
      <section class="grid grid-cols-2 md:grid-cols-3 2xl:grid-cols-6 gap-3">
        ${kpi("Clientes", esc(all.length), "", "building-2")}
        ${kpi("Cuentas Huawei", esc(accounts.length), accounts.length ? `${connected} conectada(s)` : "", "cloud")}
        ${kpi("Projects", esc(all.reduce((n, c) => n + c.projects, 0)), "", "folder-tree")}
        ${kpi("Enterprise Projects", esc(all.reduce((n, c) => n + c.enterprise_projects, 0)), "", "briefcase")}
        ${kpi("Regiones", esc(regions.size), "", "map-pin")}
        ${kpi("Recursos", esc(all.reduce((n, c) => n + c.resources, 0)), "inventario actual", "boxes")}
      </section>
      <section class="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_22rem] gap-5 items-start">
        <div id="clientes" class="space-y-3 min-w-0">
          <div class="flex flex-wrap items-center gap-3">
            <h3 class="card-title mr-auto">Clientes <span class="text-slate-400 font-medium">· ${esc(all.length)}</span></h3>
            ${all.length > 3 ? `<div class="relative w-full sm:w-64"><i data-lucide="search" class="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"></i>
              <input id="clientFilter" value="${esc(state.filter)}" placeholder="Buscar cliente…" class="input !pl-9" autocomplete="off" /></div>` : ""}
          </div>
          <div class="grid grid-cols-1 md:grid-cols-2 2xl:grid-cols-3 gap-4">
            ${shown.map(clientCard).join("") || `<div class="md:col-span-2 2xl:col-span-3 empty-state">
              <div class="empty-icon"><i data-lucide="building-2" class="w-5 h-5"></i></div>
              <p class="font-semibold text-slate-700 dark:text-slate-200">${all.length ? "Ningún cliente coincide con la búsqueda" : "Todavía no hay clientes"}</p>
              <p class="text-sm">${all.length ? "Prueba con otro nombre." : "Crea el primero con «Nuevo cliente»."}</p></div>`}
          </div>
        </div>
        <aside class="card min-w-0 overflow-hidden">
          <div class="px-4 py-3 border-b border-slate-100 dark:border-slate-800 flex items-center gap-2">
            <i data-lucide="activity" class="w-4 h-4 text-blue-600"></i><h3 class="card-title">Actividad reciente</h3></div>
          ${activityFeed()}
        </aside>
      </section>`;
    icons();
    if (state.showNewClient) { const input = document.querySelector("#newClient input"); if (input) input.focus(); }
  }

  /* =====================================================================
   * DENTRO DE UN CLIENTE
   * ===================================================================== */
  async function openClient(clientId) {
    clearAlerts();
    state.selection = { kind: "all" };
    state.service = "";
    state.confirmDelete = false;
    $("breadcrumb").innerHTML = `<a href="/clientes" data-home>Inicio</a>`;
    loadingView("client");
    try {
      if (!state.clients.length) state.clients = await api("/api/clients");
    } catch (err) { errorView("No se pudo cargar el cliente", err.message); return; }
    state.client = state.clients.find((c) => c.id === clientId);
    if (!state.client) {
      errorView("Cliente no encontrado", "No existe o no tienes acceso a este cliente.");
      return;
    }
    state.accounts = state.client.accounts;
    if (!state.accounts.some((a) => a.id === state.accountId)) state.accountId = state.accounts.length ? state.accounts[0].id : "";
    try { await loadAccountData(); } catch (err) { errorView("No se pudieron cargar los datos de la cuenta", err.message); }
  }

  async function loadAccountData() {
    const cid = state.client.id, aid = state.accountId;
    if (aid) {
      const q = `?account_id=${encodeURIComponent(aid)}`;
      [state.projects, state.regions, state.eps, state.permissions] = await Promise.all([
        api(`/api/clients/${cid}/projects${q}`), api(`/api/clients/${cid}/regions${q}`),
        api(`/api/clients/${cid}/enterprise-projects${q}`), api(`/api/clients/${cid}/accounts/${aid}/permissions`)]);
      await loadSummary();
    } else {
      state.projects = []; state.regions = []; state.eps = []; state.permissions = null; state.summary = null;
    }
    renderClient();
  }

  async function loadSummary() {
    const s = state.selection, params = new URLSearchParams();
    if (s.kind === "project") params.set("project_id", s.id);
    if (s.kind === "region") params.set("region", s.id);
    if (s.kind === "ep") params.set("enterprise_project_id", s.id);
    state.summary = await api(`/api/clients/${state.client.id}/accounts/${state.accountId}/resource-summary?${params}`);
    await loadResourceList();
  }

  /** Recursos de la selección (API de inventario existente). Por Enterprise Project no hay filtro en la API. */
  async function loadResourceList() {
    const s = state.selection;
    if (!state.summary || !state.summary.total) { state.resources = null; return; }
    const params = new URLSearchParams({ limit: String(LIST_LIMIT), sort: "service,name" });
    if (s.kind === "project") params.set("project_id", s.id);
    if (s.kind === "region") params.set("region", s.id);
    if (s.kind === "ep") params.set("enterprise_project_id", s.id);
    if (state.service) params.set("service", state.service);
    state.resources = await api(`/api/clients/${state.client.id}/accounts/${state.accountId}/resources?${params}`);
  }

  async function refreshClient() {
    state.clients = await api("/api/clients");
    await openClient(state.client.id);
  }

  const account = () => state.accounts.find((a) => a.id === state.accountId);

  /* ---------- jerarquía: Cliente → Cuenta → IAM → Projects / EP → Regiones → Recursos ---------- */
  function pathStep(icon, label, value, tone, href) {
    const [dot] = TONE[tone] || TONE.slate;
    return `<a href="${href}" class="min-w-0 flex items-center gap-2.5 card card-hover !rounded-xl px-3 py-2.5">
      <span class="shrink-0 w-8 h-8 rounded-lg bg-slate-50 dark:bg-slate-800 grid place-items-center"><i data-lucide="${icon}" class="w-4 h-4 text-slate-500"></i></span>
      <span class="min-w-0"><span class="block text-[10px] font-semibold uppercase tracking-wide text-slate-500">${esc(label)}</span>
        <span class="flex items-center gap-1.5 text-sm font-bold truncate"><span class="dot ${dot} shrink-0"></span><span class="truncate" title="${esc(String(value).replace(/<[^>]*>/g, ""))}">${value}</span></span></span></a>`;
  }

  function iamStep(acc) {
    if (!acc) return ["slate", "—"];
    const st = accountState(acc);
    if (st.label === "Credenciales rechazadas") return ["red", "Rechazada"];
    if (acc.status !== "active") return ["amber", "Sin validar"];
    return st.label === "Permisos limitados" ? ["amber", "Permisos limitados"] : ["green", "Autenticada"];
  }

  function hierarchy(acc) {
    const steps = (acc && acc.discovery_steps) || {};
    const ep = steps.enterprise_projects;
    const st = accountState(acc);
    const [iamTone, iamText] = iamStep(acc);
    const epValue = !acc ? "—" : ep && ep.status === "denied" ? "Sin permiso" : ep && ep.status === "ok" ? `${state.eps.filter((e) => e.present).length}` : ep ? "Sin datos" : "Sin discovery";
    const epTone = !acc ? "slate" : ep && ep.status === "denied" ? "amber" : ep && ep.status === "ok" ? "green" : "slate";
    const resources = acc ? acc.resources : 0;
    return `<nav aria-label="Jerarquía" class="grid grid-cols-2 md:grid-cols-3 2xl:grid-cols-6 gap-2">
      ${pathStep("building-2", "Cliente (tenant)", esc(state.client.name), "green", "#cuentas")}
      ${pathStep("cloud", "Cuenta Huawei Cloud", acc ? esc(acc.name) : "Sin cuenta", st.tone, "#cuentas")}
      ${pathStep("key-round", "Identidad IAM", esc(iamText), iamTone, "#permisos")}
      ${pathStep("folder-tree", "Projects · EP", `${acc ? esc(state.projects.length) : "—"} · ${esc(epValue)}`, acc && state.projects.length ? epTone === "amber" ? "amber" : "green" : "slate", "#estructura")}
      ${pathStep("map-pin", "Regiones", acc ? esc(state.regions.length) : "—", acc && state.regions.length ? "green" : "slate", "#estructura")}
      ${pathStep("boxes", "Recursos", acc ? esc(resources) : "—", acc && resources ? "green" : "slate", "#recursos")}
    </nav>`;
  }

  /* ---------- panel de la cuenta ---------- */
  function actionButton(action, icon, label, primary, disabled, title) {
    const running = state.busy && state.busy.action === action;
    const off = disabled || !!state.busy;
    return `<button type="button" data-action="${action}" class="${primary ? "btn-primary" : "btn-secondary"}" ${off ? "disabled" : ""} ${title ? `title="${esc(title)}"` : ""}>
      <i data-lucide="${running ? "loader" : icon}" class="w-4 h-4 ${running ? "animate-spin" : ""}"></i>${esc(running ? state.busy.label : label)}</button>`;
  }

  function discoveryLine(acc) {
    const steps = (acc && acc.discovery_steps) || {};
    if (!acc || !acc.last_discovery_at) return `<span class="badge badge-slate">Discovery pendiente</span> <span class="text-slate-500">Pulsa «Descubrir» para obtener Projects, Regiones y Enterprise Projects.</span>`;
    const part = (key, label) => {
      const s = steps[key];
      if (!s) return "";
      const [text, tone] = STEP[s.status] || [s.status, ""];
      let detail = "";
      if (s.status !== "ok" && s.message) {
        // Solo se quita el prefijo "Permiso insuficiente" (ya mostrado como etiqueta); otros mensajes se muestran completos.
        detail = s.status === "denied" && s.message.startsWith(text) ? s.message.slice(text.length).replace(/^\s*\((.*)\)\s*$/, "$1").trim() : s.message;
      }
      return `<span>${esc(label)}: <b class="${tone}">${s.status === "ok" ? esc(s.count) : esc(text)}</b>${detail ? ` <span class="text-xs text-slate-500">(${esc(detail)})</span>` : ""}</span>`;
    };
    return `<span class="text-slate-500">Discovery ${esc(fmtAgo(acc.last_discovery_at))}:</span> ${part("projects", "Projects")} ${part("enterprise_projects", "Enterprise Projects")}
      <span>Regiones: <b>${esc(state.regions.length)}</b></span>`;
  }

  function accountPanel(acc) {
    const st = accountState(acc);
    const scan = acc && acc.last_scan;
    return `<section id="cuentas" class="card p-5 space-y-4">
      <div class="flex flex-wrap items-center gap-2"><h2 class="font-bold mr-auto">Cuenta Huawei Cloud</h2>
        <span class="text-xs text-slate-500">${plural(state.accounts.length, "cuenta", "cuentas")}</span></div>
      ${acc ? `<div class="grid grid-cols-1 gap-4 items-start">
        <div class="min-w-0 space-y-2">
          <div class="flex flex-wrap items-center gap-2"><span class="text-lg font-bold break-words">${esc(acc.name)}</span>${pill(st.tone, st.label)}</div>
          ${st.detail ? `<p class="text-sm ${st.tone === "red" ? "text-red-700 dark:text-red-400" : "text-slate-600 dark:text-slate-400"}">${esc(st.detail)}</p>` : ""}
          <div class="flex flex-wrap gap-x-4 gap-y-1 items-center text-xs text-slate-500">
            <span class="inline-flex items-center gap-1"><i data-lucide="lock" class="w-3.5 h-3.5"></i>Credenciales configuradas (cifradas)</span>
            ${techId("ID cuenta", acc.id)}
          </div>
          <div class="text-sm flex flex-wrap gap-x-4 gap-y-1">${discoveryLine(acc)}</div>
          <div class="text-sm text-slate-500">Último escaneo: ${scan ? `<b class="text-slate-700 dark:text-slate-300">${esc(fmtAgo(scan.finished_at || scan.created_at))}</b> · #${esc(scan.sequence)} ${esc(SCAN_STATUS[scan.status] || scan.status)}` : "nunca"}</div>
        </div>
      </div>` : `<div class="rounded-xl border-2 border-dashed border-slate-300 dark:border-slate-700 p-6 text-center">
        <i data-lucide="cloud" class="w-7 h-7 mx-auto text-slate-400"></i><p class="mt-2 font-semibold">Este cliente aún no tiene cuenta Huawei Cloud</p>
        <p class="text-sm text-slate-500">Agrégala abajo con la AK/SK de una identidad IAM de solo lectura.</p></div>`}
      ${acc ? `<details ${st.label === "Credenciales rechazadas" ? "open" : ""} class="rounded-xl border ${st.label === "Credenciales rechazadas"
        ? "border-red-200 dark:border-red-900 bg-red-50/40 dark:bg-red-950/10" : "border-slate-200 dark:border-slate-800"} px-4 py-3">
        <summary class="text-sm font-semibold text-blue-600 cursor-pointer select-none">Reemplazar credenciales (AK/SK) de ${esc(acc.name)}</summary>
        <form id="replaceCredentials" class="mt-3 grid grid-cols-1 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] gap-3 items-end" autocomplete="off">
          <label class="field">Nueva Access Key (AK)<input name="ak" required type="password" autocomplete="new-password" spellcheck="false" class="input" /></label>
          <label class="field">Nueva Secret Key (SK)<input name="sk" required type="password" autocomplete="new-password" spellcheck="false" class="input" /></label>
          <button class="btn-primary justify-center" ${state.busy ? "disabled" : ""}><i data-lucide="key-round" class="w-4 h-4"></i>Reemplazar cifrada</button>
          <p class="md:col-span-3 text-xs text-slate-500">Las claves actuales se sustituyen y se cifran en el servidor; nunca se muestran. La cuenta quedará sin validar hasta pulsar «Conectar Huawei Cloud». Requiere rol administrador.</p>
        </form>
      </details>` : ""}
      <details ${state.accounts.length ? "" : "open"} class="rounded-xl border border-slate-200 dark:border-slate-800 px-4 py-3">
        <summary class="text-sm font-semibold text-blue-600 cursor-pointer select-none">+ Agregar cuenta Huawei Cloud</summary>
        <form id="newAccount" class="mt-3 grid grid-cols-1 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)_auto] gap-3 items-end" autocomplete="off">
          <label class="field">Nombre<input name="name" required maxlength="200" placeholder="Producción" class="input" /></label>
          <label class="field">Access Key (AK)<input name="ak" required type="password" autocomplete="new-password" spellcheck="false" class="input" /></label>
          <label class="field">Secret Key (SK)<input name="sk" required type="password" autocomplete="new-password" spellcheck="false" class="input" /></label>
          <button class="btn-primary justify-center"><i data-lucide="lock" class="w-4 h-4"></i>Guardar cifrada</button>
          <p class="md:col-span-4 text-xs text-slate-500">Las claves se cifran en el servidor y nunca se vuelven a mostrar. Usa una identidad IAM con permisos de solo lectura.</p>
        </form>
      </details>
    </section>`;
  }

  /* ---------- árbol + recursos ---------- */
  function treeItem(kind, id, label, sub, count, icon) {
    const active = state.selection.kind === kind && state.selection.id === id;
    return `<button type="button" data-select="${esc(kind)}" data-id="${esc(id || "")}" title="${esc(label)}" class="w-full min-w-0 flex items-center gap-2 px-2 py-1.5 rounded-lg text-left text-sm ${active
      ? "bg-blue-50 dark:bg-blue-950/40 text-blue-800 dark:text-blue-200 font-semibold" : "hover:bg-slate-50 dark:hover:bg-slate-800/60"}">
      <i data-lucide="${icon}" class="w-3.5 h-3.5 shrink-0 text-slate-400"></i><span class="truncate min-w-0">${esc(label)}</span>
      ${sub ? `<span class="shrink-0 text-[11px] font-mono text-slate-400">${esc(sub)}</span>` : ""}
      <span class="ml-auto shrink-0 text-xs tabular-nums text-slate-500">${count ?? ""}</span></button>`;
  }
  function treeSection(title, icon, items, empty) {
    return `<div class="mt-2"><div class="flex items-center gap-2 px-2 text-[11px] font-bold uppercase tracking-wide text-slate-500">
      <i data-lucide="${icon}" class="w-3.5 h-3.5"></i>${esc(title)}</div>
      <div class="mt-1 ml-3 pl-2 border-l border-slate-200 dark:border-slate-800 space-y-0.5">${items || `<p class="px-2 py-1 text-xs text-slate-400">${esc(empty)}</p>`}</div></div>`;
  }

  function selectionInfo() {
    const s = state.selection;
    if (s.kind === "project") {
      const p = state.projects.find((x) => x.id === s.id) || {};
      return { title: `Project ${p.name || ""}`, meta: `${regionCode(p.region_id || "")} ${techId("Project ID", p.huawei_project_id)}` };
    }
    if (s.kind === "region") return { title: `Región ${s.id}`, meta: regionCode(s.id) };
    if (s.kind === "ep") {
      const e = state.eps.find((x) => x.huawei_ep_id === s.id) || {};
      return { title: `Enterprise Project ${e.name || s.id}`, meta: techId("EP ID", s.id) };
    }
    return { title: "Toda la cuenta", meta: "" };
  }

  function structure(acc) {
    const c = state.client, steps = (acc && acc.discovery_steps) || {};
    const epStep = steps.enterprise_projects;
    const [iamTone, iamText] = iamStep(acc);
    const sel = selectionInfo();
    const tiles = state.summary ? Object.entries(state.summary.by_service).sort((a, b) => b[1] - a[1]).map(([s, n]) => {
      const on = state.service === s;
      return `<button type="button" data-service="${esc(s)}" aria-pressed="${on}" title="${on ? "Quitar filtro" : "Ver solo este servicio"}"
        class="text-left rounded-xl border p-3 min-w-0 transition ${on ? "border-blue-500 ring-2 ring-blue-500/20 bg-blue-50/60 dark:bg-blue-950/30"
          : "border-slate-200 dark:border-slate-800 hover:border-blue-300 dark:hover:border-blue-800"}">
        <div class="text-xs text-slate-500 leading-tight min-h-[2rem] break-words">${esc(serviceName(s))}</div>
        <div class="text-2xl font-extrabold tabular-nums">${esc(n)}</div></button>`;
    }).join("") : "";
    let empty;
    if (!acc) empty = "Agrega una cuenta Huawei Cloud para ver su inventario.";
    else if (!state.projects.length) empty = "Sin Projects todavía: pulsa «Descubrir» y después «Escanear inventario».";
    else if (!acc.last_scan) empty = "Aún no se ha escaneado esta cuenta: pulsa «Escanear inventario».";
    else empty = state.selection.kind === "all" ? "El último escaneo no encontró recursos." : "Sin recursos en esta selección.";
    return `<section class="grid grid-cols-1 xl:grid-cols-[minmax(0,22rem)_minmax(0,1fr)] gap-5 items-start">
      <div id="estructura" class="card p-4 min-w-0">
        <div class="flex items-center gap-2 font-bold min-w-0"><i data-lucide="building-2" class="w-4 h-4 text-blue-600 shrink-0"></i><span class="truncate" title="${esc(c.name)}">${esc(c.name)}</span></div>
        <div class="ml-2 pl-3 border-l border-slate-200 dark:border-slate-800 mt-1 min-w-0">
          <div class="flex items-center gap-2 px-2 py-1 text-sm font-semibold min-w-0"><i data-lucide="cloud" class="w-4 h-4 text-slate-400 shrink-0"></i><span class="truncate">Huawei Cloud${acc ? ` · ${esc(acc.name)}` : ""}</span></div>
          <div class="ml-2 pl-3 border-l border-slate-200 dark:border-slate-800 min-w-0">
            <div class="flex items-center gap-2 px-2 py-1 text-sm min-w-0"><i data-lucide="key-round" class="w-4 h-4 text-slate-400 shrink-0"></i><span class="truncate">Identidad IAM</span>
              <span class="ml-auto">${pill(iamTone, iamText)}</span></div>
            <div class="ml-2">
              ${treeSection("Projects", "folder", state.projects.map((p) => treeItem("project", p.id, p.name || p.huawei_project_id, p.name === p.region_id ? "" : p.region_id, p.resources, "folder-open")).join(""),
                acc ? "Sin Projects: pulsa «Descubrir»" : "Sin cuenta")}
              ${treeSection("Enterprise Projects", "briefcase", state.eps.filter((e) => e.present).map((e) => treeItem("ep", e.huawei_ep_id, e.name, e.status === 2 ? "deshabilitado" : "", e.resources, "briefcase")).join(""),
                !epStep ? "Sin discovery todavía" : epStep.status === "denied" ? "Permiso insuficiente para listarlos"
                  : epStep.status === "ok" ? "Ninguno visible para esta identidad" : "Sin datos: el último discovery no pudo consultarlos")}
              ${treeSection("Regiones", "map-pin", state.regions.map((r) => treeItem("region", r.region_id, r.region_id, "", r.resources, "map-pin")).join(""), "Ninguna")}
              <div class="mt-2">${treeItem("all", "", "Todos los recursos", "", acc ? acc.resources : 0, "boxes")}</div>
            </div>
          </div>
        </div>
      </div>
      <div id="recursos" class="card p-5 min-w-0">
        <div class="flex flex-wrap items-start gap-x-3 gap-y-1 mb-3">
          <div class="mr-auto min-w-0"><p class="text-[11px] font-semibold uppercase tracking-wide text-slate-500">Recursos</p>
            <h2 class="font-bold break-words">${esc(sel.title)}</h2>${sel.meta ? `<div class="mt-1 flex flex-wrap items-center gap-2">${sel.meta}</div>` : ""}</div>
          <div class="text-right"><div class="text-2xl font-extrabold tabular-nums">${esc(state.summary ? state.summary.total : 0)}</div>
            <a href="/dashboard?client=${encodeURIComponent(state.client.id)}${acc ? `&account=${encodeURIComponent(acc.id)}` : ""}#recursos" class="text-xs font-semibold text-blue-600 hover:underline">Ver en el inventario →</a></div>
        </div>
        <div class="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-4 gap-3">${tiles || `<div class="col-span-full rounded-xl border-2 border-dashed border-slate-200 dark:border-slate-800 p-6 text-center text-sm text-slate-500">
          <i data-lucide="boxes" class="w-6 h-6 mx-auto text-slate-400"></i><p class="mt-2">${esc(empty)}</p></div>`}</div>
        ${resourceList(acc)}
      </div>
    </section>`;
  }

  function statusBadge(status) {
    if (!status) return '<span class="text-slate-400">—</span>';
    const v = String(status).toLowerCase();
    const tone = /^(active|available|in-use|running|normal|ok|opened|up)$/.test(v) ? "badge-green"
      : /(error|fault|fail|deleted|abnormal)/.test(v) ? "badge-red" : /(stop|shutoff|frozen|down|closed|pending|build)/.test(v) ? "badge-amber" : "badge-slate";
    return `<span class="badge ${tone}">${esc(status)}</span>`;
  }

  function resourceList(acc) {
    if (!acc || !state.summary || !state.summary.total) return "";
    const page = state.resources;
    if (!page) return "";
    const link = `/dashboard?client=${encodeURIComponent(state.client.id)}&account=${encodeURIComponent(acc.id)}#recursos`;
    const rows = page.items.map((r) => `<tr>
      <td class="min-w-0"><div class="font-medium break-words">${esc(r.name || r.provider_id)}</div>
        <div class="mt-0.5">${techId("", r.provider_id)}</div></td>
      <td><span class="badge-svc badge">${esc(r.service)}</span></td>
      <td>${statusBadge(r.status)}</td>
      <td class="hidden sm:table-cell">${r.region ? regionCode(r.region) : "—"}</td>
      <td class="text-xs text-slate-500 whitespace-nowrap hidden md:table-cell" title="${esc(fmtDate(r.last_seen))}">${esc(fmtAgo(r.last_seen))}</td></tr>`).join("");
    return `<div class="mt-5">
      <div class="flex flex-wrap items-baseline gap-2 mb-1">
        <h3 class="text-sm font-bold mr-auto">${state.service ? `${esc(serviceName(state.service))}` : "Recursos"} <span class="font-normal text-slate-500">· ${esc(Math.min(page.items.length, page.total))} de ${esc(page.total)}</span></h3>
        ${state.service ? `<button type="button" data-service="" class="text-xs text-blue-600 hover:underline">Quitar filtro de servicio</button>` : ""}
      </div>
      <div class="overflow-x-auto rounded-xl border border-slate-200 dark:border-slate-800"><table class="table-pro">
        <thead><tr><th>Nombre</th><th>Servicio</th><th>Estado</th><th class="hidden sm:table-cell">Región</th><th class="hidden md:table-cell">Visto</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="5" class="py-4 text-center text-sm text-slate-500">Sin recursos con este filtro.</td></tr>`}</tbody></table></div>
      ${page.total > page.items.length ? `<a href="${link}" class="mt-2 inline-block text-xs font-semibold text-blue-600 hover:underline">Ver los ${esc(page.total)} en el inventario →</a>` : ""}
    </div>`;
  }

  function permissionsPanel(acc) {
    const services = state.permissions ? state.permissions.services : [];
    const checked = services.filter((p) => p.status !== "not_checked").length;
    const perms = services.slice().sort((x, y) => (PERM_ORDER[x.status] ?? 9) - (PERM_ORDER[y.status] ?? 9)
      || serviceName(x.service).localeCompare(serviceName(y.service))).map((p) => {
      const [sym, label, tone] = PERM[p.status] || PERM.not_checked;
      return `<div class="min-w-0 flex items-center gap-2 px-3 py-2 rounded-lg ${tone}" title="${esc([p.message, p.iam_actions.join(", ")].filter(Boolean).join(" · "))}">
        <span class="w-4 text-center font-bold shrink-0">${sym}</span><span class="font-medium truncate min-w-0">${esc(serviceName(p.service))}</span>
        <span class="ml-auto text-[11px] whitespace-nowrap shrink-0">${esc(label)}</span></div>`;
    }).join("");
    return `<section id="permisos" class="card p-5">
      <div class="flex flex-wrap items-baseline gap-2 mb-3">
        <h2 class="font-bold mr-auto">Seguridad y permisos de la identidad IAM</h2>
        <span class="text-xs text-slate-500">Según el último escaneo de cada servicio · ${checked} de ${services.length} comprobados</span>
      </div>
      ${acc && acc.last_scan_auth_failed ? `<p class="mb-3 rounded-lg border border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950/30 px-3 py-2 text-sm text-red-700 dark:text-red-300">
        Huawei Cloud rechazó las credenciales en el último escaneo: los servicios marcados como error o sin comprobar no reflejan la política IAM.</p>` : ""}
      ${acc && services.length && !checked ? `<p class="mb-3 text-sm text-slate-500">Aún no hay escaneos: pulsa «Escanear inventario» para saber qué servicios puede leer esta identidad.</p>` : ""}
      ${acc ? `<div class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-2 text-sm">${perms}</div>` : '<p class="text-sm text-slate-500">Sin cuenta Huawei Cloud: no hay identidad IAM que evaluar.</p>'}
      <p class="mt-4 text-xs text-slate-500 flex items-center gap-1.5"><i data-lucide="shield-check" class="w-3.5 h-3.5"></i>Modo solo lectura: las operaciones sobre recursos (iniciar, detener, reiniciar ECS…) no están habilitadas. Un servicio sin permiso no es un fallo de la cuenta.</p>
    </section>`;
  }

  const TABS = [["resumen", "Resumen", "layout-grid"], ["cuenta", "Cuenta e IAM", "key-round"],
    ["estructura", "Estructura y recursos", "folder-tree"], ["permisos", "Permisos", "shield-check"]];

  function nextSteps(acc) {
    // Guía basada SOLO en el estado real de la cuenta.
    if (!acc) return [["plus", "Agrega una cuenta Huawei Cloud", "En la pestaña «Cuenta e IAM»."]];
    const st = accountState(acc), steps = [];
    if (st.label === "Credenciales rechazadas") steps.push(["key-round", "Reemplaza la AK/SK", "Pestaña «Cuenta e IAM» → Reemplazar credenciales."]);
    if (acc.status !== "active" || st.label === "Credenciales rechazadas") steps.push(["plug", "Conecta con Huawei Cloud", "Valida la identidad IAM."]);
    if (!acc.last_discovery_at || st.label === "Discovery con errores") steps.push(["radar", "Ejecuta el discovery", "Projects, Regiones y Enterprise Projects."]);
    if (!acc.last_scan || acc.last_scan_auth_failed) steps.push(["scan-search", "Escanea el inventario", "Con el motor de inventario existente."]);
    return steps;
  }

  function summaryTab(acc) {
    const st = accountState(acc);
    const steps = nextSteps(acc);
    const services = state.permissions ? state.permissions.services : [];
    const limited = services.filter((p) => ["denied", "partial", "error"].includes(p.status));
    const tiles = state.summary ? Object.entries(state.summary.by_service).sort((a, b) => b[1] - a[1]).slice(0, 8).map(([s, n]) =>
      `<div class="rounded-xl border border-slate-200 dark:border-slate-800 p-3 min-w-0"><div class="text-xs text-slate-500 leading-tight min-h-[2rem] break-words">${esc(serviceName(s))}</div>
        <div class="text-2xl font-extrabold tabular-nums">${esc(n)}</div></div>`).join("") : "";
    const scan = acc && acc.last_scan;
    return `<div class="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_22rem] gap-5 items-start">
      <div class="space-y-5 min-w-0">
        <section class="card p-5">
          <div class="flex flex-wrap items-center gap-2 mb-3"><h3 class="card-title mr-auto">Estado de la cuenta</h3>${acc ? pill(st.tone, st.label) : ""}</div>
          ${acc ? `<dl class="grid grid-cols-1 sm:grid-cols-3 gap-4 text-sm">
            <div><dt class="card-sub">Cuenta Huawei Cloud</dt><dd class="font-semibold break-words">${esc(acc.name)}</dd></div>
            <div><dt class="card-sub">Última validación</dt><dd class="font-semibold">${esc(acc.last_validated_at ? fmtAgo(acc.last_validated_at) : "nunca")}</dd></div>
            <div><dt class="card-sub">Último escaneo</dt><dd class="font-semibold">${scan ? `${esc(fmtAgo(scan.finished_at || scan.created_at))} <span class="text-xs font-normal text-slate-500">#${esc(scan.sequence)} ${esc(SCAN_STATUS[scan.status] || scan.status)}</span>` : "nunca"}</dd></div>
          </dl>
          ${st.detail ? `<p class="mt-3 text-sm ${st.tone === "red" ? "text-red-700 dark:text-red-400" : "text-slate-600 dark:text-slate-400"}">${esc(st.detail)}</p>` : ""}
          <div class="mt-3 text-sm flex flex-wrap gap-x-4 gap-y-1">${discoveryLine(acc)}</div>` : `<p class="text-sm text-slate-500">Este cliente aún no tiene cuenta Huawei Cloud.</p>`}
        </section>
        <section class="card p-5">
          <div class="flex flex-wrap items-baseline gap-2 mb-3"><h3 class="card-title mr-auto">Recursos por servicio</h3>
            <button type="button" data-tab="estructura" class="btn-ghost btn-sm">Ver estructura y recursos →</button></div>
          ${tiles ? `<div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">${tiles}</div>`
            : `<div class="empty-state !py-6"><div class="empty-icon"><i data-lucide="boxes" class="w-5 h-5"></i></div><p class="text-sm">No hay datos disponibles todavía.</p></div>`}
        </section>
      </div>
      <div class="space-y-5 min-w-0">
        <section class="card p-5">
          <h3 class="card-title mb-3">Próximos pasos</h3>
          ${steps.length ? `<ol class="space-y-3">${steps.map(([icon, title, sub], i) => `<li class="flex gap-3"><span class="shrink-0 w-7 h-7 rounded-full bg-blue-50 dark:bg-blue-950/40 text-blue-700 dark:text-blue-300 grid place-items-center text-xs font-bold">${i + 1}</span>
            <span class="min-w-0"><span class="block text-sm font-semibold flex items-center gap-1.5"><i data-lucide="${icon}" class="w-3.5 h-3.5 text-slate-400"></i>${esc(title)}</span><span class="block text-xs text-slate-500">${esc(sub)}</span></span></li>`).join("")}</ol>`
            : `<p class="text-sm text-slate-600 dark:text-slate-400 flex items-center gap-2"><i data-lucide="check-circle-2" class="w-4 h-4 text-emerald-600"></i>Todo al día: cuenta conectada, discovery y escaneo realizados.</p>`}
        </section>
        <section class="card p-5">
          <div class="flex items-baseline gap-2 mb-3"><h3 class="card-title mr-auto">Limitaciones IAM</h3>
            <button type="button" data-tab="permisos" class="btn-ghost btn-sm">Ver todo →</button></div>
          ${!acc ? `<p class="text-sm text-slate-500">Sin cuenta.</p>` : limited.length ? `<ul class="space-y-2">${limited.slice(0, 6).map((p) =>
            `<li class="flex items-center gap-2 text-sm"><span class="badge ${p.status === "error" ? "badge-red" : "badge-amber"}">${esc((PERM[p.status] || PERM.not_checked)[1])}</span><span class="truncate">${esc(serviceName(p.service))}</span></li>`).join("")}</ul>`
            : `<p class="text-sm text-slate-500">${services.some((p) => p.status !== "not_checked") ? "Ningún servicio escaneado tiene restricciones." : "Sin datos: aún no hay escaneos."}</p>`}
        </section>
      </div>
    </div>`;
  }

  function renderClient() {
    const c = state.client, acc = account(), st = clientState(c);
    if (!TABS.some((t) => t[0] === state.tab)) state.tab = "resumen";
    $("breadcrumb").innerHTML = `<a href="/clientes" data-home>Inicio</a><i data-lucide="chevron-right" class="w-3 h-3"></i><span class="truncate text-slate-700 dark:text-slate-300">${esc(c.name)}</span>`;
    $("title").textContent = "Cliente";
    $("clientNav").classList.remove("hidden");
    document.querySelectorAll("[data-tab-link]").forEach((a) => a.classList.toggle("!text-blue-700", a.dataset.tabLink === state.tab));
    const accountSwitch = state.accounts.length > 1 ? `<label class="field min-w-[12rem]">Cuenta Huawei Cloud
      <select id="accountSelect" class="input">${state.accounts.map((a) => `<option value="${esc(a.id)}" ${a.id === state.accountId ? "selected" : ""}>${esc(a.name)}</option>`).join("")}</select></label>` : "";
    const panels = { resumen: summaryTab(acc), cuenta: accountPanel(acc), estructura: structure(acc), permisos: permissionsPanel(acc) };
    $("view").innerHTML = `
      <section class="card p-5">
        <div class="flex flex-wrap items-center gap-4">
          <div class="shrink-0 w-12 h-12 rounded-xl bg-blue-600 text-white grid place-items-center font-bold">${esc(initials(c.name))}</div>
          <div class="min-w-0 flex-1">
            <p class="eyebrow">Cliente (tenant)</p>
            <h2 class="page-title break-words">${esc(c.name)}</h2>
          </div>
          ${pill(st.tone, st.label)}
          <button type="button" data-action="delete-client" class="btn-ghost btn-sm !text-red-600 hover:!bg-red-50 dark:hover:!bg-red-950/30 sm:ml-auto" title="Eliminar este cliente y todos sus datos">
            <i data-lucide="trash-2" class="w-4 h-4"></i>Eliminar</button>
        </div>
        ${state.confirmDelete ? `<div class="mt-4 rounded-xl border border-red-200 dark:border-red-900 bg-red-50/60 dark:bg-red-950/20 px-4 py-3 flex flex-wrap items-center gap-3">
          <i data-lucide="alert-triangle" class="w-5 h-5 text-red-600 dark:text-red-400 shrink-0"></i>
          <p class="text-sm font-medium text-red-700 dark:text-red-300 flex-1 min-w-0">¿Eliminar <strong>${esc(c.name)}</strong> con todas sus cuentas, credenciales e inventario? Esta acción <strong>no se puede deshacer</strong>.</p>
          <div class="flex gap-2 shrink-0">
            <button type="button" data-action="cancel-delete" class="btn-secondary btn-sm" ${state.busy ? "disabled" : ""}>Cancelar</button>
            <button type="button" data-action="confirm-delete" class="btn-sm flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-red-600 hover:bg-red-700 text-white font-semibold" ${state.busy ? "disabled" : ""}>
              <i data-lucide="${state.busy && state.busy.action === 'confirm-delete' ? 'loader' : 'trash-2'}" class="w-4 h-4 ${state.busy && state.busy.action === 'confirm-delete' ? 'animate-spin' : ''}"></i>
              ${state.busy && state.busy.action === 'confirm-delete' ? 'Eliminando…' : 'Sí, eliminar'}</button>
          </div>
        </div>` : ""}
        <div class="mt-4 pt-4 border-t border-slate-100 dark:border-slate-800 flex flex-wrap items-end gap-3">
          ${accountSwitch}
          <div class="flex flex-wrap gap-2 ${accountSwitch ? "" : "w-full"} sm:ml-auto sm:w-auto">
            ${acc ? `${actionButton("validate", "plug", "Conectar Huawei Cloud", false, false)}
            ${actionButton("discover", "radar", "Descubrir", false, false)}
            ${actionButton("scan", "scan-search", "Escanear inventario", true, !state.projects.length, state.projects.length ? "" : "Primero descubre los Projects")}`
              : `<button type="button" data-tab="cuenta" class="btn-primary"><i data-lucide="plus" class="w-4 h-4"></i>Agregar cuenta Huawei Cloud</button>`}
          </div>
        </div>
      </section>
      ${hierarchy(acc)}
      <nav class="flex gap-1 border-b border-slate-200 dark:border-slate-800 overflow-x-auto overflow-y-hidden" role="tablist" aria-label="Secciones del cliente">
        ${TABS.map(([id, label, icon]) => `<button type="button" role="tab" data-tab="${id}" aria-selected="${state.tab === id}" class="tab ${state.tab === id ? "tab-active" : ""}">
          <i data-lucide="${icon}" class="w-4 h-4"></i>${esc(label)}</button>`).join("")}
      </nav>
      <div role="tabpanel" class="space-y-5">${panels[state.tab]}</div>`;
    icons();
  }

  /* ---------- acciones ---------- */
  const BUSY = { validate: "Conectando con Huawei Cloud…", discover: "Ejecutando discovery…", scan: "Iniciando escaneo…" };
  async function runAction(action) {
    const acc = account();
    if (!acc || state.busy) return;
    clearAlerts();
    const base = `/api/admin/clients/${state.client.id}/accounts/${acc.id}`;
    state.busy = { action, label: BUSY[action] };
    renderClient();
    try {
      if (action === "validate") {
        const r = await api(`${base}/iam/validate`, { method: "POST" });
        notify(r.authenticated ? `Conexión establecida.${r.projects_visible !== null ? ` Projects visibles: ${r.projects_visible}.` : ""}${r.message ? ` ${r.message}.` : ""}`
          : r.account_status === "invalid" ? `Credenciales rechazadas por Huawei Cloud: ${r.message || "AK/SK inválidas"}`
          : (r.message || "Error de conexión con Huawei Cloud"), r.authenticated ? "ok" : "error");
      } else if (action === "discover") {
        const r = await api(`${base}/iam/discover`, { method: "POST" });
        const p = r.steps.projects, e = r.steps.enterprise_projects;
        const part = (label, s) => (s.status === "ok" ? `${label} encontrados: ${s.count}` : `${label}: ${s.message || s.status}`);
        notify(`${part("Projects", p)} · Regiones encontradas: ${r.regions.length} · ${part("Enterprise Projects", e)}`, r.complete ? "ok" : "info");
      } else if (action === "scan") {
        const run = await api(`/api/clients/${state.client.id}/accounts/${acc.id}/scans`, { method: "POST", body: JSON.stringify({}) });
        notify(`Escaneo #${run.sequence} iniciado con el motor de inventario. Sigue su progreso en el Dashboard.`, "ok");
      }
    } catch (err) {
      notify(err.status === 403 ? "Tu rol no permite esta acción (requiere operador o administrador)." : err.message);
    } finally {
      state.busy = null;
      await refreshClient().catch((e) => notify(e.message));
    }
  }

  async function deleteClient() {
    if (!state.client || state.busy) return;
    clearAlerts();
    state.busy = { action: "confirm-delete", label: "Eliminando…" };
    renderClient();
    try {
      await api(`/api/admin/clients/${state.client.id}`, { method: "DELETE" });
      const name = state.client.name;
      state.client = null;
      state.confirmDelete = false;
      state.clients = await api("/api/clients");
      history.pushState(null, "", "/clientes");
      loadPortfolio();
      notify(`Cliente «${name}» eliminado correctamente.`, "ok");
    } catch (err) {
      state.confirmDelete = false;
      notify(err.status === 403 ? "Tu rol no permite eliminar clientes (requiere administrador)." : err.message);
    } finally {
      state.busy = null;
      if (state.client) renderClient();
    }
  }

  /* ---------- eventos ---------- */
  $("view").addEventListener("click", (event) => {
    const copy = event.target.closest("[data-copy]");
    if (copy) {
      event.preventDefault();
      const done = () => { copy.innerHTML = '<i data-lucide="check" class="w-3 h-3 text-emerald-600"></i>'; icons(); setTimeout(() => { copy.innerHTML = '<i data-lucide="copy" class="w-3 h-3"></i>'; icons(); }, 1200); };
      if (navigator.clipboard) navigator.clipboard.writeText(copy.dataset.copy).then(done).catch(() => {});
      return;
    }
    if (event.target.closest("[data-retry]")) { route(); return; }
    const tabBtn = event.target.closest("[data-tab]");
    if (tabBtn && state.client) { setTab(tabBtn.dataset.tab); return; }
    if (event.target.closest("[data-new-client]")) { state.showNewClient = !state.showNewClient; renderPortfolio(); return; }
    const open = event.target.closest("[data-open]");
    if (open) { event.preventDefault(); state.tab = "resumen"; history.pushState(null, "", open.getAttribute("href")); openClient(open.dataset.open); return; }
    const tab = event.target.closest("[data-account]");
    if (tab) { state.accountId = tab.dataset.account; state.selection = { kind: "all" }; state.service = ""; loadAccountData().catch((e) => notify(e.message)); return; }
    const action = event.target.closest("[data-action]");
    if (action) {
      const act = action.dataset.action;
      if (act === "delete-client") { state.confirmDelete = true; renderClient(); return; }
      if (act === "cancel-delete") { state.confirmDelete = false; renderClient(); return; }
      if (act === "confirm-delete") { deleteClient(); return; }
      runAction(act);
      return;
    }
    const tile = event.target.closest("[data-service]");
    if (tile) {
      state.service = state.service === tile.dataset.service ? "" : tile.dataset.service;
      loadResourceList().then(renderClient).catch((e) => notify(e.message));
      return;
    }
    const select = event.target.closest("[data-select]");
    if (select) {
      state.selection = { kind: select.dataset.select, id: select.dataset.id };
      state.service = "";
      loadSummary().then(renderClient).catch((e) => notify(e.message));
    }
  });
  $("view").addEventListener("change", (event) => {
    if (event.target.id !== "accountSelect") return;
    state.accountId = event.target.value; state.selection = { kind: "all" }; state.service = "";
    loadAccountData().catch((e) => notify(e.message));
  });
  $("view").addEventListener("input", (event) => {
    if (event.target.id !== "clientFilter") return;  // búsqueda de clientes
    state.filter = event.target.value;
    const caret = event.target.selectionStart;
    renderPortfolio();
    const input = $("clientFilter");
    if (input) { input.focus(); input.setSelectionRange(caret, caret); }
  });
  $("breadcrumb").addEventListener("click", (event) => {
    if (!event.target.closest("[data-home]")) return;
    event.preventDefault(); history.pushState(null, "", "/clientes"); state.client = null; clearAlerts(); loadPortfolio();
  });
  $("view").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    clearAlerts();
    try {
      if (form.id === "newClient") {
        const client = await api("/api/admin/clients", { method: "POST", body: JSON.stringify({ name: form.elements["name"].value.trim() }) });
        state.clients = await api("/api/clients");
        state.showNewClient = false;
        history.pushState(null, "", `?client=${encodeURIComponent(client.id)}`);
        await openClient(client.id);
      } else if (form.id === "replaceCredentials") {
        const acc = account();
        if (!acc) return;
        const field = (n) => form.elements[n];
        const payload = { ak: field("ak").value, sk: field("sk").value };
        field("ak").value = ""; field("sk").value = "";   // no dejar las claves en el formulario
        await api(`/api/admin/clients/${state.client.id}/accounts/${acc.id}/credentials`, { method: "PUT", body: JSON.stringify(payload) });
        notify("Credenciales reemplazadas y cifradas. Ahora pulsa «Conectar Huawei Cloud» para validarlas.", "ok");
        await refreshClient();
      } else if (form.id === "newAccount") {
        const field = (n) => form.elements[n];
        const payload = { name: field("name").value.trim(), ak: field("ak").value, sk: field("sk").value };
        field("ak").value = ""; field("sk").value = "";   // no dejar las claves en el formulario
        const created = await api(`/api/admin/clients/${state.client.id}/accounts`, { method: "POST", body: JSON.stringify(payload) });
        state.accountId = created.id;
        notify("Cuenta guardada con las claves cifradas. Ahora pulsa «Conectar Huawei Cloud» y después «Descubrir».", "ok");
        await refreshClient();
      }
    } catch (err) {
      notify(err.status === 403 ? (form.id === "replaceCredentials" ? "Tu rol no permite reemplazar credenciales (requiere administrador)."
        : "Tu rol no permite esta acción.") : err.message);
    }
  });
  function setTab(tab) {
    state.tab = tab;
    history.replaceState(null, "", `${location.pathname}${location.search}#${tab}`);
    renderClient();
    window.scrollTo({ top: 0 });
  }
  // Submenú de la barra lateral (Resumen, Cuenta e IAM, …) dentro de un cliente.
  document.addEventListener("click", (event) => {
    const link = event.target.closest("[data-tab-link]");
    if (!link || !state.client) return;
    event.preventDefault();
    setTab(link.dataset.tabLink);
  });
  window.addEventListener("popstate", route);
  $("btnRefresh").addEventListener("click", () => (state.client ? refreshClient() : loadPortfolio()).catch((e) => notify(e.message)));

  function route() {
    clearAlerts();
    const clientId = new URLSearchParams(location.search).get("client");
    if (clientId) { openClient(clientId).catch((e) => errorView("No se pudo cargar el cliente", e.message)); } else { state.client = null; loadPortfolio(); }
  }

  icons();
  route();
})();
