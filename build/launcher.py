"""Portable executable entry point and an explicitly invoked offline smoke check."""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from functools import partial
from typing import Callable


if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def self_test(output: Path) -> int:
    """Exercise packaged dependencies without opening a native login session."""
    result: dict[str, object] = {"ok": False, "frozen": bool(getattr(sys, "frozen", False))}
    application = window = None
    flush_deferred_deletes: Callable[[], None] | None = None
    try:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ.pop("IMAGEIO_FFMPEG_EXE", None)
        from PySide6.QtCore import QCoreApplication, QEvent
        flush_deferred_deletes = partial(QCoreApplication.sendPostedEvents, None,
                                        QEvent.Type.DeferredDelete)
        result["qt_core"] = True
        from PySide6.QtWidgets import QApplication
        result["qt_widgets"] = True
        import capstone
        result["capstone_import"] = True
        import certifi
        result["certifi_import"] = True
        import imageio_ffmpeg
        result["imageio_ffmpeg_import"] = True
        import pefile
        result["pefile_import"] = True
        import requests
        result["requests_import"] = True
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        result["cryptography_import"] = True
        from classin_exporter.gui import ExporterApp
        result["gui_import"] = True

        adapter_modules = (
            "classin_exporter.discovery", "classin_exporter.session",
            "classin_exporter.lms_record", "classin_exporter.lms_material",
            "classin_exporter.lms_homework",
        )
        for module_name in adapter_modules:
            importlib.import_module(module_name)
        result["dynamic_adapters"] = True
        result["requests"] = bool(requests.Session)
        certificate = Path(certifi.where())
        if not certificate.is_file():
            raise RuntimeError("Certificate bundle was not packaged")
        result["certificate_bundle"] = str(certificate)

        decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
        if [instruction.mnemonic for instruction in decoder.disasm(b"\xc3", 0x1000)] != ["ret"]:
            raise RuntimeError("The capstone native library did not load")
        result["capstone"] = True
        executable = Path(sys.executable)
        portable_image = pefile.PE(str(executable), fast_load=True)
        try:
            if getattr(portable_image.FILE_HEADER, "Machine", None) != 0x8664:
                raise RuntimeError("The executable is not Windows x64")
        finally:
            portable_image.close()
        result["pefile"] = True
        cipher = Cipher(algorithms.AES(b"\0" * 16), modes.CBC(b"\0" * 16)).encryptor()
        if (cipher.update(b"\0" * 16) + cipher.finalize()).hex() != "66e94bd4ef8a2c3b884cfa59ca342b2e":
            raise RuntimeError("The cryptography native extension did not load")
        result["cryptography"] = True

        ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve()
        bundled_binaries = (Path(imageio_ffmpeg.__file__).resolve().parent / "binaries").resolve()
        if not ffmpeg.is_file() or ffmpeg.parent != bundled_binaries:
            raise RuntimeError("The bundled FFmpeg executable was not selected")
        ffmpeg_process = subprocess.run(
            [str(ffmpeg), "-version"], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if not ffmpeg_process.stdout.startswith(b"ffmpeg version "):
            raise RuntimeError("The bundled FFmpeg executable did not run")
        result["ffmpeg"] = str(ffmpeg)

        application = QApplication.instance() or QApplication([])
        window = ExporterApp(auto_detect=False)
        window.show()
        application.processEvents()
        result["qt_window"] = bool(window.isVisible())
        result["no_account_scan"] = not window.worker.running
        default_output = Path(window.output_dir.text()).resolve()
        if result["frozen"] and default_output != (executable.parent / "exports").resolve():
            raise RuntimeError("The default export directory is not beside the executable")
        result["default_export_directory"] = str(default_output)
        if not result["qt_window"] or not result["no_account_scan"]:
            raise RuntimeError("The offline Qt window check failed")
        screenshot = output.with_suffix(".png")
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        if not window.grab().save(str(screenshot)):
            raise RuntimeError("The offline Qt screenshot could not be saved")
        result["screenshot"] = str(screenshot.resolve())
        result["ok"] = True
    except Exception as error:
        result["error_type"] = type(error).__name__
    finally:
        if window is not None:
            window.close()
            window.deleteLater()
        if application is not None and flush_deferred_deletes is not None:
            flush_deferred_deletes()
            application.processEvents()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1


def launch() -> None:
    if "--self-test" in sys.argv[1:]:
        parser = argparse.ArgumentParser(description="Run the offline portable bundle smoke check")
        parser.add_argument("--self-test", action="store_true")
        parser.add_argument("--self-test-output", type=Path, required=True)
        arguments = parser.parse_args()
        raise SystemExit(self_test(arguments.self_test_output))
    from classin_exporter.gui import main
    main()


if __name__ == "__main__":
    launch()
