import logging
import os
import sys
import threading
import time
import fcntl

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from opensearchpy import OpenSearch, exceptions

from config import read_ini
from lareferenciastatsdb import (
    IdentifierPrefixNotFoundException,
    SOURCE_TYPE_NATIONAL,
    SOURCE_TYPE_REGIONAL,
    SOURCE_TYPE_REPOSITORY,
    UsageStatsDatabaseHelper,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger("usage-stats-service")

app = FastAPI(
    title="LA Referencia Usage Statistics API",
    description="API for usage statistics of the LA Referencia service",
    version="2.0.0",
    terms_of_service="",
    contact={
        "name": "Lautaro Matas",
        "url": "http://www.lareferencia.info",
        "email": "lautaro.matas@lareferencia.redclara.net",
    },
    license_info={
        "name": "AGPL-3.0",
        "url": "https://www.gnu.org/licenses/agpl-3.0.html",
    },
    root_path=os.getenv("API_ROOT_PATH", ""),
)

origins = ["*"]
cors_enabled = False
track_origins_enabled = False
track_origins_file = "origins.txt"
tracked_origins = set()
dbhelper_refresh_seconds = 300
_dbhelper_last_refresh = time.monotonic()
_dbhelper_refresh_lock = threading.Lock()
_track_origins_lock = threading.Lock()


def _parse_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _load_cors_config(config):
    if not config.has_section("CORS"):
        return False, []

    if not config.has_option("CORS", "ENABLED"):
        return False, []

    enabled = _parse_bool(config.get("CORS", "ENABLED"), default=False)
    if not enabled:
        return False, []

    if config.has_option("CORS", "ALLOWED_ORIGINS"):
        allowed_origins = [item.strip() for item in config.get("CORS", "ALLOWED_ORIGINS").split(",") if item.strip()]
        if allowed_origins:
            return True, allowed_origins

    if config.has_option("CORS", "FILENAME"):
        cors_filename = config.get("CORS", "FILENAME")
        with open(cors_filename, "r") as cors_file:
            file_origins = [line.strip() for line in cors_file.readlines() if line.strip()]
        if file_origins:
            return True, file_origins

    raise ValueError("CORS.ENABLED=true requires CORS.ALLOWED_ORIGINS or CORS.FILENAME with values")


def _resolve_origins_file_path(config_file_path, config):
    filename = "origins.txt"
    if config.has_section("CORS") and config.has_option("CORS", "TRACK_ORIGINS_FILE"):
        filename = config.get("CORS", "TRACK_ORIGINS_FILE").strip() or "origins.txt"

    if os.path.isabs(filename):
        return filename

    config_dir = os.path.dirname(os.path.abspath(config_file_path))
    return os.path.join(config_dir, filename)


def _load_existing_tracked_origins(filename):
    if not os.path.isfile(filename):
        return set()
    with open(filename, "r", encoding="utf-8") as infile:
        return {line.strip() for line in infile.readlines() if line.strip()}


def _append_origin_if_new(origin):
    origin = origin.strip()
    if not origin:
        return

    with _track_origins_lock:
        if origin in tracked_origins:
            return

        os.makedirs(os.path.dirname(track_origins_file), exist_ok=True)
        with open(track_origins_file, "a+", encoding="utf-8") as outfile:
            fcntl.flock(outfile.fileno(), fcntl.LOCK_EX)
            outfile.seek(0)
            file_origins = {line.strip() for line in outfile.readlines() if line.strip()}

            if origin not in file_origins:
                outfile.seek(0, os.SEEK_END)
                outfile.write(origin + "\n")
                outfile.flush()
                file_origins.add(origin)

            fcntl.flock(outfile.fileno(), fcntl.LOCK_UN)

        tracked_origins.clear()
        tracked_origins.update(file_origins)

try:
    config_file_path = os.getenv("CONFIG_FILE_PATH", "config.ini")

    config = read_ini(config_file_path)
    cors_enabled, origins = _load_cors_config(config)
    track_origins_enabled = _parse_bool(
        config.get("CORS", "TRACK_ORIGINS", fallback="false") if config.has_section("CORS") else "false"
    )
    track_origins_file = _resolve_origins_file_path(config_file_path, config)
    index_prefix = config["USAGE_STATS_INDEX"]["INDEX_PREFIX"]

    dbhelper = UsageStatsDatabaseHelper(config)
    dbhelper_refresh_seconds = int(
        os.getenv(
            "DBHELPER_REFRESH_SECONDS",
            config.get("USAGE_STATS_DB", "HELPER_REFRESH_SECONDS", fallback="300"),
        )
    )

    host = config["OPENSEARCH"]["HOST"]
    port = int(config["OPENSEARCH"]["PORT"])
    is_ssl = str(config["OPENSEARCH"]["SSL"]).lower() == "true"
    auth = (config["OPENSEARCH"]["USER"], config["OPENSEARCH"]["PASSWORD"])

    client = OpenSearch(
        hosts=[{"host": host, "port": port}],
        http_compress=True,
        use_ssl=is_ssl,
        http_auth=auth,
    )

    logger.info("Connected to OpenSearch")
    logger.info(client.info())
    if track_origins_enabled:
        tracked_origins = _load_existing_tracked_origins(track_origins_file)
        logger.info("Origin tracking enabled -> %s", track_origins_file)

except Exception:
    logger.exception("Failed to initialize service")
    sys.exit(1)


if cors_enabled:
    logger.info("CORS enabled with %s origins", len(origins))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    logger.info("CORS disabled (CORS.ENABLED not configured or false)")


@app.middleware("http")
async def track_origins_middleware(request, call_next):
    if track_origins_enabled:
        request_origin = request.headers.get("origin")
        # Keep exact origin value as used by browsers for CORS checks.
        if request_origin and request_origin.lower() != "null":
            _append_origin_if_new(request_origin)
    return await call_next(request)


def _has_value(value):
    return value is not None and value != "" and value != "*"


def _refresh_dbhelper_if_needed():
    global _dbhelper_last_refresh

    if dbhelper_refresh_seconds <= 0:
        return

    now = time.monotonic()
    if now - _dbhelper_last_refresh < dbhelper_refresh_seconds:
        return

    with _dbhelper_refresh_lock:
        now = time.monotonic()
        if now - _dbhelper_last_refresh < dbhelper_refresh_seconds:
            return
        dbhelper.update_data_from_db()
        _dbhelper_last_refresh = now


def _query_aggregations(query, indices):
    if indices is None or len(indices) == 0:
        raise HTTPException(status_code=404, detail="No matching indices found")

    try:
        response = client.search(
            body=query,
            index=",".join(indices),
            allow_no_indices=True,
            ignore_unavailable=True,
        )
    except exceptions.ConnectionError:
        logger.exception("OpenSearch connection error")
        raise HTTPException(status_code=502, detail="OpenSearch connection error")
    except exceptions.TransportError:
        logger.exception("OpenSearch transport error")
        raise HTTPException(status_code=502, detail="OpenSearch transport error")
    except Exception:
        logger.exception("Unexpected OpenSearch error")
        raise HTTPException(status_code=500, detail="Unexpected error while querying OpenSearch")

    if response is None or response.get("aggregations") is None:
        raise HTTPException(status_code=404, detail="Not found")

    return response.get("aggregations", {})


def _resolve_source_or_404(source_id):
    _refresh_dbhelper_if_needed()

    if not _has_value(source_id):
        raise HTTPException(status_code=400, detail="source_id parameter is required")

    source = dbhelper.get_source_by_id(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="The source %s is not present in the database" % source_id)

    return source


def _resolve_indices_from_identifier_or_source(identifier, source_id):
    _refresh_dbhelper_if_needed()

    if _has_value(identifier):
        try:
            indices = dbhelper.get_indices_from_identifier(index_prefix, identifier)
            logger.info("indices from identifier: %s", indices)
            return indices
        except IdentifierPrefixNotFoundException:
            logger.info("identifier not found in source metadata: %s", identifier)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    if _has_value(source_id):
        source_obj = dbhelper.get_source_by_id(source_id)
        if source_obj is None:
            raise HTTPException(status_code=404, detail="The source %s is not present in the database" % source_id)
        return dbhelper.get_indices_from_source(index_prefix, source_obj)

    if _has_value(identifier):
        raise HTTPException(status_code=404, detail="The identifier %s is not present in the database" % identifier)

    raise HTTPException(status_code=400, detail="At least one of 'identifier' or 'source' is required")


def _resolve_identifier_prefix_or_404(source):
    identifier_prefix = dbhelper.get_identifier_prefix_from_source(source)
    if not identifier_prefix:
        raise HTTPException(
            status_code=404,
            detail="The source %s has no valid identifier_prefix configured" % source.source_id,
        )
    return identifier_prefix


def parametrize_query(identifier, start_date, end_date, time_unit, country=None):
    query = {
        "aggs": {
            "views": {"sum": {"field": "views"}},
            "downloads": {"sum": {"field": "downloads"}},
            "conversions": {"sum": {"field": "conversions"}},
            "outlinks": {"sum": {"field": "outlinks"}},
            "level": {
                "terms": {"field": "level", "order": {"_key": "desc"}, "size": 5},
                "aggs": {
                    "views": {"sum": {"field": "views"}},
                    "downloads": {"sum": {"field": "downloads"}},
                    "conversions": {"sum": {"field": "conversions"}},
                    "outlinks": {"sum": {"field": "outlinks"}},
                },
            },
            "time": {
                "date_histogram": {"field": "date", "calendar_interval": "1m", "min_doc_count": 1},
                "aggs": {
                    "level": {
                        "terms": {"field": "level", "order": {"_key": "desc"}, "size": 5},
                        "aggs": {
                            "views": {"sum": {"field": "views"}},
                            "downloads": {"sum": {"field": "downloads"}},
                            "conversions": {"sum": {"field": "conversions"}},
                            "outlinks": {"sum": {"field": "outlinks"}},
                        },
                    }
                },
            },
        },
        "size": 0,
        "query": {
            "bool": {
                "must": [],
                "filter": [
                    {
                        "range": {
                            "date": {
                                "gte": start_date,
                                "lte": end_date,
                                "format": "strict_date_optional_time",
                            }
                        }
                    }
                ],
            }
        },
        "track_total_hits": "false",
    }

    if identifier is not None:
        query["query"]["bool"]["must"].append({"match_phrase": {"identifier": identifier}})

    if country is not None:
        query["query"]["bool"]["must"].append({"match": {"country": country}})

    return query


def parametrize_bycountry_query(identifier, start_date, end_date, limit=10, country=None):
    query = {
        "aggs": {
            "views": {"sum": {"field": "views"}},
            "downloads": {"sum": {"field": "downloads"}},
            "conversions": {"sum": {"field": "conversions"}},
            "outlinks": {"sum": {"field": "outlinks"}},
            "country": {
                "nested": {"path": "stats_by_country"},
                "aggs": {
                    "views": {
                        "terms": {"field": "stats_by_country.country", "size": limit},
                        "aggs": {"count": {"sum": {"field": "stats_by_country.views"}}},
                    },
                    "downloads": {
                        "terms": {"field": "stats_by_country.country", "size": limit},
                        "aggs": {"count": {"sum": {"field": "stats_by_country.downloads"}}},
                    },
                    "outlinks": {
                        "terms": {"field": "stats_by_country.country", "size": limit},
                        "aggs": {"count": {"sum": {"field": "stats_by_country.outlinks"}}},
                    },
                    "conversions": {
                        "terms": {"field": "stats_by_country.country", "size": limit},
                        "aggs": {"count": {"sum": {"field": "stats_by_country.conversions"}}},
                    },
                },
            },
        },
        "size": 0,
        "query": {
            "bool": {
                "must": [],
                "filter": [
                    {
                        "range": {
                            "date": {
                                "gte": start_date,
                                "lte": end_date,
                                "format": "strict_date_optional_time",
                            }
                        }
                    }
                ],
            }
        },
        "track_total_hits": "false",
    }

    if identifier is not None:
        query["query"]["bool"]["must"].append({"match_phrase": {"identifier": identifier}})

    if country is not None:
        query["query"]["bool"]["must"].append({"match": {"country": country}})

    return query


@app.get("/report/itemWidget")
def itemWidget(identifier: str = None, source: str = "*", start_date: str = "now-1y", end_date: str = "now", time_unit: str = "year"):
    query = parametrize_query(identifier, start_date, end_date, time_unit)
    try:
        indices = _resolve_indices_from_identifier_or_source(identifier, source)
        logger.info("indices: %s", indices)
        return _query_aggregations(query, indices)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in itemWidget")
        raise HTTPException(status_code=500, detail="Unexpected server error")


@app.get("/report/itemWidgetByCountry")
def itemWidgetByCountry(
    identifier: str = None,
    source: str = "*",
    start_date: str = "now-1y",
    end_date: str = "now",
    limit: int = 10,
):
    query = parametrize_bycountry_query(identifier, start_date, end_date, limit)
    try:
        indices = _resolve_indices_from_identifier_or_source(identifier, source)
        logger.info("indices: %s", indices)
        return _query_aggregations(query, indices)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in itemWidgetByCountry")
        raise HTTPException(status_code=500, detail="Unexpected server error")


@app.get("/report/repositoryWidget")
def repositoryWidget(source_id: str = "*", start_date: str = "now-1y", end_date: str = "now", time_unit: str = "year"):
    try:
        source = _resolve_source_or_404(source_id)
        country = source.country_iso

        if source.type == SOURCE_TYPE_REPOSITORY:
            identifier_prefix = _resolve_identifier_prefix_or_404(source)
            logger.info("identifier_prefix: %s", identifier_prefix)
            identifier_pattern = identifier_prefix + "*"
            query = parametrize_query(identifier_pattern, start_date, end_date, time_unit)
            indices = dbhelper.get_indices_from_identifier(index_prefix, identifier_prefix)
        elif source.type == SOURCE_TYPE_NATIONAL:
            indices = dbhelper.get_indices_from_national_source(index_prefix, source)
            query = parametrize_query(None, start_date, end_date, time_unit, country)
        elif source.type == SOURCE_TYPE_REGIONAL:
            indices = dbhelper.get_indices_from_regional_source(index_prefix, source)
            query = parametrize_query(None, start_date, end_date, time_unit)
        else:
            raise HTTPException(
                status_code=404,
                detail="The source %s is not a repository or national source" % source_id,
            )

        logger.info("indices: %s", indices)
        return _query_aggregations(query, indices)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in repositoryWidget")
        raise HTTPException(status_code=500, detail="Unexpected server error")


@app.get("/report/repositoryWidgetByCountry")
def repositoryWidgetByCountry(source_id: str = "*", start_date: str = "now-1y", end_date: str = "now", limit: int = 10):
    try:
        source = _resolve_source_or_404(source_id)

        if source.type == SOURCE_TYPE_REPOSITORY:
            identifier_prefix = _resolve_identifier_prefix_or_404(source)
            logger.info("identifier_prefix: %s", identifier_prefix)
            identifier_pattern = identifier_prefix + "*"
            query = parametrize_bycountry_query(identifier_pattern, start_date, end_date, limit)
            indices = dbhelper.get_indices_from_identifier(index_prefix, identifier_prefix)
        elif source.type == SOURCE_TYPE_NATIONAL:
            indices = dbhelper.get_indices_from_national_source(index_prefix, source)
            query = parametrize_bycountry_query(None, start_date, end_date, limit)
        elif source.type == SOURCE_TYPE_REGIONAL:
            indices = dbhelper.get_indices_from_regional_source(index_prefix, source)
            query = parametrize_bycountry_query(None, start_date, end_date, limit)
        else:
            raise HTTPException(
                status_code=404,
                detail="The source %s is not a repository or national source" % source_id,
            )

        logger.info("indices: %s", indices)
        return _query_aggregations(query, indices)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error in repositoryWidgetByCountry")
        raise HTTPException(status_code=500, detail="Unexpected server error")
