# claude-leak-bench

End-to-end test bench: does a canary secret leave the machine while Claude
Code is working? It is measured on bare Claude Code first, then with a filter
(hook, plugin…) to check that the filter actually stops the leak.

## How it works

Each test:

1. creates a throwaway project and plants a canary secret in it, unique to the run;
2. runs `claude -p` behind a **local proxy that records everything sent to the
   API** (`ANTHROPIC_BASE_URL`);
3. checks in the `stream-json` output that the model really performed the
   requested action. If not, the result is **inconclusive** (skip), never 🟢;
4. looks for the canary in three places:

| Where | What it proves |
|---|---|
| traffic to the API | the secret was sent to Anthropic: this is the reference |
| transcript `~/.claude/projects/…/<session>.jsonl` | the secret was written to disk |
| the model's final answer | informative only (`results.json`) |

Each cell of the matrix shows the worst place reached:

- 🔴 the secret was sent to the Anthropic API;
- 🟠 it was not sent to the API, but was written to the transcript on disk;
- 🟢 neither.

In every mode, Claude Code runs with `--setting-sources project` (your global
hooks are ignored), test-specific `--settings` and `--strict-mcp-config`.

## Results

Run on 2026-10-11, podman sandbox, Claude Code 2.1.283, model Haiku 4.5,
`redacted` at commit `0b4dd23` (`main`, Go version), `redacted-pr47` at
commit `b88d507` ([svn-arv/redacted#47](https://github.com/svn-arv/redacted/pull/47),
Rust rewrite 1.0.0), as is and with the entropy heuristic on
(`redacted-pr47-heuristic`). `rdx-proxy`:
[paroque28/claude-code-redact](https://github.com/paroque28/claude-code-redact)
at `d9c57fa` plus two local fixes (see below), 3 runs, one dot per run.

| Case | bare | redacted | redacted-pr47 | redacted-pr47-heuristic | rdx-proxy |
|---|---|---|---|---|---|
| `cat_file` | 🔴 | 🔴 | 🟢 | 🟢 | 🟠🔴🟠 |
| `read_file` | 🔴 | 🔴 | 🟢 | 🟢 | 🔴🟠🔴 |
| `env` | 🔴 | 🔴 | 🟢 | 🟢 | 🟠🔴🟠 |
| `unlabeled_random` | 🔴 | 🔴 | 🔴 | 🟢 | 🔴🔴🔴 |
| `known_value` | 🔴 | 🔴 | 🟢 | 🟢 | 🟠🟠🟠 |
| `tail_foreground` | 🔴 | 🔴 | 🟢 | 🟢 | 🟠🔴🟠 |
| `tail_background` | 🔴 | 🔴 | 🟢 | 🟢 | 🟠🟠🟠 |
| `monitor` | 🔴 | 🔴 | 🔴 | 🔴 | 🟠🔴🔴 |

🔴 sent to the Anthropic API · 🟠 not sent to the API, but written to the
transcript on disk · 🟢 neither

- **bare**: the canary leaks in every case, as expected. This confirms the
  bench detects leaks.
- **redacted**: the canary still reaches the API and the transcript in every
  case. The hook detects the secret in every case except `monitor`, but it
  answers with `decision: "block"`, which does not replace the tool output:
  the raw output goes to the model and the redacted copy is only added beside
  it (see [svn-arv/redacted#46](https://github.com/svn-arv/redacted/issues/46)).
  The model, seeing the warning, merely stops repeating the secret in its
  answer.
- **redacted-pr47**: `updatedToolOutput` now mirrors the shape of the tool
  response, so Claude Code applies the replacement: the model and the
  transcript only see the marker (`DB_PASSWORD= [REDACTED ...grtZ]`, last 4
  characters kept). `unlabeled_random` leaks because nothing flags a random
  value under a neutral key: in 1.0.0 that takes a secret learned by
  `redacted init` (global config, absent from the sandbox) or the entropy
  heuristic (off by default).
- **redacted-pr47-heuristic**: with the heuristic on, `unlabeled_random` is
  caught too (`cache_ref: [REDACTED ...B95I]`, pattern `secret_value`).
- **rdx-proxy**: as an API proxy, it sees everything sent to the API,
  `Monitor` notifications included, but the transcript always keeps the
  secret, so 🟠 is its best result. Apart from the declared `known_value`,
  only its entropy detector catches the canaries: Shannon entropy of at least
  4.5 bits per character, on a value after `=` or in quotes. A random
  32-character value stays under that threshold about one time in three
  (68/200 in a direct test of its redactor), and every 🔴 above is a canary
  under 4.5 bits, every 🟠 one at 4.5 or more. `unlabeled_random`
  (`cache_ref: <value>`) matches no detector. Its context detector only knows
  quoted values and `export` lines.
  At `d9c57fa`, the proxy answers 500 to every request of a project with
  `.redaction_rules` (a `NameError` on `_cache` since `b6c4606`), so Claude
  Code cannot work at all, and its streaming responses are cut off because the
  HTTP client is closed before the stream is read. The bench ran a local
  branch with both fixed (`_cache` → `cache`, a client owned by the stream).
- **`monitor`**: `Monitor` events reach the model as notifications that never
  go through PostToolUse hooks, so a PostToolUse filter cannot see them at all.
  Only an API proxy such as `rdx-proxy` can catch them.
- **`tail_foreground`**: when the Bash timeout expires, Claude Code moves the
  command to the background and the model reads its output file with `Read`,
  so this case ends up like `tail_background`.

These results depend on the model's behavior and on the Claude Code version:
re-run the bench rather than relying on this table.

## Podman sandbox (default)

```
 ┌──────────── network leak-bench-internal (--internal, no egress) ─────────────┐
 │                                                                              │
 │  Claude Code container              proxy container                          │
 │  - dummy token                      - records every request                  │──► api.anthropic.com
 │  - read-only rootfs                 - swaps the dummy token for the real one │    (network leak-bench-egress)
 │  - cap-drop all, no-new-privileges  - the only way out                       │
 └──────────────────────────────────────────────────────────────────────────────┘
```

- **Claude Code image** (`containers/claude.Containerfile`): Fedora minimal +
  the official binary at a pinned version, verified by SHA-256 checksum. By
  default, the same version as the host's `claude` (`--claude-version` for
  another one). The image is built automatically on first run.
- **No real credential in the Claude Code container**, so the `env` case cannot
  expose it. The proxy gets the real one, mounted read-only from a temporary
  file in memory-backed storage (`$XDG_RUNTIME_DIR`).
- **Interceptor**: a subject that is a redacting API proxy (`rdx-proxy`) runs
  in a third container on the internal network, between Claude Code and the
  recording proxy, with the same hardening. Its image is built from the
  subject's checkout and tagged with its commit. The project is mounted
  read-only at the same path (`/work`) as in the Claude Code container.
- **No network egress** except through the proxy: any traffic that bypasses
  it fails instead of going unnoticed.
- The container's `~/.claude` is a throwaway directory: your `~/.claude` is
  neither read nor modified. The only exception is the feature flag cache
  (`cachedGrowthBookFeatures` from your `~/.claude.json`), copied into it so
  that tools enabled by remote flags, such as `Monitor`, are available in the
  sandbox too. It holds no account data and no credentials.
- Rootless, `--userns=keep-id`: files in the mounted project belong to your
  user on both sides.

Credential used by the proxy, in this order: `BENCH_AUTH`
(`oauth:<token>` or `apikey:<key>`), `ANTHROPIC_API_KEY`,
`CLAUDE_CODE_OAUTH_TOKEN`, then the claude.ai access token from
`~/.claude/.credentials.json`. The latter is only read, never refreshed: if it
expires in less than 30 min, the bench stops and asks you to run
`claude -p hi` on the host.

`--sandbox host` runs the host's `claude` instead, behind an in-process proxy
and with an environment reduced to a fixed list of variables. Quicker to set
up, but with no network or file isolation.

## Cases (`bench/cases.py`)

| Case | Scenario |
|---|---|
| `cat_file` | `cat config.env` containing `DB_PASSWORD=<canary>` |
| `read_file` | same with the `Read` tool |
| `env` | `env`, canary in `BENCH_DB_PASSWORD` |
| `unlabeled_random` | random value under a neutral key (`cache_ref:`) |
| `known_value` | low-entropy passphrase in free text, declared to the filter beforehand |
| `tail_foreground` | `tail -f` in the foreground; canary lines arrive after it starts |
| `tail_background` | `tail -f` in the background, then its output is read |
| `monitor` | `Monitor` tool on `tail -f` |

For the `tail` cases, a thread waits for a `tail -f <log>` process to be
running (`pgrep`) before writing the lines, so the leak really is a streaming
one.

## Subjects (`bench/subjects.py`)

- `bare`: Claude Code with nothing added. This is the **negative control**: the
  test passes when it leaks, which proves the bench detects something.
- `redacted`: [svn-arv/redacted](https://github.com/svn-arv/redacted) as a
  PostToolUse hook. Binary from `REDACTED_BIN` or the `PATH`. The test passes
  when nothing leaks (neither `w` nor `t`).
- `redacted-pr47`: the same, built from
  [svn-arv/redacted#47](https://github.com/svn-arv/redacted/pull/47) (Rust
  rewrite). Binary from `REDACTED_PR47_BIN` only. The sandbox has no global
  `~/.config/redacted`, so it runs without learned secrets or heuristic:
  vendor signatures and project `.redacted.engine.yml` patterns only.
- `redacted-pr47-heuristic`: the same binary with `heuristic.enabled: true`.
  The heuristic is read from the global config only, so the hook runs with a
  `HOME` of its own holding `.config/redacted/config.yaml`.

- `rdx-proxy`: [paroque28/claude-code-redact](https://github.com/paroque28/claude-code-redact)
  in proxy mode, its recommended mode. Checkout from `RDX_SRC`. It redacts
  requests on their way to the API and puts the real values back in the
  responses, so the transcript on disk always holds the secret: its best
  possible result is 🟠. rdx redacts nothing for a project without
  `.redaction_rules`, so the subject writes an empty one (built-in detectors
  only), then performs the file steps of `rdx setup --proxy` with rdx's own
  code (deny permissions in `.claude/settings.json`, `RDX.md`, `CLAUDE.md`).

To test another project, add a `Subject` subclass that provides `files()`
(binaries copied into the sandbox), `settings(where)` (the hooks, given each
file's path inside the sandbox) and, if possible, `declare_known()`, then
register it in `SUBJECTS`. A hook binary must be able to run on Fedora minimal
(glibc; a static binary is ideal). A subject that is not a hook provides a
`Sidecar` (its checkout and a Containerfile) and uses it in `prepare()` (setup
steps run in the project) and/or `interceptor()` (a proxy inserted before the
recording proxy).

## Running

```bash
uv run pytest                                   # everything
uv run pytest --subject bare --case monitor     # a single case
REDACTED_BIN=~/git/redacted/redacted uv run pytest --subject redacted
REDACTED_PR47_BIN=~/git/redacted-pr47/target/release/redacted uv run pytest --subject redacted-pr47 --subject redacted-pr47-heuristic
RDX_SRC=~/git/claude-code-redact uv run pytest --subject rdx-proxy
uv run pytest --model claude-sonnet-5           # another model (default: Haiku 4.5)
uv run pytest --claude-version 2.1.292          # another Claude Code version (podman)
uv run pytest --sandbox host                    # no container
```

Requirements: `uv`, rootless `podman`, `pgrep`; `claude` on the host to detect
the default version (or `--claude-version`).

Options: `--attempts N` (retries when inconclusive, default 2),
`--keep-transcripts` (keeps the container's `~/.claude` directory in the
report; in host mode, leaves the sessions in `~/.claude/projects`).

At the end of a run, a case × subject matrix is printed. It is written, along
with all the evidence, to `reports/<timestamp>/<sandbox>/`: `stream.jsonl`,
`wire.txt` (request bodies), `transcript.jsonl`, `settings.json` and, for an
interceptor, `interceptor.log`.

## Caveats

- **Cost**: one `claude -p` run per case and per subject. The `tail` cases take
  about 30 to 60 s each.
- **`reports/` contains the full system prompt** sent to the API. In host mode,
  it includes your user context (email…). The directory is git-ignored; do not
  publish it as is.
- In podman mode, only traffic to `ANTHROPIC_BASE_URL` can get out; the rest
  (telemetry…) is blocked. The bench therefore measures what Claude Code sends
  to the API, not what it would try to send elsewhere.
- In host mode, the rest of the traffic is neither blocked nor recorded, and a
  filter may read its global config (`~/.config/redacted/…`). The `rdx-proxy`
  interceptor runs from its checkout with `uv run`, which creates a `.venv`
  there.
