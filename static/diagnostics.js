// Diagnósticos Cloud Eye — SPA
'use strict';

console.info('[diagnostics] delete UI loaded');

const API = '/api';
let _token      = null;
let _clientId   = null;
let _currentIncident = null;
// Pre-inicializado desde el HTML (window._CAN_DELETE_DIAGNOSTICS inyectado por el servidor).
// loadWhoami() puede corregirlo después si el token se provee en localStorage.
let _canDelete  = !!(window._CAN_DELETE_DIAGNOSTICS);
let _deleteTarget = null;  // incidente pendiente de confirmación
let _page       = { offset: 0, limit: 50, total: 0, status: '' };
let _sseSource      = null;
let _deleteRunning  = false;   // guardia anti-doble-clic

// ─── Init ────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  initDarkMode();
  lucide.createIcons();

  _token = window._ADMIN_TOKEN || localStorage.getItem('adminToken') || '';

  await Promise.all([loadClients(), loadWhoami()]);

  document.getElementById('clientSelect').addEventListener('change', () => {
    _clientId = document.getElementById('clientSelect').value || null;
    _page.offset = 0;
    if (_clientId) { loadList(); startSSE(); }
  });
  document.getElementById('btnRefresh').addEventListener('click', () => loadList());
  document.getElementById('btnApplyFilter').addEventListener('click', () => {
    _page.status = document.getElementById('filterStatus').value;
    _page.offset = 0;
    loadList();
  });
  document.getElementById('btnBack').addEventListener('click', showList);
  document.getElementById('btnPrev').addEventListener('click', () => {
    _page.offset = Math.max(0, _page.offset - _page.limit);
    loadList();
  });
  document.getElementById('btnNext').addEventListener('click', () => {
    _page.offset = Math.min(_page.total - _page.limit, _page.offset + _page.limit);
    loadList();
  });
  // Botón eliminar en la vista de detalle
  document.getElementById('btnDelete').addEventListener('click', () => {
    if (_currentIncident) openDeleteModal(_currentIncident);
  });
  document.getElementById('btnDeleteCancel').addEventListener('click', closeDeleteModal);
  document.getElementById('btnDeleteConfirm').addEventListener('click', confirmDelete);

  document.querySelectorAll('[data-dtab]').forEach(btn => {
    btn.addEventListener('click', () => switchDetailTab(btn.dataset.dtab));
  });

  document.getElementById('btnDark').addEventListener('click', toggleDark);

  // Cerrar modal con Escape
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeDeleteModal();
  });
});

// ─── Auth ─────────────────────────────────────────────────────────────────────
function _headers() {
  const h = { 'Content-Type': 'application/json' };
  if (_token) h['Authorization'] = `Bearer ${_token}`;
  return h;
}

async function _api(method, path, body) {
  const opts = { method, headers: _headers() };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const resp = await fetch(API + path, opts);
  if (!resp.ok) {
    let detail = '';
    try { detail = (await resp.json()).detail || ''; } catch {}
    if (typeof detail === 'object') detail = detail.message || JSON.stringify(detail);
    throw Object.assign(new Error(detail || resp.statusText), { status: resp.status });
  }
  if (resp.status === 204) return null;
  return resp.json();
}

// ─── Whoami ───────────────────────────────────────────────────────────────────
async function loadWhoami() {
  // Complementa window._CAN_DELETE_DIAGNOSTICS cuando el token viene de localStorage.
  // Si falla (API deshabilitada o sin token), conserva el valor ya inyectado en el HTML.
  try {
    const me = await _api('GET', '/admin/whoami');
    _canDelete = !!me.can_delete_diagnostics;
  } catch {
    // No sobreescribir: el valor de _CAN_DELETE_DIAGNOSTICS inyectado es autoritativo.
  }
}

// ─── Clients ─────────────────────────────────────────────────────────────────
async function loadClients() {
  try {
    const clients = await _api('GET', '/admin/clients');
    const sel = document.getElementById('clientSelect');
    sel.innerHTML = '<option value="">— Selecciona cliente —</option>';
    (clients || []).forEach(c => {
      sel.insertAdjacentHTML('beforeend',
        `<option value="${c.id}">${escHtml(c.name)}</option>`);
    });
  } catch (e) {
    showAlert('No se pudieron cargar los clientes: ' + e.message, 'error');
  }
}

// ─── List ─────────────────────────────────────────────────────────────────────
async function loadList() {
  if (!_clientId) return;
  document.getElementById('listLoading').classList.remove('hidden');
  document.getElementById('incidentList').innerHTML = '';
  document.getElementById('listEmpty').classList.add('hidden');
  document.getElementById('paginationBar').classList.add('hidden');

  const qs = new URLSearchParams({ limit: _page.limit, offset: _page.offset });
  if (_page.status) qs.set('status', _page.status);

  try {
    const data = await _api('GET', `/clients/${_clientId}/diagnostics?${qs}`);
    _page.total = data.total || 0;
    renderList(data.items || []);
  } catch (e) {
    showAlert('Error al cargar diagnósticos: ' + e.message, 'error');
  } finally {
    document.getElementById('listLoading').classList.add('hidden');
  }
}

function renderList(items) {
  const container = document.getElementById('incidentList');
  if (!items.length) {
    document.getElementById('listEmpty').classList.remove('hidden');
    return;
  }

  items.forEach(inc => {
    const normalizedStatus = String(inc.status || '').trim().toLowerCase();
    const inProgress = IN_PROGRESS.has(normalizedStatus);
    const card = document.createElement('div');
    card.className = 'card p-4 hover:shadow-md transition-shadow';
    card.dataset.incidentId = inc.id;

    const deleteBtn = _canDelete
      ? inProgress
        ? `<button
             class="btn-secondary opacity-50 cursor-not-allowed text-xs flex items-center gap-1"
             disabled
             title="Este diagnóstico no puede eliminarse mientras se encuentra en ejecución."
           ><i data-lucide="trash-2" class="w-3.5 h-3.5"></i>Eliminar</button>`
        : `<button
             data-action="delete"
             class="btn-danger text-xs flex items-center gap-1"
             title="Eliminar diagnóstico"
           ><i data-lucide="trash-2" class="w-3.5 h-3.5"></i>Eliminar</button>`
      : '';

    const pdfBtn = inc.has_pdf
      ? `<a
           href="/api/clients/${_clientId}/diagnostics/${inc.id}/pdf"
           target="_blank"
           class="btn-secondary text-xs flex items-center gap-1"
           title="Abrir PDF"
           onclick="event.stopPropagation()"
         ><i data-lucide="file-text" class="w-3.5 h-3.5"></i>PDF</a>`
      : '';

    card.innerHTML = `
      <div class="flex flex-wrap items-center gap-3">
        <div class="shrink-0 w-9 h-9 rounded-xl grid place-items-center ${alarmTypeColor(inc.alarm_type)}">
          <i data-lucide="${alarmTypeIcon(inc.alarm_type)}" class="w-4 h-4"></i>
        </div>
        <div class="flex-1 min-w-0 cursor-pointer" data-action="detail">
          <div class="flex items-center gap-2 flex-wrap">
            <span class="font-semibold text-sm">${escHtml(inc.ecs_name || inc.ecs_instance_id || '—')}</span>
            ${statusBadge(inc.status)}
            ${inc.simulated
              ? '<span class="px-2 py-0.5 rounded text-xs font-medium bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300">SIMULADO</span>'
              : ''}
          </div>
          <p class="text-xs text-slate-500 mt-0.5">
            ${escHtml(inc.alarm_type.toUpperCase())} · ${escHtml(inc.region || '—')}
            ${inc.client_name ? '· ' + escHtml(inc.client_name) : ''}
            · ${formatDate(inc.created_at)}
          </p>
        </div>
        <div class="flex items-center gap-1.5 shrink-0">
          <button data-action="detail"
                  class="btn-secondary text-xs flex items-center gap-1"
                  title="Ver diagnóstico"
          ><i data-lucide="eye" class="w-3.5 h-3.5"></i>Ver</button>
          ${pdfBtn}
          ${deleteBtn}
        </div>
      </div>
      ${inc.possible_cause
        ? `<p class="text-xs text-slate-600 dark:text-slate-400 truncate mt-2">${escHtml(inc.possible_cause)}</p>`
        : ''}
    `;

    // Delegar eventos en el card
    card.addEventListener('click', e => {
      const action = e.target.closest('[data-action]')?.dataset.action;
      if (action === 'delete') {
        openDeleteModal(inc);
      } else {
        loadDetail(inc.id);
      }
    });

    container.appendChild(card);
  });

  lucide.createIcons();

  // Paginación
  if (_page.total > _page.limit) {
    const bar = document.getElementById('paginationBar');
    bar.classList.remove('hidden');
    document.getElementById('pageInfo').textContent =
      `${_page.offset + 1}–${Math.min(_page.offset + _page.limit, _page.total)} de ${_page.total}`;
    document.getElementById('btnPrev').disabled = _page.offset === 0;
    document.getElementById('btnNext').disabled = _page.offset + _page.limit >= _page.total;
  }
}

// ─── Detail ───────────────────────────────────────────────────────────────────
async function loadDetail(incidentId) {
  try {
    const inc = await _api('GET', `/clients/${_clientId}/diagnostics/${incidentId}`);
    _currentIncident = inc;
    renderDetail(inc);
    showDetail();
  } catch (e) {
    showAlert('Error al cargar el diagnóstico: ' + e.message, 'error');
  }
}

function renderDetail(inc) {
  document.getElementById('detailTitle').textContent =
    inc.ecs_name || inc.ecs_instance_id || `Incidente ${inc.id}`;
  document.getElementById('detailMeta').textContent =
    `${inc.alarm_type.toUpperCase()} · ${inc.region || '—'} · ${formatDate(inc.created_at)}`;
  document.getElementById('detailStatusBadge').innerHTML = statusBadge(inc.status);

  // Botón PDF
  const pdfBtn = document.getElementById('btnDownloadPdf');
  if (inc.has_pdf) {
    pdfBtn.href = `/api/clients/${_clientId}/diagnostics/${inc.id}/pdf`;
    pdfBtn.classList.remove('hidden');
  } else {
    pdfBtn.classList.add('hidden');
  }

  // Botón Eliminar en detalle (solo admin, y solo cuando no está en progreso)
  const deleteBtn = document.getElementById('btnDelete');
  if (_canDelete) {
    if (IN_PROGRESS.has(String(inc.status || '').trim().toLowerCase())) {
      deleteBtn.classList.remove('hidden');
      deleteBtn.disabled = true;
      deleteBtn.title = 'Este diagnóstico no puede eliminarse mientras se encuentra en ejecución.';
      deleteBtn.classList.add('opacity-50', 'cursor-not-allowed');
    } else {
      deleteBtn.classList.remove('hidden');
      deleteBtn.disabled = false;
      deleteBtn.title = 'Eliminar este diagnóstico';
      deleteBtn.classList.remove('opacity-50', 'cursor-not-allowed');
    }
  } else {
    deleteBtn.classList.add('hidden');
  }

  // Tab: Resumen
  document.getElementById('dtab-resumen').innerHTML = `
    ${inc.simulated ? `<div class="rounded-xl bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-800 p-4 flex items-center gap-2">
      <i data-lucide="flask-conical" class="w-4 h-4 text-amber-600 shrink-0"></i>
      <p class="text-sm text-amber-800 dark:text-amber-200 font-medium">Diagnóstico simulado — datos de prueba. No refleja el estado real del servidor.</p>
    </div>` : ''}
    <div class="grid grid-cols-2 md:grid-cols-3 gap-3">
      ${kv('Cliente', inc.client_name || '—')}
      ${kv('Cuenta Huawei', inc.account_name || '—')}
      ${kv('ECS', inc.ecs_name || inc.ecs_instance_id || '—')}
      ${kv('Instance ID', inc.ecs_instance_id || '—')}
      ${kv('IP', inc.ecs_ip || '—')}
      ${kv('Región', inc.region || '—')}
      ${kv('Project ID', inc.project_id_hw || '—')}
      ${kv('Enterprise Project', inc.enterprise_project_id || '—')}
      ${kv('Tipo alarma', inc.alarm_type || '—')}
      ${kv('Estado', inc.status)}
      ${kv('Confianza', inc.confidence || '—')}
      ${kv('Duración', inc.duration_ms ? `${inc.duration_ms} ms` : '—')}
    </div>
    ${inc.possible_cause ? `<div class="rounded-xl bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-800 p-4">
      <p class="text-sm font-semibold text-amber-800 dark:text-amber-200">Causa posible</p>
      <p class="text-sm mt-1">${escHtml(inc.possible_cause)}</p>
    </div>` : ''}
    ${inc.error_message_safe ? `<div class="rounded-xl bg-red-50 dark:bg-red-950/30 border border-red-200 dark:border-red-800 p-4">
      <p class="text-sm font-semibold text-red-700 dark:text-red-300">Error: ${escHtml(inc.error_kind || '')}</p>
      <p class="text-sm mt-1">${escHtml(inc.error_message_safe)}</p>
    </div>` : ''}
  `;

  // Tab: Alarma
  const r = inc.report_json || {};
  document.getElementById('dtab-alarma').innerHTML = `
    <div class="grid grid-cols-2 md:grid-cols-3 gap-3">
      ${kv('Disparada', formatDate(inc.alarm_fired_at))}
      ${kv('Métrica', inc.metric_name || '—')}
      ${kv('Umbral', inc.threshold != null ? inc.threshold : '—')}
      ${kv('Valor observado', inc.observed_value != null ? inc.observed_value : '—')}
      ${kv('Severidad', inc.severity != null ? inc.severity : '—')}
      ${kv('Proyecto HW', inc.project_id_hw || '—')}
    </div>
  `;

  // Tab: Diagnóstico
  const analysis = r.analysis || {};
  const findings = analysis.findings || [];
  const limits   = analysis.limitations || [];
  document.getElementById('dtab-diagnostico').innerHTML = `
    ${inc.possible_cause
      ? `<div class="rounded-xl bg-blue-50 dark:bg-blue-950/30 border border-blue-200 dark:border-blue-800 p-4">
           <p class="font-semibold text-sm">Causa posible (confianza: ${escHtml(inc.confidence || '—')})</p>
           <p class="text-sm mt-1">${escHtml(inc.possible_cause)}</p>
         </div>`
      : '<p class="text-sm text-slate-400">Sin análisis disponible aún.</p>'}
    ${findings.length
      ? `<div class="space-y-2">
           <p class="text-xs font-semibold text-slate-500 uppercase tracking-wide">Hallazgos</p>
           ${findings.map(f => `<div class="flex items-start gap-2 text-sm">
             <span class="shrink-0 mt-0.5 px-2 py-1 rounded text-xs font-mono bg-slate-100 dark:bg-slate-800">${escHtml(f.kind)}</span>
             <span>${escHtml(f.summary)}</span>
             <span class="ml-auto shrink-0 text-xs text-slate-400">${f.relevance}%</span>
           </div>`).join('')}
         </div>`
      : ''}
    ${limits.length
      ? `<div class="space-y-1">
           <p class="text-xs font-semibold text-slate-500 uppercase tracking-wide">Limitaciones</p>
           ${limits.map(l => `<p class="text-sm text-slate-500">• ${escHtml(l)}</p>`).join('')}
         </div>`
      : ''}
  `;

  // Tab: Evidencias
  const evidence = inc.evidence || [];
  document.getElementById('dtab-evidencias').innerHTML = evidence.length
    ? `<div class="space-y-2">${evidence.map(e => `
        <div class="card p-4">
          <div class="flex items-center gap-2 mb-2">
            <span class="px-2 py-1 rounded text-xs font-mono bg-slate-100 dark:bg-slate-800">${escHtml(e.kind)}</span>
            <span class="text-xs text-slate-400">relevancia ${e.relevance_score}%</span>
          </div>
          <p class="text-sm">${escHtml(e.summary_safe || '—')}</p>
        </div>`).join('')}
      </div>`
    : '<p class="text-sm text-slate-400">Sin evidencias registradas.</p>';

  // Tab: Comandos
  const commands = inc.commands || [];
  document.getElementById('dtab-comandos').innerHTML = commands.length
    ? `<div class="space-y-3">${commands.map(c => `
        <div class="rounded-xl border border-slate-200 dark:border-slate-800 overflow-hidden">
          <div class="flex items-center gap-2 px-4 py-3 bg-slate-50 dark:bg-slate-800/60 text-xs font-mono">
            <span class="font-bold">[${c.sequence}]</span>
            <span>${escHtml(c.command)}</span>
            <span class="text-slate-400 ml-auto">${c.exit_code != null ? `exit ${c.exit_code}` : ''} ${c.duration_ms ? `· ${c.duration_ms}ms` : ''}</span>
          </div>
          ${c.stdout_safe ? `<pre class="p-3 text-xs overflow-x-auto bg-white dark:bg-slate-900 max-h-40">${escHtml(c.stdout_safe)}</pre>` : ''}
          ${c.stderr_safe ? `<pre class="p-3 text-xs overflow-x-auto bg-red-50 dark:bg-red-950/20 text-red-700 dark:text-red-300 max-h-20">${escHtml(c.stderr_safe)}</pre>` : ''}
        </div>`).join('')}
      </div>`
    : '<p class="text-sm text-slate-400">Sin comandos ejecutados.</p>';

  switchDetailTab('resumen');
  lucide.createIcons();
}

function switchDetailTab(tab) {
  document.querySelectorAll('.detail-panel').forEach(p => p.classList.add('hidden'));
  document.querySelectorAll('[data-dtab]').forEach(b => b.classList.remove('tab-active'));
  document.getElementById(`dtab-${tab}`).classList.remove('hidden');
  document.querySelector(`[data-dtab="${tab}"]`).classList.add('tab-active');
}

// ─── Delete ───────────────────────────────────────────────────────────────────
// Debe coincidir con IN_PROGRESS_STATUSES en db/models.py.
const IN_PROGRESS = new Set([
  'alert_received', 'validating', 'pending_diagnosis',
  'connecting', 'analyzing', 'generating_report',
]);

function openDeleteModal(inc) {
  _deleteTarget = inc;
  document.getElementById('deleteReason').value = '';
  _resetDeleteBtn();

  // Rellenar info del incidente en el modal
  const infoEl = document.getElementById('deleteIncidentInfo');
  if (inc) {
    document.getElementById('di-ecs').textContent    = inc.ecs_name || inc.ecs_instance_id || '—';
    document.getElementById('di-client').textContent = inc.client_name || '—';
    document.getElementById('di-region').textContent  = inc.region || '—';
    document.getElementById('di-type').textContent    = inc.alarm_type || '—';
    document.getElementById('di-metric').textContent  = inc.metric_name || '—';
    document.getElementById('di-date').textContent    = formatDate(inc.alarm_fired_at || inc.created_at);
    infoEl.classList.remove('hidden');
  } else {
    infoEl.classList.add('hidden');
  }

  document.getElementById('deleteModal').classList.remove('hidden');
  document.getElementById('deleteReason').focus();
  lucide.createIcons();
}

function closeDeleteModal() {
  document.getElementById('deleteModal').classList.add('hidden');
  _deleteTarget = null;
  _resetDeleteBtn();
}

function _resetDeleteBtn() {
  const btn   = document.getElementById('btnDeleteConfirm');
  const label = document.getElementById('btnDeleteConfirmLabel');
  btn.disabled = false;
  if (label) label.textContent = 'Eliminar diagnóstico';
}

async function confirmDelete() {
  if (!_deleteTarget || !_clientId || _deleteRunning) return;

  const btn   = document.getElementById('btnDeleteConfirm');
  const label = document.getElementById('btnDeleteConfirmLabel');

  // Estado de carga — guardia global evita doble-clic
  _deleteRunning = true;
  btn.disabled = true;
  if (label) label.textContent = 'ELIMINANDO…';

  const inc    = _deleteTarget;
  const reason = document.getElementById('deleteReason').value.trim();
  const qs     = reason ? `?reason=${encodeURIComponent(reason)}` : '';

  try {
    await _api('DELETE', `/clients/${_clientId}/diagnostics/${inc.id}${qs}`);
    closeDeleteModal();

    // Quitar la tarjeta de la lista sin recargar toda la página
    const card = document.querySelector(`[data-incident-id="${inc.id}"]`);
    if (card) card.remove();
    _page.total = Math.max(0, _page.total - 1);

    // Actualizar contador en la paginación si estaba visible
    const pageInfo = document.getElementById('pageInfo');
    if (pageInfo && pageInfo.textContent) {
      const end = Math.min(_page.offset + _page.limit, _page.total);
      pageInfo.textContent =
        `${_page.offset + 1}–${end} de ${_page.total}`;
    }

    // Si el detalle estaba abierto para este incidente, volver a la lista
    if (_currentIncident && _currentIncident.id === inc.id) {
      _currentIncident = null;
      showList();
    }

    // Si la lista quedó vacía, mostrar aviso
    if (!document.querySelector('#incidentList [data-incident-id]')) {
      document.getElementById('listEmpty').classList.remove('hidden');
    }

    showAlert(
      'Diagnóstico eliminado correctamente. La ECS y la alarma de Cloud Eye no fueron modificadas.',
      'success',
    );
  } catch (e) {
    _deleteRunning = false;
    _resetDeleteBtn();
    closeDeleteModal();

    const msg = _deleteErrorMessage(e.status, e.message);
    showAlert(msg, 'error');
  } finally {
    _deleteRunning = false;
  }
}

function _deleteErrorMessage(status, fallback) {
  switch (status) {
    case 401: return 'Sesión no autorizada. Recarga la página e inicia sesión de nuevo.';
    case 403: return 'No tiene permisos para eliminar diagnósticos.';
    case 404: return 'El diagnóstico no existe o no pertenece al cliente seleccionado.';
    case 409: return 'El diagnóstico se encuentra en ejecución y no puede eliminarse.';
    case 500: return 'Error interno del servidor al intentar eliminar el diagnóstico.';
    default:  return `Error al eliminar: ${fallback || 'Error desconocido.'}`;
  }
}

// ─── SSE ──────────────────────────────────────────────────────────────────────
function startSSE() {
  if (_sseSource) { _sseSource.close(); _sseSource = null; }
  if (!_clientId) return;
  const url = `/api/clients/${_clientId}/diagnostics/stream` +
    (_token ? `?token=${encodeURIComponent(_token)}` : '');
  const es = new EventSource(url);
  es.onmessage = (evt) => {
    try {
      const data = JSON.parse(evt.data);
      if (data.id && data.status) {
        // Actualizar badge en la lista si la tarjeta está visible
        const card = document.querySelector(`[data-incident-id="${data.id}"]`);
        if (card) {
          const badge = card.querySelector('.status-badge');
          if (badge) badge.outerHTML = statusBadge(data.status);
        }
        // Si el detalle está abierto para este incidente, recargar
        if (_currentIncident && _currentIncident.id === data.id &&
            !document.getElementById('detailView').classList.contains('hidden')) {
          loadDetail(data.id);
        }
      }
    } catch {}
  };
  es.onerror = () => { es.close(); };
  _sseSource = es;
}

// ─── Nav helpers ─────────────────────────────────────────────────────────────
function showList() {
  document.getElementById('listView').classList.remove('hidden');
  document.getElementById('detailView').classList.add('hidden');
}

function showDetail() {
  document.getElementById('listView').classList.add('hidden');
  document.getElementById('detailView').classList.remove('hidden');
}

// ─── UI helpers ──────────────────────────────────────────────────────────────
function statusBadge(status) {
  const map = {
    alert_received:    ['badge-blue',   'Recibido'],
    validating:        ['badge-blue',   'Validando'],
    pending_diagnosis: ['badge-blue',   'En cola'],
    connecting:        ['badge-blue',   'Conectando'],
    analyzing:         ['badge-blue',   'Analizando'],
    generating_report: ['badge-blue',   'Generando informe'],
    report_available:  ['badge-green',  'Informe disponible'],
    no_server:         ['badge-yellow', 'Sin servidor'],
    failed:            ['badge-red',    'Error'],
    recovered:         ['badge-green',  'Recuperado'],
  };
  const [cls, label] = map[status] || ['badge-slate', status];
  return `<span class="badge ${cls} status-badge">${label}</span>`;
}

function alarmTypeIcon(t) {
  return { cpu: 'cpu', memory: 'memory-stick', disk: 'hard-drive', network: 'network' }[t] || 'activity';
}

function alarmTypeColor(t) {
  return {
    cpu:     'bg-orange-100 dark:bg-orange-900/30 text-orange-600',
    memory:  'bg-purple-100 dark:bg-purple-900/30 text-purple-600',
    disk:    'bg-yellow-100 dark:bg-yellow-900/30 text-yellow-600',
    network: 'bg-blue-100 dark:bg-blue-900/30 text-blue-600',
  }[t] || 'bg-slate-100 dark:bg-slate-800 text-slate-600';
}

function kv(label, value) {
  return `<div class="rounded-lg bg-slate-50 dark:bg-slate-800/60 px-3 py-2">
    <p class="text-xs text-slate-500">${escHtml(label)}</p>
    <p class="text-sm font-medium truncate">${escHtml(String(value ?? '—'))}</p>
  </div>`;
}

function escHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function formatDate(iso) {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleString('es', { dateStyle: 'short', timeStyle: 'short' });
  } catch { return iso; }
}

function showAlert(msg, type = 'info') {
  const clsMap = {
    success: 'bg-green-50 border-green-200 text-green-800 dark:bg-green-950/30 dark:border-green-800 dark:text-green-300',
    error:   'bg-red-50 border-red-200 text-red-800 dark:bg-red-950/30 dark:border-red-800 dark:text-red-300',
    info:    'bg-blue-50 border-blue-200 text-blue-800 dark:bg-blue-950/30 dark:border-blue-800 dark:text-blue-300',
  };
  const div = document.createElement('div');
  div.className = `rounded-xl border px-4 py-3 text-sm ${clsMap[type] || clsMap.info}`;
  div.textContent = msg;
  const container = document.getElementById('alerts');
  container.prepend(div);
  setTimeout(() => div.remove(), 7000);
}

// ─── Dark mode ────────────────────────────────────────────────────────────────
function initDarkMode() {
  const saved = localStorage.getItem('theme');
  if (saved === 'dark' || (!saved && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
    document.documentElement.classList.add('dark');
  }
}

function toggleDark() {
  const isDark = document.documentElement.classList.toggle('dark');
  localStorage.setItem('theme', isDark ? 'dark' : 'light');
}
