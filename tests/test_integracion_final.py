"""Suite final de integración y robustez — ETAPA 5.

Servidor real en puerto efímero + motor con BD temporal. Cubre el flujo
completo, concurrencia de dos usuarios, fragmentación, errores, límite
64 KB, desconexiones, expiración y reinicio. Sin sleep sin condición:
cada espera es polling con timeout explícito.

Ejecutar:  python -m unittest tests.test_integracion_final -v
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
from servidor.database import conectar_db, crear_tablas
from servidor.workers import TaskEngine
from compartido.protocolo import LineReader, encode_message, decode_message, MAX_MESSAGE_SIZE


def _leer(sock, reader):
    while True:
        linea = reader.readline()
        if linea is None:
            raise ConnectionError("servidor cerró la conexión")
        if linea.strip() == b"":
            continue
        return decode_message(linea)


class Cliente:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.reader = LineReader(self.sock)

    def call(self, op, **datos):
        self.sock.sendall(encode_message({"req_id": str(uuid.uuid4()), "op": op, **datos}))
        return _leer(self.sock, self.reader)

    def send_raw(self, data: bytes):
        self.sock.sendall(data)

    def close(self):
        self.sock.close()


class TestIntegracionFinal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / "final.db")
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

    def _usuario(self, nombre):
        c = Cliente(self.port)
        try:
            r = c.call("registro", usuario=nombre, contraseña="secretoFinal1")
            self.assertTrue(r["ok"], r)
            r = c.call("login", usuario=nombre, contraseña="secretoFinal1")
            self.assertTrue(r["ok"], r)
            return r["token"]
        finally:
            c.close()

    def _esperar(self, ids, timeout=8):
        self.assertTrue(self.motor.esperar_tareas(ids, timeout=timeout))

    def test_flujo_completo_dos_usuarios_simultaneos(self):
        tok_a = self._usuario("alice_f5")
        tok_b = self._usuario("bob_f5")
        ca, cb = Cliente(self.port), Cliente(self.port)
        try:
            ea = ca.call("enviar_tarea", token=tok_a, tipo="suma", payload={"a": 10, "b": 20})
            eb = cb.call("enviar_tarea", token=tok_b, tipo="fibonacci", payload={"n": 10})
            self._esperar([ea["task_id"], eb["task_id"]])
            ra = ca.call("consultar_tarea", token=tok_a, task_id=ea["task_id"])
            rb = cb.call("consultar_tarea", token=tok_b, task_id=eb["task_id"])
            self.assertEqual(ra["resultado"], 30)
            self.assertEqual(rb["resultado"], 55)
            # Aislamiento: cada uno ve solo lo suyo.
            la = ca.call("listar_tareas", token=tok_a)
            lb = cb.call("listar_tareas", token=tok_b)
            self.assertIn(ea["task_id"], [t["task_id"] for t in la["tareas"]])
            self.assertNotIn(eb["task_id"], [t["task_id"] for t in la["tareas"]])
            self.assertIn(eb["task_id"], [t["task_id"] for t in lb["tareas"]])
            # Ajena == inexistente.
            aj = ca.call("consultar_tarea", token=tok_a, task_id=eb["task_id"])
            ine = ca.call("consultar_tarea", token=tok_a, task_id=str(uuid.uuid4()))
            self.assertEqual(aj["error"]["codigo"], "tarea_no_encontrada")
            self.assertEqual(aj["error"]["codigo"], ine["error"]["codigo"])
        finally:
            ca.close(); cb.close()

    def test_login_incorrecto_y_tokens_invalidos(self):
        c = Cliente(self.port)
        try:
            c.call("registro", usuario="carol_f5", contraseña="secretoFinal1")
            mal = c.call("login", usuario="carol_f5", contraseña="incorrecta1")
            self.assertFalse(mal["ok"])
            self.assertEqual(mal["error"]["codigo"], "credenciales_invalidas")
            for datos in ({}, {"token": "noexiste"}):
                r = c.call("enviar_tarea", tipo="suma", payload={"a": 1, "b": 1}, **datos)
                self.assertEqual(r["error"]["codigo"], "autenticacion_requerida")
        finally:
            c.close()

    def test_expiracion_y_logout(self):
        c = Cliente(self.port)
        try:
            c.call("registro", usuario="dave_f5", contraseña="secretoFinal1")
            tok = c.call("login", usuario="dave_f5", contraseña="secretoFinal1")["token"]
            pasado = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            with conectar_db(self.db) as conn:
                conn.execute("UPDATE sesiones SET expira=? WHERE token=?", (pasado, tok))
            r = c.call("listar_tareas", token=tok)
            self.assertEqual(r["error"]["codigo"], "autenticacion_requerida")
            tok2 = c.call("login", usuario="dave_f5", contraseña="secretoFinal1")["token"]
            out = c.call("logout", token=tok2)
            self.assertTrue(out["ok"])
            r2 = c.call("listar_tareas", token=tok2)
            self.assertFalse(r2["ok"])
        finally:
            c.close()

    def test_consecutivas_fragmentadas_y_robustez(self):
        c = Cliente(self.port)
        try:
            c.call("registro", usuario="erin_f5", contraseña="secretoFinal1")
            tok = c.call("login", usuario="erin_f5", contraseña="secretoFinal1")["token"]
            # Consecutivas: dos envíos seguidos en la misma conexión.
            e1 = c.call("enviar_tarea", token=tok, tipo="texto_mayusculas", payload={"texto": "abc"})
            e2 = c.call("enviar_tarea", token=tok, tipo="fibonacci", payload={"n": 6})
            self._esperar([e1["task_id"], e2["task_id"]])
            # Fragmentadas por red (3 bytes por vez).
            data = encode_message({"req_id": str(uuid.uuid4()), "op": "listar_tareas", "token": tok})
            for i in range(0, len(data), 3):
                c.sock.sendall(data[i:i + 3])
            resp = _leer(c.sock, c.reader)  # noqa
            self.assertTrue(resp["ok"])
            # JSON inválido no cierra la conexión.
            c.send_raw(b"no-json\n")
            bad = _leer(c.sock, c.reader)
            self.assertEqual(bad["error"]["codigo"], "json_invalido")
            # Op desconocida.
            unk = c.call("ping")
            self.assertEqual(unk["error"]["codigo"], "op_desconocida")
        finally:
            c.close()

    def test_mensaje_grande_y_desconexion(self):
        c = Cliente(self.port)
        try:
            c.send_raw(b"x" * (MAX_MESSAGE_SIZE + 10) + b"\n")
            big = _leer(c.sock, c.reader)
            self.assertEqual(big["error"]["codigo"], "mensaje_demasiado_grande")
        finally:
            c.close()
        # Desconexión con parcial sin \n no crashea; el servidor sigue vivo.
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(b'{"req_id": "x"')
        s.close()
        time.sleep(0.1)
        c2 = Cliente(self.port)
        try:
            self.assertEqual(c2.call("ping")["error"]["codigo"], "op_desconocida")
        finally:
            c2.close()

    def test_reinicio_conserva_completadas(self):
        # BD propia para no detener el motor compartido de la clase.
        import tempfile
        from pathlib import Path

        tmp2 = tempfile.TemporaryDirectory()
        db2 = str(Path(tmp2.name) / "restart.db")
        m1 = TaskEngine(db2, num_workers=4)
        m1.iniciar()
        try:
            tok = self._usuario("frank_f5")
            # Se crea la tarea vía motor (sin servidor): evita parar el de clase.
            t = m1.enviar_tarea("suma", {"a": 1, "b": 2}, usuario_id=999)
            self.assertTrue(m1.esperar_tareas([t["task_id"]], timeout=5))
            fin = m1.consultar_tarea(t["task_id"])["finalizada"]
        finally:
            m1.detener()
        # Reinicio: motor nuevo sobre la misma BD.
        m2 = TaskEngine(db2, num_workers=4)
        m2.iniciar()
        try:
            fila = m2.consultar_tarea(t["task_id"])
            self.assertEqual(fila["estado"], "COMPLETADA")
            self.assertEqual(fila["finalizada"], fin)
            self.assertEqual(fila["resultado"], 3)
            # Nuevo motor sigue trabajando.
            t2 = m2.enviar_tarea("fibonacci", {"n": 7}, usuario_id=999)
            self.assertTrue(m2.esperar_tareas([t2["task_id"]], timeout=5))
            self.assertEqual(m2.consultar_tarea(t2["task_id"])["resultado"], 13)
        finally:
            m2.detener()
            tmp2.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
