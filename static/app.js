/* Huawei Cloud Inventory — lógica de interfaz (solo lectura) */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const state = {
    tables: [],
    avisos: [],
    service: "",
    region: "",
    consultando: false,
  };

  /* ---------- Tema claro/oscuro ---------- */
  function aplicarTema(tema) {
    const esOscuro = tema === "dark";
    document.documentElement.classList.toggle("dark", esOscuro);
    try { localStorage.setItem("hc_theme", esOscuro ? "dark" : "light"); } catch (e) {}
  }
  try { aplicarTema(localStorage.getItem("hc_theme") === "dark" ? "dark" : "light"); } catch (e) { aplicarTema("light"); }
  $("btnDark").addEventListener("click", () => {
    aplicarTema(document.documentElement.classList.contains("dark") ? "light" : "dark");
  });

  /* ---------- Iconos ---------- */
  function iconos() { if (window.lucide) window.lucide.createIcons(); }
  iconos();

  /* ---------- Utilidades ---------- */
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function claseBadge(estado) {
    const e = String(estado || "").toUpperCase();
    if (e === "ACTIVE" || e === "AVAILABLE") return "bg-emerald-100 text-emerald-700 dark:bg-emerald-400/10 dark:text-emerald-300 ring-1 ring-emerald-200 dark:ring-emerald-400/20";
    if (e === "SHUTOFF") return "bg-slate-200 text-slate-700 dark:bg-slate-700 dark:text-slate-300 ring-1 ring-slate-300 dark:ring-slate-600";
    if (e === "ERROR") return "bg-red-100 text-red-700 dark:bg-red-400/10 dark:text-red-300 ring-1 ring-red-200 dark:ring-red-400/20";
    if (e === "BUILD" || e === "CREATING" || e === "PENDING") return "bg-blue-100 text-blue-700 dark:bg-blue-400/10 dark:text-blue-300 ring-1 ring-blue-200 dark:ring-blue-400/20";
    return "bg-amber-100 text-amber-700 dark:bg-amber-400/10 dark:text-amber-300 ring-1 ring-amber-200 dark:ring-amber-400/20";
  }

  /* ---------- Alertas ---------- */
  function alerta(titulo, detalle, tono) {
    const wrap = $("alerts");
    const div = document.createElement("div");
    const aviso = tono === "aviso";
    div.className = aviso
      ? "flex items-start gap-3 rounded-xl border border-amber-200 dark:border-amber-400/30 bg-amber-50 dark:bg-amber-400/10 text-amber-700 dark:text-amber-300 px-4 py-3 text-sm animate-fade-in"
      : "flex items-start gap-3 rounded-xl border border-red-200 dark:border-red-400/30 bg-red-50 dark:bg-red-400/10 text-red-700 dark:text-red-300 px-4 py-3 text-sm animate-fade-in";
    let html = `<i data-lucide="${aviso ? "info" : "alert-triangle"}" class="w-5 h-5 shrink-0 mt-0.5"></i><div class="flex-1 min-w-0"><p class="font-bold">${escapeHtml(titulo)}</p>`;
    if (detalle) {
      html += `<p class="mt-1 break-words">${escapeHtml(detalle.mensaje || "")}</p>`;
      const meta = [];
      if (detalle.http_status) meta.push(`HTTP: ${detalle.http_status}`);
      if (detalle.request_id) meta.push(`ReqID: ${detalle.request_id}`);
      if (detalle.error_code) meta.push(`Code: ${detalle.error_code}`);
      if (meta.length) html += `<p class="mt-1 text-xs opacity-70 font-mono break-all">${escapeHtml(meta.join(" | "))}</p>`;
    }
    html += "</div>";
    div.innerHTML = html;
    const btn = document.createElement("button");
    btn.className = "opacity-50 hover:opacity-100 transition-opacity shrink-0";
    btn.innerHTML = '<i data-lucide="x" class="w-4 h-4"></i>';
    btn.addEventListener("click", () => div.remove());
    div.appendChild(btn);
    wrap.appendChild(div);
    iconos();
  }
  function limpiarAlertas() { $("alerts").innerHTML = ""; }

  /* ---------- Estado de carga ---------- */
  function setCargando(cargando) {
    state.consultando = cargando;
    const btn = $("btnConsultar");
    btn.disabled = cargando;
    $("btnLimpiar").disabled = cargando;
    $("btnConsultarText").textContent = cargando ? "Consultando..." : "Consultar";
    const icono = btn.querySelector("i, svg");
    if (icono) icono.classList.toggle("animate-spin", cargando);
  }

  /* ---------- Consulta ---------- */
  $("frmConsulta").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (state.consultando) return;
    limpiarAlertas();
    setCargando(true);
    $("results").classList.add("hidden");
    $("summary").classList.add("hidden");
    $("emptyState").classList.add("hidden");

    const payload = {
      ak: $("ak").value.trim(),
      sk: $("sk").value,
      project_id: $("projectId").value.trim(),
      region: $("region").value,
      service: $("service").value,
    };

    try {
      const resp = await fetch("/api/inventory", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await resp.json();
      if (!resp.ok) {
        const detalle = data && data.detail ? data.detail : { mensaje: "Error inesperado" };
        alerta("No se pudo completar la consulta", typeof detalle === "string" ? { mensaje: detalle } : detalle);
        $("emptyState").classList.remove("hidden");
        return;
      }
      state.service = data.service;
      state.region = data.region;
      state.tables = data.tables || [];
      $("topRegion").textContent = data.region;
      $("topProject").textContent = data.project_id_masked;

      if (Array.isArray(data.errores)) {
        data.errores.forEach((e) => alerta(`Error en ${e.servicio || ""}`, e));
      }
      if (Array.isArray(data.avisos)) {
        state.avisos = data.avisos;
        data.avisos.forEach((a) => alerta(`Aviso (${a.servicio || "servicio"})`, a, "aviso"));
      } else {
        state.avisos = [];
      }
      renderResumen(data.resumen || []);
      renderResultados();
      $("btnExportar").classList.toggle("hidden", !state.tables.some((t) => t.filas.length > 0));
      if (!state.tables.some((t) => t.filas.length > 0)) {
        $("emptyState").classList.remove("hidden");
      }
      iconos();
    } catch (err) {
      alerta("Error de red", { mensaje: err.message });
      $("emptyState").classList.remove("hidden");
    } finally {
      setCargando(false);
      iconos();
    }
  });

  /* ---------- Resumen ---------- */
  function renderResumen(resumen) {
    const sec = $("summary");
    sec.innerHTML = "";
    const iconos = { "Total ECS": "server", "ECS activas": "check-circle", "ECS apagadas": "power", "Total vCPU": "cpu", "Total RAM (GB)": "memory-stick" };
    resumen.filter((r) => r.value > 0).forEach((r) => {
      const card = document.createElement("div");
      card.className = "bg-white dark:bg-slate-900 rounded-xl border border-slate-200 dark:border-slate-800 shadow-sm p-4 flex items-center gap-3";
      const icono = iconos[r.label] || "box";
      card.innerHTML = `
        <div class="w-10 h-10 rounded-lg bg-blue-50 dark:bg-blue-500/10 grid place-items-center shrink-0">
          <i data-lucide="${icono}" class="w-5 h-5 text-blue-600 dark:text-blue-400"></i>
        </div>
        <div class="min-w-0">
          <p class="text-[11px] font-semibold uppercase tracking-wide text-slate-400 truncate">${escapeHtml(r.label)}</p>
          <p class="text-xl font-bold text-slate-800 dark:text-slate-100">${escapeHtml(r.value)}</p>
        </div>`;
      sec.appendChild(card);
    });
    sec.classList.toggle("hidden", resumen.length === 0);
  }

  /* ---------- Tabla ---------- */
  function valorEstado(fila) { return fila["Estado"] || fila["estado"] || fila["Status"] || ""; }
  function valorNombre(fila) { return fila["Nombre ECS"] || fila["Nombre"] || ""; }
  function numeroDe(fila, claves) {
    for (const k of claves) {
      if (fila[k] !== undefined && fila[k] !== "-" && fila[k] !== "") {
        const n = parseFloat(fila[k]);
        if (!isNaN(n)) return n;
      }
    }
    return 0;
  }

  function filtrarOrdenar() {
    const q = $("q").value.trim().toLowerCase();
    const est = $("fEstado").value;
    const orden = $("orden").value;
    return state.tables.map((t) => {
      let filas = t.filas.filter((f) => {
        const texto = Object.values(f).filter((v) => typeof v !== "object").join(" ").toLowerCase();
        const coincideQ = !q || texto.includes(q);
        const coincideEst = !est || String(valorEstado(f)).toUpperCase() === est.toUpperCase();
        return coincideQ && coincideEst;
      });
      if (orden === "nombre") filas = filas.slice().sort((a, b) => String(valorNombre(a)).localeCompare(String(valorNombre(b))));
      if (orden === "estado") filas = filas.slice().sort((a, b) => String(valorEstado(a)).localeCompare(String(valorEstado(b))));
      if (orden === "vcpu") filas = filas.slice().sort((a, b) => numeroDe(b, ["vCPU"]) - numeroDe(a, ["vCPU"]));
      if (orden === "ram") filas = filas.slice().sort((a, b) => numeroDe(b, ["RAM (GB)", "RAM"]) - numeroDe(a, ["RAM (GB)", "RAM"]));
      return { ...t, filas };
    });
  }

  function poblarFiltroEstado() {
    const set = new Set();
    state.tables.forEach((t) => t.filas.forEach((f) => set.add(String(valorEstado(f) || "-"))));
    const sel = $("fEstado");
    sel.innerHTML = '<option value="">Todos los estados</option>';
    [...set].sort().forEach((e) => {
      const o = document.createElement("option");
      o.value = e; o.textContent = e;
      sel.appendChild(o);
    });
  }

  function renderTablas() {
    const wrap = $("tables");
    wrap.innerHTML = "";
    const tablas = filtrarOrdenar().filter((t) => t.filas.length > 0);
    let total = 0;
    tablas.forEach((t) => (total += t.filas.length));
    $("contador").textContent = total > 0 ? `${total} resultado${total !== 1 ? "s" : ""}` : "";

    if (tablas.length === 0) {
      wrap.innerHTML = '<div class="text-center py-10"><i data-lucide="inbox" class="w-10 h-10 text-slate-300 dark:text-slate-600 mx-auto mb-3"></i><p class="text-sm text-slate-500">No hay recursos que coincidan con los filtros actuales.</p></div>';
      iconos();
      return;
    }

    tablas.forEach((t) => {
      const box = document.createElement("div");
      const encabezado = `<div class="flex items-center justify-between mb-2"><h4 class="font-bold text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400 flex items-center gap-2"><i data-lucide="database" class="w-3.5 h-3.5"></i>${escapeHtml(t.titulo)}</h4><span class="text-xs text-slate-400 font-medium bg-slate-100 dark:bg-slate-800 px-2 py-0.5 rounded-full">${t.filas.length} recurso${t.filas.length !== 1 ? "s" : ""}</span></div>`;
      let thead = t.columnas.map((c) => `<th class="px-4 py-3 text-left text-[11px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 whitespace-nowrap">${escapeHtml(c)}</th>`).join("");
      thead += '<th class="px-4 py-3"></th>';
      let body = "";
      t.filas.forEach((f, idx) => {
        const tds = t.columnas.map((c) => {
          const val = f[c];
          if (c === "Estado") {
            return `<td class="px-4 py-3"><span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-semibold ${claseBadge(val)}">${escapeHtml(val)}</span></td>`;
          }
          return `<td class="px-4 py-3 whitespace-nowrap text-slate-700 dark:text-slate-300">${escapeHtml(val === undefined || val === null ? "-" : val)}</td>`;
        }).join("");
        body += `<tr class="${idx % 2 ? "bg-slate-50 dark:bg-slate-950/40" : ""} hover:bg-blue-50/50 dark:hover:bg-blue-400/5 transition-colors">${tds}
          <td class="px-4 py-3 text-right"><button data-idx="${idx}" data-tabla="${escapeHtml(t.titulo)}" class="btn-detalle text-xs font-semibold text-blue-600 hover:text-blue-800 dark:text-blue-400 hover:underline">Ver detalles</button></td></tr>`;
      });
      box.innerHTML = `${encabezado}
        <div class="overflow-x-auto rounded-xl border border-slate-200 dark:border-slate-800">
          <table class="min-w-full text-sm">
            <thead class="bg-slate-100 dark:bg-slate-800"><tr>${thead}</tr></thead>
            <tbody class="divide-y divide-slate-100 dark:divide-slate-800">${body}</tbody>
          </table>
        </div>`;
      wrap.appendChild(box);
    });

    wrap.querySelectorAll(".btn-detalle").forEach((btn) => {
      btn.addEventListener("click", () => {
        const tit = btn.getAttribute("data-tabla");
        const idx = parseInt(btn.getAttribute("data-idx"), 10);
        const tabla = filtrarOrdenar().find((t) => t.titulo === tit);
        if (tabla && tabla.filas[idx]) abrirDrawer(tabla.filas[idx]._detalle || {});
      });
    });

    iconos();
  }

  function renderResultados() {
    poblarFiltroEstado();
    renderTablas();
    $("results").classList.remove("hidden");
  }

  ["q", "fEstado", "orden"].forEach((id) =>
    $(id).addEventListener("input", () => { if (state.tables.length) renderTablas(); })
  );

  /* ---------- Drawer ---------- */
  function abrirDrawer(detalle) {
    const body = $("drawerBody");
    body.innerHTML = "";
    const entradas = Object.entries(detalle);
    if (entradas.length === 0) {
      body.innerHTML = '<p class="text-sm text-slate-500">Sin detalles disponibles.</p>';
    }
    entradas.forEach(([k, v]) => {
      const div = document.createElement("div");
      div.className = "bg-slate-50 dark:bg-slate-800/50 rounded-lg p-3";
      div.innerHTML = `<dt class="text-[10px] font-bold text-slate-400 uppercase tracking-wider mb-1">${escapeHtml(k)}</dt><dd class="break-words text-slate-700 dark:text-slate-300 font-mono text-xs">${escapeHtml(v)}</dd>`;
      body.appendChild(div);
    });
    $("drawer").classList.remove("translate-x-full");
    $("drawerOverlay").classList.remove("hidden");
  }
  function cerrarDrawer() {
    $("drawer").classList.add("translate-x-full");
    $("drawerOverlay").classList.add("hidden");
  }
  $("drawerClose").addEventListener("click", cerrarDrawer);
  $("drawerOverlay").addEventListener("click", cerrarDrawer);

  /* ---------- Exportar ---------- */
  async function exportar() {
    if (state.tables.length === 0) return;
    limpiarAlertas();
    const payload = {
      region: state.region,
      service: state.service,
      tables: state.tables.filter((t) => t.filas.length > 0).map((t) => ({
        titulo: t.titulo,
        columnas: t.columnas,
        filas: t.filas.map((f) => f),
      })),
      avisos: (state.avisos || []).map((a) => ({
        servicio: a.servicio || "",
        http_status: a.http_status || "",
        request_id: a.request_id || "",
        error_code: a.error_code || "",
        mensaje: a.mensaje || "",
      })),
    };
    try {
      const resp = await fetch("/api/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!resp.ok) {
        alerta("No se pudo exportar", { mensaje: `HTTP Status ${resp.status}` });
        return;
      }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `inventario_${state.service}_${state.region}.xlsx`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      alerta("Error al exportar", { mensaje: err.message });
    }
  }
  $("btnExportar").addEventListener("click", exportar);
  $("navExport").addEventListener("click", (e) => { e.preventDefault(); exportar(); });

  /* ---------- Ver / ocultar SK ---------- */
  $("btnVerSk").addEventListener("click", () => {
    const sk = $("sk");
    sk.type = sk.type === "password" ? "text" : "password";
  });

  /* ---------- Limpiar ---------- */
  $("btnLimpiar").addEventListener("click", () => {
    if (state.consultando) return;
    $("frmConsulta").reset();
    state.tables = [];
    state.avisos = [];
    $("results").classList.add("hidden");
    $("summary").classList.add("hidden");
    $("tables").innerHTML = "";
    $("contador").textContent = "";
    $("btnExportar").classList.add("hidden");
    $("topRegion").textContent = "—";
    $("topProject").textContent = "—";
    $("emptyState").classList.remove("hidden");
    limpiarAlertas();
  });

  /* ---------- Nueva consulta ---------- */
  $("btnNueva").addEventListener("click", () => {
    $("btnLimpiar").click();
    $("ak").value = "";
    $("sk").value = "";
    $("sk").type = "password";
    $("projectId").value = "";
    window.scrollTo({ top: 0, behavior: "smooth" });
  });

  /* ---------- Texto del botón según servicio ---------- */
  $("service").addEventListener("change", () => {
    const map = { ecs: "Consultar ECS", evs: "Consultar EVS", vpc: "Consultar VPC", vpn: "Consultar VPN", obs: "Consultar OBS", eip: "Consultar EIP", cbr: "Consultar CBR", elb: "Consultar ELB", rds: "Consultar RDS", dcs: "Consultar DCS", nat: "Consultar NAT", ces: "Consultar Cloud Eye", hss: "Consultar HSS", waf: "Consultar WAF", cfw: "Consultar CFW", todos: "Consultar todo" };
    $("btnConsultarText").textContent = map[$("service").value] || "Consultar";
  });
  $("service").dispatchEvent(new Event("change"));

  /* ---------- Help modal ---------- */
  $("btnHelp").addEventListener("click", () => { $("helpModal").classList.remove("hidden"); });
  $("helpClose").addEventListener("click", () => { $("helpModal").classList.add("hidden"); });
  $("helpOverlay").addEventListener("click", () => { $("helpModal").classList.add("hidden"); });
  $("btnCredHelp").addEventListener("click", () => { $("helpModal").classList.remove("hidden"); });

  /* ---------- Atajos de teclado ---------- */
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      cerrarDrawer();
      $("helpModal").classList.add("hidden");
    }
  });
})();
