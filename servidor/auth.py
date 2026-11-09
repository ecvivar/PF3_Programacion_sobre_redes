"""Autenticación y sesiones — ETAPA 4 PFO3.

- Registro/login con Werkzeug (enfoque PFO2), consultas parametrizadas.
- Sesiones persistentes con expiración de 1 hora (`sesiones`).
- **Seguridad en local**: el token viaja por TCP sin cifrado. Válido para
  127.0.0.1, INAPROPIADO para redes externas sin TLS. Nunca se loguean
  contraseñas ni tokens.

Trazabilidad PFO2: el hash usa generate/check_password_hash de Werkzeug
y la tabla `usuarios(id, usuario UNIQUE, contraseña)` es la misma.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash, check_password_hash

from servidor.database import conectar_db, crear_tablas, ahora_iso

SESS_EXPIRA_HORAS = 1
MIN_USUARIO, MAX_USUARIO = 1, 50
MIN_CLAVE, MAX_CLAVE = 4, 128  # PFO2 admite "1234" (4 chars)


class AuthError(ValueError):
    """Error de autenticación con código estable para el protocolo."""

    def __init__(self, codigo: str, mensaje: str):
        super().__init__(mensaje)
        self.codigo = codigo


def _validar_credenciales(usuario, contraseña) -> tuple[str, str]:
    if not isinstance(usuario, str) or not isinstance(contraseña, str):
        raise AuthError("credenciales_invalidas", "Usuario y contraseña deben ser textos")
    usuario = usuario.strip()
    if not (MIN_USUARIO <= len(usuario) <= MAX_USUARIO):
        raise AuthError("credenciales_invalidas", "Usuario o contraseña incorrectos")
    if not (MIN_CLAVE <= len(contraseña) <= MAX_CLAVE):
        # En registro se revela la regla; en login se usa el mensaje genérico.
        raise AuthError("payload_invalido", f"La contraseña debe tener entre {MIN_CLAVE} y {MAX_CLAVE} caracteres")
    return usuario, contraseña


def registrar_usuario(usuario, contraseña, db_path: str) -> dict:
    """Registra usuario. Nunca devuelve contraseña ni hash."""
    usuario, contraseña = _validar_credenciales(usuario, contraseña)
    # Mensaje de registro más específico para longitud (auditable en cliente).
    try:
        from servidor.database import conectar_db as _c

        with _c(db_path) as conn:
            conn.execute(
                "INSERT INTO usuarios (usuario, contraseña) VALUES (?, ?)",
                (usuario, generate_password_hash(contraseña)),
            )
    except sqlite3.IntegrityError:
        raise AuthError("usuario_duplicado", "El usuario ya existe")
    return {"usuario": usuario}


def obtener_usuario_por_nombre(usuario: str, db_path: str) -> dict | None:
    with conectar_db(db_path) as conn:
        fila = conn.execute(
            "SELECT id, usuario, contraseña FROM usuarios WHERE usuario = ?",
            (usuario,),
        ).fetchone()
    return dict(fila) if fila else None


def login(usuario, contraseña, db_path: str) -> dict:
    """Verifica credenciales y crea sesión. Error genérico si falla."""
    # Validación laxa: cualquier forma inválida = genérico (no filtra).
    if not isinstance(usuario, str) or not isinstance(contraseña, str):
        raise AuthError("credenciales_invalidas", "Usuario o contraseña incorrectos")
    usuario = usuario.strip()
    if not usuario or not contraseña:
        raise AuthError("credenciales_invalidas", "Usuario o contraseña incorrectos")
    fila = obtener_usuario_por_nombre(usuario, db_path)
    if not fila or not check_password_hash(fila["contraseña"], contraseña):
        raise AuthError("credenciales_invalidas", "Usuario o contraseña incorrectos")
    sesion = _crear_sesion(fila["id"], db_path)
    return {"usuario": fila["usuario"], "token": sesion["token"],
            "expira": sesion["expira"]}


# -- sesiones ---------------------------------------------------------------
def _crear_sesion(usuario_id: int, db_path: str) -> dict:
    token = secrets.token_hex(32)
    creada = datetime.now(timezone.utc)
    expira = creada + timedelta(hours=SESS_EXPIRA_HORAS)
    with conectar_db(db_path) as conn:
        conn.execute(
            "INSERT INTO sesiones (token, usuario_id, creada, expira) VALUES (?, ?, ?, ?)",
            (token, usuario_id, creada.isoformat(), expira.isoformat()),
        )
    return {"token": token, "creada": creada.isoformat(), "expira": expira.isoformat()}


def validar_sesion(token, db_path: str) -> dict:
    """Devuelve {'usuario_id','usuario'} o lanza AuthError (requerido)."""
    if not isinstance(token, str) or not token.strip():
        raise AuthError("autenticacion_requerida", "Se requiere autenticación")
    with conectar_db(db_path) as conn:
        fila = conn.execute(
            "SELECT s.usuario_id AS usuario_id, s.expira AS expira, u.usuario AS usuario"
            " FROM sesiones s JOIN usuarios u ON u.id = s.usuario_id"
            " WHERE s.token = ?",
            (token.strip(),),
        ).fetchone()
    if fila is None:
        raise AuthError("autenticacion_requerida", "Sesión inexistente o inválida")
    # Comparación de expiración en UTC (ISO8601 con timezone).
    try:
        expira = datetime.fromisoformat(fila["expira"])
    except (ValueError, TypeError):
        raise AuthError("autenticacion_requerida", "Sesión inexistente o inválida")
    if expira.tzinfo is None:  # naive guardado a mano: inválido, no crash
        raise AuthError("autenticacion_requerida", "Sesión inexistente o inválida")
    if expira <= datetime.now(timezone.utc):
        invalidar_sesion(token.strip(), db_path)
        raise AuthError("autenticacion_requerida", "La sesión expiró; inicie sesión de nuevo")
    return {"usuario_id": fila["usuario_id"], "usuario": fila["usuario"]}


def invalidar_sesion(token: str, db_path: str) -> bool:
    """Elimina la sesión. True si existía."""
    with conectar_db(db_path) as conn:
        cur = conn.execute("DELETE FROM sesiones WHERE token = ?", (token,))
        return cur.rowcount > 0


def limpiar_sesiones_expiradas(db_path: str) -> int:
    with conectar_db(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM sesiones WHERE expira <= ?", (ahora_iso(),))
        return cur.rowcount
