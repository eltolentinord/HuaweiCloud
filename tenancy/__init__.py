# coding: utf-8
"""Capa de dominio multi-cliente: clientes, cuentas Huawei, proyectos y catálogo.

Todas las funciones reciben una ``Session`` y no hacen commit (lo decide quien
llama, p. ej. ``db.session.session_scope`` o la dependencia FastAPI ``get_db``).
Las consultas de cuentas/proyectos se filtran SIEMPRE por ``client_id`` para
garantizar el aislamiento entre clientes.
"""
