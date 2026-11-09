"""Prueba de integración TCP — ETAPA 2.

Levanta el servidor REAL en un puerto efímero (puerto 0) en un hilo
y lo ejercita con un cliente TCP real mediante socket.

Ejecutar:  python -m pytest tests/test_integracion.py -v
       o:  python -m unittest tests.test_integracion -v
"""

import socket
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "servidor"))

import server as servidor
from compartido.protocolo import LineReader, encode_message, decode_message


def _leer_respuesta(sock: socket.socket, reader: LineReader) -> dict:
    """Lee líneas vacías omitidas hasta una respuesta decodificable."""
    while True:
        linea = reader.readline()
        if linea is None:
            raise ConnectionError("el servidor cerró la conexión sin responder")
        if linea.strip() == b"":
            continue
        return decode_message(linea)


class TestIntegracionEtapa2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stop = threading.Event()
        cls.port_holder: dict = {}

        def _correr():
            # Puerto 0 = el SO asigna uno libre; lo leemos del socket del server.
            # Como serve_forever no lo expone, probamos puertos libres en bucle.
            import server  # noqa

            sock = server.create_server("127.0.0.1", 0)
            cls.port_holder["port"] = sock.getsockname()[1]
            sock.settimeout(0.5)
            try:
                while not cls.stop.is_set():
                    try:
                        conexion, addr = sock.accept()
                    except socket.timeout:
                        continue
                    t = threading.Thread(
                        target=server.handle_client, args=(conexion, addr), daemon=True
                    )
                    t.start()
            finally:
                sock.close()

        cls.hilo = threading.Thread(target=_correr, daemon=True)
        cls.hilo.start()
        # Espera a que el puerto quede publicado.
        for _ in range(100):
            if "port" in cls.port_holder:
                break
            time.sleep(0.05)
        cls.port = cls.port_holder["port"]

    @classmethod
    def tearDownClass(cls):
        cls.stop.set()
        cls.hilo.join(timeout=3)

    def _conectar(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        return sock, LineReader(sock)

    def ping(self, sock, reader, req_id="p"):
        sock.sendall(encode_message({"req_id": req_id, "op": "ping"}))
        return _leer_respuesta(sock, reader)

    def test_envio_correcto(self):
        sock, reader = self._conectar()
        try:
            resp = self.ping(sock, reader, "i-1")
            self.assertFalse(resp["ok"])
            self.assertEqual(resp["error"]["codigo"], "op_desconocida")
        finally:
            sock.close()

    def test_mensajes_consecutivos_misma_conexion(self):
        sock, reader = self._conectar()
        try:
            self.assertEqual(self.ping(sock, reader, "a")["req_id"], "a")
            self.assertEqual(self.ping(sock, reader, "b")["req_id"], "b")
        finally:
            sock.close()

    def test_fragmentado_por_red(self):
        sock, reader = self._conectar()
        try:
            datos = encode_message({"req_id": "frag", "op": "ping"})
            for i in range(0, len(datos), 3):
                sock.sendall(datos[i : i + 3])
            resp = _leer_respuesta(sock, reader)
            self.assertEqual(resp["req_id"], "frag")
        finally:
            sock.close()

    def test_json_invalido_no_cierra(self):
        sock, reader = self._conectar()
        try:
            sock.sendall(b"esto no es json\n")
            resp = _leer_respuesta(sock, reader)
            self.assertFalse(resp["ok"])
            self.assertEqual(resp["error"]["codigo"], "json_invalido")
            self.assertIsNone(resp["req_id"])
            # La conexión sigue viva tras el error.
            resp = self.ping(sock, reader, "despues")
            self.assertEqual(resp["req_id"], "despues")
        finally:
            sock.close()

    def test_sobre_invalido(self):
        sock, reader = self._conectar()
        try:
            sock.sendall(encode_message({"op": "ping"}))  # falta req_id
            resp = _leer_respuesta(sock, reader)
            self.assertFalse(resp["ok"])
            self.assertEqual(resp["error"]["codigo"], "sobre_invalido")
        finally:
            sock.close()

    def test_mensaje_demasiado_grande_cierra(self):
        sock, reader = self._conectar()
        try:
            from compartido.protocolo import MAX_MESSAGE_SIZE

            sock.sendall(b"x" * (MAX_MESSAGE_SIZE + 10) + b"\n")
            resp = _leer_respuesta(sock, reader)
            self.assertFalse(resp["ok"])
            self.assertEqual(resp["error"]["codigo"], "mensaje_demasiado_grande")
        finally:
            sock.close()

    def test_desconexion_sin_mensaje_no_crashea(self):
        sock, _ = self._conectar()
        sock.close()
        time.sleep(0.1)
        # El servidor sigue aceptando tras una desconexión abrupta.
        sock, reader = self._conectar()
        try:
            self.assertEqual(self.ping(sock, reader, "sigue")["req_id"], "sigue")
        finally:
            sock.close()

    def test_desconexion_mensaje_incompleto_no_crashea(self):
        sock, _ = self._conectar()
        sock.sendall(b'{"req_id": "x"')  # sin \n
        sock.close()
        time.sleep(0.1)
        sock, reader = self._conectar()
        try:
            self.assertEqual(self.ping(sock, reader, "vivo")["req_id"], "vivo")
        finally:
            sock.close()

    def test_doble_conexion_concurrente(self):
        s1, r1 = self._conectar()
        s2, r2 = self._conectar()
        try:
            self.assertEqual(self.ping(s1, r1, "c1")["req_id"], "c1")
            self.assertEqual(self.ping(s2, r2, "c2")["req_id"], "c2")
        finally:
            s1.close()
            s2.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
