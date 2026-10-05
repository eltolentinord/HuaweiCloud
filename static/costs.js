// Análisis de costos - UI (sin datos simulados)
const $ = (id) => document.getElementById(id);
let ultimoAnalisis = null;
let consultando = false;
const charts = {};

// Tema
function aplicarTema(t){ if(t==='dark'){document.documentElement.classList.add('dark'); $('themeLabel').textContent='Claro';} else {document.documentElement.classList.remove('dark'); $('themeLabel').textContent='Oscuro';} }
(function(){ aplicarTema(localStorage.getItem('theme')==='dark' ? 'dark':'light'); })();
$('btnTheme').addEventListener('click', () => { const d=document.documentElement.classList.toggle('dark'); localStorage.setItem('theme', d?'dark':'light'); aplicarTema(d?'dark':'light'); });

$('btnToggleSk').addEventListener('click', () => { $('sk').type = $('sk').type==='password'?'text':'password'; });
const SECCIONES = ['seccionResumen', 'seccionHallazgos', 'seccionRecursos', 'seccionTablas', 'btnExport', 'btnExportPdf', 'btnPrintPdf'];
$('btnClear').addEventListener('click', () => { $('formCosts').reset(); SECCIONES.forEach(id => $(id).classList.add('hidden')); $('alertBox').classList.add('hidden'); $('avisoPreliminares').classList.add('hidden'); ultimoAnalisis=null; });
$('alertClose').addEventListener('click', () => $('alertBox').classList.add('hidden'));

function mostrarAlerta(msg, meta){ $('alertMsg').textContent = msg || 'Error'; $('alertMeta').textContent = meta || ''; $('alertBox').classList.remove('hidden'); if(window.lucide) lucide.createIcons(); }

function dinero(v, cur){ if(v===null||v===undefined) return '—'; return `${v.toFixed(2)} ${cur}`; }
function esc(v){ return String(v).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])); }
// Celdas de texto: siempre escapadas (los nombres vienen de Huawei Cloud).
function filaTabla(celdas){ return `<tr class="border-t border-slate-100 dark:border-slate-800">${celdas.map(c=>`<td class="px-3 py-2">${c===null||c===undefined?'—':esc(c)}</td>`).join('')}</tr>`; }
// Celdas ya construidas con esc(): [html, alineación derecha?]
function filaHtml(celdas){ return `<tr class="border-t border-slate-100 dark:border-slate-800">${celdas.map(([h, der])=>`<td class="px-3 py-2${der?' text-right tabular-nums whitespace-nowrap':''}">${h}</td>`).join('')}</tr>`; }
function servicioTxt(f){ if(!f) return '—'; const s=f.servicio_sigla, n=f.servicio_nombre; return s && n && s!==n ? `${s} · ${n}` : (s || n || f.product || '—'); }
function regionTxt(f){ if(!f) return '—'; const n=f.region_nombre; return n && n!==f.region ? `${n} (${f.region})` : (f.region || '—'); }
function difHtml(v, cur){ const c = v>0 ? 'text-red-600 dark:text-red-400' : v<0 ? 'text-emerald-600 dark:text-emerald-400' : ''; return `<span class="${c}">${v>0?'+':''}${esc(dinero(v,cur))}</span>`; }
function recursoHtml(f){ const id = f.resource_id && f.resource_id!==f.resource_name ? `<div class="text-[11px] text-slate-400 font-mono break-all">${esc(f.resource_id)}</div>` : ''; return `<div class="font-medium break-words">${esc(f.resource_name||'Sin nombre')}</div>${id}`; }
function buscar(lista, clave, valor){ return (lista||[]).find(f => f[clave]===valor); }

$('formCosts').addEventListener('submit', async (e) => {
  e.preventDefault();
  if (consultando) return;
  consultando = true;
  $('btnCompare').disabled = true;
  $('alertBox').classList.add('hidden');
  try {
    const payload = {
      ak: $('ak').value.trim(), sk: $('sk').value.trim(),
      month_a: $('month_a').value, month_b: $('month_b').value,
      cost_type: $('cost_type').value, amount_type: $('amount_type').value,
    };
    const res = await fetch('/api/costs/compare', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) });
    const data = await res.json();
    if (data.error) {
      const meta = [];
      if (data.http_status !== undefined && data.http_status !== null) meta.push(`HTTP ${data.http_status}`);
      if (data.request_id) meta.push(`Request ID: ${data.request_id}`);
      if (data.error_code) meta.push(`Código: ${data.error_code}`);
      mostrarAlerta(data.message, meta.join(' · '));
      return;
    }
    ultimoAnalisis = data;
    $('avisoPreliminares').classList.toggle('hidden', !data.datos_preliminares);
    $('extraccion').textContent = `Extracción: ${data.extraction_time}`;
    renderResumen(data);
    renderTablas(data);
    renderGraficos(data);
    $('seccionResumen').classList.remove('hidden');
    $('seccionTablas').classList.remove('hidden');
    $('btnExport').classList.remove('hidden');
    $('btnExportPdf').classList.remove('hidden');
    $('btnPrintPdf').classList.remove('hidden');
    renderHallazgos(data);
    renderRecursos(data);
  } catch (err) {
    mostrarAlerta('No se pudo completar la comparación.', String(err));
  } finally {
    consultando = false;
    $('btnCompare').disabled = false;
  }
});

function renderResumen(data){
  const m = (data.analysis.monedas || [])[0];
  if (!m) { $('sTotalA').textContent='—'; return; }
  const cur = m.currency;
  $('sTotalA').textContent = dinero(m.total_mes_a, cur);
  $('sTotalB').textContent = dinero(m.total_mes_b, cur);
  $('sDiff').textContent = dinero(m.diferencia, cur);
  $('sPct').textContent = m.variacion_txt;
  $('sRegion').textContent = m.region_mayor_gasto ? regionTxt(buscar(m.por_region,'region',m.region_mayor_gasto) || {region:m.region_mayor_gasto}) : '—';
  const prod = code => code ? servicioTxt(buscar(m.por_producto,'product',code) || {product:code}) : '—';
  $('sProd').textContent = prod(m.producto_mas_costoso);
  $('sUp').textContent = prod(m.producto_mas_aumento);
  $('sDown').textContent = prod(m.producto_mas_reduccion);
  const rec = ((data.analysis.recursos||{}).monedas||[]).find(r => r.currency===cur);
  const top = rec && rec.top_mes_b[0];
  $('sTopRes').textContent = top ? `${top.resource_name} · ${top.servicio_sigla} · ${top.region_nombre} · ${dinero(top.mes_b, cur)}`
    : (data.recursos_error ? 'Detalle por recurso no disponible' : 'Sin consumo por recurso');
}

function renderHallazgos(data){
  const h = data.analysis.hallazgos || {};
  const lista = (id, items, vacio) => { $(id).innerHTML = (items && items.length ? items : [vacio]).map(t => `<li>${esc(t)}</li>`).join(''); };
  lista('listaHallazgos', h.hallazgos, 'Sin hallazgos: no hay consumo en los meses consultados.');
  lista('listaRevisar', h.revisar, 'Nada que revisar según los datos.');
  $('seccionHallazgos').classList.remove('hidden');
}

function renderRecursos(data){
  $('seccionRecursos').classList.remove('hidden');
  const err = data.recursos_error;
  $('recursosAviso').classList.toggle('hidden', !err);
  $('recursosTablas').classList.toggle('hidden', !!err);
  if (err) {
    const meta = [err.http_status ? `HTTP ${err.http_status}` : '', err.error_code ? `Código: ${err.error_code}` : '', err.request_id ? `Request ID: ${err.request_id}` : ''].filter(Boolean).join(' · ');
    $('recursosAviso').innerHTML = `<p class="font-semibold">Detalle por recurso no disponible</p><p class="mt-1">${esc(err.mensaje||'')}</p>${meta?`<p class="mt-1 text-xs opacity-80">${esc(meta)}</p>`:''}`;
    return;
  }
  const cur = ((data.analysis.monedas||[])[0]||{}).currency;
  const r = ((data.analysis.recursos||{}).monedas||[]).find(x => x.currency===cur) || {top_mes_b:[],mayores_aumentos:[],mayores_reducciones:[],nuevos:[],eliminados:[],recursos_mes_a:0,recursos_mes_b:0};
  $('recursosResumen').textContent = `Recursos facturados: ${r.recursos_mes_a} en ${data.month_a} y ${r.recursos_mes_b} en ${data.month_b}, según los registros de facturación por recurso de BSS.`;
  const vacia = (n, txt) => `<tr><td colspan="${n}" class="px-3 py-3 text-slate-500">${txt}</td></tr>`;
  const base = f => [[recursoHtml(f)], [esc(servicioTxt(f))], [esc(f.region_nombre||f.region||'—')]];
  $('tTopRecursos').innerHTML = r.top_mes_b.map(f => filaHtml([...base(f), [esc(f.spec||f.tipo||'—')], [esc(dinero(f.mes_a,cur)),1], [`<strong>${esc(dinero(f.mes_b,cur))}</strong>`,1], [difHtml(f.diferencia,cur),1]])).join('') || vacia(7,'Sin consumo por recurso en el Mes B.');
  const cmp = f => filaHtml([...base(f), [esc(dinero(f.mes_a,cur)),1], [esc(dinero(f.mes_b,cur)),1], [difHtml(f.diferencia,cur),1]]);
  $('tRecAumentos').innerHTML = r.mayores_aumentos.map(cmp).join('') || vacia(6,'Ningún recurso aumentó.');
  $('tRecReducciones').innerHTML = r.mayores_reducciones.map(cmp).join('') || vacia(6,'Ningún recurso disminuyó.');
  $('tRecNuevos').innerHTML = r.nuevos.map(f => filaHtml([...base(f), [esc(dinero(f.mes_b,cur)),1]])).join('') || vacia(4,'Ninguno.');
  $('tRecEliminados').innerHTML = r.eliminados.map(f => filaHtml([...base(f), [esc(dinero(f.mes_a,cur)),1]])).join('') || vacia(4,'Ninguno.');
}

function renderTablas(data){
  const m = (data.analysis.monedas || [])[0] || {por_producto:[],por_region:[],por_region_producto:[],servicios_nuevos:[],servicios_eliminados:[]};
  const cur = (data.analysis.monedas||[])[0]?.currency || '';
  $('tProducto').innerHTML = m.por_producto.map(f => filaTabla([servicioTxt(f), dinero(f.mes_a,cur), dinero(f.mes_b,cur), dinero(f.diferencia,cur), f.variacion_txt])).join('') || filaTabla(['Sin datos','','','','']);
  $('tRegion').innerHTML = m.por_region.map(f => filaTabla([regionTxt(f), dinero(f.mes_a,cur), dinero(f.mes_b,cur), dinero(f.diferencia,cur), f.variacion_txt])).join('') || filaTabla(['Sin datos','','','','']);
  $('tRegProd').innerHTML = m.por_region_producto.map(f => filaTabla([regionTxt(f), servicioTxt(f), dinero(f.mes_a,cur), dinero(f.mes_b,cur), dinero(f.diferencia,cur), f.variacion_txt])).join('') || filaTabla(['Sin datos','','','','','']);
  $('tNuevos').innerHTML = m.servicios_nuevos.map(f => filaTabla([regionTxt(buscar(m.por_region,'region',f.region)||f), servicioTxt(buscar(m.por_producto,'product',f.product)||f), dinero(f.mes_a,cur), dinero(f.mes_b,cur)])).join('') || filaTabla(['Sin servicios nuevos','','','']);
  $('tEliminados').innerHTML = m.servicios_eliminados.map(f => filaTabla([regionTxt(buscar(m.por_region,'region',f.region)||f), servicioTxt(buscar(m.por_producto,'product',f.product)||f), dinero(f.mes_a,cur), dinero(f.mes_b,cur)])).join('') || filaTabla(['Sin servicios eliminados','','','']);
}

function destruirChart(id){ if (charts[id]) { charts[id].destroy(); delete charts[id]; } }

function renderGraficos(data){
  const m = (data.analysis.monedas || [])[0];
  if (!m) return;
  destruirChart('chProduct'); destruirChart('chRegion'); destruirChart('chDonut'); destruirChart('chTrend');
  const productos = m.por_producto.slice(0, 10);
  charts.chProduct = new Chart($('chProduct'), { type:'bar', data:{ labels: productos.map(p=>p.servicio_sigla||p.product), datasets:[ {label:`Mes A`, data: productos.map(p=>p.mes_a), backgroundColor:'#93c5fd'}, {label:`Mes B`, data: productos.map(p=>p.mes_b), backgroundColor:'#2563eb'} ] }, options:{responsive:true} });
  charts.chRegion = new Chart($('chRegion'), { type:'bar', data:{ labels: m.por_region.map(r=>r.region_nombre||r.region), datasets:[{label:'Mes B', data: m.por_region.map(r=>r.mes_b), backgroundColor:'#38bdf8'}] }, options:{responsive:true} });
  const top = m.por_producto.slice(0, 8);
  charts.chDonut = new Chart($('chDonut'), { type:'doughnut', data:{ labels: top.map(p=>p.servicio_sigla||p.product), datasets:[{data: top.map(p=>p.mes_b), backgroundColor:['#2563eb','#3b82f6','#60a5fa','#93c5fd','#1d4ed8','#0ea5e9','#38bdf8','#7dd3fc']}] }, options:{responsive:true} });
  charts.chTrend = new Chart($('chTrend'), { type:'line', data:{ labels: m.tendencia_mensual.map(t=>t.mes), datasets:[{label:'Total', data: m.tendencia_mensual.map(t=>t.total), borderColor:'#2563eb', tension:0.3}] }, options:{responsive:true} });
}

$('btnExport').addEventListener('click', async () => {
  if (!ultimoAnalisis) return;
  const res = await fetch('/api/costs/export', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ analysis: ultimoAnalisis.analysis, month_a: ultimoAnalisis.month_a, month_b: ultimoAnalisis.month_b, errores: ultimoAnalisis.errores || [] }) });
  if (!res.ok) { mostrarAlerta('No se pudo exportar el Excel.', `HTTP ${res.status}`); return; }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `Huawei_Costos_Comparativo_${Date.now()}.xlsx`;
  a.click();
  URL.revokeObjectURL(url);
});

async function descargar(url, cuerpo, nombre, errorTxt){
  const res = await fetch(url, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(cuerpo) });
  if (!res.ok) { mostrarAlerta(errorTxt, `HTTP ${res.status}`); return; }
  const blob = await res.blob();
  const enlace = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = enlace; a.download = nombre; a.click();
  URL.revokeObjectURL(enlace);
}

function cuerpoPdf(d){
  return { analysis: d.analysis, month_a: d.month_a, month_b: d.month_b, errores: d.errores || [],
    extraction_time: d.extraction_time, datos_preliminares: !!d.datos_preliminares, recursos_error: d.recursos_error || null };
}

$('btnExportPdf').addEventListener('click', async () => {
  if (!ultimoAnalisis) return;
  const d = ultimoAnalisis;
  $('btnExportPdf').disabled = true;
  try {
    await descargar('/api/costs/export/pdf', cuerpoPdf(d), `Huawei_Costos_${d.month_a}_vs_${d.month_b}.pdf`, 'No se pudo exportar el PDF.');
  } finally { $('btnExportPdf').disabled = false; }
});

// Imprime el mismo PDF: se carga en un iframe oculto y se abre el diálogo de impresión.
// Si el navegador no permite imprimir el iframe, el PDF se abre en una pestaña nueva.
$('btnPrintPdf').addEventListener('click', async () => {
  if (!ultimoAnalisis) return;
  $('btnPrintPdf').disabled = true;
  try {
    const res = await fetch('/api/costs/export/pdf', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(cuerpoPdf(ultimoAnalisis)) });
    if (!res.ok) { mostrarAlerta('No se pudo generar el PDF para imprimir.', `HTTP ${res.status}`); return; }
    const url = URL.createObjectURL(new Blob([await res.blob()], { type: 'application/pdf' }));
    const marco = document.createElement('iframe');
    marco.style.cssText = 'position:fixed;right:0;bottom:0;width:0;height:0;border:0';
    marco.src = url;
    marco.onload = () => {
      try { marco.contentWindow.focus(); marco.contentWindow.print(); }
      catch (e) { window.open(url, '_blank'); }
      setTimeout(() => { marco.remove(); URL.revokeObjectURL(url); }, 60000);
    };
    document.body.appendChild(marco);
  } finally { $('btnPrintPdf').disabled = false; }
});

if (window.lucide) lucide.createIcons();
