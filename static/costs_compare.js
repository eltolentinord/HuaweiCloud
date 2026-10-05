/* Comparador de costos por región: ORIGEN (recurso real) → DESTINO (simulado) → RESULTADO.
 * Vanilla JS sobre la API interna (/cost-compare). Simulación: nunca modifica recursos ni
 * inventario. Precios solo de fuentes oficiales; sin precio => "Precio no disponible" (nunca 0).
 * Todo texto que viene de la nube se escapa. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const SUPPORTED = { "ecs.server": "Servidores (ECS)", "evs.volume": "Discos (EVS)", "eip.publicip": "IPs públicas (EIP)" };
  const KIND = { ecs: "ECS", evs: "EVS", eip: "EIP" };
  const OS_LABEL = { linux: "Linux", windows: "Windows" };
  const MODE_LABEL = { bandwidth: "por ancho de banda", traffic: "por tráfico" };
  const PRODUCT_ORDER = ["ecs", "evs", "ip", "bandwidth"];
  const PRODUCT_LABEL = { ecs: "ECS", evs: "EVS", ip: "EIP", bandwidth: "Ancho de banda" };
  const state = { clientId: "", accountId: "", options: null, flavors: {}, items: [], summary: null,
    compared: false, busy: false, seq: 0, timer: null };
  // Enlace directo desde el dashboard: ?client=…&account=…&resource=… (solo IDs; se validan en la API)
  const deepLink = new URLSearchParams(location.search);
  let deepLinkCompare = deepLink.has("resource");

  /* ---------- utilidades ---------- */
  function esc(value) {
    return String(value === undefined || value === null ? "" : value)
      .replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function icons() { if (window.lucide) window.lucide.createIcons(); }
  function fmtDate(value) { return value ? new Date(value).toLocaleString() : "—"; }
  const clone = (obj) => JSON.parse(JSON.stringify(obj));

  function money(amount, currency) {
    if (amount === null || amount === undefined) return null;
    const n = Number(amount);
    try { return new Intl.NumberFormat(undefined, { style: "currency", currency, currencyDisplay: "narrowSymbol" }).format(n); }
    catch (e) { return `${n.toFixed(2)} ${currency || ""}`.trim(); }
  }
  function signedMoney(amount, currency) {
    const n = Number(amount);
    return `${n > 0 ? "+" : n < 0 ? "−" : ""}${money(Math.abs(n), currency)}`;
  }
  function signedPercent(value) {
    const n = Number(value);
    return `${n > 0 ? "+" : n < 0 ? "−" : ""}${Math.abs(n).toFixed(2)} %`;
  }
  const NA = '<span class="text-amber-700 dark:text-amber-400 font-semibold">Precio no disponible</span>';

  function regionName(region) {
    if (!region) return "";
    return String(region.name || region.id).replace(/^(AP|CN|LA|AF|ME|EU|TR|SA|NA)[- ]/, "");
  }
  const regionOf = (id) => (state.options ? state.options.regions.find((r) => r.id === id) : null) || { id, name: id };

  function notify(message, tone) {
    const tones = {
      error: "border-red-200 bg-red-50 text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300",
      info: "border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300",
      ok: "border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/30 dark:text-emerald-300",
    };
    const div = document.createElement("div");
    div.className = `${tones[tone || "error"]} rounded-xl border px-4 py-3 text-sm`;
    div.textContent = message;
    $("alerts").appendChild(div);
  }
  function clearAlerts() { $("alerts").innerHTML = ""; }

  async function api(path, options) {
    const response = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, options || {}));
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        const body = await response.json();
        detail = typeof body.detail === "string" ? body.detail
          : (body.detail && (body.detail.mensaje || (Array.isArray(body.detail) && body.detail.map((d) => d.msg).join("; ")))) || detail;
      } catch (e) {}
      const error = new Error(detail); error.status = response.status; throw error;
    }
    return response;
  }
  const json = async (path, options) => (await api(path, options)).json();
  const accountBase = () => `/api/clients/${state.clientId}/accounts/${state.accountId}`;
  const base = () => `${accountBase()}/cost-compare`;

  /* ---------- tema ---------- */
  function applyTheme(dark) {
    document.documentElement.classList.toggle("dark", dark);
    try { localStorage.setItem("hc_theme", dark ? "dark" : "light"); } catch (e) {}
  }
  try { applyTheme(localStorage.getItem("hc_theme") === "dark"); } catch (e) {}
  $("btnDark").addEventListener("click", () => applyTheme(!document.documentElement.classList.contains("dark")));

  /* ---------- carga: cliente → cuenta → región → recurso ---------- */
  async function loadClients() {
    let clients;
    try { clients = await json("/api/admin/clients"); }
    catch (err) {
      notify(err.status === 404 || err.status === 401
        ? "La API interna no está activa. Arranca la aplicación con INVENTORY_ADMIN_API=true y DATABASE_URL configurada."
        : `No se pudieron cargar los clientes: ${err.message}`, "info");
      return;
    }
    $("clientSelect").innerHTML = clients.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("");
    if (!clients.length) { notify("No hay clientes. Créalos con: python manage.py client create", "info"); return; }
    const wanted = deepLink.get("client");
    state.clientId = clients.some((c) => c.id === wanted) ? wanted : clients[0].id;
    $("clientSelect").value = state.clientId;
    await loadAccounts();
  }

  async function loadAccounts() {
    const overview = await json(`/api/clients/${state.clientId}/overview`);
    $("accountSelect").innerHTML = overview.accounts.map((a) => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join("");
    const wanted = deepLink.get("account");
    state.accountId = overview.accounts.some((a) => a.id === wanted) ? wanted : (overview.accounts.length ? overview.accounts[0].id : "");
    if (!state.accountId) { notify("Este cliente no tiene cuentas de Huawei Cloud.", "info"); return; }
    $("accountSelect").value = state.accountId;
    await loadAccount();
  }

  async function loadAccount() {
    state.items = []; state.summary = null; state.compared = false; state.flavors = {};
    const [scans, stats, options] = await Promise.all([json(`${accountBase()}/scans?limit=1`), json(`${accountBase()}/stats`),
      json(`${base()}/options`)]);
    state.options = options;
    // Las pestañas «Configuración libre» y «Catálogo por región» (region_catalog.js) usan la misma cuenta.
    document.dispatchEvent(new CustomEvent("cc:account", { detail: { clientId: state.clientId, accountId: state.accountId, options } }));
    $("billingMode").innerHTML = options.billing_modes.map((m) =>
      `<option value="${esc(m.id)}">${esc(m.label[0].toUpperCase() + m.label.slice(1))}</option>`).join("");
    $("billingMode").value = "monthly";
    $("hours").value = options.hours_per_month;
    syncHours();
    const last = scans[0];
    $("scanInfo").innerHTML = last
      ? `<i data-lucide="database" class="inline w-3.5 h-3.5 -mt-0.5"></i> Configuración real según el inventario actual (escaneo #${esc(last.sequence)}, ${esc(fmtDate(last.finished_at || last.created_at))})`
      : "Esta cuenta aún no tiene escaneos: escanéala desde el dashboard.";
    const regions = Object.keys(stats.by_region).sort();
    $("originRegion").innerHTML = regions.map((r) => `<option value="${esc(r)}">${esc(regionName(regionOf(r)))} · ${esc(r)}</option>`).join("")
      || "<option value=''>Sin recursos</option>";
    const wanted = deepLink.get("resource");
    if (wanted) {
      deepLink.delete("resource");
      try { await setPrimary(wanted, true); } catch (err) { notify(`No se pudo abrir el recurso: ${err.message}`); }
      return;
    }
    await loadResources(true);
  }

  async function loadResources(autoSelect) {
    const region = $("originRegion").value;
    if (!region) { $("resourceSelect").innerHTML = ""; render(); return; }
    const page = await json(`${accountBase()}/resources?region=${encodeURIComponent(region)}&limit=1000&sort=service,name`);
    const groups = {};
    let unsupported = 0;
    page.items.forEach((r) => {
      if (!SUPPORTED[r.resource_type]) { unsupported += 1; return; }
      (groups[r.resource_type] = groups[r.resource_type] || []).push(r);
    });
    $("resourceSelect").innerHTML = Object.keys(SUPPORTED).filter((t) => groups[t]).map((t) =>
      `<optgroup label="${esc(SUPPORTED[t])}">${groups[t].map((r) =>
        `<option value="${esc(r.id)}">${esc(r.name || r.provider_id)}${r.status ? ` · ${esc(r.status)}` : ""}</option>`).join("")}</optgroup>`).join("")
      || "<option value=''>No hay ECS, EVS ni EIP en esta región</option>";
    $("resourceHint").textContent = unsupported
      ? `${unsupported} recurso(s) de otros servicios no aparecen: el comparador admite ECS, EVS y EIP.` : "";
    if (autoSelect && $("resourceSelect").value) await setPrimary($("resourceSelect").value, false);
    else render();
  }

  async function loadItem(id) {
    const origin = await json(`${base()}/origin/${encodeURIComponent(id)}`);
    const dest = clone(origin.config);
    delete dest.notes;
    dest.region = defaultDestination(origin.config.region);
    const item = { key: ++state.seq, resource: origin.resource, origin: origin.config, initialDest: clone(dest),
      dest, assumptions: {}, result: null };
    await ensureFlavors(dest.region);
    return item;
  }

  /** El recurso elegido pasa a ser el principal (sustituye al anterior si solo había uno). */
  async function setPrimary(id, syncSelectors) {
    const item = await loadItem(id);
    if (syncSelectors) {
      $("originRegion").value = item.origin.region;
      await loadResources(false);
      $("resourceSelect").value = id;
    }
    if (state.items.length <= 1) state.items = [item]; else state.items.push(item);
    invalidate();
    render();
    if (deepLinkCompare) { deepLinkCompare = false; compare(); }
  }

  $("resourceSelect").addEventListener("change", () => {
    if (!$("resourceSelect").value) return;
    clearAlerts();
    if (state.items.length > 1) return;   // con varios recursos se añade con "Agregar"
    setPrimary($("resourceSelect").value, false).catch((e) => notify(`No se pudo cargar el recurso: ${e.message}`));
  });
  $("btnAdd").addEventListener("click", async () => {
    const id = $("resourceSelect").value;
    if (!id) return;
    clearAlerts();
    if (state.items.some((i) => i.resource.id === id)) { notify("Ese recurso ya está en la comparación.", "info"); return; }
    try { state.items.push(await loadItem(id)); invalidate(); render(); }
    catch (err) { notify(`No se pudo agregar el recurso: ${err.message}`); }
  });
  $("clientSelect").addEventListener("change", () => { state.clientId = $("clientSelect").value; clearAlerts(); loadAccounts().catch((e) => notify(e.message)); });
  $("accountSelect").addEventListener("change", () => { state.accountId = $("accountSelect").value; clearAlerts(); loadAccount().catch((e) => notify(e.message)); });
  $("originRegion").addEventListener("change", () => loadResources(state.items.length <= 1).catch((e) => notify(e.message)));

  /* ---------- región destino y flavors ---------- */
  function defaultDestination(origin) {
    const others = state.options.regions.filter((r) => r.id !== origin);
    const best = others.find((r) => r.official_prices) || others.find((r) => r.has_project) || others[0];
    return best ? best.id : origin;
  }

  async function ensureFlavors(region, force) {
    if (!region || (state.flavors[region] && !force)) return;
    const options = await json(`${base()}/options?region=${encodeURIComponent(region)}`);
    state.options.regions = options.regions;   // contadores de precios actualizados
    state.flavors[region] = { list: options.flavors, updated: options.flavors_updated_at };
  }
  const flavorInfo = (region, id) => ((state.flavors[region] || { list: [] }).list).find((f) => f.flavor_id === id);

  /* ---------- petición ---------- */
  function destinationPayload(item) {
    const d = item.dest, a = item.assumptions;
    const payload = {
      region: d.region,
      disks: d.disks.map((x) => ({ volume_type: x.volume_type || null, size_gb: Number(x.size_gb) || null, name: x.name || null })),
      public_ips: d.public_ips.map((x) => ({ ip_type: x.ip_type || null, bandwidth_mbps: Number(x.bandwidth_mbps) || null,
        bandwidth_mode: x.bandwidth_mode || a.bandwidth_mode || null, shared: !!x.shared })),
    };
    if (item.origin.kind === "ecs") {
      Object.assign(payload, { flavor: d.flavor || null, vcpus: Number(d.vcpus) || null, ram_gb: Number(d.ram_gb) || null,
        os_type: d.os_type || a.os_type || null });
    }
    return payload;
  }
  function requestBody() {
    return {
      region: state.items[0].dest.region, billing_mode: $("billingMode").value, hours_per_month: Number($("hours").value) || 730,
      items: state.items.map((item) => {
        const assumptions = {};
        Object.entries(item.assumptions).forEach(([k, v]) => { if (v) assumptions[k] = v; });
        return { resource_id: item.resource.id, destination: destinationPayload(item), origin_assumptions: assumptions };
      }),
    };
  }

  /* ---------- comparar, exportar, consultar ---------- */
  function invalidate() { state.items.forEach((i) => { i.stale = true; }); if (state.compared) scheduleCompare(); }
  function scheduleCompare() { clearTimeout(state.timer); state.timer = setTimeout(compare, 350); }

  async function compare() {
    if (!state.items.length) { notify("Elige un recurso de origen.", "info"); return; }
    state.busy = true; renderBusy();
    try {
      const result = await json(`${base()}/compare`, { method: "POST", body: JSON.stringify(requestBody()) });
      result.items.forEach((r, i) => { if (state.items[i]) { state.items[i].result = r; state.items[i].stale = false; } });
      state.summary = result.summary;
      state.compared = true;
    } catch (err) { notify(`No se pudo comparar: ${err.message}`); }
    finally { state.busy = false; render(); }
  }
  $("btnCompare").addEventListener("click", () => { clearAlerts(); compare().then(() => {
    const target = $("result"); if (target.firstElementChild) target.scrollIntoView({ behavior: "smooth", block: "start" });
  }); });

  function syncHours() { $("hoursField").classList.toggle("hidden", $("billingMode").value !== "on_demand"); }
  $("billingMode").addEventListener("change", () => { syncHours(); invalidate(); render(); });
  $("hours").addEventListener("change", () => { invalidate(); render(); });

  async function exportComparison(format) {
    try {
      const response = await api(`${base()}/compare/export?format=${format}`, { method: "POST", body: JSON.stringify(requestBody()) });
      const blob = await response.blob();
      const match = /filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") || "");
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = match ? match[1] : `comparacion_regiones.${format}`;
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    } catch (err) { notify(`No se pudo exportar: ${err.message}`); }
  }

  $("btnRefreshPrices").addEventListener("click", async () => {
    if (!state.items.length) return;
    clearAlerts();
    $("btnRefreshPrices").disabled = true;
    try {
      const result = await json(`${base()}/prices/refresh`, { method: "POST", body: JSON.stringify(requestBody()) });
      notify(`Huawei Cloud devolvió ${result.stored} precio(s) oficial(es).`, "ok");
      if (result.failures.length) {
        notify(`Sin precio oficial para ${result.failures.length} componente(s): ` +
          result.failures.slice(0, 6).map((f) => `${f.component} en ${f.region} (${f.reason})`).join("; ") +
          (result.failures.length > 6 ? "…" : ""), "info");
      }
      await Promise.all(state.items.map((i) => ensureFlavors(i.dest.region, true)));
      await compare();
    } catch (err) {
      notify(err.status === 403 ? "Tu rol no permite consultar a Huawei Cloud (requiere operador)." : `No se pudieron consultar precios: ${err.message}`);
    } finally { $("btnRefreshPrices").disabled = false; }
  });

  async function refreshFlavors(region) {
    clearAlerts();
    try {
      const result = await json(`${base()}/flavors/refresh?region=${encodeURIComponent(region)}`, { method: "POST" });
      notify(`${result.flavors} flavors oficiales disponibles en ${regionName(regionOf(region))}.`, "ok");
      await ensureFlavors(region, true);
      render();
    } catch (err) {
      notify(err.status === 403 ? "Tu rol no permite consultar a Huawei Cloud (requiere operador)." : `No se pudieron consultar los flavors: ${err.message}`);
    }
  }

  /* =====================================================================
   * RENDER
   * ===================================================================== */
  const opt = (values, selected, labels, empty) => (empty !== undefined ? `<option value="">${esc(empty)}</option>` : "") +
    values.map((v) => `<option value="${esc(v)}" ${v === selected ? "selected" : ""}>${esc((labels && labels[v]) || v)}</option>`).join("");

  function specLine(config) {
    if (config.kind !== "ecs") return "";
    const parts = [];
    parts.push(config.vcpus ? `${config.vcpus} vCPU` : "vCPU ?");
    parts.push(config.ram_gb ? `${config.ram_gb} GB RAM` : "RAM ?");
    parts.push(OS_LABEL[config.os_type] || "SO desconocido");
    return parts.join(" · ");
  }

  function componentRows(config) {
    const rows = [];
    config.disks.forEach((d) => rows.push(["hard-drive", "EVS", `${d.volume_type || "tipo ?"} · ${d.size_gb} GB`]));
    config.public_ips.forEach((i) => {
      rows.push(["globe", "EIP", i.ip_type || "tipo ?"]);
      rows.push(["activity", "Ancho de banda", `${i.bandwidth_mbps || "?"} Mbps · ${i.shared ? "compartido" : MODE_LABEL[i.bandwidth_mode] || "modo de cobro ?"}`]);
    });
    if (!rows.length) return "";
    return `<ul class="mt-3 space-y-1.5 text-sm">${rows.map(([icon, label, value]) => `<li class="flex items-center gap-2">
      <i data-lucide="${icon}" class="w-3.5 h-3.5 text-slate-400 shrink-0"></i><span class="text-slate-500 w-28 shrink-0">${esc(label)}</span>
      <span class="font-medium truncate">${esc(value)}</span></li>`).join("")}</ul>`;
  }

  function costBlock(side, item, tone) {
    const stale = item.stale && state.compared;
    let value;
    if (!item.result) value = '<span class="text-slate-400 text-base font-medium">Pulsa «Comparar costos»</span>';
    else if (side.total !== null) value = `<span class="text-3xl md:text-4xl font-extrabold tracking-tight">${esc(money(side.total, side.currency))}</span>
      <span class="text-sm text-slate-500 font-medium"> / mes</span>`;
    else value = `<span class="text-lg">${NA}</span>`;
    const note = item.result && side.total === null && side.reason ? `<p class="text-xs text-amber-700 dark:text-amber-400 mt-1">${esc(side.reason)}</p>` : "";
    const currency = item.result && side.currency ? `<span class="text-xs text-slate-500 ml-1 normal-case">${esc(side.currency)}</span>` : "";
    return `<div class="mt-auto pt-4 border-t ${tone === "dest" ? "border-blue-100 dark:border-blue-900/50" : "border-slate-200 dark:border-slate-700/60"} ${stale ? "opacity-50" : ""}">
      <div class="text-[11px] font-semibold uppercase tracking-wide text-slate-500">Costo estimado ${currency}</div>
      <div class="mt-0.5">${value}</div>${note}</div>`;
  }

  function originCard(item) {
    const o = item.origin, region = regionOf(o.region);
    const result = item.result;
    const needsOs = o.kind === "ecs" && !o.os_type;
    const needsMode = o.public_ips.some((i) => !i.bandwidth_mode && !i.shared);
    return `<div class="min-w-0 flex flex-col rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 p-5 shadow-sm">
      <div class="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wider text-slate-500">
        <span class="w-2 h-2 rounded-full bg-slate-400"></span>Origen · recurso real del inventario</div>
      <div class="mt-3 flex items-baseline gap-2 min-w-0">
        <span class="px-2 py-0.5 rounded-md bg-slate-100 dark:bg-slate-800 text-xs font-bold">${esc(KIND[o.kind])}</span>
        <h3 class="text-xl font-bold truncate" title="${esc(item.resource.name || item.resource.provider_id)}">${esc(item.resource.name || item.resource.provider_id)}</h3>
      </div>
      <div class="mt-3">
        <div class="text-[11px] font-semibold uppercase tracking-wide text-slate-400">Región</div>
        <div class="text-lg font-semibold">${esc(regionName(region))} <code class="ml-1 text-xs font-mono px-1.5 py-0.5 rounded bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300">${esc(o.region)}</code></div>
      </div>
      ${o.kind === "ecs" ? `<div class="mt-3">
        <div class="text-[11px] font-semibold uppercase tracking-wide text-slate-400">Flavor</div>
        <div class="text-lg font-mono font-semibold">${esc(o.flavor || "desconocido")}</div>
        <div class="text-sm text-slate-600 dark:text-slate-400">${esc(specLine(Object.assign({}, o, { os_type: o.os_type || item.assumptions.os_type })))}</div></div>` : ""}
      ${componentRows(o)}
      ${needsOs || needsMode ? `<div class="mt-4 rounded-xl border border-amber-200 dark:border-amber-900/60 bg-amber-50 dark:bg-amber-950/20 p-3 text-xs space-y-2">
        <p class="font-semibold text-amber-800 dark:text-amber-300 flex items-center gap-1.5"><i data-lucide="help-circle" class="w-3.5 h-3.5"></i>El inventario no tiene este dato; indícalo para poder cotizar</p>
        ${needsOs ? `<label class="flex items-center justify-between gap-2">Sistema operativo <select data-assume="os_type" class="input-sm">${opt(["linux", "windows"], item.assumptions.os_type, OS_LABEL, "— elegir —")}</select></label>` : ""}
        ${needsMode ? `<label class="flex items-center justify-between gap-2">Cobro del ancho de banda <select data-assume="bandwidth_mode" class="input-sm">${opt(["bandwidth", "traffic"], item.assumptions.bandwidth_mode, MODE_LABEL, "— elegir —")}</select></label>` : ""}
      </div>` : ""}
      ${costBlock(result ? result.origin : {}, item, "origin")}
    </div>`;
  }

  function regionChips(regionId) {
    const r = regionOf(regionId), flavors = state.flavors[regionId];
    const chip = (ok, text, title) => `<span title="${esc(title || "")}" class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] ${ok
      ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300" : "bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400"}">${esc(text)}</span>`;
    return [
      chip(r.has_project, r.has_project ? "Project ID ✓" : "Sin Project ID", "Necesario para consultar precios oficiales y flavors de esta región"),
      chip(r.official_prices > 0, `${r.official_prices || 0} precios oficiales`),
      chip(flavors && flavors.list.length, flavors && flavors.list.length ? `${flavors.list.length} flavors oficiales` : "sin lista de flavors",
        flavors && flavors.updated ? `Consultada ${fmtDate(flavors.updated)}` : ""),
    ].join(" ");
  }

  function destCard(item) {
    const d = item.dest, ecs = item.origin.kind === "ecs";
    const regions = state.options.regions;
    const flavors = state.flavors[d.region] || { list: [] };
    const match = ecs ? flavorInfo(d.region, d.flavor) : null;
    const os = d.os_type || item.assumptions.os_type || item.origin.os_type;
    const spec = match ? { kind: "ecs", vcpus: match.vcpus, ram_gb: match.ram_gb, os_type: os } : { kind: "ecs", vcpus: d.vcpus, ram_gb: d.ram_gb, os_type: os };
    let flavorNote = "";
    if (ecs && flavors.list.length && !match) flavorNote = `<p class="text-xs text-amber-700 dark:text-amber-400 mt-1">Este flavor no está en la lista oficial de ${esc(regionName(regionOf(d.region)))}.</p>`;
    if (ecs && !flavors.list.length) flavorNote = `<p class="text-xs text-slate-500 mt-1">Sin lista oficial de flavors para esta región: no se puede confirmar que exista.
      ${regionOf(d.region).has_project ? `<button data-refresh-flavors class="text-blue-600 hover:underline">Consultar flavors</button>` : ""}</p>`;
    return `<div class="min-w-0 flex flex-col rounded-2xl border-2 border-blue-500/70 dark:border-blue-500/50 bg-white dark:bg-slate-900 p-5 shadow-sm shadow-blue-500/10">
      <div class="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wider text-blue-700 dark:text-blue-300">
        <span class="w-2 h-2 rounded-full bg-blue-500"></span>Destino · configuración simulada
        <button data-reset class="ml-auto normal-case tracking-normal font-medium text-xs text-blue-600 hover:underline">Restablecer destino</button></div>

      <label class="block mt-3">
        <span class="text-[11px] font-semibold uppercase tracking-wide text-slate-400">Región destino</span>
        <select data-dest="region" class="mt-1 w-full min-w-0 rounded-xl border-2 border-blue-200 dark:border-blue-900 bg-blue-50/50 dark:bg-blue-950/20 px-3 py-2.5 text-base font-semibold focus:border-blue-500 focus:outline-none">
          ${regions.map((r) => `<option value="${esc(r.id)}" ${r.id === d.region ? "selected" : ""}>${esc(regionName(r))} — ${esc(r.id)}${r.id === item.origin.region ? " (origen)" : ""}</option>`).join("")}
        </select>
      </label>
      <div class="mt-1.5 flex flex-wrap gap-1">${regionChips(d.region)}</div>

      ${ecs ? `<div class="mt-4">
        <span class="text-[11px] font-semibold uppercase tracking-wide text-slate-400">Flavor</span>
        <div class="relative mt-1" data-combo>
          <i data-lucide="search" class="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-slate-400 pointer-events-none"></i>
          <input data-flavor-input value="${esc(d.flavor || "")}" autocomplete="off" spellcheck="false" placeholder="Buscar flavor…"
            class="w-full rounded-xl border-2 border-blue-200 dark:border-blue-900 bg-blue-50/50 dark:bg-blue-950/20 pl-9 pr-9 py-2.5 text-base font-mono font-semibold focus:border-blue-500 focus:outline-none" />
          <i data-lucide="chevrons-up-down" class="w-4 h-4 absolute right-3 top-1/2 -translate-y-1/2 text-slate-400 pointer-events-none"></i>
          <div data-flavor-list class="hidden absolute z-30 mt-1 w-full max-h-72 overflow-auto rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 shadow-xl"></div>
        </div>
        <div class="mt-1.5 text-sm font-medium text-slate-700 dark:text-slate-300">${esc(specLine(spec))}</div>
        ${flavorNote}
      </div>` : ""}

      ${componentRows(d)}

      <details class="mt-3" ${item.openAdvanced ? "open" : ""} data-advanced>
        <summary class="text-xs font-semibold text-blue-600 cursor-pointer select-none">Ajustar ${ecs ? "sistema operativo, " : ""}discos y red</summary>
        <div class="mt-3 space-y-3 text-sm">
          ${ecs ? `<div class="grid grid-cols-3 gap-2">
            <label class="field">Sistema operativo<select data-dest="os_type" class="input-sm">${opt(["linux", "windows"], d.os_type, OS_LABEL, "según origen")}</select></label>
            <label class="field">vCPU<input data-dest="vcpus" type="number" min="1" max="512" value="${esc(d.vcpus ?? "")}" class="input-sm" ${match ? "disabled title='Lo fija el flavor oficial'" : ""} /></label>
            <label class="field">RAM (GB)<input data-dest="ram_gb" type="number" min="0.5" step="0.5" value="${esc(d.ram_gb ?? "")}" class="input-sm" ${match ? "disabled title='Lo fija el flavor oficial'" : ""} /></label>
          </div>` : ""}
          <div><div class="text-xs font-semibold text-slate-500 mb-1">Discos EVS</div><div class="space-y-1.5">${d.disks.map((x, i) => `<div class="flex items-center gap-2">
              <select data-disk="${i}" data-field="volume_type" class="input-sm">${opt(state.options.disk_types, x.volume_type, null, "tipo ?")}</select>
              <input data-disk="${i}" data-field="size_gb" type="number" min="1" max="65536" value="${esc(x.size_gb)}" class="input-sm w-24" /><span class="text-xs">GB</span>
              <button data-remove-disk="${i}" class="ml-auto text-xs text-slate-500 hover:text-red-600">quitar</button></div>`).join("")}</div>
            <button data-add-disk class="mt-1 text-xs text-blue-600 hover:underline">+ disco</button></div>
          <div><div class="text-xs font-semibold text-slate-500 mb-1">EIP y ancho de banda</div><div class="space-y-1.5">${d.public_ips.map((x, i) => `<div class="flex flex-wrap items-center gap-2">
              <input data-ip="${i}" data-field="ip_type" value="${esc(x.ip_type || "")}" placeholder="5_bgp" class="input-sm w-24" />
              <input data-ip="${i}" data-field="bandwidth_mbps" type="number" min="1" max="2000" value="${esc(x.bandwidth_mbps || "")}" class="input-sm w-20" /><span class="text-xs">Mbps</span>
              <select data-ip="${i}" data-field="bandwidth_mode" class="input-sm">${opt(["bandwidth", "traffic"], x.bandwidth_mode, MODE_LABEL, "según origen")}</select>
              <button data-remove-ip="${i}" class="ml-auto text-xs text-slate-500 hover:text-red-600">quitar</button></div>`).join("")}</div>
            <button data-add-ip class="mt-1 text-xs text-blue-600 hover:underline">+ EIP</button></div>
        </div>
      </details>

      ${costBlock(item.result ? item.result.destination : {}, item, "dest")}
    </div>`;
  }

  function itemBlock(item, index) {
    const many = state.items.length > 1;
    return `<article data-index="${index}">
      ${many ? `<div class="flex items-center gap-2 mb-2"><span class="text-xs font-semibold text-slate-500">Recurso ${index + 1} de ${state.items.length}</span>
        <button data-remove class="ml-auto text-xs text-slate-500 hover:text-red-600 hover:underline">Quitar de la comparación</button></div>` : ""}
      <div class="grid grid-cols-[minmax(0,1fr)] lg:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] gap-3 lg:gap-4 items-stretch">
        ${originCard(item)}
        <div class="flex lg:flex-col items-center justify-center gap-2 py-1">
          <span class="hidden lg:block w-px flex-1 bg-gradient-to-b from-transparent via-slate-300 dark:via-slate-700 to-transparent"></span>
          <span class="grid place-items-center w-11 h-11 rounded-full bg-slate-900 dark:bg-white text-white dark:text-slate-900 text-xs font-extrabold shadow-lg">VS</span>
          <i data-lucide="arrow-down" class="w-4 h-4 text-slate-400 lg:hidden"></i>
          <i data-lucide="arrow-right" class="w-4 h-4 text-slate-400 hidden lg:block"></i>
          <span class="hidden lg:block w-px flex-1 bg-gradient-to-b from-transparent via-slate-300 dark:via-slate-700 to-transparent"></span>
        </div>
        ${destCard(item)}
      </div>
    </article>`;
  }

  /* ---------- resultado ---------- */
  function sources(item) {
    const r = item.result;
    return [...new Set([...r.origin.sources, ...r.destination.sources])];
  }
  function oldest(item) {
    const dates = [item.result.origin.updated_at, item.result.destination.updated_at].filter(Boolean).sort();
    return dates[0] || null;
  }

  const NA_METRIC = '<span class="text-base md:text-lg text-amber-700 dark:text-amber-400 font-bold">Precio no disponible</span>';
  function metric(label, value, hint, emphasis) {
    return `<div class="min-w-0 rounded-2xl ${emphasis ? "bg-slate-900 text-white dark:bg-white dark:text-slate-900" : "bg-slate-50 dark:bg-slate-800/50"} p-4 md:p-5">
      <div class="text-[11px] font-semibold uppercase tracking-wider ${emphasis ? "opacity-70" : "text-slate-500"}">${esc(label)}</div>
      <div class="mt-1 text-2xl md:text-3xl font-extrabold tracking-tight">${value}</div>
      ${hint ? `<div class="text-xs mt-0.5 ${emphasis ? "opacity-70" : "text-slate-500"}">${hint}</div>` : ""}</div>`;
  }

  function renderResult() {
    if (!state.compared || !state.items.length || !state.items.every((i) => i.result)) { $("result").innerHTML = ""; return; }
    const s = state.summary;
    let originTotal, destTotal, diff, pct, currency, sentence;
    if (state.items.length === 1) {
      const r = state.items[0].result;
      originTotal = r.origin.total; destTotal = r.destination.total; diff = r.difference; pct = r.difference_percent;
      currency = r.currency || r.origin.currency || r.destination.currency;
      const extras = [r.origin_config.disks.length ? `${r.origin_config.disks.length} disco(s)` : "",
        r.origin_config.public_ips.length ? `${r.origin_config.public_ips.length} EIP` : ""].filter(Boolean);
      const what = r.origin_config.kind === "ecs" ? `El ECS completo${extras.length ? ` (con ${extras.join(" y ")})` : ""}` : `El ${KIND[r.origin_config.kind]}`;
      const destName = regionName(regionOf(r.destination_config.region));
      sentence = originTotal !== null && destTotal !== null
        ? `${esc(what)} cuesta actualmente unos <b>${esc(money(originTotal, r.origin.currency))}/mes</b> en ${esc(regionName(regionOf(r.origin_config.region)))}; en ${esc(destName)} costaría <b>${esc(money(destTotal, r.destination.currency))}/mes</b>.`
        : `${esc(what)}: ${originTotal === null ? "falta algún precio del origen" : "falta algún precio del destino"}, así que no se puede calcular la diferencia. Revisa el detalle por componente.`;
    } else {
      const group = s.by_currency[0];
      originTotal = group ? group.origin : null; destTotal = group ? group.destination : null;
      diff = group ? group.difference : null; pct = group ? group.difference_percent : null; currency = group ? group.currency : null;
      sentence = `${s.compared} de ${s.items} recursos tienen todos sus precios y entran en el total${s.not_compared ? `; ${s.not_compared} quedan fuera por falta de precio` : ""}.`
        + (s.by_currency.length > 1 ? " Hay varias monedas: se muestra la primera; el detalle tiene el resto." : "");
    }
    const allSources = [...new Set(state.items.flatMap(sources))];
    const dates = state.items.map(oldest).filter(Boolean).sort();
    const has = (v) => v !== null && v !== undefined;
    $("result").innerHTML = `<div class="bg-white dark:bg-slate-900 rounded-3xl border border-slate-200 dark:border-slate-800 p-5 md:p-6 shadow-sm">
      <div class="flex flex-wrap items-center gap-2 mb-4">
        <h2 class="text-lg font-bold mr-auto flex items-center gap-2"><i data-lucide="scale" class="w-5 h-5 text-blue-600"></i>Resultado</h2>
        <button data-export="xlsx" class="btn-secondary"><i data-lucide="file-spreadsheet" class="w-4 h-4"></i>Exportar comparación</button>
        <button data-export="csv" class="btn-secondary">CSV</button>
      </div>
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
        ${metric("Costo actual", has(originTotal) ? `${esc(money(originTotal, currency))}<span class="text-sm font-medium opacity-60"> / mes</span>` : NA_METRIC)}
        ${metric("Costo destino", has(destTotal) ? `${esc(money(destTotal, currency))}<span class="text-sm font-medium opacity-60"> / mes</span>` : NA_METRIC)}
        ${metric("Diferencia", has(diff) ? `${esc(signedMoney(diff, currency))}<span class="text-sm font-medium opacity-60"> / mes</span>` : "—",
          has(diff) ? "destino − origen" : "requiere ambos precios", true)}
        ${metric("Cambio", has(pct) ? esc(signedPercent(pct)) : "—", has(pct) ? "respecto al costo actual" : "requiere ambos precios", true)}
      </div>
      <p class="mt-4 text-sm text-slate-700 dark:text-slate-300">${sentence}</p>
      <dl class="mt-4 grid grid-cols-2 md:grid-cols-5 gap-3 text-xs">
        <div><dt class="text-slate-400 font-semibold uppercase tracking-wide">Moneda</dt><dd class="mt-0.5 font-medium">${esc(currency || "—")}</dd></div>
        <div><dt class="text-slate-400 font-semibold uppercase tracking-wide">Período</dt><dd class="mt-0.5 font-medium">${esc(s.period)}</dd></div>
        <div><dt class="text-slate-400 font-semibold uppercase tracking-wide">Modelo de cobro</dt><dd class="mt-0.5 font-medium">${esc(s.billing_label)}</dd></div>
        <div class="col-span-2 md:col-span-1"><dt class="text-slate-400 font-semibold uppercase tracking-wide">Fuente</dt><dd class="mt-0.5 font-medium break-words">${esc(allSources.join("; ") || "ninguna (sin precios)")}</dd></div>
        <div><dt class="text-slate-400 font-semibold uppercase tracking-wide">Precio actualizado</dt><dd class="mt-0.5 font-medium">${esc(dates.length ? fmtDate(dates[0]) : "—")}</dd></div>
      </dl>
    </div>`;
  }

  /* ---------- detalle por componente ---------- */
  function alignLines(item) {
    const group = (side) => {
      const g = {};
      side.lines.forEach((l) => { (g[l.product] = g[l.product] || []).push(l); });
      return g;
    };
    const o = group(item.result.origin), d = group(item.result.destination);
    const rows = [];
    PRODUCT_ORDER.forEach((p) => {
      const n = Math.max((o[p] || []).length, (d[p] || []).length);
      for (let i = 0; i < n; i += 1) rows.push({ product: p, origin: (o[p] || [])[i], dest: (d[p] || [])[i] });
    });
    return rows;
  }
  const lineAmount = (line) => (!line ? '<span class="text-slate-400">—</span>'
    : line.available ? esc(money(line.monthly_amount, line.currency)) : `<span class="text-xs">${NA}</span>`);
  function lineDiff(row) {
    if (!row.origin || !row.dest || !row.origin.available || !row.dest.available || row.origin.currency !== row.dest.currency) return '<span class="text-slate-400">—</span>';
    return esc(signedMoney(Number(row.dest.monthly_amount) - Number(row.origin.monthly_amount), row.dest.currency));
  }
  function conceptLabel(row) {
    const strip = (l) => (l ? l.label.replace(/^(ECS|EVS|EIP)\s*/, "").replace(/^Ancho de banda\s*/, "") : "");
    const a = strip(row.origin), b = strip(row.dest);
    return `<div class="font-semibold">${esc(PRODUCT_LABEL[row.product])}</div>
      <div class="text-xs text-slate-500">${esc(a || "—")}${b && b !== a ? ` → <span class="text-blue-700 dark:text-blue-300">${esc(b)}</span>` : ""}</div>`;
  }

  function detailBlock(item) {
    const r = item.result, rows = alignLines(item);
    const o = r.origin, d = r.destination;
    const cfgRows = [["Región", `${regionName(regionOf(r.origin_config.region))} (${r.origin_config.region})`, `${regionName(regionOf(r.destination_config.region))} (${r.destination_config.region})`]];
    if (r.origin_config.kind === "ecs") {
      cfgRows.push(["Flavor", r.origin_config.flavor || "—", r.destination_config.flavor || "—"]);
      cfgRows.push(["vCPU · RAM · SO", specLine(r.origin_config), specLine(r.destination_config)]);
    }
    const lineMeta = (l) => (l.available ? `${esc(l.basis)} · ${esc(l.source || "")}${l.updated_at ? ` · ${esc(fmtDate(l.updated_at))}` : ""}` : esc(l.reason));
    return `<div class="bg-white dark:bg-slate-900 rounded-2xl border border-slate-200 dark:border-slate-800 overflow-hidden">
      <div class="px-5 py-4 border-b border-slate-200 dark:border-slate-800 flex flex-wrap items-baseline gap-2">
        <h3 class="font-bold mr-auto">Comparación por componente · ${esc(r.resource.name || r.resource.provider_id)}</h3>
        <span class="text-xs text-slate-500">${esc(state.summary.billing_label)} · importes mensuales</span></div>
      <div class="overflow-x-auto"><table class="min-w-full text-sm">
        <thead class="bg-slate-50 dark:bg-slate-800/60 text-[11px] uppercase tracking-wide text-slate-500">
          <tr><th class="px-5 py-2.5 text-left">Concepto</th><th class="px-5 py-2.5 text-right">Origen<div class="normal-case font-normal">${esc(r.origin_config.region)}</div></th>
            <th class="px-5 py-2.5 text-right">Destino<div class="normal-case font-normal">${esc(r.destination_config.region)}</div></th><th class="px-5 py-2.5 text-right">Diferencia</th></tr></thead>
        <tbody class="divide-y divide-slate-100 dark:divide-slate-800">
          ${rows.map((row) => `<tr><td class="px-5 py-3">${conceptLabel(row)}</td><td class="px-5 py-3 text-right whitespace-nowrap">${lineAmount(row.origin)}</td>
            <td class="px-5 py-3 text-right whitespace-nowrap">${lineAmount(row.dest)}</td><td class="px-5 py-3 text-right whitespace-nowrap text-slate-600 dark:text-slate-300">${lineDiff(row)}</td></tr>`).join("")}
        </tbody>
        <tfoot class="border-t-2 border-slate-300 dark:border-slate-700">
          <tr class="font-bold"><td class="px-5 py-3">TOTAL</td><td class="px-5 py-3 text-right whitespace-nowrap">${o.total !== null ? esc(money(o.total, o.currency)) : NA}</td>
            <td class="px-5 py-3 text-right whitespace-nowrap">${d.total !== null ? esc(money(d.total, d.currency)) : NA}</td>
            <td class="px-5 py-3 text-right whitespace-nowrap">${r.difference !== null ? esc(signedMoney(r.difference, r.currency)) : "—"}</td></tr>
          <tr class="text-sm"><td class="px-5 pb-3 text-slate-500">Diferencia</td><td colspan="3" class="px-5 pb-3 text-right font-semibold">${r.difference !== null
            ? `${esc(signedMoney(r.difference, r.currency))} / mes · ${r.difference_percent !== null ? esc(signedPercent(r.difference_percent)) : "— %"}` : `<span class="text-amber-700 dark:text-amber-400 font-normal">${esc(r.reason || "No se puede calcular")}</span>`}</td></tr>
        </tfoot></table></div>
      ${r.warnings.length ? `<ul class="px-5 pb-3 text-xs text-amber-700 dark:text-amber-400 list-disc ml-5 space-y-0.5">${r.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
      <details class="border-t border-slate-200 dark:border-slate-800">
        <summary class="px-5 py-3 text-xs font-semibold text-blue-600 cursor-pointer select-none">Detalle técnico: configuración y fuente de cada precio</summary>
        <div class="px-5 pb-5 space-y-4 text-sm">
          <table class="w-full text-sm"><thead class="text-[11px] uppercase text-slate-500"><tr><th class="text-left py-1">Concepto</th><th class="text-left py-1">Origen</th><th class="text-left py-1">Destino</th></tr></thead>
            <tbody>${cfgRows.map(([k, a, b]) => `<tr class="border-t border-slate-100 dark:border-slate-800"><td class="py-1.5 text-slate-500">${esc(k)}</td><td class="py-1.5">${esc(a)}</td><td class="py-1.5 font-medium">${esc(b)}</td></tr>`).join("")}</tbody></table>
          <div class="grid grid-cols-1 lg:grid-cols-2 gap-4 text-xs">
            ${[["Origen", o], ["Destino", d]].map(([label, side]) => `<div><h5 class="font-bold text-slate-500 mb-1">${label}</h5>
              <ul class="space-y-1.5">${side.lines.map((l) => `<li><span class="font-semibold">${esc(l.label)}</span><br><span class="text-slate-500">${lineMeta(l)}</span></li>`).join("")}</ul></div>`).join("")}
          </div>
        </div>
      </details>
    </div>`;
  }

  function renderDetails() {
    const ready = state.compared && state.items.length && state.items.every((i) => i.result);
    $("details").innerHTML = ready ? state.items.map(detailBlock).join("") : "";
  }

  function renderBusy() {
    $("btnCompare").disabled = state.busy;
    $("btnCompare").innerHTML = state.busy ? '<i data-lucide="loader" class="w-5 h-5 animate-spin"></i>Comparando…'
      : '<i data-lucide="scale" class="w-5 h-5"></i>Comparar costos';
    icons();
  }

  function render() {
    $("actionBar").classList.toggle("hidden", !state.items.length);
    $("items").innerHTML = state.items.length ? state.items.map(itemBlock).join("")
      : `<div class="rounded-3xl border-2 border-dashed border-slate-300 dark:border-slate-700 p-10 text-center">
          <i data-lucide="arrow-left-right" class="w-8 h-8 mx-auto text-slate-400"></i>
          <p class="mt-3 font-semibold">Elige un recurso de origen</p>
          <p class="text-sm text-slate-500">Se cargará su configuración real y podrás simularla en otra región.</p></div>`;
    renderResult();
    renderDetails();
    renderBusy();
  }

  /* ---------- combobox de flavors (búsqueda por nombre) ---------- */
  function showFlavorList(input) {
    const item = itemOf(input);
    const box = input.parentElement.querySelector("[data-flavor-list]");
    const list = (state.flavors[item.dest.region] || { list: [] }).list;
    if (!list.length) { box.classList.add("hidden"); return; }
    const query = input.value.trim().toLowerCase();
    const unchanged = query === (item.dest.flavor || "").toLowerCase();
    const matches = unchanged ? list : list.filter((f) => f.flavor_id.toLowerCase().includes(query));
    const shown = matches.slice(0, 80);
    box.innerHTML = (shown.map((f) => `<button type="button" data-flavor-option="${esc(f.flavor_id)}"
        class="w-full flex items-center gap-3 px-3 py-2 text-left hover:bg-blue-50 dark:hover:bg-blue-950/30 ${f.flavor_id === item.dest.flavor ? "bg-blue-50 dark:bg-blue-950/30" : ""}">
        <span class="font-mono font-semibold text-sm">${esc(f.flavor_id)}</span>
        <span class="ml-auto text-xs text-slate-500 whitespace-nowrap">${esc(f.vcpus)} vCPU · ${esc(f.ram_gb)} GB</span></button>`).join("")
      + (shown.length < matches.length ? `<div class="px-3 py-2 text-xs text-slate-500">${matches.length - shown.length} más: escribe para filtrar</div>` : ""))
      || '<div class="px-3 py-2 text-xs text-slate-500">Ningún flavor oficial coincide. Puedes dejar el nombre escrito.</div>';
    box.classList.remove("hidden");
  }
  function chooseFlavor(item, value) {
    const flavor = (value || "").trim().toLowerCase();
    if (flavor === (item.dest.flavor || "")) return;
    item.dest.flavor = flavor || null;
    const info = flavorInfo(item.dest.region, item.dest.flavor);
    if (info) { item.dest.vcpus = info.vcpus; item.dest.ram_gb = info.ram_gb; }
    invalidate();
    render();
  }

  /* ---------- eventos de las tarjetas (delegación) ---------- */
  function itemOf(element) { const card = element.closest("[data-index]"); return card ? state.items[Number(card.dataset.index)] : null; }

  $("items").addEventListener("focusin", (e) => { if (e.target.matches("[data-flavor-input]")) { e.target.select(); showFlavorList(e.target); } });
  $("items").addEventListener("input", (e) => { if (e.target.matches("[data-flavor-input]")) showFlavorList(e.target); });
  $("items").addEventListener("focusout", (e) => {
    if (!e.target.matches("[data-flavor-input]")) return;
    const input = e.target, item = itemOf(input);
    setTimeout(() => {
      if (!document.body.contains(input)) return;
      const box = input.parentElement.querySelector("[data-flavor-list]");
      if (box) box.classList.add("hidden");
      if (item) chooseFlavor(item, input.value);
    }, 150);
  });
  $("items").addEventListener("keydown", (e) => {
    if (!e.target.matches("[data-flavor-input]")) return;
    if (e.key === "Enter") {
      e.preventDefault();
      const first = e.target.parentElement.querySelector("[data-flavor-option]");
      chooseFlavor(itemOf(e.target), first && e.target.value.trim() ? first.dataset.flavorOption : e.target.value);
    } else if (e.key === "Escape") e.target.blur();
  });
  $("items").addEventListener("mousedown", (e) => {
    const option = e.target.closest("[data-flavor-option]");
    if (!option) return;
    e.preventDefault();
    chooseFlavor(itemOf(option), option.dataset.flavorOption);
  });

  $("items").addEventListener("change", async (event) => {
    const el = event.target, item = itemOf(el);
    if (!item || el.matches("[data-flavor-input]")) return;
    const value = el.value === "" ? null : el.value;
    if (el.dataset.assume) item.assumptions[el.dataset.assume] = value;
    else if (el.dataset.dest === "region") {
      item.dest.region = value;
      try { await ensureFlavors(value); } catch (err) { notify(err.message); }
      const info = flavorInfo(value, item.dest.flavor);
      if (info) { item.dest.vcpus = info.vcpus; item.dest.ram_gb = info.ram_gb; }
    } else if (el.dataset.dest) item.dest[el.dataset.dest] = value;
    else if (el.dataset.disk !== undefined) item.dest.disks[Number(el.dataset.disk)][el.dataset.field] = value;
    else if (el.dataset.ip !== undefined) item.dest.public_ips[Number(el.dataset.ip)][el.dataset.field] = value;
    item.openAdvanced = !!el.closest("[data-advanced]");
    invalidate();
    render();
  });

  $("items").addEventListener("toggle", (e) => {
    if (e.target.matches && e.target.matches("[data-advanced]")) { const item = itemOf(e.target); if (item) item.openAdvanced = e.target.open; }
  }, true);

  $("items").addEventListener("click", (event) => {
    const el = event.target.closest("button");
    const item = el && itemOf(el);
    if (!item || el.hasAttribute("data-flavor-option")) return;
    if (el.hasAttribute("data-refresh-flavors")) { refreshFlavors(item.dest.region); return; }
    if (el.hasAttribute("data-remove")) state.items.splice(state.items.indexOf(item), 1);
    else if (el.hasAttribute("data-reset")) { item.dest = clone(item.initialDest); item.openAdvanced = false; }
    else if (el.hasAttribute("data-add-disk")) { item.dest.disks.push({ volume_type: (state.options.disk_types || [])[0], size_gb: 40 }); item.openAdvanced = true; }
    else if (el.dataset.removeDisk !== undefined) { item.dest.disks.splice(Number(el.dataset.removeDisk), 1); item.openAdvanced = true; }
    else if (el.hasAttribute("data-add-ip")) { item.dest.public_ips.push({ ip_type: "5_bgp", bandwidth_mbps: 5, bandwidth_mode: "bandwidth", shared: false }); item.openAdvanced = true; }
    else if (el.dataset.removeIp !== undefined) { item.dest.public_ips.splice(Number(el.dataset.removeIp), 1); item.openAdvanced = true; }
    else return;
    if (!state.items.length) { state.compared = false; state.summary = null; }
    invalidate();
    render();
  });

  $("result").addEventListener("click", (e) => { const b = e.target.closest("[data-export]"); if (b) exportComparison(b.dataset.export); });

  icons();
  render();
  loadClients().catch((err) => notify(err.message));
})();
