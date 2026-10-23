"""Génère assets/icon.png et assets/xbox_image_browser.ico.

Usage : python tools/make_icon.py
Dépend de Pillow (uniquement pour régénérer l'icône, pas au runtime).
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 1024
ASSETS = Path(__file__).resolve().parent.parent / "assets"


def build_icon() -> Image.Image:
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # Fond arrondi bleu nuit
    draw.rounded_rectangle(
        [24, 24, SIZE - 24, SIZE - 24], radius=210, fill=(14, 23, 38, 255)
    )

    # Disque (anneau vert, centre sombre, trou central)
    draw.ellipse(
        [130, 140, 730, 740],
        fill=(11, 42, 24, 255),
        outline=(122, 201, 67, 255),
        width=36,
    )
    draw.ellipse([390, 400, 470, 480], fill=(14, 23, 38, 255))

    # Reflet du disque
    draw.arc([190, 200, 670, 680], start=196, end=268, fill=(174, 231, 132, 255), width=18)

    # Loupe
    draw.ellipse(
        [560, 555, 845, 840], outline=(240, 246, 242, 255), width=58
    )
    draw.line(
        [(818, 818), (946, 946)], fill=(240, 246, 242, 255), width=76
    )

    return image


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    icon = build_icon()

    png = icon.resize((512, 512), Image.Resampling.LANCZOS)
    png.save(ASSETS / "icon.png", optimize=True)

    ico_sizes = [16, 24, 32, 48, 64, 128, 256]
    icon.save(
        ASSETS / "xbox_image_browser.ico",
        format="ICO",
        sizes=[(size, size) for size in ico_sizes],
    )
    print(f"Icônes écrites dans {ASSETS}")


if __name__ == "__main__":
    main()
