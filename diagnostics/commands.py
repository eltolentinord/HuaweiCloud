# coding: utf-8
"""Lista blanca de comandos de diagnóstico read-only.

REGLAS DE SEGURIDAD:
  - shell=False siempre (los args son listas, nunca cadenas de shell).
  - Ningún argumento proviene del contenido del evento SMN.
  - Rutas y nombres de servicio son literales estáticos.
  - Los comandos destructivos están completamente ausentes.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

# Nombre → (ejecutable, args_fijos)
CommandDef = Tuple[str, List[str]]

# Comandos agrupados por tipo de alarma
_COMMON: List[CommandDef] = [
    ("uptime",     ["uptime"]),
    ("hostname",   ["hostname"]),
    ("uname",      ["uname", "-a"]),
    ("date",       ["date"]),
    ("timedatectl", ["timedatectl", "status"]),
]

_CPU: List[CommandDef] = [
    ("top_snapshot", ["top", "-b", "-n", "1"]),
    ("ps_cpu",       ["ps", "aux", "--sort=-%cpu"]),
    ("loadavg",      ["cat", "/proc/loadavg"]),
    ("vmstat",       ["vmstat", "-s"]),
]

_MEMORY: List[CommandDef] = [
    ("free",     ["free", "-h"]),
    ("meminfo",  ["cat", "/proc/meminfo"]),
    ("vmstat",   ["vmstat", "-s"]),
    ("ps_mem",   ["ps", "aux", "--sort=-%mem"]),
]

_DISK: List[CommandDef] = [
    ("df",       ["df", "-h"]),
    ("lsblk",    ["lsblk"]),
    ("iostat",   ["iostat", "-x", "1", "3"]),
    ("du_varlog", ["du", "-sh", "/var/log"]),
    ("du_tmp",   ["du", "-sh", "/tmp"]),
]

_NETWORK: List[CommandDef] = [
    ("ss",       ["ss", "-tunlp"]),
    ("ip_addr",  ["ip", "addr"]),
    ("ip_route", ["ip", "route"]),
    ("netstat_s", ["netstat", "-s"]),
]

_LOGS: List[CommandDef] = [
    ("journal",  ["journalctl", "-n", "100", "--no-pager"]),
    ("syslog",   ["tail", "-n", "100", "/var/log/syslog"]),
    ("messages", ["tail", "-n", "100", "/var/log/messages"]),
]

_SERVICES: List[CommandDef] = [
    ("failed_units",  ["systemctl", "list-units", "--state=failed", "--no-pager"]),
    ("sshd_status",   ["systemctl", "status", "sshd", "--no-pager"]),
    ("cron_status",   ["systemctl", "status", "cron", "--no-pager"]),
    ("rsyslog_status", ["systemctl", "status", "rsyslog", "--no-pager"]),
]

COMMANDS_BY_TYPE: Dict[str, List[CommandDef]] = {
    "cpu":     _COMMON + _CPU + _SERVICES,
    "memory":  _COMMON + _MEMORY + _SERVICES,
    "disk":    _COMMON + _DISK + _LOGS,
    "network": _COMMON + _NETWORK + _LOGS,
    "unknown": _COMMON + _CPU + _MEMORY + _DISK + _NETWORK,
}

# Lista blanca de nombres de comando permitidos (para doble verificación en executor)
ALLOWED_EXECUTABLES = frozenset({
    "uptime", "hostname", "uname", "date", "timedatectl",
    "top", "ps", "cat", "vmstat",
    "free", "df", "lsblk", "iostat", "du",
    "ss", "ip", "netstat",
    "journalctl", "tail",
    "systemctl",
})


def commands_for(alarm_type: str) -> List[CommandDef]:
    return COMMANDS_BY_TYPE.get(alarm_type, COMMANDS_BY_TYPE["unknown"])
