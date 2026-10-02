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
$('btnClear').addEventListener('click', () => { $('formCosts').reset(); $('seccionResumen').classList.add('hidden'); $('seccionTablas').classList.add('hidden'); $('btnExport').classList.add('hidden'); $('alertBox').classList.add('hidden'); $('avisoPreliminares').classList.add('hidden'); ultimoAnalisis=null; });
$('alertClose').addEventListener('click', () => $('alertBox').classList.add('hidden'));

function mostrarAlerta(msg, meta){ $('alertMsg').textContent = msg || 'Error'; $('alertMeta').textContent = meta || ''; $('alertBox').classList.remove('hidden'); if(window.lucide) lucide.createIcons(); }

function dinero(v, cur){ if(v===null||v===undefined) return '—'; return `${v.toFixed(2)} ${cur}`; }
function filaTabla(celdas){ return `<tr class="border-t border-slate-100 dark:border-slate-800">${celdas.map(c=>`<td class="px-3 py-2">${c===null||c===undefined?'—':c}</td>`).join('')}</tr>`; }

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
  $('sRegion').textContent = m.region_mayor_gasto || '—';
  $('sProd').textContent = m.producto_mas_costoso || '—';
  $('sUp').textContent = m.producto_mas_aumento || '—';
  $('sDown').textContent = m.producto_mas_reduccion || '—';
}

function renderTablas(data){
  const m = (data.analysis.monedas || [])[0] || {por_producto:[],por_region:[],por_region_producto:[],servicios_nuevos:[],servicios_eliminados:[]};
  const cur = (data.analysis.monedas||[])[0]?.currency || '';
  $('tProducto').innerHTML = m.por_producto.map(f => filaTabla([f.product, dinero(f.mes_a,cur), dinero(f.mes_b,cur), dinero(f.diferencia,cur), f.variacion_txt])).join('') || filaTabla(['Sin datos','','','','']);
  $('tRegion').innerHTML = m.por_region.map(f => filaTabla([f.region, dinero(f.mes_a,cur), dinero(f.mes_b,cur), dinero(f.diferencia,cur), f.variacion_txt])).join('') || filaTabla(['Sin datos','','','','']);
  $('tRegProd').innerHTML = m.por_region_producto.map(f => filaTabla([f.region, f.product, dinero(f.mes_a,cur), dinero(f.mes_b,cur), dinero(f.diferencia,cur), f.variacion_txt])).join('') || filaTabla(['Sin datos','','','','','']);
  $('tNuevos').innerHTML = m.servicios_nuevos.map(f => filaTabla([f.region, f.product, dinero(f.mes_a,cur), dinero(f.mes_b,cur)])).join('') || filaTabla(['Sin servicios nuevos','','','']);
  $('tEliminados').innerHTML = m.servicios_eliminados.map(f => filaTabla([f.region, f.product, dinero(f.mes_a,cur), dinero(f.mes_b,cur)])).join('') || filaTabla(['Sin servicios eliminados','','','']);
}

function destruirChart(id){ if (charts[id]) { charts[id].destroy(); delete charts[id]; } }

function renderGraficos(data){
  const m = (data.analysis.monedas || [])[0];
  if (!m) return;
  destruirChart('chProduct'); destruirChart('chRegion'); destruirChart('chDonut'); destruirChart('chTrend');
  const productos = m.por_producto.slice(0, 10);
  charts.chProduct = new Chart($('chProduct'), { type:'bar', data:{ labels: productos.map(p=>p.product), datasets:[ {label:`Mes A`, data: productos.map(p=>p.mes_a), backgroundColor:'#93c5fd'}, {label:`Mes B`, data: productos.map(p=>p.mes_b), backgroundColor:'#2563eb'} ] }, options:{responsive:true} });
  charts.chRegion = new Chart($('chRegion'), { type:'bar', data:{ labels: m.por_region.map(r=>r.region), datasets:[{label:'Mes B', data: m.por_region.map(r=>r.mes_b), backgroundColor:'#38bdf8'}] }, options:{responsive:true} });
  const top = m.por_producto.slice(0, 8);
  charts.chDonut = new Chart($('chDonut'), { type:'doughnut', data:{ labels: top.map(p=>p.product), datasets:[{data: top.map(p=>p.mes_b), backgroundColor:['#2563eb','#3b82f6','#60a5fa','#93c5fd','#1d4ed8','#0ea5e9','#38bdf8','#7dd3fc']}] }, options:{responsive:true} });
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

if (window.lucide) lucide.createIcons();
