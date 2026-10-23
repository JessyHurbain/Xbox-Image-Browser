"""Interface graphique PyQt6 pour explorer et extraire une image Xbox 360."""

from __future__ import annotations

import hashlib
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import (
    QItemSelectionModel,
    QSortFilterProxyModel,
    QThread,
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QAction,
    QCloseEvent,
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QIcon,
    QKeySequence,
    QStandardItem,
    QStandardItemModel,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from xdvdfs import (
    DirEntry,
    ExtractionCancelled,
    ExtractStats,
    XDVDFSError,
    XDVDFSVolume,
    human_size,
    sanitize_filename,
)

ENTRY_ROLE = int(Qt.ItemDataRole.UserRole)
PATH_ROLE = ENTRY_ROLE + 1
LOADED_ROLE = ENTRY_ROLE + 2

SEARCH_LIMIT = 300


def _asset_path(*parts: str) -> Path:
    """Chemin d'une ressource, y compris dans un exécutable PyInstaller."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base.joinpath("assets", *parts)


@dataclass(frozen=True)
class IndexEntry:
    """Entrée d'index pour la recherche (métadonnées uniquement)."""

    path: str
    name: str
    is_dir: bool
    size: int


@dataclass(frozen=True)
class HashResult:
    path: str
    size: int
    md5: str
    sha1: str


class IndexWorker(QThread):
    """Indexe toute l'arborescence en arrière-plan (lecture des métadonnées)."""

    ready = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self, iso_path: str, partition_offset: int, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._iso_path = iso_path
        self._partition_offset = partition_offset
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True

    def run(self) -> None:
        try:
            with XDVDFSVolume.open(self._iso_path, self._partition_offset) as volume:
                entries: list[IndexEntry] = []
                for parts, entry in volume.walk():
                    if self._cancel_requested:
                        return
                    entries.append(
                        IndexEntry(
                            "/".join(parts), entry.name, entry.is_dir, entry.size
                        )
                    )
            if not self._cancel_requested:
                self.ready.emit(entries)
        except (XDVDFSError, OSError) as exc:
            if not self._cancel_requested:
                self.failed.emit(str(exc))


class HashWorker(QThread):
    """Calcule MD5 + SHA1 des fichiers sélectionnés, par blocs de 1 Mio."""

    progress = pyqtSignal(int, int, str)
    succeeded = pyqtSignal(object)  # (list[HashResult], list[str])
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(
        self,
        iso_path: str,
        partition_offset: int,
        jobs: list[tuple[str, DirEntry]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._iso_path = iso_path
        self._partition_offset = partition_offset
        self._jobs = jobs
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True

    def _collect_files(
        self, volume: XDVDFSVolume
    ) -> list[tuple[str, DirEntry]]:
        files: list[tuple[str, DirEntry]] = []
        for image_path, entry in self._jobs:
            if entry.is_dir:
                for parts, child in volume.walk(entry):
                    if child.is_dir:
                        continue
                    child_path = (
                        "/".join((image_path, *parts)) if image_path else "/".join(parts)
                    )
                    files.append((child_path, child))
            else:
                files.append((image_path, entry))
        return files

    def run(self) -> None:
        try:
            with XDVDFSVolume.open(self._iso_path, self._partition_offset) as volume:
                files = self._collect_files(volume)
                total = sum(entry.size for _, entry in files)
                results: list[HashResult] = []
                errors: list[str] = []
                done = 0
                last_emit = 0.0

                for image_path, entry in files:
                    if self._cancel_requested:
                        raise ExtractionCancelled()
                    md5 = hashlib.md5(usedforsecurity=False)
                    sha1 = hashlib.sha1(usedforsecurity=False)
                    try:
                        for chunk in volume.iter_file(
                            entry, cancel=lambda: self._cancel_requested
                        ):
                            md5.update(chunk)
                            sha1.update(chunk)
                            done += len(chunk)
                            now = time.monotonic()
                            if now - last_emit >= 0.1:
                                self.progress.emit(done, total, image_path)
                                last_emit = now
                    except XDVDFSError as exc:
                        errors.append(f"/{image_path}: {exc}")
                        continue
                    results.append(
                        HashResult(image_path, entry.size, md5.hexdigest(), sha1.hexdigest())
                    )

                self.progress.emit(done, total, "")
                self.succeeded.emit((results, errors))
        except ExtractionCancelled:
            self.cancelled.emit()
        except (XDVDFSError, OSError) as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - le thread ne doit jamais mourir en silence
            self.failed.emit(f"Erreur inattendue : {exc}")


class ExtractWorker(QThread):
    """Extrait une sélection dans un thread dédié (GUI fluide)."""

    progress = pyqtSignal(int, int, str)
    file_progress = pyqtSignal(int, int)
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(
        self,
        iso_path: str,
        partition_offset: int,
        jobs: list[tuple[str, DirEntry]],
        destination: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._iso_path = iso_path
        self._partition_offset = partition_offset
        self._jobs = jobs
        self._destination = Path(destination)
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True

    def _target_for(self, image_path: str) -> Path:
        parts = [sanitize_filename(part) for part in image_path.split("/") if part]
        return self._destination.joinpath(*parts)

    def run(self) -> None:  # noqa: C901 - boucle simple, lisibilité prioritaire
        try:
            with XDVDFSVolume.open(self._iso_path, self._partition_offset) as volume:
                totals = [volume.tree_totals(entry) for _, entry in self._jobs]
                job_totals = [size for size, _ in totals]
                grand_total = sum(job_totals)
                grand_files = sum(count for _, count in totals)
                overall = ExtractStats()
                base = 0

                for (image_path, entry), job_total in zip(self._jobs, job_totals):
                    def report(
                        done: int,
                        _job_total: int,
                        current: str,
                        base: int = base,
                        job_total: int = job_total,
                    ) -> None:
                        self.progress.emit(base + min(done, job_total), grand_total, current)

                    def report_files(  # noqa: ANN001
                        files_done: int, _files_total: int, base: int = base
                    ) -> None:
                        self.file_progress.emit(base + files_done, grand_files)

                    stats = volume.extract(
                        entry,
                        self._target_for(image_path),
                        progress=report,
                        cancel=lambda: self._cancel_requested,
                        file_progress=report_files,
                    )
                    overall.files += stats.files
                    overall.dirs += stats.dirs
                    overall.bytes += stats.bytes
                    overall.errors.extend(stats.errors)
                    base += job_total

                self.progress.emit(grand_total, grand_total, "")
                self.file_progress.emit(grand_files, grand_files)
                self.succeeded.emit(overall)
        except ExtractionCancelled:
            self.cancelled.emit()
        except (XDVDFSError, OSError) as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - le thread ne doit jamais mourir en silence
            self.failed.emit(f"Erreur inattendue : {exc}")


class SearchProxyModel(QSortFilterProxyModel):
    """Filtre l'arbre sur l'ensemble des chemins issus de l'index."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._visible_paths: set[str] | None = None

    def set_visible_paths(self, paths: set[str] | None) -> None:
        self._visible_paths = paths
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent) -> bool:  # noqa: ANN001
        if self._visible_paths is None:
            return True
        model = self.sourceModel()
        if not isinstance(model, QStandardItemModel):
            return True
        source_index = model.index(source_row, 0, source_parent)
        item = model.itemFromIndex(source_index)
        if item is None:
            return False
        return item.data(PATH_ROLE) in self._visible_paths


class HashResultsDialog(QDialog):
    """Tableau copiable des empreintes calculées."""

    def __init__(
        self,
        results: list[HashResult],
        errors: list[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Empreintes MD5 / SHA1")
        self.resize(980, 540)
        self._results = results

        layout = QVBoxLayout(self)
        total = sum(result.size for result in results)
        layout.addWidget(
            QLabel(f"{len(results)} fichier(s) · {human_size(total)}")
        )
        if errors:
            warning = QLabel(f"⚠ {len(errors)} fichier(s) en erreur")
            warning.setStyleSheet("color: #a15c00;")
            layout.addWidget(warning)

        self.table = QTableWidget(len(results), 4, self)
        self.table.setHorizontalHeaderLabels(["Chemin", "Taille", "MD5", "SHA1"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        monospace = QFont("Monospace")
        monospace.setStyleHint(QFont.StyleHint.TypeWriter)

        for row, result in enumerate(results):
            path_item = QTableWidgetItem("/" + result.path)
            path_item.setToolTip("/" + result.path)
            size_item = QTableWidgetItem(human_size(result.size))
            size_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            md5_item = QTableWidgetItem(result.md5)
            sha1_item = QTableWidgetItem(result.sha1)
            md5_item.setFont(monospace)
            sha1_item.setFont(monospace)
            self.table.setItem(row, 0, path_item)
            self.table.setItem(row, 1, size_item)
            self.table.setItem(row, 2, md5_item)
            self.table.setItem(row, 3, sha1_item)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)

        if errors:
            errors_view = QPlainTextEdit("\n".join(errors))
            errors_view.setReadOnly(True)
            errors_view.setMaximumHeight(90)
            layout.addWidget(errors_view)

        buttons = QHBoxLayout()
        copy_button = QPushButton("Copier (TSV)")
        copy_button.setToolTip("Copie toutes les lignes dans le presse-papiers")
        copy_button.clicked.connect(self._copy_to_clipboard)
        close_button = QPushButton("Fermer")
        close_button.clicked.connect(self.accept)
        buttons.addStretch(1)
        buttons.addWidget(copy_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

    def _copy_to_clipboard(self) -> None:
        lines = ["Chemin\tTaille\tMD5\tSHA1"]
        for result in self._results:
            lines.append(
                f"/{result.path}\t{result.size}\t{result.md5}\t{result.sha1}"
            )
        QApplication.clipboard().setText("\n".join(lines))


class MainWindow(QMainWindow):
    """Fenêtre principale : arbre XDVDFS, recherche, extraction, empreintes."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Xbox Image Browser — XDVDFS")
        self.resize(1000, 700)
        self.setAcceptDrops(True)
        icon_file = _asset_path("icon.png")
        if icon_file.is_file():
            self.setWindowIcon(QIcon(str(icon_file)))

        self.volume: XDVDFSVolume | None = None
        self.iso_path: str | None = None
        self.worker: QThread | None = None
        self.index_worker: IndexWorker | None = None
        self._index: list[IndexEntry] | None = None
        self._items: dict[str, QStandardItem] = {}
        self._pending_outcome: tuple[str, object] | None = None
        self._operation_label = "Opération en cours"
        self._files_done = 0
        self._files_total = 0

        self._build_ui()
        self._update_actions()

    # ------------------------------------------------------------------ #
    # Construction de l'interface
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        style = self.style()
        self.folder_icon = style.standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        self.file_icon = style.standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        self.open_icon = style.standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton)
        self.extract_icon = style.standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton)
        self.hash_icon = style.standardIcon(
            QStyle.StandardPixmap.SP_FileDialogInfoView
        )

        self.model = QStandardItemModel(0, 3, self)
        self.model.setHorizontalHeaderLabels(["Nom", "Taille", "Secteur"])
        self.proxy = SearchProxyModel(self)
        self.proxy.setSourceModel(self.model)

        self.tree = QTreeView()
        self.tree.setModel(self.proxy)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setExpandsOnDoubleClick(True)
        self.tree.setSortingEnabled(False)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        self.tree.expanded.connect(self._on_expanded)

        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)

        self.open_button = QPushButton(self.open_icon, "Ouvrir une image ISO…")
        self.open_button.clicked.connect(self.choose_iso)
        self.extract_button = QPushButton(self.extract_icon, "Extraire la sélection…")
        self.extract_button.clicked.connect(self.extract_selection)
        self.extract_all_button = QPushButton("Tout extraire…")
        self.extract_all_button.clicked.connect(self.extract_all)
        self.hash_button = QPushButton(self.hash_icon, "Empreintes MD5/SHA1…")
        self.hash_button.clicked.connect(self.hash_selection)

        self.info_label = QLabel("Aucune image chargée")
        self.info_label.setStyleSheet("color: #666;")

        buttons = QHBoxLayout()
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.extract_button)
        buttons.addWidget(self.extract_all_button)
        buttons.addWidget(self.hash_button)
        buttons.addStretch(1)
        buttons.addWidget(self.info_label)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Rechercher un fichier ou un dossier…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setEnabled(False)
        self.search_edit.setMaximumWidth(360)
        self.search_edit.textChanged.connect(self._apply_search)
        self.search_count = QLabel("")
        self.search_count.setStyleSheet("color: #666;")

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Rechercher :"))
        search_row.addWidget(self.search_edit, 1)
        search_row.addWidget(self.search_count)
        search_row.addStretch(1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setVisible(False)

        self.cancel_button = QPushButton("Annuler")
        self.cancel_button.clicked.connect(self._cancel_operation)
        self.cancel_button.setVisible(False)

        bottom = QHBoxLayout()
        bottom.addWidget(self.progress, 1)
        bottom.addWidget(self.cancel_button)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(buttons)
        layout.addLayout(search_row)
        layout.addWidget(self.tree, 1)
        layout.addLayout(bottom)
        self.setCentralWidget(central)

        self.statusBar().showMessage("Prêt — ouvrez une image .iso Xbox 360")

        # Menu + barre d'outils
        menu = self.menuBar().addMenu("&Fichier")
        self.open_action = QAction(self.open_icon, "&Ouvrir une image ISO…", self)
        self.open_action.setShortcut(QKeySequence.StandardKey.Open)
        self.open_action.triggered.connect(self.choose_iso)
        menu.addAction(self.open_action)

        self.extract_action = QAction(self.extract_icon, "&Extraire la sélection…", self)
        self.extract_action.setShortcut("Ctrl+E")
        self.extract_action.triggered.connect(self.extract_selection)
        menu.addAction(self.extract_action)

        self.extract_all_action = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_DriveHDIcon),
            "&Tout extraire…",
            self,
        )
        self.extract_all_action.triggered.connect(self.extract_all)
        menu.addAction(self.extract_all_action)

        menu.addSeparator()
        quit_action = QAction("&Quitter", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        menu.addAction(quit_action)

        tools_menu = self.menuBar().addMenu("&Outils")
        self.find_action = QAction("&Rechercher…", self)
        self.find_action.setShortcut(QKeySequence.StandardKey.Find)
        self.find_action.triggered.connect(self._focus_search)
        tools_menu.addAction(self.find_action)

        self.hash_action = QAction(
            self.hash_icon, "&Empreintes MD5 / SHA1…", self
        )
        self.hash_action.setShortcut("Ctrl+H")
        self.hash_action.triggered.connect(self.hash_selection)
        tools_menu.addAction(self.hash_action)

        help_menu = self.menuBar().addMenu("&Aide")
        about_action = QAction("À &propos", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)


    def _update_actions(self) -> None:
        loaded = self.volume is not None
        busy = self.worker is not None
        interactive = loaded and not busy
        for widget in (
            self.tree,
            self.extract_button,
            self.extract_all_button,
            self.hash_button,
            self.extract_action,
            self.extract_all_action,
            self.hash_action,
        ):
            widget.setEnabled(interactive)
        self.open_button.setEnabled(not busy)
        self.open_action.setEnabled(not busy)
        self.search_edit.setEnabled(interactive and self._index is not None)

    # ------------------------------------------------------------------ #
    # Ouverture de l'image et index
    # ------------------------------------------------------------------ #
    def choose_iso(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choisir une image Xbox 360",
            "",
            "Images disque (*.iso *.xiso *.img *.000);;Tous les fichiers (*)",
        )
        if path:
            self.open_iso(path)

    def open_iso(self, path: str) -> bool:
        """Charge une image (bloquant : l'analyse ne lit que des métadonnées)."""
        if self.worker is not None:
            return False
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.statusBar().showMessage(f"Analyse de {path}…")
        QApplication.processEvents()
        volume = None
        try:
            volume = XDVDFSVolume.open(path)
            children = volume.list_directory(volume.root)
        except (XDVDFSError, OSError) as exc:
            if volume is not None:
                volume.close()
            QMessageBox.critical(
                self, "Ouverture impossible", f"{Path(path).name}\n\n{exc}"
            )
            self.statusBar().showMessage(
                "Image précédente conservée"
                if self.volume is not None
                else "Aucune image chargée",
                5000,
            )
            return False
        finally:
            QApplication.restoreOverrideCursor()

        if self.volume is not None:
            self.volume.close()
        self.volume = volume
        self.iso_path = path

        self._stop_indexing()
        self.search_edit.blockSignals(True)
        self.search_edit.clear()
        self.search_edit.blockSignals(False)
        self.proxy.set_visible_paths(None)
        self.search_count.setText("")
        self._items.clear()

        self.model.removeRows(0, self.model.rowCount())
        for child in children:
            self._append_entry(self.model.invisibleRootItem(), child, child.name)

        self.setWindowTitle(f"{Path(path).name} — Xbox Image Browser")
        self.info_label.setText(
            f"{volume.format_name} · partition 0x{volume.partition_offset:X} · "
            f"{human_size(volume.file_size)}"
        )
        self.statusBar().showMessage(
            f"{len(children)} élément(s) à la racine — double-cliquez pour développer",
            8000,
        )
        self._update_actions()
        self._start_indexing()
        return True

    def _start_indexing(self) -> None:
        assert self.volume is not None and self.iso_path is not None
        self._index = None
        self.search_edit.setEnabled(False)
        self.search_edit.setPlaceholderText("Indexation de l'image…")
        self.search_count.setText("Indexation…")

        worker = IndexWorker(
            self.iso_path, self.volume.partition_offset, self
        )
        worker.ready.connect(lambda entries, w=worker: self._on_index_ready(w, entries))
        worker.failed.connect(
            lambda message, w=worker: self._on_index_failed(w, message)
        )
        worker.finished.connect(worker.deleteLater)
        self.index_worker = worker
        worker.start()

    def _on_index_ready(self, worker: IndexWorker, entries: list[IndexEntry]) -> None:
        if worker is not self.index_worker:
            return
        self.index_worker = None
        self._index = entries
        self.search_edit.setPlaceholderText(
            "Rechercher un fichier ou un dossier… (Ctrl+F)"
        )
        self.search_edit.setEnabled(self.worker is None)
        self.search_count.setText(f"{len(entries)} entrées indexées")
        self._apply_search(self.search_edit.text())

    def _on_index_failed(self, worker: IndexWorker, message: str) -> None:
        if worker is not self.index_worker:
            return
        self.index_worker = None
        self.search_edit.setPlaceholderText("Indexation indisponible")
        self.search_count.setText("")
        self.statusBar().showMessage(f"Indexation impossible : {message}", 8000)

    def _stop_indexing(self) -> None:
        worker = self.index_worker
        self.index_worker = None
        self._index = None
        self.search_edit.setEnabled(False)
        if worker is not None:
            worker.cancel()
            worker.wait(2000)

    # ------------------------------------------------------------------ #
    # Arbre paresseux
    # ------------------------------------------------------------------ #
    def _append_entry(
        self, parent: QStandardItem, entry: DirEntry, image_path: str
    ) -> None:
        name_item = QStandardItem(entry.name)
        name_item.setEditable(False)
        name_item.setIcon(self.folder_icon if entry.is_dir else self.file_icon)
        name_item.setToolTip(f"/{image_path}" if image_path else entry.name)

        size_item = QStandardItem("Dossier" if entry.is_dir else human_size(entry.size))
        size_item.setTextAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        sector_item = QStandardItem(f"0x{entry.sector:08X}")

        for item in (name_item, size_item, sector_item):
            item.setEditable(False)
            item.setData(entry, ENTRY_ROLE)
            item.setData(image_path, PATH_ROLE)

        if entry.is_dir:
            name_item.setData(False, LOADED_ROLE)
            if entry.size > 0:
                placeholder = QStandardItem("")
                placeholder.setEditable(False)
                name_item.appendRow([placeholder, QStandardItem(""), QStandardItem("")])

        parent.appendRow([name_item, size_item, sector_item])
        self._items[image_path] = name_item

    def _load_directory(self, item: QStandardItem) -> bool:
        """Charge les enfants d'un dossier (une seule fois)."""
        if self.volume is None:
            return False
        entry = item.data(ENTRY_ROLE)
        if not isinstance(entry, DirEntry) or not entry.is_dir:
            return False
        if item.data(LOADED_ROLE):
            return True

        image_path = item.data(PATH_ROLE) or ""
        item.setData(True, LOADED_ROLE)
        item.removeRows(0, item.rowCount())
        try:
            children = self.volume.list_directory(entry)
        except XDVDFSError as exc:
            QMessageBox.warning(self, "Lecture impossible", f"/{image_path}\n\n{exc}")
            return False

        for child in children:
            child_path = f"{image_path}/{child.name}" if image_path else child.name
            self._append_entry(item, child, child_path)
        if not children:
            self.statusBar().showMessage(f"Dossier vide : /{image_path}", 4000)
        return True

    def _ensure_loaded(self, image_path: str) -> None:
        """Charge les dossiers parents d'un chemin (pour la recherche)."""
        parts = image_path.split("/")
        for depth in range(1, len(parts)):
            ancestor = "/".join(parts[:depth])
            item = self._items.get(ancestor)
            if item is None:
                return
            if not item.data(LOADED_ROLE):
                self._load_directory(item)

    def _on_expanded(self, proxy_index) -> None:  # noqa: ANN001
        if proxy_index.column() != 0:
            return
        source_index = self.proxy.mapToSource(proxy_index)
        item = self.model.itemFromIndex(source_index)
        if item is None:
            return
        self._load_directory(item)

    # ------------------------------------------------------------------ #
    # Recherche instantanée
    # ------------------------------------------------------------------ #
    def _focus_search(self) -> None:
        if self.search_edit.isEnabled():
            self.search_edit.setFocus()
            self.search_edit.selectAll()

    def _apply_search(self, text: str) -> None:
        if self.volume is None or self._index is None:
            return
        terms = tuple(part.casefold() for part in text.split() if part)
        if not terms:
            self.proxy.set_visible_paths(None)
            self.search_count.setText("")
            return
        if len(terms) == 1 and len(terms[0]) < 2:
            self.proxy.set_visible_paths(set())
            self.search_count.setText("2 caractères minimum")
            return

        matches = [
            entry
            for entry in self._index
            if all(term in entry.path.casefold() for term in terms)
        ]
        total = len(matches)
        shown = matches[:SEARCH_LIMIT]

        for entry in shown:
            self._ensure_loaded(entry.path)

        ancestors: set[str] = set()
        for entry in shown:
            parts = entry.path.split("/")
            for depth in range(1, len(parts)):
                ancestors.add("/".join(parts[:depth]))
        visible = ancestors | {entry.path for entry in shown}
        self.proxy.set_visible_paths(visible)

        for path in ancestors:
            item = self._items.get(path)
            if item is None:
                continue
            entry = item.data(ENTRY_ROLE)
            if not isinstance(entry, DirEntry) or not entry.is_dir:
                continue
            proxy_index = self.proxy.mapFromSource(item.index())
            if proxy_index.isValid():
                self.tree.expand(proxy_index)

        if total == 0:
            label = "Aucun résultat"
        elif total > SEARCH_LIMIT:
            label = f"{SEARCH_LIMIT} affichés / {total} résultats"
        else:
            label = f"{total} résultat{'s' if total > 1 else ''}"
        self.search_count.setText(label)
        self.statusBar().showMessage(
            f"{total} résultat(s) pour « {text.strip()} »", 4000
        )

    # ------------------------------------------------------------------ #
    # Extraction
    # ------------------------------------------------------------------ #
    def _selection(self) -> list[tuple[str, DirEntry]]:
        """Sélection courante, sans les enfants d'un dossier déjà sélectionné."""
        if self.volume is None:
            return []
        picked: dict[str, DirEntry] = {}
        for proxy_index in self.tree.selectionModel().selectedRows(0):
            item = self.model.itemFromIndex(self.proxy.mapToSource(proxy_index))
            if item is None:
                continue
            entry = item.data(ENTRY_ROLE)
            image_path = item.data(PATH_ROLE)
            if isinstance(entry, DirEntry) and isinstance(image_path, str):
                picked[image_path] = entry

        kept: list[tuple[str, DirEntry]] = []
        for image_path in sorted(picked, key=len):
            if any(
                image_path == parent or image_path.startswith(parent + "/")
                for parent, _ in kept
            ):
                continue
            kept.append((image_path, picked[image_path]))
        return kept

    def extract_selection(self) -> None:
        if self.volume is None:
            return
        jobs = self._selection()
        if not jobs:
            QMessageBox.information(
                self,
                "Aucune sélection",
                "Sélectionnez un ou plusieurs fichiers / dossiers dans l'arbre.",
            )
            return
        self._start_extraction(jobs)

    def extract_all(self) -> None:
        if self.volume is None:
            return
        self._start_extraction([("", self.volume.root)])

    def _start_extraction(self, jobs: list[tuple[str, DirEntry]]) -> None:
        assert self.volume is not None and self.iso_path is not None
        default_dir = str(Path(self.iso_path).with_suffix(""))
        destination = QFileDialog.getExistingDirectory(
            self, "Choisir le dossier de destination", default_dir
        )
        if not destination:
            return
        self._begin_extraction(jobs, destination)

    def _begin_extraction(
        self, jobs: list[tuple[str, DirEntry]], destination: str
    ) -> None:
        assert self.volume is not None and self.iso_path is not None
        if self.worker is not None:
            return
        worker = ExtractWorker(
            self.iso_path, self.volume.partition_offset, jobs, destination, self
        )
        self._run_worker(worker, "extraction", "Extraction en cours…")

    # ------------------------------------------------------------------ #
    # Empreintes MD5 / SHA1
    # ------------------------------------------------------------------ #
    def hash_selection(self) -> None:
        if self.volume is None:
            return
        jobs = self._selection()
        if not jobs:
            QMessageBox.information(
                self,
                "Aucune sélection",
                "Sélectionnez un ou plusieurs fichiers / dossiers à hacher.",
            )
            return
        self._begin_hashing(jobs)

    def _begin_hashing(self, jobs: list[tuple[str, DirEntry]]) -> None:
        assert self.volume is not None and self.iso_path is not None
        if self.worker is not None:
            return
        worker = HashWorker(
            self.iso_path, self.volume.partition_offset, jobs, self
        )
        self._run_worker(worker, "hashes", "Calcul des empreintes MD5 / SHA1…")

    # ------------------------------------------------------------------ #
    # Worker générique (progression, annulation, résultat)
    # ------------------------------------------------------------------ #
    def _run_worker(self, worker: QThread, kind: str, message: str) -> None:
        self.worker = worker
        worker.progress.connect(self._on_progress)
        worker.succeeded.connect(
            lambda payload, k=kind: self._set_outcome(k, payload)
        )
        worker.failed.connect(lambda error: self._set_outcome("error", error))
        worker.cancelled.connect(lambda: self._set_outcome("cancelled", None))
        worker.finished.connect(self._on_worker_finished)

        self._operation_label = message.rstrip("…").strip()
        self._files_done = 0
        self._files_total = 0
        self.progress.setValue(0)
        self.progress.setFormat("%p %")
        file_progress = getattr(worker, "file_progress", None)
        if file_progress is not None:
            file_progress.connect(self._on_file_progress)
        self.progress.setVisible(True)
        self.cancel_button.setVisible(True)
        self.cancel_button.setEnabled(True)
        self.statusBar().showMessage(message)
        self._update_actions()
        worker.start()

    def _on_progress(self, done: int, total: int, current: str) -> None:
        if total > 0:
            self.progress.setValue(min(1000, int(done * 1000 / total)))
        if current:
            name = Path(current).name
            if len(name) > 60:
                name = name[:57] + "…"
            self.statusBar().showMessage(f"{self._operation_label} : {name}")
        else:
            self.statusBar().showMessage(self._operation_label)

    def _on_file_progress(self, files_done: int, files_total: int) -> None:
        self._files_done = files_done
        self._files_total = files_total
        self.progress.setFormat(self._progress_text())

    def _progress_text(self) -> str:
        if self._files_total <= 0:
            return "%p %"
        return f"%p % · {self._files_done}/{self._files_total} fichiers extraits"

    def _cancel_operation(self) -> None:
        if self.worker is not None:
            cancel = getattr(self.worker, "cancel", None)
            if callable(cancel):
                cancel()
            self.cancel_button.setEnabled(False)
            self.statusBar().showMessage("Annulation…")

    def _set_outcome(self, kind: str, payload: object) -> None:
        self._pending_outcome = (kind, payload)

    def _on_worker_finished(self) -> None:
        worker = self.worker
        self.worker = None
        if worker is not None:
            worker.deleteLater()
        self.progress.setVisible(False)
        self.cancel_button.setVisible(False)
        self.cancel_button.setEnabled(True)
        self._update_actions()

        outcome = self._pending_outcome
        self._pending_outcome = None
        if outcome is None:
            return
        kind, payload = outcome

        if kind == "extraction" and isinstance(payload, ExtractStats):
            message = (
                f"{payload.files} fichier(s) et {payload.dirs} dossier(s) extraits "
                f"({human_size(payload.bytes)})."
            )
            self.statusBar().showMessage(message, 10000)
            if payload.errors:
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Icon.Warning)
                box.setWindowTitle("Extraction terminée avec des avertissements")
                box.setText(message)
                box.setInformativeText(
                    f"{len(payload.errors)} élément(s) ignoré(s) ou en erreur."
                )
                details = "\n".join(payload.errors[:30])
                if len(payload.errors) > 30:
                    details += f"\n… et {len(payload.errors) - 30} autre(s)"
                box.setDetailedText(details)
                box.exec()
            else:
                QMessageBox.information(self, "Extraction terminée", message)
        elif kind == "hashes":
            results, errors = payload  # type: ignore[misc]
            message = (
                f"{len(results)} empreinte(s) calculée(s) — "
                f"{len(errors)} erreur(s)."
            )
            self.statusBar().showMessage(message, 10000)
            dialog = HashResultsDialog(results, errors, self)
            dialog.exec()
        elif kind == "cancelled":
            self.statusBar().showMessage("Opération annulée", 6000)
        elif kind == "error":
            self.statusBar().showMessage("Opération interrompue", 6000)
            QMessageBox.critical(self, "Erreur", str(payload))

    # ------------------------------------------------------------------ #
    # Menus contextuels et divers
    # ------------------------------------------------------------------ #
    def _show_context_menu(self, position) -> None:  # noqa: ANN001
        proxy_index = self.tree.indexAt(position)
        if not proxy_index.isValid():
            return
        selection = self.tree.selectionModel()
        if not selection.isSelected(proxy_index):
            selection.select(
                proxy_index,
                QItemSelectionModel.SelectionFlag.ClearAndSelect
                | QItemSelectionModel.SelectionFlag.Rows,
            )
            self.tree.setCurrentIndex(proxy_index)

        menu = QMenu(self)
        menu.addAction(self.extract_action)
        menu.addAction(self.hash_action)
        menu.addSeparator()
        menu.addAction(self.extract_all_action)
        menu.exec(self.tree.viewport().mapToGlobal(position))

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "À propos",
            "<b>Xbox Image Browser</b><br>"
            "Explorateur XDVDFS pour images Xbox / Xbox 360 (XISO, XGD2, XGD3).<br><br>"
            "Lecture par secteurs de 2048 octets, extraction en flux par blocs "
            "de 1 Mio : aucune image n'est chargée en mémoire.<br>"
            "Recherche indexée et empreintes MD5 / SHA1 calculées en flux.",
        )

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path:
                if self.open_iso(path):
                    event.acceptProposedAction()
                return

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.index_worker is not None:
            self.index_worker.cancel()
            self.index_worker.wait(2000)
        if self.worker is not None and self.worker.isRunning():
            answer = QMessageBox.question(
                self,
                "Opération en cours",
                "Une extraction ou un calcul d'empreintes est en cours. "
                "L'interrompre et quitter ?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            cancel = getattr(self.worker, "cancel", None)
            if callable(cancel):
                cancel()
            if not self.worker.wait(5000):
                QMessageBox.warning(
                    self,
                    "Opération en cours",
                    "L'opération n'a pas pu être interrompue à temps. Réessayez.",
                )
                event.ignore()
                return
        if self.volume is not None:
            self.volume.close()
        event.accept()


def run(image: str | None = None) -> int:
    """Lance l'application Qt (code de sortie renvoyé à ``main.py``)."""
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Xbox Image Browser")
    icon_file = _asset_path("icon.png")
    if icon_file.is_file():
        app.setWindowIcon(QIcon(str(icon_file)))
    window = MainWindow()
    window.show()
    if image:
        window.open_iso(image)
    return app.exec()
