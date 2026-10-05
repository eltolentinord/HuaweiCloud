/* Auditoría de servidores: alta/edición de servidores (contraseña cifrada en el servidor),
 * ejecución del script del usuario por SSH en lote y reportes. Vanilla JS sobre la API interna.
 * La contraseña solo viaja al guardar el formulario; nunca se lee de vuelta. Todo texto se escapa. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const st = { clientId: "", servers: [], selected: new Set(), editing: null, batch: null, poll: null };

  function esc(v) { return String(v === undefined || v === null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
  function icons() { if (window.lucide) window.lucide.createIcons(); }
  const fmt = (d) => (d ? new Date(d).toLocaleString() : "—");
  function notify(html, tone) {
    const tones = { error: "border-red-200 bg-red-50 text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300",
      info: "border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300",
      ok: "border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/30 dark:text-emerald-300" };
    const div = document.createElement("div");
    div.className = `${tones[tone || "error"]} rounded-xl border px-4 py-3 text-sm flex items-start gap-3`;
    div.innerHTML = `<div class="flex-1">${html}</div><button type="button" class="opacity-60 hover:opacity-100" aria-label="Cerrar">✕</button>`;
    div.querySelector("button").addEventListener("click", () => div.remove());
    $("alerts").appendChild(div);
  }
  async function call(path, options) {
    const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, options || {}));
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try { const b = await res.json(); msg = typeof b.detail === "string" ? b.detail : (Array.isArray(b.detail) ? b.detail.map((d) => d.msg).join("; ") : (b.detail && b.detail.mensaje) || msg); } catch (e) {}
      const err = new Error(msg); err.status = res.status; throw err;
    }
    return res;
  }
  const json = async (p, o) => { const r = await call(p, o); return r.status === 204 ? null : r.json(); };
  const base = () => `/api/clients/${st.clientId}/servers`;
  const denied = (err) => (err.status === 403 ? "Tu rol no permite esta acción." : esc(err.message));

  function applyTheme(dark) { document.documentElement.classList.toggle("dark", dark); try { localStorage.setItem("hc_theme", dark ? "dark" : "light"); } catch (e) {} }
  try { applyTheme(localStorage.getItem("hc_theme") === "dark"); } catch (e) {}
  $("btnDark").addEventListener("click", () => applyTheme(!document.documentElement.classList.contains("dark")));

  /* ---------- clientes ---------- */
  async function loadClients() {
    let clients;
    try { clients = await json("/api/admin/clients"); }
    catch (err) { notify(err.status === 404 || err.status === 401 ? "La API interna no está activa (INVENTORY_ADMIN_API=true)." : denied(err), "info"); return; }
    $("clientSelect").innerHTML = clients.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("");
    const wanted = new URLSearchParams(location.search).get("client");
    st.clientId = clients.some((c) => c.id === wanted) ? wanted : (clients[0] || {}).id || "";
    $("clientSelect").value = st.clientId;
    if (st.clientId) await loadServers();
  }
  $("clientSelect").addEventListener("change", () => { st.clientId = $("clientSelect").value; st.selected.clear(); $("detailPanel").classList.add("hidden"); loadServers().catch((e) => notify(denied(e))); });

  /* ---------- servidores ---------- */
  function bar(pct) {
    if (pct === null || pct === undefined) return '<span class="text-xs text-slate-400">—</span>';
    const color = pct >= 80 ? "bg-emerald-500" : pct >= 50 ? "bg-amber-500" : "bg-red-500";
    return `<div class="flex items-center gap-2 min-w-[7rem]"><div class="h-2 flex-1 rounded-full bg-slate-200 dark:bg-slate-700 overflow-hidden"><div class="h-full ${color}" style="width:${Math.max(0, Math.min(100, Number(pct)))}%"></div></div><span class="tabular-nums text-xs w-9 text-right">${esc(pct)}%</span></div>`;
  }
  const STATUS = { queued: ["En cola", "badge-slate"], running: ["Auditando…", "badge-blue"], succeeded: ["Listo", "badge-green"], failed: ["Error", "badge-red"] };
  const statusBadge = (s) => { const [t, c] = STATUS[s] || [s, "badge-slate"]; return `<span class="badge ${c}">${esc(t)}</span>`; };

  async function loadServers() {
    const data = await json(base());
    st.servers = data.servers;
    $("scriptInfo").textContent = `Script: server-audit.sh · SHA256 ${data.script_sha256.slice(0, 12)}… · se ejecuta por SSH con sudo, en modo de solo lectura, y se borra del servidor al terminar.`;
    renderServers();
  }
  function renderServers() {
    $("serverCount").textContent = st.servers.length || "";
    $("serverTable").innerHTML = st.servers.map((s) => {
      const a = s.last_audit;
      const last = !a ? '<span class="text-xs text-slate-400">Nunca</span>'
        : `${statusBadge(a.status)}<div class="text-xs text-slate-500">${esc(fmt(a.finished_at || a.created_at))}</div>${a.status === "failed" ? `<div class="text-xs text-red-600 dark:text-red-400 max-w-xs break-words">${esc(a.error_message || "")}</div>` : ""}`;
      return `<tr class="${s.enabled ? "" : "opacity-60"}">
        <td><input type="checkbox" data-check="${esc(s.id)}" ${st.selected.has(s.id) ? "checked" : ""} aria-label="Seleccionar ${esc(s.name)}" /></td>
        <td><button type="button" class="text-left font-medium hover:underline" data-detail="${esc(s.id)}">${esc(s.name)}</button>
          <div class="text-xs text-slate-500 font-mono">${esc(s.username)}@${esc(s.host)}:${esc(s.port)}</div>${s.enabled ? "" : '<span class="badge badge-slate">Deshabilitado</span>'}</td>
        <td class="hidden md:table-cell text-xs">${esc((a && a.os) || "—")}</td>
        <td>${bar(a && a.overall_score)}</td><td class="hidden 2xl:table-cell">${bar(a && a.hardening_pct)}</td><td class="hidden 2xl:table-cell">${bar(a && a.updates_pct)}</td>
        <td class="hidden sm:table-cell">${last}</td>
        <td class="text-right"><div class="inline-flex flex-wrap justify-end gap-1 [&_.icon-btn]:!h-8 [&_.icon-btn]:!w-8">
          <button type="button" class="icon-btn" data-audit="${esc(s.id)}" title="Auditar"><i data-lucide="play" class="w-4 h-4"></i></button>
          <button type="button" class="icon-btn" data-test="${esc(s.id)}" title="Probar conexión"><i data-lucide="plug-zap" class="w-4 h-4"></i></button>
          <button type="button" class="icon-btn" data-edit="${esc(s.id)}" title="Editar"><i data-lucide="pencil" class="w-4 h-4"></i></button>
          ${a && a.has_html ? `<a class="icon-btn" href="${base()}/audits/${esc(a.id)}/report.html" target="_blank" rel="noopener" title="Reporte HTML del script"><i data-lucide="file-search" class="w-4 h-4"></i></a>` : ""}
          <button type="button" class="icon-btn" data-delete="${esc(s.id)}" title="Borrar"><i data-lucide="trash-2" class="w-4 h-4"></i></button></div></td></tr>`;
    }).join("") || '<tr><td colspan="8" class="py-8 text-center text-slate-500">Aún no hay servidores. Pulsa «Agregar servidor».</td></tr>';
    $("btnAuditSelected").disabled = st.selected.size === 0;
    $("checkAll").checked = st.servers.length > 0 && st.selected.size === st.servers.length;
    icons();
  }

  $("checkAll").addEventListener("change", () => { st.selected = new Set($("checkAll").checked ? st.servers.map((s) => s.id) : []); renderServers(); });
  $("serverTable").addEventListener("change", (e) => {
    const c = e.target.closest("[data-check]"); if (!c) return;
    if (c.checked) st.selected.add(c.dataset.check); else st.selected.delete(c.dataset.check);
    $("btnAuditSelected").disabled = st.selected.size === 0;
  });
  $("serverTable").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    if (b.dataset.audit) audit([b.dataset.audit]);
    else if (b.dataset.test) testServer(b.dataset.test, b);
    else if (b.dataset.edit) openForm(st.servers.find((s) => s.id === b.dataset.edit));
    else if (b.dataset.delete) removeServer(st.servers.find((s) => s.id === b.dataset.delete));
    else if (b.dataset.detail) showDetail(b.dataset.detail).catch((err) => notify(denied(err)));
  });

  /* ---------- formulario ---------- */
  function openForm(server) {
    st.editing = server || null;
    const f = $("serverForm");
    f.reset();
    $("formTitle").textContent = server ? `Editar ${server.name}` : "Agregar servidor";
    f.elements.name.value = server ? server.name : "";
    f.elements.host.value = server ? server.host : "";
    f.elements.port.value = server ? server.port : 22;
    f.elements.username.value = server ? server.username : "";
    f.elements.description.value = server ? server.description || "" : "";
    f.elements.enabled.checked = server ? server.enabled : true;
    f.elements.password.required = !server;
    f.elements.password.placeholder = server ? "Déjala vacía para conservar la actual" : "";
    $("passwordHint").textContent = server ? "Déjala vacía para conservar la contraseña guardada (cifrada). Se usa para SSH y sudo."
      : "La contraseña se guarda cifrada y nunca se muestra. También se usa para sudo.";
    ["formBackdrop", "formDialog"].forEach((id) => $(id).classList.remove("hidden"));
    f.elements.name.focus();
  }
  function closeForm() { $("serverForm").reset(); ["formBackdrop", "formDialog"].forEach((id) => $(id).classList.add("hidden")); st.editing = null; }
  $("btnNew").addEventListener("click", () => openForm(null));
  ["formClose", "formCancel", "formBackdrop"].forEach((id) => $(id).addEventListener("click", closeForm));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("formDialog").classList.contains("hidden")) closeForm(); });
  $("serverForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const body = { name: f.elements.name.value.trim(), host: f.elements.host.value.trim(), port: Number(f.elements.port.value),
      username: f.elements.username.value.trim(), password: f.elements.password.value, description: f.elements.description.value.trim(),
      enabled: f.elements.enabled.checked };
    $("formSubmit").disabled = true;
    try {
      if (st.editing) await json(`${base()}/${st.editing.id}`, { method: "PUT", body: JSON.stringify(body) });
      else await json(base(), { method: "POST", body: JSON.stringify(body) });
      closeForm();   // también vacía el campo de contraseña
      notify(`Servidor ${esc(body.name)} guardado. La contraseña quedó cifrada.`, "ok");
      await loadServers();
    } catch (err) { notify(denied(err)); }
    finally { $("formSubmit").disabled = false; }
  });

  async function removeServer(server) {
    if (!server || !window.confirm(`¿Borrar el servidor «${server.name}»? Su historial de auditorías se conserva.`)) return;
    try { await json(`${base()}/${server.id}`, { method: "DELETE" }); st.selected.delete(server.id); await loadServers(); }
    catch (err) { notify(denied(err)); }
  }

  async function testServer(id, button) {
    button.disabled = true;
    const s = st.servers.find((x) => x.id === id);
    try {
      const r = await json(`${base()}/${id}/test`, { method: "POST" });
      if (r.ok) notify(`<strong>${esc(s.name)}</strong>: conexión SSH correcta. ${esc(r.detail)}<div class="text-xs mt-1 font-mono">Huella: ${esc(r.fingerprint)}</div>`, "ok");
      else notify(`<strong>${esc(s.name)}</strong>: ${esc(r.message || r.detail || "la prueba falló")}${r.fingerprint ? `<div class="text-xs mt-1 font-mono">Huella: ${esc(r.fingerprint)}</div>` : ""}`, "info");
      await loadServers();
    } catch (err) { notify(denied(err)); }
    finally { button.disabled = false; }
  }

  /* ---------- auditorías ---------- */
  async function audit(ids) {
    try {
      const r = await json(`${base()}/audits`, { method: "POST", body: JSON.stringify(ids ? { server_ids: ids } : {}) });
      st.batch = r.batch_id;
      $("batchPanel").classList.remove("hidden");
      renderBatch(r.runs);
      pollBatch();
    } catch (err) { notify(denied(err)); }
  }
  $("btnAuditAll").addEventListener("click", () => audit(null));
  $("btnAuditSelected").addEventListener("click", () => audit([...st.selected]));

  function renderBatch(runs) {
    const pending = runs.filter((r) => r.status === "queued" || r.status === "running").length;
    $("batchState").textContent = pending ? `${runs.length - pending} de ${runs.length} terminados` : `Terminado: ${runs.filter((r) => r.status === "succeeded").length} correctos, ${runs.filter((r) => r.status === "failed").length} con error`;
    $("batchList").innerHTML = runs.map((r) => `<li class="rounded-xl border border-slate-200 dark:border-slate-800 p-3">
      <div class="flex items-center gap-2"><span class="font-medium mr-auto">${esc(r.server_name)}</span>${statusBadge(r.status)}</div>
      ${r.status === "succeeded" ? `<div class="mt-2">${bar(r.overall_score)}</div>` : ""}
      ${r.status === "failed" ? `<p class="mt-1 text-xs text-red-600 dark:text-red-400 break-words">${esc(r.error_message || "")}</p>` : ""}</li>`).join("");
    return pending;
  }
  function pollBatch() {
    clearTimeout(st.poll);
    st.poll = setTimeout(async () => {
      try {
        const r = await json(`${base()}/audits?batch_id=${encodeURIComponent(st.batch)}`);
        const pending = renderBatch(r.runs);
        if (pending) pollBatch(); else { await loadServers(); notify("Auditoría terminada. Puedes descargar el comparativo en PDF, Excel o HTML.", "ok"); }
      } catch (err) { notify(denied(err)); }
    }, 3000);
  }

  /* ---------- detalle ---------- */
  async function showDetail(id) {
    const s = st.servers.find((x) => x.id === id);
    const history = await json(`${base()}/audits?server_id=${encodeURIComponent(id)}&limit=20`);
    const last = history.runs.find((r) => r.status === "succeeded");
    const detail = last ? await json(`${base()}/audits/${last.id}`) : null;
    const res = detail && detail.result;
    const order = { FAIL: 0, WARN: 1, INFO: 2, OK: 3 };
    const tone = { FAIL: "badge-red", WARN: "badge-amber", OK: "badge-green", INFO: "badge-slate" };
    const checks = res ? [...res.checks].sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9)) : [];
    $("detailPanel").innerHTML = `
      <div class="flex flex-wrap items-start gap-3"><div class="mr-auto"><h2 class="card-title">${esc(s.name)}</h2>
        <p class="text-xs text-slate-500 font-mono">${esc(s.username)}@${esc(s.host)}:${esc(s.port)} · huella ${esc(s.host_fingerprint || "aún no registrada")}</p></div>
        ${s.host_fingerprint ? `<button type="button" class="btn-ghost btn-sm" data-reset="${esc(s.id)}" title="Solo si el servidor se reinstaló o cambió de clave SSH"><i data-lucide="key-round" class="w-4 h-4"></i>Aceptar nueva huella</button>` : ""}
        <button type="button" class="icon-btn" data-close aria-label="Cerrar"><i data-lucide="x" class="w-4 h-4"></i></button></div>
      ${res ? `<div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <div class="kpi"><p class="kpi-label">Puntaje general</p>${bar(res.overall_score)}</div>
          <div class="kpi"><p class="kpi-label">Hardening ${esc(res.hardening.got)}/${esc(res.hardening.max)}</p>${bar(res.hardening.pct)}</div>
          <div class="kpi"><p class="kpi-label">Actualizaciones ${esc(res.updates.got)}/${esc(res.updates.max)}</p>${bar(res.updates.pct)}</div></div>
        <p class="text-xs text-slate-500">${esc(res.os)} · auditado ${esc(res.generated_at)} · hostname ${esc(res.hostname)}</p>
        <div class="overflow-x-auto"><table class="table-pro min-w-full text-sm"><thead><tr><th class="text-left">Estado</th><th class="text-left">Control</th><th class="text-right">Puntos</th><th class="text-left">Recomendación</th></tr></thead><tbody>
          ${checks.map((c) => `<tr><td><span class="badge ${tone[c.status] || "badge-slate"}">${esc(c.status)}</span></td><td>${esc(c.description)}<div class="text-xs text-slate-400">${esc(c.category)}</div></td><td class="text-right tabular-nums">${esc(c.points)}/${esc(c.max)}</td><td class="text-xs">${esc(c.recommendation || "—")}</td></tr>`).join("")}</tbody></table></div>
        ${res.sites && res.sites.length ? `<div><h3 class="font-semibold text-sm mb-2">Sitios web (${res.sites.length})</h3><div class="flex flex-wrap gap-1">${res.sites.map((x) => `<span class="badge badge-slate" title="${esc(x.source)}">${esc(x.name)}</span>`).join("")}</div></div>` : ""}`
        : '<p class="text-sm text-slate-500">Este servidor aún no tiene auditorías correctas.</p>'}
      <div><h3 class="font-semibold text-sm mb-2">Historial</h3><ul class="text-xs space-y-1">${history.runs.map((r) => `<li class="flex flex-wrap items-center gap-2">${statusBadge(r.status)}<span>${esc(fmt(r.finished_at || r.created_at))}</span>${r.overall_score !== null ? `<span class="tabular-nums">${esc(r.overall_score)}%</span>` : ""}${r.error_message ? `<span class="text-red-600 dark:text-red-400">${esc(r.error_message)}</span>` : ""}${r.has_html ? `<a class="text-blue-600 hover:underline" href="${base()}/audits/${esc(r.id)}/report.html" target="_blank" rel="noopener">reporte HTML</a>` : ""}</li>`).join("") || "<li>Sin auditorías.</li>"}</ul></div>`;
    $("detailPanel").classList.remove("hidden");
    icons();
    $("detailPanel").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  $("detailPanel").addEventListener("click", async (e) => {
    if (e.target.closest("[data-close]")) { $("detailPanel").classList.add("hidden"); return; }
    const r = e.target.closest("[data-reset]");
    if (r && window.confirm("¿Aceptar una huella nueva? Hazlo solo si sabes que el servidor se reinstaló o cambió su clave SSH.")) {
      try { await json(`${base()}/${r.dataset.reset}/fingerprint/reset`, { method: "POST" }); notify("La huella se registrará de nuevo en la próxima conexión.", "ok"); await loadServers(); await showDetail(r.dataset.reset); }
      catch (err) { notify(denied(err)); }
    }
  });

  /* ---------- reportes ---------- */
  document.querySelectorAll("[data-report]").forEach((b) => b.addEventListener("click", async () => {
    const format = b.dataset.report;
    const url = `${base()}/audits/report?format=${format}${st.batch ? `&batch_id=${encodeURIComponent(st.batch)}` : ""}`;
    if (format === "html") { window.open(url, "_blank", "noopener"); return; }
    b.disabled = true;
    try {
      const res = await call(url);
      const blob = await res.blob();
      const name = (res.headers.get("content-disposition") || "").match(/filename="([^"]+)"/);
      const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name ? name[1] : `auditoria.${format}`; a.click();
      URL.revokeObjectURL(a.href);
    } catch (err) { notify(denied(err)); }
    finally { b.disabled = false; }
  }));

  icons();
  loadClients().catch((e) => notify(denied(e)));
})();
