# claude-leak-bench

Banc de test de bout en bout : un secret témoin (*canary*) sort-il de la machine
quand Claude Code travaille ? On le mesure sur Claude Code nu, puis avec un
filtre (hook, plugin…) pour vérifier qu'il bloque vraiment la fuite.

## Principe

Chaque test :

1. crée un projet jetable et y place un secret témoin, unique à l'exécution ;
2. lance `claude -p` derrière un **proxy local qui enregistre tout ce qui part
   vers l'API** (`ANTHROPIC_BASE_URL`) ;
3. vérifie dans le `stream-json` que le modèle a bien fait l'action demandée.
   Sinon le résultat est **non concluant** (skip), jamais « propre » ;
4. cherche le témoin à trois endroits :

| Lettre | Où | Ce que ça prouve |
|---|---|---|
| `w` | trafic vers l'API | le secret est parti chez Anthropic : c'est la référence |
| `t` | transcript `~/.claude/projects/…/<session>.jsonl` | le secret est écrit sur disque |
| `a` | réponse finale du modèle | indicatif seulement |

Claude Code tourne isolé : `--setting-sources project` (tes hooks globaux sont
ignorés), `--settings` propre au test, `--strict-mcp-config`, et un
environnement réduit à une liste fixe de variables (`bench/runner.py`).

## Cas (`bench/cases.py`)

| Cas | Scénario |
|---|---|
| `cat_file` | `cat config.env` contenant `DB_PASSWORD=<témoin>` |
| `read_file` | idem avec l'outil `Read` |
| `env` | `env`, témoin dans `BENCH_DB_PASSWORD` |
| `unlabeled_random` | valeur aléatoire sous une clé neutre (`cache_ref:`) |
| `known_value` | phrase de passe peu aléatoire dans du texte libre, déclarée à l'avance au filtre |
| `tail_foreground` | `tail -f` au premier plan ; les lignes avec le témoin arrivent après le démarrage |
| `tail_background` | `tail -f` en arrière-plan, puis lecture de sa sortie |
| `monitor` | outil `Monitor` sur `tail -f` |

Pour les cas `tail`, un thread attend qu'un processus `tail -f <log>` tourne
(`pgrep`) avant d'écrire les lignes : la fuite est vraiment en flux.

## Sujets (`bench/subjects.py`)

- `bare` : Claude Code sans rien. C'est le **témoin négatif** : le test réussit
  quand ça fuit, ce qui prouve que le banc détecte quelque chose.
- `redacted` : [svn-arv/redacted](https://github.com/svn-arv/redacted) en hook
  PostToolUse. Binaire via `REDACTED_BIN` ou le `PATH`. Le test réussit quand
  rien ne fuit (ni `w` ni `t`).

Pour tester un autre projet, ajouter une sous-classe de `Subject` qui fournit
`settings()` (les hooks) et, si possible, `declare_known()`, puis l'inscrire
dans `SUBJECTS`.

## Lancer

```bash
uv run pytest                                   # tout
uv run pytest --subject bare --case monitor     # un cas
REDACTED_BIN=~/git/redacted/redacted uv run pytest --subject redacted
uv run pytest --model claude-sonnet-5           # autre modèle (défaut : Haiku 4.5)
```

Options : `--attempts N` (nouveaux essais si non concluant, défaut 2),
`--keep-transcripts` (sinon les sessions de test sont supprimées de
`~/.claude/projects` après copie dans le rapport).

En fin d'exécution, une matrice cas × sujet s'affiche. Elle est écrite avec
tous les éléments de preuve dans `reports/<horodatage>/` : `stream.jsonl`,
`wire.txt` (corps des requêtes), `transcript.jsonl`, `settings.json`.

## Attention

- **Coût** : une exécution `claude -p` par cas et par sujet. Les cas `tail`
  durent environ 30 à 60 s.
- **`reports/` contient le prompt système complet** envoyé à l'API, avec ton
  contexte utilisateur (e-mail…). Le dossier est ignoré par git ; ne pas le
  publier tel quel.
- Seul le trafic vers `ANTHROPIC_BASE_URL` est enregistré. La télémétrie ou
  d'autres domaines contactés par Claude Code ne sont pas couverts.
- Un filtre peut lire une config globale (`~/.config/redacted/…`) : le banc ne
  l'isole pas.
