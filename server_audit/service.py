# coding: utf-8
"""Ejecución de ``server-audit.sh`` en uno o varios servidores (solo lectura en el servidor).

Pasos por servidor: descifrar la contraseña en memoria → SSH (huella verificada) → subir el
script a un directorio temporal propio (700) → ``sudo -S`` con la contraseña por stdin →
descargar JSON + HTML → borrar el directorio temporal → guardar el resultado.
Ningún mensaje guardado o devuelto contiene la contraseña.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shlex
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from core.crypto import SecretCipher
from core.errors import safe_message
from db.models import Server, ServerAuditRun
from db.session import session_scope
from server_audit.runner import Connector, SshError, paramiko_connect
from server_audit.servers import decrypt_password

logger = logging.getLogger(__name__)
SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"
AUDIT_SCRIPT = SCRIPTS_DIR / "server-audit.sh"
SCRIPT_TIMEOUT = 600
MAX_PARALLEL = 4
MAX_HTML_BYTES = 5_000_000
SUDO_FAILURES = ("incorrect password", "sorry, try again", "not in the sudoers", "no tty present",
                 "a password is required", "contraseña incorrecta", "no está en el archivo sudoers")


def script_bytes() -> bytes:
    return AUDIT_SCRIPT.read_bytes()


def script_sha256() -> str:
    return hashlib.sha256(script_bytes()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_batch(session: Session, client_id: uuid.UUID, servers: Sequence[Server], *,
                 requested_by: str) -> List[ServerAuditRun]:
    batch_id = uuid.uuid4()
    runs = [ServerAuditRun(client_id=client_id, server_id=s.id, server_name=s.name, batch_id=batch_id,
                           status="queued", requested_by=requested_by[:200]) for s in servers]
    session.add_all(runs)
    session.flush()
    return runs


def _validate_result(raw: bytes) -> Dict[str, Any]:
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise SshError("result", "El script no produjo un JSON válido.") from None
    if not isinstance(data, dict) or not isinstance(data.get("checks"), list):
        raise SshError("result", "El JSON del script no tiene el formato esperado (falta 'checks').")
    return data


def _pct(block: Any) -> Optional[int]:
    try:
        return int(str(block.get("pct"))) if isinstance(block, dict) and block.get("pct") is not None else None
    except (TypeError, ValueError):
        return None


def audit_server(server: Server, password: str, *, connector: Connector = paramiko_connect) -> Dict[str, Any]:
    """Ejecuta la auditoría en UN servidor. Devuelve ``{fingerprint, data, html}`` o lanza ``SshError``."""
    session = connector(host=server.host, port=server.port, username=server.username, password=password,
                        expected_fingerprint=server.host_fingerprint)
    remote = f"/tmp/hci-audit-{uuid.uuid4().hex}"
    quoted = shlex.quote(remote)
    created = False
    try:
        made = session.run(f"mkdir -m 700 {quoted}", timeout=30)
        if made.exit_code != 0:
            raise SshError("script", "No se pudo crear el directorio temporal en /tmp del servidor.")
        created = True
        session.put(script_bytes(), f"{remote}/server-audit.sh", mode=0o700)
        args = f"bash {quoted}/server-audit.sh -j {quoted}/r.json -r {quoted}/r.html"
        if server.username == "root":
            result = session.run(args, timeout=SCRIPT_TIMEOUT)
        else:
            # -S: contraseña por stdin; -p '': sin prompt; -k: no reutiliza credenciales cacheadas.
            result = session.run(f"sudo -k -S -p '' {args}", stdin=password + "\n", timeout=SCRIPT_TIMEOUT)
        stderr_low = (result.stderr or "").lower()
        if result.exit_code != 0 and any(s in stderr_low for s in SUDO_FAILURES):
            raise SshError("sudo", "sudo rechazó la contraseña o el usuario no tiene permisos de sudo.")
        try:
            data = _validate_result(session.get(f"{remote}/r.json"))
        except SshError:
            raise
        except Exception:
            tail = safe_message(" ".join((result.stderr or result.stdout or "").splitlines()[-5:]), (password,))
            raise SshError("script", f"El script terminó sin generar el JSON (código {result.exit_code}). {tail}".strip())
        try:
            html = session.get(f"{remote}/r.html")[:MAX_HTML_BYTES].decode("utf-8", "replace")
        except Exception:
            html = None
        return {"fingerprint": session.fingerprint, "data": data, "html": html}
    finally:
        if created:
            try:
                # Archivos creados por root dentro de un directorio propio (700): el usuario puede borrarlos.
                session.run(f"rm -rf -- {quoted}", timeout=60)
            except Exception:
                logger.warning("No se pudo borrar el directorio temporal de auditoría en el servidor")
        session.close()


def run_audit(factory: sessionmaker, cipher: SecretCipher, run_id: uuid.UUID, *,
              connector: Connector = paramiko_connect) -> None:
    """Ejecuta una auditoría encolada y guarda su resultado (nunca lanza: el error queda en la fila)."""
    with session_scope(factory) as db:
        run = db.get(ServerAuditRun, run_id)
        if run is None or run.status != "queued":
            return
        server = db.get(Server, run.server_id) if run.server_id else None
        if server is None or not server.enabled:
            run.status, run.finished_at = "failed", _now()
            run.error_kind, run.error_message = "config", "El servidor no existe o está deshabilitado."
            return
        run.status, run.started_at, run.script_sha256 = "running", _now(), script_sha256()
        snapshot = Server(id=server.id, client_id=server.client_id, name=server.name, host=server.host,
                          port=server.port, username=server.username, password_ciphertext=server.password_ciphertext,
                          key_version=server.key_version, host_fingerprint=server.host_fingerprint)
    password = ""
    try:
        password = decrypt_password(snapshot, cipher)
        outcome = audit_server(snapshot, password, connector=connector)
        error: Optional[SshError] = None
    except SshError as exc:
        outcome, error = None, exc
    except Exception as exc:  # nunca filtra la contraseña
        outcome = None
        error = SshError("internal", f"Error interno ({type(exc).__name__}).")
    with session_scope(factory) as db:
        run = db.get(ServerAuditRun, run_id)
        if run is None:   # borrada mientras se auditaba
            return
        server = db.get(Server, snapshot.id)
        run.finished_at = _now()
        if error is not None:
            run.status, run.error_kind = "failed", error.kind
            run.error_message = safe_message(str(error), (password,))[:1000]
            return
        assert outcome is not None
        if server is not None and not server.host_fingerprint and outcome["fingerprint"]:
            server.host_fingerprint = outcome["fingerprint"]
        data = outcome["data"]
        run.status = "succeeded"
        run.result_json, run.report_html = data, outcome["html"]
        run.hostname = str(data.get("hostname") or "")[:255] or None
        run.os = str(data.get("os") or "")[:255] or None
        try:
            run.overall_score = int(data.get("overall_score"))
        except (TypeError, ValueError):
            run.overall_score = None
        run.hardening_pct, run.updates_pct = _pct(data.get("hardening")), _pct(data.get("updates"))


def run_batch(factory: sessionmaker, cipher: SecretCipher, run_ids: Sequence[uuid.UUID], *,
              connector: Connector = paramiko_connect, parallel: Optional[int] = None) -> None:
    """Varios servidores a la vez (máx. ``parallel``); un fallo no detiene a los demás."""
    parallel = MAX_PARALLEL if parallel is None else parallel
    if parallel <= 1 or len(run_ids) <= 1:
        for run_id in run_ids:
            run_audit(factory, cipher, run_id, connector=connector)
        return
    with ThreadPoolExecutor(max_workers=min(parallel, len(run_ids))) as pool:
        list(pool.map(lambda rid: run_audit(factory, cipher, rid, connector=connector), run_ids))


def check_connection(server: Server, password: str, *, connector: Connector = paramiko_connect) -> Dict[str, Any]:
    """Prueba SSH y sudo sin ejecutar el script. Devuelve la huella y si sudo funciona."""
    session = connector(host=server.host, port=server.port, username=server.username, password=password,
                        expected_fingerprint=server.host_fingerprint)
    try:
        if server.username == "root":
            sudo_ok, detail = True, "Conectado como root (no necesita sudo)."
        else:
            result = session.run("sudo -k -S -p '' true", stdin=password + "\n", timeout=30)
            sudo_ok = result.exit_code == 0
            detail = "sudo funciona." if sudo_ok else "sudo rechazó la contraseña o el usuario no tiene sudo."
        bash = session.run("command -v bash >/dev/null && echo ok", timeout=15)
        return {"fingerprint": session.fingerprint, "sudo": sudo_ok, "bash": bash.stdout.strip() == "ok",
                "detail": detail}
    finally:
        session.close()


def latest_runs(db: Session, client_id: uuid.UUID) -> Dict[uuid.UUID, ServerAuditRun]:
    """Última auditoría (cualquier estado) de cada servidor del cliente."""
    rows = db.scalars(select(ServerAuditRun).where(ServerAuditRun.client_id == client_id)
                      .order_by(ServerAuditRun.created_at.desc()))
    latest: Dict[uuid.UUID, ServerAuditRun] = {}
    for row in rows:
        if row.server_id and row.server_id not in latest:
            latest[row.server_id] = row
    return latest


def latest_successful(db: Session, client_id: uuid.UUID) -> List[ServerAuditRun]:
    rows = db.scalars(select(ServerAuditRun).where(ServerAuditRun.client_id == client_id,
                                                   ServerAuditRun.status == "succeeded")
                      .order_by(ServerAuditRun.finished_at.desc()))
    seen, out = set(), []
    for row in rows:
        key = row.server_id or row.server_name
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out
