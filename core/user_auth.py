# coding: utf-8
"""Autenticación de usuarios: contraseñas (PBKDF2), OTP por email, sesiones en BD.

Variables de entorno para email:
    SMTP_HOST        servidor SMTP (p.ej. smtp.gmail.com); vacío → solo log de consola
    SMTP_PORT        puerto (por defecto 587)
    SMTP_USER        usuario SMTP
    SMTP_PASSWORD    contraseña SMTP
    SMTP_FROM        dirección de remitente (por defecto = SMTP_USER)
"""

from __future__ import annotations

import email.mime.multipart
import email.mime.text
import hashlib
import logging
import os
import secrets
import smtplib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

SESSION_COOKIE = "inventory_session"
SESSION_DURATION_HOURS = 24
OTP_DURATION_MINUTES = 15
_PBKDF2_ITERATIONS = 260_000


# ── contraseñas ────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """Devuelve ``pbkdf2_sha256$iterations$salt$hash`` (todo hex/decimal)."""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Compara en tiempo constante para evitar timing attacks."""
    try:
        _algo, iterations_s, salt, expected_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations_s))
        return secrets.compare_digest(dk.hex(), expected_hex)
    except Exception:
        return False


# ── OTP ────────────────────────────────────────────────────────────────────────

def generate_otp() -> str:
    """Código de 6 dígitos criptográficamente aleatorio."""
    return str(secrets.randbelow(1_000_000)).zfill(6)


def send_otp_email(to_email: str, otp: str) -> bool:
    """Envía el OTP por correo.  Si SMTP_HOST no está configurado, imprime en log."""
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        logger.info("━━ OTP para %s: %s (configura SMTP_HOST para enviar por correo)", to_email, otp)
        return True
    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
        user = os.environ.get("SMTP_USER", "")
        password = os.environ.get("SMTP_PASSWORD", "")
        from_addr = os.environ.get("SMTP_FROM", user) or user
        msg = email.mime.multipart.MIMEMultipart("alternative")
        msg["Subject"] = "Código de verificación · Huawei Cloud Inventory"
        msg["From"] = from_addr
        msg["To"] = to_email
        text_body = f"Tu código de verificación es: {otp}\n\nExpira en {OTP_DURATION_MINUTES} minutos."
        html_body = (
            f"<p style='font-family:sans-serif'>Tu código de verificación para "
            f"<strong>Huawei Cloud Inventory</strong> es:</p>"
            f"<p style='font-family:monospace;font-size:2rem;letter-spacing:.25em'><strong>{otp}</strong></p>"
            f"<p style='font-family:sans-serif;color:#6b7280'>Expira en {OTP_DURATION_MINUTES} minutos. "
            f"Si no lo solicitaste, ignora este mensaje.</p>"
        )
        msg.attach(email.mime.text.MIMEText(text_body, "plain"))
        msg.attach(email.mime.text.MIMEText(html_body, "html"))
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            if user and password:
                smtp.login(user, password)
            smtp.sendmail(from_addr, [to_email], msg.as_string())
        return True
    except Exception as exc:
        logger.error("Error al enviar OTP a %s: %s", to_email, exc)
        return False


# ── sesiones ───────────────────────────────────────────────────────────────────

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Session, user_id: uuid.UUID) -> str:
    """Crea una sesión en BD y devuelve el token en claro (para la cookie)."""
    from db.models import UserSession  # evita importación circular al nivel de módulo
    token = secrets.token_urlsafe(32)
    session = UserSession(
        user_id=user_id,
        token_hash=_token_hash(token),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=SESSION_DURATION_HOURS),
    )
    db.add(session)
    db.commit()
    return token


def lookup_session(db: Session, token: str) -> Optional["db.models.User"]:  # type: ignore[name-defined]
    """Devuelve el User si la sesión es válida, o None."""
    from db.models import User, UserSession
    if not token:
        return None
    th = _token_hash(token)
    now = datetime.now(timezone.utc)
    row = db.scalars(
        select(UserSession)
        .where(UserSession.token_hash == th)
        .where(UserSession.expires_at > now)
        .where(UserSession.revoked.is_(False))
    ).first()
    if row is None:
        return None
    user = db.get(User, row.user_id)
    return user if (user and user.is_active) else None


def revoke_session(db: Session, token: str) -> None:
    from db.models import UserSession
    th = _token_hash(token)
    row = db.scalars(select(UserSession).where(UserSession.token_hash == th)).first()
    if row:
        row.revoked = True
        db.commit()


def lookup_session_from_db(token: str) -> Optional["db.models.User"]:  # type: ignore[name-defined]
    """Versión sin Session (abre y cierra su propia sesión). Para middleware."""
    from db.session import session_scope
    try:
        with session_scope() as db:
            return lookup_session(db, token)
    except Exception:
        return None
