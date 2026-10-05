# coding: utf-8
"""Conexión SSH (paramiko) para ejecutar la auditoría. Inyectable: los tests usan un doble.

Seguridad:
- Huella del servidor verificada SIEMPRE (TOFU): sin huella guardada se acepta y se devuelve
  para guardarla; si hay una guardada y no coincide, la conexión se rechaza ANTES de enviar la
  contraseña (posible suplantación).
- Sin agente ni llaves locales: solo la contraseña configurada.
- La contraseña viaja por el canal cifrado de SSH y, para sudo, por stdin (nunca en argv).
"""

from __future__ import annotations

import base64
import hashlib
import io
import socket
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

CONNECT_TIMEOUT = 15


class SshError(Exception):
    """Error de conexión/ejecución con una categoría segura (sin secretos en el mensaje)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


class SshSession(Protocol):
    fingerprint: str

    def run(self, command: str, *, stdin: Optional[str] = None, timeout: int = 60) -> ExecResult: ...

    def put(self, data: bytes, remote_path: str, *, mode: int = 0o700) -> None: ...

    def get(self, remote_path: str) -> bytes: ...

    def close(self) -> None: ...


Connector = Callable[..., SshSession]


def fingerprint_of(key: Any) -> str:
    """Huella SHA256 en el mismo formato que OpenSSH (``SHA256:…``)."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


class _ParamikoSession:
    def __init__(self, client: Any, fingerprint: str) -> None:
        self._client = client
        self.fingerprint = fingerprint
        self._sftp: Any = None

    def run(self, command: str, *, stdin: Optional[str] = None, timeout: int = 60) -> ExecResult:
        try:
            channel_in, channel_out, channel_err = self._client.exec_command(command, timeout=timeout)
            if stdin is not None:
                channel_in.write(stdin)
                channel_in.flush()
            channel_in.channel.shutdown_write()
            out = channel_out.read().decode("utf-8", "replace")
            err = channel_err.read().decode("utf-8", "replace")
            return ExecResult(channel_out.channel.recv_exit_status(), out, err)
        except socket.timeout:
            raise SshError("timeout", f"El comando no terminó en {timeout} s.") from None

    def _sftp_client(self) -> Any:
        if self._sftp is None:
            self._sftp = self._client.open_sftp()
        return self._sftp

    def put(self, data: bytes, remote_path: str, *, mode: int = 0o700) -> None:
        sftp = self._sftp_client()
        sftp.putfo(io.BytesIO(data), remote_path)
        sftp.chmod(remote_path, mode)

    def get(self, remote_path: str) -> bytes:
        buffer = io.BytesIO()
        self._sftp_client().getfo(remote_path, buffer)
        return buffer.getvalue()

    def close(self) -> None:
        try:
            if self._sftp is not None:
                self._sftp.close()
        finally:
            self._client.close()


def paramiko_connect(*, host: str, port: int, username: str, password: str,
                     expected_fingerprint: Optional[str], timeout: int = CONNECT_TIMEOUT) -> SshSession:
    import paramiko  # type: ignore[import-untyped]

    seen: dict = {}

    class _VerifyHostKey(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):  # noqa: D401 - API de paramiko
            seen["fingerprint"] = fingerprint_of(key)
            if expected_fingerprint and seen["fingerprint"] != expected_fingerprint:
                raise SshError("fingerprint", "La huella del servidor cambió: posible suplantación. "
                               "Verifícala y, si es correcta, acéptala de nuevo desde la plataforma.")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(_VerifyHostKey())
    try:
        client.connect(host, port=port, username=username, password=password, timeout=timeout,
                       banner_timeout=timeout, auth_timeout=timeout, allow_agent=False, look_for_keys=False)
    except SshError:
        client.close()
        raise
    except paramiko.AuthenticationException:
        client.close()
        raise SshError("auth", "Usuario o contraseña SSH incorrectos.") from None
    except socket.gaierror:
        client.close()
        raise SshError("dns", f"No se pudo resolver el nombre del servidor {host}.") from None
    except (socket.timeout, TimeoutError):
        client.close()
        raise SshError("timeout", f"Sin respuesta de {host}:{port} en {timeout} s.") from None
    except (paramiko.ssh_exception.NoValidConnectionsError, ConnectionError, OSError):
        client.close()
        raise SshError("network", f"No se pudo conectar a {host}:{port} (puerto cerrado o sin ruta).") from None
    except paramiko.SSHException as exc:
        client.close()
        raise SshError("ssh", f"Error de SSH: {type(exc).__name__}.") from None
    return _ParamikoSession(client, seen.get("fingerprint", ""))
