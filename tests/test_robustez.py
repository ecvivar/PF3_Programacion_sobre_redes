"""Pruebas de regresión de la auditoría — ETAPA 5.

F1: expira naive no crashea. F2: el worker sobrevive a un fallo de BD.
F3: listar muchos elementos no rompe el protocolo (límite 64 KB).

Ejecutar:  python -m unittest tests.test_robustez -v
"""

import socket
import sys
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import servidor.server as server
from servidor.auth import registrar_usuario, login, validar_sesion, AuthError
from servidor.database import crear_tablas, conectar_db, crear_tarea
from servidor.workers import TaskEngine
from compartido.protocolo import LineReader, encode_message, decode_message


def _leer(sock, reader):
    while True:
        linea = reader.readline()
        if linea is None:
            raise ConnectionError("servidor cerró")
        if linea.strip() == b"":
            continue
        return decode_message(linea)


class TestRegresiones(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "rob.db")
        crear_tablas(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_f1_expira_naive_no_crashea(self):
        registrar_usuario("naive", "secretoFinal1", self.db)
        tok = login("naive", "secretoFinal1", self.db)["token"]
        with conectar_db(self.db) as conn:
            conn.execute("UPDATE sesiones SET expira=? WHERE token=?",
                         ("2030-01-01T00:00:00", tok))  # naive, sin tz
        with self.assertRaises(AuthError) as ctx:
            validar_sesion(tok, self.db)
        self.assertEqual(ctx.exception.codigo, "autenticacion_requerida")

    def test_f2_worker_sobrevive_fallo_bd(self):
        motor = TaskEngine(self.db, num_workers=4)
        motor.iniciar()
        try:
            import servidor.workers as w

            orig = w.reclamar_tarea
            calls = {"n": 0}

            def flaky(*a, **k):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("caída simulada")
                return orig(*a, **k)

            with mock.patch("servidor.workers.reclamar_tarea", side_effect=flaky):
                t = motor.enviar_tarea("suma", {"a": 1, "b": 1})
                self.assertFalse(motor.esperar_tareas([t["task_id"]], timeout=0.2))
                self.assertEqual(motor.consultar_tarea(t["task_id"])["estado"], "PENDIENTE")
            t2 = motor.enviar_tarea("suma", {"a": 5, "b": 5})
            self.assertTrue(motor.esperar_tareas([t2["task_id"]], timeout=5))
            self.assertEqual(motor.consultar_tarea(t2["task_id"])["resultado"], 10)
        finally:
            motor.detener()


class TestListadoMasivo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / "mass.db")
        cls.motor = TaskEngine(cls.db, num_workers=4)
        cls.motor.iniciar()
        cls.stop = threading.Event()
        holder = {}
        s = server.create_server("127.0.0.1", 0)
        holder["port"] = s.getsockname()[1]
        s.settimeout(0.5)

        def _correr():
            try:
                while not cls.stop.is_set():
                    try:
                        conn, addr = s.accept()
                    except socket.timeout:
                        continue
                    threading.Thread(target=server.handle_client,
                                     args=(conn, addr, cls.motor), daemon=True).start()
            finally:
                s.close()

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

    def test_listar_200_no_rompe_protocolo(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        reader = LineReader(sock)
        try:
            tok = self._session(sock, reader)
            for i in range(200):
                sock.sendall(encode_message({"req_id": str(uuid.uuid4()), "op": "enviar_tarea",
                                             "token": tok, "tipo": "suma",
                                             "payload": {"a": i, "b": 1}}))
                r = _leer(sock, reader)
                self.assertTrue(r["ok"], r)
            sock.sendall(encode_message({"req_id": str(uuid.uuid4()), "op": "listar_tareas", "token": tok}))
            r = _leer(sock, reader)
            self.assertTrue(r["ok"], r)
            self.assertLessEqual(len(r["tareas"]), 100)
        finally:
            sock.close()

    def _session(self, sock, reader):
        sock.sendall(encode_message({"req_id": str(uuid.uuid4()), "op": "registro",
                                     "usuario": "mass", "contraseña": "secretoFinal1"}))
        self.assertTrue(_leer(sock, reader)["ok"])
        sock.sendall(encode_message({"req_id": str(uuid.uuid4()), "op": "login",
                                     "usuario": "mass", "contraseña": "secretoFinal1"}))
        r = _leer(sock, reader)
        self.assertTrue(r["ok"], r)
        return r["token"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
