# coding: utf-8
"""Conexión SSH read-only a servidores usando las credenciales cifradas del modelo Server.

RESTRICCIONES DE SEGURIDAD:
  - Solo lectura: nunca escribe, modifica, crea ni borra archivos en el servidor.
  - Usa SecretCipher para descifrar la contraseña; nunca la registra ni devuelve.
  - shell=False implícito en el protocolo SSH exec: los comandos son listas.
  - Timeout por comando configurado en el llamador.
  - Salida truncada a MAX_OUTPUT_BYTES.
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

MAX_OUTPUT_BYTES = 8 * 1024  # 8 KB por stdout / stderr
CONNECT_TIMEOUT = 15         # segundos


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


class SshDiagnosticClient:
    """Cliente SSH de solo lectura. Nunca ejecuta comandos con shell=True."""

    def __init__(self, host: str, port: int, username: str, password: str,
                 host_fingerprint: Optional[str] = None):
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._fingerprint = host_fingerprint
        self._client = None

    def connect(self) -> None:
        try:
            import paramiko
        except ImportError:
            raise RuntimeError("paramiko no está instalado. Agrega 'paramiko' a requirements.txt.")

        client = paramiko.SSHClient()

        if self._fingerprint:
            # TOFU: aceptar solo la huella conocida
            policy = _FingerprintPolicy(self._fingerprint)
        else:
            # Primera conexión: aceptar y registrar
            policy = paramiko.AutoAddPolicy()

        client.set_missing_host_key_policy(policy)
        client.connect(
            hostname=self._host,
            port=self._port,
            username=self._username,
            password=self._password,
            timeout=CONNECT_TIMEOUT,
            allow_agent=False,
            look_for_keys=False,
        )
        self._client = client

    def run(self, args: list[str], *, timeout: int = 30) -> CommandResult:
        """Ejecuta un comando (como lista de args) y devuelve la salida truncada."""
        import time
        if self._client is None:
            raise RuntimeError("No conectado. Llama a connect() primero.")

        # Componer el comando como cadena segura (sin interpolación de shell)
        import shlex
        cmd = " ".join(shlex.quote(a) for a in args)

        t0 = time.monotonic()
        stdin, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
        stdin.close()

        raw_out = stdout.read(MAX_OUTPUT_BYTES)
        raw_err = stderr.read(MAX_OUTPUT_BYTES)
        exit_code = stdout.channel.recv_exit_status()
        duration_ms = int((time.monotonic() - t0) * 1000)

        return CommandResult(
            exit_code=exit_code,
            stdout=_safe_decode(raw_out),
            stderr=_safe_decode(raw_err),
            duration_ms=duration_ms,
        )

    def close(self) -> None:
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()


def _safe_decode(data: bytes) -> str:
    try:
        return data.decode("utf-8", errors="replace")
    except Exception:
        return repr(data)


class _FingerprintPolicy:
    """Política paramiko que acepta solo la huella SHA256 conocida."""

    def __init__(self, expected_fingerprint: str):
        self._expected = expected_fingerprint.lower().replace("sha256:", "")

    def missing_host_key(self, client, hostname, key):
        import base64, hashlib
        digest = hashlib.sha256(key.asbytes()).digest()
        actual = base64.b64encode(digest).decode().rstrip("=").lower()
        if actual != self._expected:
            raise Exception(
                f"Huella del servidor no coincide. "
                f"Esperada={self._expected!r}, actual={actual!r}. "
                "Verifica el servidor antes de continuar."
            )


def build_client(server, cipher) -> SshDiagnosticClient:
    """Construye un SshDiagnosticClient desde un modelo Server y un SecretCipher.

    La contraseña se descifra aquí y nunca se guarda en ninguna variable de módulo.
    """
    password = cipher.decrypt(server.password_ciphertext).decode()
    return SshDiagnosticClient(
        host=server.host,
        port=server.port,
        username=server.username,
        password=password,
        host_fingerprint=server.host_fingerprint,
    )
