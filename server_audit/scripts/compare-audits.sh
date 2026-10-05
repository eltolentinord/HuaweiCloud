#!/usr/bin/env bash
#
# compare-audits.sh
#
# Toma varios reportes JSON generados por "server-audit.sh -j archivo.json"
# (uno por servidor) y arma un reporte HTML consolidado que compara el
# puntaje de Hardening, Actualizaciones y el puntaje general entre todos
# los servidores, ordenado de mejor a peor, para facilitar el levantamiento
# de infraestructura entre varios servidores.
#
# Este script NO esta pensado para correr en cada servidor (para eso esta
# server-audit.sh); se corre una sola vez en la maquina donde se recolectan
# los .json de todos los servidores. Requiere python3 (para parsear JSON de
# forma robusta).
#
# Uso:
#   ./compare-audits.sh -o comparativo.html servidor1.json servidor2.json ...
#   ./compare-audits.sh -o comparativo.html /ruta/a/reportes/*.json
#
set -uo pipefail

OUT_FILE="comparativo-servidores.html"
while getopts ":o:" opt; do
    case "$opt" in
        o) OUT_FILE="$OPTARG" ;;
        *) echo "Uso: $0 [-o comparativo.html] archivo1.json archivo2.json ..."; exit 1 ;;
    esac
done
shift $((OPTIND - 1))

if [ "$#" -eq 0 ]; then
    echo "Error: debes indicar al menos un archivo JSON generado por server-audit.sh -j" >&2
    echo "Uso: $0 [-o comparativo.html] archivo1.json archivo2.json ..." >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: este script requiere python3 para parsear los JSON de entrada." >&2
    exit 1
fi

for f in "$@"; do
    if [ ! -f "$f" ]; then
        echo "Error: no existe el archivo '$f'" >&2
        exit 1
    fi
done

python3 - "$OUT_FILE" "$@" <<'PYEOF'
import json
import sys
import html
from datetime import datetime

out_file = sys.argv[1]
input_files = sys.argv[2:]

servers = []
errors = []
for path in input_files:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        data["_source_file"] = path
        servers.append(data)
    except Exception as exc:
        errors.append((path, str(exc)))

# Ordenar de mejor a peor puntaje general
servers.sort(key=lambda s: s.get("overall_score", 0), reverse=True)


def esc(value):
    return html.escape(str(value), quote=True)


def bar_color(pct):
    if pct >= 80:
        return "#16a34a"
    if pct >= 50:
        return "#f59e0b"
    return "#dc2626"


def badge_class(status):
    return {
        "OK": "badge-ok",
        "WARN": "badge-warn",
        "FAIL": "badge-fail",
    }.get(status, "badge-info")


now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

rows_summary = []
for s in servers:
    host = esc(s.get("hostname", "desconocido"))
    os_name = esc(s.get("os", "desconocido"))
    overall = s.get("overall_score", 0)
    hd = s.get("hardening", {})
    up = s.get("updates", {})
    gen = esc(s.get("generated_at", "-"))
    rows_summary.append(f"""
<tr>
  <td>{host}</td>
  <td>{os_name}</td>
  <td>{gen}</td>
  <td>
    <div class="bar-row"><div class="bar-track"><div class="bar-fill" style="width:{overall}%;background:{bar_color(overall)}"></div></div><span>{overall}%</span></div>
  </td>
  <td>
    <div class="bar-row"><div class="bar-track"><div class="bar-fill" style="width:{hd.get('pct', 0)}%;background:{bar_color(hd.get('pct', 0))}"></div></div><span>{hd.get('got', 0)}/{hd.get('max', 0)}</span></div>
  </td>
  <td>
    <div class="bar-row"><div class="bar-track"><div class="bar-fill" style="width:{up.get('pct', 0)}%;background:{bar_color(up.get('pct', 0))}"></div></div><span>{up.get('got', 0)}/{up.get('max', 0)}</span></div>
  </td>
</tr>""")

# Tabla detallada: una fila por servidor + control, agrupada por control (id)
all_check_ids = []
seen = set()
for s in servers:
    for c in s.get("checks", []):
        if c["id"] not in seen:
            seen.add(c["id"])
            all_check_ids.append((c["id"], c["category"], c["description"]))

detail_rows = []
for cid, cat, desc in all_check_ids:
    cells = []
    for s in servers:
        match = next((c for c in s.get("checks", []) if c["id"] == cid), None)
        if match:
            cells.append(f'<td><span class="badge {badge_class(match["status"])}">{esc(match["status"])}</span> {match["points"]}/{match["max"]}</td>')
        else:
            cells.append('<td>-</td>')
    detail_rows.append(f'<tr><td>{esc(cat)}</td><td>{esc(desc)}</td>' + "".join(cells) + '</tr>')

server_headers = "".join(f'<th>{esc(s.get("hostname", "-"))}</th>' for s in servers)

errors_html = ""
if errors:
    items = "".join(f"<li>{esc(p)}: {esc(e)}</li>" for p, e in errors)
    errors_html = f'<section class="panel"><h2>Archivos con error</h2><ul>{items}</ul></section>'

html_out = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Comparativo de Auditorias - Solvex Dominicana</title>
<style>
  :root {{
    --ok:#16a34a; --warn:#f59e0b; --fail:#dc2626; --info:#64748b;
    --bg:#0f172a; --panel:#1e293b; --text:#e2e8f0; --muted:#94a3b8;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 2rem; background: var(--bg); color: var(--text);
    font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif;
  }}
  .report-header {{
    display: flex; align-items: center; justify-content: space-between; gap: 1.25rem;
    border-bottom: 2px solid #334155; padding-bottom: 1.25rem; margin-bottom: 1.5rem;
  }}
  .report-header h1 {{ margin: 0; font-size: 1.6rem; }}
  .report-header p {{ margin: 0.25rem 0 0; color: var(--muted); font-size: 0.95rem; }}
  .logo-wordmark {{
    display: flex; flex-direction: column; align-items: flex-end; line-height: 1;
    flex-shrink: 0; text-align: right;
  }}
  .logo-wordmark .logo-main {{
    font-size: 1.5rem; font-weight: 800; letter-spacing: 0.06em;
    color: #38bdf8; white-space: nowrap;
  }}
  .logo-wordmark .logo-main span {{ color: #e2e8f0; margin-left: 0.35em; }}
  .logo-wordmark .logo-sub {{
    margin-top: 0.3rem; font-size: 0.7rem; font-weight: 600; letter-spacing: 0.25em;
    color: var(--muted); text-transform: uppercase;
  }}
  .panel {{
    background: var(--panel); border-radius: 12px; padding: 1.5rem;
    margin-bottom: 1.5rem; border: 1px solid #334155;
  }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  .table-wrap {{ overflow-x: auto; }}
  th, td {{ text-align: left; padding: 0.6rem 0.7rem; border-bottom: 1px solid #334155; white-space: nowrap; }}
  th {{ color: var(--muted); font-weight: 600; text-transform: uppercase; font-size: 0.7rem; }}
  .bar-row {{ display: flex; align-items: center; gap: 0.5rem; min-width: 160px; }}
  .bar-track {{ flex: 1; height: 12px; background: #334155; border-radius: 6px; overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 6px; }}
  .bar-row span {{ width: 55px; text-align: right; color: var(--muted); font-size: 0.8rem; flex-shrink: 0; }}
  .badge {{
    display: inline-block; padding: 0.1rem 0.5rem; border-radius: 999px;
    font-size: 0.7rem; font-weight: 700; color: #0f172a;
  }}
  .badge-ok {{ background: var(--ok); }}
  .badge-warn {{ background: var(--warn); }}
  .badge-fail {{ background: var(--fail); color: #fff; }}
  .badge-info {{ background: var(--info); color: #fff; }}
  footer {{ color: var(--muted); font-size: 0.8rem; text-align: center; margin-top: 2rem; }}
</style>
</head>
<body>

<header class="report-header">
  <div>
    <h1>Comparativo de Auditorias de Servidores</h1>
    <p>Servidores comparados: <strong>{len(servers)}</strong> &middot; Generado: {now_str}</p>
  </div>
  <div class="logo-wordmark" aria-label="Solvex Dominicana">
    <div class="logo-main">SOLVEX<span>DOMINICANA</span></div>
    <div class="logo-sub">Auditoria de Infraestructura</div>
  </div>
</header>

<section class="panel">
<h2>Resumen por Servidor</h2>
<div class="table-wrap">
<table>
<thead><tr><th>Host</th><th>SO</th><th>Generado</th><th>Puntaje General</th><th>Hardening</th><th>Actualizaciones</th></tr></thead>
<tbody>
{"".join(rows_summary)}
</tbody>
</table>
</div>
</section>

<section class="panel">
<h2>Detalle de Controles por Servidor</h2>
<div class="table-wrap">
<table>
<thead><tr><th>Categoria</th><th>Control</th>{server_headers}</tr></thead>
<tbody>
{"".join(detail_rows)}
</tbody>
</table>
</div>
</section>

{errors_html}

<footer>Generado por compare-audits.sh &middot; {now_str}</footer>
</body>
</html>
"""

with open(out_file, "w", encoding="utf-8") as fh:
    fh.write(html_out)

print(f"\nReporte comparativo generado en: {out_file}")
print(f"Servidores procesados: {len(servers)}")
if errors:
    print(f"Archivos con error: {len(errors)}")
PYEOF
