# Schéma de `cases/<ID>/state.json` (schema_version "5")

`state.json` est la seule source des livrables : `python -m kyc_agent.build cases/<ID>` en tire le rapport PDF, la matrice XLSX, les messages, le ZIP de preuves, la Nota_FR et `state_vN.json`. Aucun texte propre à un dossier n'est écrit dans le code. Exemple fictif complet : `kyc_agent/prompts/exemple_state.json` (forme, niveau de détail, façon de qualifier les constats ; n'en reprends aucun contenu).

Textes opérationnels en portugais (Brésil), sauf `nota_fr` (français). Dates ISO `AAAA-MM-JJ` sauf mention contraire.

## Racine
| Champ | Type | Contenu |
|---|---|---|
| schema_version | "5" | |
| versao | int | numéro de la version produite (1 pour la première analyse) |
| data_analise, data_referencia | str | date du cycle |
| elaborado_por | str | "Agente KYC/KYB/AML (IA) — rascunho para revisão do Supervisor de Compliance" |
| guia_referencia | str | référence du guide utilisé (titre, version, date, fichier) |
| validacao_humana | obj | {"status": "pendente", "supervisor": null, "data": null, "comentario": null} — jamais rempli par l'agent |
| execucao_revisao | obj | {executado_por, revisado_por: null, data_revisao: null, autorizado_por: null, data_autorizacao: null, nota, autorizacao_pendente?} |

## caso
`id, nome, categoria, etapa_pipeline, tipo_entidade, valor_pipeline, valor_declarado, finalidade, prazo_declarado, bdr {nome, email, time}, outros_internos [str], contato_cliente {nome, papel_pipeline, email, telefone}, fechamento_previsto, prioridade, estado_tarefa`
+ facultatifs affichés en 1.1/1.3 : `contraparte` (qui est la contrepartie, en une ligne), `quem_paga_quem` (payeur → bénéficiaire, compte, montant, nature de l'information), `periodo_documentos` (ex. "recebidos de 20/07/2026 a 10/11/2026").
Une information absente s'écrit « não informado », jamais inventée.

## recomendacao
`status` (libellé de la matrice de décision du guide, ex. "HOLD KYC / EDD / ESCALAÇÃO"), `codigo` (KYC_CLEAR | KYC_CLEAR_DD | HOLD_TECNICO | HOLD_KYC_EDD | STOP), `resumo`, `justificativa` [str, chacune citant le § du guide], `caminho_para_validacao`, `escalonamento`.

## Listes
- **partes** : `id` (court, ex. JBM), `nome`, `tipo` (PF|PJ), `cpf` ou `cnpj`, `nascimento?`, `documento?`, `papeis` [str], `vinculo_fonte` (document qui établit le lien), `kyc` (traitement KYC appliqué).
- **documentos** : `id` (D01…), `arquivo` (chemin relatif à la racine du projet), `tipo`, `origem` (cliente|BDR|terceiro|público), `recebido_em`, `uso`, `achado`.
- **controles** : `id` (Cnn / C-ESPn), `nivel`, `controle`, `partes`, `estado` (estado da tarefa), `execucao` (documentado | executado pelo agente | não consultado – motivo), `resultado`, `conclusao`, `evidencias` [ids E..].
- **fluxos** : `id, pagador, recebedor, valor, motivo, base_contratual, natureza` (fato documentado | declaração | hipótese).
- **pontos_esclarecidos** : `id, ponto, conclusao, natureza, evidencias`.
- **riscos** : `id` (Rn), `titulo, importancia` (alta|média|baixa), `bloqueia_kyc` (bool), `evento, parte_fluxo, fato_fonte, exposicao, medida_executada, medida_proposta, risco_residual, ligacao`.
- **pendencias** (matrice externe — uniquement ce que des tiers doivent fournir ou décider) : `id` (Pnn), `item, parte, elemento_faltante, risco, esperado, criterio_fechamento, responsavel` ("Cliente (via BDR)" | "BDR" | "Jurídico" | "CEO"), `condiciona` ("KYC" | "DD"), `etapa_limite, prioridade, evidencias, status, estado_tarefa`.
- **mensagens** : `id` (Mn), `destinatario` (Cliente | BDR | Juridico | CEO), `para, assunto, corpo, anexos` [str], `pendencias` [ids P]. Une par destinataire utile ; aucune s'il n'y a rien à demander.
- **registro_tecnico** (accès bloqués, actions internes du Compliance — rubrique distincte de la matrice externe) : `id` (Tn), `controle, incidente, intervencao, impacto, prioridade` (P1|P2|P3), `evidencias`, `link` (URL de la consultation à faire, "" sinon), `retorno_esperado` (ce que le Compliance doit rapporter), `acao_humana` (bool : true si un humain doit agir — c'est ce qui l'affiche dans « Documents attendus par l'agent » ; false pour une simple information, ex. reprise automatique au prochain cycle), + `respostas` (boutons de réponse de l'application, obligatoires si `acao_humana` — schéma dans `kyc_agent/respostas.py`).
- **condicoes** : {ja_cumpridas [str], necessarias_ao_kyc [str], antes_assinatura_closing [str]}.
- **historico** : [{versao, data, evento, mudancas [str], journal? [{ponto, anterior, revisado, prova, razao, consequencia}]}] — une entrée par version.
- **evidencias** (index du ZIP, guide §10.3) : `id` (Enn), `categoria` (01_Documentos_cliente | 02_Identidade_UBO | 03_Screenings | 04_Midia_e_processos | 05_Registros_publicos | 06_Matriz_e_correspondencia | 07_Parecer_e_validacao), `origem, fonte, arquivo, descricao, sustenta`, `copiar?` (bool : copier la source sous le nom `arquivo`).
  `fonte` = chemin(s) relatif(s) à la racine du projet séparés par `|`, ou une clé générée par le build :
  `gen:identidade`, `gen:screening`, `gen:midia` (si work/media_log.json), `gen:ofac_sls` (si work/web/ofac_sls/), `gen:partes`, `gen:fluxos`, `gen:matriz`, `gen:relatorio`, et la clé `gen` de chaque `consultas_processuais`. N'utilise une clé `gen:` que si sa source existe : sinon le build s'arrête.
- **nota_fr** : str Markdown en français — note au superviseur (recommandation, points bloquants, ce qui a été fait / pas fait, actions humaines).

## Champs facultatifs des preuves générées
- `screening_fontes_texto` : phrase « Fontes : … » de l'annexe C ; peut utiliser `{OFAC_registros}`, `{ONU_data_geracao_lista}`, `{PEP_competencia}`, `{CEIS_data_arquivo}`… (valeurs de work/screening.json). Absent → phrase composée automatiquement.
- `reconciliacao_identidade` : {"colunas": ["Dado", "<source 1>", …], "pessoas": [{"nome", "linhas": [[dado, valeur source 1, …]]}], "observacoes": [str]} — rapprochement des données d'identité entre documents. Absent → tableau minimal tiré de `partes`.
- `midia_qualificacao` : conclusion qualifiée de la recherche média (guide §7.6.2).
- `consultas_processuais` : [{arquivo (réponse JSON DataJud, relatif au dossier), gen, rotulo, fonte?, motivo, alternativa?, termo?, consultado_em, leitura?, limite?}].
- `mapa_propriedade` : texte de la chaîne de propriété / contrôle.
- `fluxos_nota` : phrase d'introduction de la matrice des flux.
- `arquivos_gerados` : {"identidade": suffixe de fichier, "midia": suffixe de fichier}.

## Fichiers de travail lus par le build (`cases/<ID>/work/`)
- `screening.json` (obligatoire) : sortie de `python -m kyc_agent.screening`.
- `media_log.json` (facultatif) : {"executado_em", "ferramenta", "limitacao", "buscas": [{"data", "termo", "resultado", "conclusao", "url?"}]}.
- `web/ofac_sls/` (facultatif) : sortie de `python -m kyc_agent.ofac_sls`.
