import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

from config import (
    MAIN_EXE_BASENAME,
    OTA_HTTP_USER_AGENT,
    OTA_TEMP_DIR_NAME,
    UPDATER_EXE_BASENAME,
    UPDATE_ERROR_CAPTION,
    UPDATE_WINDOW_TITLE,
)
from ota.github import (
    UpdateInfo,
    fetch_latest_release_json,
    load_repo_from_embedded_source,
    normalize_github_repo,
    parse_update_info,
)
from theme_support import ota_theme_colors, prefers_light_theme

class OTALocalCancelled(Exception):
    """Raised when the user cancels an OTA download in the progress dialog."""


class UpdateManager:
    """
    GitHub Releases lookup, manifest + ZIP download with sha256 check,
    and spawning ``ota_updater.exe`` to replace the installed app folder.
    """

    def __init__(
        self,
        *,
        github_repo: str,
        current_version: str,
        config_dir: Path,
        github_api_url_template: str = "https://api.github.com/repos/{repo}/releases/latest",
        manifest_asset_name: str | None = None,
        timeout_s: int = 20,
    ):
        self.github_repo = (github_repo or "").strip()
        self.current_version = current_version
        self.config_dir = Path(config_dir)
        self.github_api_url_template = github_api_url_template
        if manifest_asset_name:
            self.manifest_asset_name = manifest_asset_name
        else:
            # Choose the platform-specific manifest asset:
            # - Windows -> manifest-win.json
            # - Linux -> manifest-linux.json
            # - macOS -> manifest-macos.json
            self.manifest_asset_name = (
                "manifest-win.json"
                if sys.platform.startswith("win")
                else "manifest-linux.json"
                if sys.platform.startswith("linux")
                else "manifest-macos.json"
                if sys.platform.startswith("darwin")
                else "manifest-win.json"
            )
        self.timeout_s = timeout_s
        self.log_file = self.config_dir / "ota.log"

    def _write_log(self, msg: str):
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(msg.rstrip() + "\n")
        except Exception:
            pass

    def _download_to_file(
        self,
        url: str,
        dest_path: Path,
        *,
        cancel_check=None,
        progress_dialog=None,
        label_prefix: str | None = None,
    ):
        headers = {"User-Agent": OTA_HTTP_USER_AGENT}
        timeout = (10, max(60, int(self.timeout_s) * 6))
        written = 0
        total = None
        with requests.get(url, headers=headers, stream=True, timeout=timeout) as r:
            r.raise_for_status()
            try:
                cl = r.headers.get("Content-Length")
                if cl:
                    total = int(cl)
            except Exception:
                total = None

            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with open(dest_path, "wb") as f:
                if progress_dialog is not None and total:
                    progress_dialog.setRange(0, total)
                    progress_dialog.setValue(0)
                elif progress_dialog is not None:
                    progress_dialog.setRange(0, 0)

                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    if cancel_check and cancel_check():
                        raise OTALocalCancelled()
                    f.write(chunk)
                    written += len(chunk)

                    if progress_dialog is not None and total:
                        progress_dialog.setValue(min(written, total))
                        if label_prefix:
                            percent = int((written * 100) / total)
                            progress_dialog.setLabelText(f"{label_prefix} {percent}%")

    def _sha256_file(self, path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                h.update(block)
        return h.hexdigest()

    def _get_update_info(self) -> UpdateInfo | None:
        repo = self.github_repo or load_repo_from_embedded_source(write_log=self._write_log) or ""
        repo = normalize_github_repo(repo) or ""
        if not repo:
            return None

        release = fetch_latest_release_json(
            repo,
            api_url_template=self.github_api_url_template,
            timeout_s=self.timeout_s,
            write_log=self._write_log,
        )
        if not release:
            return None

        info = parse_update_info(
            release,
            current_version=self.current_version,
            manifest_asset_name=self.manifest_asset_name,
            timeout_s=self.timeout_s,
            write_log=self._write_log,
        )
        return info

    def prompt_and_update(self, parent_widget=None) -> bool:
        try:
            from PyQt6.QtCore import Qt
            from PyQt6.QtWidgets import (
                QApplication,
                QDialog,
                QHBoxLayout,
                QLabel,
                QMessageBox,
                QProgressDialog,
                QPushButton,
                QVBoxLayout,
            )
        except Exception as e:
            self._write_log(f"[prompt_and_update] PyQt6 import failed: {e}")
            return False

        raw_repo_input = self.github_repo
        resolved_repo = raw_repo_input or load_repo_from_embedded_source(write_log=self._write_log) or ""
        resolved_repo = normalize_github_repo(resolved_repo) or ""

        self._write_log(
            f"[startup_check] current_version={self.current_version!r} "
            f"github_repo={resolved_repo!r}"
        )

        info = self._get_update_info()
        if not info:
            self._write_log("[startup_check] no update available")
            return False

        current_version = self.current_version
        latest_version = info.latest_version
        is_light = prefers_light_theme()
        colors = ota_theme_colors(is_light)

        class UpdatePromptDialog(QDialog):
            def __init__(self):
                super().__init__(parent_widget)
                self._accepted = False
                self.setWindowTitle(UPDATE_WINDOW_TITLE)
                self.setWindowModality(Qt.WindowModality.WindowModal)
                self.setWindowFlags(
                    Qt.WindowType.Dialog
                    | Qt.WindowType.WindowTitleHint
                    | Qt.WindowType.WindowCloseButtonHint
                    | Qt.WindowType.WindowSystemMenuHint
                    | Qt.WindowType.MSWindowsFixedSizeDialogHint
                )
                self.setMinimumWidth(460)
                self.setMinimumHeight(200)

                headline = QLabel("An update is available.")
                headline.setStyleSheet(f"color:{colors['text']}; font-size: 13.5pt; font-weight: 800;")

                informative = (
                    f"Current: {current_version}\n"
                    f"Latest:  {latest_version}\n\n"
                    "Download and update now?"
                )
                info_lbl = QLabel(informative)
                info_lbl.setWordWrap(True)
                info_lbl.setStyleSheet(f"color:{colors['muted']}; font-size: 11pt; font-weight: 600;")

                yes_btn = QPushButton("Yes")
                yes_btn.setObjectName("yesBtn")
                no_btn = QPushButton("No")
                no_btn.setObjectName("noBtn")

                def _accept_update():
                    self._accepted = True
                    self.accept()

                yes_btn.clicked.connect(_accept_update)
                no_btn.clicked.connect(self.reject)

                btn_row = QHBoxLayout()
                btn_row.setSpacing(16)
                btn_row.addStretch(1)
                btn_row.addWidget(yes_btn)
                btn_row.addWidget(no_btn)
                btn_row.addStretch(1)

                root = QVBoxLayout(self)
                root.setContentsMargins(18, 18, 18, 14)
                root.setSpacing(10)
                root.addWidget(headline)
                root.addWidget(info_lbl)
                root.addLayout(btn_row)

                self.setStyleSheet(
                    (
                        "QDialog {"
                        f"background-color: {colors['dialog_bg']};"
                        f"color: {colors['text']};"
                        f"border: 1px solid {colors['border']};"
                        "border-radius: 10px;"
                        "}"
                        "QPushButton#yesBtn {"
                        f"background-color: {colors['chunk']};"
                        "color: #ffffff;"
                        "border: 0px;"
                        "border-radius: 10px;"
                        "padding: 9px 26px;"
                        "font-weight: 800;"
                        "min-width: 120px;"
                        "}"
                        "QPushButton#noBtn {"
                        f"background-color: {colors['secondary_bg']};"
                        "color: #ffffff;"
                        f"border: 1px solid {colors['border']};"
                        "border-radius: 10px;"
                        "padding: 9px 26px;"
                        "font-weight: 800;"
                        "min-width: 120px;"
                        "}"
                        "QPushButton:pressed {"
                        "transform: translateY(1px);"
                        "}"
                    )
                )
                self.adjustSize()
                self.setFixedSize(self.size())

            def keyPressEvent(self, event):
                if event.key() == Qt.Key.Key_Escape:
                    self.reject()
                    event.accept()
                    return
                super().keyPressEvent(event)

            def closeEvent(self, event):
                self._accepted = False
                self.reject()
                event.accept()

        dlg = UpdatePromptDialog()
        dlg.exec()

        if not dlg._accepted:
            return False

        progress = QProgressDialog("Preparing update...", "Cancel", 0, 0, parent_widget)
        progress.setWindowTitle(UPDATE_WINDOW_TITLE)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.MSWindowsFixedSizeDialogHint
        )
        progress.setMinimumDuration(0)
        progress.setAutoClose(True)
        progress.setAutoReset(False)
        progress.setMinimumWidth(480)
        progress.setMinimumHeight(210)
        progress.setStyleSheet(
            f"""
QProgressDialog {{
  background-color: {colors['progress_bg']};
  color: {colors['text']};
  border: 1px solid {colors['border']};
  border-radius: 14px;
  padding: 22px;
}}
QLabel {{
  color: {colors['muted']};
  font-size: 14pt;
  font-weight: 800;
  margin-bottom: 10px;
}}
QPushButton {{
  border-radius: 12px;
  padding: 10px 26px;
  font-weight: 700;
  color: {colors['text']};
  border: 1px solid {colors['cancel_border']};
  background-color: {colors['cancel_bg']};
}}
QProgressBar {{
  height: 16px;
  border-radius: 8px;
  background-color: {colors['progress_bar_bg']};
  border: 1px solid {colors['border']};
  margin: 8px 0px 18px 0px;
  qproperty-alignment: AlignCenter;
  font-size: 12pt;
  qproperty-textVisible: false;
}}
QProgressBar::chunk {{
  border-radius: 6px;
  background-color: {colors['chunk']};
}}
"""
        )
        progress.show()

        cancelled = {"value": False}

        def _cancel_check() -> bool:
            return cancelled["value"] or progress.wasCanceled()

        def _on_cancel():
            cancelled["value"] = True
            progress.setLabelText("Cancelling...")

        try:
            progress.canceled.connect(_on_cancel)
        except Exception:
            pass

        try:
            def _on_finished():
                cancelled["value"] = progress.wasCanceled()

            progress.finished.connect(_on_finished)
        except Exception:
            pass

        try:
            tmp_dir = Path(tempfile.gettempdir()) / OTA_TEMP_DIR_NAME
            zip_path = tmp_dir / f"update_{info.latest_version}.zip"
            if zip_path.exists():
                try:
                    zip_path.unlink()
                except Exception:
                    pass

            progress.setLabelText("Downloading update...")
            progress.setRange(0, 0)
            self._write_log(f"[download] url={info.zip_url}")
            QApplication.processEvents()
            self._download_to_file(
                info.zip_url,
                zip_path,
                cancel_check=_cancel_check,
                progress_dialog=progress,
                label_prefix="Downloading update...",
            )

            if progress.wasCanceled():
                self._write_log("[download] cancelled by user")
                return False

            progress.setLabelText("Verifying download...")
            QApplication.processEvents()

            self._write_log("[download] verifying sha256...")
            QApplication.processEvents()
            actual = self._sha256_file(zip_path)
            if actual.lower() != info.sha256.lower():
                QMessageBox.critical(
                    parent_widget,
                    UPDATE_ERROR_CAPTION,
                    "Update download verification failed (sha256 mismatch).",
                )
                return False

            installed_dir = Path(os.path.dirname(sys.executable))
            if sys.platform.startswith("windows"):
                updater_exe = installed_dir / (UPDATER_EXE_BASENAME + '.exe')
            else:
                updater_exe = installed_dir / UPDATER_EXE_BASENAME
            if not updater_exe.exists():
                QMessageBox.critical(
                    parent_widget,
                    UPDATE_ERROR_CAPTION,
                    f"Updater executable not found: {updater_exe}",
                )
                return False

            if sys.platform.startswith("windows"):
                main_exe = installed_dir / (MAIN_EXE_BASENAME + '.exe')
            else:
                main_exe = installed_dir / MAIN_EXE_BASENAME
            log_path = str(self.log_file)
            subprocess.Popen(
                [
                    str(updater_exe),
                    "--target-dir",
                    str(installed_dir),
                    "--update-zip",
                    str(zip_path),
                    "--main-exe",
                    str(main_exe),
                    "--log-file",
                    log_path,
                ],
                close_fds=True,
                cwd=str(installed_dir),
            )
            return True
        except OTALocalCancelled:
            self._write_log("[download] cancelled by user (exception)")
            return False
        except Exception as e:
            self._write_log(f"[prompt_and_update] failed: {e}")
            QMessageBox.critical(parent_widget, UPDATE_ERROR_CAPTION, str(e))
            return False
        finally:
            try:
                progress.close()
            except Exception:
                pass
