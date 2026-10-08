# coding: utf-8
"""Generador de informes PDF para diagnósticos Cloud Eye.

Usa reportlab para producir un PDF profesional con los 26 campos requeridos:
  portada · resumen ejecutivo · datos del incidente · análisis · evidencias
  · comandos ejecutados · recomendaciones · limitaciones · declaración de seguridad

Nunca incluye AK/SK, contraseñas, tokens ni ciphertexts en ninguna sección.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

SECURITY_DECLARATION = (
    "Este diagnóstico utiliza exclusivamente mecanismos de consulta y solo lectura. "
    "No se modificaron procesos, servicios, archivos, configuraciones ni recursos "
    "de Huawei Cloud."
)

_SEVERITY_LABELS = {1: "Crítica (1)", 2: "Alta (2)", 3: "Media (3)", 4: "Baja (4)"}
_CONFIDENCE_LABELS = {"high": "Alta", "medium": "Media", "low": "Baja"}
_ALARM_TYPE_LABELS = {
    "cpu": "CPU — alto uso de procesador",
    "memory": "Memoria — consumo elevado de RAM",
    "disk": "Disco — espacio en disco crítico",
    "network": "Red — anomalía de conectividad",
    "unknown": "General / No clasificada",
}

_STATUS_LABELS = {
    "alert_received": "Alerta recibida",
    "validating": "Validando",
    "pending_diagnosis": "Pendiente de diagnóstico",
    "connecting": "Conectando",
    "analyzing": "Analizando",
    "generating_report": "Generando reporte",
    "report_available": "Reporte disponible",
    "failed": "Error en diagnóstico",
    "no_server": "Sin servidor vinculado",
    "recovered": "Recuperado",
}

_IMPACT_BY_TYPE = {
    "cpu": (
        "Degradación del rendimiento de aplicaciones, latencias elevadas en respuestas "
        "HTTP/API, posible caída de servicios si el uso supera el 100% sostenido."
    ),
    "memory": (
        "Riesgo de OOM Killer terminando procesos críticos, uso intensivo de swap "
        "degradando el rendimiento hasta 10×, posible caída de base de datos o app server."
    ),
    "disk": (
        "Imposibilidad de escribir logs, backups o datos transaccionales, "
        "posibles errores de BD por falta de espacio en tablespaces o WAL, "
        "crasheo de aplicaciones que requieren espacio temporal."
    ),
    "network": (
        "Pérdida de conectividad hacia servicios dependientes, timeout en llamadas a APIs, "
        "posible saturación de conexiones TCP con rechazo de nuevas solicitudes."
    ),
    "unknown": (
        "Impacto no determinado. Se recomienda revisar manualmente los servicios "
        "críticos del servidor y verificar logs del sistema."
    ),
}

_RECOMMENDATIONS_BY_TYPE = {
    "cpu": (
        "1. Identificar y revisar el proceso con mayor consumo de CPU (ver sección Comandos).\n"
        "2. Verificar si existe un cron job o tarea programada que se haya desbocado.\n"
        "3. Considerar escalar verticalmente el ECS si la carga es esperada por crecimiento.\n"
        "4. Revisar logs de la aplicación para errores de bucle o consultas sin índice."
    ),
    "memory": (
        "1. Identificar el proceso con mayor consumo de RAM y revisar fugas de memoria.\n"
        "2. Verificar si el uso de swap es persistente (indica RAM insuficiente).\n"
        "3. Considerar aumentar la RAM del ECS o agregar memoria de intercambio.\n"
        "4. Revisar configuración de JVM heap, PG shared_buffers u otros pools de memoria."
    ),
    "disk": (
        "1. Identificar directorios con mayor ocupación (ver du en sección Comandos).\n"
        "2. Revisar y rotar logs con logrotate si /var/log ocupa excesivo espacio.\n"
        "3. Eliminar backups temporales o dumps obsoletos.\n"
        "4. Considerar expandir el volumen EVS o mover datos a OBS."
    ),
    "network": (
        "1. Verificar si hay algún proceso abriendo conexiones en exceso (ss -tunlp).\n"
        "2. Revisar reglas de Security Group en Huawei Cloud Console.\n"
        "3. Comprobar latencia hacia servicios dependientes (RDS, ELB, etc.).\n"
        "4. Revisar logs del firewall o posibles ataques de tipo DDoS."
    ),
    "unknown": (
        "1. Revisar manualmente los servicios críticos del servidor.\n"
        "2. Consultar los logs del sistema en /var/log/syslog o journalctl.\n"
        "3. Contactar al equipo de operaciones para análisis presencial."
    ),
}


# ---------------------------------------------------------------------------
# Función principal
# ---------------------------------------------------------------------------

def generate_pdf(
    incident,
    analysis: Dict[str, Any],
    results: List[Tuple],
    *,
    context: Optional[Dict[str, Any]] = None,
    is_simulated: bool = False,
) -> bytes:
    """Genera el PDF completo para un incidente.

    Parámetros:
    - incident: instancia de DiagnosticIncident
    - analysis: dict con possible_cause, confidence, findings, limitations
    - results: lista de (seq, name, args, CommandResult|None)
    - context: dict opcional con client_name, account_name (sin secretos)
    - is_simulated: True si el diagnóstico es simulado (datos de prueba)
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
            HRFlowable, KeepTogether,
        )
    except ImportError:
        raise RuntimeError(
            "reportlab no está instalado. "
            "Ejecuta: pip install reportlab"
        )

    ctx = context or {}
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    incident_id_str = str(incident.id) if incident.id else "—"
    alarm_type = getattr(incident, "alarm_type", "unknown") or "unknown"
    fired_str = (
        incident.alarm_fired_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        if getattr(incident, "alarm_fired_at", None)
        else "—"
    )
    severity_int = getattr(incident, "severity", None)
    severity_label = _SEVERITY_LABELS.get(severity_int, str(severity_int) if severity_int else "—")
    confidence_raw = analysis.get("confidence", "low")
    confidence_label = _CONFIDENCE_LABELS.get(confidence_raw, confidence_raw)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        rightMargin=2 * cm, leftMargin=2 * cm,
        topMargin=2.5 * cm, bottomMargin=2 * cm,
        title=f"Diagnóstico Cloud Eye — {incident_id_str}",
        author="Plataforma de Inventario Huawei Cloud",
    )

    # ---- Estilos ----
    styles = getSampleStyleSheet()
    BLUE = colors.HexColor("#0070C0")
    RED = colors.HexColor("#C00000")
    ORANGE = colors.HexColor("#E55A00")
    LIGHT_GRAY = colors.HexColor("#F5F5F5")
    MID_GRAY = colors.HexColor("#CCCCCC")

    h1 = ParagraphStyle("H1", parent=styles["Title"], fontSize=20, spaceAfter=4,
                        textColor=BLUE)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=13,
                        spaceAfter=4, textColor=BLUE)
    h3 = ParagraphStyle("H3", parent=styles["Heading3"], fontSize=11, spaceAfter=3)
    body = styles["BodyText"]
    body.fontSize = 9
    body.leading = 13
    small = ParagraphStyle("Small", parent=body, fontSize=8, leading=11)
    code_style = ParagraphStyle(
        "Code", parent=styles["Code"],
        fontSize=7, leading=9, wordWrap="CJK",
        backColor=LIGHT_GRAY,
        leftIndent=6, rightIndent=6,
    )
    warning_style = ParagraphStyle(
        "Warning", parent=body, fontSize=9, textColor=ORANGE,
        borderColor=ORANGE, borderWidth=0.5, borderPadding=6,
        backColor=colors.HexColor("#FFF3E0"),
    )
    decl_style = ParagraphStyle(
        "Decl", parent=body, fontSize=9, leading=13,
        borderColor=BLUE, borderWidth=0.5, borderPadding=8,
        backColor=colors.HexColor("#EBF4FA"),
    )

    story = []

    # =========================================================
    # PORTADA
    # =========================================================
    if is_simulated:
        story.append(
            Paragraph(
                "⚠ DIAGNÓSTICO SIMULADO — DATOS DE PRUEBA",
                ParagraphStyle("SimWarn", parent=body, fontSize=11,
                               textColor=RED, spaceAfter=6),
            )
        )
        story.append(
            Paragraph(
                "Este documento contiene datos de prueba generados automáticamente "
                "sin conexión SSH real. No refleja el estado actual del servidor.",
                warning_style,
            )
        )
        story.append(Spacer(1, 0.4 * cm))

    story.append(Paragraph("Informe de Diagnóstico Automático", h1))
    story.append(Paragraph("Plataforma de Inventario Huawei Cloud", h3))
    story.append(Spacer(1, 0.3 * cm))
    story.append(HRFlowable(width="100%", thickness=1.5, color=BLUE))
    story.append(Spacer(1, 0.4 * cm))

    # =========================================================
    # SECCIÓN 1 — Datos del Incidente (26 campos)
    # =========================================================
    story.append(Paragraph("1. Datos del Incidente", h2))

    def _row(label: str, value: str) -> list:
        return [
            Paragraph(f"<b>{label}</b>", small),
            Paragraph(_safe_str(value), small),
        ]

    threshold_raw = getattr(incident, "threshold", None)
    obs_raw = getattr(incident, "observed_value", None)
    threshold_str = f"{float(threshold_raw):.4g}" if threshold_raw is not None else "—"
    observed_str = f"{float(obs_raw):.4g}" if obs_raw is not None else "—"

    incident_data = [
        _row("Número de incidente:", incident_id_str),
        _row("Cliente:", ctx.get("client_name", "—")),
        _row("Cuenta Huawei Cloud:", ctx.get("account_name", "—")),
        _row("Región:", getattr(incident, "region", "") or "—"),
        _row("Project ID:", getattr(incident, "project_id_hw", "") or "—"),
        _row("Enterprise Project:", getattr(incident, "enterprise_project_id", "") or "—"),
        _row("Nombre de la ECS:", getattr(incident, "ecs_name", "") or "—"),
        _row("Instance ID:", getattr(incident, "ecs_instance_id", "") or "—"),
        _row("Dirección IP:", getattr(incident, "ecs_ip", "") or "—"),
        _row("Tipo de alarma:", _ALARM_TYPE_LABELS.get(alarm_type, alarm_type)),
        _row("Severidad:", severity_label),
        _row("Métrica:", getattr(incident, "metric_name", "") or "—"),
        _row("Umbral configurado:", threshold_str),
        _row("Valor observado:", observed_str),
        _row("Fecha y hora de alarma:", fired_str),
        _row("Estado del incidente:", _STATUS_LABELS.get(getattr(incident, "status", ""), "—")),
        _row("Reporte generado:", now_str),
    ]

    tbl = Table(incident_data, colWidths=[5.5 * cm, 11.5 * cm])
    tbl.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, LIGHT_GRAY]),
        ("GRID", (0, 0), (-1, -1), 0.3, MID_GRAY),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 0.5 * cm))

    # =========================================================
    # SECCIÓN 2 — Resumen Ejecutivo
    # =========================================================
    story.append(Paragraph("2. Resumen Ejecutivo", h2))

    possible_cause = analysis.get("possible_cause") or "No se identificó una causa concreta con los datos disponibles."
    exec_summary = (
        f"Se recibió una alarma de tipo <b>{_ALARM_TYPE_LABELS.get(alarm_type, alarm_type)}</b> "
        f"con severidad <b>{severity_label}</b> para la instancia ECS "
        f"<b>{_safe_str(getattr(incident, 'ecs_name', '') or getattr(incident, 'ecs_instance_id', ''))}</b>. "
        f"{'El diagnóstico se generó con datos simulados de prueba. ' if is_simulated else ''}"
        f"Análisis heurístico {'de outputs simulados' if is_simulated else 'de los outputs de los comandos ejecutados'}: "
        f"{possible_cause}"
    )
    story.append(Paragraph(exec_summary, body))
    story.append(Spacer(1, 0.4 * cm))

    # =========================================================
    # SECCIÓN 3 — Análisis y Causa Posible
    # =========================================================
    story.append(Paragraph("3. Análisis y Causa Posible", h2))

    analysis_meta = [
        _row("Causa posible:", possible_cause),
        _row("Nivel de confianza:", confidence_label),
    ]
    tbl2 = Table(analysis_meta, colWidths=[5.5 * cm, 11.5 * cm])
    tbl2.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("GRID", (0, 0), (-1, -1), 0.3, MID_GRAY),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, LIGHT_GRAY]),
    ]))
    story.append(tbl2)
    story.append(Spacer(1, 0.3 * cm))

    # =========================================================
    # SECCIÓN 4 — Evidencias
    # =========================================================
    story.append(Paragraph("4. Evidencias Recopiladas", h2))
    findings = analysis.get("findings", [])
    if findings:
        ev_data = [
            [
                Paragraph("<b>Tipo</b>", small),
                Paragraph("<b>Hallazgo</b>", small),
                Paragraph("<b>Relevancia</b>", small),
            ]
        ]
        for f in findings:
            ev_data.append([
                Paragraph(_safe_str(f.get("kind", "")).upper(), small),
                Paragraph(_safe_str(f.get("summary", "")), small),
                Paragraph(f"{f.get('relevance', 0)}%", small),
            ])
        ev_tbl = Table(ev_data, colWidths=[2.5 * cm, 12 * cm, 2.5 * cm])
        ev_tbl.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("BACKGROUND", (0, 0), (-1, 0), BLUE),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("GRID", (0, 0), (-1, -1), 0.3, MID_GRAY),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_GRAY]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(ev_tbl)
    else:
        story.append(Paragraph(
            "No se recopilaron evidencias concretas con los datos disponibles.", body
        ))
    story.append(Spacer(1, 0.4 * cm))

    # =========================================================
    # SECCIÓN 5 — Impacto Probable
    # =========================================================
    story.append(Paragraph("5. Impacto Probable", h2))
    impact = _IMPACT_BY_TYPE.get(alarm_type, _IMPACT_BY_TYPE["unknown"])
    story.append(Paragraph(impact, body))
    story.append(Spacer(1, 0.4 * cm))

    # =========================================================
    # SECCIÓN 6 — Recomendación Manual
    # =========================================================
    story.append(Paragraph("6. Recomendaciones para Revisión Manual", h2))
    rec = _RECOMMENDATIONS_BY_TYPE.get(alarm_type, _RECOMMENDATIONS_BY_TYPE["unknown"])
    for line in rec.splitlines():
        story.append(Paragraph(line.strip(), body))
    story.append(Spacer(1, 0.4 * cm))

    # =========================================================
    # SECCIÓN 7 — Comandos de Consulta Previstos
    # =========================================================
    story.append(Paragraph(
        f"7. Comandos de Consulta {'Ejecutados (Simulados)' if is_simulated else 'Ejecutados'} — Solo Lectura",
        h2,
    ))

    if results:
        cmd_data = [
            [
                Paragraph("<b>#</b>", small),
                Paragraph("<b>Comando</b>", small),
                Paragraph("<b>Exit</b>", small),
                Paragraph("<b>ms</b>", small),
            ]
        ]
        for seq, name, args, result in results[:20]:
            cmd_str = " ".join(str(a) for a in args)
            exit_str = str(result.exit_code) if result and result.exit_code is not None else "—"
            dur_str = str(result.duration_ms) if result and result.duration_ms is not None else "—"
            cmd_data.append([
                Paragraph(str(seq), small),
                Paragraph(_escape_xml(_safe_str(cmd_str)), code_style),
                Paragraph(exit_str, small),
                Paragraph(dur_str, small),
            ])
        cmd_tbl = Table(cmd_data, colWidths=[1 * cm, 12 * cm, 1.5 * cm, 2.5 * cm])
        cmd_tbl.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("BACKGROUND", (0, 0), (-1, 0), BLUE),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("GRID", (0, 0), (-1, -1), 0.3, MID_GRAY),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT_GRAY]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(cmd_tbl)
        story.append(Spacer(1, 0.3 * cm))

        # Muestra stdout de los comandos más relevantes
        story.append(Paragraph("Detalle de outputs (hasta 1500 caracteres por comando):", h3))
        for seq, name, args, result in results[:10]:
            if result and result.stdout and result.stdout.strip():
                story.append(Paragraph(f"<b>[{seq}] {_escape_xml(name)}</b>", body))
                truncated = result.stdout[:1500]
                story.append(Paragraph(_escape_xml(truncated), code_style))
                story.append(Spacer(1, 0.15 * cm))
    else:
        story.append(Paragraph("No se ejecutaron comandos en este diagnóstico.", body))
    story.append(Spacer(1, 0.4 * cm))

    # =========================================================
    # SECCIÓN 8 — Limitaciones del Diagnóstico
    # =========================================================
    story.append(Paragraph("8. Limitaciones del Diagnóstico", h2))
    limitations = analysis.get("limitations", [])
    if limitations:
        for lim in limitations:
            story.append(Paragraph(f"• {_safe_str(lim)}", body))
    else:
        story.append(Paragraph("No se registraron limitaciones específicas.", body))
    story.append(Spacer(1, 0.5 * cm))

    # =========================================================
    # DECLARACIÓN DE SEGURIDAD
    # =========================================================
    story.append(HRFlowable(width="100%", thickness=1.5, color=BLUE))
    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph("Declaración de Seguridad", h2))
    story.append(Paragraph(SECURITY_DECLARATION, decl_style))
    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph(
        "Los resultados son de carácter informativo y no reemplazan el análisis de un "
        "especialista. Las credenciales de acceso SSH nunca se incluyen en este informe.",
        small,
    ))

    doc.build(story)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _safe_str(value: Any) -> str:
    if value is None:
        return "—"
    return str(value)


def _escape_xml(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
