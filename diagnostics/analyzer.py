# coding: utf-8
"""Motor de análisis de causa raíz (heurístico, read-only).

Toma los outputs de los comandos SSH y produce:
  - possible_cause: texto explicativo para el operador
  - confidence: low | medium | high
  - findings: lista de hallazgos estructurados
  - limitations: advertencias sobre lo que NO se pudo verificar

IMPORTANTE: Solo reporta hechos observados. Las hipótesis se etiquetan
explícitamente como "posible". Nunca toma acciones correctivas.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from diagnostics.ssh import CommandResult


def analyze(alarm_type: str, outputs: Dict[str, CommandResult]) -> Dict[str, Any]:
    """Analiza los resultados de los comandos y devuelve un informe estructurado."""
    findings: List[Dict[str, Any]] = []
    limitations: List[str] = []

    if alarm_type == "cpu":
        _analyze_cpu(outputs, findings, limitations)
    elif alarm_type == "memory":
        _analyze_memory(outputs, findings, limitations)
    elif alarm_type == "disk":
        _analyze_disk(outputs, findings, limitations)
    elif alarm_type == "network":
        _analyze_network(outputs, findings, limitations)
    else:
        _analyze_cpu(outputs, findings, limitations)
        _analyze_memory(outputs, findings, limitations)
        _analyze_disk(outputs, findings, limitations)

    _analyze_services(outputs, findings, limitations)
    _analyze_system(outputs, findings, limitations)

    possible_cause, confidence = _summarize(alarm_type, findings)

    return {
        "alarm_type": alarm_type,
        "possible_cause": possible_cause,
        "confidence": confidence,
        "findings": findings,
        "limitations": limitations,
    }


# ---------------------------------------------------------------------------
# Analizadores por tipo
# ---------------------------------------------------------------------------

def _analyze_cpu(outputs: Dict, findings: List, limitations: List) -> None:
    # top snapshot: buscar procesos con CPU > 50%
    top_out = _stdout(outputs, "top_snapshot") or _stdout(outputs, "ps_cpu")
    if top_out:
        high_cpu_procs = []
        for line in top_out.splitlines()[1:]:
            parts = line.split()
            # La primera columna debe ser un PID numérico (evita parsear líneas de cabecera)
            if len(parts) >= 9 and parts[0].isdigit():
                try:
                    cpu = float(parts[8])
                    if cpu > 50:
                        high_cpu_procs.append({"pid": parts[0], "process": parts[-1], "cpu_pct": cpu})
                except (ValueError, IndexError):
                    pass
        if high_cpu_procs:
            findings.append({
                "kind": "cpu",
                "summary": f"Proceso(s) con CPU > 50%: {', '.join(p['process'] for p in high_cpu_procs[:3])}",
                "data": high_cpu_procs[:10],
                "relevance": 90,
            })

    # loadavg
    loadavg_out = _stdout(outputs, "loadavg")
    if loadavg_out:
        parts = loadavg_out.strip().split()
        if parts:
            try:
                load1 = float(parts[0])
                if load1 > 4:
                    findings.append({
                        "kind": "cpu",
                        "summary": f"Carga del sistema elevada (load1={load1:.2f}). Posible saturación de CPU.",
                        "data": {"load1": load1, "load5": _safe_float(parts, 1), "load15": _safe_float(parts, 2)},
                        "relevance": 85,
                    })
            except (ValueError, IndexError):
                pass
    else:
        limitations.append("No se pudo leer /proc/loadavg")


def _analyze_memory(outputs: Dict, findings: List, limitations: List) -> None:
    free_out = _stdout(outputs, "free")
    if free_out:
        for line in free_out.splitlines():
            if line.lower().startswith("mem:"):
                parts = line.split()
                if len(parts) >= 3:
                    # Intentar parsear valores en bytes o con sufijo
                    total = _parse_mem(parts[1])
                    used = _parse_mem(parts[2])
                    if total and used and total > 0:
                        pct = used / total * 100
                        if pct > 85:
                            findings.append({
                                "kind": "memory",
                                "summary": f"Memoria RAM en uso al {pct:.1f}%.",
                                "data": {"total": parts[1], "used": parts[2]},
                                "relevance": 85,
                            })
    else:
        limitations.append("No se pudo leer el uso de memoria (free)")


def _analyze_disk(outputs: Dict, findings: List, limitations: List) -> None:
    df_out = _stdout(outputs, "df")
    if df_out:
        for line in df_out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 5:
                pct_str = parts[4].replace("%", "")
                try:
                    pct = int(pct_str)
                    if pct >= 90:
                        mount = parts[5] if len(parts) > 5 else parts[-1]
                        findings.append({
                            "kind": "disk",
                            "summary": f"Disco {mount} al {pct}% de capacidad.",
                            "data": {"filesystem": parts[0], "mount": mount, "pct": pct},
                            "relevance": 95,
                        })
                except ValueError:
                    pass
    else:
        limitations.append("No se pudo leer el uso de disco (df)")


def _analyze_network(outputs: Dict, findings: List, limitations: List) -> None:
    ss_out = _stdout(outputs, "ss")
    if ss_out:
        listen_count = sum(1 for l in ss_out.splitlines() if "LISTEN" in l)
        estab_count = sum(1 for l in ss_out.splitlines() if "ESTAB" in l)
        if estab_count > 1000:
            findings.append({
                "kind": "network",
                "summary": f"Alto número de conexiones establecidas ({estab_count}).",
                "data": {"listen": listen_count, "established": estab_count},
                "relevance": 75,
            })
    else:
        limitations.append("No se pudo leer el estado de red (ss)")


def _analyze_services(outputs: Dict, findings: List, limitations: List) -> None:
    failed_out = _stdout(outputs, "failed_units")
    if failed_out:
        failed = [l for l in failed_out.splitlines() if ".service" in l and "failed" in l.lower()]
        if failed:
            findings.append({
                "kind": "service",
                "summary": f"{len(failed)} servicio(s) en estado failed.",
                "data": {"failed_services": failed[:10]},
                "relevance": 80,
            })
    else:
        limitations.append("No se pudo verificar el estado de los servicios del sistema")


def _analyze_system(outputs: Dict, findings: List, limitations: List) -> None:
    uptime_out = _stdout(outputs, "uptime")
    if uptime_out:
        # Buscar uptime muy bajo (reinicio reciente)
        m = re.search(r"up\s+(\d+)\s+min", uptime_out)
        if m:
            mins = int(m.group(1))
            if mins < 5:
                findings.append({
                    "kind": "system",
                    "summary": f"El servidor reinició hace {mins} minuto(s).",
                    "data": {"uptime_minutes": mins},
                    "relevance": 70,
                })


# ---------------------------------------------------------------------------
# Resumen
# ---------------------------------------------------------------------------

def _summarize(alarm_type: str, findings: List[Dict]) -> tuple[str, str]:
    if not findings:
        return (
            "No se encontraron hallazgos concretos con los datos recopilados. "
            "El incidente puede haber sido transitorio o puede requerir revisión manual.",
            "low",
        )

    top = sorted(findings, key=lambda f: f.get("relevance", 0), reverse=True)[:3]
    summaries = [f["summary"] for f in top]

    confidence = "low"
    max_rel = max(f.get("relevance", 0) for f in findings)
    if max_rel >= 90:
        confidence = "high"
    elif max_rel >= 70:
        confidence = "medium"

    cause = " ".join(summaries)
    return cause, confidence


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _stdout(outputs: Dict, name: str) -> Optional[str]:
    result = outputs.get(name)
    if result is None:
        return None
    return getattr(result, "stdout", None)


def _safe_float(parts: List[str], idx: int) -> Optional[float]:
    try:
        return float(parts[idx])
    except (ValueError, IndexError):
        return None


def _parse_mem(s: str) -> Optional[int]:
    """Parsea valores como '7.8Gi', '8192Mi', '16384000' a bytes."""
    s = s.strip()
    multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4,
                   "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3}
    for suffix, mult in sorted(multipliers.items(), key=lambda x: -len(x[0])):
        if s.endswith(suffix):
            try:
                return int(float(s[:-len(suffix)]) * mult)
            except ValueError:
                return None
    try:
        return int(s)
    except ValueError:
        return None
