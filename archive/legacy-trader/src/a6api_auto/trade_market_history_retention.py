from __future__ import annotations

import gzip
import hashlib
import json
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

_DEFAULT_RETENTION_HOURS = 72
_DEFAULT_BATCH_SIZE = 500
_MAX_RETENTION_HOURS = 24 * 366
_MAX_BATCH_SIZE = 10_000
_ADVISORY_LOCK_NAMESPACE = 1_096_778_658
_ARCHIVE_FORMAT_VERSION = "a6-market-history-csv-gzip-v1"


class MarketHistoryRetentionError(RuntimeError):
    pass


@dataclass(frozen=True)
class MarketHistoryRetentionConfig:
    retention_hours: int = _DEFAULT_RETENTION_HOURS
    batch_size: int = _DEFAULT_BATCH_SIZE

    def __post_init__(self) -> None:
        if (
            isinstance(self.retention_hours, bool)
            or not isinstance(self.retention_hours, int)
            or not 1 <= self.retention_hours <= _MAX_RETENTION_HOURS
        ):
            raise MarketHistoryRetentionError("market_history_retention_hours_invalid")
        if (
            isinstance(self.batch_size, bool)
            or not isinstance(self.batch_size, int)
            or not 1 <= self.batch_size <= _MAX_BATCH_SIZE
        ):
            raise MarketHistoryRetentionError("market_history_retention_batch_size_invalid")


@dataclass(frozen=True)
class MarketHistoryRetentionCandidate:
    scan_run_id: int
    started_at: datetime
    candidate_count: int
    payload_bytes: int


@dataclass(frozen=True)
class MarketHistoryArchiveFile:
    path: Path
    row_count: int
    sha256: str
    size_bytes: int
    content_type: str = "text/csv"
    content_encoding: str = "gzip"


@dataclass(frozen=True)
class MarketHistoryExport:
    scan_runs: MarketHistoryArchiveFile
    quotes: MarketHistoryArchiveFile


@dataclass(frozen=True)
class MarketHistoryObject:
    key: str
    size_bytes: int
    sha256: str


class MarketHistoryObjectStore(Protocol):
    @property
    def identity(self) -> Mapping[str, str]: ...

    def put_file(
        self,
        key: str,
        path: Path,
        *,
        sha256: str,
        content_type: str,
        content_encoding: str | None = None,
    ) -> MarketHistoryObject: ...

    def head(self, key: str) -> MarketHistoryObject: ...


class MarketHistoryExporter(Protocol):
    def export(
        self,
        connection: Any,
        scan_run_ids: Sequence[int],
        *,
        quote_count: int,
        directory: Path,
    ) -> MarketHistoryExport: ...


@dataclass(frozen=True)
class MarketHistoryRetentionResult:
    config: MarketHistoryRetentionConfig
    cutoff: datetime
    applied: bool
    candidates: tuple[MarketHistoryRetentionCandidate, ...]
    eligible_quote_count: int
    archive_prefix: str | None = None
    scan_archive_sha256: str | None = None
    quote_archive_sha256: str | None = None
    manifest_sha256: str | None = None
    archived_bytes: int = 0
    deleted_scan_runs: int = 0
    deleted_quotes: int = 0
    deleted_payload_bytes: int = 0

    @property
    def has_more(self) -> bool:
        return len(self.candidates) == self.config.batch_size

    def summary(self) -> dict[str, Any]:
        oldest = self.candidates[0] if self.candidates else None
        newest = self.candidates[-1] if self.candidates else None
        return {
            "success": True,
            "mode": "archive-delete" if self.applied else "dry-run",
            "retention_hours": self.config.retention_hours,
            "batch_size": self.config.batch_size,
            "cutoff": self.cutoff.isoformat(),
            "eligible_scan_runs": len(self.candidates),
            "eligible_quotes": self.eligible_quote_count,
            "eligible_payload_bytes": sum(item.payload_bytes for item in self.candidates),
            "oldest_scan_run_id": oldest.scan_run_id if oldest else None,
            "oldest_started_at": oldest.started_at.isoformat() if oldest else None,
            "newest_scan_run_id": newest.scan_run_id if newest else None,
            "newest_started_at": newest.started_at.isoformat() if newest else None,
            "archive_prefix": self.archive_prefix,
            "scan_archive_sha256": self.scan_archive_sha256,
            "quote_archive_sha256": self.quote_archive_sha256,
            "manifest_sha256": self.manifest_sha256,
            "archived_bytes": self.archived_bytes,
            "deleted_scan_runs": self.deleted_scan_runs,
            "deleted_quotes": self.deleted_quotes,
            "deleted_payload_bytes": self.deleted_payload_bytes,
            "has_more": self.has_more,
            "space_reclaim": "postgres_reusable; filesystem reclaim requires online repack",
        }


_CANDIDATE_QUERY = """
    SELECT scan.id AS scan_run_id,
           scan.started_at,
           scan.candidate_count,
           coalesce(pg_column_size(scan.result_payload), 0)::bigint AS payload_bytes
    FROM a6_trade.a6_market_scan_runs scan
    WHERE scan.started_at < %s
      AND scan.status <> 'running'
      AND NOT EXISTS (
          SELECT 1
          FROM a6_trade.a6_market_channel_signals_current signal
          WHERE signal.scan_run_id = scan.id
      )
      AND NOT EXISTS (
          SELECT 1
          FROM a6_trade.a6_channel_observer_health_current health
          WHERE health.scan_run_id = scan.id
      )
    ORDER BY scan.started_at, scan.id
    LIMIT %s
"""

_SCAN_EXPORT_QUERY = """
    COPY (
        SELECT id, observer_merchant_id, proxy_lease_id, model, started_at,
               completed_at, page_count, candidate_count, http_status, elapsed_ms,
               result_hash, status, error_class, raw_artifact_ref, created_at,
               result_payload
        FROM a6_trade.a6_market_scan_runs
        WHERE id = ANY(%s)
        ORDER BY id
    ) TO STDOUT WITH (FORMAT CSV, HEADER TRUE)
"""

_QUOTE_EXPORT_QUERY = """
    COPY (
        SELECT id, scan_run_id, supplier_id, channel_id, listing_id, model,
               input_price, output_price, cache_read_price, cache_write_price,
               final_multiplier, route_status, last_success_at, circuit_state,
               market_rank, is_owned, observed_at, source_hash, created_at,
               realtime_ratio_usd, realtime_ratio_cny, exchange_rate
        FROM a6_trade.a6_market_quotes
        WHERE scan_run_id = ANY(%s)
        ORDER BY id
    ) TO STDOUT WITH (FORMAT CSV, HEADER TRUE)
"""


class PostgresCsvGzipMarketHistoryExporter:
    def export(
        self,
        connection: Any,
        scan_run_ids: Sequence[int],
        *,
        quote_count: int,
        directory: Path,
    ) -> MarketHistoryExport:
        ids = [int(item) for item in scan_run_ids]
        if not ids:
            raise MarketHistoryRetentionError("market_history_archive_empty")
        directory.mkdir(parents=True, exist_ok=True)
        scans = _copy_gzip(
            connection,
            _SCAN_EXPORT_QUERY,
            (ids,),
            directory / "a6_market_scan_runs.csv.gz",
            row_count=len(ids),
        )
        quotes = _copy_gzip(
            connection,
            _QUOTE_EXPORT_QUERY,
            (ids,),
            directory / "a6_market_quotes.csv.gz",
            row_count=quote_count,
        )
        return MarketHistoryExport(scan_runs=scans, quotes=quotes)


class S3MarketHistoryObjectStore:
    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        region: str = "auto",
    ) -> None:
        endpoint = str(endpoint_url or "").strip().rstrip("/")
        bucket_name = str(bucket or "").strip()
        access_key = str(access_key_id or "").strip()
        secret_key = str(secret_access_key or "").strip()
        region_name = str(region or "").strip()
        if not all((endpoint, bucket_name, access_key, secret_key, region_name)):
            raise MarketHistoryRetentionError("market_history_archive_s3_config_invalid")
        if not endpoint.startswith("https://"):
            raise MarketHistoryRetentionError("market_history_archive_s3_https_required")
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise MarketHistoryRetentionError("market_history_archive_boto3_missing") from exc
        self.endpoint_url = endpoint
        self.bucket = bucket_name
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region_name,
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 5, "mode": "standard"},
                s3={"addressing_style": "path"},
            ),
        )

    @property
    def identity(self) -> Mapping[str, str]:
        return {
            "backend": "s3",
            "bucket": self.bucket,
            "endpoint_sha256": hashlib.sha256(self.endpoint_url.encode()).hexdigest(),
        }

    def put_file(
        self,
        key: str,
        path: Path,
        *,
        sha256: str,
        content_type: str,
        content_encoding: str | None = None,
    ) -> MarketHistoryObject:
        extra: dict[str, Any] = {
            "ContentType": content_type,
            "Metadata": {"sha256": sha256, "archive-format": _ARCHIVE_FORMAT_VERSION},
        }
        if content_encoding:
            extra["ContentEncoding"] = content_encoding
        self._client.upload_file(str(path), self.bucket, key, ExtraArgs=extra)
        return self.head(key)

    def head(self, key: str) -> MarketHistoryObject:
        response = self._client.head_object(Bucket=self.bucket, Key=key)
        metadata = response.get("Metadata") or {}
        return MarketHistoryObject(
            key=key,
            size_bytes=int(response["ContentLength"]),
            sha256=str(metadata.get("sha256") or ""),
        )


class PostgresMarketHistoryRetention:
    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        archive_store: MarketHistoryObjectStore | None = None,
        exporter: MarketHistoryExporter | None = None,
        staging_directory: Path | str | None = None,
        object_prefix: str = "a6-market-history",
    ) -> None:
        if not callable(connection_factory):
            raise MarketHistoryRetentionError("market_history_retention_connection_factory_invalid")
        prefix = str(object_prefix or "").strip().strip("/")
        if not prefix or ".." in prefix.split("/"):
            raise MarketHistoryRetentionError("market_history_archive_prefix_invalid")
        self.connection_factory = connection_factory
        self.archive_store = archive_store
        self.exporter = exporter or PostgresCsvGzipMarketHistoryExporter()
        self.staging_directory = Path(staging_directory or tempfile.gettempdir())
        self.object_prefix = prefix

    def cleanup(
        self,
        config: MarketHistoryRetentionConfig,
        *,
        apply: bool = False,
        now: datetime | None = None,
    ) -> MarketHistoryRetentionResult:
        if not isinstance(config, MarketHistoryRetentionConfig):
            raise MarketHistoryRetentionError("market_history_retention_config_invalid")
        if not isinstance(apply, bool):
            raise MarketHistoryRetentionError("market_history_retention_apply_invalid")
        if apply and self.archive_store is None:
            raise MarketHistoryRetentionError("market_history_archive_store_required")
        observed_now = _aware_utc(now or datetime.now(timezone.utc))
        cutoff = observed_now - timedelta(hours=config.retention_hours)
        connection = self.connection_factory()
        try:
            with connection.cursor() as cursor:
                if apply:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(%s, %s)",
                        (_ADVISORY_LOCK_NAMESPACE, 0),
                    )
                    cursor.execute("SET LOCAL lock_timeout = '5s'")
                    cursor.execute("SET LOCAL statement_timeout = '5min'")
                    cursor.execute("SET LOCAL idle_in_transaction_session_timeout = '15min'")
                else:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                lock_clause = " FOR UPDATE OF scan SKIP LOCKED" if apply else ""
                cursor.execute(_CANDIDATE_QUERY + lock_clause, (cutoff, config.batch_size))
                candidates = tuple(_candidate(row) for row in _rows(cursor))
                candidate_ids = [item.scan_run_id for item in candidates]
                eligible_quotes = _quote_count(cursor, candidate_ids)
                if not apply or not candidates:
                    connection.commit()
                    return MarketHistoryRetentionResult(
                        config=config,
                        cutoff=cutoff,
                        applied=apply,
                        candidates=candidates,
                        eligible_quote_count=eligible_quotes,
                    )

                result = self._archive_and_delete(
                    connection,
                    cursor,
                    config=config,
                    cutoff=cutoff,
                    candidates=candidates,
                    eligible_quotes=eligible_quotes,
                    archived_at=observed_now,
                )
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            _close(connection)

    def _archive_and_delete(
        self,
        connection: Any,
        cursor: Any,
        *,
        config: MarketHistoryRetentionConfig,
        cutoff: datetime,
        candidates: tuple[MarketHistoryRetentionCandidate, ...],
        eligible_quotes: int,
        archived_at: datetime,
    ) -> MarketHistoryRetentionResult:
        store = self.archive_store
        if store is None:
            raise MarketHistoryRetentionError("market_history_archive_store_required")
        candidate_ids = [item.scan_run_id for item in candidates]
        archive_prefix = _archive_prefix(self.object_prefix, candidates)
        with tempfile.TemporaryDirectory(
            prefix="a6-market-history-",
            dir=self.staging_directory,
        ) as temporary_directory:
            directory = Path(temporary_directory)
            exported = self.exporter.export(
                connection,
                candidate_ids,
                quote_count=eligible_quotes,
                directory=directory,
            )
            if exported.scan_runs.row_count != len(candidates):
                raise MarketHistoryRetentionError("market_history_archive_scan_export_mismatch")
            if exported.quotes.row_count != eligible_quotes:
                raise MarketHistoryRetentionError("market_history_archive_quote_export_mismatch")

            scan_key = f"{archive_prefix}/a6_market_scan_runs-{exported.scan_runs.sha256}.csv.gz"
            quote_key = f"{archive_prefix}/a6_market_quotes-{exported.quotes.sha256}.csv.gz"
            scan_object = _upload_verified(store, scan_key, exported.scan_runs)
            quote_object = _upload_verified(store, quote_key, exported.quotes)
            manifest_path, manifest_sha256 = _write_manifest(
                directory,
                config=config,
                cutoff=cutoff,
                archived_at=archived_at,
                candidates=candidates,
                quote_count=eligible_quotes,
                store_identity=store.identity,
                archive_prefix=archive_prefix,
                scan_object=scan_object,
                quote_object=quote_object,
            )
            manifest_file = MarketHistoryArchiveFile(
                path=manifest_path,
                row_count=1,
                sha256=manifest_sha256,
                size_bytes=manifest_path.stat().st_size,
                content_type="application/json",
                content_encoding="",
            )
            manifest_key = f"{archive_prefix}/manifest.json"
            manifest_object = _upload_verified(store, manifest_key, manifest_file)

            deleted_quotes = _delete_quotes(cursor, candidate_ids)
            if deleted_quotes != eligible_quotes:
                raise MarketHistoryRetentionError(
                    "market_history_retention_quote_delete_mismatch"
                )
            cursor.execute(
                """
                DELETE FROM a6_trade.a6_market_scan_runs scan
                WHERE scan.id = ANY(%s)
                  AND scan.started_at < %s
                  AND scan.status <> 'running'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM a6_trade.a6_market_channel_signals_current signal
                      WHERE signal.scan_run_id = scan.id
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM a6_trade.a6_channel_observer_health_current health
                      WHERE health.scan_run_id = scan.id
                  )
                RETURNING scan.id
                """,
                (candidate_ids, cutoff),
            )
            deleted_ids = {int(row["id"]) for row in _rows(cursor)}
            if deleted_ids != set(candidate_ids):
                raise MarketHistoryRetentionError(
                    "market_history_retention_scan_delete_mismatch"
                )
            remaining_quotes = _quote_count(cursor, candidate_ids)
            if remaining_quotes != 0:
                raise MarketHistoryRetentionError(
                    "market_history_retention_quote_cascade_mismatch"
                )
            _insert_archive_receipt(
                cursor,
                store_identity=store.identity,
                archive_prefix=archive_prefix,
                cutoff=cutoff,
                candidates=candidates,
                quote_count=eligible_quotes,
                scan_object=scan_object,
                quote_object=quote_object,
                manifest_object=manifest_object,
                archived_at=archived_at,
            )

        return MarketHistoryRetentionResult(
            config=config,
            cutoff=cutoff,
            applied=True,
            candidates=candidates,
            eligible_quote_count=eligible_quotes,
            archive_prefix=archive_prefix,
            scan_archive_sha256=scan_object.sha256,
            quote_archive_sha256=quote_object.sha256,
            manifest_sha256=manifest_object.sha256,
            archived_bytes=(
                scan_object.size_bytes + quote_object.size_bytes + manifest_object.size_bytes
            ),
            deleted_scan_runs=len(deleted_ids),
            deleted_quotes=deleted_quotes,
            deleted_payload_bytes=sum(item.payload_bytes for item in candidates),
        )


def _copy_gzip(
    connection: Any,
    query: str,
    params: tuple[Any, ...],
    path: Path,
    *,
    row_count: int,
) -> MarketHistoryArchiveFile:
    digest = hashlib.sha256()
    with path.open("wb") as raw:
        hashing_writer = _HashingWriter(raw, digest)
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=hashing_writer,
            compresslevel=6,
            mtime=0,
        ) as compressed, connection.cursor().copy(query, params) as copy:
            while True:
                chunk = copy.read()
                if not chunk:
                    break
                compressed.write(bytes(chunk))
    return MarketHistoryArchiveFile(
        path=path,
        row_count=row_count,
        sha256=digest.hexdigest(),
        size_bytes=path.stat().st_size,
    )


class _HashingWriter:
    def __init__(self, raw: Any, digest: Any) -> None:
        self.raw = raw
        self.digest = digest

    def write(self, data: bytes) -> int:
        self.digest.update(data)
        return int(self.raw.write(data))

    def flush(self) -> None:
        self.raw.flush()

    def tell(self) -> int:
        return int(self.raw.tell())


def _archive_prefix(
    root: str,
    candidates: tuple[MarketHistoryRetentionCandidate, ...],
) -> str:
    oldest = candidates[0]
    newest = candidates[-1]
    ids = [item.scan_run_id for item in candidates]
    batch_hash = hashlib.sha256(
        json.dumps(ids, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    partition = oldest.started_at.strftime("%Y-%m-%d")
    return (
        f"{root}/version=1/date={partition}/"
        f"scan-runs={oldest.scan_run_id}-{newest.scan_run_id}-{batch_hash}"
    )


def _upload_verified(
    store: MarketHistoryObjectStore,
    key: str,
    archive_file: MarketHistoryArchiveFile,
) -> MarketHistoryObject:
    store.put_file(
        key,
        archive_file.path,
        sha256=archive_file.sha256,
        content_type=archive_file.content_type,
        content_encoding=archive_file.content_encoding or None,
    )
    observed = store.head(key)
    if observed.size_bytes != archive_file.size_bytes:
        raise MarketHistoryRetentionError("market_history_archive_size_mismatch")
    if observed.sha256 != archive_file.sha256:
        raise MarketHistoryRetentionError("market_history_archive_sha256_mismatch")
    return observed


def _write_manifest(
    directory: Path,
    *,
    config: MarketHistoryRetentionConfig,
    cutoff: datetime,
    archived_at: datetime,
    candidates: tuple[MarketHistoryRetentionCandidate, ...],
    quote_count: int,
    store_identity: Mapping[str, str],
    archive_prefix: str,
    scan_object: MarketHistoryObject,
    quote_object: MarketHistoryObject,
) -> tuple[Path, str]:
    document = {
        "archive_format": _ARCHIVE_FORMAT_VERSION,
        "archive_prefix": archive_prefix,
        "archived_at": archived_at.isoformat(),
        "cutoff": cutoff.isoformat(),
        "retention_hours": config.retention_hours,
        "store": dict(store_identity),
        "scan_run_ids": [item.scan_run_id for item in candidates],
        "scan_run_count": len(candidates),
        "quote_count": quote_count,
        "objects": {
            "a6_market_scan_runs": _object_document(scan_object, len(candidates)),
            "a6_market_quotes": _object_document(quote_object, quote_count),
        },
    }
    path = directory / "manifest.json"
    payload = json.dumps(
        document,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode() + b"\n"
    path.write_bytes(payload)
    return path, hashlib.sha256(payload).hexdigest()


def _object_document(
    value: MarketHistoryObject,
    row_count: int | None,
) -> dict[str, Any]:
    return {
        "key": value.key,
        "row_count": row_count,
        "sha256": value.sha256,
        "size_bytes": value.size_bytes,
    }


def _insert_archive_receipt(
    cursor: Any,
    *,
    store_identity: Mapping[str, str],
    archive_prefix: str,
    cutoff: datetime,
    candidates: tuple[MarketHistoryRetentionCandidate, ...],
    quote_count: int,
    scan_object: MarketHistoryObject,
    quote_object: MarketHistoryObject,
    manifest_object: MarketHistoryObject,
    archived_at: datetime,
) -> None:
    cursor.execute(
        """
        INSERT INTO a6_trade.a6_market_history_archives (
            archive_format, archive_prefix, store_backend, store_bucket,
            store_endpoint_hash, retention_cutoff, oldest_started_at,
            newest_started_at, scan_run_ids, scan_run_count, quote_count,
            scan_object_key, scan_object_sha256, scan_object_bytes,
            quote_object_key, quote_object_sha256, quote_object_bytes,
            manifest_object_key, manifest_object_sha256, manifest_object_bytes,
            archived_at, deleted_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        """,
        (
            _ARCHIVE_FORMAT_VERSION,
            archive_prefix,
            str(store_identity.get("backend") or ""),
            str(store_identity.get("bucket") or ""),
            str(store_identity.get("endpoint_sha256") or ""),
            cutoff,
            candidates[0].started_at,
            candidates[-1].started_at,
            [item.scan_run_id for item in candidates],
            len(candidates),
            quote_count,
            scan_object.key,
            scan_object.sha256,
            scan_object.size_bytes,
            quote_object.key,
            quote_object.sha256,
            quote_object.size_bytes,
            manifest_object.key,
            manifest_object.sha256,
            manifest_object.size_bytes,
            archived_at,
            archived_at,
        ),
    )


def _quote_count(cursor: Any, scan_run_ids: list[int]) -> int:
    if not scan_run_ids:
        return 0
    cursor.execute(
        """
        SELECT count(*)::bigint AS quote_count
        FROM a6_trade.a6_market_quotes
        WHERE scan_run_id = ANY(%s)
        """,
        (scan_run_ids,),
    )
    row = _row(cursor)
    if row is None:
        raise MarketHistoryRetentionError("market_history_retention_quote_count_missing")
    return int(row["quote_count"])


def _delete_quotes(cursor: Any, scan_run_ids: list[int]) -> int:
    cursor.execute(
        """
        WITH deleted_quotes AS (
            DELETE FROM a6_trade.a6_market_quotes
            WHERE scan_run_id = ANY(%s)
            RETURNING id
        )
        SELECT count(*)::bigint AS deleted_quote_count
        FROM deleted_quotes
        """,
        (scan_run_ids,),
    )
    row = _row(cursor)
    if row is None:
        raise MarketHistoryRetentionError("market_history_retention_quote_delete_missing")
    return int(row["deleted_quote_count"])


def _candidate(row: Mapping[str, Any]) -> MarketHistoryRetentionCandidate:
    scan_run_id = int(row["scan_run_id"])
    candidate_count = int(row["candidate_count"])
    payload_bytes = int(row.get("payload_bytes") or 0)
    if scan_run_id < 1 or candidate_count < 0 or payload_bytes < 0:
        raise MarketHistoryRetentionError("market_history_retention_candidate_invalid")
    return MarketHistoryRetentionCandidate(
        scan_run_id=scan_run_id,
        started_at=_aware_utc(row["started_at"]),
        candidate_count=candidate_count,
        payload_bytes=payload_bytes,
    )


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MarketHistoryRetentionError("market_history_retention_datetime_invalid")
    return value.astimezone(timezone.utc)


def _rows(cursor: Any) -> tuple[dict[str, Any], ...]:
    rows = cursor.fetchall()
    if not rows:
        return ()
    if isinstance(rows[0], Mapping):
        return tuple(dict(row) for row in rows)
    names = [column.name for column in cursor.description or ()]
    return tuple(dict(zip(names, row, strict=False)) for row in rows)


def _row(cursor: Any) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return dict(row)
    names = [column.name for column in cursor.description or ()]
    return dict(zip(names, row, strict=False))


def _close(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()
