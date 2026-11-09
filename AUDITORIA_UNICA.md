# PFO3 — Auditoría integral y corrección

## Alcance
Se auditó el archivo RAR recibido, sin modificar la base original. La copia de entrega omite `pfo3.db` porque contiene cuentas y tokens de sesiones; el servidor crea una base nueva automáticamente. Para conservar los datos existentes, mantené tu `pfo3.db` local al reemplazar los archivos de código.

## Hallazgos y correcciones
1. `servidor/server.py` importaba el módulo de autenticación **antes** de validar la operación, de modo que una dependencia no instalada podía romper incluso operaciones no autenticadas. El import se reubicó después de validar el tipo de operación y la presencia del motor.
2. `servidor/auth.py` dependía de Werkzeug. Se reemplazó la dependencia por `servidor/passwords.py`, usando `hashlib.scrypt`, `hmac.compare_digest` y el mismo formato `scrypt:32768:8:1$salt$hash` de las cuentas del archivo. Se admite además verificación de hashes PBKDF2-SHA256 heredados.
3. Las pruebas de integración de una etapa anterior lanzan el servidor sin motor. Para operaciones desconocidas ahora se responde correctamente antes de requerir motor. Las operaciones reales siguen exigiendo motor.
4. El archivo `requirements.txt` refleja que no hay dependencias externas de ejecución.

## Evidencia ejecutada en entorno aislado
- `python -m unittest discover -s tests -q`: **61 pruebas OK**.
- Prueba funcional real por socket TCP: registro exitoso, login con token, creación de tarea `suma(7,8)`, consulta de estado COMPLETADA y resultado 15, persistencia de usuario en SQLite: **OK**.
- Las pruebas se ejecutaron en Linux/Python 3.13, no en Windows. No se puede asegurar sin ejecución en el equipo del usuario que un error específico de configuración local de Windows esté resuelto.

## Ejecución
Desde la raíz del proyecto, en dos terminales:

Terminal 1: `python -m servidor.server`

Terminal 2: `python -m cliente.client`

Pruebas: `python -m unittest discover -s tests -q`

## Nota sobre el diseño
La implementación real es un servidor coordinador TCP, workers con `ThreadPoolExecutor`, cola en memoria y SQLite. RabbitMQ, balanceador y PostgreSQL/S3 pertenecen al diseño conceptual del enunciado, no al prototipo implementado.

## Corrección posterior: desconexión por inactividad

Causa confirmada en `servidor/server.py`: `conexion.settimeout(30)` cerraba el socket TCP persistente tras 30 segundos sin interacción en el menú. El cliente intentaba reutilizarlo y Windows notificaba WinError 10053. Se eliminó ese timeout de inactividad (`settimeout(None)`) y se agregó una prueba de regresión para peticiones consecutivas sobre la misma conexión. La prueba no espera 30 segundos; el cambio de configuración es la corrección directa de la causa. En una versión productiva se recomendaría un heartbeat y reconexión controlada.
