"""Tests du parseur XDVDFS sur une image synthétique valide."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from make_test_iso import build_image, demo_tree, manifest  # noqa: E402
from xdvdfs import (  # noqa: E402
    ExtractionCancelled,
    XDVDFSError,
    XDVDFSVolume,
    human_size,
    sanitize_filename,
)

XGD3_OFFSET = 0x2080000


class XDVDFSTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.tree = demo_tree()
        cls.iso = build_image(cls.tmp / "demo.iso", cls.tree)
        cls.iso_xgd3 = build_image(
            cls.tmp / "demo_xgd3.iso", cls.tree, base_offset=XGD3_OFFSET
        )
        cls.iso_custom = build_image(
            cls.tmp / "demo_custom.iso", demo_tree(seed=7), base_offset=0x400000
        )
        cls.expected = manifest(cls.tree)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def setUp(self) -> None:
        self.volume = XDVDFSVolume.open(self.iso)
        self.addCleanup(self.volume.close)

    def _cleanup_dir(self, path: Path) -> None:
        self.addCleanup(shutil.rmtree, path, True)

    # ------------------------------------------------------------------ #
    # Détection
    # ------------------------------------------------------------------ #
    def test_detection_xiso(self) -> None:
        self.assertEqual(self.volume.partition_offset, 0)
        self.assertEqual(self.volume.format_name, "XDVDFS / XISO")
        self.assertEqual(self.volume.root_sector, 33)
        self.assertGreater(self.volume.root_size, 0)

    def test_detection_xgd3(self) -> None:
        with XDVDFSVolume.open(self.iso_xgd3) as volume:
            self.assertEqual(volume.partition_offset, XGD3_OFFSET)
            self.assertEqual(volume.format_name, "XGD3")
            self.assertIn("default.xex", [e.name for e in volume.list_directory()])
            self.assertEqual(
                volume.extract(
                    volume.find("media/audio/theme.xma"),
                    self.tmp / "xgd3_theme.xma",
                ).files,
                1,
            )
            self.assertEqual(
                (self.tmp / "xgd3_theme.xma").read_bytes(),
                self.tree["media"]["audio"]["theme.xma"],
            )

    def test_detection_xgd2_constant(self) -> None:
        # L'offset XGD2 est reconnu par la même logique que XGD3.
        path = build_image(
            self.tmp / "demo_xgd2.iso", demo_tree(seed=3), base_offset=0x0FD90000
        )
        with XDVDFSVolume.open(path) as volume:
            self.assertEqual(volume.format_name, "XGD2")
            self.assertEqual(volume.partition_offset, 0x0FD90000)
            self.assertEqual(
                [e.name for e in volume.list_directory(volume.root)][0], "assets"
            )

    def test_detection_par_scan(self) -> None:
        with XDVDFSVolume.open(self.iso_custom) as volume:
            self.assertEqual(volume.partition_offset, 0x400000)
            self.assertEqual(volume.format_name, "XDVDFS")
            self.assertTrue(volume.list_directory())

    def test_image_invalide(self) -> None:
        bogus = self.tmp / "bogus.iso"
        bogus.write_bytes(b"\x00" * 4096)
        with self.assertRaises(XDVDFSError):
            XDVDFSVolume.open(bogus)

    def test_offset_force_incorrect(self) -> None:
        with self.assertRaises(XDVDFSError):
            XDVDFSVolume.open(self.iso, partition_offset=0x123456)

    # ------------------------------------------------------------------ #
    # Arborescence
    # ------------------------------------------------------------------ #
    def test_racine_triee(self) -> None:
        entries = self.volume.list_directory()
        dirs = [entry.name for entry in entries if entry.is_dir]
        self.assertTrue(dirs)
        self.assertEqual([entry.name for entry in entries[: len(dirs)]], dirs)
        self.assertEqual(dirs, sorted(dirs, key=str.casefold))
        self.assertEqual(len(entries), len(self.tree))

    def test_listing_imbrique(self) -> None:
        media = self.volume.find("media")
        self.assertIsNotNone(media)
        assert media is not None
        self.assertTrue(media.is_dir)
        names = [entry.name for entry in self.volume.list_directory(media)]
        self.assertEqual(names, ["audio", "subs", "video"])

        audio = self.volume.find("/MEDIA/Audio")
        assert audio is not None
        files = self.volume.list_directory(audio)
        self.assertEqual(
            [entry.name for entry in files], ["battle_theme.xma", "theme.xma"]
        )
        self.assertTrue(all(not entry.is_dir for entry in files))

    def test_table_multi_secteurs(self) -> None:
        gallery = self.volume.find("gallery")
        assert gallery is not None
        self.assertGreater(gallery.size, 2048)
        entries = self.volume.list_directory(gallery)
        self.assertEqual(len(entries), 150)
        self.assertEqual(entries[0].name, "pic_000.jpg")
        self.assertEqual(entries[-1].name, "pic_149.jpg")

    def test_parcours_complet(self) -> None:
        walked = {"/".join(parts) for parts, _ in self.volume.walk()}
        expected = {path for path, _, _ in self.expected}
        self.assertEqual(walked, expected)

    def test_read_hors_limites(self) -> None:
        with self.assertRaises(XDVDFSError):
            self.volume.read(self.volume.file_size, 1)
        with self.assertRaises(XDVDFSError):
            self.volume.read(-1, 10)

    def test_read_secteur(self) -> None:
        raw = self.iso.read_bytes()
        self.assertEqual(self.volume.read(0, 64), raw[:64])
        descriptor = self.volume.read(32 * 2048, 20)
        self.assertEqual(descriptor, b"MICROSOFT*XBOX*MEDIA")

    # ------------------------------------------------------------------ #
    # Extraction
    # ------------------------------------------------------------------ #
    def test_iter_file_par_blocs(self) -> None:
        entry = self.volume.find("content.bin")
        assert entry is not None
        chunks = list(self.volume.iter_file(entry, chunk_size=100_000))
        self.assertTrue(chunks)
        self.assertTrue(all(len(chunk) <= 100_000 for chunk in chunks))
        self.assertEqual(b"".join(chunks), self.tree["content.bin"])

        empty = self.volume.find("empty.bin")
        assert empty is not None
        self.assertEqual(list(self.volume.iter_file(empty)), [])

    def test_iter_file_invalide_et_annulation(self) -> None:
        gallery = self.volume.find("gallery")
        assert gallery is not None
        with self.assertRaises(XDVDFSError):
            list(self.volume.iter_file(gallery))

        entry = self.volume.find("content.bin")
        assert entry is not None
        with self.assertRaises(ExtractionCancelled):
            for _ in self.volume.iter_file(entry, cancel=lambda: True):
                pass

    def test_extraction_fichier(self) -> None:
        entry = self.volume.find("media/video/intro.wmv")
        self.assertIsNotNone(entry)
        assert entry is not None
        out = self.tmp / "intro.wmv"
        self._cleanup_dir(out)
        stats = self.volume.extract(entry, out)
        self.assertEqual(stats.files, 1)
        self.assertEqual(stats.bytes, entry.size)
        self.assertEqual(out.read_bytes(), self.tree["media"]["video"]["intro.wmv"])

    def test_extraction_complete(self) -> None:
        out = self.tmp / "extract_all"
        self._cleanup_dir(out)
        stats = self.volume.extract(self.volume.root, out)

        file_count = sum(1 for _, is_dir, _ in self.expected if not is_dir)
        dir_count = 1 + sum(1 for _, is_dir, _ in self.expected if is_dir)
        self.assertEqual(stats.files, file_count)
        self.assertEqual(stats.dirs, dir_count)
        self.assertEqual(
            stats.bytes, sum(len(data) for _, is_dir, data in self.expected if not is_dir)
        )
        self.assertEqual(stats.errors, [])

        for path, is_dir, data in self.expected:
            target = out.joinpath(*path.split("/"))
            if is_dir:
                self.assertTrue(target.is_dir(), path)
            else:
                self.assertTrue(target.is_file(), path)
                self.assertEqual(target.read_bytes(), data, path)

    def test_extraction_dossier_selectionne(self) -> None:
        media = self.volume.find("media")
        assert media is not None
        out = self.tmp / "media_out" / "media"
        self._cleanup_dir(self.tmp / "media_out")
        stats = self.volume.extract(media, out)
        self.assertEqual(stats.files, 4)
        self.assertEqual((out / "audio" / "theme.xma").read_bytes(),
                         self.tree["media"]["audio"]["theme.xma"])
        self.assertTrue((out / "subs").is_dir())

    def test_fichier_vide(self) -> None:
        out = self.tmp / "empty.bin"
        self._cleanup_dir(out)
        self.volume.extract(self.volume.find("empty.bin"), out)
        self.assertTrue(out.exists())
        self.assertEqual(out.stat().st_size, 0)

    def test_progression_et_annulation(self) -> None:
        entry = self.volume.find("content.bin")
        assert entry is not None
        out = self.tmp / "content.bin"
        self._cleanup_dir(out)
        seen: list[tuple[int, int]] = []

        def progress(done: int, total: int, _current: str) -> None:
            seen.append((done, total))

        self.volume.extract(entry, out, progress=progress)
        self.assertTrue(seen)
        self.assertEqual(seen[-1][0], entry.size)
        self.assertEqual(seen[-1][1], entry.size)

        cancelled = {"value": False}
        partial = self.tmp / "content_partial.bin"
        self._cleanup_dir(partial)
        with self.assertRaises(ExtractionCancelled):
            self.volume.extract(
                entry,
                partial,
                progress=lambda _done, _total, _current: cancelled.update(value=True),
                cancel=lambda: cancelled["value"],
            )
        self.assertLess(partial.stat().st_size, entry.size)

    def test_tree_totals_et_progression_fichiers(self) -> None:
        media = self.volume.find("media")
        assert media is not None
        total, files = self.volume.tree_totals(media)
        walked = [entry for _, entry in self.volume.walk(media) if not entry.is_dir]
        self.assertEqual(files, len(walked))
        self.assertEqual(total, sum(entry.size for entry in walked))

        out = self.tmp / "progress_media"
        self._cleanup_dir(out)
        seen: list[tuple[int, int]] = []
        stats = self.volume.extract(
            media,
            out,
            file_progress=lambda done, count: seen.append((done, count)),
        )
        self.assertEqual(stats.files, files)
        self.assertTrue(seen)
        self.assertEqual(seen[-1], (files, files))
        self.assertEqual([done for done, _ in seen], sorted(done for done, _ in seen))

    def test_fichier_long_nom(self) -> None:
        out = self.tmp / "long_out"
        self._cleanup_dir(out)
        self.volume.extract(self.volume.root, out)
        expected_name = "long_filename_" + "x" * 120 + ".dat"
        self.assertTrue((out / expected_name).is_file())

    # ------------------------------------------------------------------ #
    # Utilitaires
    # ------------------------------------------------------------------ #
    def test_sanitize_filename(self) -> None:
        self.assertEqual(sanitize_filename("a:b?.txt"), "a_b_.txt")
        self.assertEqual(sanitize_filename("CON.txt"), "_CON.txt")
        self.assertEqual(sanitize_filename(".."), "_")
        self.assertEqual(sanitize_filename(""), "_")
        self.assertEqual(len(sanitize_filename("z" * 400)), 255)

    def test_human_size(self) -> None:
        self.assertEqual(human_size(0), "0 o")
        self.assertEqual(human_size(1023), "1023 o")
        self.assertEqual(human_size(1024), "1.0 Ko")
        self.assertEqual(human_size(1536), "1.5 Ko")
        self.assertEqual(human_size(1024 ** 3), "1.0 Go")


if __name__ == "__main__":
    unittest.main()
