"""Protocolo de comunicación PFO3 — JSON por línea sobre TCP.

Contrato (Etapa 2):
- Transporte: socket TCP, mensajes UTF-8 terminados en "\\n".
- Una línea = un objeto JSON. Se lee por buffer hasta "\\n", nunca
  asumiendo que un recv() entrega un mensaje completo.
- Límite: MAX_MESSAGE_SIZE bytes por línea (64 KB).
- Cada solicitud lleva "req_id" (string) y "op" (string).
- Cada respuesta repite "req_id" (o null si no pudo extraerse) y usa
  {"ok": true/false, "error": {"codigo", "mensaje"}} ante errores.

Limitación conocida (Etapa 2): el servidor cierra con
shutdown(SHUT_RDWR)+close(). Si el cliente deja datos sin leer en el
socket, el SO puede provocar un RST y el servidor registra
"error de lectura: WinError 10054". No afecta a la corrección; se
documenta como cierre abrupto evitable con drenado previo si se requiere.
"""

from __future__ import annotations

import json

MAX_MESSAGE_SIZE = 64 * 1024  # 64 KB


class ProtocolError(Exception):
    """Error base del protocolo."""


class MessageTooLargeError(ProtocolError):
    """La línea supera MAX_MESSAGE_SIZE."""


class InvalidMessageError(ProtocolError):
    """La línea no es un objeto JSON válido o no cumple el sobre."""


def encode_message(obj: dict) -> bytes:
    """Convierte un dict en bytes JSON + '\\n', validando límite.

    El límite (MAX_MESSAGE_SIZE) se mide sobre el JSON sin el delimitador
    final, de forma consistente con decode_message() y LineReader.
    """
    if not isinstance(obj, dict):
        raise InvalidMessageError("el mensaje debe ser un objeto JSON")
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_MESSAGE_SIZE:
        raise MessageTooLargeError(
            f"mensaje de {len(payload)} bytes supera el límite de {MAX_MESSAGE_SIZE}"
        )
    return payload + b"\n"


def decode_message(line: bytes | str) -> dict:
    """Convierte una línea JSON en dict, validando límite y forma."""
    raw = line.encode("utf-8") if isinstance(line, str) else line
    raw = raw.strip()
    if len(raw) > MAX_MESSAGE_SIZE:
        raise MessageTooLargeError(
            f"mensaje de {len(raw)} bytes supera el límite de {MAX_MESSAGE_SIZE}"
        )
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise InvalidMessageError(f"JSON inválido: {e}") from e
    if not isinstance(obj, dict):
        raise InvalidMessageError("el mensaje debe ser un objeto JSON")
    return obj


def validate_envelope(obj: dict) -> tuple[str, str]:
    """Valida req_id/op y los devuelve (req_id, op)."""
    req_id = obj.get("req_id")
    op = obj.get("op")
    # req_id debe ser string no vacío; op string no vacío.
    if not isinstance(req_id, str) or not req_id.strip():
        raise InvalidMessageError("falta 'req_id' (string no vacío)")
    if not isinstance(op, str) or not op.strip():
        raise InvalidMessageError("falta 'op' (string no vacío)")
    return req_id.strip(), op.strip()


def extract_req_id(obj: dict | None) -> str | None:
    """Extrae req_id si es válido, sino None (para responder errores)."""
    if isinstance(obj, dict):
        req_id = obj.get("req_id")
        if isinstance(req_id, str) and req_id.strip():
            return req_id.strip()
    return None


def success_response(req_id: str, **extra) -> dict:
    resp = {"req_id": req_id, "ok": True}
    resp.update(extra)
    return resp


def error_response(req_id: str | None, codigo: str, mensaje: str) -> dict:
    return {
        "req_id": req_id,
        "ok": False,
        "error": {"codigo": codigo, "mensaje": mensaje},
    }


class LineReader:
    """Lee mensajes delimitados por '\\n' desde un socket TCP.

    Acumula lo que llega por recv() en un buffer y extrae líneas
    completas. No asume que recv() = un mensaje.

    - readline() devuelve la línea (sin '\\n'), None ante EOF limpio,
      o lanza MessageTooLargeError / InvalidMessageError.
    - Si el cliente se desconecta con datos sin '\\n' pendientes,
      lanza InvalidMessageError('mensaje incompleto...').
    """

    def __init__(self, sock, max_size: int = MAX_MESSAGE_SIZE, recv_size: int = 4096):
        self._sock = sock
        self._max = max_size
        self._recv_size = recv_size
        self._buffer = bytearray()

    def readline(self) -> bytes | None:
        while True:
            idx = self._buffer.find(b"\n")
            if idx >= 0:
                line = bytes(self._buffer[:idx])
                del self._buffer[: idx + 1]
                if len(line) > self._max:
                    raise MessageTooLargeError(
                        f"línea de {len(line)} bytes supera el límite de {self._max}"
                    )
                # Ignora línea vacía (p.ej. doble salto) devolviendo b'' es
                # ambiguo; la tratamos como vacía y el llamador la ignora.
                return line
            # Sin salto completo: si el buffer ya supera el límite, rechazar
            # sin seguir acumulando.
            if len(self._buffer) > self._max:
                raise MessageTooLargeError(
                    f"mensaje supera el límite de {self._max} bytes"
                )
            chunk = self._sock.recv(self._recv_size)
            if not chunk:  # EOF
                if not self._buffer:
                    return None  # cierre limpio
                raise InvalidMessageError(
                    "mensaje incompleto: el cliente se desconectó sin enviar '\\n'"
                )
            self._buffer.extend(chunk)


def send_message(sock, obj: dict) -> None:
    """Envía un dict como JSON + '\\n'."""
    sock.sendall(encode_message(obj))


def receive_message(sock, reader: LineReader | None = None) -> dict | None:
    """Lee una línea del socket y la decodifica.

    Devuelve None ante EOF limpio. Omite líneas vacías.
    """
    r = reader or LineReader(sock)
    while True:
        line = r.readline()
        if line is None:
            return None
        if line.strip() == b"":
            continue
        return decode_message(line)
