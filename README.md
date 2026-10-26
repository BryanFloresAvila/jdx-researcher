# jdx-researcher

Investigación local con trabajos persistentes, evidencia versionada y revisión desde un cliente MCP.

El daemon busca y conserva fuentes, propone hallazgos mediante un modelo local y entrega un paquete de evidencia al cliente cloud. El cliente revisa las afirmaciones y guarda el informe. Las citas apuntan a fragmentos conservados de una versión concreta del documento.

## Inicio rápido sin descargar modelos

Requisitos: macOS o Linux, Python 3.12+ y [uv](https://docs.astral.sh/uv/).

~~~bash
git clone https://github.com/BryanFloresAvila/jdx-researcher.git
cd jdx-researcher
uv sync --extra dev
uv run jdx-researcher --config configs/local.yaml serve --mode demo
~~~

En otra terminal:

~~~bash
uv run jdx-researcher --config configs/local.yaml start examples/demo-request.json
uv run jdx-researcher --config configs/local.yaml status JOB_ID
uv run jdx-researcher --config configs/local.yaml result JOB_ID
uv run jdx-researcher --config configs/local.yaml export JOB_ID --output var/export-demo
~~~

El modo demo usa tres fuentes **sintéticas**, con cifras inventadas y una discrepancia explícita. Comprueba instalación, persistencia, referencias y transportes; sus resultados no son investigación real. Repetir la solicitud devuelve el mismo JOB_ID. Cambia la clave de idempotencia para crear otro trabajo.

## Investigación con el modelo local

1. Sirve Agents-A1-4B original con un runtime compatible con tool calls estructuradas. El alias configurado es research-worker, en http://127.0.0.1:8080/v1.
2. Configura una instancia privada de SearXNG en http://127.0.0.1:8888, con JSON habilitado en search.formats. GitHub y arXiv tienen adaptadores adicionales.
3. Registra revisión, SHA-256 del GGUF y revisión de template en configs/local.yaml.
4. Ejecuta doctor y probe-model antes de servir en modo local.

~~~bash
uv run jdx-researcher --config configs/local.yaml doctor
uv run jdx-researcher --config configs/local.yaml probe-model
uv run jdx-researcher --config configs/local.yaml serve
~~~

Una invocación inicial de llama-server, si el GGUF y tu build incorporan el template correcto:

~~~bash
llama-server -m "$JDX_MODEL_PATH" --alias research-worker \
  --host 127.0.0.1 --port 8080 --ctx-size 16384 --n-gpu-layers 99 --jinja
~~~

Comprueba las opciones de tu build con llama-server --help. El probe realiza 20 casos de esquema; exige 19 válidos para el control inicial de integración. No demuestra calidad de investigación ni sustituye las pruebas con herramientas en varios turnos. El parser qwen3_coder mencionado por la ficha del modelo corresponde a configuraciones de serving que lo ofrecen; no se pasa como un flag genérico a llama.cpp.

El modo retrieval permite buscar y conservar evidencia sin llamar al modelo. No propone relaciones semánticas automáticamente.

## Conectar MCP

Mantén el daemon en ejecución y adapta [examples/mcp-client.json](examples/mcp-client.json) con la ruta absoluta del repositorio.

- stdio: el comando mcp-stdio es un proxy ligero al daemon.
- Streamable HTTP: http://127.0.0.1:8787/mcp, con Authorization: Bearer TOKEN.
- Obtén el token mediante el comando token solo cuando necesites configurarlo explícitamente. El proxy lo lee automáticamente.

Herramientas: research_start, research_status, research_result, research_source, research_finalize y research_cancel.

Cerrar el cliente MCP no detiene el daemon. Un servicio remoto necesita un transporte y acceso a la máquina configurados explícitamente; localhost del Mac no es accesible automáticamente desde un cliente cloud.

## Informes y referencias

El cliente finaliza con título, Markdown y afirmaciones estructuradas. Cada referencia usa el formato [[p_IDENTIFICADOR]] y debe existir en el trabajo. Las afirmaciones de tipo sourced requieren citas. Las inferencias e incertidumbres se etiquetan explícitamente. Para marcar una afirmación verified se exige un revisor y referencias.

La validación comprueba pertenencia al trabajo, integridad del objeto y offsets del fragmento. **No certifica que la evidencia sostenga semánticamente la afirmación.** Las relaciones del worker son propuestas; los vacíos y las contradicciones siguen visibles al finalizar.

Los resultados incluyen fecha de captura, fecha de publicación cuando se conoce, hash de bytes y texto, extractor, páginas y familia de copias exactas. La fecha de captura nunca sustituye una fecha de publicación desconocida.

## Límites y recuperación

Por defecto: 4 subpreguntas, 12 consultas, 30 documentos, 60 pasos, 20 minutos y 4 descargas simultáneas. Solo hay un scheduler y una inferencia generativa activa por directorio de datos.

SQLite WAL conserva cola, eventos, presupuestos y evidencia. LangGraph guarda sus checkpoints en una segunda base. Las acciones terminadas se reutilizan después de reiniciar. Una petición externa en vuelo puede repetirse y vuelve a consumir presupuesto. Las escrituras tardías tras cancelar se rechazan.

El lector admite HTML, texto y PDF con texto seleccionable. Rechaza URLs privadas, credenciales embebidas y puertos distintos de 80/443. Conecta a direcciones DNS públicas verificadas y valida cada redirección. Los servicios locales configurados usan clientes separados.

## Ranking y evaluación

El selector inicial usa FTS5 y ranking léxico con cobertura por subpregunta y diversidad. El adaptador Qwen3-Reranker-0.6B es opcional:

~~~bash
uv sync --extra dev --extra ranking
~~~

Después configura ranking: qwen3 y fija reranker_revision. La primera llamada puede descargar pesos; se carga un único modelo de ranking y procesa un documento a la vez. Evalúa su memoria junto al modelo generativo antes de activarlo habitualmente.

~~~bash
uv run jdx-researcher evaluate evals/fixture-pairs.jsonl --top-k 2
~~~

El corpus de ejemplo es sintético y comprueba métricas. [evals/questions.json](evals/questions.json) contiene 30 preguntas propuestas, separadas por pregunta en train/calibration/test. Requieren recuperar y etiquetar 300–450 pares reales antes de medir calidad. No se publican porcentajes de precisión a partir de datos inventados.

Laya, heretic y session-mesh quedan como experimentos separados del baseline. No se instalan ni descartan evidencia por defecto. Consulta el [estado del plan](docs/implementation-status.md).

## Pruebas y operación

~~~bash
uv run ruff check .
uv run pytest -q
uv build
~~~

CI prueba Linux y macOS, incluyendo HTTP MCP y stdio con el SDK real, sin pesos de modelos ni búsqueda en vivo. Los modelos y proveedores se simulan en las pruebas de investigación. Una vez validada la primera entrega, CI conserva el lockfile resuelto; las ejecuciones posteriores usan ese archivo.

Para una copia íntegra, detén el daemon con Ctrl-C:

~~~bash
uv run jdx-researcher --config configs/local.yaml backup --output var/backup.tar.gz
uv run jdx-researcher restore var/backup.tar.gz --destination var/restored
~~~

Se verifican hashes y bases al restaurar. El token local no se incluye. Datos y pesos quedan fuera de Git.

Más detalles: [arquitectura](docs/architecture.md), [operación en Mac](docs/macos.md), [plan original](docs/plan-implementacion.md).
