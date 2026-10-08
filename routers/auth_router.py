# coding: utf-8
"""Rutas de autenticación: login con email+contraseña y código OTP por email.

Flujo:
    GET  /login                → formulario de email y contraseña
    POST /login                → valida credenciales, genera OTP, lo envía por email
    GET  /login/verificar      → formulario del código OTP
    POST /login/verificar      → valida OTP, crea sesión, redirige al destino
    POST /logout               → revoca sesión, borra cookie

Variables de entorno para SMTP (opcionales — sin ellas el OTP se imprime en el log):
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote, unquote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.user_auth import (
    SESSION_COOKIE, SESSION_DURATION_HOURS,
    OTP_DURATION_MINUTES,
    create_session, generate_otp, lookup_session,
    revoke_session, send_otp_email, verify_password,
)
from db.models import User
from db.session import get_db

router = APIRouter(tags=["auth"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

_SAFE_NEXT_PREFIXES = ("/clientes", "/dashboard", "/calculadora", "/costos", "/servidores", "/diagnosticos", "/")


def _safe_next(raw: str) -> str:
    """Valida que ``next`` sea una ruta interna (no un open redirect)."""
    path = unquote(raw or "").strip()
    if any(path == p or path.startswith(p + "/") or path.startswith(p + "?")
           for p in _SAFE_NEXT_PREFIXES):
        return path
    return "/clientes"


# ── páginas ────────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "", error: str = ""):
    return templates.TemplateResponse(
        request, "login.html",
        {"next": quote(next, safe=""), "error": error},
    )


@router.get("/login/verificar", response_class=HTMLResponse)
def otp_page(request: Request, email: str = "", next: str = "", error: str = ""):
    if not email:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(
        request, "login_otp.html",
        {"email": email, "next": quote(next, safe=""), "error": error},
    )


# ── acciones ───────────────────────────────────────────────────────────────────

@router.post("/login")
def login_submit(
    request: Request,
    email_input: str = Form(..., alias="email"),
    password: str = Form(...),
    next_url: str = Form("", alias="next"),
    db: Session = Depends(get_db),
):
    from datetime import timedelta

    email_lower = email_input.strip().lower()
    user = db.scalars(select(User).where(User.email == email_lower)).first()

    # Credenciales incorrectas: mismo mensaje para no revelar si el email existe
    if not user or not user.password_hash or not verify_password(password, user.password_hash):
        return RedirectResponse(
            f"/login?error=Correo+o+contraseña+incorrectos&next={quote(next_url, safe='')}",
            status_code=302,
        )
    if not user.is_active:
        return RedirectResponse(
            f"/login?error=Cuenta+desactivada&next={quote(next_url, safe='')}",
            status_code=302,
        )

    # Generar y guardar OTP
    otp = generate_otp()
    user.otp_code = otp
    user.otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_DURATION_MINUTES)
    db.commit()

    # Enviar OTP (si SMTP no está configurado, va al log)
    send_otp_email(user.email, otp)

    return RedirectResponse(
        f"/login/verificar?email={quote(email_lower, safe='')}&next={quote(next_url, safe='')}",
        status_code=302,
    )


@router.post("/login/verificar")
def otp_submit(
    request: Request,
    email_form: str = Form(..., alias="email"),
    otp_input: str = Form(..., alias="otp"),
    next_url: str = Form("", alias="next"),
    db: Session = Depends(get_db),
):
    email_lower = email_form.strip().lower()
    user = db.scalars(select(User).where(User.email == email_lower)).first()
    now = datetime.now(timezone.utc)

    otp_exp = user.otp_expires_at if user and user.otp_expires_at else None
    if otp_exp is not None and otp_exp.tzinfo is None:
        otp_exp = otp_exp.replace(tzinfo=timezone.utc)
    invalid = (
        not user
        or not user.otp_code
        or otp_exp is None
        or otp_exp < now
        or user.otp_code.strip() != otp_input.strip()
    )
    if invalid:
        return RedirectResponse(
            f"/login/verificar?email={quote(email_lower, safe='')}"
            f"&next={quote(next_url, safe='')}&error=Código+incorrecto+o+expirado",
            status_code=302,
        )

    # OTP válido → limpiar y crear sesión
    user.otp_code = None
    user.otp_expires_at = None
    token = create_session(db, user.id)

    destination = _safe_next(next_url) or "/clientes"
    response = RedirectResponse(destination, status_code=302)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        secure=False,  # True en producción con HTTPS
        max_age=SESSION_DURATION_HOURS * 3600,
    )
    return response


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        revoke_session(db, token)
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/logout")
def logout_get(request: Request, db: Session = Depends(get_db)):
    return logout(request, db)
