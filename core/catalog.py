# coding: utf-8
"""Catálogo de regiones y servicios soportados."""

from __future__ import annotations

REGIONES = [
    {"id": "af-north-1", "nombre": "AF-Cairo"},
    {"id": "ap-southeast-1", "nombre": "CN-Hong Kong"},
    {"id": "ap-southeast-2", "nombre": "AP-Bangkok"},
    {"id": "ap-southeast-3", "nombre": "AP-Singapore"},
    {"id": "cn-north-4", "nombre": "CN North-Beijing4"},
    {"id": "cn-south-1", "nombre": "CN South-Guangzhou"},
    {"id": "la-north-2", "nombre": "LA-Mexico City2"},
    {"id": "la-south-2", "nombre": "LA-Santiago"},
    {"id": "me-east-1", "nombre": "ME-Riyadh"},
    {"id": "na-mexico-1", "nombre": "LA-Mexico City1"},
    {"id": "sa-brazil-1", "nombre": "LA-Sao Paulo1"},
]

ALL_SERVICES = "todos"

# El orden define el orden de las tablas cuando se consultan todos los servicios.
SERVICIOS = [
    {"id": ALL_SERVICES, "nombre": "Todos los servicios"},
    {"id": "ecs", "nombre": "Elastic Cloud Server (ECS)"},
    {"id": "evs", "nombre": "Elastic Volume Service (EVS)"},
    {"id": "eip", "nombre": "Elastic IP (EIP)"},
    {"id": "obs", "nombre": "Object Storage Service (OBS)"},
    {"id": "cbr", "nombre": "Cloud Backup and Recovery (CBR)"},
    {"id": "elb", "nombre": "Elastic Load Balance (ELB)"},
    {"id": "rds", "nombre": "Relational Database Service (RDS)"},
    {"id": "dcs", "nombre": "Distributed Cache Service (DCS/Redis)"},
    {"id": "vpc", "nombre": "Virtual Private Cloud (VPC)"},
    {"id": "nat", "nombre": "NAT Gateway"},
    {"id": "vpn", "nombre": "Virtual Private Network (VPN)"},
    {"id": "ces", "nombre": "Cloud Eye"},
    {"id": "hss", "nombre": "Host Security Service (HSS)"},
    {"id": "waf", "nombre": "Web Application Firewall (WAF)"},
    {"id": "cfw", "nombre": "Cloud Firewall (CFW)"},
]
