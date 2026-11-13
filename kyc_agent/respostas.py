"""Réponses humaines aux vérifications bloquées (registre technique) — schéma générique + boîte de réception.

Schéma (déclaré par l'agent dans state["registro_tecnico"][i]["respostas"]) — une liste d'actions :
  {"id": "sem_base", "rotulo": "...", "ajuda": "...", "efeito": "...",
   "texto": "nao" | "opcional" | "obrigatorio", "arquivos": "nao" | "opcional" | "obrigatorio",
   "multiplo": bool, "aceita": ".pdf,.png", "confirmacao": "texto de confirmação (déclaration en un clic)"}
L'app web affiche ces actions sans rien connaître du dossier. Les réponses sont déposées dans
cases/<ID>/respostas/<horodatage>_<item>_<acao>/ (resposta.json + fichiers) et restent « recebida »
jusqu'à ce que l'agent les intègre dans une nouvelle version (champ integrada_em_versao).

CLI : python -m kyc_agent.respostas <case_dir>   → liste des réponses en attente d'intégration.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

DEFAULT_ACEITA = ".pdf,.png,.jpg,.jpeg,.xlsx,.csv,.txt"


def acoes_padrao(item: dict) -> list[dict]:
    """Actions par défaut si l'agent n'en a pas déclaré : texte libre + documents."""
    return [{"id": "resposta", "rotulo": "Responder / enviar documentos", "texto": "opcional", "arquivos": "opcional",
             "multiplo": True, "efeito": "Resposta tratada pelo agente na próxima versão."}]


# Le superviseur peut toujours écarter une demande de l'agent (l'agent est volontairement sur-prudent) :
# motif obligatoire ; pour un message, choix des pendances écartées. Ajoutée par l'application, jamais par l'agent.
IGNORAR = "ignorar"


def acao_ignorar(pendencias: list[str] | None = None) -> dict:
    a = {"id": IGNORAR, "rotulo": "Não é necessário (ignorar)", "dispensa": True, "texto": "obrigatorio",
         "rotulo_texto": "Motivo (obrigatório)", "arquivos": "nao",
         "ajuda": "O supervisor considera que este pedido não é necessário para este dossiê.",
         "efeito": "Na próxima atualização, o agente encerra o item como «dispensado pelo supervisor» (motivo, autor, data). "
                   "Não é um resultado negativo; o agente não o recria, salvo fato novo que ele deverá explicar."}
    if pendencias is not None:
        a["pendencias"] = list(pendencias)
        a["efeito"] = ("Na próxima atualização, o agente encerra as pendências marcadas como «dispensadas pelo supervisor» "
                       "(motivo, autor, data) e as retira das mensagens. Não é um resultado negativo; ele não as recria, "
                       "salvo fato novo que deverá explicar.")
    return a


def _com_ignorar(lista: list[dict], pendencias: list[str] | None = None) -> list[dict]:
    return [a for a in lista if a.get("id") != IGNORAR] + [acao_ignorar(pendencias)]


def acoes(item: dict) -> list[dict]:
    return _com_ignorar(item.get("respostas") or acoes_padrao(item))


def acoes_mensagem(m: dict) -> list[dict]:
    """Réponse à un message : texte collé et/ou pièces jointes (au moins l'un des deux), réponses multiples."""
    return _com_ignorar(_acoes_mensagem_base(m), m.get("pendencias") or [])


def _acoes_mensagem_base(m: dict) -> list[dict]:
    return m.get("respostas") or [{
        "id": "resposta", "rotulo": "Registrar a resposta recebida", "texto": "opcional", "arquivos": "opcional",
        "um_dos_dois": True, "multiplo": True,
        "rotulo_texto": "Resposta (colar o texto recebido)", "rotulo_arquivos": "Anexos recebidos",
        "ajuda": "Colar o texto da resposta e/ou juntar os documentos enviados pelo destinatário. "
                 "Uma resposta parcial pode ser registrada; as seguintes se somam a ela.",
        "efeito": "O agente confronta a resposta com as pendências cobertas por esta mensagem e atualiza o dossiê na próxima versão.",
    }]


def listar(case_dir: Path) -> list[dict]:
    out = []
    d = Path(case_dir) / "respostas"
    if d.is_dir():
        for f in sorted(d.glob("*/resposta.json")):
            try:
                r = json.loads(f.read_text(encoding="utf-8"))
                r["_dir"] = f.parent.name
                out.append(r)
            except (OSError, ValueError):
                continue
    return out


def pendentes(case_dir: Path) -> list[dict]:
    return [r for r in listar(case_dir) if not r.get("integrada_em_versao")]


if __name__ == "__main__":
    for r in pendentes(Path(sys.argv[1])):
        print(f"{r['recebida_em']}  {r['item']}/{r['acao']}  par {r['autor']}  "
              f"texto={len(r.get('texto') or '')} car.  arquivos={[a['nome'] for a in r.get('arquivos', [])]}  [{r['_dir']}]")
