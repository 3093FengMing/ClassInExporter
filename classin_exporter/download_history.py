"""Verified export records, scoped to each readable course output directory.

Records contain relative filenames and content identity, never request URLs,
credentials or machine paths. Files are checked before an export is skipped.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
from typing import Any

from .grouping import group_directory_names, resource_group, resource_output_dir
from .resources import DownloadResult, Resource


HISTORY_FILENAME = ".classin-downloads.json"
_MAX_HISTORY_BYTES = 8 * 1024 * 1024
_REGISTRY_LOCK = threading.Lock()
_STATES: dict[str, _DirectoryState] = {}
_CLAIMS: dict[tuple[str, str], threading.Lock] = {}
_RECORD_WARNING = "文件已保存，但本地下载记录未能更新；下次获取资源时可能需要重新校验。"


@dataclass(frozen=True)
class ExistingDownload:
    path: str
    bytes_written: int
    sha256: str
    source: str
    warning: str = ""


@dataclass
class _DirectoryState:
    lock: Any = field(default_factory=threading.RLock)
    loaded: bool = False
    signature: tuple[int, int, int, int] | None = None
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    unreadable: bool = False


def _cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        from .downloader import DownloadCancelled
        raise DownloadCancelled("下载已取消。")


def _key(resource: Resource) -> str:
    # A caller's resource ID may itself include a URL; persist its stable digest.
    return hashlib.sha256(str(resource.id).encode("utf-8")).hexdigest()


def _state(directory: Path) -> _DirectoryState:
    name = os.path.normcase(str(directory))
    with _REGISTRY_LOCK:
        if name not in _STATES:
            _STATES[name] = _DirectoryState()
        return _STATES[name]


def _signature(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino


def _digests(resource: Resource) -> dict[str, str]:
    result = {}
    for algorithm, length in (("sha256", 64), ("md5", 32)):
        value = str(resource.metadata.get(algorithm) or "").strip().strip('"').lower()
        if re.fullmatch(r"[0-9a-f]{" + str(length) + r"}", value):
            result[algorithm] = value
    return result


def _identity(resource: Resource) -> dict[str, Any]:
    from .downloader import _expected_size, _filename, _is_playlist
    fields = {}
    for key in ("sid", "schoolUid", "courseId", "activityId", "lessonId", "fileId", "lmsFileId", "userFileId", "referenceId"):
        value = resource.metadata.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and value not in ("", 0):
            fields[key] = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    return {
        "name_sha256": hashlib.sha256(_filename(resource, _is_playlist(resource)).encode("utf-8")).hexdigest(),
        "expected_size": _expected_size(resource),
        "kind": resource.kind if resource.kind in {"file", "video", "replay"} else "other",
        "group_sha256": hashlib.sha256(resource_group(resource)[0].encode("utf-8")).hexdigest(),
        "fields": fields,
        "expected_hashes": _digests(resource),
    }


def _load(directory: Path, state: _DirectoryState) -> None:
    history = directory / HISTORY_FILENAME
    try:
        info = history.lstat()
    except FileNotFoundError:
        state.loaded, state.signature, state.records, state.unreadable = True, None, {}, False
        return
    except OSError:
        state.loaded, state.signature, state.records, state.unreadable = True, None, {}, True
        return
    signature = _signature(info)
    if state.loaded and signature == state.signature:
        return
    state.loaded, state.signature, state.records, state.unreadable = True, signature, {}, True
    if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_HISTORY_BYTES:
        return
    try:
        payload = json.loads(history.read_text(encoding="utf-8"))
        if (_signature(history.lstat()) != signature or not isinstance(payload, dict)
                or payload.get("version") != 1 or not isinstance(payload.get("resources"), dict)
                or len(payload["resources"]) > 50000):
            return
        state.records = {key: value for key, value in payload["resources"].items()
                         if isinstance(key, str) and isinstance(value, dict)}
        state.unreadable = False
    except (OSError, ValueError, UnicodeError):
        return


def _write(directory: Path, state: _DirectoryState) -> bool:
    if state.unreadable:
        return False
    temporary: Path | None = None
    try:
        data = json.dumps({"version": 1, "resources": state.records}, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(data) > _MAX_HISTORY_BYTES:
            return False
        descriptor, name = tempfile.mkstemp(prefix=".classin-downloads-", suffix=".tmp", dir=directory)
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, directory / HISTORY_FILENAME)
        state.signature = _signature((directory / HISTORY_FILENAME).lstat())
        state.loaded = True
        return True
    except (OSError, ValueError, UnicodeError):
        state.loaded = False
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _file(directory: Path, filename: object) -> tuple[Path, os.stat_result] | None:
    if (not isinstance(filename, str) or not filename or filename in {".", "..", HISTORY_FILENAME}
            or any(character in filename for character in "/\\:") or Path(filename).name != filename):
        return None
    path = directory / filename
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or path.resolve().parent != directory:
            return None
        return path, info
    except OSError:
        return None


def _hash_file(path: Path, cancel: threading.Event | None) -> tuple[str, str, os.stat_result] | None:
    try:
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            sha256 = hashlib.sha256()
            md5 = hashlib.md5(usedforsecurity=False)
            while chunk := source.read(64 * 1024):
                _cancel(cancel)
                sha256.update(chunk)
                md5.update(chunk)
            _cancel(cancel)
            after = os.fstat(source.fileno())
        current = path.lstat()
        if (_signature(before) != _signature(after) or _signature(current) != _signature(before)
                or not stat.S_ISREG(current.st_mode)):
            return None
        return sha256.hexdigest(), md5.hexdigest(), current
    except OSError:
        return None


def _entry(resource: Resource, path: Path, info: os.stat_result, sha256: str, source: str) -> dict[str, Any]:
    return {"filename": path.name, "size": info.st_size, "mtime_ns": info.st_mtime_ns,
            "sha256": sha256, "source": source if source in {"remote", "cache"} else "remote",
            "identity": _identity(resource)}


def _existing(directory: Path, entry: dict[str, Any], warning: str = "") -> ExistingDownload:
    return ExistingDownload(str(directory / entry["filename"]), entry["size"], entry["sha256"], entry["source"], warning)


def _recover_legacy(resource: Resource, directory: Path, state: _DirectoryState,
                    cancel: threading.Event | None) -> ExistingDownload | None:
    from .downloader import _filename, _is_playlist
    expected = _digests(resource)
    # A server playlist digest does not describe the locally merged MP4.
    if not expected or _is_playlist(resource):
        return None
    name = _filename(resource)
    base = Path(name)
    numbered = re.compile(re.escape(base.stem) + r" \(([1-9][0-9]{0,3})\)" + re.escape(base.suffix), re.IGNORECASE)
    try:
        candidates = sorted((path for path in directory.iterdir()
                             if path.name.casefold() == name.casefold() or numbered.fullmatch(path.name)),
                            key=lambda path: (path.name.casefold() != name.casefold(), path.name.casefold()))
    except OSError:
        return None
    for candidate in candidates:
        _cancel(cancel)
        found = _file(directory, candidate.name)
        if found is None:
            continue
        path, info = found
        if resource.size and type(resource.size) is int and resource.size > 0 and info.st_size != resource.size:
            continue
        hashed = _hash_file(path, cancel)
        if hashed is None:
            continue
        sha256, md5, info = hashed
        values = {"sha256": sha256, "md5": md5}
        if not all(values[algorithm] == digest for algorithm, digest in expected.items()):
            continue
        entry = _entry(resource, path, info, sha256, "remote")
        state.records[_key(resource)] = entry
        warning = "" if _write(directory, state) else _RECORD_WARNING
        return _existing(directory, entry, warning)
    return None


def lookup_download(resource: Resource, output_dir: str | Path, *, verify_changed: bool = False,
                    recover_legacy: bool = False, cancel: threading.Event | None = None) -> ExistingDownload | None:
    """Find a verified export; rehash changes and legacy files only in workers."""
    _cancel(cancel)
    try:
        directory = Path(output_dir).expanduser().resolve()
    except (OSError, ValueError):
        return None
    state = _state(directory)
    with state.lock:
        _load(directory, state)
        key = _key(resource)
        entry = state.records.get(key)
        if entry is not None and entry.get("identity") == _identity(resource):
            found = _file(directory, entry.get("filename"))
            valid_digest = isinstance(entry.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
            valid_stats = type(entry.get("size")) is int and type(entry.get("mtime_ns")) is int
            valid_source = entry.get("source") in {"remote", "cache"}
            if found is not None and valid_digest and valid_stats and valid_source:
                path, info = found
                if (info.st_size, info.st_mtime_ns) == (entry["size"], entry["mtime_ns"]):
                    return _existing(directory, entry)
                if verify_changed:
                    hashed = _hash_file(path, cancel)
                    if hashed is not None and hashed[0] == entry["sha256"]:
                        entry = _entry(resource, path, hashed[2], hashed[0], entry["source"])
                        state.records[key] = entry
                        warning = "" if _write(directory, state) else _RECORD_WARNING
                        return _existing(directory, entry, warning)
            if verify_changed:
                state.records.pop(key, None)
                _write(directory, state)
        if verify_changed and recover_legacy:
            return _recover_legacy(resource, directory, state, cancel)
    return None


def record_download(resource: Resource, result: DownloadResult) -> str:
    """Record a published file; an index failure cannot fail the actual export."""
    try:
        path = Path(result.path).expanduser().resolve()
        directory = path.parent
        found = _file(directory, path.name)
        if (found is None or found[1].st_size != result.bytes_written
                or not re.fullmatch(r"[0-9a-f]{64}", result.sha256)):
            return _RECORD_WARNING
        state = _state(directory)
        with state.lock:
            _load(directory, state)
            if state.unreadable:
                return _RECORD_WARNING
            state.records[_key(resource)] = _entry(resource, path, found[1], result.sha256, result.source)
            return "" if _write(directory, state) else _RECORD_WARNING
    except (OSError, ValueError, TypeError, UnicodeError):
        return _RECORD_WARNING


def find_downloaded_resources(resources: Iterable[Resource], output_root: str | Path, *,
                              verify_changed: bool = True, cancel: threading.Event | None = None) -> dict[str, ExistingDownload]:
    """Check a complete scan against its stable course directory plan."""
    rows = list(resources)
    names = group_directory_names(rows)
    completed = {}
    for resource in rows:
        _cancel(cancel)
        directory = resource_output_dir(output_root, resource, directory_names=names)
        existing = lookup_download(resource, directory, verify_changed=verify_changed,
                                   recover_legacy=verify_changed, cancel=cancel)
        if existing is not None:
            completed[resource.id] = existing
    return completed


@contextmanager
def download_lock(resource: Resource, output_dir: str | Path,
                  cancel: threading.Event | None = None) -> Iterator[None]:
    """Serialize duplicate requests for one resource, keeping other files parallel."""
    directory = os.path.normcase(str(Path(output_dir).expanduser().resolve()))
    with _REGISTRY_LOCK:
        key = (directory, _key(resource))
        if key not in _CLAIMS:
            _CLAIMS[key] = threading.Lock()
        claim = _CLAIMS[key]
    while not claim.acquire(timeout=0.1):
        _cancel(cancel)
    try:
        _cancel(cancel)
        yield
    finally:
        claim.release()
