# Capturas y evidencias — PFO3

> Las capturas de pantalla de PowerShell **deben obtenerse manualmente en
> Windows** (el entorno de desarrollo no captura la pantalla interactiva).
> Deben corresponder a ejecuciones reales, sin contraseñas ni tokens
> visibles. Al subirlas a GitHub, colocarlas en esta carpeta con los nombres
> indicados y referenciarlas desde el README.

## Capturas requeridas (9)

| N.º | Archivo sugerido | Qué muestra |
|---|---|---|
| 1 | `01_servidor_iniciado.png` | Terminal 1: `python -m servidor.server 127.0.0.1 5000` + `[motor] 4 workers iniciados` + `[servidor] escuchando` |
| 2 | `02_cliente_conectado.png` | Terminal 2: menú sin sesión (Registrar / Iniciar sesión / Salir) |
| 3 | `03_registro_usuario.png` | Registro exitoso (`ana` → *Usuario registrado correctamente*) |
| 4 | `04_login_exitoso.png` | Login (`Bienvenido, ana.`, contraseña oculta) |
| 5 | `05_envio_tarea.png` | Envío `suma 2 3` → `Tarea creada: <uuid> / Estado: PENDIENTE` |
| 6 | `06_resultado_completado.png` | Consultar → `COMPLETADA / Resultado: 5` (quizá `fibonacci` → `55`) |
| 7 | `07_listado_tareas.png` | Listar mis tareas (id / tipo / estado) |
| 8 | `08_dos_usuarios.png` | Dos usuarios con tareas independientes (o salida de `test_flujo_completo_dos_usuarios_simultaneos`) |
| 9 | `09_pruebas.png` | `python -m unittest discover -s tests -v` → `Ran 61 tests ... OK` |

## Cómo obtenerlas (PowerShell + captura con Windows)

1. Terminal 1:
   ```powershell
   python -m servidor.server 127.0.0.1 5000
   ```
   Esperar: `[motor] 4 workers iniciados` + `[servidor] escuchando en 127.0.0.1:5000`.
   `Win+Shift+S` → captura → guardar en esta carpeta como `01_...png`.

2. Terminal 2:
   ```powershell
   python cliente/client.py
   ```
   Seguir el flujo (§8 del README): registrar, login, enviar tarea, listar,
   consultar, logout. Capturar cada paso.
   Repetir con un segundo usuario (`bob`) para `08_...`.

3. En Terminal 1 (o una nueva):
   ```powershell
   python -m unittest discover -s tests -v
   ```
   Capturar la última línea `Ran 61 tests ... OK` como `09_...png`.

## Evidencia terminal (real, sin pantallas)

- [`evidencia_terminal.txt`](evidencia_terminal.txt): salida de cliente TCP
  real contra el servidor (registro→login→3 tareas→listado→logout), generada
  durante la Etapa 6. No es una captura de pantalla, sino registro de
  ejecución verificable.

## Cuándo volver a ejecutar

No generar evidencias con datos ficticio/simulados presentándolos como reales.
Si se regenera evidencia, mantener el mismo formato y dejar constancia.
