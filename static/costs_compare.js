/* Comparador de costos por región. Vanilla JS sobre la API interna (/cost-compare).
 * Simulación: nunca modifica recursos ni inventario. Todo texto de la nube se escapa. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const SUPPORTED = { "ecs.server": "Servidores (ECS)", "evs.volume": "Discos (EVS)", "eip.publicip": "IPs públicas (EIP)" };
  const OS_LABEL = { linux: "Linux", windows: "Windows" };
  const MODE_LABEL = { bandwidth: "por ancho de banda", traffic: "por tráfico" };
  const state = { clientId: "", accountId: "", options: null, items: [], summary: null, stale: false, seq: 0 };
  // Enlace directo desde el dashboard: ?client=…&account=…&resource=… (solo IDs; se validan en la API)
  const deepLink = new URLSearchParams(location.search);

  /* ---------- utilidades ---------- */
  function esc(value) {
    return String(value === undefined || value === null ? "" : value)
      .replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function icons() { if (window.lucide) window.lucide.createIcons(); }
  function fmtDate(value) { return value ? new Date(value).toLocaleString() : "—"; }
  const clone = (obj) => JSON.parse(JSON.stringify(obj));

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
    div.scrollIntoView({ behavior: "smooth", block: "nearest" });
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

  /* ---------- 1. origen ---------- */
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
    const resource = deepLink.get("resource");
    if (resource) {
      deepLink.delete("resource");
      try { await addResource(resource); await compare(); } catch (err) { notify(`No se pudo abrir el recurso: ${err.message}`); }
    }
  }

  async function loadAccount() {
    state.items = []; state.summary = null;
    const [scans, stats] = await Promise.all([json(`${accountBase()}/scans?limit=1`), json(`${accountBase()}/stats`)]);
    const last = scans[0];
    $("scanSelect").innerHTML = last
      ? `<option title="Configuración según el último escaneo">Actual (escaneo #${esc(last.sequence)}, ${esc(new Date(last.finished_at || last.created_at).toLocaleDateString())})</option>`
      : "<option>Sin escaneos: escanea la cuenta desde el dashboard</option>";
    const regions = Object.keys(stats.by_region).sort();
    $("originRegion").innerHTML = regions.map((r) => `<option>${esc(r)}</option>`).join("") || "<option value=''>Sin recursos</option>";
    await loadOptions();
    await loadResources();
    render();
  }

  async function loadResources() {
    const region = $("originRegion").value;
    if (!region) { $("resourceSelect").innerHTML = ""; return; }
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
      ? `${unsupported} recurso(s) de otros servicios no se muestran: por ahora el comparador admite ECS, EVS y EIP.` : "";
  }

  async function addResource(id) {
    const origin = await json(`${base()}/origin/${encodeURIComponent(id)}`);
    const dest = clone(origin.config);
    delete dest.notes;
    dest.region = null;   // null = usa la región destino global
    state.items.push({ key: ++state.seq, resource: origin.resource, origin: origin.config, initialDest: clone(dest),
      dest, assumptions: {}, result: null });
    state.stale = true;
    render();
  }

  $("btnAdd").addEventListener("click", async () => {
    const id = $("resourceSelect").value;
    if (!id) return;
    clearAlerts();
    try { await addResource(id); } catch (err) { notify(`No se pudo agregar el recurso: ${err.message}`); }
  });

  /* ---------- 2. destino ---------- */
  async function loadOptions() {
    const region = $("destRegion").value;
    const options = await json(`${base()}/options${region ? `?region=${encodeURIComponent(region)}` : ""}`);
    const first = !state.options;
    state.options = options;
    if (first) {
      $("destRegion").innerHTML = options.regions.map((r) => `<option value="${esc(r.id)}">${esc(r.name)} (${esc(r.id)})</option>`).join("");
      $("billingMode").innerHTML = options.billing_modes.map((m) => `<option value="${esc(m.id)}">${esc(m.label[0].toUpperCase() + m.label.slice(1))}</option>`).join("");
      $("billingMode").value = "monthly";
      $("hours").value = options.hours_per_month;
      // Destino por defecto: otra región con precios oficiales guardados o, si no, con Project ID.
      const origin = $("originRegion").value;
      const others = options.regions.filter((r) => r.id !== origin);
      const best = others.find((r) => r.official_prices) || others.find((r) => r.has_project) || others[0];
      if (best) $("destRegion").value = best.id;
      return loadOptions();
    }
    $("flavorList").innerHTML = options.flavors.map((f) => `<option value="${esc(f.flavor_id)}">${esc(f.vcpus)} vCPU · ${esc(f.ram_gb)} GB</option>`).join("");
    const info = options.regions.find((r) => r.id === region) || {};
    const parts = [];
    parts.push(info.has_project ? "La cuenta tiene Project ID en esta región (se pueden consultar precios oficiales)."
      : "La cuenta no tiene Project ID en esta región: no se pueden consultar precios oficiales de BSS para ella.");
    parts.push(options.flavors.length ? `${options.flavors.length} flavors oficiales (consultados ${fmtDate(options.flavors_updated_at)}).`
      : "Sin lista oficial de flavors para esta región.");
    parts.push(`${info.official_prices || 0} precios oficiales guardados.`);
    parts.push(`Tabla de precios: ${options.price_table}.`);
    $("destInfo").textContent = parts.join(" ");
    $("hoursField").classList.toggle("hidden", $("billingMode").value !== "on_demand");
  }

  $("destRegion").addEventListener("change", () => { state.stale = true; loadOptions().then(render).catch((e) => notify(e.message)); });
  $("billingMode").addEventListener("change", () => { state.stale = true; loadOptions().then(render).catch((e) => notify(e.message)); });
  $("hours").addEventListener("change", () => { state.stale = true; render(); });
  $("clientSelect").addEventListener("change", () => { state.clientId = $("clientSelect").value; clearAlerts(); loadAccounts().catch((e) => notify(e.message)); });
  $("accountSelect").addEventListener("change", () => { state.accountId = $("accountSelect").value; clearAlerts(); loadAccount().catch((e) => notify(e.message)); });
  $("originRegion").addEventListener("change", () => loadResources().catch((e) => notify(e.message)));

  /* ---------- cuerpo de la petición ---------- */
  function destinationPayload(item) {
    const d = item.dest, a = item.assumptions;
    const payload = {
      disks: d.disks.map((x) => ({ volume_type: x.volume_type || null, size_gb: Number(x.size_gb) || null, name: x.name || null })),
      public_ips: d.public_ips.map((x) => ({ ip_type: x.ip_type || null, bandwidth_mbps: Number(x.bandwidth_mbps) || null,
        bandwidth_mode: x.bandwidth_mode || a.bandwidth_mode || null, shared: !!x.shared })),
    };
    if (item.origin.kind === "ecs") {
      Object.assign(payload, { flavor: d.flavor || null, vcpus: Number(d.vcpus) || null, ram_gb: Number(d.ram_gb) || null,
        os_type: d.os_type || a.os_type || null });
    }
    if (d.region) payload.region = d.region;
    return payload;
  }
  function requestBody() {
    return {
      region: $("destRegion").value, billing_mode: $("billingMode").value, hours_per_month: Number($("hours").value) || 730,
      items: state.items.map((item) => {
        const assumptions = {};
        Object.entries(item.assumptions).forEach(([k, v]) => { if (v) assumptions[k] = v; });
        return { resource_id: item.resource.id, destination: destinationPayload(item), origin_assumptions: assumptions };
      }),
    };
  }

  /* ---------- 3. comparar, exportar, consultar ---------- */
  async function compare() {
    if (!state.items.length) { notify("Agrega al menos un recurso (paso 1).", "info"); return; }
    clearAlerts();
    $("btnCompare").disabled = true;
    try {
      const result = await json(`${base()}/compare`, { method: "POST", body: JSON.stringify(requestBody()) });
      result.items.forEach((r, i) => { state.items[i].result = r; });
      state.summary = result.summary;
      state.stale = false;
      render();
    } catch (err) { notify(`No se pudo comparar: ${err.message}`); }
    finally { $("btnCompare").disabled = false; }
  }
  $("btnCompare").addEventListener("click", compare);

  async function exportComparison(format) {
    if (!state.items.length) { notify("Agrega al menos un recurso (paso 1).", "info"); return; }
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
  $("btnExportXlsx").addEventListener("click", () => exportComparison("xlsx"));
  $("btnExportCsv").addEventListener("click", () => exportComparison("csv"));

  $("btnRefreshPrices").addEventListener("click", async () => {
    if (!state.items.length) { notify("Agrega primero los recursos cuyos precios quieres consultar.", "info"); return; }
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
      await loadOptions();
      await compare();
    } catch (err) {
      notify(err.status === 403 ? "Tu rol no permite consultar a Huawei Cloud (requiere operador)." : `No se pudieron consultar precios: ${err.message}`);
    } finally { $("btnRefreshPrices").disabled = false; }
  });

  $("btnRefreshFlavors").addEventListener("click", async () => {
    clearAlerts();
    $("btnRefreshFlavors").disabled = true;
    try {
      const result = await json(`${base()}/flavors/refresh?region=${encodeURIComponent($("destRegion").value)}`, { method: "POST" });
      notify(`${result.flavors} flavors oficiales disponibles en ${result.region}.`, "ok");
      await loadOptions();
      render();
    } catch (err) {
      notify(err.status === 403 ? "Tu rol no permite consultar a Huawei Cloud (requiere operador)." : `No se pudieron consultar los flavors: ${err.message}`);
    } finally { $("btnRefreshFlavors").disabled = false; }
  });

  /* ---------- render ---------- */
  const opt = (values, selected, labels, empty) => (empty !== undefined ? `<option value="">${esc(empty)}</option>` : "") +
    values.map((v) => `<option value="${esc(v)}" ${v === selected ? "selected" : ""}>${esc((labels && labels[v]) || v)}</option>`).join("");

  function describe(config) {
    const rows = {};
    rows["Región"] = config.region;
    if (config.kind === "ecs") {
      rows["Flavor"] = config.flavor || "—";
      rows["vCPU"] = config.vcpus ?? "—";
      rows["RAM"] = config.ram_gb ? `${config.ram_gb} GB` : "—";
      rows["Sistema operativo"] = OS_LABEL[config.os_type] || "desconocido";
    }
    rows["Discos EVS"] = config.disks.length ? config.disks.map((d) => `${d.volume_type || "?"} ${d.size_gb} GB`).join(" · ") : "ninguno";
    rows["EIP"] = config.public_ips.length ? config.public_ips.map((i) => i.ip_type || "?").join(" · ") : "ninguna";
    rows["Ancho de banda"] = config.public_ips.length ? config.public_ips.map((i) =>
      `${i.bandwidth_mbps || "?"} Mbps ${i.shared ? "(compartido)" : MODE_LABEL[i.bandwidth_mode] || "(modo ?)"}`).join(" · ") : "—";
    return rows;
  }

  function originPanel(item) {
    const o = item.origin;
    const needsOs = o.kind === "ecs" && !o.os_type;
    const needsMode = o.public_ips.some((i) => !i.bandwidth_mode && !i.shared);
    const rows = describe(o);
    return `<div class="rounded-xl bg-slate-50 dark:bg-slate-800/40 p-4">
      <h4 class="text-xs font-bold uppercase tracking-wide text-slate-500 mb-2">Origen (real, solo lectura)</h4>
      <dl class="grid grid-cols-2 gap-x-3 gap-y-1.5 text-sm">${Object.entries(rows).map(([k, v]) =>
        `<dt class="text-slate-500">${esc(k)}</dt><dd class="font-medium break-words">${esc(v)}</dd>`).join("")}</dl>
      ${needsOs || needsMode ? `<div class="mt-3 rounded-lg border border-amber-200 dark:border-amber-900/60 bg-amber-50 dark:bg-amber-950/20 p-3 text-xs space-y-2">
        <p class="font-semibold text-amber-800 dark:text-amber-300">El inventario no tiene estos datos; indícalos para poder cotizar:</p>
        ${needsOs ? `<label class="flex items-center gap-2">Sistema operativo <select data-assume="os_type" class="input-sm">${opt(["linux", "windows"], item.assumptions.os_type, OS_LABEL, "— elegir —")}</select></label>` : ""}
        ${needsMode ? `<label class="flex items-center gap-2">Cobro del ancho de banda <select data-assume="bandwidth_mode" class="input-sm">${opt(["bandwidth", "traffic"], item.assumptions.bandwidth_mode, MODE_LABEL, "— elegir —")}</select></label>` : ""}
      </div>` : ""}
    </div>`;
  }

  function destinationPanel(item) {
    const d = item.dest, ecs = item.origin.kind === "ecs";
    const regions = state.options ? state.options.regions : [];
    const disks = d.disks.map((x, i) => `<div class="flex items-center gap-2">
        <select data-disk="${i}" data-field="volume_type" class="input-sm">${opt(state.options.disk_types, x.volume_type, null, "tipo ?")}</select>
        <input data-disk="${i}" data-field="size_gb" type="number" min="1" max="65536" value="${esc(x.size_gb)}" class="input-sm w-24" /> GB
        <button data-remove-disk="${i}" class="text-xs text-red-600 hover:underline">quitar</button></div>`).join("");
    const ips = d.public_ips.map((x, i) => `<div class="flex flex-wrap items-center gap-2">
        <input data-ip="${i}" data-field="ip_type" value="${esc(x.ip_type || "")}" placeholder="5_bgp" class="input-sm w-24" />
        <input data-ip="${i}" data-field="bandwidth_mbps" type="number" min="1" max="2000" value="${esc(x.bandwidth_mbps || "")}" class="input-sm w-20" /> Mbps
        <select data-ip="${i}" data-field="bandwidth_mode" class="input-sm">${opt(["bandwidth", "traffic"], x.bandwidth_mode, MODE_LABEL, "modo: según origen")}</select>
        <button data-remove-ip="${i}" class="text-xs text-red-600 hover:underline">quitar</button></div>`).join("");
    return `<div class="rounded-xl border border-blue-200 dark:border-blue-900/60 p-4 space-y-3">
      <div class="flex items-center gap-2"><h4 class="text-xs font-bold uppercase tracking-wide text-blue-700 dark:text-blue-300 mr-auto">Destino (editable, simulación)</h4>
        <button data-reset class="text-xs text-blue-600 hover:underline">Restablecer destino</button></div>
      <label class="field">Región destino
        <select data-dest="region" class="input-sm"><option value="">Usar la global (${esc($("destRegion").value)})</option>${regions.map((r) =>
          `<option value="${esc(r.id)}" ${r.id === d.region ? "selected" : ""}>${esc(r.name)} (${esc(r.id)})</option>`).join("")}</select></label>
      ${ecs ? `<div class="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <label class="field col-span-2">Flavor<input data-dest="flavor" list="flavorList" value="${esc(d.flavor || "")}" class="input-sm" /></label>
        <label class="field">vCPU<input data-dest="vcpus" type="number" min="1" max="512" value="${esc(d.vcpus ?? "")}" class="input-sm" /></label>
        <label class="field">RAM (GB)<input data-dest="ram_gb" type="number" min="0.5" step="0.5" value="${esc(d.ram_gb ?? "")}" class="input-sm" /></label>
        <label class="field col-span-2">Sistema operativo<select data-dest="os_type" class="input-sm">${opt(["linux", "windows"], d.os_type, OS_LABEL, "según origen")}</select></label>
      </div><p class="text-[11px] text-slate-500">El precio lo determina el flavor; vCPU y RAM se ajustan al flavor oficial cuando hay lista de flavors.</p>` : ""}
      <div><div class="text-xs font-semibold text-slate-500 mb-1">Discos EVS (${d.disks.length})</div><div class="space-y-1.5">${disks}</div>
        <button data-add-disk class="mt-1 text-xs text-blue-600 hover:underline">+ disco</button></div>
      <div><div class="text-xs font-semibold text-slate-500 mb-1">EIP y ancho de banda (${d.public_ips.length})</div><div class="space-y-1.5">${ips}</div>
        <button data-add-ip class="mt-1 text-xs text-blue-600 hover:underline">+ EIP</button></div>
    </div>`;
  }

  function delta(value, percent, currency) {
    if (value === null || value === undefined) return '<span class="text-slate-400">—</span>';
    const n = Number(value);
    const tone = n > 0 ? "text-red-600 dark:text-red-400" : n < 0 ? "text-emerald-600 dark:text-emerald-400" : "text-slate-600";
    return `<span class="${tone} font-bold">${n > 0 ? "+" : ""}${esc(value)} ${esc(currency || "")}${percent !== null && percent !== undefined ? ` (${n > 0 ? "+" : ""}${esc(percent)}%)` : ""}</span>`;
  }

  function lineList(side) {
    return side.lines.map((l) => `<tr class="border-t border-slate-100 dark:border-slate-800">
      <td class="py-1.5 pr-3">${esc(l.label)}</td>
      <td class="py-1.5 pr-3 text-right whitespace-nowrap">${l.available ? `${esc(l.monthly_amount)} ${esc(l.currency)}` : '<span class="text-amber-700 dark:text-amber-400">no disponible</span>'}</td>
      <td class="py-1.5 text-xs text-slate-500">${l.available ? `${esc(l.basis)} · ${esc(l.source || "")}${l.updated_at ? ` · ${esc(fmtDate(l.updated_at))}` : ""}` : esc(l.reason)}</td></tr>`).join("");
  }

  function resultPanel(item) {
    const r = item.result;
    if (!r) return `<p class="text-sm text-slate-500">Pulsa <b>Comparar</b> para ver el resultado.</p>`;
    const o = describe(r.origin_config), d = describe(r.destination_config);
    const keys = Array.from(new Set([...Object.keys(o), ...Object.keys(d)]));
    const total = (side) => side.total ? `<b>${esc(side.total)} ${esc(side.currency)}</b>` : '<span class="text-amber-700 dark:text-amber-400 font-semibold">precio no disponible</span>';
    const s = state.summary || {};
    return `<div class="space-y-3">
      <div class="overflow-x-auto"><table class="min-w-full text-sm">
        <thead class="text-[11px] uppercase tracking-wide text-slate-500"><tr><th class="text-left py-1.5">Concepto</th><th class="text-left py-1.5">Origen</th><th class="text-left py-1.5">Destino</th></tr></thead>
        <tbody>
          <tr class="border-t border-slate-100 dark:border-slate-800"><td class="py-1.5 text-slate-500">Servicio</td><td colspan="2" class="py-1.5">${esc(SUPPORTED[r.resource.resource_type] || r.resource.service)}</td></tr>
          ${keys.map((k) => `<tr class="border-t border-slate-100 dark:border-slate-800 ${o[k] !== d[k] ? "bg-blue-50/50 dark:bg-blue-950/10" : ""}">
            <td class="py-1.5 text-slate-500">${esc(k)}</td><td class="py-1.5">${esc(o[k] ?? "—")}</td><td class="py-1.5 font-medium">${esc(d[k] ?? "—")}</td></tr>`).join("")}
          <tr class="border-t-2 border-slate-200 dark:border-slate-700"><td class="py-2 font-semibold">Precio mensual</td><td class="py-2">${total(r.origin)}</td><td class="py-2">${total(r.destination)}</td></tr>
        </tbody></table></div>
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-2">
        <div class="metric"><span>Costo origen</span>${total(r.origin)}</div>
        <div class="metric"><span>Costo destino</span>${total(r.destination)}</div>
        <div class="metric"><span>Diferencia</span>${delta(r.difference, null, r.currency)}</div>
        <div class="metric"><span>Diferencia %</span>${r.difference_percent !== null ? delta(r.difference_percent, null, "%").replace(" %", "%") : '<span class="text-slate-400">—</span>'}</div>
      </div>
      ${r.reason ? `<p class="text-sm text-amber-700 dark:text-amber-400">${esc(r.reason)}</p>` : ""}
      ${r.warnings.length ? `<ul class="text-xs text-amber-700 dark:text-amber-400 list-disc ml-5">${r.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
      <p class="text-xs text-slate-500">Moneda: ${esc(r.currency || r.origin.currency || r.destination.currency || "—")} · Período: ${esc(s.period || "—")} ·
        Modelo de cobro: ${esc(s.billing_label || "—")} · Fuente: ${esc([...new Set([...r.origin.sources, ...r.destination.sources])].join("; ") || "ninguna")} ·
        Precio más antiguo usado: ${esc(fmtDate(r.origin.updated_at && r.destination.updated_at ? [r.origin.updated_at, r.destination.updated_at].sort()[0] : r.origin.updated_at || r.destination.updated_at))}</p>
      <details><summary class="text-xs font-semibold text-blue-600 cursor-pointer">Detalle de precios por componente</summary>
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-4 mt-2 text-sm">
          <div><h5 class="text-xs font-bold text-slate-500 mb-1">Origen</h5><table class="w-full">${lineList(r.origin)}</table></div>
          <div><h5 class="text-xs font-bold text-slate-500 mb-1">Destino</h5><table class="w-full">${lineList(r.destination)}</table></div>
        </div></details>
    </div>`;
  }

  function renderSummary() {
    const s = state.summary;
    if (!state.items.length) { $("summary").innerHTML = ""; return; }
    const stale = state.stale ? `<p class="text-xs text-amber-700 dark:text-amber-400 mt-2">Hay cambios sin comparar: pulsa «Comparar» para actualizar.</p>` : "";
    if (!s) { $("summary").innerHTML = stale; return; }
    $("summary").innerHTML = `<div class="bg-white dark:bg-slate-900 rounded-2xl border border-slate-200 dark:border-slate-800 p-5">
      <div class="flex flex-wrap items-baseline gap-2 mb-3"><h3 class="font-bold mr-auto">Total de la comparación</h3>
        <span class="text-xs text-slate-500">${esc(s.billing_label)} · ${esc(s.period)} · ${s.compared} de ${s.items} recurso(s) con precio completo</span></div>
      ${s.by_currency.length ? s.by_currency.map((g) => `<div class="grid grid-cols-2 lg:grid-cols-4 gap-2">
        <div class="metric"><span>Costo origen</span><b>${esc(g.origin)} ${esc(g.currency)}</b></div>
        <div class="metric"><span>Costo destino</span><b>${esc(g.destination)} ${esc(g.currency)}</b></div>
        <div class="metric"><span>Diferencia</span>${delta(g.difference, null, g.currency)}</div>
        <div class="metric"><span>Diferencia %</span>${g.difference_percent !== null ? delta(g.difference_percent, null, "%").replace(" %", "%") : "—"}</div></div>`).join("")
        : '<p class="text-sm text-amber-700 dark:text-amber-400">Ningún recurso tiene todos sus precios: consulta los precios oficiales o carga una tabla oficial.</p>'}
      ${s.not_compared ? `<p class="text-xs text-slate-500 mt-2">${s.not_compared} recurso(s) no entran en el total porque les falta algún precio.</p>` : ""}${stale}</div>`;
  }

  function render() {
    renderSummary();
    $("items").innerHTML = state.items.map((item, index) => `<article data-index="${index}" class="bg-white dark:bg-slate-900 rounded-2xl border border-slate-200 dark:border-slate-800 p-5 space-y-4">
      <div class="flex flex-wrap items-center gap-2">
        <span class="px-2 py-0.5 rounded-full bg-slate-100 dark:bg-slate-800 text-xs font-semibold">${esc(SUPPORTED[item.resource.resource_type])}</span>
        <h3 class="font-bold mr-auto">${esc(item.resource.name || item.resource.provider_id)}</h3>
        <span class="text-xs text-slate-500 font-mono">${esc(item.origin.region)} → ${esc(item.dest.region || $("destRegion").value)}</span>
        <button data-remove class="text-xs text-slate-500 hover:text-red-600 hover:underline">Quitar</button>
      </div>
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">${originPanel(item)}${destinationPanel(item)}</div>
      <div class="border-t border-slate-200 dark:border-slate-800 pt-4">${resultPanel(item)}</div>
    </article>`).join("") || `<div class="rounded-2xl border-2 border-dashed border-slate-300 dark:border-slate-700 p-10 text-center text-sm text-slate-500">
      Elige un recurso en el paso 1 y pulsa <b>Agregar recurso</b>. Puedes agregar varios para compararlos juntos.</div>`;
    icons();
  }

  /* ---------- edición del destino (delegación de eventos) ---------- */
  function itemOf(element) { const card = element.closest("[data-index]"); return card ? state.items[Number(card.dataset.index)] : null; }
  function touched() { state.stale = true; renderSummary(); }

  $("items").addEventListener("change", (event) => {
    const el = event.target, item = itemOf(el);
    if (!item) return;
    const value = el.value === "" ? null : el.value;
    if (el.dataset.assume) item.assumptions[el.dataset.assume] = value;
    else if (el.dataset.dest) item.dest[el.dataset.dest] = el.dataset.dest === "flavor" && value ? value.trim().toLowerCase() : value;
    else if (el.dataset.disk !== undefined) item.dest.disks[Number(el.dataset.disk)][el.dataset.field] = value;
    else if (el.dataset.ip !== undefined) item.dest.public_ips[Number(el.dataset.ip)][el.dataset.field] = value;
    if (el.dataset.dest === "flavor" && state.options) {   // sugiere vCPU/RAM del flavor oficial
      const flavor = state.options.flavors.find((f) => f.flavor_id === item.dest.flavor);
      if (flavor) { item.dest.vcpus = flavor.vcpus; item.dest.ram_gb = flavor.ram_gb; render(); }
    }
    if (el.dataset.dest === "region") render();
    touched();
  });

  $("items").addEventListener("click", (event) => {
    const el = event.target.closest("button");
    const item = el && itemOf(el);
    if (!item) return;
    if (el.hasAttribute("data-remove")) state.items.splice(state.items.indexOf(item), 1);
    else if (el.hasAttribute("data-reset")) item.dest = clone(item.initialDest);
    else if (el.hasAttribute("data-add-disk")) item.dest.disks.push({ volume_type: (state.options.disk_types || [])[0], size_gb: 40 });
    else if (el.dataset.removeDisk !== undefined) item.dest.disks.splice(Number(el.dataset.removeDisk), 1);
    else if (el.hasAttribute("data-add-ip")) item.dest.public_ips.push({ ip_type: "5_bgp", bandwidth_mbps: 5, bandwidth_mode: "bandwidth", shared: false });
    else if (el.dataset.removeIp !== undefined) item.dest.public_ips.splice(Number(el.dataset.removeIp), 1);
    else return;
    state.stale = true;
    render();
  });

  icons();
  render();
  loadClients().catch((err) => notify(err.message));
})();
