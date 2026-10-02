# coding: utf-8
"""Tests sin red: todos los clientes de Huawei Cloud son dobles de prueba."""

import logging
import socket

# Silencia los logs del motor durante los tests (los errores se verifican con asserts).
logging.basicConfig(level=logging.CRITICAL)

# Guardia de red: ningún test puede conectarse fuera de la máquina (p. ej. a Huawei
# Cloud). Solo se permiten conexiones locales (PostgreSQL embebido de los tests).
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
_original_create_connection = socket.create_connection
_original_connect = socket.socket.connect


class ExternalNetworkBlocked(ConnectionRefusedError):
    """Se comporta como un fallo de red real (el SDK lo trata como error de conexión)."""


def _host(address):
    return address[0] if isinstance(address, tuple) else address


def _guarded_create_connection(address, *args, **kwargs):
    if _host(address) not in _LOCAL_HOSTS:
        raise ExternalNetworkBlocked(f"Conexión externa bloqueada en tests: {_host(address)}")
    return _original_create_connection(address, *args, **kwargs)


def _guarded_connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and _host(address) not in _LOCAL_HOSTS:
        raise ExternalNetworkBlocked(f"Conexión externa bloqueada en tests: {_host(address)}")
    return _original_connect(self, address)


socket.create_connection = _guarded_create_connection
socket.socket.connect = _guarded_connect
