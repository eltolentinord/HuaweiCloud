# coding: utf-8
"""Errores de dominio con código HTTP asociado (mensajes sin secretos)."""

from __future__ import annotations


class TenancyError(Exception):
    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(TenancyError):
    status_code = 404


class ConflictError(TenancyError):
    status_code = 409


class InvalidStateError(TenancyError):
    status_code = 409


class ValidationFailedError(TenancyError):
    status_code = 422
