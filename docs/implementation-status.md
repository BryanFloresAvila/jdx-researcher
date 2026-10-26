# Estado de implementación

La entrega implementa el núcleo del MVP y herramientas de evaluación/operación. Las pruebas automatizadas usan proveedores y modelos simulados; no se afirma haber completado el piloto con 30 preguntas ni validado Agents-A1 en el Mac.

| Tareas del plan | Entrega | Validación pendiente |
|---|---|---|
| RP001 | Paquete Python, configuración, manifiesto, CI y captura de lockfile | Registrar runtime y pesos exactos del Mac |
| RP002–004 | Probe de 20 esquemas; pruebas multitur herramientas y transportes MCP | 19/20 en runtime real; recursos 16K/32K; cliente habitual |
| RP005–008 | Contratos, cola, leases, checkpoints, idempotencia y recuperación | Prueba de apagado real en el equipo |
| RP009–012 | SearXNG/GitHub/arXiv, HTML/PDF, versiones, FTS5 y relaciones | Cobertura de proveedores y corpus real |
| RP013–016 | Agente acotado, consultas de vacíos, evidencia paginada, informes y exportación | Cinco investigaciones reales revisadas; pruebas CI son sintéticas |
| RP017 | 30 preguntas propuestas, particiones y formato de pares | Etiquetar 300–450 pares reales |
| RP018–020 | Adaptador opcional Qwen3 y evaluador offline recall/nDCG | Benchmark MPS/CPU y decisión de adopción |
| RP021–022 | Pruebas de fallos, cancelación, recuperación, límites y política de red | Ejecución prolongada con servicios reales |
| RP023–024 | CLI, exportación, backup/restore y documentación | LaunchAgent, validación operativa en Mac y piloto final |

Laya permanece fuera de la ruta de selección. No se ha integrado su checkpoint ni un servidor remoto. El plan de adopción sigue siendo observación, calibración y comparación antes de filtrar.

La variante heretic, session-mesh, OCR, navegador dinámico y síntesis por API cloud no forman parte del baseline entregado. El cliente MCP realiza la síntesis cloud.

La deduplicación implementada identifica copias exactas por hash. La identificación automática de copias aproximadas y familias de resultados compartidos requiere validación adicional. Los scores de ranking no se presentan como probabilidades de verdad.

No se han medido precisión factual, recall del piloto, consumo del M4 Pro ni coste total de investigaciones reales. Esos valores se obtendrán con el corpus revisado y el hardware de referencia.
