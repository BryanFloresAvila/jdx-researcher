# Plan de implementación de Researcher Pro

Versión 1.0 · 26 de octubre de 2026 · Entorno de referencia: MacBook Pro M4 Pro con 24 GB de memoria unificada.

Researcher Pro investigará una pregunta mediante un modelo local, recuperará fuentes y conservará los fragmentos que sustentan o contradicen sus hallazgos. Un modelo cloud definirá el objetivo y redactará el informe a partir de ese material. El primer producto será una herramienta personal accesible por MCP, con trabajos persistentes y resultados auditables.

La primera entrega utilizará Agents-A1-4B original. La variante heretic, el reranker y Laya se incorporarán como comparaciones separadas para identificar su aporte. Los límites y umbrales de este plan son objetivos iniciales de ingeniería; se ajustarán con mediciones locales.

# Resultado y alcance

## Resultado esperado

Ante una consulta como «Compara servidores locales de modelos para mi Mac, verifica tool calling y sustenta la recomendación», el sistema deberá:

1. Convertir el objetivo en hasta cuatro subpreguntas con restricciones explícitas.
2. Buscar fuentes, abrirlas y conservar el contenido consultado.
3. Separar mediciones de sus autores, reproducciones independientes y opiniones.
4. Relacionar afirmaciones con fragmentos de evidencia y sus condiciones.
5. Identificar contradicciones, información ausente y diferencias de versiones.
6. Entregar al modelo cloud un paquete compacto con referencias recuperables.
7. Guardar el informe y señalar qué afirmaciones requieren revisión.
8. Reanudar el trabajo después de cerrar y volver a abrir el cliente MCP.

El MVP cubrirá investigación técnica en español e inglés, páginas HTML, documentación, repositorios y PDF con texto seleccionable. El navegador para páginas dinámicas se añadirá durante el endurecimiento si los casos reales lo necesitan. OCR, sitios autenticados, entrenamiento de modelos y operación distribuida quedarán para entregas posteriores.

## Dos modos de uso

| Modo | Funcionamiento | Entrega |
| --- | --- | --- |
| Cliente MCP con modelo cloud | El cliente planifica; Researcher Pro investiga localmente; el cliente revisa y redacta | Modo inicial |
| Ejecución mediante API | El servicio llama al proveedor cloud para planificación y síntesis | Extensión posterior |

El modo inicial permite usar un cliente MCP local que ya esté disponible. La conexión a servicios cloud desde un navegador requiere comprobar su transporte y su acceso a la máquina; no se asumirá que un servicio remoto puede alcanzar localhost. La tarifa de una suscripción y el consumo de una API se presupuestarán por separado cuando correspondan.

# Decisiones de arquitectura

| Área | Decisión inicial | Motivo y condición |
| --- | --- | --- |
| Lenguaje del núcleo | Python 3.12 y uv | Integración con modelos, extracción y evaluación; fijar versiones compatibles |
| Motor del flujo | LangGraph con checkpoints persistentes | Hacer explícitas las etapas, los reintentos y la reanudación |
| Interfaz externa | SDK oficial de MCP para Python | Mantener estable el contrato frente al cliente |
| Servicio local | FastAPI y Uvicorn, con API de trabajos y transporte MCP compatible | Los trabajos sobreviven a la desconexión del cliente |
| Modelo de investigación | Agents-A1-4B original en GGUF Q4_K_M | Referencia inicial, con revisión exacta del modelo y del archivo |
| Runtime del modelo | llama.cpp con Metal y llama-server | Endpoint local y control explícito del runtime |
| Runtime alternativo | Ollama mediante un adaptador | Usarlo si supera las pruebas del protocolo y aporta mejor operación |
| Búsqueda general | SearXNG privado con salida JSON | Primer proveedor sin fijar una tarifa por consulta; depende de motores externos |
| Búsqueda especializada | Adaptadores de GitHub y arXiv | Recuperar metadatos y fuentes primarias; habilitarlos según la pregunta |
| Lectura HTML | HTTPX y Trafilatura | Extraer contenido y metadatos con bajo coste de inferencia |
| PDF | PyMuPDF para documentos con texto | Preservar páginas y localización de fragmentos |
| Evidencia y cola | SQLite, modo WAL, migraciones y objetos por hash | Instalación local sencilla y trazabilidad |
| Recuperación interna | SQLite FTS5 y candidatos por subpregunta | Evitar una base vectorial durante el MVP |
| Reranker | Qwen3-Reranker-0.6B, después del baseline | Comparación especializada de consulta y fragmento |
| Juez experimental | Laya, con adaptador y posibilidad de desactivarlo | Medir utilidad sin convertirlo en requisito del producto |
| Validación y pruebas | Pydantic, pytest y Ruff | Contratos tipados, pruebas de comportamiento y calidad estática |

**Relación con session-mesh-mcp.** Se conserva como integración opcional para coordinar herramientas o sesiones existentes. El estado del trabajo y la evidencia pertenecerán a Researcher Pro. No se instalarán dos motores que planifiquen, reintenten y persistan la misma tarea. Si interesa reutilizar session-mesh-mcp, la fase 0 evaluará su modo run_pipeline como alternativa al motor interno; cualquier sustitución deberá preservar los contratos y superar las mismas pruebas de reanudación.

**Relación con ResearchHarness.** Servirá como referencia del bucle de herramientas y de trazas. Adoptarlo como dependencia exigirá comprobar sus dependencias externas y evitar un segundo bucle de control del mismo agente.

La documentación de LangGraph diferencia checkpoints de ejecución y almacenamiento de datos entre trabajos. El plan usa esa separación: el checkpoint permite retomar la ejecución; la base de evidencia conserva las fuentes y las afirmaciones de forma independiente. Véanse las fuentes técnicas al final.

# Procesos y flujo de ejecución

## Procesos locales

| Proceso | Responsabilidad |
| --- | --- |
| Daemon de Researcher Pro | API, cola persistente, worker del flujo y servidor MCP |
| Servidor del modelo | Inferencia de Agents-A1-4B |
| SearXNG privado | Consultas a los motores habilitados |
| Proxy MCP por stdio, cuando se necesite | Traducir el transporte del cliente hacia el daemon; no cargar modelos |

Se utilizará un daemon por perfil de usuario. Abrir dos clientes MCP no duplicará los modelos ni creará dos colas para el mismo perfil. Las direcciones serán configurables y el preflight detectará puertos ocupados sin detener servicios ajenos.

## Flujo lógico

1. **Validar:** comprobar objetivo, restricciones, subpreguntas, presupuesto e idempotencia.
2. **Buscar:** generar o ejecutar consultas acotadas por subpregunta e idioma.
3. **Leer:** descargar, extraer, deduplicar y registrar documentos.
4. **Recuperar:** localizar fragmentos candidatos para cada subpregunta.
5. **Ordenar:** aplicar el selector del experimento activo y preservar diversidad y contradicciones conocidas.
6. **Relacionar:** proponer afirmaciones y relaciones de apoyo, contradicción o evidencia insuficiente.
7. **Revisar vacíos:** buscar evidencia adicional si quedan cuestiones esenciales y presupuesto.
8. **Entregar evidencia:** detener el worker y exponer el material al cliente cloud.
9. **Finalizar:** recibir el informe, validar sus referencias y guardarlo con su estado de revisión.

El presupuesto y la cobertura controlarán el bucle. Un texto recuperado no podrá ampliar herramientas, presupuesto ni permisos del agente.

# Contratos de entrada y salida

## Solicitud de investigación

La solicitud incluirá un objetivo, restricciones, idioma de salida, subpreguntas opcionales, fecha de referencia, presupuesto y clave de idempotencia. Si faltan subpreguntas, el worker propondrá una descomposición acotada.

Ejemplo del contrato que se implementará:

```json
{
  "objective": "Comparar runtimes locales para investigación técnica",
  "constraints": {
    "hardware": "Apple M4 Pro",
    "unified_memory_gb": 24,
    "must_support": ["tool_calling", "local_inference"]
  },
  "languages": ["es", "en"],
  "output_language": "es",
  "subquestions": [
    "Compatibilidad y soporte de herramientas",
    "Memoria y rendimiento medidos",
    "Madurez y limitaciones documentadas"
  ],
  "as_of": "2026-10-26",
  "budget_profile": "balanced",
  "idempotency_key": "runtime-comparison-example-001"
}
```

La clave de idempotencia se limitará al perfil de usuario. Repetir clave y solicitud devolverá el mismo trabajo. Reutilizar la clave con un contenido diferente devolverá un conflicto.

## Herramientas MCP

| Herramienta propuesta | Entrada principal | Salida y comportamiento |
| --- | --- | --- |
| research_start | Solicitud tipada | Devuelve job_id y estado; no espera a que termine la investigación |
| research_status | job_id | Etapa, presupuesto consumido, cobertura, errores y eventos recientes |
| research_result | job_id y cursor opcional | Paquete de evidencia paginado y estado de revisión |
| research_source | job_id, source_id y localización | Fragmento original, metadatos y procedencia |
| research_finalize | job_id e informe estructurado | Valida referencias, conserva avisos y guarda una versión del informe |
| research_cancel | job_id | Registra cancelación y detiene el trabajo en el siguiente punto cooperativo |

El contrato incluirá schema_version. No se devolverán HTML completo ni historiales de razonamiento por defecto. Los fragmentos tendrán identificadores estables y límites de tamaño. El cliente podrá recuperar más evidencia por necesidad.

## Estados del trabajo

| Estado | Significado |
| --- | --- |
| queued | Solicitud persistida y pendiente |
| running | Ejecución en curso, con etapa y checkpoint |
| awaiting_review | Evidencia lista para revisión y redacción |
| completed | Informe guardado y referencias estructurales válidas |
| partial | Resultado útil con límites, vacíos o errores explícitos |
| cancelled | Trabajo cancelado |
| failed | Sin resultado útil recuperable |

completed no certifica la verdad del informe. La revisión factual tendrá un campo separado con valores pending, reviewed o needs_attention y una lista de incidencias. Si se agota el presupuesto con evidencia insuficiente, el resultado será parcial.

# Modelo de datos y procedencia

| Entidad | Campos esenciales |
| --- | --- |
| Job | Identificador, solicitud, estado, etapa, presupuesto, fechas e idempotencia |
| Subquestion | Pregunta, restricciones, cobertura y vacíos |
| SearchRun | Proveedor, consulta, idioma, parámetros, fecha y resultados |
| Source | URL original y final, URL canónica, título, autor, tipo y fechas |
| DocumentVersion | Hash de bytes, hash de texto, extractor y versión, fecha de captura |
| Passage | Documento, texto, página o rango, offsets y hash |
| Claim | Afirmación atómica, condiciones, unidades, alcance y subpregunta |
| EvidenceLink | Claim, passage, relación, origen del juicio y estado de revisión |
| Decision | Selector o modelo, versión, entrada referenciada, score y latencia |
| Event | Trabajo, etapa, acción, intento, estado y consumo |
| Report | Versión, texto, afirmaciones, referencias y avisos |
| EvaluationRun | Dataset, partición, configuración, resultados y entorno |

## Reglas de conservación

- La fecha de consulta no sustituirá una fecha de publicación desconocida.
- Una actualización de una web creará una nueva versión; no sobrescribirá la evidencia previa.
- Las citas se resolverán contra la versión de documento utilizada por el trabajo.
- Un fragmento citado deberá localizarse en el texto extraído conservado.
- Se distinguirá el contenido original de una traducción o un resumen.
- Una fuente podrá respaldar una afirmación y contradecir otra.
- Las relaciones propuestas por modelos llevarán ese origen; no se presentarán como anotaciones humanas.
- Las copias exactas se agruparán por hash. Las copias aproximadas y publicaciones que remiten a un mismo resultado se marcarán como familia probable, conservando los originales.

Los objetos descargados se guardarán en disco por hash. SQLite conservará sus metadatos y referencias. Se almacenarán los originales necesarios para reproducir las comprobaciones; las políticas de retención podrán retirar cuerpos antiguos sin borrar el registro de procedencia.

# Recuperación, lectura y selección

## Búsqueda

El proveedor implementará una interfaz común con consulta, idioma, límite, filtros admitidos y resultados normalizados. La primera integración será una instancia privada de SearXNG con JSON habilitado. Un preflight comprobará esa salida y los motores disponibles. SearXNG depende de motores externos y puede sufrir bloqueos o cobertura irregular.

Un segundo proveedor será un adaptador intercambiable para una API de búsqueda. Se habilitará con credenciales y presupuesto configurado, si las mediciones muestran que mejora cobertura o estabilidad. Su coste se tomará de la tarifa vigente al configurarlo.

Los adaptadores de GitHub y arXiv se priorizarán para preguntas sobre repositorios y publicaciones. Una API devolverá solo los campos que realmente ofrece; estrellas, descargas o antigüedad no se convertirán automáticamente en madurez o calidad.

## Lectura

Cada fetch registrará estado HTTP, redirecciones, tipo de contenido y duración. HTML y PDF se procesarán por rutas separadas. Los errores de extracción se harán visibles para que el agente no trate un documento vacío como ausencia de evidencia.

El extractor conservará encabezados, tablas y bloques de código cuando sean relevantes. La fragmentación comenzará con aproximadamente 600 tokens y 80 de solapamiento, ajustándose a límites de sección. Son parámetros iniciales, no tamaños óptimos demostrados.

## Selección

La versión de referencia utilizará búsqueda textual y selección por subpregunta. El reranker se añadirá sin modificar los documentos recuperados en la comparación offline.

El selector reservará candidatos para:

- Cada subpregunta esencial.
- Evidencia primaria disponible.
- Limitaciones y evidencia contraria identificada.
- Resultados independientes cuando existan.
- Versiones vigentes para la pregunta, conservando versiones anteriores relevantes.

No se rellenará una cuota con material irrelevante. Si no existe evidencia suficiente de una categoría, se registrará el vacío. Reducir documentos redundantes no podrá borrar el único soporte de una afirmación material.

# Agente local y revisión cloud

## Herramientas del worker local

El modelo verá herramientas internas de búsqueda, lectura de fragmentos, registro de hallazgos y consulta de cobertura. El código validará sus argumentos y ejecutará las acciones. Los argumentos inválidos recibirán un error breve y, como máximo, un intento de reparación.

La configuración registrará prompt, chat template, parser, revisión del modelo, cuantización y parámetros de generación. El modelo original será el baseline; la variante heretic tendrá un perfil separado.

El worker deberá distinguir entre lo leído y lo inferido. Sus afirmaciones propuestas incluirán las condiciones relevantes: hardware, versión, fecha, unidades, dataset o configuración del experimento. Cuando falten condiciones, la relación quedará pendiente de revisión.

## Paquete de evidencia para el modelo cloud

El paquete contendrá objetivo, restricciones, subpreguntas, hallazgos, referencias, contradicciones y vacíos. Incluirá fragmentos originales suficientes para revisar los hallazgos más importantes, junto con identificadores para recuperar más material.

El cliente cloud redactará el informe manteniendo las diferencias entre evidencia publicada e inferencia. Las discrepancias entre fuentes se resolverán comprobando alcance, fecha, versión y método, o se dejarán abiertas si no hay pruebas para resolverlas.

## Validaciones al finalizar

research_finalize comprobará que:

1. Cada referencia existe y pertenece al trabajo.
2. Los fragmentos citados corresponden a la versión almacenada.
3. Los identificadores y las relaciones del informe tienen una estructura válida.
4. Las afirmaciones que se presentan como verificadas incluyen evidencia y estado de revisión.
5. Los vacíos y contradicciones abiertos no desaparecen del resultado sin una resolución registrada.

La validación estructural no reemplazará la revisión semántica. Una afirmación mal respaldada podrá detectarse mediante revisión cloud o auditoría humana; el sistema conservará esa decisión y su evidencia.

# Persistencia, límites y recuperación

## Recuperación después de fallos

La cola estará en SQLite, con un único scheduler propietario durante el MVP. Cada trabajo tendrá lease, heartbeat y una clave de idempotencia por acción. Al reiniciar, el scheduler recuperará leases vencidos y comprobará los artefactos ya completados antes de repetir una acción.

Los checkpoints del flujo y los datos de evidencia tendrán responsabilidades separadas. Una escritura de artefacto se confirmará antes de avanzar el checkpoint. Si el proceso falla entre ambos pasos, la reanudación reconocerá el artefacto por su clave y reutilizará el resultado.

Las peticiones externas pueden repetirse si el proveedor no admite idempotencia y la conexión se pierde antes de persistir la respuesta. El sistema registrará esos intentos y reservará presupuesto para reintentos. No se prometerá ejecución exactamente una vez frente a servicios externos.

La cancelación evitará nuevas acciones y nuevas escrituras de resultados tardíos. Las peticiones HTTP cancelables se interrumpirán; una inferencia que el runtime no pueda cancelar quedará limitada por su timeout.

## Perfil inicial balanced

| Parámetro | Valor inicial propuesto |
| --- | --- |
| Subpreguntas | Hasta 4 |
| Consultas de búsqueda por trabajo | Hasta 12, incluidos reintentos facturables |
| Documentos descargados | Hasta 30 |
| Subtareas lógicas activas | Hasta 2 |
| Inferencias generativas simultáneas | 1 |
| Descargas simultáneas | 4 |
| Pasos del agente | Hasta 60, sujetos también a los límites de consultas y lecturas |
| Contexto del worker | Comparar 16K y 32K; elegir después de medir |
| Límite de duración | 20 minutos para el perfil; configurable |
| Tamaño máximo de HTML | 5 MiB descargados |
| Tamaño máximo de PDF | 20 MiB y 50 páginas procesadas |
| Paquete cloud inicial | Objetivo de 8K tokens; paginar el resto |
| Reparaciones por llamada inválida | 1 |
| Nuevas rondas para cubrir vacíos | Hasta 2, dentro del mismo presupuesto |
| Reranker | Lotes de 1, 4 y 8 para elegir una configuración local |
| Laya | Desactivado durante el MVP |

Los límites de descarga se comprobarán durante el streaming, no solo con Content-Length. Cuando una fuente exceda un límite, se registrará el motivo y se buscará una alternativa o se marcará el vacío.

## Costes

Se registrarán consultas, lecturas de pago, reintentos y uso de API cloud cuando sea observable. Los precios tendrán proveedor, fecha y unidad. Antes de una acción de pago se reservará su coste estimado; al terminar se conciliará con el uso conocido.

Un proveedor de pago requerirá una tarifa configurada y un límite monetario antes de habilitarse. Si el coste final no es observable, se conservará la estimación como tal y se mostrarán sus límites, sin inventar una factura. El modelo local reduce llamadas a proveedores de inferencia, pero no elimina el coste de búsquedas, lectura externa, electricidad o suscripciones.

En el modo de cliente MCP, los tokens totales usados por el proveedor cloud pueden no estar disponibles para el daemon. Se registrará el tamaño enviado como una aproximación separada y no como una factura conocida.

# Plan de trabajo

Las estimaciones corresponden a una persona desarrolladora. Incluyen implementación y revisión activa de cada fase. Las descargas y ejecuciones de evaluación pueden ampliar el tiempo de calendario; no se suponen fechas comprometidas ni trabajo paralelo de varias personas.

| Fase | Esfuerzo | Entregable principal | Dependencia |
| --- | --- | --- | --- |
| 0 Compatibilidad y referencia | 6 a 8 horas | Configuración reproducible del modelo y cliente | Inicio |
| 1 Núcleo y trabajos MCP | 10 a 14 horas | Crear, consultar, cancelar y reanudar trabajos | Fase 0 |
| 2 Recuperación y evidencia | 16 a 22 horas | Búsqueda, lectura y fuentes versionadas | Fase 1 |
| 3 Agente y entrega cloud | 16 a 22 horas | MVP completo con informe y referencias | Fase 2 |
| 4 Reranker y evaluación | 12 a 18 horas | Comparación de calidad y coste | Fase 3 |
| 5 Operación y publicación local | 12 a 16 horas | Versión personal estable | Fase 4 |
| Experimento Laya | 12 a 20 horas adicionales | Decisión documentada de adopción | Fase 4 |

**MVP hasta fase 3: 48 a 66 horas. Versión estable hasta fase 5: 72 a 100 horas.** A cuatro horas efectivas al día, la versión estable requiere aproximadamente 18 a 25 días de trabajo. La medición de rendimiento puede cambiar la elección de runtime y los límites, por lo que se realiza antes de ampliar el producto.

## Fase 0 Compatibilidad y referencia

**RP001. Crear la referencia reproducible.** Preparar el repositorio, entorno Python y manifiesto con macOS, hardware, runtime, revisión del modelo, hash del GGUF, cuantización, template y parser. Fijar las versiones verificadas en el lockfile; no inventar números de versiones antes de resolver la compatibilidad.

**RP002. Verificar el bucle de herramientas.** Ejecutar 20 casos que cubran una llamada, varias llamadas, argumentos inválidos, respuestas de herramientas, abstención y fin de tarea. El baseline deberá completar correctamente al menos 19 de 20; este umbral sirve como control inicial de integración, no como estimación estadística del rendimiento real.

**RP003. Medir recursos.** Comparar contexto de 16K y 32K, calentamiento y ejecución sostenida. Registrar tiempo de carga, velocidad de generación, tiempo hasta la primera respuesta, memoria del proceso y swap inicial y final. Seleccionar la configuración que complete la tarea sin presión sostenida de memoria ni crecimiento continuo de swap.

**RP004. Verificar el cliente MCP.** Probar descubrimiento, llamada y reconexión con un cliente MCP local. Documentar el transporte que se verificó. Si se evalúa session-mesh-mcp, limitar esta comprobación a un spike de dos horas y conservar el motor base si no supera los contratos.

**Salida de fase:** manifiesto de entorno, resultados de 20 casos y decisión sobre runtime. Si el original no funciona correctamente con herramientas, resolver template, parser o adaptador antes de sustituir el modelo.

## Fase 1 Núcleo y trabajos MCP

**RP005. Implementar contratos.** Crear modelos Pydantic para solicitud, presupuesto, fuente, fragmento, afirmación y resultado. Versionar los esquemas.

**RP006. Crear cola y eventos.** Implementar migraciones, idempotencia, leases, heartbeat, registro de eventos y recuperación. Usar un flujo simulado sin búsquedas para probar los estados.

**RP007. Exponer herramientas MCP.** Implementar el contrato de seis herramientas y el proxy stdio si el cliente lo necesita. Mantener los modelos fuera del proxy.

**RP008. Probar reanudación.** Cerrar el cliente con un trabajo activo, reiniciar el daemon y repetir una solicitud con la misma clave. Comprobar que se conserva el trabajo y no se duplican sus artefactos. Verificar cancelación y aislamiento entre trabajos.

**Salida de fase:** el cliente puede iniciar, consultar, cancelar y recuperar un trabajo simulado después de un reinicio.

## Fase 2 Recuperación y evidencia

**RP009. Integrar búsqueda.** Implementar SearXNG privado y su comprobación JSON. Añadir los adaptadores mínimos de GitHub y arXiv con límites de proveedor y metadatos de procedencia.

**RP010. Implementar lectura y extracción.** Crear rutas de HTML y PDF, timeouts, límites de bytes, localizadores y errores tipados. Conservar texto extraído y objetos por hash.

**RP011. Implementar deduplicación y caché.** Registrar URL original y canónica sin eliminar parámetros funcionales. Reutilizar capturas válidas cuando corresponda y permitir refresco explícito. Las consultas de «último» deberán revalidar fuentes relevantes.

**RP012. Crear fragmentos y relaciones.** Fragmentar por secciones, indexar en FTS5 y guardar evidencia con versiones. Las relaciones semánticas permanecerán propuestas hasta revisión.

**Salida de fase:** un corpus de prueba con HTML, tablas, PDF, duplicados, redirecciones y páginas modificadas mantiene citas recuperables a la versión correcta. Ninguna fecha desconocida se convierte en una fecha inventada.

## Fase 3 Agente y entrega cloud

**RP013. Implementar el worker.** Conectar el modelo al conjunto reducido de herramientas, validar argumentos, limitar reparaciones y registrar consumo. Incorporar el control de cobertura por subpregunta.

**RP014. Añadir el bucle de vacíos.** Proponer búsquedas adicionales cuando falte evidencia y detenerse por cobertura o presupuesto. Detectar repetición de consultas sin hallazgos nuevos.

**RP015. Construir el paquete cloud.** Entregar hallazgos, fragmentos, contradicciones y preguntas abiertas en un tamaño acotado. Implementar recuperación paginada de fuentes.

**RP016. Finalizar y exportar.** Recibir el informe estructurado, verificar sus referencias y guardar Markdown y JSON de resultados. Conservar avisos y estado de revisión.

**Salida de fase y MVP:** completar cinco investigaciones de demostración, al menos dos en español. Cada afirmación material del informe debe tener una referencia o una etiqueta explícita de inferencia, incertidumbre o falta de evidencia. El cliente puede volver a abrir un trabajo y recuperar su informe.

## Fase 4 Reranker y evaluación

**RP017. Construir el conjunto de evaluación.** Preparar las 30 preguntas y los fragmentos etiquetados descritos en la sección de evaluación. Separar ajuste y prueba por pregunta.

**RP018. Integrar el reranker.** Implementar Qwen3-Reranker-0.6B mediante un adaptador. Comprobar inferencia MPS; usar CPU como fallback registrado si la compatibilidad o la estabilidad lo requieren. No tratar un score de relevancia como probabilidad de verdad.

**RP019. Comparar variantes.** Ejecutar baseline sin reranker y variante con reranker sobre el mismo corpus. Evaluar después el flujo completo con búsqueda en vivo, registrando cambios de fuentes y fechas.

**RP020. Decidir el perfil por defecto.** Elegir el selector que preserve cobertura y calidad bajo el presupuesto. Mantener el selector anterior si el reranker no aporta una mejora útil. Documentar el resultado y los límites de muestra.

**Salida de fase:** informe de evaluación reproducible con calidad, coste, latencia y memoria por variante. Las pruebas de modelos heretic y de cuantizaciones se realizarán por separado, cambiando una variable cada vez.

## Fase 5 Operación y publicación local

**RP021. Endurecer fallos reales.** Probar 429, 5xx, timeouts, respuesta vacía, extracción fallida, cierre de proceso, agotamiento de presupuesto y resultados tardíos después de cancelar.

**RP022. Validar el tratamiento de fuentes no confiables.** Comprobar que instrucciones dentro de documentos no cambian herramientas ni presupuestos. El lector web no accederá a destinos locales o privados; las conexiones a servicios locales configurados usarán un canal separado.

**RP023. Empaquetar la operación.** Crear comandos de instalación, doctor, inicio, parada, consulta, exportación y backup. En macOS, verificar el arranque opcional mediante LaunchAgent. Documentar cómo desinstalar sin borrar evidencia sin intención expresa.

**RP024. Cerrar la entrega.** Ejecutar los criterios de aceptación, registrar limitaciones y guardar un ejemplo de investigación exportada. Añadir una página de estado simple solo si facilita la operación; el dashboard completo no bloquea esta entrega.

**Salida de fase:** instalación reproducible, recuperación verificada, exportaciones y backup restaurable en el mismo equipo.

# Evaluación y criterios de aceptación

## Datos

El conjunto inicial tendrá 30 preguntas: 10 para ajustar prompts y parámetros, 10 para calibrar umbrales y 10 reservadas para la prueba final. Cada partición deberá incluir español e inglés y al menos un caso con evidencia contradictoria.

Se etiquetarán aproximadamente 300 a 450 pares consulta y fragmento, seleccionados para incluir positivos claros, negativos difíciles, datos antiguos y fuentes que repiten un mismo resultado. El corpus es pequeño y sirve como piloto; los informes mostrarán denominadores e incertidumbre. No se proclamará un porcentaje general de calidad a partir de diez preguntas finales.

Las preguntas cubrirán compatibilidad técnica, rendimiento, precios con fecha, comparación de versiones y limitaciones de proyectos. Un ejemplo será investigar APNs en Go y distinguir throughput publicado, condiciones de las pruebas y recomendaciones no medidas.

Las etiquetas de referencia serán revisadas manualmente. Si se utiliza un LLM para proponer etiquetas, se conservará ese origen y se revisarán los casos utilizados como referencia.

## Dos tipos de comparación

| Comparación | Qué permanece fijo | Qué mide |
| --- | --- | --- |
| Offline sobre corpus conservado | Documentos, fragmentos y rúbricas | Efecto del selector, reranker o juez |
| Investigación en vivo | Pregunta, presupuesto y configuración registrada | Calidad y coste del sistema completo, sujetos a cambios de la web |

El baseline y las variantes usarán el mismo modelo cloud y una pauta de redacción equivalente cuando sea posible. El informe de evaluación separará el tiempo del daemon del tiempo total observado por el usuario.

## Métricas

| Métrica | Definición |
| --- | --- |
| Recall de evidencia | Fragmentos relevantes retenidos dividido entre fragmentos relevantes etiquetados en el corpus de prueba |
| Ordenamiento | nDCG con relevancia etiquetada; no comparar valores de score entre modelos sin validación |
| Cobertura | Elementos esenciales satisfechos de la rúbrica de cada pregunta |
| Soporte de afirmaciones | Afirmaciones auditadas que están sustentadas dividido entre afirmaciones auditadas presentadas como sustentadas |
| Contradicciones | Contradicciones de referencia detectadas y falsas contradicciones reportadas |
| Validez estructural de citas | Referencias que resuelven a fuentes y localizadores existentes |
| Operación | Éxito por tarea, reintentos, recuperación y cancelación |
| Coste | Consumo externo observable, tamaño de paquetes cloud y costes desconocidos declarados |
| Rendimiento | p50 y p95 de duración, recursos y velocidad; con tamaño de muestra visible |

## Objetivos iniciales de salida

- 100 % de las referencias estructurales resuelven a evidencia conservada.
- Cero citas inventadas detectadas en el conjunto de aceptación.
- Al menos 95 % de soporte correcto entre las afirmaciones auditadas presentadas como sustentadas; publicar numerador y denominador.
- Recall de evidencia de al menos 90 % en el corpus piloto; mostrar también el resultado por pregunta.
- Ninguna contradicción crítica de la rúbrica se elimina del informe sin explicación.
- Todas las rutas de fallo relevantes conservan un resultado parcial o un error recuperable.
- Cerrar el cliente no cancela el daemon ni pierde el trabajo.
- Repetir una solicitud idempotente no duplica el trabajo.
- El trabajo se detiene al alcanzar sus límites y deja el consumo registrado.
- La configuración elegida funciona sin crecimiento sostenido de memoria o swap durante una ejecución prolongada.

Los objetivos de calidad son criterios propuestos para este producto. Alcanzarlos en el piloto no garantiza su cumplimiento en nuevos temas; se revisarán con investigaciones posteriores.

Se tomarán ideas de DeepResearch Bench II para rúbricas de recuperación, análisis y presentación. No es necesario ejecutar todo el benchmark para validar el MVP.

# Experimento de Laya

Laya comenzará en modo observación: emitirá decisiones y diagnósticos, pero no eliminará evidencia. Se estudiarán preguntas pequeñas y acotadas, como la relevancia de un fragmento para una subpregunta. La resolución factual de contradicciones seguirá utilizando las fuentes y la revisión cloud.

## Implementación del experimento

1. Crear DecisionProvider para poder desactivar o sustituir Laya sin alterar la recuperación.
2. Fijar un checkpoint apropiado para idioma y tarea; conservar revisión y presupuestos de tokens.
3. Ejecutar PyTorch como referencia y evaluar laya-mlx en el equipo cuando la compatibilidad de la función requerida esté comprobada.
4. Medir orden, nombres de opciones, truncación y estabilidad en ejemplos etiquetados.
5. Usar OptionStabilityHook donde el runtime lo soporte. En caso contrario, ejecutar variantes equivalentes y registrar esa diferencia.
6. Ajustar temperaturas y umbrales en la partición de calibración; probarlos en preguntas reservadas.
7. Comparar baseline, reranker, Laya y reranker más Laya sobre el mismo corpus.
8. Incluir inferencia, batching, serialización y revisiones adicionales en el coste total.

La política de abstención pasará los casos inciertos al siguiente revisor y conservará la evidencia. La estabilidad entre permutaciones no se interpretará como corrección factual.

## Condición de adopción

Laya podrá filtrar solo si el piloto mantiene el objetivo de recall y no pierde evidencia crítica, y además reduce de forma útil el tiempo o el coste total. Se propone como mejora material una reducción de al menos 15 % en el recurso que se pretende optimizar; debe medirse después de incluir el coste de Laya y sus verificaciones.

Si no cumple esa condición, permanecerá desactivado. El MVP y la versión estable seguirán funcionando.

Los checkpoints con layout paralelo requieren pesos entrenados para ese layout. No se activará esa función cambiando solamente la configuración de pesos secuenciales. Los torneos de eliminación no se utilizarán como ranking general de todos los documentos.

# Estructura propuesta del repositorio

| Ruta | Responsabilidad |
| --- | --- |
| pyproject.toml y uv.lock | Dependencias y comandos del proyecto |
| src/researcher_pro/domain/ | Contratos, estados y reglas |
| src/researcher_pro/application/ | Casos de uso y presupuestos |
| src/researcher_pro/workflows/ | Grafo, etapas y reanudación |
| src/researcher_pro/adapters/llm/ | llama.cpp y adaptador Ollama |
| src/researcher_pro/adapters/search/ | SearXNG, GitHub, arXiv y proveedores alternativos |
| src/researcher_pro/adapters/readers/ | HTML, PDF y extracción |
| src/researcher_pro/adapters/ranking/ | Selector de referencia, reranker y Laya |
| src/researcher_pro/storage/ | Migraciones, objetos, caché y repositorios |
| src/researcher_pro/interfaces/ | MCP, HTTP y CLI |
| prompts/ | Pautas versionadas del worker y la revisión |
| configs/ | Perfiles y ejemplo sin secretos |
| tests/ | Contratos, recuperación, citas y límites |
| evals/ | Preguntas, rúbricas, corpus de prueba y ejecuciones |
| docs/ | Instalación, decisiones de arquitectura y operación |

Los pesos, los cuerpos descargados, las claves y los datos de ejecución no se guardarán en Git. Un manifiesto registrará referencias y hashes para reproducir el entorno.

# Configuración inicial propuesta

Este bloque define el contrato de configuración que se implementará; no es todavía un comando o archivo de una aplicación instalada.

```yaml
profile: balanced
data_dir: ./var/researcher-pro

service:
  bind: 127.0.0.1
  port: 8787
  scheduler_workers: 1

llm:
  provider: llama_cpp
  base_url: http://127.0.0.1:8080/v1
  model_alias: research-worker
  context_tokens: 32768
  concurrent_requests: 1
  model_revision: SET_DURING_PHASE_0
  artifact_sha256: SET_DURING_PHASE_0
  template_revision: SET_DURING_PHASE_0

search:
  provider: searxng
  base_url: http://127.0.0.1:8888
  languages: [es, en]
  max_queries: 12

reading:
  concurrency: 4
  max_documents: 30
  max_html_bytes: 5242880
  max_pdf_bytes: 20971520
  max_pdf_pages: 50
  browser_enabled: false

budget:
  max_subquestions: 4
  max_agent_steps: 60
  max_runtime_seconds: 1200
  max_gap_rounds: 2
  external_cost_limit_usd: null

ranking:
  provider: lexical
  preserve_subquestion_coverage: true
  reranker_enabled: false

decision:
  laya_enabled: false
  mode: observe

cloud:
  mode: mcp_host
  initial_evidence_token_budget: 8000
```

El límite monetario nulo significa que no se ha definido un importe; no significa presupuesto infinito autorizado. Los límites de consultas y lecturas siguen activos. Un proveedor de pago requerirá un límite monetario explícito antes de habilitarse. Los secretos se leerán desde variables de entorno o un almacén apropiado y no se exportarán con las investigaciones.

# Primer corte de implementación

El primer cambio del proyecto deberá contener RP001, los contratos mínimos de RP005 y un flujo simulado de RP006 a RP008. La prueba de demostración será:

1. Iniciar el daemon y conectar el cliente MCP.
2. Crear un trabajo con clave de idempotencia.
3. Consultar su estado.
4. Cerrar el cliente y reiniciar el daemon.
5. Reconectar y recuperar el mismo trabajo.
6. Repetir la solicitud y comprobar que devuelve el mismo identificador.
7. Cancelar un segundo trabajo sin afectar el primero.
8. Exportar el resultado simulado con sus referencias.

Este corte establece el comportamiento sobre el que se incorporarán la búsqueda y el modelo. El siguiente cambio añadirá un único proveedor de búsqueda y una única lectura real con evidencia versionada.

# Fuentes técnicas

Documentación y fuentes consultadas para las decisiones, con fecha de referencia 26 de octubre de 2026. Las cifras de esfuerzo, límites y objetivos de aceptación son estimaciones de este plan.

- [Agents-A1-4B original](https://huggingface.co/InternScience/Agents-A1-4B) y [GGUF oficial Q4_K_M](https://huggingface.co/InternScience/Agents-A1-4B-Q4_K_M-GGUF): referencia del worker y artefacto de pesos.
- [llama.cpp y function calling](https://github.com/ggml-org/llama.cpp/blob/master/docs/function-calling.md): compatibilidad de templates y llamadas.
- [Ollama y tool calling](https://docs.ollama.com/capabilities/tool-calling): bucle de solicitud, ejecución y devolución de resultados de herramientas.
- [SDK oficial MCP para Python](https://github.com/modelcontextprotocol/python-sdk) y [arquitectura MCP](https://modelcontextprotocol.io/docs/learn/architecture): contratos y transportes.
- [Persistencia de LangGraph](https://docs.langchain.com/oss/python/langgraph/persistence): checkpoints y almacenamiento entre trabajos.
- [SearXNG Search API](https://docs.searxng.org/dev/search_api.html): API y habilitación de formatos de salida.
- [Trafilatura](https://trafilatura.readthedocs.io/en/latest/): extracción de texto y metadatos.
- [Qwen3-Reranker-0.6B](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B): candidato para ordenar relevancia.
- [session-mesh-mcp](https://github.com/ShashankIIITN/session-mesh-mcp): integración opcional de orquestación.
- [ResearchHarness](https://github.com/InternScience/ResearchHarness): referencia de bucle de herramientas y trazas.
- [Laya y límites conocidos](https://nandhakishorm.github.io/laya/benchmarks/), [hooks](https://nandhakishorm.github.io/laya/hooks/api/) y [laya-mlx](https://github.com/mizorewww/laya-mlx): experimento de decisiones locales.
- [Fast Models, Slow Evidence](https://arxiv.org/abs/2610.02267): evaluación de decisiones, sesgo de orden y costes del flujo completo.
- [DeepResearch Bench II](https://github.com/imlrz/DeepResearch-Bench-II): rúbricas para recuperación, análisis y presentación.
