# coding: utf-8
"""Capa de persistencia: único lugar con consultas SQLAlchemy.

- Los collectors no conocen la base de datos.
- ``tenancy/`` y ``scanning/`` aplican reglas de negocio y llaman a estos repositorios.
- Ninguna función hace commit: la transacción la controla quien llama.
"""
