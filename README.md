# Oscalis

An [MCP](https://modelcontextprotocol.io) server for NIST OSCAL catalogs, baselines and crosswalks. It lets AI agents
query security and AI governance catalogs, resolve baselines, and walk a confidence-scored crosswalk graph between
frameworks. The package, command and repository are named `oscalis-mcp`.

> Oscalis is an independent project. It is not affiliated with or endorsed by NIST, FedRAMP or GovRAMP. It returns
> reference data and mappings to help people and agents find controls. It is not legal or compliance advice, and
> mappings marked `intersecting` or `advisory` are not compliance evidence. Check the official publications.

| Kind | Supported |
|------|-----------|
| Catalogs | NIST SP 800-53 Rev 5.2 (`800-53r5.2`), NIST CSF 2.0 (`csf-v2`), NIST AI RMF 1.0 (`ai-rmf`) |
| Profiles | NIST SP 800-53B Low / Moderate / High / Privacy, FedRAMP Rev 5 Low / Moderate / High, GovRAMP Rev 5 (11 profiles, see GovRAMP) |
| Crosswalks | CSF 2.0 to SP 800-53 (NIST's published references), AI RMF to SP 800-53 (analyst-authored) |
| Transports | `stdio` (default) and `sse` |

ISO/IEC 27001 and 42001 and CIS Controls are out of scope (licensing).

## Install

Oscalis is built for Windows, Linux and macOS (see Platform status below) and needs Python 3.11 or later. The simplest
route is
[uv](https://docs.astral.sh/uv/), which also fetches a suitable Python if you have none. The data (catalogs, profiles
and crosswalks) is bundled in the package, so the installed command works from any directory.

**1. Install uv** (skip if you have it):

| System | Command |
|--------|---------|
| Windows (PowerShell) | `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` |
| Linux and macOS | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |

macOS users can also run `brew install uv`. Open a new terminal afterwards so `uv` is on your PATH.

**2. Install Oscalis.** The command is the same on all three systems:

```
uv tool install git+https://github.com/Cyversight/oscalis-mcp
```

This puts the `oscalis-mcp` command on your PATH. Update later with `uv tool upgrade oscalis-mcp`.

**3. Add it to your MCP client.** If you installed the command in step 2:

```json
{ "mcpServers": { "oscalis": { "command": "oscalis-mcp" } } }
```

Or skip step 2 and let the client run it through `uvx`, which downloads and caches it on first use:

```json
{
  "mcpServers": {
    "oscalis": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Cyversight/oscalis-mcp", "oscalis-mcp"]
    }
  }
}
```

Check the install with `oscalis-mcp --help`. The server speaks MCP over `stdio` by default, so it waits silently for a
client when started by hand.

**Platform status.**

| System | Tested |
|--------|--------|
| Windows | Python 3.11 and 3.14: full test suite, plus an install from the built wheel that is then driven over stdio from an empty directory (`src/scripts/smoke_installed.py`) |
| Linux (Ubuntu) | Python 3.11 and 3.14: the same checks, on an earlier revision. CI covers the current code |
| macOS | Not yet tested |

The GitHub Actions workflow in `.github/workflows/ci.yml` runs these checks on Ubuntu, macOS and Windows, but it has not
run yet. Python builds without SQLite FTS5 (some minimal or custom builds) are rejected at startup with a message that
says so.

## Uninstall

**1. Remove the command.** The command is the same on all three systems:

```
uv tool uninstall oscalis-mcp
```

This deletes the `oscalis-mcp` command and its environment. `uv tool list` no longer shows it afterwards. If your client
ran Oscalis through `uvx` instead, there is nothing to uninstall; `uv cache clean oscalis-mcp` removes the cached copy.

**2. Remove the client entry.** Delete the `oscalis` entry from your MCP client's configuration, or the client will keep
trying to start a command that no longer exists.

**3. Source checkouts only.** Delete the checkout folder, or just its `.venv` folder. If you set `OSCALIS_DB_PATH`,
delete that file too.

uv itself stays installed. To remove it as well, follow
[uv's uninstall instructions](https://docs.astral.sh/uv/getting-started/installation/#uninstallation).

## Develop from source

```
git clone https://github.com/Cyversight/oscalis-mcp.git
cd oscalis-mcp
uv sync --extra dev          # creates .venv and installs everything, including the dev tools
uv run oscalis-mcp           # stdio
uv run oscalis-mcp --transport sse --port 8000   # SSE on 127.0.0.1, no authentication (loopback only)
uv run pytest -q
```

When running from a checkout, the server reads `data/` in the repository. Set `OSCALIS_DATA_DIR` to use another folder.

`data/` ships with the real NIST and FedRAMP content, the generated GovRAMP profiles, and the CSF crosswalk. To refresh:

```
python src/scripts/ingest_catalogs.py [--schemas]                 # NIST catalogs, 800-53B and FedRAMP profiles
python src/scripts/import_csf_crosswalk.py                        # CSF 2.0 to 800-53 crosswalk, from NIST's download
python src/scripts/import_govramp.py --source <folder>            # GovRAMP profiles, from GovRAMP's workbooks (optional)
```

Oscalis does not need the GovRAMP workbooks to run. They are not in this repository, and the generated profiles in
`data/profiles/` are what the server reads. The last command is only for regenerating those profiles after GovRAMP
publishes new workbooks, which you download from GovRAMP yourself.

Tests use their own small fixtures in `tests/fixtures/data/` and do not depend on `data/`.

To point a client at a source checkout, use the checkout's Python (`<repo>/.venv/bin/python` on Linux and macOS,
`<repo>\.venv\Scripts\python.exe` on Windows) with `"args": ["-m", "oscalis.server"]`.

Environment: `OSCALIS_DATA_DIR`, `OSCALIS_DB_PATH` (file-backed index), `OSCALIS_TRANSPORT`, `OSCALIS_HOST`,
`OSCALIS_PORT`, `OSCALIS_MAX_HOPS` (1-4), `OSCALIS_MIN_CONFIDENCE`, `OSCALIS_DEFAULT_LIMIT`, `OSCALIS_MAX_LIMIT`,
`OSCALIS_MAX_FULL_ITEMS`, `OSCALIS_MAX_RESPONSE_CHARS` (default 24,000), `OSCALIS_AUTH_TOKEN` (SSE bearer token).

## SSE security

stdio needs no authentication because the client launches the server itself. SSE listens on a socket:

- With no token, SSE binds to loopback only. Starting it on any other address **refuses to start**. On every loopback
  address (127.0.0.1, 127.0.0.2, `localhost`, `::1` and their other spellings) the server checks the `Host` and
  `Origin` headers, so a web page cannot reach it through DNS rebinding.
- Set `OSCALIS_AUTH_TOKEN` (at least 16 characters, no whitespace) to require `Authorization: Bearer <token>` on
  every request, including unknown paths. The comparison is constant-time and the token is never logged. A token is
  also what allows a non-loopback bind.
- This is one shared secret over plain HTTP. Put TLS in front of it (a reverse proxy) before exposing it beyond a
  trusted network. There is no per-user identity, rate limiting or audit log.

```powershell
$env:OSCALIS_AUTH_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
oscalis-mcp --transport sse --host 0.0.0.0 --port 8000
```

On Linux and macOS (bash or zsh):

```bash
export OSCALIS_AUTH_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
oscalis-mcp --transport sse --host 0.0.0.0 --port 8000
```

## Tools

All tools are read-only. Every control-returning tool takes `projection`: `summary` (default), `prose_only`, or
`full_oscal`. List tools take `limit`/`offset` and report `total`, `truncated`, and `next_offset`. Arguments are bounded:
`offset` at most 1,000,000, `profiles` and `target_catalogs` at most 25 distinct keys, and search queries at most 512
characters.

| Tool | Purpose |
|------|---------|
| `list_catalogs` | Loaded catalogs and profiles with control counts (catalogs also give their withdrawn-control count) |
| `get_control` | One control by catalog and id (case-insensitive; `AC-2(1)` is accepted for `ac-2.1`). Withdrawn controls carry `status` and the control they were incorporated into or moved to. Optional `profiles` adds baseline membership, each profile's parameter values, and its additional requirements |
| `search_controls` | Full-text search (SQLite FTS5) over title, statement, guidance |
| `crosswalk_control` | Direct and transitive mappings with confidence, method, hop path, and optional baseline membership, parameter values and requirements |
| `baseline_delta` | Controls added or removed between two profiles, plus changed parameter values and changed requirement text. Works across programs: values FedRAMP sets on legacy parameter ids are compared on the ids they stand for, and identical sentences filed under different note labels are not reported |
| `list_profile_controls` | The controls a profile (baseline) selects, paginated, with projection |
| `ai_governance_map` | AI RMF subcategories to security controls, or a listing of AI RMF controls with mapped-target counts |

Resources: `oscal://catalogs`, `oscal://catalog/{key}/control/{control_id}`, `oscal://profile/{key}`.

Measured on the real 800-53 Rev 5.2 data: index load under 1 s with all 18 profiles and about 1,500 crosswalk edges,
control lookup about 0.01 ms median, FTS search about 0.4 ms.

**Token cost.** Results are sent as one block of compact JSON, with no indentation and no structured duplicate. Sizes
below are estimates at 3.6 characters per token, not tokenizer counts:

| Call | Approx. tokens |
|------|---------------:|
| `get_control` `summary` (default) | 43 |
| `get_control` `prose_only` | 460 |
| `get_control` `full_oscal` | 7,200 |
| `search_controls`, default 25 results | 1,100 |
| `crosswalk_control`, 14 mappings | 1,300 |
| `crosswalk_control`, 14 mappings and 2 profiles | 2,400 |
| `baseline_delta`, Moderate to High | 2,400 |
| `list_profile_controls`, default 25 controls | 950 |
| All 7 tool definitions (always in context) | 1,700 |

Compared with indented JSON and the earlier crosswalk shape, 16 representative calls went from about 70,400 to 46,300
tokens (34% fewer), and the crosswalk responses fell 39–61%. `tests/test_wire_format.py` holds a size ceiling for each
common call so a regression fails the build.

List tools return at most 3 items when `projection` is `full_oscal` (`OSCALIS_MAX_FULL_ITEMS`). A control's full OSCAL
has a median of about 1,150 tokens and reaches about 9,300 for the largest (`pl-2`). Use `summary` to scan, then fetch
single controls with `get_control`.

List tools also keep each response under `OSCALIS_MAX_RESPONSE_CHARS` (default 24,000 characters), because MCP clients
cap a single tool result (Kiro: 30,000 characters) and a page over that limit is cut off. When a page would be bigger,
the server returns fewer items, reports the smaller `limit`, and says so in `hint`. The next page starts at
`offset + limit` as usual. One item larger than the budget is still returned whole, so `get_control` with `full_oscal`
on the largest controls (PL-2, about 33,600 characters) can still exceed a client's limit.

`list_catalogs` shortens NIST's long titles (it drops "Electronic (OSCAL) Version of" and abbreviates "Special
Publication" and "Revision") and writes OSCAL versions without a leading "v". `oscal://catalogs` gets the same
values. The published titles remain in the OSCAL files and the index.

Crosswalk responses keep `path` only for multi-hop mappings. A one-hop mapping is its own hop: `source` is the starting
control, `target` the destination, and `ref` points into the response's `refs` table, where each provenance string is
stored once. Fields that carry no information are omitted (null `class`/`parent_id`, empty `values`/`constraints`, and
`derived_inverse` unless it is true).

## Crosswalks

| File | Source | Edges | Status |
|------|--------|------:|--------|
| `csf-v2__800-53r5.2.json` | NIST's CSF 2.0 informative references (SP 800-53 Rev 5.2.0), public domain | 737 | Official source. Relation `related`, method `intersecting` |
| `ai-rmf__800-53r5.2.json` | Analyst-authored. No published source exists | 6 | Illustrative. Do not rely on it |

NIST's CSF download says a control is an informative reference for an outcome and gives **no relationship type** (no
"equal to" or "subset of"). So the mapping is labelled a partial overlap and nothing is marked `direct`. Confidence
(0.50-0.70) is an Oscalis heuristic computed only from NIST's own counts: an outcome that cites few controls, and a
control cited by few outcomes, are tighter fits. It is not a NIST value. NIST publishes no AI RMF to SP 800-53 mapping,
so the AI RMF edges stay analyst-authored. Regenerate the CSF file with `import_csf_crosswalk.py`.

## GovRAMP

GovRAMP publishes its baselines as spreadsheets, not OSCAL. `src/scripts/import_govramp.py` reads the workbooks and
writes OSCAL profiles that import the NIST 800-53 catalog. Control text always comes from NIST, never from the
workbooks. What is GovRAMP's own is the membership, the parameter values, and the additional requirements.

| Profile | Controls | Built from |
|---------|---------:|------------|
| `govramp-low` / `-moderate` / `-high` | 153 / 319 / 406 | Controls Matrix, "Required for Authorized" |
| `govramp-core` | 60 | Moderate controls flagged Core |
| `govramp-ready-low` / `-moderate` / `-high` | 29 / 80 / 90 | controls flagged Ready |
| `govramp-moderate-cjis` | 332 | Moderate + CJIS 6.0 matrix |
| `govramp-low-federal` / `-moderate-federal` / `-high-federal` | 153 / 321 / 408 | baseline plus the FedRAMP alignment overlay (chained; tabs cascade as the workbook states) |

Parameter values such as `AC-2 (h) (1) [twenty-four (24) hours]` are mapped onto catalog parameter ids
(`ac-02_odp.06`) by finding the statement item the locator names. A value is placed only when it fits without
guessing; everything else is kept as text. Text that could not be placed, and every "additional requirement", is
attached to the control as a note and returned by `get_control` / `crosswalk_control` when `profiles` is given
(count only for `summary`, full text for `prose_only` and `full_oscal`).

How well the mapping worked. Entries are lines in GovRAMP's parameter cells. "Kept as text" excludes pointers such as
"see additional requirements", which carry no value:

| Profile | Entries | Kept as text |
|---------|--------:|-------------:|
| Low | 148 | 0 |
| Moderate | 213 | 1 |
| High | 248 | 4 |
| Moderate + CJIS | 476 | 16 |

Checked against FedRAMP: of the 202 parameters both programs set, 185 carry the same wording on the same OSCAL id, 17
differ in program vocabulary ("GovRAMP PMO" vs "FedRAMP PMO"), and none conflicts. Eight CJIS values were placed by
the sub-item rule (`cjis-subitem`), which is the lowest-confidence tier. The report records the method for every value.

`reports/govramp-import-report.json` holds the full detail: how each value was placed, everything that was not, and the
cross-checks against GovRAMP's own Core, Ready and CJIS workbooks and against FedRAMP's published profiles.

Deliberately **not** imported: the 3PAO assessor matrices (their objectives and test methods are NIST SP 800-53A content
already in the catalog, regrouped, plus an empty results form) and the "MITRE protection value" column (a score from
MITRE that cannot be attributed or acted on here).

## Layout

```
checks/           pre-tool-eval.py (quality gate), git-pre-commit.py (checks behind the git hook),
                  validate-oscal-schemas.py, verify-crosswalk-integrity.py, check-data-on-save.py (for an editor's
                  "validate on save" hook), _bootstrap.py
.githooks/        git pre-commit, a small sh wrapper (enable: git config core.hooksPath .githooks)
data/             catalogs/, profiles/, crosswalks/, sources.lock.json
src/oscalis/      core/ (config, oscal, graph, integrity, auth, govramp, govramp_profiles), tools/, server.py
src/scripts/      ingest_catalogs.py, import_govramp.py, import_csf_crosswalk.py
tests/            loader, crosswalk engine, MCP endpoints, wire format and size budgets, GovRAMP, ingest,
                  importers, auth; fixtures/data/ is the test seed set
reports/          govramp-import-report.json
```

## Quality gates

```
python checks/pre-tool-eval.py [--fast]   # ruff, mypy --strict, pytest, schema validation, crosswalk integrity
python checks/validate-oscal-schemas.py [paths]
python checks/verify-crosswalk-integrity.py
```

The same commands work in PowerShell, bash and zsh; no bash is needed on Windows. `--fast` skips pytest.

`validate-oscal-schemas.py` rejects non-OSCAL keys, missing or malformed UUIDs, dangling `#id` links, profile imports
that do not resolve, OSCAL versions below 1.1.0, and out-of-range crosswalk confidence. Run
`python src/scripts/ingest_catalogs.py --schemas` to also check catalogs and profiles against NIST's official JSON
schemas. `tests/test_mcp_endpoints.py` fails if a tool or resource is registered without a test case.

The quality gate stops at the first failing stage. The git pre-commit hook refuses private local files listed in
`checks/git-pre-commit.py`, blocks bad data (file names with spaces included), and lets unrelated commits through.
It checks only what is staged under `data/`. Its checks are Python (`checks/git-pre-commit.py`); `.githooks/pre-commit`
is a POSIX sh wrapper, which git runs with its own sh on every system, including the one bundled with Git for Windows.

For an editor's "validate on save" hook, run `python checks/check-data-on-save.py`. It re-runs itself inside `.venv`
when the `python` on PATH lacks the dependencies.

## Known gaps

- **AI RMF crosswalk is analyst-authored.** Six edges, labelled `intersecting` or `advisory`. NIST publishes no
  AI RMF to SP 800-53 mapping. Treat these as leads, not compliance evidence. The CSF crosswalk has an official source
  but no relationship types, so nothing in the project is `direct`.
- **Crosswalk format is project-specific.** It follows OSCAL's mapping-collection structure but is not the official
  schema, so NIST's schemas cannot validate it. Catalogs and profiles are standard OSCAL.
- **GovRAMP is derived, not official.** No official GovRAMP OSCAL was found. The profiles are generated from the
  workbooks and reflect the versions recorded in `data/sources.lock.json` and the import report. 16 CJIS parameter
  entries stay as text because CJIS Policy numbers statement sub-items differently from NIST in ways that cannot be
  resolved safely. Re-run the importer when GovRAMP publishes new workbooks. `data/profiles/govramp-*.json` and
  `reports/` contain GovRAMP-authored text (parameter values and additional requirements). That text remains
  GovRAMP's, so check GovRAMP's current terms before reusing it.
- **GovRAMP and FedRAMP disagree on two controls.** FedRAMP's published profile includes `ca-8.2` (Moderate, High) and
  `sa-4.10` (all levels). GovRAMP's Federal Overlay marks both "not required" with empty FedRAMP columns. The profiles
  follow GovRAMP's workbook. Which source is current is not determinable from the files.
- **GovRAMP parameter values are text.** They are what GovRAMP wrote (for example "at least annually"), stored as
  `values`. FedRAMP stores equivalent text as `constraints`; `baseline_delta` compares the text, not the field.
- **FedRAMP source:** the original `GSA/fedramp-automation` repository is no longer reachable. Ingest uses the
  `oscal-compass-lab/fedramp-automation` mirror. Those files are OSCAL 1.0.4, below the 1.1 floor, and are accepted
  with a warning because they are pinned in `data/sources.lock.json`. Verify provenance before relying on them.
- **Profile resolution covers what this server outputs.** Resolved: `include`/`exclude` by id and by `matching`
  wildcard, `with-child-controls` (per selector), `include-all`, chained imports, `set-parameters`, and text parts added
  by `alters`. Not applied: `merge`, and `alters` that remove parts or insert text into a control. Both change the
  structure or wording of a *resolved catalog document*, which this server never emits. It returns control text from the
  source catalog and a profile's selection, parameters and notes separately, so there is nothing for them to act on.
- **Edges are followed outgoing only.** Inverses are derived for `equivalent`, `related`, `superset`, `subset`.
  `satisfies` is not inverted, so query from the satisfying side.
- **The ingest and CSF import scripts need network access** and are therefore not run in CI. Their logic is tested
  offline with a fake downloader (the network is blocked in those tests).

## License

Copyright 2026 Cyversight LLC. The code, tests, scripts, documentation and the hand-authored AI RMF crosswalk are
licensed under the [Apache License, Version 2.0](LICENSE). Created and maintained by Al Lyle at Cyversight LLC.

The bundled NIST, FedRAMP and GovRAMP data is third-party material and is not covered by that license. [NOTICE](NOTICE)
lists each file group, its source and its terms. In particular, GovRAMP-authored text in
`data/profiles/govramp-*.json` and `reports/` remains GovRAMP's.
