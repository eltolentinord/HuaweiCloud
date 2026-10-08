# coding: utf-8
"""Ejecutor de diagnóstico SSH para un incidente.

Orquesta: conexión SSH → comandos → evidencia → actualización de estado.

RESTRICCIONES DE SEGURIDAD:
  - Nunca usa shell=True.
  - Todos los comandos provienen de la lista blanca (diagnostics.commands).
  - No escribe, modifica ni borra archivos en el servidor remoto.
  - Las credenciales SSH se descifran in-memory y no se registran.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import sessionmaker

from core.crypto import SecretCipher
from diagnostics.commands import commands_for, ALLOWED_EXECUTABLES
from diagnostics.ssh import CommandResult, MAX_OUTPUT_BYTES

logger = logging.getLogger(__name__)

MAX_COMMANDS = 25  # límite de seguridad por diagnóstico

# Etiqueta visible en todos los outputs de simulación
_SIM_TAG = "[DATOS DE PRUEBA — SIN CONEXIÓN SSH REAL]"


# ---------------------------------------------------------------------------
# Diagnóstico SIMULADO (Phase 1 — sin servidor SSH real)
# ---------------------------------------------------------------------------

def _simulated_outputs(alarm_type: str) -> Dict[str, CommandResult]:
    """Genera outputs simulados realistas por tipo de alarma.

    Todos los valores están claramente marcados como datos de prueba.
    No se ejecuta ningún comando real, no se conecta a ningún servidor.
    """
    common = {
        "uptime": CommandResult(
            exit_code=0,
            stdout=(
                f"{_SIM_TAG}\n"
                " 10:42:17 up 15 days,  3:18,  2 users,  "
                "load average: 5.92, 5.61, 4.38"
            ),
            stderr="",
            duration_ms=12,
        ),
        "hostname": CommandResult(
            exit_code=0,
            stdout=f"{_SIM_TAG}\necs-diagnostico-prueba-001",
            stderr="",
            duration_ms=8,
        ),
        "uname": CommandResult(
            exit_code=0,
            stdout=f"{_SIM_TAG}\nLinux ecs-diagnostico-prueba-001 5.15.0-91-generic #101-Ubuntu SMP x86_64 GNU/Linux",
            stderr="",
            duration_ms=9,
        ),
        "date": CommandResult(
            exit_code=0,
            stdout=f"{_SIM_TAG}\n{datetime.now(timezone.utc).strftime('%a %b %d %H:%M:%S UTC %Y')}",
            stderr="",
            duration_ms=7,
        ),
        "failed_units": CommandResult(
            exit_code=0,
            stdout=f"{_SIM_TAG}\n  UNIT                         LOAD   ACTIVE SUB    DESCRIPTION\n0 loaded units listed.",
            stderr="",
            duration_ms=45,
        ),
    }

    cpu_outputs = {
        "top_snapshot": CommandResult(
            exit_code=0,
            stdout=(
                f"{_SIM_TAG}\n"
                "top - 10:42:17 up 15 days,  3:18,  2 users,  load average: 5.92, 5.61, 4.38\n"
                "Tasks: 312 total,   4 running, 308 sleeping\n"
                "%Cpu(s): 87.3 us,  8.2 sy,  0.0 ni,  2.1 id,  0.8 wa,  0.0 hi,  1.6 si\n"
                "MiB Mem:  15872.0 total,    312.4 free,  13984.2 used,   1575.4 buff/cache\n\n"
                "  PID USER     PR  NI    VIRT    RES    SHR S  %CPU  %MEM  TIME+  COMMAND\n"
                "12847 www-data 20   0 2456789 512344 102456 R  89.2  12.4  312:17 nginx\n"
                " 8291 postgres 20   0 1234567 340123  45678 S  45.1   8.2  201:44 postgres\n"
                " 3421 java     20   0 4567890 901234 123456 S  38.7  22.1  489:32 java\n"
                " 1024 root     20   0  234567  45678   8901 S   8.3   1.1   12:05 systemd\n"
            ),
            stderr="",
            duration_ms=250,
        ),
        "ps_cpu": CommandResult(
            exit_code=0,
            stdout=(
                f"{_SIM_TAG}\n"
                "USER       PID %CPU %MEM    VSZ   RSS TTY STAT START   TIME COMMAND\n"
                "www-data 12847 89.2 12.4 2456789 512344 ? Sl  09:00 312:17 nginx: worker\n"
                "postgres  8291 45.1  8.2 1234567 340123 ? S   08:30 201:44 postgres: main\n"
                "java      3421 38.7 22.1 4567890 901234 ? Sl  07:15 489:32 java -jar app.jar\n"
            ),
            stderr="",
            duration_ms=180,
        ),
        "loadavg": CommandResult(
            exit_code=0,
            stdout=f"{_SIM_TAG}\n5.92 5.61 4.38 4/312 12847\n",
            stderr="",
            duration_ms=6,
        ),
        "vmstat": CommandResult(
            exit_code=0,
            stdout=(
                f"{_SIM_TAG}\n"
                "         7 forks\n"
                "      4096 pages swapped in\n"
                "      8192 pages swapped out\n"
            ),
            stderr="",
            duration_ms=22,
        ),
    }

    memory_outputs = {
        "free": CommandResult(
            exit_code=0,
            stdout=(
                f"{_SIM_TAG}\n"
                "              total        used        free      shared  buff/cache   available\n"
                "Mem:       16252928    14845440      245760      134144     1161728      921600\n"
                "Swap:       2097152     1966080      131072\n"
            ),
            stderr="",
            duration_ms=18,
        ),
        "meminfo": CommandResult(
            exit_code=0,
            stdout=(
                f"{_SIM_TAG}\n"
                "MemTotal:       16252928 kB\n"
                "MemFree:          245760 kB\n"
                "MemAvailable:     921600 kB\n"
                "Buffers:          123456 kB\n"
                "Cached:          1038272 kB\n"
                "SwapCached:       327680 kB\n"
                "SwapTotal:       2097152 kB\n"
                "SwapFree:         131072 kB\n"
            ),
            stderr="",
            duration_ms=14,
        ),
        "ps_mem": CommandResult(
            exit_code=0,
            stdout=(
                f"{_SIM_TAG}\n"
                "USER       PID %CPU %MEM    VSZ   RSS TTY STAT START   TIME COMMAND\n"
                "java      3421 38.7 22.1 4567890 901234 ? Sl  07:15 489:32 java -jar app.jar\n"
                "postgres  8291 45.1  8.2 1234567 340123 ? S   08:30 201:44 postgres: main\n"
            ),
            stderr="",
            duration_ms=165,
        ),
    }

    disk_outputs = {
        "df": CommandResult(
            exit_code=0,
            stdout=(
                f"{_SIM_TAG}\n"
                "Filesystem      Size  Used Avail Use% Mounted on\n"
                "/dev/vda1        80G   74G  2.5G  97% /\n"
                "/dev/vdb1       200G  178G   12G  94% /data\n"
                "tmpfs           7.8G  2.1G  5.7G  27% /tmp\n"
                "tmpfs           7.8G  1.2G  6.6G  16% /dev/shm\n"
            ),
            stderr="",
            duration_ms=32,
        ),
        "lsblk": CommandResult(
            exit_code=0,
            stdout=(
                f"{_SIM_TAG}\n"
                "NAME   MAJ:MIN RM   SIZE RO TYPE MOUNTPOINT\n"
                "vda    252:0    0    80G  0 disk\n"
                "└─vda1 252:1    0    80G  0 part /\n"
                "vdb    252:16   0   200G  0 disk\n"
                "└─vdb1 252:17   0   200G  0 part /data\n"
            ),
            stderr="",
            duration_ms=20,
        ),
        "du_varlog": CommandResult(
            exit_code=0,
            stdout=f"{_SIM_TAG}\n18G\t/var/log\n",
            stderr="",
            duration_ms=890,
        ),
    }

    network_outputs = {
        "ss": CommandResult(
            exit_code=0,
            stdout=(
                f"{_SIM_TAG}\n"
                "Netid  State   Recv-Q Send-Q   Local Address:Port  Peer Address:Port\n"
                "tcp    LISTEN  0      128      0.0.0.0:22           0.0.0.0:*\n"
                "tcp    LISTEN  0      128      0.0.0.0:80           0.0.0.0:*\n"
                "tcp    LISTEN  0      128      0.0.0.0:443          0.0.0.0:*\n"
                "tcp    ESTAB   0      0        10.0.1.5:22         203.0.113.42:58921\n"
                # Simular muchas conexiones
                + "".join(
                    f"tcp    ESTAB   0      0        10.0.1.5:80         "
                    f"203.0.113.{i % 256}:{40000 + i}\n"
                    for i in range(1, 1100)
                )
            ),
            stderr="",
            duration_ms=88,
        ),
        "ip_addr": CommandResult(
            exit_code=0,
            stdout=(
                f"{_SIM_TAG}\n"
                "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536\n"
                "    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00\n"
                "    inet 127.0.0.1/8 scope host lo\n"
                "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n"
                "    link/ether fa:16:3e:ab:cd:ef brd ff:ff:ff:ff:ff:ff\n"
                "    inet 10.0.1.5/24 brd 10.0.1.255 scope global eth0\n"
            ),
            stderr="",
            duration_ms=14,
        ),
        "ip_route": CommandResult(
            exit_code=0,
            stdout=(
                f"{_SIM_TAG}\n"
                "default via 10.0.1.1 dev eth0 proto dhcp src 10.0.1.5 metric 100\n"
                "10.0.1.0/24 dev eth0 proto kernel scope link src 10.0.1.5\n"
            ),
            stderr="",
            duration_ms=11,
        ),
    }

    if alarm_type == "cpu":
        return {**common, **cpu_outputs}
    if alarm_type == "memory":
        return {**common, **memory_outputs}
    if alarm_type == "disk":
        return {**common, **disk_outputs}
    if alarm_type == "network":
        return {**common, **network_outputs}
    # unknown / otros: todos
    return {**common, **cpu_outputs, **memory_outputs, **disk_outputs, **network_outputs}


def run_diagnostic_simulated(factory: sessionmaker, incident_id: uuid.UUID) -> None:
    """Diagnóstico simulado: sin SSH real, sin credenciales.

    Genera evidencias de prueba claramente etiquetadas como datos simulados,
    analiza con las mismas reglas heurísticas que el diagnóstico real,
    y produce un PDF marcado como SIMULACIÓN.

    Nunca:
    - Conecta a ningún servidor.
    - Descifra credenciales.
    - Ejecuta comandos del sistema operativo.
    - Usa shell=True.
    """
    with factory() as session:
        from db.models import DiagnosticIncident
        from diagnostics.queue import set_status

        incident = session.get(DiagnosticIncident, incident_id)
        if incident is None:
            return

        set_status(session, incident_id, "analyzing")
        session.commit()

    t_start = time.monotonic()

    # Outputs simulados por tipo de alarma
    with factory() as session:
        from db.models import DiagnosticIncident
        incident = session.get(DiagnosticIncident, incident_id)
        alarm_type = incident.alarm_type if incident else "unknown"

    sim_outputs = _simulated_outputs(alarm_type)

    # Construir lista de resultados en formato (seq, name, args, result)
    cmd_defs = commands_for(alarm_type)[:MAX_COMMANDS]
    results: List[Tuple] = []
    for seq, (name, args) in enumerate(cmd_defs):
        result = sim_outputs.get(name)
        results.append((seq, name, args, result))

    elapsed_ms = int((time.monotonic() - t_start) * 1000)

    # Análisis de causa
    from diagnostics.analyzer import analyze
    outputs_for_analysis = {name: res for _, name, _, res in results if res is not None}
    analysis = analyze(alarm_type, outputs_for_analysis)

    # Enriquecer el análisis con contexto de simulación
    analysis["simulated"] = True
    analysis["limitations"] = analysis.get("limitations", []) + [
        "DIAGNÓSTICO SIMULADO: No se realizó conexión SSH a ningún servidor real.",
        "Los datos mostrados son de prueba y no reflejan el estado real del sistema.",
        "Estos resultados deben tratarse únicamente como verificación del flujo de la plataforma.",
    ]

    # Persistir en BD
    with factory() as session:
        from db.models import DiagnosticIncident, DiagnosticCommand, DiagnosticEvidence
        from diagnostics.queue import set_status

        set_status(session, incident_id, "generating_report", duration_ms=elapsed_ms)
        session.flush()

        # Guardar comandos simulados
        for seq, name, args, result in results:
            cmd = DiagnosticCommand(
                incident_id=incident_id,
                sequence=seq,
                command=name,
                args=args,
                exit_code=result.exit_code if result else None,
                stdout_safe=result.stdout[:MAX_OUTPUT_BYTES] if result else None,
                stderr_safe=result.stderr[:MAX_OUTPUT_BYTES] if result else None,
                duration_ms=result.duration_ms if result else None,
                started_at=datetime.now(timezone.utc),
            )
            session.add(cmd)

        session.flush()

        # Guardar evidencia
        for finding in analysis.get("findings", []):
            ev = DiagnosticEvidence(
                incident_id=incident_id,
                kind=finding.get("kind", "unknown"),
                summary_safe=f"[SIMULADO] {finding.get('summary', '')}",
                data_json=finding.get("data"),
                relevance_score=finding.get("relevance", 0),
            )
            session.add(ev)

        # Actualizar incidente
        incident = session.get(DiagnosticIncident, incident_id)
        if incident:
            incident.possible_cause = analysis.get("possible_cause", "")
            incident.confidence = analysis.get("confidence", "low")
            incident.report_json = {
                "alarm_type": alarm_type,
                "analysis": analysis,
                "command_count": len(results),
                "simulated": True,
            }
            incident.duration_ms = elapsed_ms

        set_status(session, incident_id, "report_available", duration_ms=elapsed_ms)

        # Generar PDF
        try:
            from diagnostics.pdf_generator import generate_pdf
            incident = session.get(DiagnosticIncident, incident_id)
            # Obtener nombres de cliente y cuenta
            context = _build_context(session, incident)
            pdf_bytes = generate_pdf(incident, analysis, results, context=context, is_simulated=True)
            if incident:
                incident.pdf_data = pdf_bytes
                incident.pdf_name = f"diagnostico_sim_{incident_id}.pdf"
        except Exception as exc:
            logger.warning("PDF simulado no generado para %s: %s", incident_id, exc)

        session.commit()

    logger.info("Diagnóstico simulado completado para incidente %s (tipo=%s elapsed=%dms)",
                incident_id, alarm_type, elapsed_ms)


def _build_context(session, incident) -> Dict[str, Any]:
    """Obtiene client_name y account_name para el PDF."""
    ctx: Dict[str, Any] = {}
    if incident is None:
        return ctx
    try:
        if incident.client_id:
            from db.models import Client
            client = session.get(Client, incident.client_id)
            if client:
                ctx["client_name"] = client.name
        if incident.account_id:
            from db.models import CloudAccount
            account = session.get(CloudAccount, incident.account_id)
            if account:
                ctx["account_name"] = account.name
    except Exception:
        pass
    return ctx


# ---------------------------------------------------------------------------
# Diagnóstico SSH real (Phase B — servidor vinculado)
# ---------------------------------------------------------------------------

def run_diagnostic(factory: sessionmaker, cipher: SecretCipher,
                   incident_id: uuid.UUID) -> None:
    """Ejecuta el diagnóstico completo para un incidente. Actualiza la BD al terminar."""
    with factory() as session:
        from db.models import DiagnosticIncident, Server
        from diagnostics.queue import set_status

        incident = session.get(DiagnosticIncident, incident_id)
        if incident is None:
            return

        if not incident.server_id:
            set_status(session, incident_id, "no_server",
                       error_message_safe="Sin servidor SSH vinculado.")
            session.commit()
            return

        server = session.get(Server, incident.server_id)
        if server is None or not server.enabled:
            set_status(session, incident_id, "no_server",
                       error_message_safe="Servidor no encontrado o deshabilitado.")
            session.commit()
            return

        set_status(session, incident_id, "connecting")
        session.commit()

    t_start = time.monotonic()
    try:
        _ssh_and_collect(factory, cipher, incident_id)
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        with factory() as session:
            from diagnostics.queue import set_status
            set_status(session, incident_id, "failed",
                       error_kind="ssh_error",
                       error_message_safe=str(exc)[:500],
                       duration_ms=elapsed_ms)
            session.commit()
        raise


def _ssh_and_collect(factory: sessionmaker, cipher: SecretCipher,
                     incident_id: uuid.UUID) -> None:
    with factory() as session:
        from db.models import DiagnosticIncident
        from diagnostics.queue import set_status

        incident = session.get(DiagnosticIncident, incident_id)
        set_status(session, incident_id, "analyzing")
        session.commit()

    t_start = time.monotonic()
    cmd_defs = commands_for(incident.alarm_type)[:MAX_COMMANDS]
    results: List[Tuple] = []

    from diagnostics.ssh import build_client
    with factory() as session:
        from db.models import DiagnosticIncident, Server
        incident = session.get(DiagnosticIncident, incident_id)
        server = session.get(Server, incident.server_id)

    with build_client(server, cipher) as ssh:
        for seq, (name, args) in enumerate(cmd_defs):
            if args[0] not in ALLOWED_EXECUTABLES:
                logger.warning("Comando %r NO está en lista blanca. Saltando.", args[0])
                continue
            try:
                result = ssh.run(args, timeout=30)
                results.append((seq, name, args, result))
            except Exception as exc:
                logger.warning("Comando %r falló en %s: %s", name, incident_id, exc)
                results.append((seq, name, args, None))

    elapsed_ms = int((time.monotonic() - t_start) * 1000)

    with factory() as session:
        from db.models import DiagnosticIncident, DiagnosticCommand, DiagnosticEvidence
        from diagnostics.queue import set_status
        from diagnostics.analyzer import analyze

        set_status(session, incident_id, "generating_report", duration_ms=elapsed_ms)
        session.flush()

        for seq, name, args, result in results:
            cmd = DiagnosticCommand(
                incident_id=incident_id,
                sequence=seq,
                command=name,
                args=args,
                exit_code=result.exit_code if result else None,
                stdout_safe=result.stdout[:MAX_OUTPUT_BYTES] if result else None,
                stderr_safe=result.stderr[:MAX_OUTPUT_BYTES] if result else None,
                duration_ms=result.duration_ms if result else None,
                started_at=datetime.now(timezone.utc),
            )
            session.add(cmd)

        session.flush()

        outputs = {name: res for _, name, _, res in results if res is not None}
        analysis = analyze(incident.alarm_type, outputs)

        for finding in analysis.get("findings", []):
            ev = DiagnosticEvidence(
                incident_id=incident_id,
                kind=finding.get("kind", "unknown"),
                summary_safe=finding.get("summary", ""),
                data_json=finding.get("data"),
                relevance_score=finding.get("relevance", 0),
            )
            session.add(ev)

        incident = session.get(DiagnosticIncident, incident_id)
        if incident:
            incident.possible_cause = analysis.get("possible_cause", "")
            incident.confidence = analysis.get("confidence", "low")
            incident.report_json = {
                "alarm_type": incident.alarm_type,
                "analysis": analysis,
                "command_count": len(results),
            }
            incident.duration_ms = elapsed_ms

        set_status(session, incident_id, "report_available", duration_ms=elapsed_ms)

        try:
            from diagnostics.pdf_generator import generate_pdf
            incident = session.get(DiagnosticIncident, incident_id)
            context = _build_context(session, incident)
            pdf_bytes = generate_pdf(incident, analysis, results, context=context, is_simulated=False)
            if incident:
                incident.pdf_data = pdf_bytes
                incident.pdf_name = f"diagnostico_{incident_id}.pdf"
        except Exception as exc:
            logger.warning("PDF no generado para %s: %s", incident_id, exc)

        session.commit()
