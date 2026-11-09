"""Cliente de consola TCP — ETAPA 4 PFO3.

Conserva el estilo del menú PFO2. Menús:
  sin sesión:  1 Registrar  2 Iniciar sesión  3 Salir
  con sesión:  1 Enviar tarea  2 Listar mis tareas  3 Consultar resultado
              4 Cerrar sesión  5 Salir

- Una conexión TCP persistente; JSON por línea (compartido.protocolo).
- req_id uuid4 por solicitud; token SOLO en memoria (nunca en disco/log).
- NOTA: TCP local sin cifrado — no usar en redes externas sin TLS.
"""

from __future__ import annotations

import getpass
import socket
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compartido.protocolo import (
    LineReader, InvalidMessageError, MessageTooLargeError, send_message,
)

HOST = "127.0.0.1"
PORT = 5000


class ClienteTCP:
    def __init__(self, host: str = HOST, port: int = PORT, timeout: int = 10):
        self.host, self.port, self.timeout = host, port, timeout
        self.sock: socket.socket | None = None
        self.reader: LineReader | None = None
        self.token: str | None = None
        self.usuario: str | None = None

    def conectar(self) -> bool:
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self.reader = LineReader(self.sock)
            return True
        except OSError as e:
            print(f"\nError: no se pudo conectar con el servidor ({e}).")
            return False

    def cerrar(self) -> None:
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
            self.sock = None
            self.reader = None

    def enviar(self, op: str, **datos) -> dict | None:
        """Envía una solicitud y devuelve la respuesta dict (None si falla)."""
        if self.sock is None or self.reader is None:
            print("\nError: sin conexión con el servidor.")
            return None
        req = {"req_id": str(uuid.uuid4()), "op": op, **datos}
        if self.token and op not in ("registro", "login"):
            req["token"] = self.token
        try:
            send_message(self.sock, req)
            while True:
                linea = self.reader.readline()
                if linea is None:
                    print("\nError: el servidor cerró la conexión.")
                    return None
                if linea.strip() == b"":
                    continue
                from compartido.protocolo import decode_message

                return decode_message(linea)
        except MessageTooLargeError as e:
            print(f"\nError: mensaje demasiado grande ({e}).")
        except InvalidMessageError as e:
            print(f"\nError: respuesta no válida ({e}).")
        except OSError as e:
            print(f"\nError: problema de conexión ({e}).")
        return None

    # -- operaciones de consola -------------------------------------------
    def registrar(self) -> None:
        print("\n--- REGISTRO DE USUARIO ---")
        usuario = input("Usuario: ").strip()
        clave = getpass.getpass("Contraseña: ")
        if not usuario or not clave:
            print("\nUsuario y contraseña son obligatorios.")
            return
        resp = self.enviar("registro", usuario=usuario, contraseña=clave)
        if resp is None:
            return
        if resp["ok"]:
            print(f"\n{resp.get('mensaje', 'Usuario registrado correctamente')}")
        else:
            print(f"\nError: {resp['error']['mensaje']}")

    def iniciar_sesion(self) -> bool:
        print("\n--- INICIO DE SESIÓN ---")
        usuario = input("Usuario: ").strip()
        clave = getpass.getpass("Contraseña: ")
        if not usuario or not clave:
            print("\nUsuario y contraseña son obligatorios.")
            return False
        resp = self.enviar("login", usuario=usuario, contraseña=clave)
        if resp is None:
            return False
        if resp["ok"]:
            self.token = resp["token"]  # solo en memoria
            self.usuario = resp.get("usuario", usuario)
            print(f"\n{resp.get('mensaje', 'Inicio de sesión exitoso')}")
            print(f"Bienvenido, {self.usuario}. (El token no se muestra.)")
            return True
        print(f"\nError: {resp['error']['mensaje']}")
        return False

    def cerrar_sesion(self) -> None:
        resp = self.enviar("logout")
        if resp and resp["ok"]:
            print(f"\n{resp.get('mensaje', 'Sesión cerrada correctamente')}")
        else:
            print("\nLa sesión ya no es válida; se desconectó localmente.")
        self.token = None
        self.usuario = None

    def enviar_tarea(self) -> None:
        print("\n--- ENVIAR TAREA ---")
        print("1. Suma")
        print("2. Texto en mayúsculas")
        print("3. Fibonacci")
        opcion = input("Seleccione tipo (1-3): ").strip()
        tipo, payload = None, None
        if opcion == "1":
            tipo = "suma"
            a_txt = input("Primer número: ").strip()
            b_txt = input("Segundo número: ").strip()
            try:
                def _num(s):
                    return float(s) if ("." in s or "e" in s.lower()) else int(s)
                payload = {"a": _num(a_txt), "b": _num(b_txt)}
            except ValueError:
                print("\nError: ambos valores deben ser números.")
                return
        elif opcion == "2":
            tipo = "texto_mayusculas"
            texto = input("Texto: ")
            if len(texto) > 10000:
                print("\nError: el texto es demasiado largo.")
                return
            payload = {"texto": texto}
        elif opcion == "3":
            tipo = "fibonacci"
            n_txt = input("n (0-100): ").strip()
            try:
                n = int(n_txt)
            except ValueError:
                print("\nError: n debe ser entero.")
                return
            if not (0 <= n <= 100):
                print("\nError: n debe estar entre 0 y 100.")
                return
            payload = {"n": n}
        else:
            print("\nOpción inválida.")
            return

        resp = self.enviar("enviar_tarea", tipo=tipo, payload=payload)
        if resp is None:
            return
        if resp["ok"]:
            print(f"\nTarea creada: {resp['task_id']}")
            print(f"Estado: {resp['estado']} (use 'Consultar resultado' para ver el avance)")
        else:
            print(f"\nError: {resp['error']['mensaje']}")

    def listar_tareas(self) -> None:
        print("\n--- MIS TAREAS ---")
        resp = self.enviar("listar_tareas")
        if resp is None:
            return
        if not resp["ok"]:
            print(f"\nError: {resp['error']['mensaje']}")
            return
        tareas = resp.get("tareas", [])
        if not tareas:
            print("\nNo tiene tareas registradas.")
            return
        for t in tareas:
            print(f"  {t['task_id']}  [{t['tipo']}]  {t['estado']}")

    def consultar_resultado(self) -> None:
        print("\n--- CONSULTAR RESULTADO ---")
        task_id = input("ID de tarea: ").strip()
        if not task_id:
            print("\nDebe ingresar un identificador.")
            return
        resp = self.enviar("consultar_tarea", task_id=task_id)
        if resp is None:
            return
        if not resp["ok"]:
            print(f"\nError: {resp['error']['mensaje']}")
            return
        print(f"ID: {resp['task_id']}")
        print(f"Estado: {resp['estado']}")
        if resp.get("resultado") is not None:
            print(f"Resultado: {resp['resultado']}")
        if resp.get("error"):
            print(f"Error de procesamiento: {resp['error']}")
        if resp["estado"] in ("PENDIENTE", "EN_PROCESO"):
            print("(La tarea sigue en proceso; vuelva a consultar en unos instantes.)")


def menu(cliente: ClienteTCP | None = None) -> None:
    cli = cliente or ClienteTCP()
    if not cli.conectar():
        return
    try:
        while True:
            print("\n" + "=" * 40)
            print("     SISTEMA DE GESTIÓN DE TAREAS")
            if cli.token:
                print(f"     Sesión: {cli.usuario}")
                print("=" * 40)
                print("1. Enviar tarea")
                print("2. Listar mis tareas")
                print("3. Consultar estado y resultado")
                print("4. Cerrar sesión")
                print("5. Salir")
            else:
                print("=" * 40)
                print("1. Registrarse")
                print("2. Iniciar sesión")
                print("3. Salir")
            opcion = input("Seleccione una opción: ").strip()
            if cli.token:
                if opcion == "1":
                    cli.enviar_tarea()
                elif opcion == "2":
                    cli.listar_tareas()
                elif opcion == "3":
                    cli.consultar_resultado()
                elif opcion == "4":
                    cli.cerrar_sesion()
                elif opcion == "5":
                    print("\nPrograma finalizado.")
                    break
                else:
                    print("\nOpción inválida.")
            else:
                if opcion == "1":
                    cli.registrar()
                elif opcion == "2":
                    cli.iniciar_sesion()
                elif opcion == "3":
                    print("\nPrograma finalizado.")
                    break
                else:
                    print("\nOpción inválida.")
    finally:
        cli.cerrar()


if __name__ == "__main__":
    menu()
