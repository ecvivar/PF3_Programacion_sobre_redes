"""Servidor coordinador TCP — ETAPAS 2-4 PFO3.

Despacho definitivo por op (Etapa 4):
  registro, login, logout               (sin token)
  enviar_tarea, consultar_tarea,        (con token válido)
  listar_tareas
Op desconocida -> op_desconocida. Sin eco temporal.

Uso:
    python servidor/server.py            # 127.0.0.1:5000
    python -m servidor.server            # idem, desde la raíz
Apagado: Ctrl+C (cierre controlado).
NOTA: token sobre TCP sin cifrado — solo 127.0.0.1. TLS para redes externas.
"""

from __future__ import annotations

import socket
import sys
import threading
from pathlib import Path

# El servidor vive en servidor/; el protocolo en compartido/ (raíz del proyecto).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compartido.protocolo import (
    LineReader,
    MessageTooLargeError,
    InvalidMessageError,
    decode_message,
    validate_envelope,
    success_response,
    error_response,
    send_message,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5000

OPS_CONOCIDAS = {"registro", "login", "logout", "enviar_tarea", "consultar_tarea", "listar_tareas"}


def log(msg: str) -> None:
    print(f"[servidor] {msg}", flush=True)


def _responder_desde_linea(conexion: socket.socket, linea: bytes, motor=None) -> bool:
    """Procesa una línea cruda y envía respuesta.

    motor: TaskEngine (Etapa 3+). Sin motor solo registro/login funcionan
    si se pasa db_path vía motor; en tests se inyecta motor real.
    """
    from servidor.auth import registrar_usuario, login, validar_sesion, invalidar_sesion, AuthError
    from servidor.database import (
        listar_tareas_usuario, obtener_tarea_usuario,
    )

    try:
        obj = decode_message(linea)
    except MessageTooLargeError as e:
        send_message(conexion, error_response(None, "mensaje_demasiado_grande", str(e)))
        return False
    except InvalidMessageError as e:
        send_message(conexion, error_response(None, "json_invalido", str(e)))
        return True

    try:
        req_id, op = validate_envelope(obj)
    except InvalidMessageError as e:
        from compartido.protocolo import extract_req_id

        rid = extract_req_id(obj)
        send_message(conexion, error_response(rid, "sobre_invalido", str(e)))
        return True

    if op not in OPS_CONOCIDAS:
        send_message(conexion, error_response(req_id, "op_desconocida", f"operación desconocida: {op}"))
        return True

    if motor is None:
        send_message(conexion, error_response(req_id, "error_interno", "servidor sin motor de tareas"))
        return True
    db = motor.db_path

    try:
        if op == "registro":
            r = registrar_usuario(obj.get("usuario"), obj.get("contraseña"), db)
            send_message(conexion, success_response(
                req_id, mensaje="Usuario registrado correctamente", usuario=r["usuario"]))

        elif op == "login":
            r = login(obj.get("usuario"), obj.get("contraseña"), db)
            send_message(conexion, success_response(
                req_id, mensaje="Inicio de sesión exitoso", usuario=r["usuario"],
                token=r["token"], expira=r["expira"]))

        elif op in ("logout", "enviar_tarea", "consultar_tarea", "listar_tareas"):
            sesion = validar_sesion(obj.get("token"), db)  # AuthError si falla
            uid = sesion["usuario_id"]

            if op == "logout":
                invalidar_sesion(obj["token"], db)
                send_message(conexion, success_response(req_id, mensaje="Sesión cerrada correctamente"))

            elif op == "enviar_tarea":
                tarea = motor.enviar_tarea(obj.get("tipo"), obj.get("payload"), usuario_id=uid)
                send_message(conexion, success_response(
                    req_id, task_id=tarea["task_id"], estado=tarea["estado"]))

            elif op == "consultar_tarea":
                task_id = obj.get("task_id")
                if not isinstance(task_id, str) or not task_id:
                    send_message(conexion, error_response(req_id, "payload_invalido", "falta task_id"))
                else:
                    tarea = obtener_tarea_usuario(task_id, uid, db)
                    if tarea is None:  # inexistente o ajena: mismo error
                        send_message(conexion, error_response(req_id, "tarea_no_encontrada", "la tarea no existe"))
                    else:
                        send_message(conexion, success_response(
                            req_id, task_id=tarea["task_id"], estado=tarea["estado"],
                            resultado=tarea["resultado"], error=tarea["error"],
                            tipo=tarea["tipo"]))

            else:  # listar_tareas (limitado: la respuesta debe caber en 64 KB)
                tareas = listar_tareas_usuario(uid, db)[:100]
                send_message(conexion, success_response(
                    req_id, tareas=[{"task_id": t["task_id"], "estado": t["estado"],
                                      "tipo": t["tipo"]} for t in tareas]))

    except AuthError as e:
        send_message(conexion, error_response(req_id, e.codigo, str(e)))
    except ValueError as e:
        send_message(conexion, error_response(req_id, "payload_invalido", str(e)))
    except Exception as e:
        log(f"error interno procesando {op}: {e}")
        send_message(conexion, error_response(req_id, "error_interno", "error interno del servidor"))
    return True


def handle_client(conexion: socket.socket, addr, motor=None) -> None:
    """Atiende una conexión: bucle de lectura secuencial."""
    log(f"cliente conectado desde {addr}")
    reader = LineReader(conexion)
    try:
        conexion.settimeout(30)
        while True:
            try:
                linea = reader.readline()
            except MessageTooLargeError as e:
                try:
                    send_message(
                        conexion,
                        error_response(None, "mensaje_demasiado_grande", str(e)),
                    )
                except OSError:
                    pass
                break
            except InvalidMessageError as e:
                # Mensaje incompleto por desconexión: no hay a quién responder.
                log(f"mensaje incompleto de {addr}: {e}")
                break
            except OSError as e:
                log(f"error de lectura con {addr}: {e}")
                break

            if linea is None:  # EOF limpio
                break
            if linea.strip() == b"":
                continue  # ignora línea vacía

            try:
                if not _responder_desde_linea(conexion, linea, motor):
                    break  # mensaje demasiado grande: cerrar
            except OSError as e:
                log(f"error de escritura con {addr}: {e}")
                break
            except Exception as e:  # p.ej. respuesta >64KB: cierre limpio, no caída
                log(f"error enviando respuesta a {addr}: {e}")
                break
    finally:
        log(f"cliente desconectado {addr}")
        try:
            conexion.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        conexion.close()


def create_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(50)
    sock.settimeout(0.5)  # permite chequeo periódico de apagado
    return sock


def serve_forever(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, stop_event: threading.Event | None = None, motor=None) -> None:
    """Bucle de aceptación (usado por main y por tests de integración)."""
    server = create_server(host, port)
    actual_port = server.getsockname()[1]
    log(f"escuchando en {host}:{actual_port}")
    try:
        while stop_event is None or not stop_event.is_set():
            try:
                conexion, addr = server.accept()
            except socket.timeout:
                continue
            except OSError as e:
                if stop_event is not None and stop_event.is_set():
                    break
                log(f"error en accept: {e}")
                continue
            t = threading.Thread(target=handle_client, args=(conexion, addr, motor), daemon=True)
            t.start()
    except KeyboardInterrupt:
        log("apagado solicitado por el usuario (Ctrl+C)")
    finally:
        try:
            server.close()
        except OSError:
            pass
        log("servidor detenido")


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    host = argv[0] if len(argv) > 0 else DEFAULT_HOST
    port = int(argv[1]) if len(argv) > 1 else DEFAULT_PORT
    from servidor.database import DEFAULT_DB_PATH
    from servidor.workers import TaskEngine

    motor = TaskEngine(DEFAULT_DB_PATH, num_workers=4)
    motor.iniciar()
    try:
        serve_forever(host, port, motor=motor)
    finally:
        motor.detener()


if __name__ == "__main__":
    main()
