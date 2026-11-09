"""Pruebas unitarias del protocolo — ETAPA 2.

Ejecutar:  python -m pytest tests/test_protocolo.py -v
       o:  python -m unittest tests.test_protocolo -v
"""

import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compartido.protocolo import (
    MAX_MESSAGE_SIZE,
    LineReader,
    MessageTooLargeError,
    InvalidMessageError,
    encode_message,
    decode_message,
    validate_envelope,
    success_response,
    error_response,
)


class TestEncodeDecode(unittest.TestCase):
    def test_roundtrip(self):
        obj = {"req_id": "r-1", "op": "ping", "datos": {"a": "ñ", "n": 3}}
        data = encode_message(obj)
        self.assertTrue(data.endswith(b"\n"))
        self.assertEqual(decode_message(data), obj)

    def test_rechaza_no_objeto(self):
        with self.assertRaises(InvalidMessageError):
            encode_message([1, 2])  # type: ignore[arg-type]

    def test_decode_json_invalido(self):
        with self.assertRaises(InvalidMessageError):
            decode_message(b"no es json\n")

    def test_decode_no_objeto(self):
        with self.assertRaises(InvalidMessageError):
            decode_message(b"[1, 2]\n")

    def test_encode_limite(self):
        with self.assertRaises(MessageTooLargeError):
            encode_message({"req_id": "r", "op": "x", "payload": "x" * MAX_MESSAGE_SIZE})

    def test_decode_limite(self):
        with self.assertRaises(MessageTooLargeError):
            decode_message(b"x" * (MAX_MESSAGE_SIZE + 1))


class TestEnvelope(unittest.TestCase):
    def test_valido(self):
        self.assertEqual(
            validate_envelope({"req_id": "r-1", "op": "enviar_tarea"}),
            ("r-1", "enviar_tarea"),
        )

    def test_sin_req_id(self):
        with self.assertRaises(InvalidMessageError):
            validate_envelope({"op": "ping"})

    def test_sin_op(self):
        with self.assertRaises(InvalidMessageError):
            validate_envelope({"req_id": "r-1"})

    def test_req_id_no_string(self):
        with self.assertRaises(InvalidMessageError):
            validate_envelope({"req_id": 123, "op": "ping"})

    def test_respuestas(self):
        ok = success_response("r-1", op="ping")
        self.assertEqual((ok["req_id"], ok["ok"]), ("r-1", True))
        err = error_response(None, "json_invalido", "mal")
        self.assertEqual(err["error"]["codigo"], "json_invalido")


class TestLineReader(unittest.TestCase):
    def _par(self):
        a, b = socket.socketpair()
        return a, b

    def test_fragmentado(self):
        """Una línea enviada en fragmentos se reconstruye."""
        cli, srv = self._par()
        try:
            r = LineReader(srv)
            cli.sendall(b'{"req_id": "r-')
            cli.sendall(b'1", "op": "ping"')
            cli.sendall(b"}\n")
            linea = r.readline()
            self.assertEqual(decode_message(linea), {"req_id": "r-1", "op": "ping"})
        finally:
            cli.close()
            srv.close()

    def test_consecutivos_pegados(self):
        """Dos mensajes pegados en un solo sendall se separan."""
        cli, srv = self._par()
        try:
            r = LineReader(srv)
            cli.sendall(b'{"req_id":"1","op":"a"}\n{"req_id":"2","op":"b"}\n')
            self.assertEqual(decode_message(r.readline()), {"req_id": "1", "op": "a"})
            self.assertEqual(decode_message(r.readline()), {"req_id": "2", "op": "b"})
        finally:
            cli.close()
            srv.close()

    def test_eof_limpio(self):
        cli, srv = self._par()
        try:
            cli.close()
            self.assertIsNone(LineReader(srv).readline())
        finally:
            srv.close()

    def test_incompleto_sin_salto(self):
        """Desconexión con datos sin '\\n' se reporta como incompleto."""
        cli, srv = self._par()
        try:
            cli.sendall(b'{"req_id": "r-1"')
            cli.close()
            with self.assertRaises(InvalidMessageError):
                LineReader(srv).readline()
        finally:
            srv.close()

    def test_demasiado_grande(self):
        cli, srv = self._par()
        try:
            cli.sendall(b"x" * (MAX_MESSAGE_SIZE + 10))
            with self.assertRaises(MessageTooLargeError):
                LineReader(srv).readline()
        finally:
            cli.close()
            srv.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
