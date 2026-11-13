"""Capture une page web officielle (PDF + PNG + texte) comme preuve datée.
Usage : python -m kyc_agent.webcap <url> <out_prefix> [wait_ms]"""
import sys, json
from datetime import datetime, timezone
from pathlib import Path
from playwright.sync_api import sync_playwright

def capture(url, prefix, wait_ms=4000):
    prefix = Path(prefix); prefix.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(locale="pt-BR", viewport={"width": 1280, "height": 1600})
        resp = pg.goto(url, wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(int(wait_ms))
        # bandeau de traçabilité injecté en haut de page
        pg.evaluate("""([u,t]) => {const d=document.createElement('div');
          d.style='font:12px monospace;background:#ffd;border:1px solid #990;padding:4px;margin:4px';
          d.textContent='GF CAPITAL – captura de evidência | URL: '+u+' | UTC: '+t; document.body.prepend(d);}""", [url, ts])
        pg.screenshot(path=str(prefix) + ".png", full_page=True)
        try:
            pg.pdf(path=str(prefix) + ".pdf", format="A4", print_background=True)
        except Exception:
            pass
        text = pg.inner_text("body")
        Path(str(prefix) + ".txt").write_text(text, encoding="utf-8")
        meta = {"url": url, "final_url": pg.url, "status": resp.status if resp else None, "capturado_em_utc": ts}
        Path(str(prefix) + ".json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        b.close()
    print(json.dumps(meta, ensure_ascii=False)); print(text[:1500])

if __name__ == "__main__":
    capture(*sys.argv[1:])
