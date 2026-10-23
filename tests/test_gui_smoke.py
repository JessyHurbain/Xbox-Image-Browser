"""Test de fumée de l'interface PyQt6, sans affichage (plateforme offscreen)."""

from __future__ import annotations

import hashlib
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shutil  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest import mock  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from gui import (  # noqa: E402
    LOADED_ROLE,
    ExtractWorker,
    HashResultsDialog,
    MainWindow,
)
from make_test_iso import build_image, demo_tree  # noqa: E402


class GuiSmokeTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.tree = demo_tree()
        cls.iso = build_image(cls.tmp / "gui_demo.iso", cls.tree)
        cls._patches = [
            mock.patch.object(
                QMessageBox,
                "information",
                return_value=QMessageBox.StandardButton.Ok,
            ),
            mock.patch.object(
                QMessageBox, "critical", return_value=QMessageBox.StandardButton.Ok
            ),
            mock.patch.object(
                QMessageBox, "warning", return_value=QMessageBox.StandardButton.Ok
            ),
        ]
        for patch in cls._patches:
            patch.start()
        cls.window = MainWindow()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.window.close()
        cls.window.deleteLater()
        cls.app.processEvents()
        for patch in cls._patches:
            patch.stop()
        cls._tmp.cleanup()

    def _wait_for(self, predicate, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(predicate(), "condition non satisfaite dans le délai imparti")

    def _wait_until_idle(self, timeout: float = 20.0) -> None:
        self._wait_for(lambda: self.window.worker is None, timeout)

    def test_ouverture_et_arbre_paresseux(self) -> None:
        window = self.window
        self.assertTrue(window.open_iso(str(self.iso)))
        self.assertIn("XDVDFS / XISO", window.info_label.text())

        labels = [
            window.model.item(row, 0).text() for row in range(window.model.rowCount())
        ]
        self.assertEqual(labels[:3], ["assets", "gallery", "media"])
        media = window.model.item(labels.index("media"), 0)
        self.assertEqual(media.rowCount(), 1, "un placeholder doit signaler le dossier")

        window.tree.expand(window.proxy.mapFromSource(media.index()))
        self.app.processEvents()
        self.assertEqual(media.rowCount(), 3)
        self.assertEqual(
            [media.child(row).text() for row in range(media.rowCount())],
            ["audio", "subs", "video"],
        )

    def test_recherche_instantancee(self) -> None:
        window = self.window
        self.assertTrue(window.open_iso(str(self.iso)))
        self._wait_for(lambda: window._index is not None)
        self.assertTrue(window.search_edit.isEnabled())

        window.search_edit.setText("theme")
        self.app.processEvents()
        self.assertEqual(window.search_count.text(), "2 résultats")
        self.assertEqual(window.proxy.rowCount(), 1)  # seule la branche media reste

        theme = window._items["media/audio/theme.xma"]
        proxy_index = window.proxy.mapFromSource(theme.index())
        self.assertTrue(proxy_index.isValid())
        readme = window._items["readme.txt"]
        self.assertFalse(window.proxy.mapFromSource(readme.index()).isValid())

        window.search_edit.setText("media audio")
        self.app.processEvents()
        # les deux fichiers d'audio + le dossier media/audio lui-même
        self.assertEqual(window.search_count.text(), "3 résultats")

        window.search_edit.clear()
        self.app.processEvents()
        self.assertEqual(window.proxy.rowCount(), window.model.rowCount())
        self.assertEqual(window.search_count.text(), "")

    def test_recherche_chemin_imbrique_charge_les_parents(self) -> None:
        window = self.window
        self.assertTrue(window.open_iso(str(self.iso)))
        self._wait_for(lambda: window._index is not None)

        window.search_edit.setText("intro.wmv")
        self.app.processEvents()
        self.assertEqual(window.search_count.text(), "1 résultat")
        video = window._items.get("media/video")
        self.assertIsNotNone(video, "les dossiers parents doivent être chargés")
        self.assertTrue(video.data(LOADED_ROLE))
        intro = window._items["media/video/intro.wmv"]
        proxy_index = window.proxy.mapFromSource(intro.index())
        self.assertTrue(proxy_index.isValid())

    def test_extraction_via_interface(self) -> None:
        window = self.window
        self.assertTrue(window.open_iso(str(self.iso)))
        entry = window.volume.find("default.xex")
        assert entry is not None
        out_dir = self.tmp / "gui_out"
        self.addCleanup(shutil.rmtree, out_dir, True)

        window._begin_extraction([("default.xex", entry)], str(out_dir))
        self.assertIsNotNone(window.worker)
        self.assertFalse(window.extract_button.isEnabled())
        self._wait_until_idle()

        target = out_dir / "default.xex"
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), self.tree["default.xex"])
        self.assertTrue(window.extract_button.isEnabled())
        self.assertFalse(window.progress.isVisible())

    def test_extraction_dossier_et_selection(self) -> None:
        window = self.window
        self.assertTrue(window.open_iso(str(self.iso)))
        media = window.volume.find("media")
        assert media is not None
        out_dir = self.tmp / "gui_media"
        self.addCleanup(shutil.rmtree, out_dir, True)

        window._begin_extraction([("media", media)], str(out_dir))
        self._wait_until_idle()

        target = out_dir / "media"
        self.assertEqual(
            (target / "audio" / "theme.xma").read_bytes(),
            self.tree["media"]["audio"]["theme.xma"],
        )
        self.assertTrue((target / "subs").is_dir())
        self.assertEqual(window.statusBar().currentMessage()[:6], "4 fich")

    def test_progression_fichiers_extraits(self) -> None:
        window = self.window
        self.addCleanup(window.progress.setFormat, "%p %")

        window._on_file_progress(0, 4)
        self.assertEqual(window.progress.format(), "%p % · 0/4 fichiers extraits")
        window._on_file_progress(3, 4)
        self.assertEqual(window.progress.format(), "%p % · 3/4 fichiers extraits")
        window._on_file_progress(4, 4)
        self.assertEqual(window.progress.format(), "%p % · 4/4 fichiers extraits")

    def test_extraction_signale_les_fichiers(self) -> None:
        window = self.window
        self.assertTrue(window.open_iso(str(self.iso)))
        media = window.volume.find("media")
        assert media is not None
        out_dir = self.tmp / "gui_files_count"
        self.addCleanup(shutil.rmtree, out_dir, True)

        worker = ExtractWorker(
            str(self.iso),
            window.volume.partition_offset,
            [("media", media)],
            str(out_dir),
        )
        seen: list[tuple[int, int]] = []
        worker.file_progress.connect(
            lambda done, total: seen.append((done, total)),
            Qt.ConnectionType.DirectConnection,
        )
        worker.start()
        self.assertTrue(worker.wait(20000))

        self.assertTrue(seen)
        self.assertEqual(seen[-1], (4, 4))
        self.assertEqual([done for done, _ in seen], sorted(done for done, _ in seen))

    def test_empreintes_md5_sha1(self) -> None:
        window = self.window
        self.assertTrue(window.open_iso(str(self.iso)))
        self._wait_for(lambda: window._index is not None)

        entry = window.volume.find("default.xex")
        assert entry is not None
        captured: dict[str, list] = {}

        def fake_exec(dialog: HashResultsDialog) -> int:
            captured["results"] = dialog._results
            return 0

        with mock.patch.object(HashResultsDialog, "exec", fake_exec):
            window._begin_hashing([("default.xex", entry)])
            self.assertIsNotNone(window.worker)
            self.assertFalse(window.hash_button.isEnabled())
            self._wait_until_idle()

        results = captured["results"]
        self.assertEqual(len(results), 1)
        expected = self.tree["default.xex"]
        self.assertEqual(results[0].path, "default.xex")
        self.assertEqual(results[0].size, len(expected))
        self.assertEqual(results[0].md5, hashlib.md5(expected).hexdigest())
        self.assertEqual(results[0].sha1, hashlib.sha1(expected).hexdigest())
        self.assertTrue(window.hash_button.isEnabled())

    def test_empreintes_dossier_complet(self) -> None:
        window = self.window
        self.assertTrue(window.open_iso(str(self.iso)))
        media = window.volume.find("media")
        assert media is not None
        captured: dict[str, list] = {}

        def fake_exec(dialog: HashResultsDialog) -> int:
            captured["results"] = dialog._results
            return 0

        with mock.patch.object(HashResultsDialog, "exec", fake_exec):
            window._begin_hashing([("media", media)])
            self._wait_until_idle()

        results = captured["results"]
        self.assertEqual(len(results), 4)
        paths = {result.path for result in results}
        self.assertIn("media/audio/theme.xma", paths)
        self.assertIn("media/video/intro.wmv", paths)
        theme = next(r for r in results if r.path == "media/audio/theme.xma")
        expected = self.tree["media"]["audio"]["theme.xma"]
        self.assertEqual(theme.md5, hashlib.md5(expected).hexdigest())
        self.assertEqual(theme.sha1, hashlib.sha1(expected).hexdigest())


if __name__ == "__main__":
    unittest.main()
