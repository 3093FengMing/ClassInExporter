"""Read ClassIn's Qt disk cache without opening a writable cache manager.

Qt 5.15 QNetworkDiskCache v8 and QNetworkCacheMetaData serialization are
documented in analysis/public_web/qt5_network*.cpp. Metadata and body boundaries
are parsed with Qt's QDataStream; a cached HTTP fragment is never an original.
"""

from __future__ import annotations

import hashlib
import json
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from PySide6.QtCore import QByteArray, QDataStream, QDateTime, QFile, QIODevice, QUrl

from .resources import Resource


MAX_JSON_BYTES = 32 * 1024 * 1024
FILE_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt",
    ".zip", ".rar", ".7z", ".edb", ".edpx", ".mp4", ".webm", ".mkv",
    ".mov", ".avi", ".m3u8", ".pm3u8", ".mp3", ".wav", ".png", ".jpg",
}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m3u8", ".pm3u8"}
PLAYLIST_EXTENSIONS = {".m3u8", ".pm3u8"}


@dataclass
class CacheEntry:
    path: Path
    url: str = field(repr=False)
    headers: dict[str, str] = field(repr=False)
    status: int
    body_offset: int
    body_size: int
    compressed: bool
    payload: bytes | None = field(default=None, repr=False)
    complete: bool = False
    name: str = ""
    md5: str = ""


def _md5_header(headers: dict[str, str]) -> str:
    for key in ("x-cos-meta-md5", "etag"):
        value = headers.get(key, "").strip('" ')
        if re.fullmatch(r"[a-fA-F0-9]{32}", value):
            return value.lower()
    return ""


def _name(url: str, headers: dict[str, str]) -> str:
    disposition = headers.get("content-disposition", "")
    extended = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", disposition, re.I)
    quoted = re.search(r'filename\s*=\s*(?:"([^"]+)"|([^;]+))', disposition, re.I)
    if extended:
        return unquote(extended.group(1).strip())
    if quoted:
        return quoted.group(1) or quoted.group(2).strip()
    return unquote(Path(urlsplit(url).path).name) or "未命名资源"


def read_qt_cache(path: str | Path) -> CacheEntry:
    """Parse one Qt .d file. Raise ValueError for unsupported/truncated files."""
    path = Path(path)
    before = path.stat()
    file = QFile(str(path))
    if not file.open(QIODevice.OpenModeFlag.ReadOnly):
        raise ValueError("无法读取 Qt 缓存。")
    try:
        stream = QDataStream(file)
        marker, version, stream_version = (stream.readInt32() for _ in range(3))
        if marker != 232 or version != 8 or not 13 <= stream_version <= 19:
            raise ValueError("不支持的 Qt 缓存格式。")
        stream.setVersion(stream_version)
        url = QUrl()
        expiration, modified = QDateTime(), QDateTime()
        stream >> url >> expiration >> modified
        stream.readBool()  # saveToDisk
        attributes = {}
        count = stream.readUInt32()
        if count > 256:
            raise ValueError("无效的缓存属性数量。")
        for _ in range(count):
            key = stream.readInt32()
            attributes[key] = stream.readQVariant()
        headers = {}
        count = stream.readUInt32()
        if count > 256:
            raise ValueError("无效的缓存响应头数量。")
        for _ in range(count):
            key, value = QByteArray(), QByteArray()
            stream >> key >> value
            headers[bytes(key).decode("ascii", "ignore").lower()] = bytes(value).decode("utf-8", "replace")
        compressed = stream.readBool()
        offset = file.pos()
        if stream.status() != QDataStream.Status.Ok or not url.isValid():
            raise ValueError("缓存元数据不完整。")
        url_text = url.toString(QUrl.ComponentFormattingOption.FullyEncoded)
        parts = urlsplit(url_text)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("缓存地址不是 HTTP 资源。")
        payload = None
        body_size = before.st_size - offset
        if compressed:
            if body_size > MAX_JSON_BYTES:
                raise ValueError("压缩缓存超过读取上限。")
            data = QByteArray()
            stream >> data
            packed = bytes(data)
            if stream.status() != QDataStream.Status.Ok or len(packed) < 4 or file.pos() != file.size():
                raise ValueError("压缩缓存不完整。")
            declared = int.from_bytes(packed[:4], "big")
            if declared > MAX_JSON_BYTES:
                raise ValueError("解压缓存超过读取上限。")
            inflater = zlib.decompressobj()
            payload = inflater.decompress(packed[4:], MAX_JSON_BYTES + 1)
            if not inflater.eof or inflater.unused_data or len(payload) != declared:
                raise ValueError("压缩缓存正文不完整。")
            body_size = len(payload)
        status = int(attributes.get(0, 0) or 0)  # QNetworkRequest::HttpStatusCodeAttribute
    finally:
        file.close()
    md5 = _md5_header(headers)
    try:
        length = int(headers.get("content-length", "-1"))
    except ValueError:
        length = -1
    complete = status == 200 and body_size > 0 and length == body_size
    # A server MD5 verifies identity as well as length. For large files this is
    # streamed; no copy of the cache or its private request URL is saved.
    if md5 and status == 200 and body_size > 0:
        digest = hashlib.md5()
        if payload is not None:
            digest.update(payload)
        else:
            with path.open("rb") as source:
                source.seek(offset)
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
        complete = (length in (-1, body_size)) and digest.hexdigest() == md5
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        complete = False
    return CacheEntry(path, url_text, headers, status, offset, body_size, compressed,
                      payload, complete, _name(url_text, headers), md5)


def _entries(profile_dir: str | Path):
    profile = Path(profile_dir)
    if not profile.is_dir():
        return
    for path in profile.glob("data8/*/*.d"):
        try:
            yield read_qt_cache(path)
        except (ValueError, OSError, RuntimeError, zlib.error):
            continue


def discover_cache_resources(profile_dir: str | Path) -> list[Resource]:
    """Discover original remote URLs from the active profile's Qt cache index."""
    resources = {}
    for entry in _entries(profile_dir):
        parts = urlsplit(entry.url)
        extension = Path(parts.path).suffix.lower()
        if extension not in FILE_EXTENSIONS:
            continue
        # A server digest confirms equal original bodies across renewed signed
        # URLs. Without it, query selectors remain part of resource identity.
        identity_key = "md5:" + entry.md5 if entry.md5 else "url:" + entry.url
        identity = hashlib.sha256(identity_key.encode()).hexdigest()[:24]
        existing = resources.get(identity)
        if existing is not None and existing[0] and not entry.complete:
            continue
        playlist = extension in PLAYLIST_EXTENSIONS
        metadata = {
            "cache_offset": entry.body_offset,
            "cache_length": entry.body_size,
            "cache_qt_compressed": entry.compressed,
            "cache_complete": entry.complete and not playlist,
            "cache_status": ("仅播放列表" if playlist else
                             "完整" if entry.complete else "未确认"),
            "cache_original_url": entry.url,
            "cache_same_original": not playlist,
        }
        if playlist:
            metadata.update(stream_type="hls", stream_entry_only=True,
                            stream_format=extension.lstrip("."))
        if entry.md5 and not playlist:
            metadata.update(md5=entry.md5, cache_md5=entry.md5)
        try:
            size = max(0, int(entry.headers.get("content-length", "0")))
        except ValueError:
            size = 0
        resource = Resource("cache:" + identity, entry.name,
            "video" if extension in VIDEO_EXTENSIONS else "file", "cache-index",
            entry.url, 0 if playlist else size, local_path="" if playlist else str(entry.path), metadata=metadata)
        resources[identity] = (entry.complete, resource)
    return [resource for _complete, resource in resources.values()]


def read_cache_payloads(profile_dir: str | Path):
    """Yield actual cached HTTP JSON responses, excluding JS code and mocks."""
    for entry in _entries(profile_dir):
        if entry.status != 200 or entry.body_size > MAX_JSON_BYTES:
            continue
        mime = entry.headers.get("content-type", "").lower()
        if "json" not in mime:
            continue
        try:
            if entry.payload is None:
                with entry.path.open("rb") as source:
                    source.seek(entry.body_offset)
                    data = source.read(MAX_JSON_BYTES + 1)
            else:
                data = entry.payload
            if len(data) > MAX_JSON_BYTES:
                continue
            yield entry.url, json.loads(data.decode("utf-8-sig"))
        except (OSError, ValueError, UnicodeError):
            continue
