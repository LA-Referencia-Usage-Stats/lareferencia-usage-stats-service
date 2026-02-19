# lareferencia-usage-stats-service

API FastAPI para consultar estadísticas agregadas de uso desde OpenSearch con resolución de alcance por metadata de fuentes.

## Rol en la arquitectura

- Resuelve qué índices consultar según:
  - `identifier` OAI
  - `source_id`
  - tipo de fuente (`R/N/L`)
- Ejecuta agregaciones OpenSearch y entrega JSON para widgets/reportes.

## Endpoints

- `GET /report/itemWidget`
- `GET /report/itemWidgetByCountry`
- `GET /report/repositoryWidget`
- `GET /report/repositoryWidgetByCountry`

Parámetros comunes:

- `identifier`
- `source` o `source_id`
- `start_date`, `end_date`
- `limit` (en variantes por país)

## Flujo de resolución

```mermaid
flowchart LR
    Q[Request] --> H[UsageStatsDatabaseHelper]
    H --> R[Lista de índices]
    R --> OS[OpenSearch search]
    OS --> A[aggregations JSON]
```

1. Carga config (`config.ini`) al iniciar.
2. Inicializa helper DB (`UsageStatsDatabaseHelper`).
3. Inicializa cliente OpenSearch.
4. En cada request:
   - arma query agregada;
   - determina índices aplicables;
   - ejecuta `client.search(...)`;
   - devuelve `aggregations`.

## Configuración

`config.ini.model` define:

- `OPENSEARCH`: host, puerto, SSL, usuario/clave.
- `USAGE_STATS_DB`: URI SQLAlchemy para metadata de fuentes.
- `USAGE_STATS_INDEX`: `INDEX_PREFIX`.
- `CORS`:
  - `ENABLED=true` habilita CORS.
  - si `ENABLED` no existe o es `false`, la API arranca sin middleware CORS.
  - con CORS habilitado, usa `ALLOWED_ORIGINS` (csv) o `FILENAME`.
  - `TRACK_ORIGINS=true` guarda los `Origin` recibidos (deduplicados) en `origins.txt`.
  - `TRACK_ORIGINS_FILE` permite cambiar el archivo (default: `origins.txt`).

## Ejecución

Desarrollo:

```bash
uvicorn main:app
```

Producción (según unit file incluida):

- Hypercorn bind local `127.0.0.1:8099`
- `--root-path /api/usage_stats/v2`
- Nginx reverse proxy hacia ese root path

## Integración con otros módulos

- Consume índices producidos por `lareferencia-usage-stats-processor`.
- Depende de metadata (`Source/Country`) mantenida por `admin`/`db`.

## Notas técnicas

- Si `INDEX_PREFIX` de este servicio no coincide con el usado por processor al indexar, la API no encontrará documentos.
- El endpoint se apoya fuertemente en los mappings generados por `ElasticOutputStage` (incluye nested `stats_by_country`).
