"""Prépare la version portable de l'application (Windows 64 bits), SANS AUCUN document ni dossier client.

    .venv\\Scripts\\python -X utf8 portage\\preparer_portage.py [destination]     (défaut : C:\\projets\\GF-KYC-Agent-portable)

Contenu produit (aucune installation nécessaire sur l'ordinateur cible) :
    Lancer GF KYC.cmd        double-clic : démarre l'application et ouvre le navigateur
    LISEZMOI.txt
    app\\                     code (webapp, kyc_agent) + guides de référence (data\\format_output.txt, data\\job_guidelines*.pdf)
    runtime\\python\\          Python 3.14 + bibliothèques de l'application (copie de .venv, hors ligne)
    runtime\\navigateurs\\     Chromium headless (captures des sites officiels par l'agent)
    donnees\\cases\\           VIDE : les dossiers seront créés dans l'application

Exclus volontairement : cases\\, sauvegardes\\, data\\files\\ (pièces du dossier TC-187), kyc_agent\\cases_tc187_v2.py
(script propre à TC-187), tests\\, journaux, __pycache__. Une vérification finale refuse le paquet si un document
(pdf, image, tableur, zip…) se trouve ailleurs que dans les guides de référence autorisés.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = Path(sys.argv[1] if len(sys.argv) > 1 else r"C:\projets\GF-KYC-Agent-portable").resolve()
VENV_SP = ROOT / ".venv" / "Lib" / "site-packages"
NAVIG = Path(os.environ["LOCALAPPDATA"]) / "ms-playwright"

CODE = ["webapp", "kyc_agent"]
DATA_OK = ["format_output.txt", "job_guidelines.pdf", "job_guidelines_v2.pdf"]  # guides de référence, pas des pièces client
EXCLUS_CODE = {"cases_tc187_v2.py"}
IGN = shutil.ignore_patterns("__pycache__", "*.pyc", "*.log", ".pytest_cache")
DOCS = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".webp", ".xlsx", ".xls", ".csv", ".docx", ".doc", ".zip",
        ".msg", ".eml", ".heic"}


def base_python() -> Path:
    cfg = (ROOT / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8")
    home = next(l.split("=", 1)[1].strip() for l in cfg.splitlines() if l.startswith("home"))
    return Path(home)


def copiar_python(dest: Path):
    src = base_python()
    ign_base = shutil.ignore_patterns("__pycache__", "*.pyc", "Doc", "Scripts", "include", "libs", "tcl", "test", "idlelib",
                                      "tkinter", "turtledemo", "__install__.json", "site-packages")
    shutil.copytree(src, dest, ignore=ign_base)
    shutil.copytree(VENV_SP, dest / "Lib" / "site-packages", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "_pytest",
                                                                                           "pytest", "pytest-*"))


def copiar_navegador(dest: Path):
    shells = sorted(NAVIG.glob("chromium_headless_shell-*"))
    if not shells:
        raise SystemExit(f"Chromium headless introuvable dans {NAVIG} (python -m playwright install chromium-headless-shell)")
    shutil.copytree(shells[-1], dest / shells[-1].name)


def verificar(dest: Path):
    """Refuse le paquet s'il contient un document hors des guides autorisés, ou une trace de dossier client."""
    ok_docs = {(dest / "app" / "data" / n).resolve() for n in DATA_OK}
    suspeitos = [p for p in (dest / "app").rglob("*") if p.is_file() and p.suffix.lower() in DOCS and p.resolve() not in ok_docs
                 and "fonts" not in p.parts]
    suspeitos += [p for p in (dest / "donnees").rglob("*") if p.is_file()]
    for nome in ("cases_tc187_v2.py",):
        suspeitos += list((dest / "app").rglob(nome))
    if suspeitos:
        raise SystemExit("PAQUET REFUSÉ — documents ou données de dossier trouvés :\n" + "\n".join(map(str, suspeitos[:30])))


LANCEUR = r'''@echo off
rem GF Capital - Agent KYC/KYB/AML (version portable). Double-cliquer pour lancer ; fermer cette fenetre pour arreter.
setlocal
cd /d "%~dp0"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0runtime\navigateurs"
set "DATA_DIR=%~dp0donnees"
set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"
set "KYC_AGENT_MAX_USD=30"
set "KYC_AGENT_SIMULATE="
set "ANTHROPIC_API_KEY="
if not exist "%DATA_DIR%\cases" mkdir "%DATA_DIR%\cases"
echo Demarrage de l'application GF KYC... (ne pas fermer cette fenetre pendant l'utilisation)
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 5; Start-Process 'http://127.0.0.1:8765/'"
"%~dp0runtime\python\python.exe" -m uvicorn webapp.app:app --host 127.0.0.1 --port 8765 --app-dir "%~dp0app"
echo.
echo L'application s'est arretee. Si le port 8765 est deja utilise, elle tourne peut-etre deja : http://127.0.0.1:8765/
pause
'''

LISEZMOI = """GF Capital US — Agent KYC/KYB/AML (version portable)
=====================================================

Lancer : double-cliquer sur « Lancer GF KYC.cmd ». Le navigateur s'ouvre sur http://127.0.0.1:8765/
Arrêter : fermer la fenêtre noire « Lancer GF KYC ».

Aucune installation n'est nécessaire (Python, bibliothèques et navigateur sont dans le dossier runtime).
Le dossier peut être copié n'importe où (de préférence hors d'un dossier synchronisé OneDrive).

Clé API Anthropic
-----------------
Au premier lancement, si aucune clé API n'est enregistrée sur l'ordinateur, une fenêtre la demande.
La clé est vérifiée par un échange très court avec le modèle, puis enregistrée uniquement sur cet ordinateur
(variable d'environnement utilisateur Windows KYC_ANTHROPIC_API_KEY). Elle n'est jamais affichée.
« Plus tard » : l'application fonctionne en mode SIMULATION (bandeau orange, aucun appel à l'API) ; la fenêtre
revient au prochain lancement.
Plafond de coût par mise à jour : 30 $ (KYC_AGENT_MAX_USD dans « Lancer GF KYC.cmd »).

Données
-------
Le paquet ne contient AUCUN dossier ni document client. Les dossiers créés sont enregistrés dans donnees\\cases,
les sauvegardes automatiques dans donnees\\sauvegardes.
La validation finale des livrables reste humaine.
"""


def main():
    if DEST.exists():
        if any(DEST.iterdir()):
            raise SystemExit(f"La destination existe déjà et n'est pas vide : {DEST} (la supprimer ou en choisir une autre)")
    DEST.mkdir(parents=True, exist_ok=True)
    app = DEST / "app"
    for d in CODE:
        shutil.copytree(ROOT / d, app / d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.log", *EXCLUS_CODE))
    (app / "data").mkdir(parents=True)
    for n in DATA_OK:
        shutil.copy2(ROOT / "data" / n, app / "data" / n)
    for n in ("requirements.txt", "requirements-web.txt"):
        shutil.copy2(ROOT / n, app / n)
    print("code copié")
    copiar_python(DEST / "runtime" / "python")
    print("python copié")
    copiar_navegador(DEST / "runtime" / "navigateurs")
    print("navigateur copié")
    (DEST / "donnees" / "cases").mkdir(parents=True)
    (DEST / "Lancer GF KYC.cmd").write_text(LANCEUR.replace("\n", "\r\n"), encoding="ascii")
    (DEST / "LISEZMOI.txt").write_text(LISEZMOI.replace("\n", "\r\n"), encoding="utf-8-sig")
    verificar(DEST)
    tam = sum(p.stat().st_size for p in DEST.rglob("*") if p.is_file())
    print(json.dumps({"destination": str(DEST), "taille_mo": round(tam / 1048576)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
