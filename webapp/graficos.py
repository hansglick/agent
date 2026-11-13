"""Petits graphiques SVG (sans bibliothèque) pour le tableau de bord — fond noir.

Palette catégorielle validée sur la surface #0d0d0d (skill dataviz, validate_palette.js : ALL PASS) :
entrée = bleu #3987e5, sortie = orange #d95926, total = vert d'eau #199e70.
Chaque point / barre porte une infobulle (<title>) ; le texte reste en encre neutre, jamais à la couleur de la série.
"""
from __future__ import annotations

from html import escape

from markupsafe import Markup

SERIES = (("entrada", "#3987e5"), ("saida", "#d95926"), ("total", "#199e70"))
INK, MUTED, GRID = "#e6e6e6", "#9a9a9a", "#2a2a2a"


def _escala(vmax, h, top, bottom):
    vmax = vmax or 1
    return lambda v: top + (h - top - bottom) * (1 - v / vmax)


def _rot(p, rotulos):
    """Libellé d'infobulle : « v1 » ou « v1 ✕ — tentative sans version »."""
    return p["rotulo"] + (f" — {rotulos['falha']}" if p.get("falha") else "")


def linhas_tokens(serie, rotulos, fmt_int, w=300, h=130) -> Markup:
    """Trois courbes (entrée, sortie, total) par mise à jour. serie = [{"versao", "entrada", "saida", "total"}]."""
    if not serie:
        return Markup("")
    left, right, top, bottom = 8, 8, 10, 22
    n = len(serie)
    xs = [left + (w - left - right) * (i / (n - 1) if n > 1 else 0.5) for i in range(n)]
    y = _escala(max(p["total"] for p in serie), h, top, bottom)
    out = [f'<svg class="viz" viewBox="0 0 {w} {h}" role="img" aria-label="{escape(rotulos["titulo"])}">']
    out.append(f'<line x1="{left}" x2="{w - right}" y1="{h - bottom}" y2="{h - bottom}" stroke="{GRID}" stroke-width="1"/>')
    for x, p in zip(xs, serie):
        out.append(f'<text x="{x:.1f}" y="{h - 6}" fill="{MUTED}" font-size="10" text-anchor="middle">{escape(p["rotulo"])}</text>')
    # total dessiné en premier et en pointillé : il reste visible même quand il se confond presque avec l'entrée
    for k, cor in sorted(SERIES, key=lambda s: s[0] != "total"):
        pts = " ".join(f"{x:.1f},{y(p[k]):.1f}" for x, p in zip(xs, serie))
        dash = ' stroke-dasharray="5 4"' if k == "total" else ""
        if n > 1:
            out.append(f'<polyline points="{pts}" fill="none" stroke="{cor}" stroke-width="2"{dash} stroke-linejoin="round" stroke-linecap="round"/>')
        for x, p in zip(xs, serie):
            # tentative sans version : point creux (et « ✕ » dans l'étiquette — jamais la couleur seule)
            pt = f'fill="#0d0d0d" stroke="{cor}"' if p.get("falha") else f'fill="{cor}" stroke="#0d0d0d"'
            out.append(f'<g class="pt"><circle cx="{x:.1f}" cy="{y(p[k]):.1f}" r="4" {pt} stroke-width="2"/>'
                       f'<circle cx="{x:.1f}" cy="{y(p[k]):.1f}" r="10" fill="transparent">'
                       f'<title>{escape(_rot(p, rotulos))} · {escape(rotulos[k])} : {fmt_int(p[k])}</title></circle></g>')
    out.append("</svg>")
    leg = "".join(f'<span><i class="{"dash" if k == "total" else ""}" style="--c:{cor}"></i>{escape(rotulos[k])}</span>' for k, cor in SERIES)
    return Markup(f'<div class="viz-leg">{leg}</div>' + "".join(out))


def barras_custo(serie, rotulos, fmt_usd, w=560, h=240) -> Markup:
    """Facture par mise à jour (une barre par version mesurée), valeur directe au-dessus de chaque barre."""
    if not serie:
        return Markup("")
    left, right, top, bottom = 12, 12, 26, 26
    n = len(serie)
    slot = (w - left - right) / n
    bw = min(56, slot * 0.6)
    y = _escala(max(p["custo"] for p in serie), h, top, bottom)
    base = h - bottom
    out = [f'<svg class="viz" viewBox="0 0 {w} {h}" role="img" aria-label="{escape(rotulos["titulo"])}">',
           f'<line x1="{left}" x2="{w - right}" y1="{base}" y2="{base}" stroke="{GRID}" stroke-width="1"/>']
    for i, p in enumerate(serie):
        cx = left + slot * (i + 0.5)
        top_y = min(y(p["custo"]), base - 1)
        r = min(4, (base - top_y) / 2)
        x0 = cx - bw / 2
        # barre aux coins supérieurs arrondis, ancrée sur la ligne de base
        d = (f"M{x0:.1f},{base} V{top_y + r:.1f} Q{x0:.1f},{top_y:.1f} {x0 + r:.1f},{top_y:.1f} "
             f"H{x0 + bw - r:.1f} Q{x0 + bw:.1f},{top_y:.1f} {x0 + bw:.1f},{top_y + r:.1f} V{base} Z")
        barra = 'fill="none" stroke="#d95926" stroke-width="2"' if p.get("falha") else 'fill="#d95926"'
        out.append(f'<g class="pt"><path d="{d}" {barra}/>'
                   f'<rect x="{cx - slot / 2:.1f}" y="{top}" width="{slot:.1f}" height="{base - top}" fill="transparent">'
                   f'<title>{escape(_rot(p, rotulos))} · {escape(fmt_usd(p["custo"]))}</title></rect></g>')
        out.append(f'<text x="{cx:.1f}" y="{top_y - 6:.1f}" fill="{INK}" font-size="12" text-anchor="middle">{escape(fmt_usd(p["custo"]))}</text>')
        out.append(f'<text x="{cx:.1f}" y="{h - 8}" fill="{MUTED}" font-size="11" text-anchor="middle">{escape(p["rotulo"])}</text>')
    out.append("</svg>")
    return Markup("".join(out))
