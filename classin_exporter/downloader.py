"""Remote-first downloads with atomic completion and verified cache fallback.

Credentials are scoped to the resource's original HTTP origin. Redirects to a
different origin use a clean session so custom session authentication headers
and manually supplied cookies cannot be forwarded to another server.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit

import requests

from .resources import DownloadResult, Resource


Progress = Callable[[int, int], None]
CHUNK_SIZE = 64 * 1024


class DownloadError(RuntimeError):
    """A failure suitable for display without signed URLs or session secrets."""


class DownloadCancelled(DownloadError):
    """The requested transfer was cancelled and no export was completed."""


def _origin(url: str) -> tuple[str, str, int]:
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError
        if parts.username or parts.password:
            raise ValueError
        return parts.scheme.lower(), parts.hostname.lower(), parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise DownloadError("下载地址无效，仅支持 HTTP 或 HTTPS。") from None


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled("下载已取消。")


def _expected_size(resource: Resource) -> int:
    try:
        value = int(resource.size or 0)
    except (TypeError, ValueError):
        raise DownloadError("资源大小信息无效。") from None
    return max(value, 0)


def _is_playlist(resource: Resource) -> bool:
    return (urlsplit(resource.url).path.lower().endswith((".m3u8", ".pm3u8"))
            or str(resource.metadata.get("stream_type", "")).lower() == "hls"
            or resource.metadata.get("stream_entry_only") is True)


def _hash_expectations(resource: Resource, source: str) -> list[tuple[str, str]]:
    metadata = resource.metadata
    fields = [("sha256", metadata.get("sha256")), ("md5", metadata.get("md5"))]
    # A cache may contain a transformed preview while the remote URL is an
    # original file. Apply its digest remotely only after identity is verified.
    if source == "cache" or metadata.get("cache_same_original") is True:
        fields.append(("md5", metadata.get("cache_md5")))
    expected = []
    for algorithm, value in fields:
        if value is None or value == "":
            continue
        normalized = str(value).strip().strip('"').lower()
        length = 64 if algorithm == "sha256" else 32
        if not re.fullmatch(r"[0-9a-f]{" + str(length) + r"}", normalized):
            label = "SHA-256" if algorithm == "sha256" else "MD5"
            raise DownloadError(f"资源 {label} 校验信息无效。")
        expected.append((algorithm, normalized))
    return expected


def _verify_hashes(expected: list[tuple[str, str]], sha256: str, md5: str | None,
                   source: str) -> None:
    for algorithm, value in expected:
        actual = sha256 if algorithm == "sha256" else md5
        if actual != value:
            label = "SHA-256" if algorithm == "sha256" else "MD5"
            origin = "缓存" if source == "cache" else "远程原文件"
            raise DownloadError(f"{origin} {label} 校验失败，未完成导出。")


def _filename(resource: Resource, stream: bool = False) -> str:
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", str(resource.name or "未命名资源"))
    name = name.strip(" .") or "未命名资源"
    # Avoid device names on Windows even when the name has an extension.
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", name.split(".")[0]):
        name = "_" + name
    suffix = Path(name).suffix
    if stream:
        name = str(Path(name).with_suffix(".mp4"))
    elif not suffix and resource.url:
        remote_suffix = Path(urlsplit(resource.url).path).suffix
        if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", remote_suffix):
            name += remote_suffix
    # Keep room for collision suffixes and avoid excessively long NTFS names.
    suffix = Path(name).suffix[:16]
    stem = name[:-len(suffix)] if suffix else name
    return stem[:160].rstrip(" .") + suffix


class DownloadManager:
    def __init__(self, output_dir: str | Path, session: requests.Session | None = None):
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.session = session if session is not None else requests.Session()

    def download(self, resource: Resource, progress: Progress | None = None,
                 cancel: threading.Event | None = None) -> DownloadResult:
        _check_cancel(cancel)
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise DownloadError("无法创建导出目录，请检查目录权限。") from None
        remote_error = None
        if resource.url:
            try:
                return self._download_remote(resource, progress, cancel)
            except DownloadCancelled:
                raise
            except DownloadError as error:
                remote_error = error
        _check_cancel(cancel)
        if resource.local_path:
            try:
                return self._download_cache(resource, progress, cancel)
            except DownloadCancelled:
                raise
            except DownloadError as error:
                if remote_error:
                    raise DownloadError(f"{remote_error} 本地缓存无法验证或读取：{error}") from None
                raise
        if remote_error:
            raise remote_error
        raise DownloadError("没有可用的远程地址或已验证的完整缓存。")

    def _request(self, url: str, resource: Resource) -> requests.Response:
        trusted_origin = _origin(resource.url)
        current_url = url
        for _ in range(11):
            trusted = _origin(current_url) == trusted_origin
            client = self.session if trusted else requests.Session()
            if not trusted:
                client.trust_env = self.session.trust_env
            headers = {"Accept-Encoding": "identity"}
            cookies = None
            if trusted:
                supplied_headers = resource.metadata.get("headers", {})
                if isinstance(supplied_headers, dict):
                    headers.update(supplied_headers)
                supplied_cookies = resource.metadata.get("cookies")
                if isinstance(supplied_cookies, dict):
                    cookies = supplied_cookies
            try:
                response = client.get(current_url, headers=headers, cookies=cookies,
                                      stream=True, allow_redirects=False, timeout=(10, 20))
            except (requests.RequestException, ValueError, TypeError):
                if not trusted:
                    client.close()
                raise DownloadError("远程下载失败（连接或请求错误）。") from None
            # Closing a session does not close the streamed response; its
            # response context below owns that connection until transfer ends.
            if not trusted:
                client.close()
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise DownloadError("服务器重定向缺少目标地址。")
                current_url = urljoin(current_url, location)
                continue
            if not 200 <= response.status_code < 300:
                code = response.status_code
                response.close()
                if code in (401, 403):
                    raise DownloadError(f"下载授权已失效或无权限（HTTP {code}），请重新扫描资源。")
                raise DownloadError(f"服务器下载失败（HTTP {code}）。")
            if response.status_code == 206:
                response.close()
                raise DownloadError("服务器只返回了资源片段，无法确认完整文件。")
            return response
        raise DownloadError("服务器重定向次数过多。")

    def _partial(self) -> Path:
        try:
            descriptor, name = tempfile.mkstemp(prefix=".classin-", suffix=".part", dir=self.output_dir)
            os.close(descriptor)
            return Path(name)
        except OSError:
            raise DownloadError("无法写入导出目录，请检查目录权限或磁盘空间。") from None

    def _commit(self, partial: Path, resource: Resource, source: str, size: int,
                digest: str, cancel: threading.Event | None, stream: bool = False) -> DownloadResult:
        name = _filename(resource, stream)
        base = Path(name)
        for index in range(10000):
            _check_cancel(cancel)
            candidate = self.output_dir / (name if index == 0 else f"{base.stem} ({index}){base.suffix}")
            try:
                # Hard-link publication is atomic and refuses to replace any
                # existing file, including when downloads finish concurrently.
                try:
                    os.link(partial, candidate)
                except OSError as error:
                    unsupported = (error.errno in (errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP)
                                   or getattr(error, "winerror", None) in (1, 50))
                    if sys.platform != "win32" or not unsupported:
                        raise
                    # FAT/exFAT cannot publish hard links. Windows rename in
                    # this same directory is atomic and refuses an existing
                    # target; POSIX rename would overwrite it and is excluded.
                    os.rename(partial, candidate)
            except FileExistsError:
                continue
            except OSError:
                raise DownloadError("无法完成导出，请检查目录权限或磁盘空间。") from None
            partial.unlink(missing_ok=True)
            return DownloadResult(resource.id, str(candidate), source, size, digest)
        raise DownloadError("同名文件过多，请更换导出目录或资源名称。")

    def _download_remote(self, resource: Resource, progress: Progress | None,
                         cancel: threading.Event | None) -> DownloadResult:
        if _is_playlist(resource):
            return self._download_hls(resource, progress, cancel)
        partial = self._partial()
        try:
            with self._request(resource.url, resource) as response:
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type in ("application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl", "audio/x-mpegurl"):
                    response.close()
                    partial.unlink(missing_ok=True)
                    return self._download_hls(resource, progress, cancel)
                expected = _expected_size(resource)
                try:
                    content_length = int(response.headers.get("Content-Length", "0"))
                except ValueError:
                    raise DownloadError("服务器返回了无效的文件长度。") from None
                encoded = response.headers.get("Content-Encoding", "identity").lower() != "identity"
                header_size = 0 if encoded else max(content_length, 0)
                total = expected or header_size
                done = 0
                digest = hashlib.sha256()
                hashes = _hash_expectations(resource, "remote")
                md5 = hashlib.md5(usedforsecurity=False) if any(key == "md5" for key, _ in hashes) else None
                if progress:
                    progress(0, total)
                with partial.open("wb") as output:
                    for chunk in response.iter_content(CHUNK_SIZE):
                        _check_cancel(cancel)
                        if not chunk:
                            continue
                        output.write(chunk)
                        digest.update(chunk)
                        if md5 is not None:
                            md5.update(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
                    output.flush()
                    os.fsync(output.fileno())
                _check_cancel(cancel)
                if not done or (expected and done != expected) or (header_size and done != header_size):
                    raise DownloadError("文件长度不符，下载未完成。")
                _verify_hashes(hashes, digest.hexdigest(), md5.hexdigest() if md5 is not None else None, "remote")
                return self._commit(partial, resource, "remote", done, digest.hexdigest(), cancel)
        except DownloadError:
            raise
        except requests.RequestException:
            raise DownloadError("远程传输中断，下载未完成。") from None
        except OSError:
            raise DownloadError("无法写入导出文件，请检查磁盘空间或目录权限。") from None
        finally:
            partial.unlink(missing_ok=True)

    def _download_cache(self, resource: Resource, progress: Progress | None,
                        cancel: threading.Event | None) -> DownloadResult:
        if _is_playlist(resource):
            raise DownloadError("本地仅有回放播放列表，视频片段未验证，无法作为完整视频导出。")
        cache = Path(resource.local_path)
        expected = _expected_size(resource)
        try:
            with cache.open("rb") as original:
                stat = os.fstat(original.fileno())
                metadata = resource.metadata
                compressed = metadata.get("cache_qt_compressed") is True
                sliced = "cache_offset" in metadata or "cache_length" in metadata
                offset = 0
                payload = None
                if compressed or sliced:
                    if metadata.get("cache_complete") is not True:
                        raise DownloadError("Qt 缓存未验证为完整原文件。")
                if compressed:
                    # Read the Qt container lazily: only the validated,
                    # decompressed HTTP body is eligible for export.
                    try:
                        from .cache import read_qt_cache
                        entry = read_qt_cache(cache)
                    except Exception:
                        raise DownloadError("无法解析或解压 Qt 缓存正文。") from None
                    if not entry.compressed or not entry.complete or not isinstance(entry.payload, bytes):
                        raise DownloadError("Qt 压缩缓存没有可验证的完整正文。")
                    payload = memoryview(entry.payload)
                    size = len(payload)
                    if size != entry.body_size:
                        raise DownloadError("Qt 缓存解压后的正文长度不符。")
                elif sliced:
                    offset = metadata.get("cache_offset")
                    size = metadata.get("cache_length")
                    if (type(offset) is not int or type(size) is not int or offset < 0 or size <= 0
                            or offset + size != stat.st_size):
                        raise DownloadError("Qt 缓存正文范围无效或长度不完整。")
                    original.seek(offset)
                else:
                    size = stat.st_size
                if not size:
                    raise DownloadError("缓存为空。")
                if expected and size != expected:
                    raise DownloadError("缓存长度与资源原始大小不符。")
                if not expected and resource.metadata.get("cache_complete") is not True:
                    raise DownloadError("缓存未标记完整，也没有可核对的原始大小。")
                # A signed stream endpoint may have no extension or stream
                # metadata. Its cached manifest still cannot stand in for the
                # media, even when the manifest itself is complete.
                if payload is not None:
                    prefix = bytes(payload[:32])
                else:
                    prefix = original.read(32)
                    original.seek(offset)
                if prefix.removeprefix(b"\xef\xbb\xbf").lstrip().startswith(b"#EXTM3U"):
                    raise DownloadError("本地仅有回放播放列表，视频片段未验证，无法作为完整视频导出。")
                partial = self._partial()
                try:
                    done = 0
                    digest = hashlib.sha256()
                    hashes = _hash_expectations(resource, "cache")
                    md5 = hashlib.md5(usedforsecurity=False) if any(key == "md5" for key, _ in hashes) else None
                    if progress:
                        progress(0, size)
                    with partial.open("wb") as output:
                        while done < size:
                            _check_cancel(cancel)
                            length = min(CHUNK_SIZE, size - done)
                            chunk = payload[done:done + length] if payload is not None else original.read(length)
                            if not chunk:
                                break
                            output.write(chunk)
                            digest.update(chunk)
                            if md5 is not None:
                                md5.update(chunk)
                            done += len(chunk)
                            if progress:
                                progress(done, size)
                        output.flush()
                        os.fsync(output.fileno())
                    _check_cancel(cancel)
                    after = os.fstat(original.fileno())
                    current = cache.stat()
                    original_signature = (stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino)
                    after_signature = (after.st_size, after.st_mtime_ns, after.st_dev, after.st_ino)
                    current_signature = (current.st_size, current.st_mtime_ns, current.st_dev, current.st_ino)
                    if done != size or after_signature != original_signature or current_signature != original_signature:
                        raise DownloadError("缓存仍在变化，无法确认完整文件。")
                    _verify_hashes(hashes, digest.hexdigest(), md5.hexdigest() if md5 is not None else None, "cache")
                    return self._commit(partial, resource, "cache", done, digest.hexdigest(), cancel)
                finally:
                    partial.unlink(missing_ok=True)
        except DownloadError:
            raise
        except OSError:
            raise DownloadError("无法读取缓存或写入导出文件。") from None

    def _download_hls(self, resource: Resource, progress: Progress | None,
                      cancel: threading.Event | None) -> DownloadResult:
        executable = shutil.which("ffmpeg")
        if executable is None:
            try:
                import imageio_ffmpeg
                executable = imageio_ffmpeg.get_ffmpeg_exe()
            except (ImportError, RuntimeError, OSError):
                raise DownloadError("回放流需要 FFmpeg，请安装项目的可选 imageio-ffmpeg 依赖。") from None
        partial = self._partial()
        try:
            with tempfile.TemporaryDirectory(prefix=".classin-hls-", dir=self.output_dir) as working:
                bundle = _HlsBundle(self, resource, Path(working), progress, cancel)
                playlist = bundle.playlist(resource.url)
                _check_cancel(cancel)
                # FFmpeg sees only downloaded local files. It cannot forward
                # cookies/headers to segment hosts or follow network redirects.
                # Use argv rather than a shell and keep stderr private: it may
                # otherwise include sensitive playlist names or URL values.
                command = [
                    executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                    "-protocol_whitelist", "file,crypto,data", "-allowed_extensions", "ALL",
                    "-i", str(playlist), "-map", "0:v:0?", "-map", "0:a:0?",
                    "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(partial),
                ]
                with tempfile.TemporaryFile() as diagnostics:
                    process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                               stdout=subprocess.DEVNULL, stderr=diagnostics,
                                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    try:
                        while True:
                            _check_cancel(cancel)
                            try:
                                code = process.wait(timeout=0.1)
                                break
                            except subprocess.TimeoutExpired:
                                continue
                    finally:
                        if process.poll() is None:
                            process.terminate()
                            try:
                                process.wait(timeout=3)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait()
                    if code != 0:
                        raise DownloadError("FFmpeg 无法合并回放流，可能是不支持的编码或加密格式。")
                _check_cancel(cancel)
                digest = hashlib.sha256()
                size = 0
                with partial.open("rb") as merged:
                    while chunk := merged.read(CHUNK_SIZE):
                        _check_cancel(cancel)
                        digest.update(chunk)
                        size += len(chunk)
                if not size:
                    raise DownloadError("回放流合并后为空，未完成导出。")
                return self._commit(partial, resource, "remote", size, digest.hexdigest(), cancel, stream=True)
        except DownloadError:
            raise
        except requests.RequestException:
            raise DownloadError("回放流传输中断，未完成导出。") from None
        except (OSError, ValueError):
            raise DownloadError("无法保存或合并回放流，请检查 FFmpeg 和导出目录。") from None
        finally:
            partial.unlink(missing_ok=True)


class _HlsBundle:
    """Materialize one highest-bandwidth VOD variant and its media/key files."""

    def __init__(self, manager: DownloadManager, resource: Resource, root: Path,
                 progress: Progress | None, cancel: threading.Event | None):
        self.manager = manager
        self.resource = resource
        self.root = root
        self.progress = progress
        self.cancel = cancel
        self.done = 0
        self.files: dict[str, Path] = {}
        self.active: set[str] = set()

    def _report(self, length: int) -> None:
        self.done += length
        if self.progress:
            # HLS playlist duration is not an original byte size. A zero total
            # keeps the GUI progress indeterminate while reporting real bytes.
            self.progress(self.done, 0)

    def _path(self, url: str, playlist: bool = False) -> Path:
        suffix = ".m3u8" if playlist else Path(urlsplit(url).path).suffix
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix):
            suffix = ".bin"
        return self.root / f"asset-{len(self.files):05d}{suffix}"

    def _asset(self, url: str) -> Path:
        _check_cancel(self.cancel)
        _origin(url)  # Remote manifests must never open local file references.
        if url in self.files:
            return self.files[url]
        path = self._path(url)
        self.files[url] = path
        with self.manager._request(url, self.resource) as response, path.open("wb") as output:
            try:
                expected = int(response.headers.get("Content-Length", "0"))
            except ValueError:
                raise DownloadError("回放片段长度信息无效。") from None
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                expected = 0
            written = 0
            for chunk in response.iter_content(CHUNK_SIZE):
                _check_cancel(self.cancel)
                if chunk:
                    output.write(chunk)
                    written += len(chunk)
                    self._report(len(chunk))
            _check_cancel(self.cancel)
            if not written or (expected and written != expected):
                raise DownloadError("回放片段下载不完整。")
        return path

    @staticmethod
    def _attribute(line: str, name: str) -> str:
        match = re.search(r'(?:[:,])' + re.escape(name) + r'=(?:"([^"]*)"|([^,]*))', line)
        return (match.group(1) if match.group(1) is not None else match.group(2)) if match else ""

    def _select_variant(self, lines: list[str]) -> list[str]:
        variants = []
        for index, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF:"):
                if index + 1 >= len(lines) or lines[index + 1].startswith("#"):
                    raise DownloadError("回放主播放列表缺少视频地址。")
                try:
                    bandwidth = int(self._attribute(line, "BANDWIDTH") or "0")
                except ValueError:
                    bandwidth = 0
                variants.append((bandwidth, line, lines[index + 1]))
        if not variants:
            return lines
        _, variant, uri = max(variants, key=lambda item: item[0])
        audio_group = self._attribute(variant, "AUDIO")
        selected = ["#EXTM3U"]
        for line in lines:
            if line.startswith(("#EXT-X-VERSION:", "#EXT-X-INDEPENDENT-SEGMENTS", "#EXT-X-SESSION-KEY:")):
                selected.append(line)
            elif (line.startswith("#EXT-X-MEDIA:") and self._attribute(line, "TYPE") == "AUDIO"
                  and self._attribute(line, "GROUP-ID") == audio_group):
                selected.append(line)
        selected.extend([variant, uri])
        return selected

    def playlist(self, url: str, depth: int = 0) -> Path:
        _check_cancel(self.cancel)
        _origin(url)
        if url in self.active or depth > 8:
            raise DownloadError("回放播放列表嵌套过多或包含循环引用。")
        if url in self.files:
            return self.files[url]
        path = self._path(url, playlist=True)
        self.files[url] = path
        self.active.add(url)
        with self.manager._request(url, self.resource) as response:
            content = bytearray()
            resolved_url = response.url
            for chunk in response.iter_content(CHUNK_SIZE):
                _check_cancel(self.cancel)
                content.extend(chunk)
                if len(content) > 4 * 1024 * 1024:
                    raise DownloadError("回放播放列表过大。")
                self._report(len(chunk))
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise DownloadError("回放播放列表不是有效的 UTF-8 文本。") from None
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines or lines[0] != "#EXTM3U":
            raise DownloadError("服务器未返回有效的 HLS 回放播放列表。")
        master = any(line.startswith("#EXT-X-STREAM-INF:") for line in lines)
        if not master and "#EXT-X-ENDLIST" not in lines:
            raise DownloadError("回放流尚未结束或属于直播，无法验证完整导出。")
        lines = self._select_variant(lines)
        local_lines = []
        next_is_playlist = False
        for line in lines:
            _check_cancel(self.cancel)
            if line.startswith("#"):
                tag = line.split(":", 1)[0]

                def replace_uri(match: re.Match) -> str:
                    remote = urljoin(resolved_url, match.group(1))
                    if tag in ("#EXT-X-MEDIA", "#EXT-X-I-FRAME-STREAM-INF"):
                        local = self.playlist(remote, depth + 1)
                    else:
                        local = self._asset(remote)
                    return f'URI="{local.name}"'

                line = re.sub(r'URI="([^"]*)"', replace_uri, line)
                local_lines.append(line)
                next_is_playlist = tag == "#EXT-X-STREAM-INF"
            else:
                remote = urljoin(resolved_url, line)
                local = self.playlist(remote, depth + 1) if next_is_playlist else self._asset(remote)
                local_lines.append(local.name)
                next_is_playlist = False
        path.write_text("\n".join(local_lines) + "\n", encoding="utf-8")
        self.active.remove(url)
        return path
