"""Immutable, content-addressed derived market data for research tasks.

Publishing runs beside PostgreSQL and projects accepted raw records into a
small, explicit research schema.  Raw provider JSON is never written to the
Parquet objects transferred to compute nodes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from app.services.compact_feature_store import (
    _cutoff,
    _month_ranges,
    _sha256_file,
    _ymd,
    feature_cache_root,
)

try:  # pragma: no cover - platform-specific import
    import fcntl as _file_lock
except ImportError:  # Windows developer/test hosts
    _file_lock = None

CONTRACT = "alis_market_dataset_v2_derived_fields_four_clocks"
CLOCKS = ("observed_at", "first_observed_at", "economic_available_time", "created_at")
RAW_APIS = ("fund_daily", "index_daily")
METADATA_FIELDS = (
    "api_name",
    "record_key",
    "ts_code",
    "trade_date",
    "source_code",
    "economic_available_time",
    "observed_at",
    "first_observed_at",
    "observation_provenance",
    "content_hash",
    "created_at",
)
PRICE_FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "change",
    "pct_chg",
    "vol",
    "amount",
)
FEATURE_FIELDS = {
    "fund_daily": (*PRICE_FIELDS, "name"),
    "index_daily": PRICE_FIELDS,
    "snapshot": (
        "name",
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "change_pct",
        "volume",
        "turnover",
        "total_mv",
        "circ_mv",
        "pe_ttm",
        "pb",
        "turnover_rate",
        "adj_factor",
        "up_limit",
        "down_limit",
        "is_suspended",
        "data_quality_score",
    ),
}
FORBIDDEN_DATASET_FIELDS = frozenset(
    {"payload_json", "raw_json", "raw_payload", "provider_payload"}
)
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_binding_lock = threading.RLock()


class DatasetUnavailable(RuntimeError):
    pass


def dataset_configuration_status() -> dict[str, object]:
    """Cheap preflight used before a worker claims research work."""
    required = str(os.getenv("ALIS_MARKET_DATASET_REQUIRED", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    source = str(os.getenv("ALIS_MARKET_DATASET_SOURCE", "")).strip()
    root = dataset_root()
    errors: list[str] = []
    if required and not source:
        errors.append("ALIS_MARKET_DATASET_SOURCE is not configured")
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".preflight"
        probe.write_text("ok", encoding="ascii")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        errors.append(f"dataset root is not writable: {type(exc).__name__}")
    return {
        "required": required,
        "configured": bool(source),
        "source": source or None,
        "root": str(root),
        "ready": not errors,
        "errors": errors,
    }


def dataset_root() -> Path:
    return Path(
        os.getenv("ALIS_MARKET_DATASET_ROOT")
        or feature_cache_root() / "market_datasets"
    )


def _json_bytes(value: dict) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _identity(value: dict) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _dataset_schemas() -> dict[str, list[str]]:
    return {
        category: [*METADATA_FIELDS, *FEATURE_FIELDS[category]]
        for category in ("snapshot", *RAW_APIS)
    }


def _sealed(start: str, end: str) -> bool:
    # Keep the existing governance authority as the single source of dates.
    from app.services.sealed_strategy_holdout import (
        SEALED_HOLDOUT_END,
        SEALED_HOLDOUT_START,
    )

    return start <= SEALED_HOLDOUT_END and end >= SEALED_HOLDOUT_START


@contextmanager
def _lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".sync.lock").open("a+b") as handle:
        if _file_lock is not None:
            _file_lock.flock(handle, _file_lock.LOCK_EX)
        else:  # pragma: no cover - exercised on Windows only
            import msvcrt

            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if _file_lock is not None:
                _file_lock.flock(handle, _file_lock.LOCK_UN)
            else:  # pragma: no cover - exercised on Windows only
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _atomic(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".dataset-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _validate_manifest(manifest: dict, version: str) -> None:
    if not _HEX.fullmatch(version) or _identity(manifest) != version:
        raise DatasetUnavailable("dataset manifest identity mismatch")
    if manifest.get("contract") != CONTRACT:
        raise DatasetUnavailable("unsupported dataset contract")
    if manifest.get("schemas") != _dataset_schemas():
        raise DatasetUnavailable("research dataset schema contract mismatch")
    start, end = _ymd(manifest["start"]), _ymd(manifest["end"])
    if start > end or _sealed(start, end):
        raise DatasetUnavailable("invalid or sealed dataset range")
    expected = {
        (category, lo, hi)
        for lo, hi in _month_ranges(start, end)
        for category in ("snapshot", *RAW_APIS)
    }
    actual = set()
    for part in manifest["partitions"]:
        key = (part["category"], part["start"], part["end"])
        if key in actual or key not in expected or not _HEX.fullmatch(part["sha256"]):
            raise DatasetUnavailable("invalid dataset partition")
        if int(part["rows"]) < 0 or int(part["bytes"]) <= 0:
            raise DatasetUnavailable("invalid dataset partition size")
        actual.add(key)
    if actual != expected:
        raise DatasetUnavailable("dataset partition coverage missing")
    dates = manifest["trade_dates"]
    if dates != sorted(set(dates)) or any(not start <= _ymd(d) <= end for d in dates):
        raise DatasetUnavailable("invalid dataset calendar")


def _valid_rows(rows: list[dict]) -> list[dict]:
    result = []
    for row in rows:
        if not row.get("content_hash") or str(
            row.get("observation_provenance", "")
        ).lower().startswith("legacy_"):
            continue
        try:
            for clock in CLOCKS:
                _cutoff(row[clock])
        except (KeyError, TypeError, ValueError):
            continue
        result.append(row)
    return result


def _derived_row(category: str, row: dict) -> dict:
    """Project one DB-local raw row into the non-reversible research contract."""
    try:
        payload = json.loads(row.get("payload_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    projected = {field: row.get(field) for field in METADATA_FIELDS}
    for field in FEATURE_FIELDS[category]:
        value = payload.get(field)
        if field == "name":
            projected[field] = str(value or "")
        elif field == "is_suspended":
            projected[field] = bool(value) if value is not None else None
        else:
            try:
                projected[field] = (
                    float(value) if value is not None and value != "" else None
                )
            except (TypeError, ValueError):
                projected[field] = None
    return projected


def _validate_derived_columns(
    category: str, columns, *, require_required: bool = True
) -> None:
    names = set(columns)
    forbidden = names & FORBIDDEN_DATASET_FIELDS
    if forbidden:
        raise DatasetUnavailable(
            f"raw payload columns forbidden in research dataset: {sorted(forbidden)}"
        )
    allowed = set(METADATA_FIELDS) | set(FEATURE_FIELDS[category])
    unexpected = names - allowed
    if unexpected:
        raise DatasetUnavailable(
            f"unexpected research dataset columns: {sorted(unexpected)}"
        )
    required = {
        "ts_code",
        "trade_date",
        *CLOCKS,
        "observation_provenance",
        "content_hash",
    }
    if require_required:
        missing = required - names
        if missing:
            raise DatasetUnavailable(
                f"research dataset columns missing: {sorted(missing)}"
            )


def publish_market_dataset(start: str, end: str, *, root: Path | None = None) -> dict:
    """Run on the DB host only; repeatable-read freezes every accepted join."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from app.data_platform.db import _read_conn

    start, end = _ymd(start), _ymd(end)
    if start > end or _sealed(start, end):
        raise ValueError("public research dataset must exclude sealed holdout")
    db_host = urlsplit(os.environ.get("DATA_PLATFORM_DATABASE_URL", "")).hostname
    if db_host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("publish requires database-local DATA_PLATFORM_DATABASE_URL")
    root = root or dataset_root()
    with _lock(root), _read_conn() as conn:
        conn.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        try:
            dates = [
                str(dict(r)["trade_date"])
                for r in conn.execute(
                    "SELECT trade_date FROM dc_available_trade_dates WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
                    (start, end),
                ).fetchall()
            ]
            partitions = []
            for lo, hi in _month_ranges(start, end):
                for category in ("snapshot", *RAW_APIS):
                    if category == "snapshot":
                        sql = """SELECT v.ts_code,v.trade_date,v.payload_json,v.source_code,
                            v.economic_available_time,v.observed_at,v.first_observed_at,
                            v.observation_provenance,v.content_hash,v.created_at
                            FROM dc_stock_daily_snapshot_version v JOIN dc_stock_daily_snapshot p
                            ON p.ts_code=v.ts_code AND p.trade_date=v.trade_date AND p.content_hash=v.content_hash
                            WHERE v.trade_date BETWEEN ? AND ? ORDER BY v.ts_code,v.trade_date,v.observed_at,v.id"""
                        params = (lo, hi)
                    else:
                        sql = """SELECT v.api_name,v.record_key,v.ts_code,v.trade_date,v.payload_json,v.source_code,
                            v.economic_available_time,v.observed_at,v.first_observed_at,
                            v.observation_provenance,v.content_hash,v.created_at
                            FROM dc_tushare_raw_record_version v JOIN dc_tushare_raw_records p
                            ON p.api_name=v.api_name AND p.record_key=v.record_key AND p.content_hash=v.content_hash
                            WHERE v.api_name=? AND v.trade_date BETWEEN ? AND ? ORDER BY v.observed_at,v.id"""
                        params = (category, lo, hi)
                    raw_rows = _valid_rows(
                        [dict(r) for r in conn.execute(sql, params).fetchall()]
                    )
                    rows = [_derived_row(category, row) for row in raw_rows]
                    objects = root / "objects"
                    objects.mkdir(exist_ok=True)
                    fd, name = tempfile.mkstemp(dir=objects, prefix=".publish-")
                    os.close(fd)
                    try:
                        pq.write_table(
                            pa.Table.from_pylist(rows), name, compression="zstd"
                        )
                        sha = _sha256_file(Path(name))
                        target = objects / f"{sha}.parquet"
                        size = Path(name).stat().st_size
                        if not target.exists() or _sha256_file(target) != sha:
                            os.replace(name, target)
                        partitions.append(
                            dict(
                                category=category,
                                start=lo,
                                end=hi,
                                sha256=sha,
                                rows=len(rows),
                                bytes=size,
                            )
                        )
                    finally:
                        Path(name).unlink(missing_ok=True)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        manifest = dict(
            contract=CONTRACT,
            schemas=_dataset_schemas(),
            start=start,
            end=end,
            trade_dates=dates,
            partitions=partitions,
        )
        version = _identity(manifest)
        _validate_manifest(manifest, version)
        _atomic(root / "manifests" / f"{version}.json", _json_bytes(manifest))
        _atomic(root / "latest.json", _json_bytes({"version": version}))
    return dict(
        version=version,
        partitions=len(partitions),
        rows=sum(p["rows"] for p in partitions),
    )


def _fetch(source: str, relative: str, target: Path) -> None:
    """Read only known manifest/object names via local copy or authenticated SSH."""
    if not re.fullmatch(
        r"(?:latest\.json|manifests/[0-9a-f]{64}\.json|objects/[0-9a-f]{64}\.parquet)",
        relative,
    ):
        raise DatasetUnavailable("invalid dataset object path")
    if source.startswith("ssh://"):
        parsed = urlsplit(source)
        if (
            parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.port not in {None, 22}
        ):
            raise DatasetUnavailable("invalid dataset SSH source")
        userhost = (
            f"{parsed.username}@{parsed.hostname}"
            if parsed.username
            else str(parsed.hostname or "")
        )
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+", userhost) or userhost.startswith("-"):
            raise DatasetUnavailable("invalid SSH host")
        if (
            not re.fullmatch(r"/[A-Za-z0-9_./-]+", parsed.path)
            or ".." in Path(parsed.path).parts
        ):
            raise DatasetUnavailable("invalid SSH dataset directory")
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ProxyCommand=none",
            "-o",
            "StrictHostKeyChecking=yes",
        ]
        key = os.getenv("ALIS_MARKET_DATASET_SSH_KEY", "")
        if key:
            command += ["-i", key]
        command += [userhost, f"cat -- {parsed.path.rstrip('/')}/{relative}"]
        with target.open("wb") as handle:
            subprocess.run(
                command,
                stdout=handle,
                stderr=subprocess.DEVNULL,
                timeout=300,
                check=True,
            )
    else:
        shutil.copyfile(Path(source) / relative, target)


def sync_market_dataset(
    *,
    source: str | None = None,
    version: str | None = None,
    root: Path | None = None,
    progress=None,
) -> dict:
    source = source or os.getenv("ALIS_MARKET_DATASET_SOURCE", "")
    root = root or dataset_root()
    if not source:
        raise DatasetUnavailable("dataset source not configured")
    downloaded = reused = transferred = 0
    with (
        _lock(root),
        tempfile.TemporaryDirectory(dir=root, prefix=".prepare-") as stage_name,
    ):
        stage = Path(stage_name)
        if not version:
            _fetch(source, "latest.json", stage / "latest.json")
            version = json.loads((stage / "latest.json").read_text())["version"]
        if not isinstance(version, str) or not _HEX.fullmatch(version):
            raise DatasetUnavailable("invalid dataset version")
        manifest_path = root / "manifests" / f"{version}.json"
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text())
        else:
            _fetch(source, f"manifests/{version}.json", stage / "manifest.json")
            manifest = json.loads((stage / "manifest.json").read_text())
        _validate_manifest(manifest, version)
        for part in manifest["partitions"]:
            if progress:
                progress()
            relative = f"objects/{part['sha256']}.parquet"
            target = root / relative
            if (
                target.is_file()
                and target.stat().st_size == part["bytes"]
                and _sha256_file(target) == part["sha256"]
            ):
                reused += 1
                continue
            temporary = stage / "object.parquet"
            _fetch(source, relative, temporary)
            if (
                temporary.stat().st_size != part["bytes"]
                or _sha256_file(temporary) != part["sha256"]
            ):
                raise DatasetUnavailable("downloaded dataset checksum mismatch")
            _read_partition(temporary, part)
            target.parent.mkdir(exist_ok=True)
            os.replace(temporary, target)
            downloaded += 1
            transferred += part["bytes"]
        _atomic(manifest_path, _json_bytes(manifest))
        _atomic(root / "latest.json", _json_bytes({"version": version}))
    return dict(
        version=version,
        downloaded_partitions=downloaded,
        reused_partitions=reused,
        downloaded_bytes=transferred,
    )


def _read_partition(path: Path, part: dict) -> list[dict]:
    import pyarrow.parquet as pq

    if (
        not path.is_file()
        or path.stat().st_size != part["bytes"]
        or _sha256_file(path) != part["sha256"]
    ):
        raise DatasetUnavailable("dataset object missing or corrupt")
    with pq.ParquetFile(path) as parquet:
        _validate_derived_columns(
            part["category"],
            parquet.schema_arrow.names,
            require_required=int(part["rows"]) > 0,
        )
        rows = parquet.read().to_pylist()
    if len(rows) != part["rows"] or len(_valid_rows(rows)) != len(rows):
        raise DatasetUnavailable("dataset row/clock contract mismatch")
    if any(not part["start"] <= _ymd(row["trade_date"]) <= part["end"] for row in rows):
        raise DatasetUnavailable("dataset row outside partition")
    return rows


class MarketDataset:
    def __init__(self, version: str, *, root: Path | None = None):
        if not _HEX.fullmatch(version):
            raise DatasetUnavailable("invalid dataset version")
        self.root = root or dataset_root()
        self.version = version
        self.manifest = json.loads(
            (self.root / "manifests" / f"{version}.json").read_text()
        )
        _validate_manifest(self.manifest, version)
        self._cache: OrderedDict[str, list[dict]] = OrderedDict()
        self.reads = 0

    def dates(self, start: str, end: str) -> list[str]:
        start, end = _ymd(start), _ymd(end)
        if start < self.manifest["start"] or end > self.manifest["end"]:
            raise DatasetUnavailable("requested dates outside frozen dataset")
        return [d for d in self.manifest["trade_dates"] if start <= d <= end]

    def rows(
        self,
        category: str,
        *,
        cutoff: str | None,
        ymd: str = "",
        ts_codes=None,
        date_clause: str = "",
        date_params: tuple = (),
    ) -> list[dict]:
        if category not in ("snapshot", *RAW_APIS):
            raise DatasetUnavailable("category outside frozen market contract")
        lo, hi = self.manifest["start"], self.manifest["end"]
        lower_exclusive = False
        clause = " ".join(date_clause.strip().removeprefix("AND ").split()).lower()
        if ymd:
            lo = hi = _ymd(ymd)
        elif clause == "trade_date>? and trade_date<=?":
            lo, hi = map(_ymd, date_params)
            lower_exclusive = True
        elif clause == "trade_date between ? and ?":
            lo, hi = map(_ymd, date_params)
        elif clause == "trade_date<=?":
            hi = _ymd(date_params[0])
        elif clause:
            raise DatasetUnavailable("unsupported frozen dataset date predicate")
        self.dates(lo, hi)
        at = _cutoff(cutoff) if cutoff else None
        symbols = set(ts_codes or [])
        result = []
        for part in self.manifest["partitions"]:
            if part["category"] != category or part["end"] < lo or part["start"] > hi:
                continue
            sha = part["sha256"]
            if sha not in self._cache:
                self._cache[sha] = _read_partition(
                    self.root / "objects" / f"{sha}.parquet", part
                )
                if len(self._cache) > 2:
                    self._cache.popitem(last=False)
            self._cache.move_to_end(sha)
            for row in self._cache[sha]:
                day = row["trade_date"]
                if not lo <= day <= hi or (lower_exclusive and day == lo):
                    continue
                if symbols and row["ts_code"] not in symbols:
                    continue
                if at is not None and any(_cutoff(row[c]) > at for c in CLOCKS):
                    continue
                result.append(dict(row))
        self.reads += 1
        return result


def configured_dataset() -> MarketDataset | None:
    version = os.getenv("ALIS_MARKET_DATASET_VERSION", "")
    if version:
        return MarketDataset(version)
    if str(os.getenv("ALIS_MARKET_DATASET_REQUIRED", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise DatasetUnavailable(
            "required market dataset is not bound; remote SQL fallback is forbidden"
        )
    return None


@contextmanager
def bind_dataset(version: str):
    # Research worker has one claimant. The process binding also reaches its
    # replay executor threads; ContextVar alone would silently miss those.
    with _binding_lock:
        previous = os.environ.get("ALIS_MARKET_DATASET_VERSION")
        os.environ["ALIS_MARKET_DATASET_VERSION"] = version
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("ALIS_MARKET_DATASET_VERSION", None)
            else:
                os.environ["ALIS_MARKET_DATASET_VERSION"] = previous


@contextmanager
def without_dataset():
    """Temporarily hide public research data from the sealed holdout path."""
    with _binding_lock:
        previous = os.environ.pop("ALIS_MARKET_DATASET_VERSION", None)
        try:
            yield
        finally:
            if previous is not None:
                os.environ["ALIS_MARKET_DATASET_VERSION"] = previous
