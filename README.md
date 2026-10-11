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

Dans tous les cas, Claude Code tourne avec `--setting-sources project` (tes
hooks globaux sont ignorés), des `--settings` propres au test et
`--strict-mcp-config`.

## Bac à sable podman (par défaut)

```
 ┌──────────── réseau leak-bench-internal (--internal, sans sortie) ────────────┐
 │                                                                              │
 │  conteneur Claude Code              conteneur proxy                          │
 │  - jeton factice                    - enregistre chaque requête              │──► api.anthropic.com
 │  - rootfs en lecture seule          - remplace le jeton factice par le vrai  │    (réseau leak-bench-egress)
 │  - cap-drop all, no-new-privileges  - seul accès vers l'extérieur            │
 └──────────────────────────────────────────────────────────────────────────────┘
```

- **Image Claude Code** (`containers/claude.Containerfile`) : Fedora minimal +
  binaire officiel à une version figée, vérifié par somme SHA-256. Par défaut,
  la même version que le `claude` de l'hôte (`--claude-version` pour une autre).
  L'image est construite automatiquement au premier lancement.
- **Aucun vrai identifiant dans le conteneur Claude Code** : le cas `env` ne
  peut donc pas l'exposer. Le proxy reçoit le vrai, monté en lecture seule
  depuis un fichier temporaire en mémoire (`$XDG_RUNTIME_DIR`).
- **Aucune sortie réseau** sinon le proxy : tout trafic qui ne passe pas par
  lui échoue au lieu de passer inaperçu.
- `~/.claude` du conteneur est un dossier jetable : ton `~/.claude` n'est ni lu
  ni modifié.
- Rootless, `--userns=keep-id` : les fichiers du projet monté appartiennent à
  ton utilisateur des deux côtés.

Identifiant utilisé par le proxy, dans cet ordre : `BENCH_AUTH`
(`oauth:<jeton>` ou `apikey:<clé>`), `ANTHROPIC_API_KEY`,
`CLAUDE_CODE_OAUTH_TOKEN`, puis le jeton d'accès claude.ai de
`~/.claude/.credentials.json`. Ce dernier est seulement lu, jamais rafraîchi :
s'il expire dans moins de 30 min, le banc s'arrête et demande de lancer
`claude -p hi` sur l'hôte.

`--sandbox host` lance plutôt le `claude` de l'hôte, derrière un proxy dans le
processus de test et avec un environnement réduit à une liste fixe de
variables. Plus rapide à mettre en place, mais sans isolation réseau ni
fichiers.

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
`files()` (binaires copiés dans le bac à sable), `settings(where)` (les hooks,
avec le chemin de chaque fichier dans le bac à sable) et, si possible,
`declare_known()`, puis l'inscrire dans `SUBJECTS`. Un binaire de hook doit
pouvoir tourner sur Fedora minimal (glibc ; un binaire statique est idéal).

## Lancer

```bash
uv run pytest                                   # tout
uv run pytest --subject bare --case monitor     # un cas
REDACTED_BIN=~/git/redacted/redacted uv run pytest --subject redacted
uv run pytest --model claude-sonnet-5           # autre modèle (défaut : Haiku 4.5)
uv run pytest --claude-version 2.1.292          # autre version de Claude Code (podman)
uv run pytest --sandbox host                    # sans conteneur
```

Prérequis : `uv`, `podman` rootless, `pgrep` ; `claude` sur l'hôte pour
détecter la version par défaut (ou `--claude-version`).

Options : `--attempts N` (nouveaux essais si non concluant, défaut 2),
`--keep-transcripts` (garde le dossier `~/.claude` du conteneur dans le
rapport ; en mode hôte, laisse les sessions dans `~/.claude/projects`).

En fin d'exécution, une matrice cas × sujet s'affiche. Elle est écrite avec
tous les éléments de preuve dans `reports/<horodatage>/<bac à sable>/` : `stream.jsonl`,
`wire.txt` (corps des requêtes), `transcript.jsonl`, `settings.json`.

## Attention

- **Coût** : une exécution `claude -p` par cas et par sujet. Les cas `tail`
  durent environ 30 à 60 s.
- **`reports/` contient le prompt système complet** envoyé à l'API. En mode
  hôte, il inclut ton contexte utilisateur (e-mail…). Le dossier est ignoré par
  git ; ne pas le publier tel quel.
- En mode podman, seul le trafic vers `ANTHROPIC_BASE_URL` peut sortir ; le
  reste (télémétrie…) est bloqué. Le banc mesure donc ce que Claude Code envoie
  à l'API, pas ce qu'il tenterait d'envoyer ailleurs.
- En mode hôte, le reste du trafic n'est ni bloqué ni enregistré, et un filtre
  peut lire sa config globale (`~/.config/redacted/…`).
