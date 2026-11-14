"""Current ClassIn native login state and its verified HTTP signing protocol.

The credential source is the running ClassIn main process. The browser Cookies
and cookie.data files hold browser statistics and UI preferences in this client
version, so neither is treated as proof of a current login. No process writes,
injection, memory dumps, password recovery, or credential files are used.
"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import struct
import threading
import time
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlsplit
import uuid

import requests


class SessionError(RuntimeError):
    """A login/session operation failed; messages never include credentials."""


def parse_cookie_archive(data: bytes, *, max_entries: int = 100_000) -> dict[str, str]:
    """Decode CookieManager's cereal binary string map (UI preferences).

    Each count and string byte length is a native little-endian uint64. This is
    a bounded structural parser, not a login-token extractor.
    """
    offset = 0

    def size() -> int:
        nonlocal offset
        if offset + 8 > len(data):
            raise SessionError("ClassIn 偏好文件不完整。")
        result = struct.unpack_from("<Q", data, offset)[0]
        offset += 8
        return result

    def string() -> str:
        nonlocal offset
        length = size()
        if length > len(data) - offset:
            raise SessionError("ClassIn 偏好文件的字段长度无效。")
        value = data[offset : offset + length]
        offset += length
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            raise SessionError("ClassIn 偏好文件的文本编码无效。") from None

    count = size()
    if count > max_entries:
        raise SessionError("ClassIn 偏好文件条目过多。")
    result = {}
    for _ in range(count):
        key, value = string(), string()
        if key in result:
            raise SessionError("ClassIn 偏好文件包含重复字段。")
        result[key] = value
    if offset != len(data):
        raise SessionError("ClassIn 偏好文件包含无法识别的尾部数据。")
    return result


def _qt_string(value: Any) -> str:
    """QJsonValue -> QVariant -> QString for scalar API fields."""
    if value is None or isinstance(value, (list, dict)):
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SessionError("请求参数包含无效数字。")
        return format(value, ".15g")
    return str(value)


def _qt_key(key: str) -> bytes:
    # QString compares UTF-16 code units, including supplementary characters.
    return key.encode("utf-16-be", errors="surrogatepass")


def generate_form_sign(fields: Mapping[str, Any], timestamp: int, secret: str) -> str:
    """EeoNetworkConfig::generateUrlEncodeSign -> generateSign (MD5).

    Native code percent-encodes query items and decodes values once before
    signing, which preserves each original scalar value. Values larger than
    1024 UTF-8 bytes are omitted; 'key' and 'timeStamp' are replaced.
    """
    values = {
        str(k): _qt_string(v)
        for k, v in fields.items()
        if str(k) not in {"key", "timeStamp"}
    }
    values["timeStamp"] = str(int(timestamp))
    values = {k: v for k, v in values.items() if len(v.encode("utf-8")) <= 1024}
    message = "&".join(f"{k}={values[k]}" for k in sorted(values, key=_qt_key))
    # generateSign collects sorted QMap fields into a QVector<QPair>, then
    # appends the secret pair. The secret is deliberately the final pair.
    message += ("&" if message else "") + "key=" + secret
    return hashlib.md5(message.encode("utf-8")).hexdigest()


def generate_json_sign(body: bytes, timestamp: int) -> str:
    """Native JSON sign: insert timestamp at timestamp % 10, then MD5."""
    stamp = str(int(timestamp)).encode("ascii")
    split = int(timestamp) % 10
    material = body[:split] + stamp + body[split:] if len(body) > split else body + stamp
    return hashlib.md5(material).hexdigest()


def _json_bytes(value: Any) -> bytes:
    # QJsonDocument::Compact writes object keys in QString sort order.
    def ordered(item: Any) -> Any:
        if isinstance(item, dict):
            return {k: ordered(item[k]) for k in sorted(item, key=_qt_key)}
        if isinstance(item, list):
            return [ordered(child) for child in item]
        return item

    try:
        return json.dumps(ordered(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise SessionError("请求参数无法编码为 JSON。") from None


def _token_expiry(token: str) -> int:
    try:
        payload = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==="))
        return int(payload.get("exp", 0))
    except (ValueError, TypeError, IndexError, UnicodeError):
        return 0


def _origin(host: str) -> str:
    value = host.strip()
    if "://" not in value:
        value = "https://" + value
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise SessionError("ClassIn 当前服务地址无效。")
    if parsed.query or parsed.fragment:
        raise SessionError("ClassIn 当前服务地址无效。")
    return value.rstrip("/")


@dataclass(repr=False)
class _Snapshot:
    uid: int
    dynamic_host: str
    static_host: str
    secret: str
    telephone: str = ""
    token: str = ""
    refresh_token: str = ""
    token_expiry: int = 0
    device_id: str = ""
    device_map: str = ""
    client_version: str = ""
    cache_directory: Path | None = None


@dataclass(repr=False)
class ClassInSession:
    uid: int
    dynamic_host: str
    static_host: str
    telephone: str = field(default="", repr=False)
    cache_directory: Path | None = field(default=None, repr=False)
    requests_session: requests.Session = field(default_factory=requests.Session, repr=False)
    _secret: str = field(default="", repr=False)
    _token: str = field(default="", repr=False)
    _refresh_token: str = field(default="", repr=False)
    _token_expires: int = field(default=0, repr=False)
    _device_id: str = field(default="", repr=False)
    _device_map: str = field(default="", repr=False)
    _client_version: str = field(default="", repr=False)
    _snapshot_loader: Callable[[], _Snapshot] | None = field(default=None, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _transport_factory: Callable[[], requests.Session] | None = field(default=None, repr=False)
    _local: threading.local = field(default_factory=threading.local, repr=False)
    _transports: list[Any] = field(default_factory=list, repr=False)
    _closed: bool = field(default=False, repr=False)
    _refreshing: bool = field(default=False, repr=False)
    _generation: int = field(default=0, repr=False)
    _refresh_condition: threading.Condition = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._refresh_condition = threading.Condition(self._lock)
        self._local.transport = self.requests_session
        self._transports.append(self.requests_session)

    @property
    def session(self) -> requests.Session:
        """The current thread's independent HTTP connection pool."""
        with self._lock:
            if self._closed:
                raise SessionError("ClassIn 会话已关闭，请重新扫描。")
            transport = getattr(self._local, "transport", None)
            if transport is None:
                # Do not clone cookies, auth or global credential headers from
                # another thread's transport. API headers remain request-local.
                transport = (self._transport_factory or requests.Session)()
                self._local.transport = transport
                self._transports.append(transport)
            return transport

    def release_thread_transport(self) -> None:
        """Release this task's HTTP pool while retaining the constructor pool.

        Call from the owning worker thread after its task has stopped using the
        transport. Claiming registry ownership under the state lock prevents a
        concurrent whole-session close from closing the same pool twice.
        """
        with self._lock:
            transport = getattr(self._local, "transport", None)
            if transport is None or transport is self.requests_session:
                return
            del self._local.transport
            for index, registered in enumerate(self._transports):
                if registered is transport:
                    self._transports.pop(index)
                    break
            else:
                # Whole-session close has already claimed this transport.
                return
        try:
            transport.close()
        except Exception:
            raise SessionError("ClassIn 网络连接清理失败。") from None

    def __repr__(self) -> str:
        return "ClassInSession(current_login=True, credentials=<redacted>)"

    def diagnostics(self) -> dict[str, Any]:
        return {"credential_source": "ClassIn current native process", "uid_available": bool(self.uid),
                "signing_secret_available": bool(self._secret), "token_available": bool(self._token),
                "token_expired": bool(self._token) and self._token_expires <= int(time.time()),
                "telephone_available": bool(self.telephone)}

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            transports, self._transports = self._transports, []
            self._secret = self._token = self._refresh_token = self.telephone = ""
            self._device_id = self._device_map = ""
            self._snapshot_loader = None
            self._refresh_condition.notify_all()
        cleanup_failed = False
        for transport in transports:
            try:
                transport.close()
            except Exception:
                cleanup_failed = True
        if cleanup_failed:
            raise SessionError("ClassIn 网络连接清理失败。") from None

    def format_static_url(self, path: str) -> str:
        """Resolve an original-resource path against the current static CDN.

        EeoHttpManager::formatStaticUrl preserves HTTP URLs and otherwise
        concatenates the CDN and path. This method validates URL structure
        before matching that behavior; it never applies API authentication.
        """
        if not isinstance(path, str) or not path or path != path.strip() or "\\" in path:
            raise SessionError("ClassIn 下载地址无效。")
        if any(ord(char) < 32 or ord(char) == 127 for char in path):
            raise SessionError("ClassIn 下载地址无效。")
        try:
            parsed = urlsplit(path)
        except ValueError:
            raise SessionError("ClassIn 下载地址无效。") from None
        if parsed.scheme:
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise SessionError("ClassIn 下载地址无效。")
            try:
                parsed.port
            except ValueError:
                raise SessionError("ClassIn 下载地址无效。") from None
            return path
        if parsed.netloc or path.startswith("//") or not parsed.path:
            raise SessionError("ClassIn 下载地址无效。")
        with self._lock:
            static_host = self.static_host
        separator = "" if path.startswith("/") or static_host.endswith("/") else "/"
        return static_host + separator + path

    def _check_login(self) -> None:
        with self._lock:
            if self._closed:
                raise SessionError("ClassIn 会话已关闭，请重新扫描。")
            self._read_login()

    def _read_login(self) -> None:
        if self._snapshot_loader is None:
            return
        current = self._snapshot_loader()
        if current.uid != self.uid:
            raise SessionError("ClassIn 当前账号已切换，请重新扫描。")
        if current.secret != self._secret:
            self._generation += 1
            self._token = self._refresh_token = ""
            self._token_expires = 0
        self._secret = current.secret
        self.telephone = current.telephone
        self.dynamic_host, self.static_host = current.dynamic_host, current.static_host
        self._device_id, self._device_map = current.device_id, current.device_map
        self._client_version = current.client_version
        self.cache_directory = current.cache_directory
        # Keep a token refreshed by this exporter when it expires later than
        # ClassIn's cached token; all remain memory only and for the same uid.
        if current.token and current.token_expiry >= self._token_expires:
            self._token, self._refresh_token = current.token, current.refresh_token
            self._token_expires = current.token_expiry

    def _url(self, path: str) -> str:
        base = urlsplit(self.dynamic_host)
        given = urlsplit(path)
        if given.scheme or given.netloc:
            if (given.scheme, given.hostname, given.port) != (base.scheme, base.hostname, base.port):
                raise SessionError("认证请求必须发往当前 ClassIn 服务。")
            return path
        if path.startswith("//") or "\\" in path:
            raise SessionError("ClassIn 请求路径无效。")
        return self.dynamic_host.rstrip("/") + "/" + path.lstrip("/")

    def _headers(self, signature: str, timestamp: int, *, json_body: bool, token_request: bool = False) -> dict[str, str]:
        headers = {"X-EEO-SIGN": signature, "X-EEO-TS": str(timestamp), "X-APP-ID": "classin",
                   "X-EEO-CLIENT-TYPE": "9", "X-EEO-CLIENT-OS-FLAG": "3",
                   "X-EEO-CLIENT-LANGUAGE": "zh_CN"}
        device_parts = dict(item.split("=", 1) for item in self._device_map.split(",") if "=" in item)
        if self._device_map:
            headers["X-EEO-DEVICE-MAP"] = self._device_map
        if self._device_id:
            headers["X-EEO-DEVICE-ID"] = self._device_id
            headers["X-EEO-CLIENT-FLAG"] = self._device_id
        if self._client_version or device_parts.get("version"):
            headers["X-EEO-CLIENT-VER"] = self._client_version or device_parts["version"]
        for key, header in [("brand", "X-EEO-CLIENT-BRAND"), ("model", "X-EEO-CLIENT-MODEL")]:
            if device_parts.get(key):
                headers[header] = device_parts[key]
        if json_body:
            headers["X-EEO-SIGN-VERSION"] = "1"
        if not token_request:
            headers["X-EEO-UID"] = str(self.uid)
            if json_body and self._token:
                headers["X-EEO-TOKEN"] = self._token
        return headers

    def _send(self, url: str, body: bytes, headers: dict[str, str], timeout: float) -> dict[str, Any]:
        # Credentials are request-local. The downloader uses the same clean
        # Session without sending API authentication headers to CDN origins.
        try:
            response = self.session.post(url, data=body, headers=headers, timeout=timeout,
                                         allow_redirects=False)
            if response.status_code in {401, 403}:
                raise SessionError("ClassIn 登录状态被服务端拒绝，请在客户端重新登录后扫描。")
            if not 200 <= response.status_code < 300:
                raise SessionError(f"ClassIn 服务返回 HTTP {response.status_code}。")
            result = response.json()
            if not isinstance(result, dict):
                raise SessionError("ClassIn 服务返回的数据格式无法识别。")
            return result
        except requests.RequestException:
            raise SessionError("无法连接 ClassIn 服务，请检查网络后重试。") from None
        except (ValueError, UnicodeError):
            raise SessionError("ClassIn 服务返回的数据无法解析。") from None

    def post_form(self, path: str, fields: Mapping[str, Any], *, timeout: float = 30) -> dict[str, Any]:
        with self._lock:
            self._check_login()
            url = self._url(path)
            values = {str(k): _qt_string(v) for k, v in fields.items()}
            timestamp = int(time.time())
            signature = generate_form_sign(values, timestamp, self._secret)
            body = "&".join(f"{quote(k, safe='')}={quote(values[k], safe='')}" for k in sorted(values, key=_qt_key)).encode("ascii")
            headers = self._headers(signature, timestamp, json_body=False)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        result = self._send(url, body, headers, timeout)
        self._check_login()
        return result

    def post_json(self, path: str, payload: Mapping[str, Any], *, timeout: float = 30) -> dict[str, Any]:
        body = _json_bytes(dict(payload))
        while True:
            with self._lock:
                self._check_login()
                url = self._url(path)
                timestamp = int(time.time())
                if self._token and self._token_expires > timestamp + 30:
                    headers = self._headers(generate_json_sign(body, timestamp), timestamp, json_body=True)
                    headers["Content-Type"] = "application/json"
                    break
            self._ensure_token(timeout)
        result = self._send(url, body, headers, timeout)
        self._check_login()
        return result

    def _ensure_token(self, timeout: float) -> None:
        with self._refresh_condition:
            while True:
                self._check_login()
                now = int(time.time())
                if self._token and self._token_expires > now + 30:
                    return
                if not self._refreshing:
                    break
                self._refresh_condition.wait()
            if not self._secret or not self.uid:
                raise SessionError("ClassIn 登录状态不完整。")
            if self._refresh_token:
                path = "/usercenter/v2/app/user/refresh-token"
                payload = {"uid": self.uid, "refreshToken": self._refresh_token}
            else:
                path = "/zero-usercenter/app/user/tokenExchange"
                payload = {"uid": self.uid, "token": _make_exchange_token(self.uid, self._secret, now)}
            url = self._url(path)
            generation = self._generation
            body = _json_bytes(payload)
            headers = self._headers(generate_json_sign(body, now), now, json_body=True, token_request=True)
            headers["Content-Type"] = "application/json"
            self._refreshing = True
        try:
            result = self._send(url, body, headers, timeout)
            data = result.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not data["token"]:
                raise SessionError("无法刷新 ClassIn 访问令牌，请在客户端重新登录后扫描。")
            expiry = _token_expiry(data["token"]) or int(data.get("expTime") or now + int(data.get("expires") or 0))
            with self._lock:
                self._check_login()
                if generation != self._generation:
                    raise SessionError("ClassIn 登录状态在刷新期间发生变化，请重新扫描。")
                if expiry <= int(time.time()) + 30:
                    raise SessionError("ClassIn 刷新令牌到期，请重新登录后扫描。")
                self._token = data["token"]
                self._refresh_token = data.get("refreshToken", "")
                self._token_expires = expiry
        finally:
            with self._refresh_condition:
                self._refreshing = False
                self._refresh_condition.notify_all()


def _make_exchange_token(uid: int, secret: str, now: int) -> str:
    """Verified AES-128-CBC + HS256 native tokenExchange envelope."""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = secret.encode("utf-8")[:16].ljust(16, b"\0")
    iv = secret[::-1].encode("utf-8")[:16].ljust(16, b"\0")
    padder = padding.PKCS7(128).padder()
    text = str(uid).encode("ascii")
    padded = padder.update(text) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    encrypted = cipher.update(padded) + cipher.finalize()
    payload = {"exp": now + 1800, "iat": now - 1800, "nbf": now - 1800, "jti": str(uuid.uuid4()),
               "data": {"token": base64.b64encode(encrypted).decode("ascii")}}

    def encoded(item: Any) -> bytes:
        return base64.urlsafe_b64encode(_json_bytes(item)).rstrip(b"=")

    signing = encoded({"alg": "HS256", "typ": "JWT"}) + b"." + encoded(payload)
    signature = base64.urlsafe_b64encode(hmac.new(secret.encode("utf-8"), signing, hashlib.sha256).digest()).rstrip(b"=")
    return (signing + b"." + signature).decode("ascii")


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


class _ModuleEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("GlblcntUsage", wintypes.DWORD), ("ProccntUsage", wintypes.DWORD),
                ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", wintypes.DWORD),
                ("hModule", ctypes.c_void_p), ("szModule", wintypes.WCHAR * 256), ("szExePath", wintypes.WCHAR * 260)]


def _kernel() -> Any:
    if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise SessionError("当前登录状态读取需要 64 位 Windows 和运行中的 ClassIn。")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    api.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    api.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    api.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ModuleEntry)]
    api.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ModuleEntry)]
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    return api


def _processes(api: Any) -> list[int]:
    handle = api.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise SessionError("无法读取当前 ClassIn 进程列表。")
    item = _ProcessEntry()
    item.dwSize = ctypes.sizeof(item)
    result = []
    try:
        ok = api.Process32FirstW(handle, ctypes.byref(item))
        while ok:
            if item.szExeFile.casefold() == "classin.exe":
                result.append(item.th32ProcessID)
            ok = api.Process32NextW(handle, ctypes.byref(item))
    finally:
        api.CloseHandle(handle)
    return result


def _modules(api: Any, pid: int) -> dict[str, tuple[int, int, Path]]:
    handle = api.CreateToolhelp32Snapshot(0x18, pid)
    if handle == ctypes.c_void_p(-1).value:
        raise SessionError("无法读取当前 ClassIn 的模块。")
    item = _ModuleEntry()
    item.dwSize = ctypes.sizeof(item)
    result = {}
    try:
        ok = api.Module32FirstW(handle, ctypes.byref(item))
        while ok:
            result[item.szModule.casefold()] = (item.modBaseAddr, item.modBaseSize, Path(item.szExePath))
            ok = api.Module32NextW(handle, ctypes.byref(item))
    finally:
        api.CloseHandle(handle)
    return result


class _Reader:
    def __init__(self, api: Any, pid: int):
        self.api = api
        self.handle = api.OpenProcess(0x1010, False, pid)  # QUERY_LIMITED_INFORMATION | VM_READ
        if not self.handle:
            raise SessionError("无法读取 ClassIn 当前登录状态，请使用与 ClassIn 相同的用户权限运行。")

    def close(self) -> None:
        self.api.CloseHandle(self.handle)

    def read(self, address: int, size: int) -> bytes:
        if not address or not 0 <= size <= 1024 * 1024:
            raise SessionError("ClassIn 登录对象格式无法识别。")
        buffer = ctypes.create_string_buffer(size)
        actual = ctypes.c_size_t()
        if not self.api.ReadProcessMemory(self.handle, address, buffer, size, ctypes.byref(actual)) or actual.value != size:
            raise SessionError("ClassIn 登录已失效或客户端已关闭，请重新扫描。")
        return buffer.raw

    def qword(self, address: int) -> int:
        return struct.unpack("<Q", self.read(address, 8))[0]

    def qstring(self, address: int) -> str:
        pointer = self.qword(address)
        _, size, _, offset = struct.unpack("<iiQq", self.read(pointer, 24))
        if not 0 <= size <= 32768 or not 0 <= offset <= 1024 * 1024:
            raise SessionError("ClassIn 登录对象的文本格式无法识别。")
        try:
            return self.read(pointer + offset, size * 2).decode("utf-16-le")
        except UnicodeError:
            raise SessionError("ClassIn 登录对象的文本格式无法识别。") from None


class _NativeSource:
    """Derive getter paths from installed code; read only those exact fields."""
    def __init__(self, api: Any, pid: int, modules: dict[str, tuple[int, int, Path]]):
        try:
            import capstone
            import pefile
        except ImportError:
            raise SessionError("缺少当前登录状态读取依赖，请安装项目 requirements.txt 后重试。") from None

        self.api, self.pid, self.modules = api, pid, modules
        self.md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
        self.md.detail = True
        self.core_pe = pefile.PE(modules["eeocoreengine.dll"][2])
        self.base_pe = pefile.PE(modules["eeobase.dll"][2])
        if self.core_pe.FILE_HEADER.Machine != 0x8664 or self.base_pe.FILE_HEADER.Machine != 0x8664:
            raise SessionError("暂不支持当前 ClassIn 版本客户端结构。")
        self.core_global = self._returned_global(self.core_pe, self._export(self.core_pe, "?instance@CoreEngine"), "mov")
        base_token = self._code(self.base_pe, self._export(self.base_pe, "?token@EeoNetworkConfig"))
        calls = self._direct_calls(base_token)
        if len(calls) < 2:
            raise SessionError("暂不支持当前 ClassIn 令牌结构。")
        self.token_global = self._returned_global(self.base_pe, calls[0], "lea")
        self.token_offset = self._add_rax(self._code(self.base_pe, calls[1]))
        machine_code = self._code(self.base_pe, self._export(self.base_pe, "?getMachineUniqueFlag@EeoBaseUtils"), 4096)
        # The final RIP-relative LEA passed to the QString copy constructor is
        # the cached device identifier returned by this exported getter.
        self.device_global = self._rip_lea_rdx(machine_code)[-1]
        self.cache_global = self._cache_directory_global()
        core_base = modules["eeocoreengine.dll"][0]
        reader = _Reader(api, pid)
        try:
            core = reader.qword(core_base + self.core_global)
            if not core:
                raise SessionError("ClassIn 尚未建立登录对象，请登录后重试。")
            table = reader.qword(core)
            uid_code = self._vcode(reader, table, 240)
            self.uid_offset = self._rax_deref(uid_code)[-1]
            self.secret_offset = self._add_rax(self._vcode(reader, table, 352))
            self.telephone_offset = self._add_rax(self._vcode(reader, table, 296))
            dynamic_code = self._vcode(reader, table, 168)
            # CoreEngine's current static URL callback forwards to the named
            # LoginObject getter. Slot 176 is a reserve CDN *name*, not a URL.
            static_code = self._vcode(reader, table, 152)
            self.config_offset = self._rax_deref(dynamic_code)[-1]
            if self._rax_deref(static_code)[-1] != self.config_offset:
                raise SessionError("暂不支持当前 ClassIn 服务配置结构。")
            dynamic_inner = self._code(self.core_pe, self._direct_calls(dynamic_code)[0])
            static_target = self._direct_calls(static_code)[0]
            if static_target != self._export(self.core_pe, "?cdnStaticUrl@LoginObject"):
                raise SessionError("暂不支持当前 ClassIn 下载服务结构。")
            static_inner = self._code(self.core_pe, static_target)
            self.settings_offset = self._rax_deref(dynamic_inner)[-1]
            if self._rax_deref(static_inner)[-1] != self.settings_offset:
                raise SessionError("暂不支持当前 ClassIn 服务配置结构。")
            self.dynamic_offset, self.static_offset = self._add_rax(dynamic_inner), self._add_rax(static_inner)
            login_code = self._code(self.core_pe, self._direct_calls(uid_code)[0])
            self.login_offset = self._byte_rax_deref(login_code)
            if self._rax_deref(login_code)[-1] != self.settings_offset:
                raise SessionError("暂不支持当前 ClassIn 版本登录状态结构。")
            if self._rax_deref(self._vcode(reader, table, 88))[-1] != self.config_offset:
                raise SessionError("暂不支持当前 ClassIn 版本登录状态结构。")
        finally:
            reader.close()
        # State offsets and the adjacent device-map cache are checked against
        # verified native EeoNetworkConfig::TokenState layout.
        if self.token_offset != 80:
            raise SessionError("暂不支持当前 ClassIn 版本令牌结构。")

    def _export(self, pe: Any, prefix: str) -> int:
        candidates = [x.address for x in pe.DIRECTORY_ENTRY_EXPORT.symbols if (x.name or b"").decode().startswith(prefix)]
        if len(candidates) != 1:
            raise SessionError("暂不支持当前 ClassIn 客户端结构。")
        return candidates[0]

    def _code(self, pe: Any, rva: int, size: int = 2048) -> list[Any]:
        result = []
        for ins in self.md.disasm(pe.get_data(rva, size), pe.OPTIONAL_HEADER.ImageBase + rva):
            result.append(ins)
            if ins.mnemonic == "ret":
                break
        if not result or result[-1].mnemonic != "ret":
            raise SessionError("暂不支持当前 ClassIn 客户端函数结构。")
        return result

    def _vcode(self, reader: _Reader, table: int, slot: int) -> list[Any]:
        base, size, _ = self.modules["eeocoreengine.dll"]
        target = reader.qword(table + slot)
        if not base <= target < base + size:
            raise SessionError("暂不支持当前 ClassIn 登录函数结构。")
        return self._code(self.core_pe, target - base)

    @staticmethod
    def _rax_deref(code: list[Any]) -> list[int]:
        import capstone.x86 as x
        return [i.operands[1].mem.disp for i in code if i.mnemonic == "mov" and len(i.operands) == 2
                and i.operands[0].type == x.X86_OP_REG and i.operands[0].reg == x.X86_REG_RAX
                and i.operands[1].type == x.X86_OP_MEM and i.operands[1].mem.base == x.X86_REG_RAX
                and i.operands[1].mem.disp > 0]

    @staticmethod
    def _add_rax(code: list[Any]) -> int:
        import capstone.x86 as x
        values = [i.operands[1].imm for i in code if i.mnemonic == "add" and len(i.operands) == 2
                  and i.operands[0].type == x.X86_OP_REG and i.operands[0].reg == x.X86_REG_RAX
                  and i.operands[1].type == x.X86_OP_IMM]
        if len(values) != 1 or not 0 < values[0] < 4096:
            raise SessionError("暂不支持当前 ClassIn 登录字段结构。")
        return values[0]

    def _direct_calls(self, code: list[Any]) -> list[int]:
        import capstone.x86 as x
        # Both supported ClassIn DLLs use this preferred image base; runtime
        # addresses are translated separately through the module inventory.
        if self.core_pe.OPTIONAL_HEADER.ImageBase != self.base_pe.OPTIONAL_HEADER.ImageBase:
            raise SessionError("暂不支持当前 ClassIn 客户端模块结构。")
        return [i.operands[0].imm - self.core_pe.OPTIONAL_HEADER.ImageBase
                for i in code if i.mnemonic == "call" and i.operands[0].type == x.X86_OP_IMM]

    def _returned_global(self, pe: Any, rva: int, mnemonic: str) -> int:
        import capstone.x86 as x
        values = [i.address + i.size + i.operands[1].mem.disp - pe.OPTIONAL_HEADER.ImageBase
                  for i in self._code(pe, rva) if i.mnemonic == mnemonic and len(i.operands) == 2
                  and i.operands[0].type == x.X86_OP_REG and i.operands[0].reg == x.X86_REG_RAX
                  and i.operands[1].type == x.X86_OP_MEM and i.operands[1].mem.base == x.X86_REG_RIP]
        if not values:
            raise SessionError("暂不支持当前 ClassIn 版本登录结构。")
        return values[-1]

    def _rip_lea_rdx(self, code: list[Any]) -> list[int]:
        import capstone.x86 as x
        return [i.address + i.size + i.operands[1].mem.disp - self.base_pe.OPTIONAL_HEADER.ImageBase
                for i in code if i.mnemonic == "lea" and len(i.operands) == 2
                and i.operands[0].type == x.X86_OP_REG and i.operands[0].reg == x.X86_REG_RDX
                and i.operands[1].type == x.X86_OP_MEM and i.operands[1].mem.base == x.X86_REG_RIP]

    def _cache_directory_global(self) -> int:
        import capstone.x86 as x
        from collections import Counter

        # getCacheDirectory has an early return for the one-folder mode. Read
        # the complete function here; its cached QString is the repeated data
        # address used by all return paths, assignments and directory setup.
        start = self._export(self.base_pe, "?getCacheDirectory@CacheManager")
        counts = Counter()
        for ins in self.md.disasm(self.base_pe.get_data(start, 3072), self.base_pe.OPTIONAL_HEADER.ImageBase + start):
            if ins.mnemonic == "lea" and len(ins.operands) == 2 and ins.operands[1].type == x.X86_OP_MEM and ins.operands[1].mem.base == x.X86_REG_RIP:
                target = ins.address + ins.size + ins.operands[1].mem.disp - self.base_pe.OPTIONAL_HEADER.ImageBase
                section = self.base_pe.get_section_by_rva(target)
                if section is not None and section.Characteristics & 0x80000000:
                    counts[target] += 1
        if not counts or counts.most_common(1)[0][1] < 5:
            raise SessionError("暂不支持当前 ClassIn 版本缓存结构。")
        return counts.most_common(1)[0][0]

    @staticmethod
    def _byte_rax_deref(code: list[Any]) -> int:
        import capstone.x86 as x
        values = [i.operands[1].mem.disp for i in code if i.mnemonic == "movzx" and len(i.operands) == 2
                  and i.operands[1].type == x.X86_OP_MEM and i.operands[1].mem.base == x.X86_REG_RAX
                  and i.operands[1].size == 1]
        if len(values) != 1 or not 0 < values[0] < 4096:
            raise SessionError("暂不支持当前 ClassIn 版本登录结构。")
        return values[0]

    def snapshot(self) -> _Snapshot:
        reader = _Reader(self.api, self.pid)
        try:
            core_base, _, _ = self.modules["eeocoreengine.dll"]
            base_base, _, _ = self.modules["eeobase.dll"]
            core = reader.qword(core_base + self.core_global)
            config = reader.qword(core + self.config_offset)
            settings = reader.qword(config + self.settings_offset)
            if reader.read(settings + self.login_offset, 1) != b"\x01":
                raise SessionError("ClassIn 当前未登录，请登录后扫描。")
            uid = reader.qword(core + self.uid_offset)
            secret = reader.qstring(core + self.secret_offset)
            if not uid or not secret:
                raise SessionError("ClassIn 登录失败，请登录成功后扫描。")
            state = base_base + self.token_global
            token_uid = reader.qword(state + 112)
            token = reader.qstring(state + 80) if token_uid == uid else ""
            refresh = reader.qstring(state + 88) if token_uid == uid else ""
            device_map = reader.qstring(state + 168)
            parts = dict(item.split("=", 1) for item in device_map.split(",") if "=" in item)
            snapshot = _Snapshot(uid=uid, secret=secret,
                                 telephone=reader.qstring(core + self.telephone_offset),
                                 dynamic_host=_origin(reader.qstring(settings + self.dynamic_offset)),
                                 static_host=_origin(reader.qstring(settings + self.static_offset)),
                                 token=token, refresh_token=refresh, token_expiry=_token_expiry(token),
                                 device_map=device_map, device_id=reader.qstring(base_base + self.device_global),
                                 client_version=parts.get("version", ""),
                                 cache_directory=Path(reader.qstring(base_base + self.cache_global)))
            # Detect a concurrent logout/account switch while copying fields.
            if reader.qword(core + self.uid_offset) != uid or reader.read(settings + self.login_offset, 1) != b"\x01":
                raise SessionError("ClassIn 登录状态已变化，请重新扫描。")
            return snapshot
        finally:
            reader.close()


def load_current_session(install_dir: str | Path | None = None, roaming_dir: str | Path | None = None,
                         local_dir: str | Path | None = None) -> ClassInSession:
    """Load the authenticated current main-process login, never a history entry.

    Profile path arguments are accepted for the discovery facade. They do not
    replace current native login proof or trigger historical-password reads.
    """
    del roaming_dir, local_dir
    api = _kernel()
    candidates = []
    for pid in _processes(api):
        try:
            modules = _modules(api, pid)
            if not {"classin.exe", "eeocoreengine.dll", "eeobase.dll"}.issubset(modules):
                continue
            if install_dir is not None and modules["classin.exe"][2].parent.resolve() != Path(install_dir).resolve():
                continue
            source = _NativeSource(api, pid, modules)
            snapshot = source.snapshot()
            candidates.append((source, snapshot))
        except (IndexError, KeyError, OSError, ValueError, AttributeError):
            raise SessionError("当前 ClassIn 客户端结构无法识别，请确认已登录并使用受支持版本。") from None
    if not candidates:
        raise SessionError("未找到已登录的 ClassIn 主进程，请先打开 ClassIn 并登录。")
    if len(candidates) != 1:
        raise SessionError("检测到多个已登录 ClassIn 进程，请保留一个客户端后重新扫描。")
    source, snapshot = candidates[0]
    return ClassInSession(uid=snapshot.uid, dynamic_host=snapshot.dynamic_host, static_host=snapshot.static_host,
                          telephone=snapshot.telephone, cache_directory=snapshot.cache_directory,
                          _secret=snapshot.secret, _token=snapshot.token,
                          _refresh_token=snapshot.refresh_token, _token_expires=snapshot.token_expiry,
                          _device_id=snapshot.device_id, _device_map=snapshot.device_map,
                          _client_version=snapshot.client_version, _snapshot_loader=source.snapshot)


Session = ClassInSession
