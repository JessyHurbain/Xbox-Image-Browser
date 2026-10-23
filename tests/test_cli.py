"""Tests du point d'entrée en ligne de commande (sans interface graphique)."""

from __future__ import annotations

import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from main import main  # noqa: E402
from make_test_iso import build_image, demo_tree  # noqa: E402


class CliTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.tree = demo_tree()
        cls.iso = build_image(cls.tmp / "cli_demo.iso", cls.tree)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _run(self, *args: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_info(self) -> None:
        code, out, _ = self._run("--iso", str(self.iso), "--info")
        self.assertEqual(code, 0)
        self.assertIn("Format       : XDVDFS / XISO", out)
        self.assertIn("Partition    : 0x0", out)
        self.assertIn("fichier(s)", out)

    def test_list(self) -> None:
        code, out, _ = self._run(str(self.iso), "--list")
        self.assertEqual(code, 0)
        self.assertIn("media/", out)
        self.assertIn("theme.xma", out)
        self.assertIn("default.xex", out)

    def test_extract_fichier(self) -> None:
        out_dir = self.tmp / "cli_file"
        self.addCleanup(shutil.rmtree, out_dir, True)
        code, out, err = self._run(
            "--iso", str(self.iso), "--extract", "media/audio/theme.xma", "-o", str(out_dir)
        )
        self.assertEqual(code, 0, err)
        self.assertIn("1 fichier(s)", out)
        target = out_dir / "media" / "audio" / "theme.xma"
        self.assertEqual(target.read_bytes(), self.tree["media"]["audio"]["theme.xma"])

    def test_extract_tout(self) -> None:
        out_dir = self.tmp / "cli_all"
        self.addCleanup(shutil.rmtree, out_dir, True)
        code, _, err = self._run("--iso", str(self.iso), "--all", "-o", str(out_dir))
        self.assertEqual(code, 0, err)
        self.assertTrue((out_dir / "default.xex").is_file())
        self.assertTrue((out_dir / "media" / "video" / "intro.wmv").is_file())
        self.assertTrue((out_dir / "media" / "subs").is_dir())

    def test_chemin_introuvable(self) -> None:
        code, _, err = self._run(
            "--iso", str(self.iso), "--extract", "nope/missing.bin", "-o", str(self.tmp)
        )
        self.assertEqual(code, 1)
        self.assertIn("introuvable", err)

    def test_sans_image(self) -> None:
        code, _, err = self._run("--list")
        self.assertEqual(code, 2)
        self.assertIn("indiquez une image", err)

    def test_image_invalide(self) -> None:
        bad = self.tmp / "bad.iso"
        bad.write_bytes(b"\xFF" * 8192)
        code, _, err = self._run("--iso", str(bad), "--info")
        self.assertEqual(code, 1)
        self.assertIn("MICROSOFT*XBOX*MEDIA", err)


if __name__ == "__main__":
    unittest.main()
