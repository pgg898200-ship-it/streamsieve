"""Build the product index and social cards using the local, editable SVG marks."""

import argparse
import json
import os
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
PAPER = "#FAFAFA"
INK = "#141619"
SECONDARY = "#555C65"
RULE = "#DADDE1"
ACCENT = "#315BFF"
SANS = "'Instrument Sans', Arial, sans-serif"
MONO = "'IBM Plex Mono', 'DejaVu Sans Mono', monospace"
INDEX_URL = "https://pgg898200-ship-it.github.io/cachepin/tools/"


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
    cell_width, cell_height = 240, 196
    width = cell_width * columns
    height = cell_height * ceil(len(products) / columns)
    desc = " ".join(f"{p['name']}. {alts[p['slug']]}" for p in products)
    root = canvas(width, height, category, desc)
    element(root, "rect", width=width, height=height, fill=PAPER)
    for i, product in enumerate(products):
        row, col = divmod(i, columns)
        x, y = col * cell_width, row * cell_height
        if row:
            element(root, "path", d=f"M{x+12} {y}h{cell_width-24}", stroke=RULE)
        place_mark(root, product, x + 57, y + 12, 126)
        label(root, x + 120, y + 164, product["name"], 22, font_weight="600", text_anchor="middle")
    return root


def collection_plate(products, alts):
    products = sorted(products, key=lambda product: product["name"].casefold())
    columns, cell_width, cell_height = 5, 184, 147
    left, top = 40, 127
    width = left * 2 + columns * cell_width
    height = top + cell_height * ceil(len(products) / columns) + 20
    desc = "Product diagrams in alphabetical order. " + " ".join(
        f"{p['name']}. {alts[p['slug']]}" for p in products
    )
    root = canvas(width, height, "PGG Engineering tool index", desc)
    element(root, "rect", width=width, height=height, fill=PAPER)
    label(root, left, 76, "PGG Engineering", 49, font_weight="600", letter_spacing="-2")
    label(root, width-left, 74, f"{len(products)} tools", 19, font=MONO, color=SECONDARY, text_anchor="end")
    element(root, "path", d=f"M{left} 104H{width-left}", stroke=RULE)
    for i, product in enumerate(products):
        row, col = divmod(i, columns)
        x, y = left + cell_width * col, top + row * cell_height
        if row and not col:
            element(root, "path", d=f"M{left} {y-4}H{width-left}", stroke=RULE)
        place_mark(root, product, x + 40, y, 104)
        label(root, x + 92, y + 123, product["name"], 20, font_weight="500", text_anchor="middle")
    return root


def share_card(product, category, mark_alt):
    name = product["name"]
    alt = f"{name}. {' '.join(product['lines'])} {mark_alt}"
    root = canvas(1200, 630, name, alt)
    element(root, "rect", width=1200, height=630, fill=PAPER)
    label(root, 64, 76, "PGG Engineering", 31, font_weight="600", letter_spacing="-1", id="brand")
    label(root, 1136, 73, category, 20, font=MONO, color=SECONDARY, text_anchor="end", id="family")
    element(root, "path", d="M64 105H1136 M64 528H1136", stroke=RULE, stroke_width=1.5)
    name_size = min(105, int(1080 / len(name)))
    label(root, 58, 272, name, name_size, font_weight="600", letter_spacing="-4", id="product-name")
    for i, line in enumerate(product["lines"]):
        label(root, 64, 352 + i * 44, line, 35, color=SECONDARY, id=f"claim-{i+1}")
    place_mark(root, product, 744, 135, 370)
    if "space" in product:
        footer = "hf.co" + urlparse(product["space"]).path
    else:
        repository = urlparse(product["source"]).path.strip("/").split("/")[:2]
        footer = "github.com/" + "/".join(repository)
    label(root, 64, 574, footer, 20, font=MONO, color=SECONDARY, id="source")
    return root, alt


def index_cover(catalogue):
    products = sorted(
        [p for group in catalogue.values() for p in group],
        key=lambda p: p["name"].casefold(),
    )
    count = len(products)
    desc = f"PGG Engineering tool index: {count} products grouped by operation. A grid on the right shows each product’s diagram."
    root = canvas(1200, 630, "PGG Engineering tool index", desc)
    element(root, "rect", width=1200, height=630, fill=PAPER)
    label(root, 64, 76, "PGG Engineering", 31, font_weight="600", letter_spacing="-1")
    label(root, 1136, 73, f"{count} tools", 20, font=MONO, color=SECONDARY, text_anchor="end")
    element(root, "path", d="M64 105H1136 M64 528H1136", stroke=RULE, stroke_width=1.5)
    label(root, 59, 238, "Tool index", 94, font_weight="600", letter_spacing="-4.5")
    for i, category in enumerate(catalogue):
        label(root, 64, 297 + i * 39, category, 27, color=SECONDARY)
    columns = 5
    rows = ceil(count / columns)
    size = min(72, 374 / rows, 426 / columns)
    for i, product in enumerate(products):
        row, col = divmod(i, columns)
        place_mark(root, product, 1136 - columns * size + col * size, 126 + row * size, size)
    label(root, 64, 574, urlparse(INDEX_URL).netloc + urlparse(INDEX_URL).path,
          20, font=MONO, color=SECONDARY)
    return root, desc


def export_png(svg, png):
    if shutil.which("rsvg-convert"):
        command = ["rsvg-convert", "-o", str(png), str(svg)]
    elif shutil.which("inkscape"):
        command = ["inkscape", str(svg), f"--export-filename={png.resolve()}"]
    else:
        raise SystemExit("PNG export needs rsvg-convert or Inkscape on PATH.")
    system_config = Path(os.environ.get("FONTCONFIG_FILE", "/etc/fonts/fonts.conf"))
    if not (system_config.is_file() and (ROOT / "fonts").is_dir()):
        subprocess.run(command, check=True, capture_output=True)
        return

    # Fontconfig can register the bundled WOFF2s for this render alone.
    with TemporaryDirectory() as scratch:
        config = ET.Element("fontconfig")
        ET.SubElement(config, "include").text = str(system_config.resolve())
        ET.SubElement(config, "dir").text = str(ROOT / "fonts")
        ET.SubElement(config, "cachedir").text = scratch
        config_path = Path(scratch) / "fonts.conf"
        ET.ElementTree(config).write(config_path, encoding="utf-8", xml_declaration=True)
        env = dict(os.environ, FONTCONFIG_FILE=str(config_path))
        subprocess.run(command, check=True, capture_output=True, env=env)


def write_index(catalogue, alts):
    products = [product for family in catalogue.values() for product in family]
    write_svg(collection_plate(products, alts), ROOT / "sheets" / "all-products.svg")
    write_svg(index_cover(catalogue)[0], ROOT / "sheets" / "share.svg")
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
        "Each mark is a schematic of one operation on a common 96-unit grid. Graphite (`#141619`) draws its structure, secondary gray (`#555C65`) labels it, and light gray (`#DADDE1`) separates rows on the off-white field (`#FAFAFA`). Blue (`#315BFF`) singles out the revision, selected value or decision boundary. Instrument Sans sets names and captions. IBM Plex Mono sets technical labels. System fonts provide fallbacks.", "",
        "The diagrams illustrate inputs, state changes and outputs. A measured figure also needs units, a labeled scale and a source. Animate a transition once when its order matters. A control response often takes 180–350 ms, while a multi-step explanation may need up to two seconds. With reduced motion, show the final state immediately and describe it in text.", "",
        "## Reuse a mark", "",
        "The SVGs in [motifs](motifs) scale to any size. Their adjacent JSON files describe the artwork for screen readers. [products.json](products.json) contains each card’s caption and destinations. The family sheets above also work in print.", "",
        "From this directory, run one of these commands with Python 3.9 or later. SVG generation uses the standard library. PNG export uses a local copy of Inkscape or librsvg’s `rsvg-convert`.", "",
        "The [fonts](fonts) directory includes the two WOFF2 files and their OFL notices. On Linux with Fontconfig and WOFF2 support, PNG export loads them directly from this checkout. Elsewhere, install Instrument Sans and IBM Plex Mono for matching output. Without those families, the SVG uses Arial or the system sans-serif for names and captions, and DejaVu Sans Mono or the system monospace for technical labels.", "",
        "```sh", "python3 render.py --product rankpatch --out build", "python3 render.py --spaces --png --out build", "python3 render.py --cover --png --out build", "python3 render.py --index", "```", "",
        "Each card is 1200 × 630. Each product folder contains its card and plain-text description under `art/`. Omit `--product` and `--spaces` to export all product cards. The [index cover](sheets/share.svg) shows the whole collection. Use `--cover` to export it and its description to the output directory.", "",
        "The [live tool index](https://pgg898200-ship-it.github.io/cachepin/tools/) uses the same catalogue, captions and marks. To rebuild the page in a CachePin checkout, run:", "",
        "```sh", "python3 render_web.py /path/to/cachepin/site/tools/index.html", "```", ""])
    (ROOT / "README.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--index", action="store_true", help="refresh this directory’s README and family sheets")
    action.add_argument("--cover", action="store_true", help="render the 1200 × 630 portfolio cover")
    action.add_argument("--product", metavar="SLUG", help="render one product")
    action.add_argument("--spaces", action="store_true", help="render only products with a Hugging Face Space")
    parser.add_argument("--png", action="store_true", help="render PNG instead of SVG")
    parser.add_argument("--out", type=Path, default=Path("build"))
    args = parser.parse_args()
    catalogue, alts = read_catalogue()
    if args.index:
        write_index(catalogue, alts)
        return
    if args.cover:
        args.out.mkdir(parents=True, exist_ok=True)
        cover, description = index_cover(catalogue)
        path = args.out / "portfolio.svg"
        write_svg(cover, path)
        (args.out / "portfolio.alt.txt").write_text(description + "\n", encoding="utf-8")
        if args.png:
            export_png(path, args.out / "portfolio.png")
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
