"""A Qt desktop UI with network/file work isolated in QThread jobs."""

from __future__ import annotations

import json
import math
import os
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from urllib.parse import urlsplit

import requests
from PySide6.QtCore import QEvent, QObject, QSignalBlocker, QSize, QStandardPaths, QThread, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QCloseEvent, QDesktopServices, QFont, QFontDatabase, QKeyEvent, QKeySequence, QResizeEvent, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFileDialog, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget, QTabBar, QSpinBox, QDialog, QListWidgetItem,
    QCheckBox, QListWidget, QSplitter, QStackedWidget,
)

from .downloader import DownloadCancelled, DownloadError, DownloadManager
from .download_history import ExistingDownload, find_downloaded_resources, lookup_download
from .resources import DownloadResult, Resource, ScanResult
from .session import SessionError
from .grouping import group_directory_names, group_labels, resource_group, resource_output_dir
from .gui_workspace import build_workspace
from .gui_views import TOKENS

if TYPE_CHECKING:
    from .discovery import ClassInClient


PROJECT_ROOT = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
                else Path(__file__).resolve().parent.parent)
KIND_LABELS = {"video": "视频素材", "file": "普通文件", "replay": "课堂回放"}
KIND_ALIASES = {"video_material": "video", "material": "video", "recording": "replay"}
SOURCE_LABELS = {
    "remote": "远程资源", "cloud": "云盘", "cloud_disk": "云盘", "course": "课程资料",
    "replay": "课堂回放", "cache": "本地缓存", "local": "本地缓存", "manual": "导入清单",
    "cache-index": "缓存中的远程原件", "cached-api": "缓存的资源索引", "organization": "机构云盘",
    "course-homework": "作业附件", "course-record": "课程录播",
}


def readable_text(value: object, limit: int = 600) -> str:
    """Hide signed URLs, complete auth headers and common secret fields."""
    text = str(value or "")
    text = re.sub(r"https?://[^\s<>\"']+", "[地址已隐藏]", text, flags=re.I)
    text = re.sub(r"(?im)\b(?:authorization|set-cookie|cookie)\s*[:=]\s*[^\r\n]*",
                  "[授权信息已隐藏]", text)
    text = re.sub(r'''(?i)\b(?:token|access_token|refresh_token|password|secret|signature)\b["']?\s*[:=]\s*["']?[^\s,;"']+''',
                  "[授权信息已隐藏]", text)
    text = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "[授权信息已隐藏]", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return text[:limit]


def format_size(size: float) -> str:
    if size <= 0:
        return "未知"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "未知"


def resource_size_label(resource: Resource) -> str:
    if resource.size > 0:
        return format_size(resource.size)
    reported = resource.metadata.get("fileSize")
    if isinstance(reported, str):
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(B|[KMGT]I?B)\s*", reported, re.I)
        if match:
            unit = match[2].upper().replace("IB", "B")
            estimate = float(match[1]) * 1024 ** ("B", "KB", "MB", "GB", "TB").index(unit)
            if math.isfinite(estimate) and estimate > 0:
                return format_size(estimate)
    try:
        estimate = float(resource.metadata.get("approximate_size_kib", 0)) * 1024
    except (TypeError, ValueError, OverflowError):
        estimate = 0.0
    return format_size(estimate) if math.isfinite(estimate) and estimate > 0 else "未知"


def selectable_resource(resource: Resource) -> bool:
    return not resource.metadata.get("candidate_only")


def initial_resource_status(resource: Resource) -> str:
    if resource.metadata.get("candidate_only"):
        return readable_text(resource.metadata.get("status") or "无回放读取权限（809）")
    return readable_text(resource.metadata.get("cache_status") or "待下载")


def download_source_label(resource: Resource | None, source: str) -> str:
    if source != "remote":
        return "完整缓存"
    if resource is not None:
        try:
            playlist_url = urlsplit(resource.url).path.lower().endswith((".m3u8", ".pm3u8"))
        except ValueError:
            playlist_url = False
        if (playlist_url or str(resource.metadata.get("stream_type", "")).lower() == "hls"
                or resource.metadata.get("stream_entry_only") is True):
            return "HLS合并"
        if (resource.metadata.get("content_role") == "transcoded-playback"
                or resource.metadata.get("original_available") is False):
            return "播放资源"
    return "远程原件"


def kind_label(resource: Resource) -> str:
    return KIND_LABELS.get(KIND_ALIASES.get(resource.kind, resource.kind), "其他文件")


def _ui_font() -> QFont:
    families = set(QFontDatabase.families())
    for candidate in ("Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans CJK SC", "SimSun"):
        if candidate in families:
            return QFont(candidate, 10)
    # Qt's Windows offscreen plugin has no system font database. Load the
    # installed Chinese font explicitly so previews/tests also remain readable.
    if not families and sys.platform == "win32":
        font_dirs = [Path(value) for value in QStandardPaths.standardLocations(
            QStandardPaths.StandardLocation.FontsLocation)]
        for variable in ("WINDIR", "SystemRoot"):
            value = os.environ.get(variable)
            if value:
                font_dirs.append(Path(value) / "Fonts")
        for directory in dict.fromkeys(font_dirs):
            font_path = directory / "msyh.ttc"
            if not font_path.is_file():
                continue
            font_id = QFontDatabase.addApplicationFont(str(font_path))
            loaded = QFontDatabase.applicationFontFamilies(font_id)
            if loaded:
                return QFont(loaded[0], 10)
    return QFont(QApplication.font().family(), 10)


def _make_client(install_dir: str | None = None, max_workers: int = 4):
    from .discovery import ClassInClient
    return ClassInClient(install_dir=install_dir, max_workers=max_workers)


def _read_manifest(path: str) -> list[Resource]:
    from .discovery import resources_from_payload
    manifest = Path(path)
    if manifest.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("manifest too large")
    with manifest.open("r", encoding="utf-8-sig") as source:
        return resources_from_payload(json.load(source), source="manual")


@dataclass
class WorkerEvent:
    kind: str
    data: dict = field(default_factory=dict)


class _OperationThread(QThread):
    """Own the callable until Qt has delivered its finished signal."""

    def __init__(self, operation: str, target: Callable[[], None], worker: BackgroundWorker):
        super().__init__(worker)
        self.operation = operation
        self.target = target
        self.worker = worker
        self.setObjectName(f"classin-{operation}")

    def run(self) -> None:
        try:
            self.target()
        except Exception:
            messages = {
                "scan": "资源扫描失败，请确认 ClassIn 已登录，并检查安装目录和网络连接。",
                "import": "无法读取资源清单，请检查 JSON 格式和文件权限。",
                "download": "无法开始批量下载，请检查导出目录和资源信息。",
                "history": "无法检查已下载内容，请检查导出目录后重试。",
                "detect": "无法检测 ClassIn 登录状态，请确认 ClassIn 已打开并登录后重新获取资源。",
            }
            # Unexpected exception messages can contain credentials or URLs.
            self.worker._emit("failed", operation=self.operation, message=messages[self.operation])


class BackgroundWorker(QObject):
    """Serial Qt jobs. Signals are the only path from a job to GUI widgets."""

    event_ready = Signal(object)
    _events_pending = Signal()

    def __init__(self, client_factory: Callable[..., ClassInClient] = _make_client,
                 manager_factory: Callable[..., DownloadManager] = DownloadManager,
                 manifest_reader: Callable[[str], list[Resource]] = _read_manifest, parent: QObject | None = None):
        super().__init__(parent)
        self.client_factory = client_factory
        self.manager_factory = manager_factory
        self.manifest_reader = manifest_reader
        self.client: ClassInClient | None = None
        self.cancel_event = threading.Event()
        self._thread: _OperationThread | None = None
        self._event_queue: deque[WorkerEvent] = deque()
        self._event_lock = threading.Lock()
        self._event_delivery_scheduled = False
        self._event_timer = QTimer(self)
        self._event_timer.setSingleShot(True)
        self._event_timer.setInterval(1)
        self._event_timer.timeout.connect(self._deliver_events)
        self._events_pending.connect(self._deliver_events, Qt.ConnectionType.QueuedConnection)

    @property
    def running(self) -> bool:
        # Keep the job busy until the queued native finished signal is handled.
        # This prevents a late finished event from clearing a subsequent job.
        return self._thread is not None

    def _emit(self, kind: str, **data) -> None:
        with self._event_lock:
            self._event_queue.append(WorkerEvent(kind, data))
            wake = not self._event_delivery_scheduled
            self._event_delivery_scheduled = True
        if wake:
            self._events_pending.emit()

    @Slot()
    def _deliver_events(self) -> None:
        # One wakeup serves a burst of worker events. Deliver bounded batches
        # on the main thread so input and repaint events can run between them.
        with self._event_lock:
            events = [self._event_queue.popleft() for _ in range(min(64, len(self._event_queue)))]
            more = bool(self._event_queue)
            if not more:
                self._event_delivery_scheduled = False
        for event in events:
            self.event_ready.emit(event)
        if more:
            self._event_timer.start()

    def _launch(self, operation: str, target: Callable[[], None]) -> bool:
        if self.running:
            return False
        self.cancel_event.clear()
        self._thread = _OperationThread(operation, target, self)
        self._thread.finished.connect(self._thread_finished, Qt.ConnectionType.QueuedConnection)
        self._thread.start()
        return True

    @Slot()
    def _thread_finished(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._thread = None
        self._emit("worker_finished", operation=thread.operation)
        thread.deleteLater()

    def close_client(self) -> None:
        client, self.client = self.client, None
        session = getattr(client, "auth_session", None)
        if session is not None:
            session.close()

    def start_scan(self, install_dir: str | None = None, max_workers: int = 4) -> bool:
        def scan():
            self.close_client()
            candidate = self.client_factory(install_dir=install_dir, max_workers=max_workers)
            try:
                result = candidate.scan()
            except Exception:
                session = getattr(candidate, "auth_session", None)
                if session is not None:
                    session.close()
                raise
            self.client = candidate
            self._emit("scan_finished", result=result)
        return self._launch("scan", scan)

    def start_detect(self, install_dir: str | None = None) -> bool:
        def detect():
            self.close_client()
            candidate = None
            try:
                if self.cancel_event.is_set():
                    raise DownloadCancelled("登录检测已取消。")
                candidate = self.client_factory(install_dir=install_dir)
                session = candidate._current_session()
                if self.cancel_event.is_set():
                    raise DownloadCancelled("登录检测已取消。")
                session._check_login()
                diagnostics = session.diagnostics()
                if not diagnostics.get("uid_available") or not diagnostics.get("signing_secret_available"):
                    raise SessionError("ClassIn 当前登录信息不完整，请登录成功后重新获取资源。")
                if self.cancel_event.is_set():
                    raise DownloadCancelled("登录检测已取消。")
                self.client = candidate
                self._emit("login_detected")
            except DownloadCancelled:
                self._emit("login_cancelled")
            except SessionError as error:
                self._emit("login_cancelled" if self.cancel_event.is_set() else "login_failed",
                           message=readable_text(error))
            finally:
                if candidate is not None and candidate is not self.client:
                    session = getattr(candidate, "auth_session", None)
                    if session is not None:
                        session.close()
        return self._launch("detect", detect)

    def start_import(self, path: str) -> bool:
        def import_manifest():
            self._emit("import_finished", resources=self.manifest_reader(path))
        return self._launch("import", import_manifest)

    def start_history(self, resources: list[Resource], output_dir: str, generation: int) -> bool:
        def history():
            try:
                downloaded = find_downloaded_resources(resources, output_dir, cancel=self.cancel_event)
            except DownloadCancelled:
                self._emit("history_cancelled", generation=generation)
            else:
                self._emit("history_finished", downloaded=downloaded, output_dir=output_dir,
                           generation=generation)
        return self._launch("history", history)

    def start_download(self, resources: list[Resource], output_dir: str, max_workers: int = 4,
                       *, resource_snapshot: list[Resource] | None = None) -> bool:
        if type(max_workers) is not int or not 1 <= max_workers <= 16:
            raise ValueError("下载线程数必须为 1–16 的整数。")
        # Checkbox/filter edits cannot change a batch already in progress.
        batch = [resource for resource in resources if selectable_resource(resource)]
        directory_names = group_directory_names(list(resource_snapshot) if resource_snapshot is not None else resources)

        def download():
            completed = failed = skipped = 0
            # The course plan is fixed for this batch. Resolve each destination
            # once in the background rather than for every concurrent file.
            course_dirs: dict[str, Path] = {}
            for resource in batch:
                group_key = resource_group(resource)[0]
                if group_key not in course_dirs:
                    course_dirs[group_key] = resource_output_dir(output_dir, resource, directory_names=directory_names)
            def transfer(index, resource):
                if self.cancel_event.is_set():
                    return "not_started"
                self._emit("download_started", resource_id=resource.id, index=index, total=len(batch))
                last_progress = 0.0

                def progress(done: int, total: int):
                    nonlocal last_progress
                    now = time.monotonic()
                    if now - last_progress >= 0.12 or (total > 0 and done >= total):
                        last_progress = now
                        self._emit("progress", resource_id=resource.id, done=done, total=total)

                refresh_message = ""
                owned_transport = None
                try:
                    course_dir = course_dirs[resource_group(resource)[0]]
                    existing = lookup_download(resource, course_dir, verify_changed=True,
                                               recover_legacy=True, cancel=self.cancel_event)
                    if existing is not None:
                        result = DownloadResult(resource.id, existing.path, existing.source,
                                                existing.bytes_written, existing.sha256, skipped=True,
                                                warning=existing.warning)
                        self._emit("download_finished", resource_id=resource.id, result=result)
                        return "skipped"
                    transport = getattr(self.client, "session", None)
                    if transport is None:
                        owned_transport = transport = requests.Session()
                    manager = self.manager_factory(course_dir, session=transport)
                    resolved = resource
                    if self.client is not None:
                        try:
                            resolved = self.client.resolve(resource)
                        except DownloadCancelled:
                            raise
                        except Exception as error:
                            if isinstance(error, DownloadError):
                                reason = readable_text(error)
                            elif isinstance(error, requests.RequestException):
                                reason = "更新资源地址时网络连接失败。"
                            else:
                                reason = "无法更新当前资源的下载地址。"
                            refresh_message = reason + " 将尝试已有地址，远程失败后核验完整缓存。"
                            self._emit("download_warning", resource_id=resource.id, message=refresh_message)
                    if self.cancel_event.is_set():
                        raise DownloadCancelled("下载已取消。")
                    result = manager.download(resolved, progress=progress, cancel=self.cancel_event)
                    self._emit("download_finished", resource_id=resource.id, result=result)
                    return "skipped" if result.skipped else "completed"
                except DownloadCancelled:
                    self._emit("download_cancelled", resource_id=resource.id)
                    return "cancelled"
                except DownloadError as error:
                    self._emit("download_failed", resource_id=resource.id,
                               message=readable_text(refresh_message + " " + str(error)))
                    return "failed"
                except Exception:
                    self._emit("download_failed", resource_id=resource.id,
                               message="无法下载此资源，请检查资源信息并重新扫描后重试。")
                    return "failed"
                finally:
                    if owned_transport is not None:
                        owned_transport.close()
                    else:
                        release = getattr(getattr(self.client, "auth_session", None), "release_thread_transport", None)
                        if callable(release):
                            try:
                                release()
                            except Exception:
                                self._emit("download_warning", resource_id=resource.id,
                                           message="此任务的网络连接清理失败，请重新扫描。")

            # Only keep max_workers submitted jobs. Cancellation therefore
            # stops replenishing the queue instead of launching the whole batch.
            queue = iter(enumerate(batch, 1))
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="classin-download") as pool:
                pending = {}

                def replenish():
                    while len(pending) < max_workers and not self.cancel_event.is_set():
                        entry = next(queue, None)
                        if entry is None:
                            break
                        pending[pool.submit(transfer, *entry)] = entry[0]

                replenish()
                while pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in sorted(done, key=lambda item: pending[item]):
                        pending.pop(future)
                        if future.cancelled():
                            continue
                        outcome = future.result()
                        completed += outcome == "completed"
                        failed += outcome == "failed"
                        skipped += outcome == "skipped"
                    if self.cancel_event.is_set():
                        for future in pending:
                            future.cancel()
                    else:
                        replenish()
            self._emit("batch_finished", completed=completed, failed=failed, skipped=skipped,
                       cancelled=self.cancel_event.is_set(), remaining=len(batch) - completed - failed - skipped)
        return self._launch("download", download)

    def cancel(self) -> None:
        self.cancel_event.set()


class ExporterApp(QMainWindow):
    # The external layout builder initializes these before signals are connected.
    # Explicit declarations let editors verify both assignments and consumers.
    navigation_toggle_button: QPushButton
    session_text: QLabel
    scan_button: QPushButton
    settings_button: QPushButton
    workspace_splitter: QSplitter
    navigation_panel: QWidget
    course_nav: QListWidget
    scope_title: QLabel
    bulk_all_button: QPushButton
    bulk_none_button: QPushButton
    bulk_invert_button: QPushButton
    visible_text: QLabel
    name_filter: QLineEdit
    clear_filter_button: QPushButton
    course_filter: QComboBox
    type_filter: QComboBox
    scope_tabs: QTabBar
    kind_tabs: QTabBar
    kind_tab_panel: QWidget
    warning_panel: QWidget
    warning_text: QLabel
    task_button: QPushButton
    warning_gap: QWidget
    tree: QTreeWidget
    header_check: QCheckBox
    list_stack: QStackedWidget
    empty_title: QLabel
    empty_text: QLabel
    detail_name: QLabel
    detail_meta: QLabel
    progress_bar: QProgressBar
    output_dir: QLineEdit
    output_button: QPushButton
    open_output_button: QPushButton
    progress_text: QLabel
    log_button: QPushButton
    selection_text: QLabel
    action_stack: QStackedWidget
    download_button: QPushButton
    cancel_button: QPushButton
    settings_dialog: QDialog
    install_dir: QLineEdit
    install_button: QPushButton
    scan_threads: QSpinBox
    download_threads: QSpinBox
    import_button: QPushButton
    task_dialog: QDialog
    messages: QPlainTextEdit
    summary_text: QLabel
    resource_detail_text: QPlainTextEdit

    def __init__(self, parent: QWidget | None = None, worker: BackgroundWorker | None = None,
                 *, auto_detect: bool | None = None):
        super().__init__(parent)
        # Injected workers are offline fixtures unless startup detection is
        # explicitly requested; the normal desktop app detects on every launch.
        self._startup_detection_enabled = (worker is None) if auto_detect is None else auto_detect
        self.worker = worker or BackgroundWorker(parent=self)
        self._startup_timer = QTimer(self)
        self._startup_timer.setSingleShot(True)
        self._startup_timer.timeout.connect(self.detect_login)
        self.resources: dict[str, Resource] = {}
        self.items: dict[str, QTreeWidgetItem] = {}
        self.checked: set[str] = set()
        self.statuses: dict[str, str] = {}
        self._downloaded: dict[str, ExistingDownload] = {}
        self._output_cache_text: str | None = None
        self._output_cache_path = ""
        self._download_output_root = ""
        self._active_download_output_root = ""
        self._history_auto_enabled = False
        self._history_generation = 0
        self._history_pending = False
        self._history_timer = QTimer(self)
        self._history_timer.setSingleShot(True)
        self._history_timer.setInterval(250)
        self._history_timer.timeout.connect(self.refresh_download_history)
        self._busy = ""
        self._closing = False
        self._download_total = 0
        self._active_downloads: set[str] = set()
        self._finished_downloads: set[str] = set()
        self._resource_progress: dict[str, tuple[int, int]] = {}
        self._pending_status_updates: set[str] = set()
        self._selection_update_pending = False
        self._download_update_timer = QTimer(self)
        self._download_update_timer.setSingleShot(True)
        self._download_update_timer.setInterval(50)
        self._download_update_timer.timeout.connect(self._flush_download_updates)
        self.setWindowTitle("ClassInExporter")
        self.resize(1280, 800)
        self.setMinimumSize(920, 680)
        font = _ui_font()
        font.setPixelSize(14)
        self.setFont(font)
        self._has_loaded = False
        self._scan_failed = False
        self._scan_warnings: list[str] = []
        self._detail_resource_id: str | None = None
        self.resource_dialog: QDialog | None = None
        self._build()
        self.worker.event_ready.connect(self._handle_event, Qt.ConnectionType.QueuedConnection)
        self.name_filter.textChanged.connect(self._render)
        self.type_filter.currentTextChanged.connect(self._type_filter_changed)
        self.course_filter.currentTextChanged.connect(self._course_filter_changed)
        self.output_dir.textChanged.connect(self._output_changed)
        self.scope_tabs.currentChanged.connect(self._scope_tab_changed)
        self.kind_tabs.currentChanged.connect(self._kind_tab_changed)
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.itemDoubleClicked.connect(self._double_click)
        self.tree.installEventFilter(self)
        self._search_shortcut = QShortcut(QKeySequence("Ctrl+F"), self)
        self._search_shortcut.activated.connect(self.name_filter.setFocus)
        self._message("发现条目不代表可下载；实际导出会校验长度、缓存完整性或回放流。")
        self._rebuild_scope_tabs()
        self._render()
        if self._startup_detection_enabled:
            self._startup_timer.start(0)

    def _button(self, text: str, callback: Callable[[], None], primary: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setMinimumHeight(38)
        button.setAccessibleName(text)
        if primary:
            button.setObjectName("primary")
            button.setProperty("primary", True)
        button.clicked.connect(callback)
        return button

    def _build(self) -> None:
        build_workspace(self, PROJECT_ROOT, KIND_LABELS)
        self.bulk_all_button.setToolTip("全选当前列表中尚未下载的资源；跳过已下载内容和权限候选")
        self.bulk_all_button.setAccessibleName("全选当前尚未下载的可下载资源")
        self.header_check.setToolTip("全选或取消当前尚未下载的资源")
        self.header_check.setAccessibleName("全选或取消当前尚未下载的可下载资源")

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.tree and event.type() == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
            if event.key() == Qt.Key.Key_Space and not event.modifiers() & (
                    Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
                self._space_toggle()
                return True
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.show_resource_details()
                return True
        return super().eventFilter(watched, event)

    def _toggle_navigation(self) -> None:
        visible = not self.navigation_panel.isVisible()
        self.navigation_panel.setVisible(visible)
        self.navigation_toggle_button.setText("收起课程" if visible else "展开课程")

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "navigation_toggle_button"):
            compact = self.width() < 1120
            self.navigation_toggle_button.setVisible(compact)
            self.session_text.setMinimumWidth(120 if compact else 220)
            if not compact:
                self.navigation_panel.show()
                self.navigation_toggle_button.setText("收起课程")

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if (event.type() == QEvent.Type.ActivationChange and self.isActiveWindow()
                and getattr(self, "_history_auto_enabled", False)):
            self._schedule_history_refresh()

    def show_settings(self) -> None:
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    def show_task_details(self) -> None:
        self.task_dialog.show()
        self.task_dialog.raise_()
        self.task_dialog.activateWindow()
        self.messages.setFocus()

    def _set_scan_warnings(self, warnings: list[str]) -> None:
        self._scan_warnings = [readable_text(value, 4096) for value in warnings if str(value).strip()]
        has_warnings = bool(self._scan_warnings)
        self.warning_panel.setVisible(has_warnings)
        self.warning_gap.setVisible(has_warnings)
        if has_warnings:
            groups = {}
            for value in self._scan_warnings:
                match = re.search(r"：(课程云盘补充目录未确认（HTTP \d+）)[；，]", value)
                if match:
                    groups.setdefault(match[1], []).append(value)
            group_label, group = max(groups.items(), key=lambda entry: len(entry[1]), default=("", []))
            if len(group) > 1:
                summary = f"{len(group)} 条{group_label.removeprefix('课程云盘')}"
                remaining = len(self._scan_warnings) - len(group)
            else:
                summary = self._scan_warnings[0].replace("\n", " ")
                remaining = len(self._scan_warnings) - 1
            if remaining:
                summary += f"（另 {remaining} 项，查看详情）"
            self.warning_text.setText(summary)
            self.warning_text.setToolTip("\n".join(self._scan_warnings))
        else:
            self.warning_text.clear()

    @Slot(object, object)
    def _navigation_changed(self, current: QListWidgetItem | None, previous: QListWidgetItem | None = None) -> None:
        if current is None:
            return
        key = current.data(Qt.ItemDataRole.UserRole)
        if key is not None:
            self.course_filter.setCurrentIndex(self.course_filter.findData(key))

    def _sync_navigation(self) -> None:
        key = self.course_filter.currentData() or "all"
        with QSignalBlocker(self.course_nav):
            for index in range(self.course_nav.count()):
                item = self.course_nav.item(index)
                if item.data(Qt.ItemDataRole.UserRole) == key:
                    self.course_nav.setCurrentItem(item)
                    break
        self.scope_title.setText("全部资源" if key == "all" else self.course_filter.currentText())
        self.scope_title.setToolTip(self.scope_title.text())

    @Slot(object, object)
    def _selected_resource_changed(self, current: QTreeWidgetItem | None, previous: QTreeWidgetItem | None = None) -> None:
        self._detail_resource_id = current.data(0, Qt.ItemDataRole.UserRole) if current is not None else None
        self._update_detail()

    def _update_detail(self) -> None:
        resource = self.resources.get(self._detail_resource_id) if self._detail_resource_id is not None else None
        if resource is None:
            self.detail_name.setText("选择资源查看完整名称")
            self.detail_meta.clear()
            return
        status = self._resource_status(resource)
        name = readable_text(resource.name, 4096)
        self.detail_name.setText(name)
        self.detail_name.setToolTip(name)
        course = readable_text(resource.course or "未归属", 4096)
        source = SOURCE_LABELS.get(resource.source, "发现的资源")
        qualification = ("全选与批量下载时跳过" if resource.id in self._downloaded else
                         "不可下载" if not selectable_resource(resource) else
                         "优先下载远程原件" if download_source_label(resource, "remote") == "远程原件" else
                         "下载可用播放资源")
        detail = f"{course} / {source} {resource_size_label(resource)} {status} {qualification}"
        self.detail_meta.setText(detail)
        self.detail_meta.setToolTip(detail)

    def show_resource_details(self, resource_id: str | None = None) -> None:
        if not isinstance(resource_id, str):
            resource_id = self._detail_resource_id
        if resource_id is None:
            return
        resource = self.resources.get(resource_id)
        if resource is None:
            return
        if self.resource_dialog is None:
            self.resource_dialog = QDialog(self)
            self.resource_dialog.setObjectName("resource-details")
            self.resource_dialog.setWindowTitle("资源详情")
            self.resource_dialog.resize(720, 440)
            layout = QVBoxLayout(self.resource_dialog)
            layout.setContentsMargins(24, 24, 24, 24)
            layout.setSpacing(12)
            heading = QLabel("资源详情")
            heading.setProperty("role", "pageTitle")
            layout.addWidget(heading)
            self.resource_detail_text = QPlainTextEdit()
            self.resource_detail_text.setObjectName("fullResourceDetails")
            self.resource_detail_text.setAccessibleName("资源完整名称与状态，可选择复制")
            self.resource_detail_text.setReadOnly(True)
            layout.addWidget(self.resource_detail_text, 1)
            layout.addWidget(self._button("关闭", self.resource_dialog.hide), 0, Qt.AlignmentFlag.AlignRight)
        status = self._resource_status(resource)
        qualification = ("全选和批量下载会跳过此资源。" if resource.id in self._downloaded else
                         "不可下载；仅为权限或资源候选。" if not selectable_resource(resource) else
                         "优先下载远程原件；失败后核验完整缓存。" if download_source_label(resource, "remote") == "远程原件" else
                         "下载可用播放资源；失败后核验完整缓存。")
        self.resource_detail_text.setPlainText(
            readable_text(resource.name, 4096) + "\n\n" +
            f"类型：{kind_label(resource)}\n" +
            f"课程：{readable_text(resource.course or '未归属', 4096)}\n" +
            f"来源：{SOURCE_LABELS.get(resource.source, '发现的资源')}\n" +
            f"大小：{resource_size_label(resource)}\n\n" +
            f"状态：{status}\n\n{qualification}")
        self.resource_dialog.show()
        self.resource_dialog.raise_()
        self.resource_dialog.activateWindow()
        self.resource_detail_text.setFocus()

    def _message(self, text: object, replace: bool = False) -> None:
        cleaned = readable_text(text)
        if replace:
            self.messages.setPlainText(cleaned)
        else:
            self.messages.appendPlainText(cleaned)
        self.messages.verticalScrollBar().setValue(self.messages.verticalScrollBar().maximum())

    def _choose_install(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择 ClassIn 安装目录", self.install_dir.text())
        if path:
            self.install_dir.setText(path)

    def _choose_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择导出目录", self.output_dir.text())
        if path:
            self.output_dir.setText(path)

    def _output_path(self) -> str:
        output = self.output_dir.text().strip()
        if output == self._output_cache_text:
            return self._output_cache_path
        self._output_cache_text = output
        self._output_cache_path = ""
        if not output:
            return ""
        try:
            self._output_cache_path = str(Path(output).expanduser().resolve())
        except (OSError, ValueError, RuntimeError):
            pass
        return self._output_cache_path

    @Slot()
    def _output_changed(self) -> None:
        self._history_generation += 1
        self._download_output_root = self._output_path()
        self._clear_downloaded()
        self._render()
        self._schedule_history_refresh()

    def _clear_downloaded(self) -> None:
        for resource_id in self._downloaded:
            if self.statuses.get(resource_id, "").startswith("已下载"):
                self.statuses.pop(resource_id, None)
        self._downloaded.clear()

    def _schedule_history_refresh(self) -> None:
        if self._history_auto_enabled and self.resources and not self._closing:
            self._history_pending = True
            self._history_timer.start()

    @Slot()
    def refresh_download_history(self) -> None:
        self._history_timer.stop()
        if not self._history_auto_enabled or not self.resources or self._closing:
            self._history_pending = False
            return
        if self._busy or self.worker.running:
            self._history_pending = True
            return
        self._history_pending = False
        output = self._output_path()
        if not output:
            return
        if self.worker.start_history(list(self.resources.values()), output, self._history_generation):
            self._set_busy("history")
            self.progress_text.setText("正在检查已下载内容…")
            self._set_progress(None)

    def _can_download(self, resource: Resource) -> bool:
        return selectable_resource(resource) and resource.id not in self._downloaded

    def _resource_status(self, resource: Resource) -> str:
        existing = self._downloaded.get(resource.id)
        if existing is not None:
            suffix = download_source_label(resource, existing.source)
            return f"已下载 · {suffix}"
        return self.statuses.get(resource.id, initial_resource_status(resource))

    def _open_output(self) -> None:
        output = self.output_dir.text().strip()
        if not output:
            QMessageBox.information(self, "导出目录", "请填写或选择导出目录。")
            return
        try:
            path = Path(output).expanduser().resolve()
            path.mkdir(parents=True, exist_ok=True)
        except (FileExistsError, NotADirectoryError):
            QMessageBox.warning(self, "导出目录", "导出路径已被文件占用，请重新选择目录。")
            return
        except PermissionError:
            QMessageBox.warning(self, "导出目录", "没有权限创建或访问该目录，请选择有写入权限的目录。")
            return
        except (OSError, ValueError, RuntimeError):
            QMessageBox.warning(self, "导出目录", "导出目录无效或无法创建，请检查路径后重新选择目录。")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.warning(self, "导出目录", "无法打开该目录。")

    @Slot()
    def detect_login(self) -> None:
        self._startup_timer.stop()
        if not self._startup_detection_enabled or self._closing or self._busy or self.worker.running:
            return
        self._startup_detection_enabled = False
        if self.worker.start_detect(self.install_dir.text().strip() or None):
            self._set_busy("detect")
            self.session_text.setToolTip("")
            self.session_text.setText("正在检测 ClassIn 登录状态…")
            self.progress_text.setText("正在读取当前客户端的登录状态…")
            self._set_progress(None)

    @Slot()
    def scan(self) -> None:
        self._startup_timer.stop()
        self._startup_detection_enabled = False
        if not self._busy and self.worker.start_scan(self.install_dir.text().strip() or None, self.scan_threads.value()):
            self._history_timer.stop()
            self._history_pending = False
            self._set_busy("scan")
            self.session_text.setToolTip("")
            self.session_text.setText("正在检测 ClassIn 和当前登录会话…")
            self.progress_text.setText("正在扫描资源元数据，请稍候…")
            self._set_progress(None)

    def import_manifest(self, path: str | bool | None = None) -> None:
        if self._busy:
            return
        if not isinstance(path, str):
            path, _ = QFileDialog.getOpenFileName(self, "导入资源 JSON Manifest", "", "JSON Manifest (*.json);;所有文件 (*)")
        if path and self.worker.start_import(path):
            self._set_busy("import")
            self.progress_text.setText("正在读取资源清单…")
            self._set_progress(None)

    @Slot()
    def download(self) -> None:
        if self._busy:
            return
        resources = [resource for resource_id, resource in self.resources.items()
                     if resource_id in self.checked and self._can_download(resource)]
        output = self.output_dir.text().strip()
        if not resources:
            return
        if not output:
            QMessageBox.information(self, "选择导出目录", "请填写或选择导出目录。")
            return
        try:
            output_path = str(Path(output).expanduser().resolve())
        except (OSError, ValueError):
            self._message("导出目录无效，请重新选择目录。")
            return
        self._download_total = len(resources)
        self._active_downloads.clear()
        self._finished_downloads.clear()
        self._resource_progress.clear()
        self._download_update_timer.stop()
        self._pending_status_updates.clear()
        if self.worker.start_download(resources, output_path, self.download_threads.value(),
                                      resource_snapshot=list(self.resources.values())):
            self._history_timer.stop()
            self._history_pending = False
            self._active_download_output_root = output_path
            for resource in resources:
                self.statuses[resource.id] = "等待下载"
                self._queue_download_update(resource.id)
            self._set_busy("download")
            self.progress_text.setText(f"正在准备 {len(resources)} 个资源…")

    @Slot()
    def cancel(self) -> None:
        if self._busy in {"download", "history", "detect"}:
            if self._busy == "history":
                self._history_pending = False
                self._history_timer.stop()
            self.worker.cancel()
            self.cancel_button.setEnabled(False)
            self.progress_text.setText("正在取消登录检测…" if self._busy == "detect" else
                                      "正在取消检查…" if self._busy == "history" else
                                      "正在取消，等待当前请求结束并清理未完成文件…")

    def _set_busy(self, operation: str) -> None:
        self._busy = operation
        for widget in (self.scan_button, self.import_button, self.install_button,
                       self.install_dir, self.output_dir, self.output_button):
            widget.setEnabled(not operation and not self._closing)
        self.scan_threads.setEnabled(not operation and not self._closing)
        self.download_threads.setEnabled(not operation and not self._closing)
        self.settings_button.setEnabled(not self._closing)
        self.cancel_button.setEnabled(operation in {"download", "history", "detect"} and not self._closing)
        self.cancel_button.setText("取消检测" if operation == "detect" else "取消检查" if operation == "history" else "取消下载")
        self.cancel_button.setAccessibleName(self.cancel_button.text())
        self.action_stack.setCurrentIndex(1 if operation in {"download", "history", "detect"} else 0)
        self.progress_bar.setVisible(bool(operation))
        self.scan_button.setText("正在获取…" if operation == "scan" else "获取资源")
        self._update_summary()

    def _set_progress(self, value: int | None) -> None:
        self.progress_bar.setRange(0, 0 if value is None else 100)
        if value is not None:
            self.progress_bar.setValue(value)
        self.progress_bar.setVisible(value is None or bool(self._busy))

    def _visible(self) -> list[Resource]:
        name = self.name_filter.text().strip().casefold()
        kind = self.type_filter.currentText()
        scope = self.course_filter.currentData() or "all"
        return [resource for resource in self.resources.values()
                if (not name or name in resource.name.casefold())
                and (kind == "全部类型" or kind_label(resource) == kind)
                and (scope == "all" or resource_group(resource)[0] == scope)]

    @Slot()
    def _course_filter_changed(self) -> None:
        key = self.course_filter.currentData() or "all"
        with QSignalBlocker(self.scope_tabs):
            for index in range(self.scope_tabs.count()):
                if self.scope_tabs.tabData(index) == key:
                    self.scope_tabs.setCurrentIndex(index)
                    break
        self._sync_navigation()
        self._render()

    @Slot(int)
    def _scope_tab_changed(self, index: int) -> None:
        with QSignalBlocker(self.course_filter):
            self.course_filter.setCurrentIndex(self.course_filter.findData(self.scope_tabs.tabData(index)))
        self._sync_navigation()
        self._render()

    @Slot()
    def _type_filter_changed(self) -> None:
        with QSignalBlocker(self.kind_tabs):
            for index in range(self.kind_tabs.count()):
                if self.kind_tabs.tabData(index) == self.type_filter.currentText():
                    self.kind_tabs.setCurrentIndex(index)
                    break
        self._render()

    @Slot(int)
    def _kind_tab_changed(self, index: int) -> None:
        with QSignalBlocker(self.type_filter):
            self.type_filter.setCurrentText(self.kind_tabs.tabData(index))
        self._render()

    def _rebuild_scope_tabs(self) -> None:
        selected = self.course_filter.currentData() or "all"
        resources = list(self.resources.values())
        labels = group_labels(resources)
        labels.setdefault("cloud", "个人云盘")
        labels.setdefault("organization", "机构云盘")
        labels["unassigned"] = "未归属资源"
        course_keys = list(dict.fromkeys(resource_group(resource)[0] for resource in resources
                                       if resource_group(resource)[0].startswith("course")))
        keys = ["cloud", "organization", *course_keys, "unassigned"]
        counts = {key: 0 for key in keys}
        for resource in resources:
            counts[resource_group(resource)[0]] += 1
        with QSignalBlocker(self.scope_tabs), QSignalBlocker(self.course_filter):
            while self.scope_tabs.count():
                self.scope_tabs.removeTab(0)
            self.course_filter.clear()
            self.scope_tabs.addTab(f"全部资源 ({len(resources)})")
            self.scope_tabs.setTabData(0, "all")
            self.course_filter.addItem("全部课程", "all")
            for key in keys:
                label = readable_text(labels[key], 240)
                index = self.scope_tabs.addTab(f"{label} ({counts[key]})")
                self.scope_tabs.setTabData(index, key)
                self.scope_tabs.setTabToolTip(index, label)
                self.course_filter.addItem(label, key)
            selected_index = self.course_filter.findData(selected)
            selected_index = max(0, selected_index)
            self.course_filter.setCurrentIndex(selected_index)
            self.scope_tabs.setCurrentIndex(selected_index)
        with QSignalBlocker(self.course_nav):
            self.course_nav.clear()
            def add_scope(key, label, count, height=46):
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, key)
                item.setData(Qt.ItemDataRole.UserRole + 1, count)
                item.setData(Qt.ItemDataRole.AccessibleTextRole, f"{label}，{count} 项资源")
                item.setToolTip(label)
                item.setSizeHint(QSize(190, height))
                self.course_nav.addItem(item)
            def section(label, height):
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole + 2, True)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                item.setSizeHint(QSize(190, height))
                self.course_nav.addItem(item)
            add_scope("all", "全部资源", len(resources), 42)
            section("课程", 42)
            for key in keys:
                if key.startswith("course"):
                    add_scope(key, readable_text(labels[key], 4096), counts[key])
            section("云盘与其他", 68 if any(key.startswith("course") for key in keys) else 48)
            for key in ("cloud", "organization", "unassigned"):
                add_scope(key, readable_text(labels[key], 4096), counts[key])
        self._sync_navigation()

    def _update_kind_counts(self) -> None:
        name = self.name_filter.text().strip().casefold()
        scope = self.course_filter.currentData() or "all"
        scoped = [resource for resource in self.resources.values()
                  if (scope == "all" or resource_group(resource)[0] == scope)
                  and (not name or name in resource.name.casefold())]
        with QSignalBlocker(self.kind_tabs):
            for index, text in enumerate(("全部", "普通文件", "视频资料", "课堂回放", "其他文件")):
                kind = self.kind_tabs.tabData(index)
                count = sum(kind == "全部类型" or kind_label(resource) == kind for resource in scoped)
                self.kind_tabs.setTabText(index, f"{text}  {count}")
                if index == 4:
                    self.kind_tabs.setTabVisible(index, count > 0 or self.kind_tabs.currentIndex() == index)

    @Slot()
    def _render(self) -> None:
        visible_ids = {resource.id for resource in self._visible()}
        with QSignalBlocker(self.tree):
            for resource_id, resource in self.resources.items():
                item = self.items.get(resource_id)
                if item is None:
                    item = QTreeWidgetItem(self.tree)
                    item.setData(0, Qt.ItemDataRole.UserRole, resource_id)
                    self.items[resource_id] = item
                if not self._can_download(resource):
                    self.checked.discard(resource_id)
                name = readable_text(resource.name, 4096)
                course = readable_text(resource.course or "未归属", 4096)
                source = SOURCE_LABELS.get(resource.source, "发现的资源")
                values = (name, kind_label(resource), course, resource_size_label(resource), source)
                for column, value in enumerate(values, 1):
                    item.setText(column, value)
                    item.setToolTip(column, value)
                    item.setForeground(column, QColor(TOKENS["secondary"] if column == 4 else TOKENS["text"]))
                item.setData(1, Qt.ItemDataRole.UserRole + 1, f"{course}-{source}")
                self._update_resource_status(resource_id)
                item.setHidden(resource_id not in visible_ids)
            current = self.tree.currentItem()
            if current is None or current.isHidden():
                current = next((self.items[key] for key in self.resources if key in visible_ids), None)
                if current is not None:
                    self.tree.setCurrentItem(current)
                else:
                    self.tree.selectionModel().clearCurrentIndex()
            self._detail_resource_id = current.data(0, Qt.ItemDataRole.UserRole) if current is not None else None
        self._update_summary()
        self._update_kind_counts()
        self._update_detail()
        self.list_stack.setCurrentIndex(0 if visible_ids else 1)
        if not visible_ids:
            if not self._has_loaded:
                self.empty_title.setText("本次获取未完成" if self._scan_failed else "登录 ClassIn后获取课程资源")
                self.empty_text.setText("查看任务详情，确认 ClassIn 已登录后重新获取。" if self._scan_failed else "点击右上方“获取资源”，自动检测本机已登录的 ClassIn。")
            elif self.name_filter.text().strip():
                self.empty_title.setText("没有匹配的资源")
                self.empty_text.setText("尝试其他名称，或清除筛选。切换课程与类型会保留已选项目。")
            elif self._scan_warnings:
                self.empty_title.setText("此范围暂未发现可确认的资源")
                self.empty_text.setText("部分目录读取尚未确认；查看任务详情后可重新获取。")
            else:
                self.empty_title.setText("此范围暂无资源")
                self.empty_text.setText("可切换课程、云盘或资源类型，也可以重新获取资源。")

    def _update_resource_status(self, resource_id: str) -> None:
        resource = self.resources.get(resource_id)
        item = self.items.get(resource_id)
        if resource is None or item is None:
            return
        status = self._resource_status(resource)
        display_status = status
        if not selectable_resource(resource) and "809" in status:
            display_status = "权限不足（809）\n录像未确认"
        elif resource_id not in self.statuses and any(word in status for word in ("未确认", "片段", "部分")):
            display_status = "缓存完整性未确认"
        elif resource_id not in self.statuses and status.startswith("完整缓存"):
            display_status = "完整缓存"
        # Completion can remove a checkbox without rebuilding names, filters or
        # other rows. Concurrent progress remains bounded by the update timer.
        with QSignalBlocker(self.tree):
            selectable = self._can_download(resource)
            downloaded = resource_id in self._downloaded
            selected = selectable and resource_id in self.checked
            selection_state = int(selectable) | int(selected) * 2 | int(downloaded) * 4
            # Progress does not change checkbox semantics. Apply native flags
            # and accessibility only when selection or qualification changes.
            if item.data(0, Qt.ItemDataRole.UserRole + 4) != selection_state:
                item.setFlags((item.flags() | Qt.ItemFlag.ItemIsUserCheckable) if selectable else
                              (item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable))
                item.setData(0, Qt.ItemDataRole.UserRole + 3, not selectable)
                item.setCheckState(0, Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked)
                item.setToolTip(0, "已下载，全选和批量下载时跳过" if downloaded else
                                "勾选此资源" if selectable else "此资源不可下载")
                item.setData(0, Qt.ItemDataRole.AccessibleTextRole,
                             "已下载，不可勾选" if downloaded else "可勾选下载" if selectable else "不可下载")
                item.setData(0, Qt.ItemDataRole.UserRole + 4, selection_state)
            item.setText(6, status)
            item.setToolTip(6, status)
            item.setForeground(6, QColor(TOKENS["text"]))
            item.setData(6, Qt.ItemDataRole.UserRole + 2, display_status)
            item.setData(1, Qt.ItemDataRole.AccessibleTextRole,
                         f"{item.text(1)}，{item.text(3)}，{item.text(5)}，{status}")
        if self._detail_resource_id == resource_id:
            self._update_detail()

    def _update_summary(self) -> None:
        visible_ids = {resource_id for resource_id, item in self.items.items() if not item.isHidden()}
        selected = {key for key in self.checked if key in self.resources and self._can_download(self.resources[key])}
        hidden = len(selected - visible_ids)
        summary = f"显示 {len(visible_ids)} / {len(self.resources)} 项  已勾选 {len(selected)} 项"
        if hidden:
            summary += f"（隐藏 {hidden} 项）"
        self.summary_text.setText(summary)
        self.visible_text.setText(f"显示 {len(visible_ids)} / {len(self.resources)} 项" if self._has_loaded else "尚未获取资源")
        self.selection_text.setText(f"已选 {len(selected)} 项 · 隐藏 {hidden} 项")
        self.selection_text.setAccessibleName(f"已选 {len(selected)} 项，其中隐藏 {hidden} 项；下载包含全部已选项目，跳过已下载内容")
        self.download_button.setText(f"下载已选 {len(selected)} 项")
        self.download_button.setAccessibleName(f"下载全部已选 {len(selected)} 项，包括隐藏 {hidden} 项；已下载内容会跳过")
        self.download_button.setEnabled(bool(selected) and not self._busy and not self._closing)
        selectable_ids = {key for key in visible_ids if self._can_download(self.resources[key])}
        for button in (self.bulk_all_button, self.bulk_none_button, self.bulk_invert_button):
            button.setEnabled(bool(selectable_ids))
        with QSignalBlocker(self.header_check):
            self.header_check.setEnabled(bool(selectable_ids))
            count = len(selected & selectable_ids)
            state = Qt.CheckState.Checked if count and count == len(selectable_ids) else Qt.CheckState.PartiallyChecked if count else Qt.CheckState.Unchecked
            self.header_check.setCheckState(state)

    @Slot(QTreeWidgetItem, int)
    def _item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column == 0:
            resource_id = item.data(0, Qt.ItemDataRole.UserRole)
            if not self._can_download(self.resources[resource_id]):
                self.checked.discard(resource_id)
                with QSignalBlocker(self.tree):
                    item.setCheckState(0, Qt.CheckState.Unchecked)
            elif item.checkState(0) == Qt.CheckState.Checked:
                self.checked.add(resource_id)
            else:
                self.checked.discard(resource_id)
            self._update_summary()

    @Slot(QTreeWidgetItem, int)
    def _double_click(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0:
            self.show_resource_details(item.data(0, Qt.ItemDataRole.UserRole))

    @Slot()
    def _space_toggle(self) -> None:
        for item in self.tree.selectedItems():
            if not self._can_download(self.resources[item.data(0, Qt.ItemDataRole.UserRole)]):
                continue
            item.setCheckState(0, Qt.CheckState.Unchecked if item.checkState(0) == Qt.CheckState.Checked else Qt.CheckState.Checked)

    def _bulk_check(self, mode: str) -> None:
        ids = {resource.id for resource in self._visible() if self._can_download(resource)}
        if mode == "all":
            self.checked.update(ids)
        elif mode == "none":
            self.checked.difference_update(ids)
        else:
            self.checked.symmetric_difference_update(ids)
        self._render()

    def _load_resources(self, resources: list[Resource], replace: bool) -> None:
        self._startup_timer.stop()
        self._startup_detection_enabled = False
        self._history_generation += 1
        self._has_loaded = True
        self._scan_failed = False
        if replace:
            self.resources.clear()
            self.items.clear()
            self.checked.clear()
            self.statuses.clear()
            self._downloaded.clear()
            self.tree.clear()
        for resource in resources:
            if resource.id in self._downloaded:
                self._downloaded.pop(resource.id)
                self.statuses.pop(resource.id, None)
            self.resources[resource.id] = resource
        self._rebuild_scope_tabs()
        self._render()
        self._schedule_history_refresh()

    def _download_summary(self) -> None:
        if self._download_total <= 0:
            return
        fraction = len(self._finished_downloads)
        fraction += sum(min(1, done / total) for key, (done, total) in self._resource_progress.items()
                        if key not in self._finished_downloads and total > 0)
        self._set_progress(min(100, int(100 * fraction / self._download_total)))
        if not self._closing and not self.worker.cancel_event.is_set():
            self.progress_text.setText(f"下载：已完成 {len(self._finished_downloads)} / {self._download_total} 项，"
                                       f"进行中 {len(self._active_downloads)} 项。")

    def _queue_download_update(self, resource_id: str) -> None:
        self._pending_status_updates.add(resource_id)
        if not self._download_update_timer.isActive():
            self._download_update_timer.start()

    @Slot()
    def _flush_download_updates(self) -> None:
        self._download_update_timer.stop()
        if not self._pending_status_updates:
            return
        pending = self._pending_status_updates
        self._pending_status_updates = set()
        for resource_id in pending:
            self._update_resource_status(resource_id)
        if self._selection_update_pending:
            self._selection_update_pending = False
            self._update_summary()
        self._download_summary()

    @Slot(object)
    def _handle_event(self, event: WorkerEvent) -> None:
        data, kind = event.data, event.kind
        if kind == "login_detected":
            self.session_text.setText("已检测到 ClassIn 登录")
            self.session_text.setToolTip("")
            self.progress_text.setText("登录状态已确认，点击“获取资源”开始整理课程资料。")
        elif kind == "login_failed":
            self.session_text.setText("未检测到有效 ClassIn 登录")
            self.session_text.setToolTip(data["message"])
            self.progress_text.setText(data["message"])
            self._message(data["message"])
        elif kind == "login_cancelled":
            self.session_text.setText("已取消登录检测")
            self.progress_text.setText("已取消登录检测；登录 ClassIn 后可点击“获取资源”。")
        elif kind == "scan_finished":
            result: ScanResult = data["result"]
            self._history_auto_enabled = True
            self._set_scan_warnings(result.warnings)
            self._load_resources(result.resources, replace=True)
            self.session_text.setText(readable_text(result.session_status or "扫描完成"))
            self.session_text.setToolTip("")
            self.progress_text.setText(f"扫描完成，发现 {len(result.resources)} 个资源。")
            self._message("\n".join(result.warnings) if result.warnings else "扫描完成。下载时会刷新资源地址。", replace=True)
        elif kind == "import_finished":
            resources = data["resources"]
            self._history_auto_enabled = True
            self._load_resources(resources, replace=False)
            self.progress_text.setText(f"已导入 {len(resources)} 个资源，当前共 {len(self.resources)} 项。")
            self._message("已补充导入Json Manifest")
        elif kind == "history_finished":
            if (data["generation"] != self._history_generation or data["output_dir"] != self._output_path()):
                return
            self._clear_downloaded()
            self._downloaded = {resource_id: existing for resource_id, existing in data["downloaded"].items()
                                if resource_id in self.resources and selectable_resource(self.resources[resource_id])}
            self._download_output_root = data["output_dir"]
            self.checked.difference_update(self._downloaded)
            for resource_id in self._downloaded:
                self.statuses[resource_id] = self._resource_status(self.resources[resource_id])
            self._render()
            self.progress_text.setText(f"已检查 {len(self.resources)} 项，已下载 {len(self._downloaded)} 项；全选会跳过。")
            warnings = {existing.warning for existing in self._downloaded.values() if existing.warning}
            for warning in sorted(warnings):
                self._message(warning)
        elif kind == "history_cancelled":
            if data["generation"] == self._history_generation:
                self.progress_text.setText("已取消检查；下载前仍会核验已有文件。")
        elif kind == "failed":
            self._flush_download_updates()
            self.progress_text.setText(data["message"])
            if data["operation"] == "scan":
                self.session_text.setText("本次扫描未完成。")
                self._scan_failed = True
                self._set_scan_warnings([data["message"]])
                self._render()
            elif data["operation"] == "detect":
                self.session_text.setText("登录检测未完成")
            self._message(data["message"])
        elif kind == "download_started":
            resource_id = data["resource_id"]
            if resource_id in self._finished_downloads:
                return
            self.statuses[resource_id] = "正在获取 / 下载"
            self._active_downloads.add(resource_id)
            self._queue_download_update(resource_id)
        elif kind == "progress":
            resource_id = data["resource_id"]
            if resource_id in self._finished_downloads:
                return
            done, total = data["done"], data["total"]
            self._resource_progress[resource_id] = (done, total)
            self.statuses[resource_id] = f"下载 {done * 100 / total:.0f}%" if total > 0 else f"已接收 {format_size(done)}"
            self._queue_download_update(resource_id)
        elif kind in ("download_warning", "download_failed"):
            resource_id = data["resource_id"]
            if kind == "download_failed":
                self.statuses[resource_id] = "下载失败"
                self._active_downloads.discard(resource_id)
                self._finished_downloads.add(resource_id)
                self._queue_download_update(resource_id)
            self._message(f"{readable_text(self.resources[resource_id].name, 100)}：{data['message']}")
        elif kind == "download_finished":
            download_result: DownloadResult = data["result"]
            resource_id = data["resource_id"]
            self._active_downloads.discard(resource_id)
            self._finished_downloads.add(resource_id)
            suffix = download_source_label(self.resources.get(resource_id), download_result.source)
            if self._active_download_output_root == self._output_path():
                self._download_output_root = self._active_download_output_root
                self._downloaded[resource_id] = ExistingDownload(download_result.path, download_result.bytes_written,
                                                               download_result.sha256, download_result.source,
                                                               download_result.warning)
                self.statuses[resource_id] = f"已下载 · {suffix}"
                self.checked.discard(resource_id)
                self._selection_update_pending = True
            self._message(f"{'已下载，跳过' if download_result.skipped else '已下载'}：{Path(download_result.path).name}"
                          f"（{format_size(download_result.bytes_written)}，{suffix}）")
            if download_result.warning:
                self._message(download_result.warning)
            self._queue_download_update(resource_id)
        elif kind == "download_cancelled":
            self._active_downloads.discard(data["resource_id"])
            self._finished_downloads.add(data["resource_id"])
            self.statuses[data["resource_id"]] = "已取消"
            self._queue_download_update(data["resource_id"])
        elif kind == "batch_finished":
            for resource_id, status in self.statuses.items():
                if status == "等待下载":
                    self.statuses[resource_id] = "未开始"
                    self._queue_download_update(resource_id)
            # Flush before the final summary so the timer cannot overwrite it.
            self._flush_download_updates()
            text = f"{'下载已取消' if data['cancelled'] else '批量下载结束'}：成功 {data['completed']} 项，失败 {data['failed']} 项"
            if data.get("skipped", 0):
                text += f"，已下载跳过 {data['skipped']} 项"
            if data["cancelled"]:
                text += f"，取消或未开始 {data['remaining']} 项"
            self.progress_text.setText(text + "。")
            self._message(text + "。")
        elif kind == "worker_finished":
            self._flush_download_updates()
            self._set_progress(0)
            self._set_busy("")
            if self._closing:
                self.close()
            elif self._history_pending:
                self._history_timer.start(0)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._startup_timer.stop()
        self._startup_detection_enabled = False
        self._history_timer.stop()
        self._history_pending = False
        if self.worker.running:
            event.ignore()
            self._closing = True
            self.worker.cancel()
            self.progress_text.setText("正在结束后台任务...")
            self._set_busy(self._busy)
        else:
            self.worker.close_client()
            event.accept()


def main() -> None:
    application = QApplication.instance()
    if not isinstance(application, QApplication):
        application = QApplication(sys.argv)
    application.setApplicationName("ClassInExporter")
    window = ExporterApp()
    window.show()
    application.aboutToQuit.connect(window.deleteLater)
    application.exec()
