/* Comparador de regiones: pestañas «Configuración libre» y «Catálogo por región».
 * Vanilla JS sobre la API interna (/cost-compare/catalog y /simulate). Solo lectura; los
 * datos vienen de las APIs oficiales de Huawei (ECS ListFlavors, EVS CinderListVolumeTypes,
 * BSS). Sin dato => se dice («Sin catálogo», «Precio no disponible», «Sin Project»), nunca 0.
 * Todo texto que viene de la nube se escapa. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const MAX_REGIONS = 6;
  const ctx = { clientId: "", accountId: "", options: null, catalogs: {}, regions: [], disks: [], lastSim: null };

  /* ---------- utilidades ---------- */
  function esc(value) {
    return String(value === undefined || value === null ? "" : value)
      .replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function icons() { if (window.lucide) window.lucide.createIcons(); }
  function fmtDate(value) { return value ? new Date(value).toLocaleString() : null; }
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
  const NA = '<span class="text-amber-700 dark:text-amber-400 font-semibold">Precio no disponible</span>';
  const base = () => `/api/clients/${ctx.clientId}/accounts/${ctx.accountId}/cost-compare`;

  async function call(path, options) {
    const response = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, options || {}));
    let body = null;
    try { body = await response.json(); } catch (e) {}
    if (!response.ok) {
      const d = body && body.detail;
      const message = typeof d === "string" ? d : (d && (d.mensaje || (Array.isArray(d) && d.map((x) => x.msg).join("; ")))) || `HTTP ${response.status}`;
      const error = new Error(message); error.status = response.status; error.detail = d; throw error;
    }
    return body;
  }
  const regionInfo = (id) => (ctx.options ? ctx.options.regions.find((r) => r.id === id) : null) || { id, name: id };
  const regionLabel = (id) => `${regionInfo(id).name || id}`;

  function banner(target, message, tone) {
    const tones = {
      error: "border-red-200 bg-red-50 text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300",
      info: "border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300",
      ok: "border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/30 dark:text-emerald-300",
    };
    target.innerHTML = message ? `<div class="${tones[tone || "info"]} rounded-xl border px-4 py-3 text-sm">${message}</div>` : "";
  }
  function errorHtml(err) {
    const d = err && err.detail && typeof err.detail === "object" ? err.detail : {};
    const meta = [d.http_status ? `HTTP ${d.http_status}` : "", d.error_code ? `Código ${d.error_code}` : "",
      d.request_id ? `Request ID ${d.request_id}` : "", d.accion_iam ? `Acción IAM: ${d.accion_iam}` : ""].filter(Boolean).join(" · ");
    return `${esc(err.message || err.mensaje || "Error")}${meta ? `<div class="mt-1 text-xs opacity-80">${esc(meta)}</div>` : ""}`;
  }

  /* ---------- pestañas ---------- */
  const TABS = ["recurso", "libre", "catalogo"];
  function setTab(tab, push) {
    if (!TABS.includes(tab)) tab = "recurso";
    document.querySelectorAll("[data-cc-tab]").forEach((b) => {
      const on = b.dataset.ccTab === tab;
      b.classList.toggle("tab-active", on); b.setAttribute("aria-selected", on ? "true" : "false");
    });
    document.querySelectorAll("[data-cc-pane]").forEach((p) => p.classList.toggle("hidden", p.dataset.ccPane !== tab));
    if (push) history.replaceState(null, "", tab === "recurso" ? location.pathname + location.search : `#${tab}`);
    if (tab === "catalogo" && ctx.accountId) loadCatalog($("catRegion").value);
    if (tab === "libre" && ctx.accountId) refreshFlavorHints();
  }
  document.querySelectorAll("[data-cc-tab]").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.ccTab, true)));
  window.addEventListener("hashchange", () => setTab(location.hash.slice(1), false));

  /* ---------- cuenta seleccionada (evento de costs_compare.js) ---------- */
  document.addEventListener("cc:account", (event) => {
    Object.assign(ctx, event.detail, { catalogs: {}, lastSim: null });
    const regions = ctx.options.regions;
    $("catRegion").innerHTML = regions.map((r) =>
      `<option value="${esc(r.id)}">${esc(r.name || r.id)} · ${esc(r.id)}${r.has_project ? "" : " (sin Project)"}</option>`).join("");
    const firstWithProject = regions.find((r) => r.has_project);
    if (firstWithProject) $("catRegion").value = firstWithProject.id;
    $("freeSysType").innerHTML = diskOptions("GPSSD");
    if (!ctx.regions.length || ctx.regions.some((r) => !regions.find((x) => x.id === r))) {
      ctx.regions = regions.filter((r) => r.has_project).slice(0, 3).map((r) => r.id);
    }
    renderRegionChips();
    $("freeResult").innerHTML = "";
    setTab(location.hash.slice(1), false);
  });

  /* ---------- catálogo por región ---------- */
  async function loadCatalog(region, force) {
    if (!region) return;
    if (!force && ctx.catalogs[region]) { renderCatalog(region); return; }
    $("catFlavors").innerHTML = `<tr><td colspan="9" class="py-6 text-center text-slate-500">Cargando…</td></tr>`;
    try {
      ctx.catalogs[region] = await call(`${base()}/catalog?region=${encodeURIComponent(region)}`);
      renderCatalog(region);
    } catch (err) {
      banner($("catNotice"), errorHtml(err), "error");
    }
  }

  function statusBadge(f) {
    const tone = f.available ? (f.status === "obt" ? "badge-amber" : "badge-green") : "badge-red";
    return `<span class="badge ${tone}">${esc(f.estado)}</span>`;
  }
  function zonesText(f) {
    const zones = Object.entries(f.az_status || {});
    if (!zones.length) return '<span class="text-slate-400">Toda la región</span>';
    return zones.map(([z, s]) => `<span class="${s === "sellout" ? "line-through text-slate-400" : ""}" title="${esc(s)}">${esc(z)}</span>`).join(", ");
  }

  function renderCatalog(region) {
    const data = ctx.catalogs[region];
    if (!data) return;
    const updated = fmtDate(data.flavors_updated_at);
    $("catMeta").textContent = updated ? `Consultado a Huawei: ${updated}` : "Aún no consultado a Huawei";
    if (!data.has_project) {
      banner($("catNotice"), "Sin Project en la cuenta para esta región: Huawei no permite consultar su catálogo ni sus precios con esta cuenta.", "info");
    } else if (!data.flavors.length && !data.volume_types.length) {
      banner($("catNotice"), "Sin catálogo consultado para esta región. Pulsa «Actualizar desde Huawei».", "info");
    } else if ($("catNotice").dataset.keep !== "1") {
      banner($("catNotice"), "");
    }
    $("catNotice").dataset.keep = "";
    $("catRefresh").disabled = !data.has_project;

    const families = [...new Set(data.flavors.map((f) => f.familia))].sort();
    const keepFamily = $("catFamily").value;
    $("catFamily").innerHTML = `<option value="">Todas</option>` + families.map((f) => `<option>${esc(f)}</option>`).join("");
    if (families.includes(keepFamily)) $("catFamily").value = keepFamily;
    const cpus = [...new Set(data.flavors.map((f) => f.vcpus))].sort((a, b) => a - b);
    const keepCpu = $("catVcpu").value;
    $("catVcpu").innerHTML = `<option value="">Todos</option>` + cpus.map((c) => `<option value="${c}">${c}</option>`).join("");
    if (cpus.map(String).includes(keepCpu)) $("catVcpu").value = keepCpu;
    renderFlavorRows();

    $("catDiskCount").textContent = data.volume_types.length || "";
    $("catDisks").innerHTML = data.volume_types.map((v) => `<tr>
        <td class="font-medium">${esc(v.tipo)}</td><td class="font-mono text-xs">${esc(v.name)}</td>
        <td>${v.available_zones.length ? esc(v.available_zones.join(", ")) : (v.availability_zones.length ? '<span class="badge badge-red">Agotado</span>' : '<span class="text-slate-400">Toda la región</span>')}</td>
        <td>${v.sold_out_zones.length ? esc(v.sold_out_zones.join(", ")) : "—"}</td></tr>`).join("")
      || `<tr><td colspan="4" class="py-6 text-center text-slate-500">${data.volume_types_updated_at ? "Sin tipos de disco" : "Sin catálogo de discos consultado"}</td></tr>`;
  }

  function renderFlavorRows() {
    const data = ctx.catalogs[$("catRegion").value];
    if (!data) return;
    const q = $("catSearch").value.trim().toLowerCase();
    const fam = $("catFamily").value, cpu = $("catVcpu").value, st = $("catStatus").value;
    const rows = data.flavors.filter((f) =>
      (!q || [f.flavor_id, f.cpu_name, f.gpu_name, f.familia].some((x) => x && String(x).toLowerCase().includes(q)))
      && (!fam || f.familia === fam) && (!cpu || String(f.vcpus) === cpu)
      && (!st || (st === "disponible" ? f.available && f.status !== "obt" : !f.available || f.status === "obt")));
    $("catFlavorCount").textContent = data.flavors.length ? `${rows.length}/${data.flavors.length}` : "";
    const shown = rows.slice(0, 400);
    $("catFlavors").innerHTML = shown.map((f) => `<tr>
        <td><span class="font-mono font-medium">${esc(f.flavor_id)}</span>${f.generation ? `<div class="text-xs text-slate-400">Generación ${esc(f.generation)}</div>` : ""}</td>
        <td class="text-right tabular-nums">${esc(f.vcpus)}</td><td class="text-right tabular-nums whitespace-nowrap">${esc(f.ram_gb)} GB</td>
        <td>${esc(f.familia)}${f.architecture === "arm64" ? ' <span class="badge badge-slate">Arm</span>' : ""}</td>
        <td class="hidden lg:table-cell text-xs">${esc([f.cpu_name, f.gpu_name].filter(Boolean).join(" · ") || "—")}</td>
        <td class="hidden md:table-cell text-right text-xs whitespace-nowrap">${f.max_bandwidth_gbps ? `${esc(f.max_bandwidth_gbps)} Gbit/s` : "—"}</td>
        <td>${statusBadge(f)}</td><td class="hidden xl:table-cell text-xs">${zonesText(f)}</td>
        <td class="text-right"><button type="button" class="btn-ghost btn-sm" data-use-flavor="${esc(f.flavor_id)}" title="Usar en «Configuración libre»">Usar</button></td></tr>`).join("")
      || `<tr><td colspan="9" class="py-6 text-center text-slate-500">${data.flavors.length ? "Ningún flavor coincide con los filtros." : (data.flavors_updated_at ? "Sin flavors" : "Sin catálogo consultado")}</td></tr>`;
    if (rows.length > shown.length) $("catFlavors").insertAdjacentHTML("beforeend", `<tr><td colspan="9" class="py-3 text-center text-xs text-slate-500">Se muestran ${shown.length} de ${rows.length}; usa los filtros para acotar.</td></tr>`);
  }

  $("catRegion").addEventListener("change", () => { banner($("catNotice"), ""); loadCatalog($("catRegion").value); });
  ["catSearch", "catFamily", "catVcpu", "catStatus"].forEach((id) => $(id).addEventListener(id === "catSearch" ? "input" : "change", renderFlavorRows));
  $("catRefresh").addEventListener("click", async () => {
    const region = $("catRegion").value;
    $("catRefresh").disabled = true;
    banner($("catNotice"), "Consultando a Huawei Cloud…", "info");
    try {
      const r = await call(`${base()}/catalog/refresh?region=${encodeURIComponent(region)}`, { method: "POST" });
      const parts = [r.flavors !== null ? `${r.flavors} flavors` : null, r.volume_types !== null ? `${r.volume_types} tipos de disco` : null].filter(Boolean);
      const errors = (r.errors || []).map((e) => `<li><strong>${esc(e.servicio.toUpperCase())}:</strong> ${errorHtml(e)}</li>`).join("");
      banner($("catNotice"), `Actualizado: ${esc(parts.join(" y "))}.${errors ? `<ul class="mt-2 list-disc pl-5">${errors}</ul>` : ""}`, errors ? "info" : "ok");
      $("catNotice").dataset.keep = "1";
      await loadCatalog(region, true);
    } catch (err) {
      banner($("catNotice"), err.status === 403 ? "Tu rol no permite consultar a Huawei (requiere operador)." : errorHtml(err), "error");
      $("catRefresh").disabled = false;
    }
  });
  $("catFlavors").addEventListener("click", (e) => {
    const b = e.target.closest("[data-use-flavor]");
    if (!b) return;
    $("freeFlavor").value = b.dataset.useFlavor;
    const region = $("catRegion").value;
    if (!ctx.regions.includes(region) && ctx.regions.length < MAX_REGIONS) ctx.regions.unshift(region);
    renderRegionChips();
    setTab("libre", true);
    refreshFlavorHints();
  });

  /* ---------- configuración libre ---------- */
  function diskOptions(selected) {
    const types = (ctx.options && ctx.options.disk_types) || ["SATA", "SAS", "GPSSD", "SSD", "ESSD", "GPSSD2", "ESSD2"];
    const names = { SATA: "Común (SATA)", SAS: "Alto I/O (SAS)", GPSSD: "SSD de uso general", SSD: "Ultra alto I/O (SSD)",
      ESSD: "SSD extremo", GPSSD2: "SSD de uso general V2", ESSD2: "SSD extremo V2" };
    return types.map((t) => `<option value="${esc(t)}"${t === selected ? " selected" : ""}>${esc(names[t] || t)}</option>`).join("");
  }

  function renderRegionChips() {
    if (!ctx.options) return;
    $("freeRegions").innerHTML = ctx.options.regions.map((r) => {
      const on = ctx.regions.includes(r.id);
      const order = on ? ctx.regions.indexOf(r.id) + 1 : "";
      return `<button type="button" data-region-chip="${esc(r.id)}" aria-pressed="${on}" class="inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm transition-colors ${on
        ? "border-blue-600 bg-blue-50 text-blue-800 dark:bg-blue-950/40 dark:text-blue-200 dark:border-blue-500"
        : "border-slate-200 text-slate-600 hover:border-slate-300 dark:border-slate-700 dark:text-slate-300"}">
        ${on ? `<span class="text-[11px] font-bold tabular-nums">${order}</span>` : ""}${esc(r.name || r.id)}
        ${r.has_project ? "" : '<span class="text-[11px] text-slate-400">sin Project</span>'}</button>`;
    }).join("");
  }
  $("freeRegions").addEventListener("click", (e) => {
    const chip = e.target.closest("[data-region-chip]");
    if (!chip) return;
    const id = chip.dataset.regionChip;
    if (ctx.regions.includes(id)) ctx.regions = ctx.regions.filter((r) => r !== id);
    else if (ctx.regions.length < MAX_REGIONS) ctx.regions.push(id);
    else { banner($("freeResult"), `Máximo ${MAX_REGIONS} regiones por comparación.`, "info"); return; }
    renderRegionChips();
    refreshFlavorHints();
  });

  function renderDisks() {
    $("freeDisks").innerHTML = ctx.disks.map((d, i) => `<div class="flex flex-wrap items-center gap-2" data-disk="${i}">
        <span class="text-xs text-slate-500 w-20">Datos ${i + 1}</span>
        <select class="input w-auto flex-1 min-w-[10rem] max-w-sm" data-disk-type>${diskOptions(d.volume_type)}</select>
        <input type="number" min="10" max="32768" value="${esc(d.size_gb)}" class="input w-28" data-disk-size aria-label="Tamaño (GB)" />
        <span class="text-xs text-slate-500">GB</span>
        <button type="button" class="icon-btn" data-disk-remove title="Quitar disco"><i data-lucide="x" class="w-4 h-4"></i></button></div>`).join("")
      || '<p class="text-xs text-slate-400">Sin discos de datos.</p>';
    icons();
  }
  $("freeAddDisk").addEventListener("click", () => { if (ctx.disks.length < 8) { ctx.disks.push({ volume_type: "GPSSD", size_gb: 100 }); renderDisks(); } });
  $("freeDisks").addEventListener("input", (e) => {
    const row = e.target.closest("[data-disk]"); if (!row) return;
    const d = ctx.disks[Number(row.dataset.disk)];
    if (e.target.matches("[data-disk-type]")) d.volume_type = e.target.value;
    if (e.target.matches("[data-disk-size]")) d.size_gb = Number(e.target.value);
  });
  $("freeDisks").addEventListener("click", (e) => {
    if (!e.target.closest("[data-disk-remove]")) return;
    ctx.disks.splice(Number(e.target.closest("[data-disk]").dataset.disk), 1); renderDisks();
  });
  $("freeEip").addEventListener("change", () => ["freeEipType", "freeEipMbps", "freeEipMode"].forEach((id) => { $(id).disabled = !$("freeEip").checked; }));
  function syncHours() { $("freeHoursField").classList.toggle("hidden", $("freeBilling").value !== "on_demand"); }
  $("freeBilling").addEventListener("change", syncHours);
  syncHours();

  /** Sugerencias de flavor: catálogo de la región de referencia (la primera elegida). */
  async function refreshFlavorHints() {
    const region = ctx.regions[0];
    if (!region) { $("freeFlavorList").innerHTML = ""; $("freeFlavorInfo").textContent = "Elige al menos una región."; return; }
    if (!ctx.catalogs[region]) {
      try { ctx.catalogs[region] = await call(`${base()}/catalog?region=${encodeURIComponent(region)}`); } catch (e) { return; }
    }
    const flavors = ctx.catalogs[region].flavors;
    $("freeFlavorList").innerHTML = flavors.filter((f) => f.available).slice(0, 600)
      .map((f) => `<option value="${esc(f.flavor_id)}">${esc(f.descripcion)}</option>`).join("");
    describeFlavor();
  }
  function describeFlavor() {
    const region = ctx.regions[0];
    const data = region && ctx.catalogs[region];
    const id = $("freeFlavor").value.trim().toLowerCase();
    if (!data || !data.flavors.length) { $("freeFlavorInfo").textContent = region ? `Sin catálogo consultado en ${regionLabel(region)}.` : ""; return; }
    const f = data.flavors.find((x) => x.flavor_id === id);
    $("freeFlavorInfo").textContent = !id ? `${data.flavors.length} flavors en ${regionLabel(region)}.`
      : f ? `${f.descripcion} · ${f.estado} en ${regionLabel(region)}` : `No aparece en ${regionLabel(region)}.`;
  }
  $("freeFlavor").addEventListener("input", describeFlavor);

  function payload() {
    const config = {
      flavor: $("freeFlavor").value.trim().toLowerCase(), os_type: $("freeOs").value,
      system_disk: { volume_type: $("freeSysType").value, size_gb: Number($("freeSysSize").value) },
      data_disks: ctx.disks.map((d) => ({ volume_type: d.volume_type, size_gb: Number(d.size_gb) })),
      eip: $("freeEip").checked ? { ip_type: $("freeEipType").value.trim(), bandwidth_mbps: Number($("freeEipMbps").value), bandwidth_mode: $("freeEipMode").value } : null,
    };
    return { regions: ctx.regions.slice(), billing_mode: $("freeBilling").value, hours_per_month: Number($("freeHours").value) || 730, config };
  }

  async function simulate() {
    if (!ctx.accountId) return;
    if (!$("freeFlavor").value.trim()) { banner($("freeResult"), "Indica un flavor (puedes elegirlo en «Catálogo por región»).", "info"); return; }
    if (!ctx.regions.length) { banner($("freeResult"), "Elige al menos una región.", "info"); return; }
    $("freeCompare").disabled = true;
    try {
      ctx.lastSim = await call(`${base()}/simulate`, { method: "POST", body: JSON.stringify(payload()) });
      renderSimulation(ctx.lastSim);
    } catch (err) { banner($("freeResult"), errorHtml(err), "error"); }
    finally { $("freeCompare").disabled = false; }
  }
  $("freeCompare").addEventListener("click", simulate);
  $("freeRefreshPrices").addEventListener("click", async () => {
    if (!$("freeFlavor").value.trim() || !ctx.regions.length) { banner($("freeResult"), "Indica un flavor y al menos una región.", "info"); return; }
    $("freeRefreshPrices").disabled = true;
    banner($("freeResult"), "Consultando precios oficiales a Huawei Cloud…", "info");
    try {
      const r = await call(`${base()}/simulate/prices/refresh`, { method: "POST", body: JSON.stringify(payload()) });
      await simulate();
      const fails = (r.failures || []).map((f) => `<li>${esc(regionLabel(f.region))} · ${esc(f.component)}: ${esc(f.reason)}</li>`).join("");
      $("freeResult").insertAdjacentHTML("afterbegin", `<div class="${fails ? "border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300" : "border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/30 dark:text-emerald-300"} rounded-xl border px-4 py-3 text-sm">
        ${esc(r.stored)} precio(s) oficial(es) guardado(s).${fails ? `<ul class="mt-2 list-disc pl-5">${fails}</ul>` : ""}</div>`);
    } catch (err) {
      banner($("freeResult"), err.status === 403 ? "Tu rol no permite consultar a Huawei (requiere operador)." : errorHtml(err), "error");
    } finally { $("freeRefreshPrices").disabled = false; }
  });

  const CHECK = {
    disponible: ["check-circle-2", "text-emerald-600 dark:text-emerald-400"], agotado: ["alert-triangle", "text-amber-600 dark:text-amber-400"],
    beta: ["flask-conical", "text-amber-600 dark:text-amber-400"], no_existe: ["x-circle", "text-red-600 dark:text-red-400"],
    sin_catalogo: ["help-circle", "text-slate-400"],
  };
  const checkIcon = (state) => { const [icon, cls] = CHECK[state] || CHECK.sin_catalogo; return `<i data-lucide="${icon}" class="w-4 h-4 shrink-0 ${cls}"></i>`; };

  function renderSimulation(sim) {
    const s = sim.summary;
    const diffs = Object.fromEntries((s.differences || []).map((d) => [d.region, d]));
    const head = `<div class="card p-4 md:p-5 flex flex-wrap items-center gap-x-6 gap-y-2">
        <div><p class="kpi-label">Más barata</p><p class="text-xl font-bold">${s.cheapest_region ? `${esc(regionLabel(s.cheapest_region))} · ${esc(money(s.cheapest_total, s.currency))}` : "Sin regiones con precio completo"}</p></div>
        <div class="text-sm text-slate-500">${esc(s.billing_label)} · ${esc(s.period)}<br/>Referencia: ${esc(regionLabel(s.reference_region))}</div>
        ${s.incomplete_regions.length ? `<p class="text-xs text-amber-700 dark:text-amber-400 basis-full">Sin precio completo en: ${esc(s.incomplete_regions.map(regionLabel).join(", "))}. Pulsa «Consultar precios oficiales».</p>` : ""}
      </div>`;
    const cards = sim.regions.map((r) => {
      const cost = r.cost, f = r.flavor_check;
      const cheapest = s.cheapest_region === r.region && sim.regions.length > 1;
      const d = diffs[r.region];
      const total = cost.total !== null ? `<p class="text-2xl font-bold tabular-nums">${esc(money(cost.total, cost.currency))}<span class="text-sm font-normal text-slate-500"> /mes</span></p>`
        : `<p class="text-lg">${NA}</p><p class="text-xs text-slate-500">${esc(cost.reason || "")}</p>`;
      const delta = d && r.region !== s.reference_region && d.difference !== null
        ? `<p class="text-sm tabular-nums ${Number(d.difference) > 0 ? "text-red-600 dark:text-red-400" : "text-emerald-600 dark:text-emerald-400"}">${esc(signedMoney(d.difference, s.currency))}${d.difference_percent !== null ? ` (${Number(d.difference_percent) > 0 ? "+" : ""}${esc(d.difference_percent)} %)` : ""} vs. referencia</p>` : "";
      const equiv = (f.equivalents || []).length ? `<div class="mt-1 flex flex-wrap gap-1">${f.equivalents.map((e) =>
          `<button type="button" class="badge badge-blue" data-equivalent="${esc(e.flavor_id)}" title="${esc(e.descripcion)} · ${esc(e.estado)}">${esc(e.flavor_id)}${e.status === "obt" ? " · beta" : ""}</button>`).join("")}</div>` : "";
      const disks = r.disk_checks.map((dk) => `<li class="flex items-start gap-2">${checkIcon(dk.state)}<span>${esc(dk.name)}: ${esc(dk.tipo)} ${esc(dk.size_gb)} GB <span class="text-slate-500">· ${esc(dk.label)}</span></span></li>`).join("");
      const lines = cost.lines.map((l) => `<tr><td class="py-1 pr-2">${esc(l.label)}</td><td class="py-1 text-right tabular-nums whitespace-nowrap">${l.available ? esc(money(l.monthly_amount, l.currency)) : NA}</td></tr>`).join("");
      return `<article class="card p-4 md:p-5 flex flex-col gap-3 ${cheapest ? "ring-2 ring-emerald-500/60" : ""}">
          <header class="flex items-start justify-between gap-2">
            <div class="min-w-0"><h3 class="font-semibold">${esc(regionLabel(r.region))}</h3><p class="text-xs text-slate-500 font-mono">${esc(r.region)}</p></div>
            <div class="flex flex-wrap gap-1 justify-end">${cheapest ? '<span class="badge badge-green">Más barata</span>' : ""}${r.region === s.reference_region ? '<span class="badge badge-slate">Referencia</span>' : ""}${r.has_project ? "" : '<span class="badge badge-amber">Sin Project</span>'}</div>
          </header>
          <div>${total}${delta}</div>
          <div class="text-sm"><div class="flex items-start gap-2">${checkIcon(f.state)}<span>Flavor ${esc(sim.config.flavor)}: <strong>${esc(f.label)}</strong>${f.flavor ? ` <span class="text-slate-500">· ${esc(f.flavor.descripcion)}</span>` : ""}</span></div>
            ${f.equivalents && f.equivalents.length ? `<p class="mt-1 text-xs text-slate-500">Equivalentes disponibles (mismos vCPU y RAM):</p>${equiv}` : ""}</div>
          <ul class="text-sm space-y-1">${disks}</ul>
          <details class="text-sm"><summary class="cursor-pointer text-slate-500">Desglose del precio</summary><table class="w-full mt-2">${lines}</table>
            ${cost.updated_at ? `<p class="mt-1 text-xs text-slate-400">Precio oficial más antiguo usado: ${esc(fmtDate(cost.updated_at))}</p>` : ""}</details>
          ${r.warnings.length ? `<ul class="text-xs text-amber-700 dark:text-amber-400 list-disc pl-4 space-y-0.5">${r.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
        </article>`;
    }).join("");
    $("freeResult").innerHTML = head + `<div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">${cards}</div>`;
    icons();
  }
  $("freeResult").addEventListener("click", (e) => {
    const b = e.target.closest("[data-equivalent]");
    if (!b) return;
    $("freeFlavor").value = b.dataset.equivalent;
    describeFlavor();
    simulate();
  });

  renderDisks();
})();
