# Validación pendiente en Mac M4 Pro de 24 GB

La implementación no sustituye las mediciones sobre el equipo real.

1. Instala el proyecto con uv sync --extra dev.
2. Ejecuta el modo demo y conecta tu cliente MCP por stdio.
3. Levanta el modelo original Agents-A1-4B GGUF Q4_K_M con alias research-worker.
4. Verifica GET /v1/models del runtime y ejecuta probe-model.
5. Configura SearXNG con JSON. Ejecuta doctor y corrige los servicios que no respondan.
6. Sirve el modo local y ejecuta examples/research-request.json con una nueva clave.
7. Revisa evidencia, fechas, condiciones de benchmark y contradicciones antes de finalizar.
8. Compara contexto de 16384 y 32768 con las mismas preguntas.

Conserva junto a cada medición: macOS, chip, memoria, runtime y commit, revisión exacta del GGUF, SHA-256, template, tamaño de contexto y doctor. El manifiesto inicial marca revisiones sin verificar; no inventa hashes.

## Recursos

En una terminal separada, registra vm_stat, memory_pressure y sysctl vm.swapusage antes y después. Usa /usr/bin/time -l al ejecutar probe-model para guardar tiempo y memoria del cliente, y Activity Monitor o las métricas del runtime para la memoria del servidor del modelo.

La memoria del cliente no representa la memoria de llama-server. El probe mide integración y latencia observada; las velocidades de generación y prefill deben obtenerse del runtime. Realiza calentamiento y varias repeticiones antes de comparar p50 y p95.

Empieza con el ranking léxico. Cargar Qwen3-Reranker añade memoria y latencia; prueba CPU y MPS bajo las mismas tareas antes de elegir.

## Datos y arranque

El directorio de datos de configs/local.yaml es relativo al directorio desde el que se ejecuta el daemon. El ejemplo de cliente usa uv --directory para mantenerlo estable. Puedes definir una ruta absoluta en JDX_DATA_DIR.

El daemon se ejecuta en primer plano y se detiene con Ctrl-C. La instalación de un LaunchAgent no se automatiza en esta entrega: primero valida la configuración y el directorio de trabajo. Cerrar el cliente MCP conserva el trabajo si el daemon sigue vivo; al reiniciar el daemon se recuperan los trabajos interrumpidos.

No se ha instalado ni ejecutado ningún modelo en tu Mac desde esta sesión.
