"""Génère des images XDVDFS synthétiques (tests et démo, sans contenu copyrighté).

Usage ::

    python tests/make_test_iso.py demo.iso

L'image produite est une vraie image XDVDFS (descripteur au secteur 32,
tables de répertoires en arbre binaire équilibré, fichiers alignés sur des
secteurs de 2048 octets). Elle peut donc être ouverte par l'application.
"""

from __future__ import annotations

import random
import struct
import sys
from pathlib import Path

SECTOR = 2048
MAGIC = b"MICROSOFT*XBOX*MEDIA"
DESCRIPTOR_SECTOR = 32
ATTR_DIRECTORY = 0x10
ATTR_FILE = 0x20


def align4(value: int) -> int:
    return (value + 3) & ~3


def demo_tree(seed: int = 2026) -> dict:
    """Arborescence de démonstration déterministe."""
    rng = random.Random(seed)
    return {
        "default.xex": rng.randbytes(180_000),
        "content.bin": rng.randbytes(1024 * 1024 + 17),
        "readme.txt": (
            "Image XDVDFS de demonstration generee localement.\n"
            "Aucun contenu sous copyright.\n"
        ).encode("ascii"),
        "empty.bin": b"",
        "long_filename_" + "x" * 120 + ".dat": rng.randbytes(777),
        "media": {
            "audio": {
                "theme.xma": rng.randbytes(48_000),
                "battle_theme.xma": rng.randbytes(65_537),
            },
            "video": {
                "intro.wmv": rng.randbytes(120_000),
                "credits.wmv": rng.randbytes(23_456),
            },
            "subs": {},
        },
        "assets": {
            "textures": {
                f"texture_{index:03d}.dds": rng.randbytes(300 + index * 7)
                for index in range(150)
            },
        },
        "gallery": {
            f"pic_{index:03d}.jpg": rng.randbytes(64) for index in range(150)
        },
    }


class _Node:
    __slots__ = (
        "name",
        "is_dir",
        "data",
        "children",
        "table_sector",
        "table_size",
        "sector",
    )

    def __init__(
        self,
        name: str,
        is_dir: bool,
        data: bytes | None = None,
        children: list["_Node"] | None = None,
    ) -> None:
        self.name = name
        self.is_dir = is_dir
        self.data = data or b""
        self.children = children or []
        self.table_sector = 0
        self.table_size = 0
        self.sector = 0

    def size(self) -> int:
        return self.table_size if self.is_dir else len(self.data)

    def start_sector(self) -> int:
        return self.table_sector if self.is_dir else self.sector


def _make_nodes(tree: dict) -> list[_Node]:
    nodes: list[_Node] = []
    for name, value in tree.items():
        encoded = name.encode("latin-1")
        if not 0 < len(encoded) <= 255:
            raise ValueError(f"Nom invalide pour XDVDFS : {name!r}")
        if isinstance(value, dict):
            nodes.append(_Node(name, True, children=_make_nodes(value)))
        else:
            nodes.append(_Node(name, False, bytes(value)))
    return nodes


def _table_size(node: _Node) -> int:
    return sum(align4(14 + len(child.name.encode("latin-1"))) for child in node.children)


def _serialize_directory(node: _Node) -> bytes:
    """Sérialise les entrées en arbre binaire équilibré (liens en unités de 4 octets)."""
    ordered = sorted(node.children, key=lambda child: child.name.casefold())
    records: dict[int, tuple[_Node, int, int]] = {}
    cursor = 0

    def place(low: int, high: int) -> int:
        nonlocal cursor
        if low > high:
            return 0
        middle = (low + high) // 2
        child = ordered[middle]
        offset = cursor
        cursor += align4(14 + len(child.name.encode("latin-1")))
        left = place(low, middle - 1)
        right = place(middle + 1, high)
        records[offset] = (child, left, right)
        return offset

    place(0, len(ordered) - 1)

    table = bytearray(cursor)
    for offset, (child, left, right) in records.items():
        name = child.name.encode("latin-1")
        struct.pack_into(
            "<HHIIBB",
            table,
            offset,
            left // 4,
            right // 4,
            child.start_sector(),
            child.size(),
            ATTR_DIRECTORY if child.is_dir else ATTR_FILE,
            len(name),
        )
        table[offset + 14 : offset + 14 + len(name)] = name
    return bytes(table)


def build_image(
    out_path: str | Path,
    tree: dict,
    base_offset: int = 0,
) -> Path:
    """Écrit une image XDVDFS complète ; ``base_offset`` simule XGD2/XGD3."""
    root = _Node("", True, children=_make_nodes(tree))

    directories: list[_Node] = []
    queue = [root]
    while queue:
        node = queue.pop(0)
        directories.append(node)
        node.table_size = _table_size(node)
        node.table_sector = 0  # attribué ci-dessous
        for child in node.children:
            if child.is_dir:
                queue.append(child)

    cursor = DESCRIPTOR_SECTOR + 1
    for node in directories:
        node.table_sector = cursor
        cursor += max(1, (node.table_size + SECTOR - 1) // SECTOR)

    files: list[_Node] = []

    def collect_files(node: _Node) -> None:
        for child in node.children:
            if child.is_dir:
                collect_files(child)
            else:
                files.append(child)

    collect_files(root)
    for node in files:
        if node.data:
            node.sector = cursor
            cursor += (len(node.data) + SECTOR - 1) // SECTOR
        else:
            node.sector = 0  # fichier vide : secteur 0, taille 0

    image = bytearray(cursor * SECTOR)

    for node in directories:
        table = _serialize_directory(node)
        start = node.table_sector * SECTOR
        image[start : start + len(table)] = table

    for node in files:
        if node.data:
            start = node.sector * SECTOR
            image[start : start + len(node.data)] = node.data

    struct.pack_into(
        "<20sIIQ",
        image,
        DESCRIPTOR_SECTOR * SECTOR,
        MAGIC,
        root.table_sector,
        root.table_size,
        0,
    )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as fp:
        if base_offset:
            fp.seek(base_offset - 1)
            fp.write(b"\0")
        fp.write(image)
    return out_path


def manifest(tree: dict, prefix: str = "") -> list[tuple[str, bool, bytes]]:
    """Liste ``(chemin, est_dossier, contenu)`` attendue après extraction."""
    entries: list[tuple[str, bool, bytes]] = []
    for name, value in tree.items():
        path = f"{prefix}/{name}" if prefix else name
        if isinstance(value, dict):
            entries.append((path, True, b""))
            entries.extend(manifest(value, path))
        else:
            entries.append((path, False, bytes(value)))
    return entries


if __name__ == "__main__":
    destination = sys.argv[1] if len(sys.argv) > 1 else "demo.iso"
    path = build_image(destination, demo_tree())
    print(f"Image XDVDFS de démonstration écrite : {path} ({path.stat().st_size} octets)")
