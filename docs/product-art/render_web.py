"""Build the static Pages index: python3 render_web.py /path/to/cachepin/site/tools/index.html."""

import argparse
from html import escape
from pathlib import Path
from string import Template
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from render import ROOT, SVG, read_catalogue


def mark(slug, description):
    svg = ET.parse(ROOT / 'motifs' / f'{slug}.svg').getroot()
    for title in svg.findall(f'{{{SVG}}}title'):
        svg.remove(title)
    svg.set('role', 'img')
    svg.set('aria-label', description)
    return ET.tostring(svg, encoding='unicode')


def demo_label(url):
    if url.endswith('/llms.txt'):
        return 'Agent guide'
    return 'Example' if urlparse(url).netloc == 'github.com' else 'Open demo'


def product_row(product, descriptions):
    slug, name = product['slug'], escape(product['name'])
    claim = escape(' '.join(product['lines']))
    demo, source = (escape(product[k], quote=True) for k in ('demo', 'source'))
    action = demo_label(product['demo'])
    return f'''<li class="entry" id="{slug}"><article aria-labelledby="name-{slug}">
  <div class="glyph">{mark(slug, descriptions[slug])}</div>
  <div class="entry-copy"><h4 id="name-{slug}"><a href="{source}">{name}</a></h4><p>{claim}</p>
  <div class="links"><a href="{demo}" aria-label="{name} — {action}">{action}</a><a href="{source}" aria-label="{name} source">Source</a></div></div>
</article></li>'''


def render_page():
    groups, descriptions = read_catalogue()
    sections, navigation = [], []
    for category, products in groups.items():
        anchor = 'group-' + products[0]['slug']
        label = escape(category)
        navigation.append(f'<li><a href="#{anchor}">{label}</a></li>')
        rows = '\n'.join(product_row(p, descriptions) for p in products)
        sections.append(f'<section class="group" aria-labelledby="{anchor}"><h3 id="{anchor}" tabindex="-1">{label}</h3><ul class="entries">{rows}</ul></section>')
    afterkey = next(p for group in groups.values() for p in group if p['slug'] == 'afterkey')
    template = Template((ROOT / 'tools-page.html.in').read_text(encoding='utf-8'))
    return template.substitute(groups='\n'.join(sections), nav='\n'.join(navigation),
        demo=escape(afterkey['demo'], quote=True), source=escape(afterkey['source'], quote=True),
        count=sum(map(len, groups.values())))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    target = parser.parse_args().output
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_page(), encoding='utf-8')
