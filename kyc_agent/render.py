"""Rend des pages PDF en PNG pour lecture visuelle. Usage : python -m kyc_agent.render <pdf> <out_dir> [pages ex: 1-3] [zoom]"""
import sys
from pathlib import Path
import pymupdf

def main(pdf, out, pages=None, zoom=1.6):
    doc = pymupdf.open(pdf)
    rng = range(doc.page_count)
    if pages:
        a, _, b = pages.partition("-")
        rng = range(int(a) - 1, int(b or a))
    Path(out).mkdir(parents=True, exist_ok=True)
    stem = Path(pdf).stem[-40:].replace(" ", "_")
    for i in rng:
        p = Path(out) / f"{stem}_p{i+1}.png"
        doc[i].get_pixmap(matrix=pymupdf.Matrix(float(zoom), float(zoom))).save(p)
        print(p)

if __name__ == "__main__":
    main(*sys.argv[1:])
