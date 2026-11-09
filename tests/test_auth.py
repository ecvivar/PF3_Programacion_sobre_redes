"""Pruebas de autenticación + sesiones + aislamiento — ETAPA 4.

Usa BD temporal y servidor TCP real con motor (sin auth: 401-like).

Ejecutar:  python -m pytest tests/test_auth.py -v
       o:  python -m unittest tests.test_auth -v
"""

import socket
import sys
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import servidor.server as server
from servidor.auth import registrar_usuario, login, validar_sesion, invalidar_sesion, limpiar_sesiones_expiradas, AuthError
from servidor.database import crear_tablas, conectar_db, ahora_iso
from servidor.workers import TaskEngine
from compartido.protocolo import LineReader, encode_message, decode_message


def _leer(sock, reader):
    while True:
        linea = reader.readline()
        if linea is None:
            raise ConnectionError("servidor cerró la conexión")
        if linea.strip() == b"":
            continue
        return decode_message(linea)


class TestAuthUnitario(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "auth.db")
        crear_tablas(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_registro_exitoso_y_hash(self):
        r = registrar_usuario("ana", "secreto1", self.db)
        self.assertEqual(r["usuario"], "ana")
        with conectar_db(self.db) as conn:
            fila = conn.execute("SELECT usuario, contraseña FROM usuarios").fetchone()
        self.assertNotIn("secreto1", fila["contraseña"])  # hash, no plano

    def test_registro_duplicado(self):
        registrar_usuario("ana", "secreto1", self.db)
        with self.assertRaises(AuthError) as ctx:
            registrar_usuario("ana", "otra1234", self.db)
        self.assertEqual(ctx.exception.codigo, "usuario_duplicado")

    def test_validacion_registro(self):
        for u, c in [(123, "secreto1"), ("", "secreto1"), ("  ", "secreto1"),
                     ("ana", "abc"), ("x" * 51, "secreto1"), (None, None)]:
            with self.assertRaises(AuthError, msg=str((u, c))):
                registrar_usuario(u, c, self.db)  # type: ignore[arg-type]

    def test_login_correcto_e_incorrecto(self):
        registrar_usuario("ana", "secreto1", self.db)
        ok = login("ana", "secreto1", self.db)
        self.assertIn("token", ok)
        for u, c in [("ana", "mala1234"), ("nadie", "secreto1"),
                     ("ana", ""), (None, None)]:
            with self.assertRaises(AuthError) as ctx:
                login(u, c, self.db)  # type: ignore[arg-type]
            self.assertEqual(ctx.exception.codigo, "credenciales_invalidas")

    def test_sesion_validar_expirar_invalidar(self):
        registrar_usuario("ana", "secreto1", self.db)
        s = login("ana", "secreto1", self.db)
        info = validar_sesion(s["token"], self.db)
        self.assertEqual(info["usuario"], "ana")
        # Expira artificialmente.
        pasado = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        with conectar_db(self.db) as conn:
            conn.execute("UPDATE sesiones SET expira=? WHERE token=?",
                         (pasado, s["token"]))
        with self.assertRaises(AuthError) as ctx:
            validar_sesion(s["token"], self.db)
        self.assertEqual(ctx.exception.codigo, "autenticacion_requerida")
        # Token inexistente / vacío / logout.
        with self.assertRaises(AuthError):
            validar_sesion("no-existe", self.db)
        with self.assertRaises(AuthError):
            validar_sesion("", self.db)
        s2 = login("ana", "secreto1", self.db)
        self.assertTrue(invalidar_sesion(s2["token"], self.db))
        with self.assertRaises(AuthError):
            validar_sesion(s2["token"], self.db)


class TestAuthIntegracion(unittest.TestCase):
    """Servidor real en puerto efímero + motor con BD temporal."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / "integ.db")
        cls.motor = TaskEngine(cls.db, num_workers=4)
        cls.motor.iniciar()
        cls.stop = threading.Event()
        holder = {}
        server_sock = server.create_server("127.0.0.1", 0)
        holder["port"] = server_sock.getsockname()[1]
        server_sock.settimeout(0.5)

        def _correr():
            try:
                while not cls.stop.is_set():
                    try:
                        conn, addr = server_sock.accept()
                    except socket.timeout:
                        continue
                    t = threading.Thread(target=server.handle_client,
                                         args=(conn, addr, cls.motor), daemon=True)
                    t.start()
            finally:
                server_sock.close()

        cls.hilo = threading.Thread(target=_correr, daemon=True)
        cls.hilo.start()
        for _ in range(100):
            if "port" in holder:
                break
            time.sleep(0.05)
        cls.port = holder["port"]

    @classmethod
    def tearDownClass(cls):
        cls.stop.set()
        cls.hilo.join(timeout=3)
        cls.motor.detener()
        cls.tmp.cleanup()

    def _call(self, op, **datos):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        reader = LineReader(sock)
        try:
            sock.sendall(encode_message({"req_id": str(uuid.uuid4()), "op": op, **datos}))
            return _leer(sock, reader)
        finally:
            sock.close()

    def _session(self, nombre):
        self._call("registro", usuario=nombre, contraseña="secreto1")
        r = self._call("login", usuario=nombre, contraseña="secreto1")
        assert r["ok"], r
        return r["token"]

    def test_flujo_completo(self):
        tok = self._session("fulana")
        env = self._call("enviar_tarea", token=tok, tipo="suma", payload={"a": 2, "b": 3})
        self.assertTrue(env["ok"])
        assert self.motor.esperar_tareas([env["task_id"]], timeout=5)
        cons = self._call("consultar_tarea", token=tok, task_id=env["task_id"])
        self.assertEqual(cons["resultado"], 5)
        lst = self._call("listar_tareas", token=tok)
        self.assertTrue(any(t["task_id"] == env["task_id"] for t in lst["tareas"]))
        out = self._call("logout", token=tok)
        self.assertTrue(out["ok"])
        # Tras logout el token ya no sirve.
        mal = self._call("listar_tareas", token=tok)
        self.assertFalse(mal["ok"])

    def test_tres_tipos(self):
        tok = self._session("tipos_usr")
        casos = [
            ("suma", {"a": 4, "b": 5}, 9),
            ("texto_mayusculas", {"texto": "hola"}, "HOLA"),
            ("fibonacci", {"n": 10}, 55),
        ]
        for tipo, payload, esperado in casos:
            env = self._call("enviar_tarea", token=tok, tipo=tipo, payload=payload)
            self.assertTrue(env["ok"], env)
            assert self.motor.esperar_tareas([env["task_id"]], timeout=5)
            cons = self._call("consultar_tarea", token=tok, task_id=env["task_id"])
            self.assertEqual(cons["resultado"], esperado)

    def test_sin_token_y_token_invalido(self):
        for datos in ({}, {"token": "invalido"}):
            r = self._call("enviar_tarea", tipo="suma", payload={"a": 1, "b": 1}, **datos)
            self.assertFalse(r["ok"])
            self.assertEqual(r["error"]["codigo"], "autenticacion_requerida")

    def test_aislamiento_ajena_no_revelada(self):
        t1 = self._session("usuario_uno")
        t2 = self._session("usuario_dos")
        env = self._call("enviar_tarea", token=t1, tipo="fibonacci", payload={"n": 6})
        assert self.motor.esperar_tareas([env["task_id"]], timeout=5)
        # Consultar ajena = mismo error que inexistente.
        ajena = self._call("consultar_tarea", token=t2, task_id=env["task_id"])
        inexistente = self._call("consultar_tarea", token=t2, task_id=str(uuid.uuid4()))
        self.assertEqual(ajena["error"]["codigo"], "tarea_no_encontrada")
        self.assertEqual(ajena["error"]["codigo"], inexistente["error"]["codigo"])
        # Listar ajena no muestra nada.
        lst2 = self._call("listar_tareas", token=t2)
        self.assertNotIn(env["task_id"], [t["task_id"] for t in lst2["tareas"]])

    def test_op_desconocida(self):
        r = self._call("ping")
        self.assertEqual(r["error"]["codigo"], "op_desconocida")


if __name__ == "__main__":
    unittest.main(verbosity=2)
