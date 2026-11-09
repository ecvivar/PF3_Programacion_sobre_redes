# PFO3 — Rediseño como Sistema Distribuido (Cliente–Servidor)

**Programación sobre Redes — Tecnicatura Superior en Desarrollo de Software, IFTS N.º 29 · 2026**
**Autor: Edison Cristian Vivar** (evolución de la PFO2)
**Repositorio: https://github.com/ecvivar/PF3_Programacion_sobre_redes**

## 1. Descripción y objetivo

Transformar la aplicación de la PFO2 (Flask + `requests` + SQLite) en un sistema
**cliente–servidor con sockets TCP, cola de tareas y workers concurrentes**,
procesando tareas reales. El cliente de consola registra usuarios, inicia sesión
y envía tareas (`suma`, `texto_mayusculas`, `fibonacci`) al servidor, que las
distribuye entre 4 workers y persiste estados y resultados.

> Nota de alcance: hilos + cola = **concurrencia**, no distribución entre
> máquinas. Nginx, RabbitMQ, PostgreSQL y S3 son **conceptuales** (diseño
> exigido por la consigna), no implementados.

**Requisitos y estado**

| Requisito | Estado | Ubicación |
|---|---|---|
| Diagrama con clientes web/móvil, Nginx, workers, RabbitMQ, PostgreSQL, S3 | ✅ Diseño conceptual | `docs/arquitectura_distribuida.png` |
| Servidor Python por socket que distribuye tareas | ✅ Implementado | `servidor/server.py`, `servidor/workers.py` |
| Cliente que envía tareas y recibe resultados | ✅ Implementado | `cliente/client.py` |
| Código en GitHub | ✅ Publicado | https://github.com/ecvivar/PF3_Programacion_sobre_redes |

## 2. Arquitectura conceptual e implementada

### Conceptual (consigna, no implementada)

![Arquitectura conceptual](docs/arquitectura_distribuida.png)
Fuente: [`docs/arquitectura_distribuida.mmd`](docs/arquitectura_distribuida.mmd)
— clientes web/móvil → Nginx/HAProxy → coordinador → RabbitMQ →
servidores worker con pool de hilos → PostgreSQL / S3.

### Implementada (código real)

![Arquitectura implementada](docs/arquitectura_implementada.png)
Fuente: [`docs/arquitectura_implementada.mmd`](docs/arquitectura_implementada.mmd)

```text
Cliente TCP --JSON+\n--> Coordinador (hilo/conexión)
  --> auth (Werkzeug + sesiones 1h) --> SQLite
  --> queue.Queue --> 4 workers (ThreadPoolExecutor)
  <-- PENDIENTE / consultar (polling) <-- COMPLETADA | ERROR
```

Flujo: cliente envía con token → el servidor valida la sesión, crea la tarea
`PENDIENTE` y encola su `task_id` → un worker la reclama atómicamente
(`PENDIENTE → EN_PROCESO`) → ejecuta sin transacción abierta → persiste
`COMPLETADA`/`ERROR` → el cliente consulta por *polling*.

## 3. Tecnologías y estructura

**Usadas (reales):** Python 3.11, `socket` TCP, `json` + `\n` (64 KB),
`queue.Queue`, `ThreadPoolExecutor` (4), `sqlite3` (WAL), Werkzeug
(`generate/check_password_hash`), `secrets.token_hex(32)`, `unittest`.

**Conceptuales (no implementadas):** Nginx/HAProxy, RabbitMQ, PostgreSQL, S3,
TLS, clientes web/móvil.

```text
servidor/    server.py, workers.py, database.py, auth.py
cliente/     client.py
compartido/  protocolo.py
docs/        arquitectura_distribuida.mmd/.png
             arquitectura_implementada.mmd/.png
tests/       6 módulos (61 pruebas)
requirements.txt, .gitignore, README.md
```

## 4. Instalación y ejecución

```powershell
git clone https://github.com/ecvivar/PF3_Programacion_sobre_redes.git
cd PF3_Programacion_sobre_redes
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt   # solo Werkzeug; resto stdlib

# Terminal 1 — servidor (Ctrl+C para detener)
python -m servidor.server 127.0.0.1 5000
# Terminal 2 — cliente
python cliente/client.py
```

Uso: menú **sin sesión** (Registrar → Iniciar sesión → Salir); **con sesión**
(Enviar tarea → Listar mis tareas → Consultar resultado → Cerrar sesión → Salir).

## 5. Funcionamiento del sistema

- **Registro/login/logout:** hash Werkzeug, sesiones con expiración de 1 h,
  token solo en memoria (nunca en disco ni logs).
- **Aislamiento:** consultar/listar filtra por `usuario_id` de la sesión;
  tarea ajena e inexistente devuelven el mismo `tarea_no_encontrada`.
- **Estados:** `PENDIENTE → EN_PROCESO → COMPLETADA | ERROR`.
- **Persistencia:** `pfo3.db` (SQLite WAL) con `usuarios, sesiones, tareas`.
  La `tareas.db` de la PFO2 no se modifica.
- **Recuperación básica:** al reiniciar, `EN_PROCESO` vuelve a `PENDIENTE` y
  los `PENDIENTE` se re-encolan; `COMPLETADA`/`ERROR` no se re-ejecutan.

## 6. Protocolo y pruebas

TCP UTF-8, **una línea JSON por mensaje** (`\n`), lectura por buffer hasta el
delimitador, **64 KB** máx. Cada solicitud lleva `req_id` (uuid) y `op`; cada
respuesta lo repite. Una solicitud por vez dentro de cada conexión (hilo por
conexión).

```json
// Solicitud (enviar tarea)
{
  "req_id": "7f3a2d10-3b1e-4c2f-9d4e-5a6b7c8d9e0f",
  "op": "enviar_tarea",
  "token": "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90",
  "tipo": "suma",
  "payload": { "a": 2, "b": 3 }
}
```

```json
// Respuesta (éxito, tarea aceptada)
{
  "req_id": "7f3a2d10-3b1e-4c2f-9d4e-5a6b7c8d9e0f",
  "ok": true,
  "task_id": "b2c3d4e5-6f78-49ab-bcde-0f1e2d3c4b5a",
  "estado": "PENDIENTE"
}
```

```json
// Respuesta (error tipificado)
{
  "req_id": "7f3a2d10-3b1e-4c2f-9d4e-5a6b7c8d9e0f",
  "ok": false,
  "error": { "codigo": "credenciales_invalidas", "mensaje": "Usuario o contraseña incorrectos" }
}
```

| `op` | Campos | Token |
|---|---|---|
| `registro`, `login` | `usuario`, `contraseña` | No |
| `logout`, `listar_tareas` | — | Sí |
| `enviar_tarea` | `tipo`, `payload` | Sí |
| `consultar_tarea` | `task_id` | Sí |

Errores: `json_invalido`, `sobre_invalido`, `mensaje_demasiado_grande`,
`op_desconocida`, `autenticacion_requerida`, `credenciales_invalidas`,
`usuario_duplicado`, `payload_invalido`, `tarea_no_encontrada`, `error_interno`.

```powershell
python -m unittest discover -s tests -v
```

Resultado real: **61/61 OK** (verificado en la ejecución de esta revisión).

## 7. Alcance y limitaciones

1. **Hilos ≠ máquinas**: 1 sola instancia admitida; sin escalado horizontal.
2. Recuperación **básica** (no exactamente-una-vez ante cualquier fallo).
3. TCP **sin TLS** — solo `127.0.0.1`; contraseñas con hash, tokens 1 h.
4. `listar_tareas` capado a 100; sin rate-limiting; cierre abrupto puede
   generar RST (ruido, sin impacto funcional).

**Futuro:** RabbitMQ + PostgreSQL + S3, Nginx/HAProxy, TLS, paginación,
escalado horizontal.
