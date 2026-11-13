"""Extrait le texte de chaque PDF d'un dossier vers work/text/ et produit un inventaire JSON.
Usage : python -m kyc_agent.extract <dossier_source> <dossier_case>
"""
import json, sys, hashlib
from pathlib import Path
import pymupdf

def main(src: str, case: str):
    src_p, case_p = Path(src), Path(case)
    out = case_p / "work" / "text"
    out.mkdir(parents=True, exist_ok=True)
    inv = []
    for f in sorted(src_p.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(src_p).as_posix()
        item = {"path": rel, "size": f.stat().st_size,
                "sha256": hashlib.sha256(f.read_bytes()).hexdigest()[:16]}
        if f.suffix.lower() == ".pdf":
            try:
                doc = pymupdf.open(f)
                txt = "\n".join(f"--- page {i+1} ---\n" + p.get_text() for i, p in enumerate(doc))
                item.update(pages=doc.page_count, chars=len(txt.strip()),
                            scanned=len(txt.strip()) < 50 * doc.page_count)
                (out / (item["sha256"] + ".txt")).write_text(txt, encoding="utf-8")
            except Exception as e:
                item["error"] = str(e)
        inv.append(item)
    (case_p / "work" / "inventory.json").write_text(json.dumps(inv, ensure_ascii=False, indent=1), encoding="utf-8")
    for i in inv:
        print(f"{i['sha256']}  p={i.get('pages','-'):>3}  chars={i.get('chars','-'):>6}  {'SCAN ' if i.get('scanned') else '     '}{i['path']}")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
