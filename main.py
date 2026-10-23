"""Point d'entrée : interface graphique ou mode ligne de commande.

Exemples ::

    python main.py                              # interface graphique
    python main.py game.iso                     # GUI, image préchargée
    python main.py --iso game.iso --info        # métadonnées de l'image
    python main.py --iso game.iso --list        # arborescence complète
    python main.py --iso game.iso --extract media/audio/theme.xma -o sortie
    python main.py --iso game.iso --all -o sortie
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from xdvdfs import (
    XDVDFSError,
    XDVDFSVolume,
    human_size,
    sanitize_filename,
)


def _print_tree(volume: XDVDFSVolume) -> None:
    for parts, entry in volume.walk():
        indent = "  " * (len(parts) - 1)
        if entry.is_dir:
            print(f"{indent}{entry.name}/")
        else:
            print(f"{indent}{entry.name}  ({human_size(entry.size)})")


def _print_info(volume: XDVDFSVolume) -> None:
    entries = list(volume.walk())
    files = [entry for _, entry in entries if not entry.is_dir]
    total = sum(entry.size for entry in files)
    print(f"Image        : {volume.path}")
    print(f"Taille       : {human_size(volume.file_size)} ({volume.file_size} octets)")
    print(f"Format       : {volume.format_name}")
    print(f"Partition    : 0x{volume.partition_offset:X}")
    print(
        f"Racine       : secteur {volume.root_sector}, "
        f"{volume.root_size} octets de table"
    )
    print(
        f"Contenu      : {len(files)} fichier(s), "
        f"{len(entries) - len(files)} dossier(s), {human_size(total)}"
    )


def _extract(volume: XDVDFSVolume, image_path: str, out_dir: str) -> int:
    entry = volume.find(image_path)
    if entry is None:
        print(f"Chemin introuvable dans l'image : {image_path}", file=sys.stderr)
        return 1

    out_root = Path(out_dir)
    if entry.name:
        parts = [sanitize_filename(part) for part in image_path.split("/") if part]
        target = out_root.joinpath(*parts)
    else:
        target = out_root

    print(f"Extraction de /{image_path or ''} vers {target}…")
    stats = volume.extract(
        entry,
        target,
        progress=lambda done, total, current: None,
    )
    print(
        f"Terminé : {stats.files} fichier(s), {stats.dirs} dossier(s), "
        f"{human_size(stats.bytes)}."
    )
    for message in stats.errors[:10]:
        print(f"  avertissement : {message}", file=sys.stderr)
    if len(stats.errors) > 10:
        print(f"  … et {len(stats.errors) - 10} autre(s)", file=sys.stderr)
    return 0 if not stats.errors else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xbox-image-browser",
        description=(
            "Explorateur / extracteur d'images Xbox 360 (XDVDFS : XISO, XGD2, XGD3). "
            "Sans action, lance l'interface graphique."
        ),
    )
    parser.add_argument("image", nargs="?", help="image .iso / .xiso à ouvrir")
    parser.add_argument("--iso", dest="iso_option", help="alternative à l'argument image")
    parser.add_argument(
        "--partition-offset",
        type=lambda value: int(value, 0),
        default=None,
        metavar="OFFSET",
        help="forcer l'offset de la partition de jeu (ex. 0xFD90000)",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--info", action="store_true", help="afficher les métadonnées")
    actions.add_argument("--list", action="store_true", help="lister l'arborescence")
    actions.add_argument(
        "--extract", metavar="CHEMIN", help="extraire un fichier ou dossier de l'image"
    )
    actions.add_argument(
        "--all", action="store_true", help="extraire toute l'image dans --out"
    )
    parser.add_argument(
        "-o",
        "--out",
        default=".",
        help="dossier de destination pour --extract / --all (défaut : .)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    image = args.image or args.iso_option

    wants_cli = bool(args.info or args.list or args.extract or args.all)
    if not wants_cli:
        from gui import run  # import tardif : le CLI fonctionne sans PyQt6

        return run(image)

    if not image:
        print("Erreur : indiquez une image avec --iso (ou en argument).", file=sys.stderr)
        return 2

    try:
        with XDVDFSVolume.open(image, args.partition_offset) as volume:
            if args.info:
                _print_info(volume)
                return 0
            if args.list:
                _print_tree(volume)
                return 0
            if args.extract is not None:
                return _extract(volume, args.extract, args.out)
            if args.all:
                return _extract(volume, "", args.out)
    except (XDVDFSError, OSError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
