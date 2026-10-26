# Arquitectura y contratos

Un proceso FastAPI aloja el scheduler y el transporte MCP. El proxy stdio comunica con ese daemon mediante HTTP autenticado; no crea workers ni carga modelos.

El flujo LangGraph contiene planning → retrieving → analyzing → packaging. Los checkpoints guardan progreso, no sustituyen al repositorio de evidencia. Cada consulta, lectura y salida del modelo tiene una clave persistente. Los artefactos se confirman antes de avanzar el checkpoint.

SQLite evidencia conserva:
- Solicitudes y su huella para idempotencia.
- Estado, lease, propietario, deadline y contadores de consumo.
- Eventos de etapa, consultas y errores.
- Fuentes, versiones por hash, fragmentos, índices FTS5 y hallazgos.
- Informes versionados y sus incidencias de revisión.

Los cuerpos y textos se conservan por SHA-256 en objects/. El identificador de fuente depende de URL final y hash de bytes; el fragmento depende de la versión y sus offsets. Los espejos exactos comparten family_id; esto identifica duplicados, no todas las posibles dependencias entre publicaciones.

## Herramientas

| MCP | HTTP |
|---|---|
| research_start(request) | POST /v1/research |
| research_status(job_id) | GET /v1/research/{job_id} |
| research_result(job_id, cursor, limit) | GET /v1/research/{job_id}/result |
| research_source(job_id, source_id, passage_id?) | GET /v1/research/{job_id}/sources/{source_id} |
| research_finalize(job_id, report) | POST /v1/research/{job_id}/finalize |
| research_cancel(job_id) | POST /v1/research/{job_id}/cancel |

Los modelos Pydantic rechazan campos desconocidos. El esquema público tiene versión 1.0.

queued → running → awaiting_review/partial/failed. La cancelación afecta queued/running. El informe estructuralmente válido cambia awaiting_review/partial a completed. review_status es independiente: pending, reviewed o needs_attention.

## Reintentos y límites

Un lease y heartbeat protegen cada trabajo; un flock limita a un daemon por directorio. Se comprueba propietario, deadline y estado antes de guardar artefactos. Reanudar no amplía el deadline.

Una respuesta persistida se reutiliza. Una llamada externa que terminó sin llegar a persistirse puede repetirse, con un nuevo cargo al presupuesto. No se garantiza exactamente una ejecución de peticiones externas.

Se permite una reparación de argumentos inválidos. Los errores HTTP no generan reintentos invisibles; el trabajo conserva el error y, si existe evidencia, un resultado parcial. Los modelos no pueden elevar presupuestos, añadir herramientas ni guardar decisiones como revisiones humanas.

## Confianza y alcance

Las fuentes entran como datos con procedencia. No hay herramienta de shell o ejecución de código en el worker. Un backend HTTP propio verifica las direcciones resueltas y conecta al IP comprobado preservando el hostname TLS. Las redirecciones vuelven a validarse. No se reenvían credenciales de los servicios al lector.

Los filtros de red reducen SSRF; una aplicación personal no debe exponerse a Internet sin una capa de despliegue y autenticación apropiada. Este daemon solo permite bind local y exige un token para HTTP/MCP.

La selección determina relevancia. El registro de hallazgos propone relaciones. La revisión cloud o humana decide si las fuentes realmente sostienen la conclusión. Las validaciones de citas no reemplazan esa revisión.

## Fuentes de integración

- [SDK MCP Python](https://github.com/modelcontextprotocol/python-sdk), API v1 fijada por uv.lock.
- [Persistencia LangGraph](https://docs.langchain.com/oss/python/langgraph/persistence).
- [SearXNG API](https://docs.searxng.org/dev/search_api.html).
- [HTTPX transportes](https://www.python-httpx.org/advanced/transports/).
- [Qwen3-Reranker-0.6B](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B).

Las dependencias instaladas y los parámetros configurados aparecen en doctor. Las revisiones de pesos y templates deben registrarse al validar en el Mac.
