# coding: utf-8
"""Módulo Cloud Eye Auto-Diagnóstico.

Flujo: SMN → POST /webhook/ces-alarm → CesAlarmEvent → DiagnosticIncident (worker) →
       SSH read-only → DiagnosticCommand / DiagnosticEvidence → analyzer → PDF.
"""
