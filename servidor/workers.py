"""Motor de procesamiento concurrente — ETAPA 3.

- Cola FIFO `queue.Queue` de `task_id` (solo IDs; la verdad está en SQLite).
- 4 consumidores persistentes con `ThreadPoolExecutor`.
- Cada worker: get -> reclamo atómico PENDIENTE->EN_PROCESO -> cómputo
  (SIN transacción abierta) -> COMPLETADA/ERROR -> task_done().
- Recuperación al iniciar: EN_PROCESO -> PENDIENTE y re-encolado de
  PENDIENTE; COMPLETADA/ERROR jamás se re-ejecutan.
- Deduplicación en memoria: un set protege contra doble encolado en la
  misma instancia; la persistencia atómica protege entre instancias.
"""

from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from servidor.database import (
    crear_tarea,
    obtener_tarea,
    reclamar_tarea,
    finalizar_tarea,
    revertir_en_proceso,
    listar_pendientes,
)

# Demora simulada leve para hacer observable la concurrencia en demos/tests.
DEMORA_SIMULADA_S = 0.02


def fibonacci(n: int) -> int:
    """Fibonacci iterativo determinístico: F(0)=0, F(1)=1."""
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a


def ejecutar_operacion(tipo: str, payload: dict):
    """Ejecuta una operación ya validada. Lanza ValueError si falla."""
    time.sleep(DEMORA_SIMULADA_S)
    if tipo == "suma":
        return payload["a"] + payload["b"]
    if tipo == "texto_mayusculas":
        return payload["texto"].upper()
    if tipo == "fibonacci":
        return fibonacci(payload["n"])
    raise ValueError(f"tipo no soportado: {tipo!r}")


class TaskEngine:
    """Coordinador de cola + workers (sin red ni autenticación)."""

    def __init__(self, db_path: str, num_workers: int = 4):
        self.db_path = db_path
        self.num_workers = num_workers
        self.cola: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._executor = ThreadPoolExecutor(
            max_workers=num_workers, thread_name_prefix="worker"
        )
        self._encoladas: set[str] = set()
        self._lock = threading.Lock()
        self._workers: list = []

    # -- ciclo de vida ---------------------------------------------------
    def iniciar(self) -> None:
        """Recupera pendientes y arranca los consumidores."""
        from servidor.database import crear_tablas

        crear_tablas(self.db_path)
        revertidas = revertir_en_proceso(self.db_path)
        if revertidas:
            print(f"[motor] {revertidas} tarea(s) EN_PROCESO revertidas a PENDIENTE")
        pendientes = listar_pendientes(self.db_path)
        for t in pendientes:
            self._encolar(t["task_id"])
        for _ in range(self.num_workers):
            self._workers.append(
                self._executor.submit(self._bucle_worker)
            )
        print(f"[motor] {self.num_workers} workers iniciados, "
              f"{len(pendientes)} tarea(s) recuperada(s)")

    def detener(self) -> None:
        self._stop.set()
        # Sentinel None para despertar a los workers bloqueados en get().
        for _ in range(self.num_workers):
            self.cola.put(None)  # type: ignore[arg-type]
        self._executor.shutdown(wait=True)
        print("[motor] workers detenidos")

    # -- API de tareas ---------------------------------------------------
    def enviar_tarea(self, tipo: str, payload: dict,
                     usuario_id: int | None = None) -> dict:
        """Valida, persiste y encola. Devuelve la tarea en PENDIENTE."""
        tarea = crear_tarea(tipo, payload, usuario_id, self.db_path)
        self._encolar(tarea["task_id"])
        return tarea

    def consultar_tarea(self, task_id: str) -> dict | None:
        return obtener_tarea(task_id, self.db_path)

    def esperar_tareas(self, task_ids: list[str], timeout: float = 5.0) -> bool:
        """Espera (polling) a que todas las tareas terminen. True si OK."""
        fin = time.time() + timeout
        while time.time() < fin:
            if all(
                (self.consultar_tarea(t) or {}).get("estado") in ("COMPLETADA", "ERROR")
                for t in task_ids
            ):
                return True
            time.sleep(0.02)
        return False

    # -- internos --------------------------------------------------------
    def _encolar(self, task_id: str) -> bool:
        with self._lock:
            if task_id in self._encoladas:
                return False
            self._encoladas.add(task_id)
        self.cola.put(task_id)
        return True

    def _bucle_worker(self) -> None:
        while not self._stop.is_set():
            item = self.cola.get()
            try:
                if item is None:
                    return
                self._procesar(item)
            finally:
                self.cola.task_done()

    def _procesar(self, task_id: str) -> None:
        try:
            # Reclamo + cómputo + finalización con red de seguridad: ningún
            # error (sobre todo de BD) debe matar al worker ni dejar la
            # tarea colgada. La tarea queda PENDIENTE si falló antes de
            # completarse, lista para re-encolar en el próximo arranque.
            tarea = reclamar_tarea(task_id, self.db_path)
            if tarea is None:
                return  # otro worker ya la reclamó o ya terminó
            with self._lock:
                self._encoladas.discard(task_id)
            try:
                resultado = ejecutar_operacion(tarea["tipo"], tarea["payload"])
                finalizar_tarea(task_id, resultado, db_path=self.db_path)
            except Exception as e:  # error de cómputo -> estado ERROR
                try:
                    finalizar_tarea(task_id, None, error=str(e) or e.__class__.__name__,
                                    db_path=self.db_path)
                except Exception as e2:
                    print(f"[motor] no se pudo persistir ERROR de {task_id}: {e2}")
        except Exception as e:  # p.ej. DB caída: worker sobrevive
            print(f"[motor] error procesando {task_id}: {e}")
