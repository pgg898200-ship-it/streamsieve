"""Build the product index and social cards using the local, editable SVG marks."""

import argparse
import json
from math import ceil
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent
SVG = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG)
PAPER = "#fff3d7"
INK = "#171729"
BLUE = "#384cfa"
RED = "#ef543c"
SANS = "'DejaVu Sans', Arial, sans-serif"
SERIF = "'DejaVu Serif', Georgia, serif"
MONO = "'DejaVu Sans Mono', monospace"


def element(parent, tag, **attributes):
    return ET.SubElement(parent, f"{{{SVG}}}{tag}", {k.replace("_", "-"): str(v) for k, v in attributes.items()})


def label(parent, x, y, value, size, *, font=SANS, color=INK, **attributes):
    node = element(parent, "text", x=x, y=y, font_size=size, font_family=font, fill=color, **attributes)
    node.text = value
    return node


def canvas(width, height, title, desc):
    node = ET.Element(f"{{{SVG}}}svg", {
        "width": str(width), "height": str(height), "viewBox": f"0 0 {width} {height}",
        "role": "img", "aria-labelledby": "title desc",
    })
    element(node, "title", id="title").text = title
    element(node, "desc", id="desc").text = desc
    return node


def place_mark(parent, product, x, y, size):
    # Nest the original viewBox so that every mark retains its proportions.
    mark = ET.parse(ROOT / "motifs" / (product["slug"] + ".svg")).getroot()
    mark.attrib.update(x=str(x), y=str(y), width=str(size), height=str(size))
    mark.set("aria-hidden", "true")
    parent.append(mark)


def write_svg(node, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(node, space="  ")
    path.write_bytes(ET.tostring(node, encoding="utf-8") + b"\n")


def read_catalogue():
    catalogue = json.loads((ROOT / "products.json").read_text(encoding="utf-8"))
    alts = {}
    for path in sorted((ROOT / "motifs").glob("*.json")):
        for slug, description in json.loads(path.read_text(encoding="utf-8")).items():
            if slug in alts:
                raise ValueError(f"Duplicate mark description for {slug}")
            alts[slug] = description
    slugs = [product["slug"] for group in catalogue.values() for product in group]
    if len(set(slugs)) != len(slugs) or set(slugs) != set(alts):
        raise ValueError("Product slugs and mark description keys must match one-to-one.")
    return catalogue, alts


def contact_sheet(products, category, alts):
    columns = min(3, len(products))
    width, height = 260 * columns, 230 * ceil(len(products) / columns)
    desc = " ".join(f"{p['name']}. {alts[p['slug']]}" for p in products)
    root = canvas(width, height, category, desc)
    element(root, "rect", width=width, height=height, fill=PAPER)
    for i, product in enumerate(products):
        row, col = divmod(i, columns)
        x, y = col * 260, row * 230
        if col:
            element(root, "path", d=f"M{x} {y+12}v206", stroke=INK, stroke_opacity=".18")
        if row:
            element(root, "path", d=f"M{x+12} {y}h236", stroke=INK, stroke_opacity=".18")
        place_mark(root, product, x + 39, y + 7, 182)
        label(root, x + 130, y + 211, product["name"], 20,
              font=SERIF, font_weight="bold", text_anchor="middle")
    return root


def collection_plate(products, alts):
    products = sorted(products, key=lambda product: product["name"].casefold())
    columns, cell_width, cell_height = 5, 186, 170
    grid_x, grid_y = 35, 132
    rows = ceil(len(products) / columns)
    width = grid_x * 2 + cell_width * columns
    height = grid_y + cell_height * rows + 30
    desc = "Alphabetical product marks. " + " ".join(
        f"{p['name']}. {alts[p['slug']]}" for p in products
    )
    root = canvas(width, height, f"PGG Engineering / {len(products)} tools", desc)
    element(root, "rect", width=width, height=height, fill=PAPER)
    label(root, 35, 89, "PGG Engineering", 60, font=SERIF, font_weight="bold", letter_spacing="-2.5")
    label(root, width - 36, 83, f"/  {len(products)} tools", 30, font=MONO, text_anchor="end")
    element(root, "path", d=f"M35 113H{width-35}", stroke=INK, stroke_width=3)
    for row in range(1, rows):
        y = grid_y + row * cell_height
        element(root, "path", d=f"M35 {y}H{width-35}", stroke=INK, stroke_opacity=".18")
    for col in range(1, columns):
        x = grid_x + cell_width * col
        element(root, "path", d=f"M{x} {grid_y}V{height-30}", stroke=INK, stroke_opacity=".18")
    for i, product in enumerate(products):
        row, col = divmod(i, columns)
        x = grid_x + col * cell_width
        y = grid_y + row * cell_height
        place_mark(root, product, x + 27, y + 3, 132)
        label(root, x + 93, y + 153, product["name"], 17.5,
              font=SERIF, font_weight="bold", text_anchor="middle")
    return root


def share_card(product, category, mark_alt):
    name = product["name"]
    claim = " ".join(product["lines"])
    alt = f"{name}. {claim} {mark_alt}"
    root = canvas(1200, 630, name, alt)
    element(root, "rect", width=1200, height=630, fill=INK)
    # A single register notch aligns the mark with the product name.
    element(root, "path", d="M0 0H496V263a14 14 0 0 0 0 28V630H0Z", fill=PAPER)
    label(root, 54, 76, "PGG Engineering", 28, font=SERIF, font_weight="bold", id="brand")
    place_mark(root, product, 38, 127, 402)
    label(root, 555, 77, category, 22, font=MONO, color=PAPER, id="family")
    element(root, "path", d="M555 111H1146", stroke=PAPER, stroke_opacity=".43", stroke_width=1)
    size = min(88, int(865 / max(1, len(name))))
    label(root, 550, 282, name, size, font=SERIF, color=PAPER, font_weight="bold", letter_spacing="-3", id="product-name")
    for i, line in enumerate(product["lines"]):
        label(root, 555, 368 + i * 48, line, 35, color=PAPER, id=f"claim-{i+1}")
    element(root, "path", d="M555 520H1146", stroke=PAPER, stroke_opacity=".43", stroke_width=1)
    if "space" in product:
        footer = "hf.co" + urlparse(product["space"]).path
    else:
        repository = urlparse(product["source"]).path.strip("/").split("/")[:2]
        footer = "github.com/" + "/".join(repository)
    label(root, 555, 568, footer, 19, font=MONO, color=PAPER, id="source")
    return root, alt


def export_png(svg, png):
    if shutil.which("rsvg-convert"):
        command = ["rsvg-convert", "-o", str(png), str(svg)]
    elif shutil.which("inkscape"):
        command = ["inkscape", str(svg), f"--export-filename={png.resolve()}"]
    else:
        raise SystemExit("PNG export needs rsvg-convert or Inkscape on PATH.")
    subprocess.run(command, check=True, capture_output=True)


def write_index(catalogue, alts):
    products = [product for family in catalogue.values() for product in family]
    write_svg(collection_plate(products, alts), ROOT / "sheets" / "all-products.svg")
    lines = ["# PGG Engineering product marks", "",
             "Choose a tool by the operation you need. Each product has editable vector artwork, source and a working example.", "",
             f"[View or print all {len(products)} product marks](sheets/all-products.svg)", ""]
    for category, products in catalogue.items():
        filename = re.sub(r"[^a-z]+", "-", category.lower()).strip("-") + ".svg"
        write_svg(contact_sheet(products, category, alts), ROOT / "sheets" / filename)
        names = ", ".join(p["name"] for p in products)
        lines.extend([f"## {category}", "", f"![Product marks for {names}.](sheets/{filename})", "",
                      "| Product | What it does | Links |", "| --- | --- | --- |"])
        for product in products:
            claim = " ".join(product["lines"])
            links = f"[Example]({product['demo']}) · [Artwork](motifs/{product['slug']}.svg)"
            lines.append(f"| [{product['name']}]({product['source']}) | {claim} | {links} |")
        lines.append("")
    lines.extend(["## Visual rules", "",
        "Ink (`#171729`) carries type and outlines. Paper (`#fff3d7`) supplies the field and negative space. Cobalt (`#384cfa`) distinguishes a second shape or state. Vermilion (`#ef543c`) draws attention to a change. Pair color with shape or position. Local DejaVu Serif sets names, Sans sets captions, and Sans Mono sets categories and addresses. Each has a system fallback. Use the individual mark for a square thumbnail.", "",
        "A mark identifies one product operation. A figure showing a measured result needs a labeled scale, units, and a trace or benchmark source. Use motion when the sequence explains the operation. A control response often takes 180–350 ms. An expanded sequence may need up to about two seconds. Play it once. With reduced motion, show the completed state immediately. Keep the same result visible in the static image and its text description.", "",
        "## Reuse a mark", "",
        "The SVGs in [motifs](motifs) scale to any size. Their adjacent JSON files describe the artwork for screen readers. [products.json](products.json) contains each card’s caption and destinations. The family sheets above also work in print.", "",
        "From this directory, run one of these commands with Python 3.9 or later. SVG generation uses the standard library. PNG export uses a local copy of Inkscape or librsvg’s `rsvg-convert`.", "",
        "```sh", "python3 render.py --product rankpatch --out build", "python3 render.py --spaces --png --out build", "python3 render.py --index", "```", "",
        "Each card is 1200 × 630. The output directory contains one folder per product, with the card and its plain-text image description under `art/`. Omit `--product` and `--spaces` to export the full collection.", ""])
    (ROOT / "README.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--index", action="store_true", help="refresh this directory’s README and family sheets")
    action.add_argument("--product", metavar="SLUG", help="render one product")
    action.add_argument("--spaces", action="store_true", help="render only products with a Hugging Face Space")
    parser.add_argument("--png", action="store_true", help="render PNG instead of SVG")
    parser.add_argument("--out", type=Path, default=Path("build"))
    args = parser.parse_args()
    catalogue, alts = read_catalogue()
    if args.index:
        write_index(catalogue, alts)
        return
    selected = [(c, p) for c, products in catalogue.items() for p in products
                if (not args.product or p["slug"] == args.product) and (not args.spaces or "space" in p)]
    if not selected:
        parser.error("No matching products.")
    with TemporaryDirectory() as temporary:
        for category, product in selected:
            directory = args.out / product["slug"] / "art"
            directory.mkdir(parents=True, exist_ok=True)
            card, alt = share_card(product, category, alts[product["slug"]])
            (directory / "share.alt.txt").write_text(alt + "\n", encoding="utf-8")
            if args.png:
                scratch = Path(temporary) / (product["slug"] + ".svg")
                write_svg(card, scratch)
                export_png(scratch, directory / "share.png")
            else:
                write_svg(card, directory / "share.svg")


if __name__ == "__main__":
    main()
