/* Calculadora de precios: configura ECS / EVS / EIP, ve su precio oficial de lista y arma una
 * lista con total. Vanilla JS sobre la API interna (/calculator). Simulación: nunca crea recursos.
 * Sin precio oficial guardado => "Precio no disponible" (nunca 0). Todo texto de la nube se escapa.
 * La lista se guarda en este navegador (solo configuraciones; nada sensible). */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const LIST_KEY = "hc_calculator_list_v1";
  const st = { clientId: "", accountId: "", options: null, region: "", product: "ecs", flavor: "", disks: [],
    seq: 0, timer: null, editing: null, list: loadList() };

  /* ---------- utilidades ---------- */
  function esc(value) {
    return String(value === undefined || value === null ? "" : value)
      .replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function icons() { if (window.lucide) window.lucide.createIcons(); }
  function money(amount, currency) {
    if (amount === null || amount === undefined) return null;
    const n = Number(amount);
    try { return new Intl.NumberFormat(undefined, { style: "currency", currency, currencyDisplay: "narrowSymbol" }).format(n); }
    catch (e) { return `${n.toFixed(2)} ${currency || ""}`.trim(); }
  }
  const NA = '<span class="text-amber-700 dark:text-amber-400 font-semibold">Precio no disponible</span>';
  function notify(message, tone) {
    const tones = {
      error: "border-red-200 bg-red-50 text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300",
      info: "border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300",
      ok: "border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/30 dark:text-emerald-300",
    };
    const div = document.createElement("div");
    div.className = `${tones[tone || "error"]} rounded-xl border px-4 py-3 text-sm flex items-start gap-3`;
    div.innerHTML = `<div class="flex-1">${message}</div><button type="button" class="opacity-60 hover:opacity-100" aria-label="Cerrar">✕</button>`;
    div.querySelector("button").addEventListener("click", () => div.remove());
    $("alerts").appendChild(div);
  }
  async function call(path, options) {
    const response = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, options || {}));
    if (!response.ok) {
      let message = `HTTP ${response.status}`, detail = null;
      try {
        const body = await response.json(); detail = body.detail;
        message = typeof detail === "string" ? detail : (detail && (detail.mensaje || (Array.isArray(detail) && detail.map((d) => d.msg).join("; ")))) || message;
      } catch (e) {}
      const error = new Error(message); error.status = response.status; error.detail = detail; throw error;
    }
    return response;
  }
  const json = async (path, options) => (await call(path, options)).json();
  const base = () => `/api/clients/${st.clientId}/accounts/${st.accountId}`;
  function errorText(err) {
    const d = err.detail && typeof err.detail === "object" ? err.detail : {};
    const meta = [d.http_status ? `HTTP ${d.http_status}` : "", d.error_code ? `Código ${d.error_code}` : "",
      d.request_id ? `Request ID ${d.request_id}` : "", d.accion_iam ? `Acción IAM ${d.accion_iam}` : ""].filter(Boolean).join(" · ");
    return `${esc(err.message)}${meta ? `<div class="text-xs mt-1">${esc(meta)}</div>` : ""}`;
  }
  const calc = () => `${base()}/calculator`;
  const regionName = (id) => { const r = st.options && st.options.regions.find((x) => x.id === id); return r ? (r.name || r.id) : id; };
  const modeInfo = (id) => st.options.modes.find((m) => m.id === id);

  function loadList() { try { return JSON.parse(localStorage.getItem(LIST_KEY) || "[]").slice(0, 50); } catch (e) { return []; } }
  function saveList() { try { localStorage.setItem(LIST_KEY, JSON.stringify(st.list)); } catch (e) {} }

  /* ---------- tema ---------- */
  function applyTheme(dark) { document.documentElement.classList.toggle("dark", dark); try { localStorage.setItem("hc_theme", dark ? "dark" : "light"); } catch (e) {} }
  try { applyTheme(localStorage.getItem("hc_theme") === "dark"); } catch (e) {}
  $("btnDark").addEventListener("click", () => applyTheme(!document.documentElement.classList.contains("dark")));

  /* ---------- cliente / cuenta ---------- */
  async function loadClients() {
    let clients;
    try { clients = await json("/api/admin/clients"); }
    catch (err) {
      notify(err.status === 404 || err.status === 401 ? "La API interna no está activa. Arranca la aplicación con INVENTORY_ADMIN_API=true." : `No se pudieron cargar los clientes: ${esc(err.message)}`, "info");
      return;
    }
    $("clientSelect").innerHTML = clients.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("");
    if (!clients.length) { notify("No hay clientes.", "info"); return; }
    st.clientId = clients[0].id;
    await loadAccounts();
  }
  async function loadAccounts() {
    const overview = await json(`/api/clients/${st.clientId}/overview`);
    $("accountSelect").innerHTML = overview.accounts.map((a) => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join("");
    st.accountId = overview.accounts.length ? overview.accounts[0].id : "";
    if (!st.accountId) { notify("Este cliente no tiene cuentas de Huawei Cloud.", "info"); return; }
    await loadOptions(true);
    renderList();
  }
  $("clientSelect").addEventListener("change", () => { st.clientId = $("clientSelect").value; loadAccounts().catch((e) => notify(esc(e.message))); });
  $("accountSelect").addEventListener("change", () => { st.accountId = $("accountSelect").value; loadOptions(true).then(renderList).catch((e) => notify(esc(e.message))); });

  /* ---------- opciones de la región ---------- */
  async function loadOptions(first) {
    const mode = $("billingMode").value || "monthly";
    const params = new URLSearchParams({ billing_mode: mode, os_type: $("ecsOs").value });
    if (!first || st.region) params.set("region", st.region || "");
    if (first && !st.region) {
      const opts = await json(`${calc()}/options`);
      const withProject = opts.regions.find((r) => r.has_project);
      st.region = (withProject || opts.regions[0] || {}).id || "";
      params.set("region", st.region);
    }
    st.options = await json(`${calc()}/options?${params}`);
    if (first) {
      $("region").innerHTML = st.options.regions.map((r) => `<option value="${esc(r.id)}">${esc(r.name || r.id)} · ${esc(r.id)}${r.has_project ? "" : " (sin Project)"}</option>`).join("");
      $("billingMode").innerHTML = st.options.modes.map((m) => `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join("");
      $("billingMode").value = mode;
      fillDuration();
      fillDiskSelects();
      renderEipFields();
    }
    $("region").value = st.region;
    $("regionNote").innerHTML = st.options.has_project ? ""
      : '<span class="text-amber-700 dark:text-amber-400">Sin Project en la cuenta para esta región: Huawei no permite cotizarla con esta cuenta.</span>';
    renderFilters();
    renderFlavors();
    markDisks();
    scheduleQuote();
  }
  function fillDuration() {
    const m = modeInfo($("billingMode").value);
    $("durationLabel").textContent = m.id === "on_demand" ? "Horas de uso" : `Duración (${m.units})`;
    const values = m.id === "on_demand" ? [1, 24, 168, 730, 2190, 4380, 8760] : Array.from({ length: m.max - m.min + 1 }, (_, i) => m.min + i);
    $("duration").innerHTML = values.map((v) => `<option value="${v}">${v} ${v === 1 ? esc(m.unit) : esc(m.units)}${m.id === "on_demand" && v === 730 ? " (1 mes)" : ""}</option>`).join("");
    $("duration").value = m.id === "on_demand" ? "730" : "1";
  }
  function diskOptions(selected) {
    return st.options.disk_types.map((d) => `<option value="${esc(d.id)}"${d.id === selected ? " selected" : ""}>${esc(d.name)}</option>`).join("");
  }
  function fillDiskSelects() { $("sysType").innerHTML = diskOptions("GPSSD"); $("evsType").innerHTML = diskOptions("GPSSD"); renderDisks(); }
  function markDisks() {
    const avail = st.options.disk_availability || {};
    document.querySelectorAll("#sysType option, #evsType option, [data-disk-type] option").forEach((o) => {
      const d = st.options.disk_types.find((x) => x.id === o.value);
      const state = avail[o.value];
      o.textContent = `${d ? d.name : o.value}${state === "no_existe" ? " — no existe en la región" : state === "agotado" ? " — agotado" : ""}`;
    });
  }

  $("region").addEventListener("change", () => { st.region = $("region").value; st.flavor = ""; loadOptions(false).catch((e) => notify(esc(e.message))); });
  $("billingMode").addEventListener("change", () => { fillDuration(); loadOptions(false).catch((e) => notify(esc(e.message))); });
  $("ecsOs").addEventListener("change", () => loadOptions(false).catch((e) => notify(esc(e.message))));
  ["duration", "quantity", "sysType", "sysSize", "evsType", "evsSize"].forEach((id) => $(id).addEventListener("input", scheduleQuote));

  /* ---------- productos ---------- */
  document.querySelectorAll("[data-product]").forEach((b) => b.addEventListener("click", () => setProduct(b.dataset.product)));
  function setProduct(p) {
    st.product = p;
    // OBS solo tiene precio de pago por uso en la API de Huawei Cloud.
    if (p === "obs" && $("billingMode").value !== "on_demand") {
      $("billingMode").value = "on_demand"; fillDuration();
      loadOptions(false).catch((e) => notify(esc(e.message)));
    }
    $("billingMode").disabled = p === "obs";
    document.querySelectorAll("[data-product]").forEach((b) => { const on = b.dataset.product === p; b.classList.toggle("tab-active", on); b.setAttribute("aria-selected", on); });
    document.querySelectorAll("[data-panel]").forEach((el) => el.classList.toggle("hidden", el.dataset.panel !== p));
    scheduleQuote();
  }

  /* ---------- flavors ECS ---------- */
  function renderFilters() {
    const fl = st.options.flavors || [];
    const keep = { v: $("ecsVcpu").value, r: $("ecsRam").value, f: $("ecsFamily").value };
    const uniq = (arr) => [...new Set(arr)].sort((a, b) => a - b);
    $("ecsVcpu").innerHTML = '<option value="">Todos</option>' + uniq(fl.map((f) => f.vcpus)).map((v) => `<option value="${v}">${v} vCPU</option>`).join("");
    $("ecsRam").innerHTML = '<option value="">Todas</option>' + uniq(fl.map((f) => f.ram_gb)).map((v) => `<option value="${v}">${v} GB</option>`).join("");
    $("ecsFamily").innerHTML = '<option value="">Todas</option>' + [...new Set(fl.map((f) => f.familia))].sort().map((v) => `<option>${esc(v)}</option>`).join("");
    if ([...$("ecsVcpu").options].some((o) => o.value === keep.v)) $("ecsVcpu").value = keep.v;
    if ([...$("ecsRam").options].some((o) => o.value === keep.r)) $("ecsRam").value = keep.r;
    if ([...$("ecsFamily").options].some((o) => o.value === keep.f)) $("ecsFamily").value = keep.f;
  }
  function visibleFlavors() {
    const q = $("ecsSearch").value.trim().toLowerCase(), v = $("ecsVcpu").value, r = $("ecsRam").value, f = $("ecsFamily").value, a = $("ecsArch").value;
    return (st.options.flavors || []).filter((x) => (!q || x.flavor_id.includes(q)) && (!v || String(x.vcpus) === v) && (!r || String(x.ram_gb) === r)
      && (!f || x.familia === f) && (!a || (a === "arm64" ? x.architecture === "arm64" : x.architecture !== "arm64")));
  }
  function renderFlavors() {
    const all = st.options.flavors || [];
    if (!all.length) {
      $("ecsFlavors").innerHTML = `<tr><td colspan="8" class="py-6 text-center text-slate-500">${st.options.has_project ? "Sin catálogo de flavors consultado para esta región. Pulsa «Actualizar catálogo»." : "Sin Project en la cuenta: no se puede consultar el catálogo de esta región."}</td></tr>`;
      $("ecsFlavorMeta").textContent = "";
      $("ecsSelected").textContent = st.flavor || "—";
      return;
    }
    const rows = visibleFlavors();
    const shown = rows.slice(0, 300);
    const unit = { hour: "/h", month: "/mes", year: "/año" };
    $("ecsFlavors").innerHTML = shown.map((f) => {
      const sel = f.flavor_id === st.flavor;
      const price = f.price ? `<span class="tabular-nums whitespace-nowrap">${esc(money(f.price.amount, f.price.currency))}<span class="text-slate-400">${unit[f.price.period] || ""}</span></span>` : '<span class="text-xs text-slate-400">Sin consultar</span>';
      const badge = `<span class="badge ${f.available ? (f.status === "obt" ? "badge-amber" : "badge-green") : "badge-red"}">${esc(f.estado)}</span>`;
      return `<tr class="cursor-pointer ${sel ? "bg-blue-50 dark:bg-blue-950/30" : ""}" data-flavor="${esc(f.flavor_id)}">
        <td><input type="radio" name="flavor" ${sel ? "checked" : ""} ${f.available ? "" : "disabled"} aria-label="${esc(f.flavor_id)}" /></td>
        <td class="font-mono">${esc(f.flavor_id)}</td><td class="text-right tabular-nums">${esc(f.vcpus)}</td><td class="text-right tabular-nums whitespace-nowrap">${esc(f.ram_gb)} GB</td>
        <td class="text-right">${price}</td><td>${badge}</td>
        <td class="hidden md:table-cell whitespace-nowrap">${esc(f.familia)}${f.architecture === "arm64" ? ' <span class="badge badge-slate">Arm</span>' : ""}</td>
        <td class="hidden 2xl:table-cell text-xs">${esc([f.cpu_name, f.gpu_name].filter(Boolean).join(" · ") || "—")}</td></tr>`;
    }).join("") || '<tr><td colspan="8" class="py-6 text-center text-slate-500">Ningún flavor coincide con los filtros.</td></tr>';
    const updated = st.options.flavors_updated_at ? new Date(st.options.flavors_updated_at).toLocaleString() : "";
    $("ecsFlavorMeta").textContent = `${rows.length} de ${all.length} flavors${updated ? ` · catálogo consultado ${updated}` : ""}`;
    $("ecsSelected").textContent = st.flavor || "—";
  }
  ["ecsVcpu", "ecsRam", "ecsFamily", "ecsArch"].forEach((id) => $(id).addEventListener("change", renderFlavors));
  $("ecsSearch").addEventListener("input", renderFlavors);
  $("ecsFlavors").addEventListener("click", (e) => {
    const row = e.target.closest("[data-flavor]");
    if (!row) return;
    const f = st.options.flavors.find((x) => x.flavor_id === row.dataset.flavor);
    if (f && !f.available) { notify(`${esc(f.flavor_id)} está agotado en esta región.`, "info"); return; }
    st.flavor = row.dataset.flavor;
    renderFlavors();
    scheduleQuote();
  });

  /* ---------- discos de datos ---------- */
  function renderDisks() {
    $("dataDisks").innerHTML = st.disks.map((d, i) => `<div class="flex flex-wrap items-center gap-2" data-disk="${i}">
        <span class="text-xs text-slate-500 w-16">Datos ${i + 1}</span>
        <select class="input w-auto flex-1 min-w-[10rem] max-w-sm" data-disk-type>${diskOptions(d.volume_type)}</select>
        <input type="number" min="10" max="32768" value="${esc(d.size_gb)}" class="input w-28" data-disk-size aria-label="GB" /><span class="text-xs text-slate-500">GB</span>
        <button type="button" class="icon-btn" data-disk-remove title="Quitar"><i data-lucide="x" class="w-4 h-4"></i></button></div>`).join("")
      || '<p class="text-xs text-slate-400">Sin discos de datos.</p>';
    if (st.options) markDisks();
    icons();
  }
  $("addDisk").addEventListener("click", () => { if (st.disks.length < 8) { st.disks.push({ volume_type: "GPSSD", size_gb: 100 }); renderDisks(); scheduleQuote(); } });
  $("dataDisks").addEventListener("input", (e) => {
    const row = e.target.closest("[data-disk]"); if (!row) return;
    const d = st.disks[Number(row.dataset.disk)];
    if (e.target.matches("[data-disk-type]")) d.volume_type = e.target.value;
    if (e.target.matches("[data-disk-size]")) d.size_gb = Number(e.target.value);
    scheduleQuote();
  });
  $("dataDisks").addEventListener("click", (e) => {
    if (!e.target.closest("[data-disk-remove]")) return;
    st.disks.splice(Number(e.target.closest("[data-disk]").dataset.disk), 1); renderDisks(); scheduleQuote();
  });

  /* ---------- EIP ---------- */
  function eipFieldsHtml(prefix) {
    return `<label class="field">Tipo de IP<select id="${prefix}Type" class="input"><option value="5_bgp">BGP dinámico (5_bgp)</option><option value="5_sbgp">BGP estático (5_sbgp)</option></select></label>
      <label class="field">Cobro del ancho de banda<select id="${prefix}Mode" class="input"><option value="bandwidth">Por ancho de banda</option><option value="traffic">Por tráfico</option></select></label>
      <label class="field">Ancho de banda (Mbps)<input id="${prefix}Mbps" type="number" min="1" max="2000" value="5" class="input" /></label>
      <label class="field" id="${prefix}TrafficField">Tráfico estimado (GB)<input id="${prefix}Traffic" type="number" min="0" max="1000000" value="100" class="input" /></label>`;
  }
  function renderEipFields() {
    $("ecsEipFields").innerHTML = eipFieldsHtml("ecsEip");
    $("eipFields").innerHTML = eipFieldsHtml("eip");
    ["ecsEip", "eip"].forEach((p) => {
      const sync = () => { $(`${p}TrafficField`).classList.toggle("hidden", $(`${p}Mode`).value !== "traffic"); scheduleQuote(); };
      [`${p}Type`, `${p}Mode`, `${p}Mbps`, `${p}Traffic`].forEach((id) => $(id).addEventListener("input", sync));
      $(`${p}TrafficField`).classList.add("hidden");
    });
  }
  $("ecsEip").addEventListener("change", () => { $("ecsEipFields").classList.toggle("hidden", !$("ecsEip").checked); scheduleQuote(); });
  function eipConfig(p) {
    return { ip_type: $(`${p}Type`).value, bandwidth_mode: $(`${p}Mode`).value, bandwidth_mbps: Number($(`${p}Mbps`).value),
      traffic_gb: $(`${p}Mode`).value === "traffic" ? Number($(`${p}Traffic`).value) : 0 };
  }

  /* ---------- ítem actual ---------- */
  function currentItem() {
    let config;
    if (st.product === "ecs") {
      config = { flavor: st.flavor, os_type: $("ecsOs").value, system_disk: { volume_type: $("sysType").value, size_gb: Number($("sysSize").value) },
        data_disks: st.disks.map((d) => ({ volume_type: d.volume_type, size_gb: Number(d.size_gb) })), eip: $("ecsEip").checked ? eipConfig("ecsEip") : null };
    } else if (st.product === "evs") {
      config = { volume_type: $("evsType").value, size_gb: Number($("evsSize").value) };
    } else if (st.product === "obs") {
      config = { storage_class: $("obsClass").value };
    } else {
      config = eipConfig("eip");
    }
    return { region: st.region, billing_mode: $("billingMode").value, duration: Number($("duration").value),
      quantity: Number($("quantity").value) || 1, product: st.product, config };
  }

  function scheduleQuote() { clearTimeout(st.timer); st.timer = setTimeout(() => quote().catch(() => {}), 250); }
  async function quote() {
    if (!st.accountId || !st.options) return;
    const item = currentItem();
    if (item.product === "ecs" && !item.config.flavor) {
      $("quoteTotal").textContent = "—"; $("quoteSub").textContent = "Elige un flavor en la tabla."; $("quoteDetail").innerHTML = ""; return;
    }
    const seq = ++st.seq;
    let q;
    try { q = await json(`${calc()}/quote`, { method: "POST", body: JSON.stringify(item) }); }
    catch (err) { if (seq === st.seq) { $("quoteTotal").textContent = "—"; $("quoteSub").textContent = err.message; $("quoteDetail").innerHTML = ""; } return; }
    if (seq !== st.seq) return;
    $("quoteTotal").innerHTML = q.total !== null ? esc(money(q.total, q.currency)) : NA;
    $("quoteSub").textContent = `${regionName(q.region)} · ${modeInfo(q.billing_mode).label} · ${q.duration_text} · cantidad ${q.quantity}`;
    $("quoteDetail").innerHTML = linesTable(q) + (q.warnings.length ? `<ul class="mt-2 text-xs text-amber-700 dark:text-amber-400 list-disc pl-4">${q.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : "");
  }
  function linesTable(q) {
    return `<div class="overflow-x-auto"><table class="w-full text-sm"><thead><tr class="text-xs text-slate-500 text-left"><th class="py-1 pr-2 font-medium">Componente</th><th class="py-1 pr-2 font-medium text-right">Precio unitario</th><th class="py-1 pr-2 font-medium text-right">Cálculo</th><th class="py-1 font-medium text-right">Subtotal</th></tr></thead><tbody>${
      q.lines.map((l) => `<tr class="border-t border-slate-100 dark:border-slate-800">
        <td class="py-1.5 pr-2">${esc(l.label)}</td>
        <td class="py-1.5 pr-2 text-right tabular-nums whitespace-nowrap">${l.available ? `${esc(money(l.unit_amount, l.currency))}/${esc(l.unit)}` : "—"}</td>
        <td class="py-1.5 pr-2 text-right text-xs text-slate-500 whitespace-nowrap">${l.available ? `× ${esc(l.multiplier)}${l.unit === "GB" ? " GB" : ""}${q.quantity * l.units_per_item !== 1 ? ` × ${q.quantity * l.units_per_item}` : ""}` : ""}</td>
        <td class="py-1.5 text-right tabular-nums whitespace-nowrap">${l.available ? esc(money(l.subtotal, l.currency)) : `<span title="${esc(l.reason || "")}">${NA}</span>`}</td></tr>`).join("")
    }</tbody></table></div>`;
  }

  /* ---------- consultas oficiales a Huawei ---------- */
  function showFailures(r) {
    const fails = (r.failures || []).map((f) => `<li>${esc(regionName(f.region))} · ${esc(f.component)}: ${esc(f.reason)}</li>`).join("");
    notify(`${esc(r.stored)} precio(s) oficial(es) guardado(s).${fails ? `<ul class="mt-2 list-disc pl-5">${fails}</ul>` : ""}`, fails ? "info" : "ok");
  }
  async function refresh(items, button) {
    button.disabled = true;
    try { showFailures(await json(`${calc()}/prices/refresh`, { method: "POST", body: JSON.stringify({ items }) })); await loadOptions(false); renderList(); }
    catch (err) { notify(err.status === 403 ? "Tu rol no permite consultar a Huawei (requiere operador)." : esc(err.message)); }
    finally { button.disabled = false; }
  }
  $("btnRefresh").addEventListener("click", () => {
    const item = currentItem();
    if (item.product === "ecs" && !item.config.flavor) { notify("Elige un flavor antes de consultar el precio.", "info"); return; }
    refresh([item], $("btnRefresh"));
  });
  $("btnFlavorPrices").addEventListener("click", () => {
    const flavors = visibleFlavors().filter((f) => f.available).slice(0, 20);
    if (!flavors.length) { notify("No hay flavors disponibles en esta región.", "info"); return; }
    const items = flavors.map((f) => ({ region: st.region, billing_mode: $("billingMode").value, duration: 1, quantity: 1, product: "ecs",
      config: { flavor: f.flavor_id, os_type: $("ecsOs").value, system_disk: { volume_type: $("sysType").value, size_gb: Number($("sysSize").value) } } }));
    refresh(items, $("btnFlavorPrices"));
  });
  $("btnCatalog").addEventListener("click", async () => {
    $("btnCatalog").disabled = true;
    try {
      const r = await json(`${base()}/cost-compare/catalog/refresh?region=${encodeURIComponent(st.region)}`, { method: "POST" });
      const errs = (r.errors || []).map((e) => `<li>${esc(String(e.servicio).toUpperCase())}: ${esc(e.mensaje)}${e.accion_iam ? ` (acción IAM ${esc(e.accion_iam)})` : ""}${e.request_id ? ` · Request ID ${esc(e.request_id)}` : ""}</li>`).join("");
      notify(`Catálogo actualizado: ${r.flavors ?? 0} flavors, ${r.volume_types ?? 0} tipos de disco.${errs ? `<ul class="mt-2 list-disc pl-5">${errs}</ul>` : ""}`, errs ? "info" : "ok");
      await loadOptions(false);
    } catch (err) { notify(err.status === 403 ? "Tu rol no permite consultar a Huawei (requiere operador)." : esc(err.message)); }
    finally { $("btnCatalog").disabled = false; }
  });
  $("btnRefreshFlavors").addEventListener("click", async () => {
    $("btnRefreshFlavors").disabled = true;
    try {
      const r = await json(`${calc()}/flavors/refresh?region=${encodeURIComponent(st.region)}`, { method: "POST" });
      const errs = (r.failures || []).map((f) => `<li>${esc(f.region || "desconocida")}: ${esc(f.reason || f.error || "error")}${f.error_code ? ` (${esc(f.error_code)})` : ""}</li>`).join("");
      const stored = Object.values(r.stored || {}).reduce((a, b) => a + b, 0);
      notify(`${stored} flavors guardado(s).${errs ? `<ul class="mt-2 list-disc pl-5">${errs}</ul>` : ""}`, errs && !stored ? "info" : "ok");
      await loadOptions(false);
    } catch (err) { notify(err.status === 403 ? "Tu rol no permite consultar a Huawei (requiere operador)." : esc(err.message)); }
    finally { $("btnRefreshFlavors").disabled = false; }
  });

  /* ---------- lista de precios ---------- */
  $("btnAdd").addEventListener("click", async () => {
    const item = currentItem();
    if (item.product === "ecs" && !item.config.flavor) { notify("Elige un flavor antes de agregarlo.", "info"); return; }
    try { await json(`${calc()}/quote`, { method: "POST", body: JSON.stringify(item) }); }   // valida antes de agregar
    catch (err) { notify(esc(err.message)); return; }
    if (st.editing !== null) { st.list[st.editing] = item; st.editing = null; $("btnAdd").innerHTML = '<i data-lucide="list-plus" class="w-4 h-4"></i>Agregar a la lista'; }
    else if (st.list.length < 50) st.list.push(item);
    else { notify("Máximo 50 ítems en la lista.", "info"); return; }
    saveList(); renderList(); icons();
  });
  $("btnClearList").addEventListener("click", () => { if (st.list.length && confirmClear()) { st.list = []; saveList(); renderList(); } });
  function confirmClear() { return window.confirm ? window.confirm("¿Vaciar la lista de precios?") : true; }

  const PRODUCT = { ecs: "Servidor (ECS)", evs: "Disco (EVS)", eip: "IP pública (EIP)", obs: "Almacenamiento (OBS)" };
  async function renderList() {
    $("listCount").textContent = st.list.length || "";
    if (!st.list.length) { $("list").innerHTML = '<li class="text-sm text-slate-500">Agrega configuraciones para ver aquí el total.</li>'; $("listTotal").innerHTML = ""; return; }
    let priced;
    try { priced = await json(`${calc()}/list`, { method: "POST", body: JSON.stringify({ items: st.list }) }); }
    catch (err) { $("list").innerHTML = `<li class="text-sm text-red-600">${esc(err.message)}</li>`; return; }
    $("list").innerHTML = priced.items.map((it, i) => `<li class="rounded-xl border border-slate-200 dark:border-slate-800 p-3">
        <div class="flex items-start gap-2"><div class="min-w-0 flex-1">
          <p class="text-sm font-semibold">${esc(PRODUCT[it.product])} ${it.quantity > 1 ? `<span class="text-slate-500 font-normal">× ${esc(it.quantity)}</span>` : ""}</p>
          <p class="text-xs text-slate-500 break-words">${esc(it.summary)}</p>
          <p class="text-xs text-slate-500">${esc(regionName(it.region))} · ${esc(modeInfo(it.billing_mode).label)} · ${esc(it.duration_text)}</p></div>
          <div class="text-right"><p class="text-sm font-semibold tabular-nums whitespace-nowrap">${it.total !== null ? esc(money(it.total, it.currency)) : NA}</p>
            <div class="mt-1 flex justify-end gap-1"><button type="button" class="icon-btn" data-edit="${i}" title="Editar"><i data-lucide="pencil" class="w-3.5 h-3.5"></i></button>
            <button type="button" class="icon-btn" data-remove="${i}" title="Quitar"><i data-lucide="x" class="w-3.5 h-3.5"></i></button></div></div></div></li>`).join("");
    $("listTotal").innerHTML = (priced.totals.length ? priced.totals.map((t) => `<div class="flex items-baseline justify-between"><span class="text-sm text-slate-500">Total estimado</span><span class="text-2xl font-bold tabular-nums">${esc(money(t.total, t.currency))}</span></div>`).join("")
      : `<p class="text-sm">${NA}</p>`) + (priced.incomplete ? `<p class="mt-1 text-xs text-amber-700 dark:text-amber-400">${esc(priced.incomplete)} ítem(s) sin precio completo no se suman. Pulsa «Consultar precios oficiales».</p>` : "");
    icons();
  }
  $("list").addEventListener("click", (e) => {
    const rm = e.target.closest("[data-remove]"), ed = e.target.closest("[data-edit]");
    if (rm) { st.list.splice(Number(rm.dataset.remove), 1); saveList(); renderList(); }
    if (ed) loadIntoForm(Number(ed.dataset.edit)).catch((err) => notify(esc(err.message)));
  });
  async function loadIntoForm(index) {
    const item = st.list[index];
    st.editing = index;
    st.region = item.region; $("billingMode").value = item.billing_mode; fillDuration();
    await loadOptions(false);
    $("duration").value = String(item.duration); $("quantity").value = item.quantity;
    setProduct(item.product);
    const c = item.config;
    if (item.product === "ecs") {
      st.flavor = c.flavor; $("ecsOs").value = c.os_type; $("sysType").value = c.system_disk.volume_type; $("sysSize").value = c.system_disk.size_gb;
      st.disks = (c.data_disks || []).map((d) => ({ ...d })); renderDisks();
      $("ecsEip").checked = !!c.eip; $("ecsEipFields").classList.toggle("hidden", !c.eip);
      if (c.eip) setEip("ecsEip", c.eip);
      renderFlavors();
    } else if (item.product === "evs") { $("evsType").value = c.volume_type; $("evsSize").value = c.size_gb; }
    else if (item.product === "obs") { $("obsClass").value = c.storage_class; }
    else setEip("eip", c);
    $("btnAdd").innerHTML = '<i data-lucide="save" class="w-4 h-4"></i>Guardar cambios'; icons();
    scheduleQuote();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
  function setEip(p, c) {
    $(`${p}Type`).value = c.ip_type; $(`${p}Mode`).value = c.bandwidth_mode; $(`${p}Mbps`).value = c.bandwidth_mbps;
    $(`${p}Traffic`).value = c.traffic_gb || 0; $(`${p}TrafficField`).classList.toggle("hidden", c.bandwidth_mode !== "traffic");
  }

  document.querySelectorAll("[data-export]").forEach((b) => b.addEventListener("click", async () => {
    if (!st.list.length) { notify("La lista está vacía.", "info"); return; }
    b.disabled = true;
    try {
      const res = await call(`${calc()}/export?format=${b.dataset.export}`, { method: "POST", body: JSON.stringify({ items: st.list }) });
      const blob = await res.blob();
      const name = (res.headers.get("content-disposition") || "").match(/filename="([^"]+)"/);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a"); a.href = url; a.download = name ? name[1] : `calculadora.${b.dataset.export}`; a.click();
      URL.revokeObjectURL(url);
    } catch (err) { notify(`No se pudo exportar: ${esc(err.message)}`); }
    finally { b.disabled = false; }
  }));

  renderDisks();
  icons();
  loadClients().catch((e) => notify(esc(e.message)));
})();
