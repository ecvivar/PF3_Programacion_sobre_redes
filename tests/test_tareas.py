"""Pruebas del motor de tareas — ETAPA 3 (SQLite temporal por test).

Ejecutar:  python -m pytest tests/test_tareas.py -v
       o:  python -m unittest tests.test_tareas -v
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from servidor.database import (
    crear_tablas, crear_tarea, obtener_tarea, reclamar_tarea,
    finalizar_tarea, revertir_en_proceso, listar_pendientes,
    validar_payload,
)
from servidor.workers import TaskEngine, fibonacci, ejecutar_operacion


class BaseCasos(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "test.db")
        crear_tablas(self.db)

    def tearDown(self):
        self.tmp.cleanup()


class TestDatabase(BaseCasos):
    def test_creacion_y_persistencia(self):
        t = crear_tarea("suma", {"a": 1, "b": 2}, db_path=self.db)
        self.assertTrue(t["task_id"])
        self.assertEqual(t["estado"], "PENDIENTE")
        self.assertIsNone(t["usuario_id"])  # nullable temporal Etapa 3
        self.assertIsNotNone(t["creada"])
        # Persistencia: reread desde archivo.
        t2 = obtener_tarea(t["task_id"], self.db)
        self.assertEqual(t2["task_id"], t["task_id"])
        self.assertEqual(t2["payload"], {"a": 1, "b": 2})

    def test_ids_unicos(self):
        a = crear_tarea("suma", {"a": 0, "b": 0}, db_path=self.db)
        b = crear_tarea("suma", {"a": 0, "b": 0}, db_path=self.db)
        self.assertNotEqual(a["task_id"], b["task_id"])

    def test_validacion_payload(self):
        casos = [
            ("tipo_malo", {}), ("suma", {"a": "x", "b": 1}),
            ("suma", {"a": True, "b": 1}), ("texto_mayusculas", {"texto": 5}),
            ("texto_mayusculas", {"texto": "x" * 10001}),
            ("fibonacci", {"n": -1}), ("fibonacci", {"n": 101}),
            ("fibonacci", {"n": 1.5}), ("suma", "no dict"),
        ]
        for tipo, payload in casos:
            with self.assertRaises(ValueError, msg=f"{tipo} {payload}"):
                validar_payload(tipo, payload)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            crear_tarea("suma", {"a": "x", "b": 1}, db_path=self.db)

    def test_transiciones_y_fechas(self):
        t = crear_tarea("suma", {"a": 2, "b": 3}, db_path=self.db)
        r = reclamar_tarea(t["task_id"], self.db)
        self.assertEqual(r["estado"], "EN_PROCESO")
        self.assertIsNotNone(r["iniciada"])
        f = finalizar_tarea(t["task_id"], 5, db_path=self.db)
        self.assertEqual((f["estado"], f["resultado"]), ("COMPLETADA", 5))
        self.assertIsNotNone(f["finalizada"])
        # Segunda reclamación: None (ya no PENDIENTE).
        self.assertIsNone(reclamar_tarea(t["task_id"], self.db))

    def test_error_persiste(self):
        t = crear_tarea("suma", {"a": 1, "b": 1}, db_path=self.db)
        reclamar_tarea(t["task_id"], self.db)
        f = finalizar_tarea(t["task_id"], None, error="boom", db_path=self.db)
        self.assertEqual((f["estado"], f["error"]), ("ERROR", "boom"))


class TestOperaciones(unittest.TestCase):
    def test_suma(self):
        self.assertEqual(ejecutar_operacion("suma", {"a": 2, "b": 3}), 5)
        self.assertEqual(ejecutar_operacion("suma", {"a": -1.5, "b": 1.5}), 0.0)
        with self.assertRaises(ValueError):
            ejecutar_operacion("otro", {})

    def test_mayusculas(self):
        self.assertEqual(
            ejecutar_operacion("texto_mayusculas", {"texto": "hola ñ"}), "HOLA Ñ")

    def test_fibonacci(self):
        for n, esperado in [(0, 0), (1, 1), (2, 1), (10, 55), (12, 144)]:
            self.assertEqual(fibonacci(n), esperado)
        self.assertEqual(ejecutar_operacion("fibonacci", {"n": 10}), 55)


class TestMotor(BaseCasos):
    def test_procesa_tres_tipos(self):
        motor = TaskEngine(self.db, num_workers=4)
        motor.iniciar()
        try:
            ids = [
                motor.enviar_tarea("suma", {"a": 2, "b": 3})["task_id"],
                motor.enviar_tarea("texto_mayusculas", {"texto": "abc"})["task_id"],
                motor.enviar_tarea("fibonacci", {"n": 10})["task_id"],
            ]
            self.assertTrue(motor.esperar_tareas(ids, timeout=5))
            res = [motor.consultar_tarea(i)["resultado"] for i in ids]
            self.assertEqual(res, [5, "ABC", 55])
            for i in ids:
                self.assertEqual(motor.consultar_tarea(i)["estado"], "COMPLETADA")
        finally:
            motor.detener()

    def test_concurrencia_multiples(self):
        motor = TaskEngine(self.db, num_workers=4)
        motor.iniciar()
        try:
            ids = [motor.enviar_tarea("fibonacci", {"n": 8})["task_id"]
                   for _ in range(12)]
            self.assertTrue(motor.esperar_tareas(ids, timeout=8))
            self.assertEqual({motor.consultar_tarea(i)["resultado"] for i in ids}, {21})
            self.assertEqual(len(set(ids)), 12)
        finally:
            motor.detener()

    def test_sin_duplicados_misma_instancia(self):
        motor = TaskEngine(self.db, num_workers=4)
        motor.iniciar()
        try:
            t = crear_tarea("suma", {"a": 1, "b": 1}, db_path=self.db)
            # Encolar dos veces: la segunda debe perderse.
            self.assertTrue(motor._encolar(t["task_id"]))
            self.assertFalse(motor._encolar(t["task_id"]))
            # Esperar y verificar que se completó una sola vez en BD.
            self.assertTrue(motor.esperar_tareas([t["task_id"]], timeout=5))
            self.assertEqual(motor.consultar_tarea(t["task_id"])["resultado"], 2)
        finally:
            motor.detener()

    def test_error_de_operacion_marca_error(self):
        motor = TaskEngine(self.db, num_workers=4)
        motor.iniciar()
        try:
            t = crear_tarea("suma", {"a": 1, "b": 1}, db_path=self.db)
            # Patch ANTES de encolar: evita la carrera con un worker real.
            with mock.patch("servidor.workers.ejecutar_operacion", side_effect=RuntimeError("boom")):
                motor._encolar(t["task_id"])
                self.assertTrue(motor.esperar_tareas([t["task_id"]], timeout=5))
            fila = motor.consultar_tarea(t["task_id"])
            self.assertEqual(fila["estado"], "ERROR")
            self.assertIn("boom", fila["error"])
        finally:
            motor.detener()

    def test_recuperacion_pendientes(self):
        # Crea pendientes sin motor, luego inicia: deben procesarse.
        ids = [crear_tarea("suma", {"a": i, "b": 1}, db_path=self.db)["task_id"]
               for i in range(3)]
        motor = TaskEngine(self.db, num_workers=4)
        motor.iniciar()
        try:
            self.assertTrue(motor.esperar_tareas(ids, timeout=5))
            self.assertEqual(
                [motor.consultar_tarea(i)["resultado"] for i in ids], [1, 2, 3])
        finally:
            motor.detener()

    def test_recuperacion_interrumpidas(self):
        # PENDIENTE + EN_PROCESO (interrumpida) deben procesarse.
        p = crear_tarea("suma", {"a": 10, "b": 5}, db_path=self.db)
        e = crear_tarea("fibonacci", {"n": 5}, db_path=self.db)
        reclamar_tarea(e["task_id"], self.db)  # queda EN_PROCESO
        self.assertEqual(revertir_en_proceso(self.db), 1)
        motor = TaskEngine(self.db, num_workers=4)
        motor.iniciar()
        try:
            self.assertTrue(motor.esperar_tareas([p["task_id"], e["task_id"]], timeout=5))
            self.assertEqual(motor.consultar_tarea(e["task_id"])["resultado"], 5)
        finally:
            motor.detener()

    def test_completadas_no_se_reejecutan(self):
        motor = TaskEngine(self.db, num_workers=4)
        motor.iniciar()
        try:
            t = motor.enviar_tarea("suma", {"a": 7, "b": 8})
            self.assertTrue(motor.esperar_tareas([t["task_id"]], timeout=5))
            fin1 = motor.consultar_tarea(t["task_id"])["finalizada"]
        finally:
            motor.detener()
        # Reinicio: no debe tocar nada completado.
        motor2 = TaskEngine(self.db, num_workers=4)
        motor2.iniciar()
        try:
            fila = motor2.consultar_tarea(t["task_id"])
            self.assertEqual(fila["finalizada"], fin1)
            self.assertEqual(fila["resultado"], 15)
            self.assertEqual(listar_pendientes(self.db), [])
        finally:
            motor2.detener()

    def test_reinicio_motor_sin_duplicados(self):
        motor = TaskEngine(self.db, num_workers=4)
        motor.iniciar()
        ids = [motor.enviar_tarea("fibonacci", {"n": 6})["task_id"] for _ in range(5)]
        motor.detener()  # reinicio con tareas posiblemente sin procesar
        motor2 = TaskEngine(self.db, num_workers=4)
        motor2.iniciar()
        try:
            self.assertTrue(motor2.esperar_tareas(ids, timeout=8))
            # Cada una completada exactamente una vez.
            self.assertEqual(len(set(ids)), 5)
            self.assertEqual({motor2.consultar_tarea(i)["estado"] for i in ids}, {"COMPLETADA"})
            self.assertEqual({motor2.consultar_tarea(i)["resultado"] for i in ids}, {8})
        finally:
            motor2.detener()

    def test_wal_activo(self):
        conn = sqlite3.connect(self.db)
        try:
            modo = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(modo.lower(), "wal")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
