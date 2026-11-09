"""Persistencia SQLite PFO3 — ETAPA 3.

Base de desarrollo independiente: `pfo3.db` en la raíz del proyecto
(la `tareas.db` de la PFO2 NO se usa ni se modifica).

Compatibilidad PFO2: la tabla `usuarios` se define igual que en PFO2
(id, usuario UNIQUE, contraseña) para que la PFO3 pueda reutilizar
cuentas si se importa la BD original. La tabla `tareas` de PFO2
(id, descripcion) era solo un esqueleto sin uso; PFO3 crea la suya
(task_id, ...) en la BD nueva.

Etapa 3: `usuario_id` es NULLABLE de forma temporal. En la Etapa 4
(autenticación) se exigirá no-nulo y se validará ownership.

Recuperación al reiniciar: PENDIENTE se reencola; EN_PROCESO se
revierte a PENDIENTE; COMPLETADA/ERROR nunca se re-ejecutan.
Garantía: recuperación básica, NO exactamente-una-vez ante fallos
arbitrarios (p.ej. caída entre fin del cómputo y commit del resultado
puede re-ejecutar una tarea una vez).
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# Raíz del proyecto = carpeta padre de servidor/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = str(PROJECT_ROOT / "pfo3.db")

ESTADOS_VALIDOS = ("PENDIENTE", "EN_PROCESO", "COMPLETADA", "ERROR")
TIPOS_VALIDOS = ("suma", "texto_mayusculas", "fibonacci")

# Límites razonables de entrada (evitan cómputos excesivos / bloqueos).
MAX_TEXTO_LEN = 10_000
MAX_FIBONACCI_N = 100

ESQUEMA = """
CREATE TABLE IF NOT EXISTS usuarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario TEXT UNIQUE NOT NULL,
    contraseña TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tareas (
    task_id TEXT PRIMARY KEY,
    usuario_id INTEGER NULL,
    tipo TEXT NOT NULL,
    payload TEXT NOT NULL,
    estado TEXT NOT NULL CHECK (estado IN ('PENDIENTE','EN_PROCESO','COMPLETADA','ERROR')),
    resultado TEXT NULL,
    error TEXT NULL,
    creada TEXT NOT NULL,
    iniciada TEXT NULL,
    finalizada TEXT NULL
);
CREATE TABLE IF NOT EXISTS sesiones (
    token TEXT PRIMARY KEY,
    usuario_id INTEGER NOT NULL REFERENCES usuarios(id),
    creada TEXT NOT NULL,
    expira TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tareas_estado ON tareas (estado);
CREATE INDEX IF NOT EXISTS idx_tareas_usuario ON tareas (usuario_id);
CREATE INDEX IF NOT EXISTS idx_sesiones_usuario ON sesiones (usuario_id);
"""


def ahora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def conectar_db(db_path: str = DEFAULT_DB_PATH):
    """Conexión SQLite corta y aislada por operación (thread-safe)."""
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def crear_tablas(db_path: str = DEFAULT_DB_PATH) -> None:
    with conectar_db(db_path) as conn:
        conn.executescript(ESQUEMA)


def validar_payload(tipo: str, payload) -> dict:
    """Valida tipo y carga útil según el contrato de Etapa 3."""
    if tipo not in TIPOS_VALIDOS:
        raise ValueError(f"tipo no soportado: {tipo!r}; válidos: {list(TIPOS_VALIDOS)}")
    if not isinstance(payload, dict):
        raise ValueError("payload debe ser un objeto JSON")
    if tipo == "suma":
        a, b = payload.get("a"), payload.get("b")
        for nombre, v in (("a", a), ("b", b)):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"suma: '{nombre}' debe ser número")
            if isinstance(v, float) and not math.isfinite(v):
                raise ValueError(f"suma: '{nombre}' debe ser finito")
    elif tipo == "texto_mayusculas":
        texto = payload.get("texto")
        if not isinstance(texto, str):
            raise ValueError("texto_mayusculas: 'texto' debe ser string")
        if len(texto) > MAX_TEXTO_LEN:
            raise ValueError(f"texto_mayusculas: máximo {MAX_TEXTO_LEN} caracteres")
    elif tipo == "fibonacci":
        n = payload.get("n")
        if isinstance(n, bool) or not isinstance(n, int):
            raise ValueError("fibonacci: 'n' debe ser entero")
        if not (0 <= n <= MAX_FIBONACCI_N):
            raise ValueError(f"fibonacci: 'n' debe estar entre 0 y {MAX_FIBONACCI_N}")
    return payload


def crear_tarea(tipo: str, payload: dict, usuario_id: int | None = None,
                db_path: str = DEFAULT_DB_PATH) -> dict:
    """Valida, persiste en PENDIENTE y devuelve la fila creada."""
    validar_payload(tipo, payload)
    task_id = str(uuid.uuid4())
    creada = ahora_iso()
    payload_json = json.dumps(payload, ensure_ascii=False)
    with conectar_db(db_path) as conn:
        conn.execute(
            "INSERT INTO tareas (task_id, usuario_id, tipo, payload, estado, creada)"
            " VALUES (?, ?, ?, ?, 'PENDIENTE', ?)",
            (task_id, usuario_id, tipo, payload_json, creada),
        )
    fila = obtener_tarea(task_id, db_path)
    assert fila is not None
    return fila


def obtener_tarea(task_id: str, db_path: str = DEFAULT_DB_PATH) -> dict | None:
    with conectar_db(db_path) as conn:
        fila = conn.execute(
            "SELECT * FROM tareas WHERE task_id = ?", (task_id,)
        ).fetchone()
    return _a_dict(fila) if fila else None


def listar_tareas(db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    with conectar_db(db_path) as conn:
        filas = conn.execute(
            "SELECT * FROM tareas ORDER BY creada ASC"
        ).fetchall()
    return [_a_dict(f) for f in filas]


def listar_tareas_usuario(usuario_id: int, db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    """Solo tareas del dueño (aislamiento por usuario)."""
    with conectar_db(db_path) as conn:
        filas = conn.execute(
            "SELECT * FROM tareas WHERE usuario_id = ? ORDER BY creada ASC",
            (usuario_id,),
        ).fetchall()
    return [_a_dict(f) for f in filas]


def obtener_tarea_usuario(task_id: str, usuario_id: int,
                          db_path: str = DEFAULT_DB_PATH) -> dict | None:
    """Tarea solo si pertenece a usuario_id; None si no existe o es ajena.

    Devolver None en ambos casos evita revelar si existe otra cuenta.
    """
    with conectar_db(db_path) as conn:
        fila = conn.execute(
            "SELECT * FROM tareas WHERE task_id = ? AND usuario_id = ?",
            (task_id, usuario_id),
        ).fetchone()
    return _a_dict(fila) if fila else None


def listar_pendientes(db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    """PENDIENTES + EN_PROCESO interrumpidos (para recuperación)."""
    with conectar_db(db_path) as conn:
        filas = conn.execute(
            "SELECT * FROM tareas WHERE estado IN ('PENDIENTE','EN_PROCESO')"
            " ORDER BY creada ASC"
        ).fetchall()
    return [_a_dict(f) for f in filas]


def reclamar_tarea(task_id: str, db_path: str = DEFAULT_DB_PATH) -> dict | None:
    """Transición atómica PENDIENTE -> EN_PROCESO.

    Devuelve la tarea reclamada o None si ya no estaba PENDIENTE
    (otro worker la tomó o ya terminó). Solo un reclamador gana.
    """
    with conectar_db(db_path) as conn:
        cur = conn.execute(
            "UPDATE tareas SET estado='EN_PROCESO', iniciada=? "
            "WHERE task_id=? AND estado='PENDIENTE'",
            (ahora_iso(), task_id),
        )
        if cur.rowcount == 0:
            return None
    return obtener_tarea(task_id, db_path)


def finalizar_tarea(task_id: str, resultado, error: str | None = None,
                    db_path: str = DEFAULT_DB_PATH) -> dict:
    """Marca COMPLETADA (resultado) o ERROR (mensaje) con fecha de fin."""
    finalizada = ahora_iso()
    with conectar_db(db_path) as conn:
        if error is None:
            conn.execute(
                "UPDATE tareas SET estado='COMPLETADA', resultado=?, error=NULL,"
                " finalizada=? WHERE task_id=?",
                (json.dumps(resultado, ensure_ascii=False), finalizada, task_id),
            )
        else:
            conn.execute(
                "UPDATE tareas SET estado='ERROR', resultado=NULL, error=?,"
                " finalizada=? WHERE task_id=?",
                (error, finalizada, task_id),
            )
    fila = obtener_tarea(task_id, db_path)
    assert fila is not None
    return fila


def revertir_en_proceso(db_path: str = DEFAULT_DB_PATH) -> int:
    """Revierte EN_PROCESO -> PENDIENTE (recuperación tras apagado)."""
    with conectar_db(db_path) as conn:
        cur = conn.execute(
            "UPDATE tareas SET estado='PENDIENTE', iniciada=NULL"
            " WHERE estado='EN_PROCESO'"
        )
        return cur.rowcount


def _a_dict(fila: sqlite3.Row | None) -> dict:
    if fila is None:
        return None
    d = dict(fila)
    # Decodifica payload/resultado JSON para el llamador.
    try:
        d["payload"] = json.loads(d["payload"]) if d["payload"] else None
    except json.JSONDecodeError:
        pass
    if d.get("resultado") is not None:
        try:
            d["resultado"] = json.loads(d["resultado"])
        except json.JSONDecodeError:
            pass
    return d
