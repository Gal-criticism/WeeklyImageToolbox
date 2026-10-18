# Repository structure

The maintained implementation lives in `src/galgame_news/` and is launched
with the module CLI:

```powershell
python -m galgame_news run INPUT.docx --issue ISSUE --output output
```

`--output` names the root that contains per-run task folders, not an issue's
direct output directory.

The production flow is intentionally layered:

- `domain.py` and `config.py` define models, enums, and validated settings.
- `ingestion/`, `discovery/`, and `curation/` parse, discover, download, and
  score candidates.
- `pipeline/runner.py` is the single public orchestration entry point;
  `pipeline/workspace.py` owns task-directory safety and
  `pipeline/checkpoint.py` owns checkpoint persistence and integrity checks.
- `delivery/` writes deterministic raw indexes and stores review state.
- `review/` performs non-destructive decisions and final export.
- `desktop/` and `cli.py` are frontends over the same runner contracts.

Discovery adapters remain import-compatible through
`galgame_news.discovery.adapters`, now organized as `adapters/html.py`,
`adapters/x.py`, `adapters/media.py`, and shared `adapters/common.py`.

## Local data and packaging

`input/`, `output/`, `.state/`, `cache/`, `dist/`, `build/`, and generated
Python metadata are local-only paths. Existing user files in those paths are
not removed by structural cleanup. The packaging spec includes only
`config/default.toml`, plus optional `assets/` and `bin/` files. The build
script builds in a temporary staging directory, overlays only produced files
into `dist/GalgameNewsToolbox/`, preserves unknown bundle files and
`dist/.../output`, and creates the root `00_启动工具箱.lnk` on Windows after a
successful build.

The former `image_prescan.py` implementation, its legacy engine, and the
tests/docs dedicated solely to that engine are retired. The compatibility
script now exits non-zero with the supported module-CLI usage. `scripts/` keeps
the offline `evaluate.py` helper; the empty live-smoke placeholder is removed.

No network or model call is part of structural verification. Runtime behavior,
checkpoint schema, retry semantics, event order, output naming, and public
imports remain unchanged.
