"""WeatherNext 3 point reads from the official statistics Zarr store.

The published arrays use one Zstd-compressed global plane per forecast hour.
Each selected plane is therefore read and validated as a complete immutable
GCS object before the requested point is extracted.
"""
from __future__ import annotations

import json
import hashlib
import math
import os
import re
import stat
import struct
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


BUCKET = "weathernext3_statistics_spatial"
ROOT = "weathernext_3_0_0_statistics/zarr/2026_to_present"
RUN_RE = re.compile(r"^\d{8}_\d{2}hr_01_preds$")
ARRAY_RE = re.compile(r"^[a-z0-9_]+_(?:mean|p10|p25|p50|p75|p90)$")
KCDW = (40.8752, -74.2814)
FLOAT32_BYTES = 4


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("time must include UTC offset")
    return parsed.astimezone(timezone.utc)


def run_name(value: str) -> tuple[str, datetime]:
    run = parse_utc(value)
    if run.minute or run.second or run.microsecond or run.hour % 6:
        raise ValueError("run must be a 00/06/12/18 UTC initialization")
    name = run.strftime("%Y%m%d_%Hhr_01_preds")
    if not RUN_RE.fullmatch(name):
        raise ValueError("invalid run")
    return name, run


def nearest_index(values: tuple[float, ...], target: float) -> int:
    if not values or not math.isfinite(target):
        raise ValueError("invalid coordinate")
    return min(range(len(values)), key=lambda index: abs(values[index] - target))


def validate_array_metadata(name: str, metadata: dict[str, Any]) -> tuple[int, int]:
    if not ARRAY_RE.fullmatch(name):
        raise ValueError("invalid array name")
    if metadata.get("data_type") != "float32":
        raise ValueError("unsupported data type")
    shape = metadata.get("shape")
    chunk = metadata.get("chunk_grid", {}).get("configuration", {}).get("chunk_shape")
    dimensions = metadata.get("dimension_names")
    codecs = metadata.get("codecs")
    if shape != [360, 1801, 3600] or chunk != [1, 1801, 3600]:
        raise ValueError("unsupported array shape")
    if dimensions != ["lead_time", "lat_0p1", "lon_0p1"]:
        raise ValueError("unsupported array dimensions")
    expected = [
        {"name": "bytes", "configuration": {"endian": "little"}},
        {"name": "zstd", "configuration": {"level": 0, "checksum": False}},
    ]
    if codecs != expected:
        raise ValueError("unsupported codecs")
    return shape[1], shape[2]


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    generation: int
    crc32c: int


class GrpcStore:
    """Small authenticated GCS gRPC facade; credentials never enter results."""

    def __init__(self, *, project: str | None = None, direct_path: bool = False,
                 cache_dir: Path | str | None = None):
        try:
            import google.auth
            from google.auth.exceptions import DefaultCredentialsError
            from google.auth.credentials import Credentials
            from google.cloud import _storage_v2
            from google.cloud._storage_v2.services.storage.transports.grpc import StorageGrpcTransport
        except ImportError as error:
            raise RuntimeError("install requirements.txt") from error

        # The statistics bucket is not requester-pays. Do not infer a quota
        # project from ADC: an identity can read this allowlisted bucket while
        # lacking serviceusage.services.use on that unrelated project.
        project = project or os.environ.get("WN3_GCS_PROJECT")
        try:
            credentials, detected_project = google.auth.default(
                scopes=("https://www.googleapis.com/auth/devstorage.read_only",)
            )
            del detected_project
        except DefaultCredentialsError:
            class GcloudCredentials(Credentials):
                """Refreshable local credential without persisting token data."""
                def refresh(self, request):
                    del request
                    token = subprocess.run(
                        ["gcloud", "auth", "print-access-token"], check=True,
                        capture_output=True, text=True, timeout=15,
                    ).stdout.strip()
                    if not token or not re.fullmatch(r"[A-Za-z0-9._~+\-/=]+", token):
                        raise RuntimeError("could not obtain GCS credentials")
                    self.token = token
                    # google-auth's credential base compares against a naive
                    # UTC clock even though forecast times remain aware.
                    self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=50)
                    del token

            credentials = GcloudCredentials()
            credentials.refresh(None)

        if project and hasattr(credentials, "with_quota_project"):
            credentials = credentials.with_quota_project(project)

        channel = StorageGrpcTransport.create_channel(
            credentials=credentials, attempt_direct_path=direct_path
        )
        transport = StorageGrpcTransport(credentials=credentials, channel=channel)
        self._api = _storage_v2
        self._client = _storage_v2.StorageClient(transport=transport)
        self.project = project
        configured_cache = os.environ.get("WN3_ZARR_CACHE_DIR")
        self.cache_dir = Path(cache_dir or configured_cache) if cache_dir or configured_cache else None
        self.cache_max_bytes = int(os.environ.get("WN3_ZARR_CACHE_MAX_BYTES", str(24 * 1024**3)))
        if self.cache_max_bytes <= 0:
            raise ValueError("WN3_ZARR_CACHE_MAX_BYTES must be positive")
        self._cache_lock = threading.Lock()
        self._cache_bytes = 0
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            for entry in self.cache_dir.iterdir():
                try:
                    info = entry.lstat()
                    if entry.name.endswith(".zstd") and stat.S_ISREG(info.st_mode):
                        self._cache_bytes += info.st_size
                except OSError:
                    pass
        from google.api_core.exceptions import NotFound
        self._not_found = NotFound

    @property
    def bucket_path(self) -> str:
        return f"projects/_/buckets/{BUCKET}"

    def info(self, name: str) -> ObjectInfo:
        request = self._api.GetObjectRequest(bucket=self.bucket_path, object_=name)
        result = self._client.get_object(request=request)
        return ObjectInfo(size=int(result.size), generation=int(result.generation),
                          crc32c=int(result.checksums.crc32c))

    def is_not_found(self, error: BaseException) -> bool:
        return isinstance(error, self._not_found)

    def _cache_path(self, name: str, info: ObjectInfo) -> Path | None:
        if self.cache_dir is None:
            return None
        identity = f"{BUCKET}\0{name}\0{info.generation}\0{info.size}\0{info.crc32c}"
        return self.cache_dir / (hashlib.sha256(identity.encode()).hexdigest() + ".zstd")

    def _prune_cache_locked(self, protected: Path) -> None:
        if self.cache_dir is None or self._cache_bytes <= self.cache_max_bytes:
            return
        candidates = []
        for entry in self.cache_dir.iterdir():
            try:
                info = entry.lstat()
                if entry != protected and entry.name.endswith(".zstd") and stat.S_ISREG(info.st_mode):
                    candidates.append((info.st_mtime_ns, entry, info.st_size))
            except OSError:
                pass
        target = self.cache_max_bytes * 4 // 5
        for _, path, size in sorted(candidates):
            if self._cache_bytes <= target:
                break
            try:
                path.unlink()
                self._cache_bytes -= size
            except OSError:
                pass

    @staticmethod
    def _checksum(data: bytes, expected: int) -> None:
        if not expected:
            return
        import google_crc32c
        if google_crc32c.value(data) != expected:
            raise ValueError("GCS object checksum mismatch")

    def read_object(self, name: str, info: ObjectInfo) -> tuple[bytes, bool]:
        """Read one generation-bound object, reusing a checksum-bound cache."""
        cached = self._cache_path(name, info)
        if cached is not None:
            try:
                data = cached.read_bytes()
                if len(data) != info.size:
                    raise ValueError("cached object size mismatch")
                self._checksum(data, info.crc32c)
                return data, True
            except (OSError, ValueError):
                with self._cache_lock:
                    try:
                        size = cached.lstat().st_size
                        cached.unlink()
                        self._cache_bytes = max(0, self._cache_bytes - size)
                    except OSError:
                        pass
        data = self.read(name, length=info.size, generation=info.generation)
        self._checksum(data, info.crc32c)
        if cached is not None:
            descriptor, temporary = tempfile.mkstemp(prefix=cached.name + ".", dir=cached.parent)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                with self._cache_lock:
                    previous = cached.lstat().st_size if cached.exists() else 0
                    os.replace(temporary, cached)
                    self._cache_bytes += len(data) - previous
                    self._prune_cache_locked(cached)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        return data, False

    def read(self, name: str, *, offset: int = 0, length: int = 0,
             generation: int = 0) -> bytes:
        request = self._api.ReadObjectRequest(
            bucket=self.bucket_path,
            object_=name,
            generation=generation,
            read_offset=offset,
            read_limit=length,
        )
        parts = []
        for response in self._client.read_object(request=request):
            part = bytes(response.checksummed_data.content)
            checksum = int(response.checksummed_data.crc32c)
            if checksum:
                import google_crc32c
                if google_crc32c.value(part) != checksum:
                    raise ValueError("gRPC content checksum mismatch")
            parts.append(part)
        data = b"".join(parts)
        if length and len(data) != length:
            raise ValueError("short gRPC range read")
        return data


class WeatherNext3Zarr:
    def __init__(self, store: GrpcStore, run: str):
        self.store = store
        self.run, self.init = run_name(run)
        self.prefix = f"{ROOT}/{self.run}/predictions.zarr"
        raw = self._object(f"{self.prefix}/zarr.json")[0]
        document = json.loads(raw)
        if document.get("zarr_format") != 3:
            raise ValueError("unsupported Zarr version")
        consolidated = document.get("consolidated_metadata")
        if not isinstance(consolidated, dict) or consolidated.get("kind") != "inline":
            raise ValueError("missing consolidated metadata")
        self.metadata = consolidated.get("metadata")
        if not isinstance(self.metadata, dict):
            raise ValueError("missing consolidated array metadata")
        self.latitudes = self._coordinate("lat_0p1")
        self.longitudes = self._coordinate("lon_0p1")
        self._point_infos: dict[str, ObjectInfo] = {}
        self._point_infos_lock = threading.Lock()

    @classmethod
    def candidates(cls, store: GrpcStore, now: datetime, *, attempts: int = 4):
        """Yield published run metadata from newest to oldest."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        candidate = now.astimezone(timezone.utc).replace(
            hour=now.astimezone(timezone.utc).hour // 6 * 6,
            minute=0, second=0, microsecond=0,
        )
        for _ in range(attempts):
            try:
                yield cls(store, candidate.isoformat().replace("+00:00", "Z"))
            except Exception as error:
                if not store.is_not_found(error):
                    raise
            candidate -= timedelta(hours=6)

    @classmethod
    def latest(cls, store: GrpcStore, now: datetime, *, attempts: int = 4) -> WeatherNext3Zarr:
        """Open the newest published 6-hourly run at or before ``now``."""
        for candidate in cls.candidates(store, now, attempts=attempts):
            return candidate
        raise ValueError("no recent WeatherNext 3 Zarr run is available")

    def _object(self, name: str) -> tuple[bytes, bool]:
        info = self.store.info(name)
        return self.store.read_object(name, info)

    def _coordinate(self, name: str) -> tuple[float, ...]:
        metadata = self.metadata[name]
        size = metadata["shape"][0]
        if metadata.get("data_type") != "float32" or metadata.get("dimension_names") != [name]:
            raise ValueError("unsupported coordinate")
        encoded, _ = self._object(f"{self.prefix}/{name}/c/0")
        try:
            import zstandard
        except ImportError as error:
            raise RuntimeError("install requirements.txt") from error
        decoded = zstandard.ZstdDecompressor().decompress(encoded)
        if len(decoded) != size * FLOAT32_BYTES:
            raise ValueError("invalid coordinate chunk")
        values = struct.unpack(f"<{size}f", decoded)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("invalid coordinate values")
        return values

    def point_info(self, array: str, valid: str) -> ObjectInfo:
        """Validate a point request and resolve its immutable chunk metadata."""
        when = parse_utc(valid)
        hours = (when - self.init).total_seconds() / 3600
        if not hours.is_integer() or not 1 <= hours <= 360:
            raise ValueError("valid time must be an hourly lead from 1 through 360")
        lead_index = int(hours) - 1
        metadata = self.metadata.get(array)
        if not isinstance(metadata, dict):
            raise ValueError("array is not present in this run")
        validate_array_metadata(array, metadata)
        object_name = f"{self.prefix}/{array}/c/{lead_index}/0/0"
        with self._point_infos_lock:
            cached = self._point_infos.get(object_name)
        if cached is not None:
            return cached
        info = self.store.info(object_name)
        with self._point_infos_lock:
            return self._point_infos.setdefault(object_name, info)

    def point(self, array: str, valid: str, *, latitude: float = KCDW[0],
              longitude: float = KCDW[1]) -> dict[str, Any]:
        when = parse_utc(valid)
        hours = (when - self.init).total_seconds() / 3600
        if not hours.is_integer() or not 1 <= hours <= 360:
            raise ValueError("valid time must be an hourly lead from 1 through 360")
        lead_index = int(hours) - 1
        metadata = self.metadata.get(array)
        if not isinstance(metadata, dict):
            raise ValueError("array is not present in this run")
        lat_count, lon_count = validate_array_metadata(array, metadata)
        lat_index = nearest_index(self.latitudes, latitude)
        lon_index = nearest_index(self.longitudes, longitude % 360)
        if len(self.latitudes) != lat_count or len(self.longitudes) != lon_count:
            raise ValueError("coordinate shape mismatch")

        element = lat_index * lon_count + lon_index
        target_offset = element * FLOAT32_BYTES
        decoded_size = lat_count * lon_count * FLOAT32_BYTES
        object_name = f"{self.prefix}/{array}/c/{lead_index}/0/0"
        info = self.point_info(array, valid)

        try:
            import zstandard
        except ImportError as error:
            raise RuntimeError("install requirements.txt") from error
        encoded, cache_hit = self.store.read_object(object_name, info)
        decoded = zstandard.ZstdDecompressor().decompress(encoded)
        if len(decoded) != decoded_size:
            raise ValueError("invalid decoded chunk size")
        value = struct.unpack_from("<f", decoded, target_offset)[0]
        if not math.isfinite(value):
            raise ValueError("point value is missing or nonfinite")
        return {
            "model": "WeatherNext 3",
            "source": "official statistics Zarr via GCS gRPC object read",
            "run_utc": self.init.isoformat().replace("+00:00", "Z"),
            "valid_utc": when.isoformat().replace("+00:00", "Z"),
            "array": array,
            "unit": metadata.get("attributes", {}).get("units"),
            "value": value,
            "requested": {"latitude": latitude, "longitude": longitude},
            "grid_point": {
                "latitude": self.latitudes[lat_index],
                "longitude": ((self.longitudes[lon_index] + 180) % 360) - 180,
            },
            "transfer": {
                "bytes_read": len(encoded),
                "object_bytes": info.size,
                "fraction": 1.0,
                "cache_hit": cache_hit,
            },
        }
