"""Lecteur et extracteur du système de fichiers XDVDFS (Xbox / Xbox 360).

Format
------
* Secteurs logiques de 2048 octets, numérotés depuis le début de la
  *partition de jeu* (et non depuis le début du fichier .iso).
* Descripteur de volume au secteur 32 de la partition ::

      +0x00  "MICROSOFT*XBOX*MEDIA"   20 octets
      +0x14  secteur du répertoire racine   uint32 LE
      +0x18  taille de la table racine      uint32 LE
      +0x1C  horodatage                     uint64 LE

* Entrée de répertoire (en-tête de 14 octets + nom, alignée sur 4 octets) ::

      +0x00  sous-arbre gauche   uint16  (en unités de 4 octets depuis le
      +0x02  sous-arbre droit    uint16   début de la table du répertoire)
      +0x04  secteur de début    uint32 LE (relatif à la partition de jeu)
      +0x08  taille en octets    uint32 LE
      +0x0C  attributs           uint8   (0x10 = répertoire)
      +0x0D  longueur du nom     uint8
      +0x0E  nom (ASCII, non terminé par NUL)

Les entrées d'un répertoire forment un arbre binaire de recherche ; on le
parcourt via les liens gauche/droit. Aucune donnée n'est chargée en entier :
les fichiers sont lus et copiés par blocs de 1 Mio.
"""

from __future__ import annotations

import os
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, Optional

SECTOR_SIZE = 2048
MAGIC = b"MICROSOFT*XBOX*MEDIA"
VOLUME_DESCRIPTOR_SECTOR = 32
VOLUME_DESCRIPTOR_OFFSET = VOLUME_DESCRIPTOR_SECTOR * SECTOR_SIZE

#: Offset du début de la partition de jeu dans une image disque complète.
GAME_PARTITION_OFFSETS: tuple[tuple[str, int], ...] = (
    ("XDVDFS / XISO", 0x00000000),
    ("XGD2", 0x0FD90000),
    ("XGD3", 0x02080000),
)
_OFFSET_LABELS = {offset: label for label, offset in GAME_PARTITION_OFFSETS}

SCAN_LIMIT = 512 * 1024 * 1024  # recherche de secours : 512 premiers Mio
SCAN_CHUNK = 4 * 1024 * 1024
COPY_CHUNK = 1024 * 1024

MAX_DIRECTORY_SIZE = 16 * 1024 * 1024
MAX_DIRECTORY_DEPTH = 64
MAX_WALK_ENTRIES = 1_000_000
ATTR_DIRECTORY = 0x10
ATTR_READONLY = 0x01

ProgressCallback = Callable[[int, int, str], None]
FileProgressCallback = Callable[[int, int], None]


class XDVDFSError(Exception):
    """Image invalide ou donnée illisible."""


class ExtractionCancelled(Exception):
    """Extraction interrompue par l'utilisateur."""


@dataclass(frozen=True)
class DirEntry:
    """Une entrée (fichier ou répertoire) de l'arborescence XDVDFS."""

    name: str
    sector: int
    size: int
    attributes: int

    @property
    def is_dir(self) -> bool:
        return bool(self.attributes & ATTR_DIRECTORY)

    @property
    def is_readonly(self) -> bool:
        return bool(self.attributes & ATTR_READONLY)


@dataclass
class ExtractStats:
    """Compteurs d'une extraction."""

    files: int = 0
    dirs: int = 0
    bytes: int = 0
    errors: list[str] = field(default_factory=list)


def human_size(size: int) -> str:
    """Formate une taille en octets (unités françaises, base 1024)."""
    value = float(size)
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if value < 1024.0 or unit == "To":
            if unit == "o":
                return f"{int(value)} o"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{size} o"


_INVALID_CHARS = '<>:"/\\|?*'
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str) -> str:
    """Rend un nom XDVDFS compatible avec le système de fichiers hôte."""
    cleaned = "".join(
        "_" if (char in _INVALID_CHARS or ord(char) < 32) else char
        for char in name
    )
    cleaned = cleaned.strip().rstrip(" .")
    if not cleaned:
        cleaned = "_"
    if cleaned.split(".")[0].upper() in _RESERVED_NAMES:
        cleaned = "_" + cleaned
    return cleaned[:255]


def _unique_name(name: str, used: set[str]) -> str:
    """Évite les collisions (XDVDFS est insensible à la casse)."""
    stem, extension = os.path.splitext(name)
    candidate = name
    counter = 2
    while candidate.casefold() in used:
        candidate = f"{stem} ({counter}){extension}"
        counter += 1
    used.add(candidate.casefold())
    return candidate


def _decode_name(raw: bytes) -> str:
    return raw.decode("latin-1").rstrip("\x00").strip()


def _probe_descriptor(
    fp: BinaryIO, file_size: int, partition_offset: int
) -> Optional[tuple[int, int]]:
    """Valide le descripteur de volume à ``partition_offset``.

    Retourne ``(secteur_racine, taille_racine)`` ou ``None``.
    """
    descriptor_offset = partition_offset + VOLUME_DESCRIPTOR_OFFSET
    if partition_offset < 0 or descriptor_offset + 0x20 > file_size:
        return None
    fp.seek(descriptor_offset)
    header = fp.read(0x20)
    if len(header) < 0x20 or header[: len(MAGIC)] != MAGIC:
        return None
    root_sector, root_size = struct.unpack_from("<II", header, 0x14)
    if root_sector == 0 or not 0 < root_size <= MAX_DIRECTORY_SIZE:
        return None
    root_offset = partition_offset + root_sector * SECTOR_SIZE
    if root_offset + root_size > file_size:
        return None
    return root_sector, root_size


def _scan_for_magic(fp: BinaryIO, file_size: int) -> Optional[int]:
    """Cherche la signature dans les 512 premiers Mio, par blocs."""
    limit = min(file_size, SCAN_LIMIT)
    fp.seek(0)
    overlap = len(MAGIC) - 1
    tail = b""
    position = 0
    while position < limit:
        chunk = fp.read(min(SCAN_CHUNK, limit - position))
        if not chunk:
            break
        buffer = tail + chunk
        index = buffer.find(MAGIC)
        if index >= 0:
            return position - len(tail) + index
        tail = buffer[-overlap:]
        position += len(chunk)
    return None


def detect_partition(fp: BinaryIO, file_size: int) -> tuple[int, str]:
    """Localise la partition de jeu et identifie le format du disque.

    Essaie les offsets connus (XISO, XGD2, XGD3) puis, en secours, scanne
    l'image à la recherche de la signature (utile pour les images non
    standard).
    """
    for label, offset in GAME_PARTITION_OFFSETS:
        if offset + VOLUME_DESCRIPTOR_OFFSET + 0x20 <= file_size and _probe_descriptor(
            fp, file_size, offset
        ):
            return offset, label

    magic_offset = _scan_for_magic(fp, file_size)
    if magic_offset is not None and magic_offset >= VOLUME_DESCRIPTOR_OFFSET:
        offset = magic_offset - VOLUME_DESCRIPTOR_OFFSET
        if _probe_descriptor(fp, file_size, offset):
            return offset, _OFFSET_LABELS.get(offset, "XDVDFS")

    raise XDVDFSError(
        "Signature « MICROSOFT*XBOX*MEDIA » introuvable : ce fichier n'est pas "
        "une image XDVDFS (Xbox / Xbox 360) valide."
    )


class XDVDFSVolume:
    """Accès en lecture à une image XDVDFS.

    Utilisation ::

        with XDVDFSVolume.open("game.iso") as volume:
            for entry in volume.list_directory():
                print(entry.name, entry.size, entry.is_dir)
            volume.extract(volume.root, "sortie")
    """

    def __init__(
        self,
        fp: BinaryIO,
        path: str,
        file_size: int,
        partition_offset: int,
        format_name: str,
        root_sector: int,
        root_size: int,
    ) -> None:
        self._fp = fp
        self._lock = threading.Lock()
        self.path = path
        self.file_size = file_size
        self.partition_offset = partition_offset
        self.format_name = format_name
        self.root_sector = root_sector
        self.root_size = root_size

    # ------------------------------------------------------------------ #
    # Ouverture / fermeture
    # ------------------------------------------------------------------ #
    @classmethod
    def open(
        cls, path: str | os.PathLike[str], partition_offset: Optional[int] = None
    ) -> "XDVDFSVolume":
        """Ouvre une image et détecte automatiquement XISO / XGD2 / XGD3."""
        path = os.fspath(path)
        fp = open(path, "rb")
        try:
            file_size = os.fstat(fp.fileno()).st_size
            if partition_offset is None:
                offset, label = detect_partition(fp, file_size)
            else:
                offset = int(partition_offset)
                probe = _probe_descriptor(fp, file_size, offset)
                if probe is None:
                    raise XDVDFSError(
                        f"Aucun descripteur XDVDFS valide à l'offset 0x{offset:X}."
                    )
                label = _OFFSET_LABELS.get(offset, "XDVDFS")
            probe = _probe_descriptor(fp, file_size, offset)
            assert probe is not None
            root_sector, root_size = probe
            return cls(
                fp, path, file_size, offset, label, root_sector, root_size
            )
        except BaseException:
            fp.close()
            raise

    def close(self) -> None:
        if not self._fp.closed:
            self._fp.close()

    def __enter__(self) -> "XDVDFSVolume":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def root(self) -> DirEntry:
        """Entrée synthétique représentant la racine du volume."""
        return DirEntry("", self.root_sector, self.root_size, ATTR_DIRECTORY)

    @property
    def sector_count(self) -> int:
        return max(0, (self.file_size - self.partition_offset) // SECTOR_SIZE)

    # ------------------------------------------------------------------ #
    # Lecture bas niveau
    # ------------------------------------------------------------------ #
    def read(self, offset: int, size: int) -> bytes:
        """Lit ``size`` octets à ``offset`` sans charger le reste de l'image."""
        if offset < 0 or size < 0:
            raise XDVDFSError("Offset ou taille négatif.")
        if offset + size > self.file_size:
            raise XDVDFSError(
                f"Lecture hors limites ({size} octets à 0x{offset:X})."
            )
        with self._lock:
            self._fp.seek(offset)
            data = self._fp.read(size)
        if len(data) != size:
            raise XDVDFSError(
                f"Lecture incomplète à 0x{offset:X} (image tronquée ?)."
            )
        return data

    def _sector_offset(self, sector: int) -> int:
        return self.partition_offset + sector * SECTOR_SIZE

    # ------------------------------------------------------------------ #
    # Répertoires
    # ------------------------------------------------------------------ #
    def iter_file(
        self,
        entry: DirEntry,
        chunk_size: int = COPY_CHUNK,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> Iterator[bytes]:
        """Produit le contenu d'un fichier par blocs (empreintes, copie…)."""
        if entry.is_dir:
            raise XDVDFSError(f"{entry.name!r} est un répertoire.")
        if entry.size == 0:
            return
        if entry.sector == 0:
            raise XDVDFSError(f"{entry.name}: secteur invalide (0)")
        offset = self._sector_offset(entry.sector)
        if offset + entry.size > self.file_size:
            raise XDVDFSError(f"{entry.name}: données hors limites")
        remaining = entry.size
        while remaining:
            if cancel is not None and cancel():
                raise ExtractionCancelled()
            chunk = self.read(offset, min(chunk_size, remaining))
            offset += len(chunk)
            remaining -= len(chunk)
            yield chunk

    def _directory_table(self, entry: DirEntry) -> bytes:
        if entry.size == 0:
            return b""
        if entry.size > MAX_DIRECTORY_SIZE:
            raise XDVDFSError(
                f"Table de répertoire invalide ({entry.size} octets) : {entry.name!r}"
            )
        if entry.sector == 0:
            raise XDVDFSError(f"Répertoire sans secteur valide : {entry.name!r}")
        offset = self._sector_offset(entry.sector)
        if offset + entry.size > self.file_size:
            raise XDVDFSError(
                f"Table de répertoire hors de l'image : {entry.name!r}"
            )
        return self.read(offset, entry.size)

    @staticmethod
    def _parse_entries(raw: bytes, start: int = 0) -> list[DirEntry]:
        """Parcourt l'arbre binaire contenu dans une table de répertoire."""
        entries: list[DirEntry] = []
        visited: set[int] = set()
        stack = [start]
        while stack:
            position = stack.pop()
            if position in visited or position + 14 > len(raw):
                continue
            visited.add(position)
            left, right, sector, size, attributes, name_length = struct.unpack_from(
                "<HHIIBB", raw, position
            )
            end = position + 14 + name_length
            if name_length == 0 or end > len(raw):
                continue
            name = _decode_name(raw[position + 14 : end])
            if not name:
                continue
            entries.append(DirEntry(name, sector, size, attributes))
            if left:
                stack.append(left * 4)
            if right:
                stack.append(right * 4)
        return entries

    @staticmethod
    def _scan_entries(raw: bytes) -> list[DirEntry]:
        """Lecture séquentielle de secours (arbres aux liens invalides)."""
        entries: list[DirEntry] = []
        position = 0
        while position + 14 <= len(raw):
            _, _, sector, size, attributes, name_length = struct.unpack_from(
                "<HHIIBB", raw, position
            )
            if name_length == 0:
                break
            end = position + 14 + name_length
            if end > len(raw):
                break
            name = _decode_name(raw[position + 14 : end])
            if name:
                entries.append(DirEntry(name, sector, size, attributes))
            position = (end + 3) & ~3
        return entries

    def list_directory(self, entry: Optional[DirEntry] = None) -> list[DirEntry]:
        """Liste le contenu d'un répertoire (trié : dossiers puis noms)."""
        directory = self.root if entry is None else entry
        if not directory.is_dir:
            raise XDVDFSError(f"{directory.name!r} n'est pas un répertoire.")
        raw = self._directory_table(directory)
        entries = self._parse_entries(raw)
        if not entries and len(raw) >= 14:
            entries = self._scan_entries(raw)
        entries.sort(key=lambda item: (not item.is_dir, item.name.casefold()))
        return entries

    def walk(
        self, entry: Optional[DirEntry] = None
    ) -> Iterator[tuple[tuple[str, ...], DirEntry]]:
        """Parcourt récursivement l'arborescence : ``(chemin, entrée)``.

        La descente est refusée dans un répertoire dont la clé
        ``(sector, size)`` figure déjà parmi ses ancêtres : deux branches
        peuvent partager la même table, mais un cycle ne boucle jamais.
        Le nombre total d'entrées produites est plafonné par
        :data:`MAX_WALK_ENTRIES`.
        """
        root = self.root if entry is None else entry
        root_key = (root.sector, root.size)
        stack: list[
            tuple[tuple[str, ...], Iterator[DirEntry], int, frozenset[tuple[int, int]]]
        ] = [((), iter(self.list_directory(root)), 0, frozenset({root_key}))]
        produced = 0
        while stack:
            parts, iterator, depth, ancestors = stack[-1]
            try:
                child = next(iterator)
            except StopIteration:
                stack.pop()
                continue
            child_parts = parts + (child.name,)
            produced += 1
            if produced > MAX_WALK_ENTRIES:
                raise XDVDFSError(
                    "Image trop volumineuse ou corrompue : trop d'entrées."
                )
            yield child_parts, child
            if child.is_dir and depth < MAX_DIRECTORY_DEPTH:
                key = (child.sector, child.size)
                if key in ancestors:
                    continue
                try:
                    children = self.list_directory(child)
                except XDVDFSError:
                    continue
                if children:
                    stack.append(
                        (child_parts, iter(children), depth + 1, ancestors | {key})
                    )

    def find(self, image_path: str) -> Optional[DirEntry]:
        """Retrouve une entrée par chemin (insensible à la casse)."""
        wanted = [part for part in image_path.replace("\\", "/").split("/") if part]
        if not wanted:
            return self.root
        current = self.root
        for part in wanted:
            match = None
            for child in self.list_directory(current):
                if child.name.casefold() == part.casefold():
                    match = child
                    break
            if match is None:
                return None
            current = match
        return current

    # ------------------------------------------------------------------ #
    # Extraction
    # ------------------------------------------------------------------ #
    def total_size(self, entry: Optional[DirEntry] = None) -> int:
        """Taille cumulée des fichiers d'une entrée (métadonnées seulement)."""
        return self.tree_totals(entry)[0]

    def tree_totals(self, entry: Optional[DirEntry] = None) -> tuple[int, int]:
        """Taille cumulée et nombre de fichiers d'une entrée, en un seul parcours."""
        root = self.root if entry is None else entry
        if not root.is_dir:
            return root.size, 1
        total = 0
        files = 0
        visited: set[tuple[int, int]] = set()
        stack: list[tuple[DirEntry, int]] = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            key = (current.sector, current.size)
            if key in visited or depth > MAX_DIRECTORY_DEPTH:
                continue
            visited.add(key)
            try:
                children = self.list_directory(current)
            except XDVDFSError:
                continue
            for child in children:
                if child.is_dir:
                    stack.append((child, depth + 1))
                else:
                    total += child.size
                    files += 1
        return total, files

    def extract(
        self,
        entry: Optional[DirEntry],
        destination: str | os.PathLike[str],
        *,
        progress: Optional[ProgressCallback] = None,
        cancel: Optional[Callable[[], bool]] = None,
        overwrite: bool = True,
        file_progress: Optional[FileProgressCallback] = None,
    ) -> ExtractStats:
        """Extrait une entrée (fichier ou dossier) vers ``destination``.

        Les fichiers sont copiés par blocs de 1 Mio : la mémoire utilisée
        reste constante, quelle que soit la taille de l'image.

        ``file_progress(faits, total)`` est appelé après chaque fichier terminé,
        ce qui permet de suivre l'avancement en nombre de fichiers.
        """
        target = Path(destination)
        stats = ExtractStats()
        total, files_total = self.tree_totals(entry)
        self._extract(
            self.root if entry is None else entry,
            target,
            stats,
            total,
            files_total,
            progress,
            file_progress,
            cancel,
            overwrite,
            depth=0,
            visited=set(),
        )
        return stats

    def _extract(
        self,
        entry: DirEntry,
        destination: Path,
        stats: ExtractStats,
        total: int,
        files_total: int,
        progress: Optional[ProgressCallback],
        file_progress: Optional[FileProgressCallback],
        cancel: Optional[Callable[[], bool]],
        overwrite: bool,
        depth: int,
        visited: set[tuple[int, int]],
    ) -> None:
        if cancel is not None and cancel():
            raise ExtractionCancelled()

        if entry.is_dir:
            key = (entry.sector, entry.size)
            if depth > MAX_DIRECTORY_DEPTH:
                stats.errors.append(f"Profondeur maximale dépassée : {destination}")
                return
            if key in visited:
                stats.errors.append(f"Répertoire cyclique ignoré : {destination}")
                return
            visited.add(key)
            destination.mkdir(parents=True, exist_ok=True)
            stats.dirs += 1
            try:
                children = self.list_directory(entry)
            except XDVDFSError as exc:
                stats.errors.append(f"{destination}: {exc}")
                return
            used: set[str] = set()
            for child in children:
                name = _unique_name(sanitize_filename(child.name), used)
                try:
                    self._extract(
                        child,
                        destination / name,
                        stats,
                        total,
                        files_total,
                        progress,
                        file_progress,
                        cancel,
                        overwrite,
                        depth + 1,
                        visited,
                    )
                except XDVDFSError as exc:
                    stats.errors.append(f"{child.name}: {exc}")
            return

        destination.parent.mkdir(parents=True, exist_ok=True)
        if not overwrite and destination.exists():
            return

        if entry.size == 0 or entry.sector == 0:
            if entry.size and entry.sector == 0:
                stats.errors.append(
                    f"{entry.name}: secteur invalide (0), fichier laissé vide"
                )
            with open(destination, "wb"):
                pass
            stats.files += 1
            if file_progress is not None:
                file_progress(stats.files, files_total)
            return

        with open(destination, "wb") as output:
            for chunk in self.iter_file(entry, COPY_CHUNK, cancel):
                output.write(chunk)
                stats.bytes += len(chunk)
                if progress is not None:
                    progress(stats.bytes, total, str(destination))
        stats.files += 1
        if file_progress is not None:
            file_progress(stats.files, files_total)


def open_image(path: str | os.PathLike[str]) -> XDVDFSVolume:
    """Raccourci pour :func:`XDVDFSVolume.open`."""
    return XDVDFSVolume.open(path)
