# Méthode de travail et points de vigilance

Le guide complet est reproduit plus bas : c'est la règle. Cette page rappelle comment l'appliquer sans rien oublier, sans t'autoriser à en lire seulement une partie. Lis le guide en entier avant de conclure quoi que ce soit.

## Principe de prudence (choix du superviseur : prime sur toute recherche d'économie)
Le superviseur préfère **une demande de trop à un point raté**. Une tâche ou une pendance inutile se ferme en un clic ;
un risque manqué ne se rattrape pas. Dans le doute, tu **demandes**, tu ne conclus pas.
- **Jamais de clôture par hypothèse.** Un contrôle n'est « concluído » que sur preuve ; il n'est « não aplicável » que si
  le guide l'écarte explicitement pour ce dossier **et** que les pièces le démontrent. Sinon : « aguardando » + tâche ou pendance.
- **Pas d'accès ≠ n'existe pas.** Un contrôle qui dépend d'une ressource interne (liste interne C16, base, accès à un
  portail, information détenue par GF Capital) n'est jamais classé « não aplicável » de ta propre initiative : il reste
  « aguardando acesso » avec une tâche humaine qui **pose la question** (la ressource existe-t-elle ? si oui, la consulter
  ou te donner l'accès ; sinon, le superviseur confirme la non-applicabilité).
- **Consultation officielle possible mais non faite = tâche humaine**, même si la cohérence entre pièces semble suffire
  (ex. situação cadastral CPF/CNPJ à la Receita, certidões, registres) : la cohérence documentaire est notée, la
  consultation officielle reste demandée.
- **Toute partie qui paie ou reçoit des fonds, ou intervient dans l'acte, et dont l'identité ou le KYC manque** fait
  l'objet d'une **pendance explicite de KYC** (identification, documents, UBO, responsable, échéance), **en plus** de
  toute demande sur les termes commerciaux. Cela vaut aussi pour l'acheteur / payeur, même côté GF Capital, s'il n'est
  pas identifié dans le dossier.
- **Signal faible = constat.** Une incohérence mineure, une pièce étrangère, une mention manuscrite, un écart de valeur,
  une partie au rôle flou : consigne-la, évalue-la et, si elle ne peut pas être levée avec les pièces, crée la demande.
- **Demande écartée par le superviseur** (réponse « Não é necessário (ignorar) », motif obligatoire) : c'est sa décision,
  pas un constat. Clos la tâche ou les pendances visées avec le statut « dispensado pelo supervisor » (motif, auteur,
  date, dans `estado_tarefa` / `status` et l'historique), retire-les des messages et ne les recrée pas. Seule exception :
  un fait nouveau qui change le risque — tu peux alors la rouvrir en expliquant précisément ce qui a changé. Un contrôle
  écarté n'est jamais présenté comme négatif ; s'il conditionnait la recommandation, dis-le dans la justification.
- En cas d'hésitation entre « pendance » et « rien », choisis la pendance (priorité basse si l'enjeu est faible) ;
  entre P2 et P1, choisis P1 si un risque bloquant est possible.

## Exhaustivité
- **Chaque pièce est lue** (texte extrait ou image pour les scans), y compris les sous-dossiers et les pièces qui semblent hors sujet ; une pièce qui n'appartient pas à l'opération (autre actif, autre client) est elle-même un constat.
- **Chaque contrôle du catalogue du guide (C01 à C42) reçoit un statut** dans `controles` : exécuté par l'agent, documenté par une pièce, non applicable (avec la raison tirée du guide) ou non consulté (avec la raison). Utilise la numérotation du guide, pas une numérotation propre. Ajoute des contrôles spécifiques (C-ESPn) quand le dossier l'exige (ex. vérification d'authenticité d'une certidão via son code).
- **Toutes les parties utiles** : contrepartie, vendeurs, cédants, conjoints (régime matrimonial), titulaires registraux de chaque matrícula, mandataires et avocats, intermédiaires, payeurs et bénéficiaires des flux, UBO. Chaque partie a sa source (`vinculo_fonte`) et son traitement KYC.
- **Chaque flux** (qui paie qui, combien, sur quelle base) et **chaque incohérence** entre pièces (noms, surfaces, numéros de matrícula, dates, montants, mentions manuscrites) sont relevés.

## Contrôles et sources
- **Screening** : fixe la méthode AVANT d'exécuter (listes, seuils, champs comparés) et ne la change pas après avoir vu les résultats. Résous chaque candidat un par un dans `work/resolucoes.json` (identifiants comparés, décision, motif). OFAC : en plus des fichiers officiels, la recherche officielle Sanctions List Search (`python -m kyc_agent.ofac_sls`) avec capture.
- **Sites protégés par captcha** : jamais contournés. Le contrôle est « não consultado – acesso » et devient une tâche humaine dans `registro_tecnico` (avec `acao_humana: true`, le lien, le résultat attendu et les boutons `respostas`).
- **Portail inaccessible** (ex. tribunal) : utilise l'alternative officielle avant de déclarer le contrôle non consulté. Processus judiciaires : `python -m kyc_agent.datajud <tribunal> <numero> cases/<ID>/work/web/datajud_<n>.json` (API pública CNJ). Si DataJud ne répond pas non plus, tâche humaine. N'ajoute une entrée `consultas_processuais` que si la réponse contient au moins un résultat.
- **Authenticité des pièces** : quand une pièce porte un code ou un lien de vérification (certidões, documents municipaux), tente la vérification officielle (`webfetch` / `webcap`) ; si elle est bloquée, tâche humaine.
- **Média adverse** : sur chaque partie, termes en portugais, consigné dans `work/media_log.json`, conclusion qualifiée.
- Un contrôle non exécuté n'est **jamais** présenté comme négatif.

## Restitution
- **Matrice externe (`pendencias`)** : uniquement ce que des tiers doivent fournir ou décider. Les blocages d'accès et actions internes du Compliance vont dans `registro_tecnico`, rubrique distincte.
- **Messages** (guide, choix du destinataire) : un message par destinataire qui a réellement quelque chose à faire —
  - Cliente (via BDR) : faits, identité, rôle, origine, pièces absentes ou à corriger ;
  - BDR : collecte, coordination, conditions commerciales, preuve d'un accord annoncé ; dès que des demandes passent par le client, le BDR reçoit aussi un message qui lui explique quoi obtenir ;
  - Jurídico : effet d'un acte ou d'une procédure, validation d'une solution précise ;
  - CEO : décision sur un risque résiduel ou une condition de poursuite — **obligatoire dès que la recommandation comporte une escalade** (faits, analyse, risque restant, décision explicite à prendre).
  Chaque pendance est reprise dans au moins un message ; chaque message liste ses pendances.
- **Recommandation** : statut de la matrice de décision du guide, justifiée point par point (§ du guide), cohérente avec les risques bloquants et les pendances.
- **ZIP de preuves** : catégories 01 à 07 du guide ; chaque constat important a sa preuve (E..) ; copies des pièces client utilisées.

## Relecture finale obligatoire (avant de terminer)
Écris `cases/<ID>/work/autocontrole.json` : une liste de vérifications {verificacao, ok (bool), correcao} couvrant au minimum :
1. toutes les pièces de l'inventaire sont dans `documentos` (ou explicitement écartées avec motif) ;
2. les contrôles C01–C42 ont tous un statut, avec la numérotation du guide ;
3. chaque contrôle « não consultado » a une tâche dans `registro_tecnico` ou une raison de non-applicabilité ;
4. chaque tâche humaine a `acao_humana: true`, un lien si pertinent, `retorno_esperado` et des `respostas` ;
5. chaque pendance est dans un message ; destinataires conformes au guide (BDR si demandes au client, CEO si escalade) ;
6. chaque risque bloquant est reflété dans la recommandation et les conditions ;
7. chaque constat important a une preuve (E..) ;
8. les livrables de la version existent et la Nota_FR est cohérente avec l'état ;
9. principe de prudence : aucun « não aplicável » fondé sur une absence d'accès ou une hypothèse ; aucune consultation
   officielle possible laissée sans tâche ; chaque partie payeuse, bénéficiaire ou intervenante non identifiée a sa
   pendance de KYC ; chaque signal faible non levé a sa demande.
Pour chaque point non satisfait : corrige l'état, relance le cycle et le build, puis réécris l'autocontrôle. Ne termine qu'avec tous les points satisfaits ou expliqués.
