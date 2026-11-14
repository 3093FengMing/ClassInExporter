"""Render the real Qt UI using synthetic Chinese resources and no network."""
from __future__ import annotations

import json
import argparse
import os
from pathlib import Path
import shutil
import sys
from typing import NoReturn

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_SCALE_FACTOR"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests
from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import QApplication, QDialog

from classin_exporter.gui import BackgroundWorker, ExporterApp, WorkerEvent
from classin_exporter.download_history import ExistingDownload
from classin_exporter.resources import Resource


def block_external_work(*args: object, **kwargs: object) -> NoReturn:
    raise RuntimeError("This synthetic preview never performs network or discovery work")


def preview_resources() -> list[Resource]:
    """The exact synthetic content approved in the course-library concept."""
    return [
        Resource("preview-material", "示例课堂讲义_第06章_函数与方程（修订版）.pdf", "file", "course",
                 size=512 * 1024, course="示例课程 · 数学", metadata={"adapter": "lms-material"}),
        Resource("preview-video", "示例原始录播_第06讲_函数与方程.mp4", "video", "course-record",
                 size=128 * 1024 * 1024, course="示例课程 · 数学",
                 metadata={"adapter": "lms-record", "original_available": True}),
        Resource("preview-complete-cache", "示例英语词汇表_Unit 08_课堂补充.pdf", "file", "cache-index",
                 size=256 * 1024, course="示例课程 · 英语",
                 metadata={"cache_complete": True, "cache_status": "完整缓存"}),
        Resource("preview-partial-cache", "示例实验步骤_探究报告与操作说明.docx", "file", "cache-index",
                 size=2 * 1024 * 1024, course="示例课程 · 化学",
                 metadata={"cache_complete": False, "cache_status": "缓存完整性未确认"}),
        Resource("preview-exercise", "示例课后练习_第06章_答案与解析.pdf", "file", "local",
                 size=round(1.2 * 1024 * 1024)),
        Resource("preview-denied-replay", "示例课堂回放权限候选", "replay", "replay",
                 course="示例课程 · 数学", metadata={"candidate_only": True, "availability": "denied",
                                                   "record_exists": None, "last_errno": 809,
                                                   "status": "权限不足（809），录像存在性未确认"}),
    ]


def preview_warnings() -> list[str]:
    return [f"{course}：课程云盘补充目录未确认（HTTP 404），保留已发现资源。"
            for course in ("示例课程 · 数学", "示例课程 · 英语", "示例课程 · 化学", "示例归档课程")]


def inject_download_state(window: ExporterApp, state: str) -> None:
    """Replay real UI events without starting a transport or a worker job."""
    ids = ["preview-material", "preview-video", "preview-partial-cache"]
    window._download_total = len(ids)
    window._active_downloads.clear()
    window._finished_downloads.clear()
    window._resource_progress.clear()
    window.statuses.update({resource_id: "等待下载" for resource_id in ids})
    window._set_busy("download")
    window._render()
    for index, resource_id in enumerate(ids[:2], 1):
        window._handle_event(WorkerEvent("download_started", {"resource_id": resource_id, "index": index, "total": len(ids)}))
        total = window.resources[resource_id].size
        window._handle_event(WorkerEvent("progress", {"resource_id": resource_id,
                                                      "done": int(total * (0.42 if index == 1 else 0.28)), "total": total}))
    if state == "canceling":
        # This exercises the real cancel UI path. No operation thread exists.
        window.cancel()
    elif state == "failed":
        for resource_id in ids:
            window._handle_event(WorkerEvent("download_failed", {"resource_id": resource_id,
                "message": "合成失败状态：请求失败（HTTP 503），检查网络后可重试。"}))
        window._handle_event(WorkerEvent("batch_finished", {"completed": 0, "failed": 3,
                                                            "remaining": 0, "cancelled": False}))
        window._handle_event(WorkerEvent("worker_finished", {"operation": "download"}))
    window._flush_download_updates()
    window.session_text.setText("合成任务状态 · 未发起下载")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("qt-preview.png"))
    parser.add_argument("--metadata-output", type=Path)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=800)
    parser.add_argument("--export-label", default="exports", help="Displayed export directory in the synthetic preview")
    parser.add_argument("--state", choices=("main", "hidden", "permission", "empty", "settings", "tasks", "focus",
                                            "small", "downloaded", "downloading", "canceling", "failed", "stress"), default="main")
    args = parser.parse_args()
    # A hard guard protects this standalone preview even if a future GUI change
    # accidentally starts a scan or request. No real client is constructed.
    requests.Session.request = block_external_work
    application = QApplication.instance()
    if not isinstance(application, QApplication):
        application = QApplication([])
    application.setQuitOnLastWindowClosed(False)
    worker = BackgroundWorker(client_factory=block_external_work,
                              manager_factory=block_external_work,
                              manifest_reader=block_external_work)
    window = ExporterApp(worker=worker, auto_detect=False)
    if args.state == "small" and (args.width, args.height) == (1280, 800):
        args.width, args.height = 960, 680
    window.resize(args.width, args.height)
    resources = preview_resources()
    if args.state == "stress":
        resources[0].name = "示例课堂讲义_长中文名称_函数与方程_" * 30 + "（最终修订版）.pdf"
        resources[0].metadata["cache_status"] = "完整缓存（长度与摘要已验证）；" * 20
    window._load_resources(resources, replace=True)
    window.session_text.setText("已检测到 ClassIn 登录")
    window.output_dir.setText(args.export_label)
    # Model one verified local export in memory; this fixture never reads files.
    downloaded_id = "preview-complete-cache"
    window._downloaded[downloaded_id] = ExistingDownload("示例已下载资源.pdf", resources[2].size, "0" * 64, "cache")
    window._download_output_root = window._output_path()
    window.checked.update({"preview-material", "preview-video", "preview-partial-cache"})

    # Exercise the real combined filters, then show all resource categories in
    # the final frame while retaining a visible, active name filter.
    window.course_filter.setCurrentText("示例课程 · 数学")
    window.type_filter.setCurrentText("视频素材")
    window.name_filter.setText("原始录播")
    assert [row.id for row in window._visible()] == ["preview-video"]
    window.course_filter.setCurrentText("全部课程")
    window.type_filter.setCurrentText("全部类型")
    window.name_filter.setText("示例")
    window._render()
    candidate = window.items["preview-denied-replay"]
    assert not candidate.flags() & Qt.ItemFlag.ItemIsUserCheckable
    assert candidate.checkState(0) == Qt.CheckState.Unchecked
    assert window.items[downloaded_id].text(6) == "已下载 · 完整缓存"
    assert not window.items[downloaded_id].flags() & Qt.ItemFlag.ItemIsUserCheckable
    assert window.items[downloaded_id].checkState(0) == Qt.CheckState.Unchecked
    assert not worker.running

    window._set_scan_warnings(preview_warnings())
    window._message("\n".join(preview_warnings()), replace=True)
    window.tree.setCurrentItem(window.items["preview-material"])
    window.progress_text.setText("合成设计数据")
    if args.state == "hidden":
        window.course_filter.setCurrentText("示例课程 · 数学")
        window.name_filter.setText("原始录播")
    elif args.state == "permission":
        window.type_filter.setCurrentText("课堂回放")
        window.tree.setCurrentItem(candidate)
    elif args.state == "empty":
        window.name_filter.setText("不存在的资源名称")
    elif args.state == "downloaded":
        window._bulk_check("all")
        assert downloaded_id not in window.checked
        assert "preview-denied-replay" not in window.checked
        window.tree.setCurrentItem(window.items[downloaded_id])
        window.progress_text.setText("已下载内容会在全选和批量下载时跳过")
    elif args.state in {"downloading", "canceling", "failed"}:
        inject_download_state(window, args.state)
    window.show()
    application.processEvents()
    application.processEvents()
    dialog = None
    if args.state == "settings":
        window.show_settings()
    elif args.state == "tasks":
        window.show_task_details()
    application.processEvents()
    if args.state in {"settings", "tasks"}:
        visible_dialogs = [item for item in window.findChildren(QDialog) if item.isVisible()]
        if len(visible_dialogs) != 1:
            raise RuntimeError(f"Expected one visible {args.state} dialog, got {len(visible_dialogs)}")
        dialog = visible_dialogs[0]
    if args.state == "main":
        focused = QApplication.focusWidget()
        if focused is not None:
            focused.clearFocus()
            application.processEvents()
    elif args.state == "focus":
        window.name_filter.setFocus()
        application.processEvents()
    screenshot = (dialog or window).grab()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    default = Path(__file__).with_name("qt-preview.png").resolve()
    backup = Path(__file__).resolve().parents[1] / "analysis" / "design" / "public-preview-before-cleanup.png"
    if output == default and default.is_file() and not backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(default, backup)
    if not screenshot.save(str(output), "PNG"):
        raise RuntimeError("Could not save Qt preview")
    project = Path(__file__).resolve().parents[1]
    public_name = output.relative_to(project).as_posix() if output.is_relative_to(project) else output.name
    current_item = window.tree.currentItem()
    metadata = {"file": public_name, "width": screenshot.width(), "height": screenshot.height(),
                      "synthetic_resource_count": len(resources), "checked_count": len(window.checked),
                      "visible_ids": [row.id for row in window._visible()],
                      "selected_id": current_item.data(0, Qt.ItemDataRole.UserRole) if current_item is not None else None,
                      "candidate_checkable": False, "network_requests": 0,
                      "downloaded_ids": sorted(window._downloaded),
                      "downloaded_checkable": bool(window.items[downloaded_id].flags() & Qt.ItemFlag.ItemIsUserCheckable),
                      "worker_running": worker.running, "busy_state": window._busy,
                      "cancel_requested": worker.cancel_event.is_set(),
                      "supplemental_unconfirmed_count": len(preview_warnings()), "state": args.state,
                      "logical_viewport": [window.width(), window.height()],
                      "requested_viewport": [args.width, args.height],
                      "capture_logical_size": [round(screenshot.width() / screenshot.devicePixelRatio()),
                                               round(screenshot.height() / screenshot.devicePixelRatio())],
                      "device_pixel_ratio": screenshot.devicePixelRatio()}
    if args.metadata_output is not None:
        args.metadata_output.parent.mkdir(parents=True, exist_ok=True)
        args.metadata_output.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False))
    if dialog is not None:
        dialog.close()
    # Simulated busy/cancel state must never keep the standalone preview open.
    worker.cancel_event.clear()
    window._set_busy("")
    window.close()
    window.deleteLater()
    worker.deleteLater()
    application.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    application.processEvents()


if __name__ == "__main__":
    main()
