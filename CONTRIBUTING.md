# Contributing to Filter Maker Pro

Thanks for helping! Bug reports, feature ideas and pull requests are all welcome.

## Reporting bugs

Use the [bug report form](https://github.com/Tooboi/FilterMakerPro/issues/new?template=bug_report.yml). The most useful things to include:

- Your Blender version (*Help → About*) and OS
- The source type: New Filter Line, curve, mesh edges or faces
- Steps to reproduce, ideally with a small `.blend`
- Any error from *Window → Toggle System Console* (Windows) or the terminal

## Development setup

1. Fork and clone the repo.
2. Link the clone into your Blender add-ons folder, so edits are picked up without reinstalling:
   - **Windows:** `mklink /J "%APPDATA%\Blender Foundation\Blender\5.2\scripts\addons\FilterMakerPro" C:\path\to\FilterMakerPro`
   - **macOS / Linux:** `ln -s /path/to/FilterMakerPro ~/.config/blender/5.2/scripts/addons/FilterMakerPro` (macOS: `~/Library/Application Support/Blender/5.2/...`)
3. Enable **Object: Filter Maker Pro** and use *F3 → Reload Scripts* after edits.

## Running the tests

The suite runs in Blender without a window and loads the add-on directly from the repo:

```bash
blender -b --factory-startup --python-exit-code 1 --python tests/run_tests.py
```

- On Windows you can double-click `tests/run_tests.bat`.
- Results go to `tests/results/`: `test_log.txt`, `summary.json` and preview renders.
- To benchmark against an older build, set `FM_COMPARE=/path/to/old/__init__.py`.
- `tests/render_showcase.py` regenerates the README renders (transparent PNGs in `tests/results/showcase/`).

Please run the suite before opening a PR and mention the result.

## Code guidelines

- Keep the add-on dependency-free (Blender's bundled Python and numpy only).
- Everything should stay controllable from the N-panel. Generated node groups are implementation details.
- New settings need a `description`, a sensible default, and an entry in `PRESET_KEYS` if presets should store them.
- Settings that only affect the scatter should use the `_upd_scatter` update, so they don't rebuild the mesh.
- Bump the version in **both** `bl_info` and `blender_manifest.toml`, and add a `CHANGELOG.md` entry.

## Releasing (maintainers)

1. Update the version in `__init__.py` (`bl_info`), `blender_manifest.toml` and the panel label, and move the changelog's *Unreleased* entries under the new version.
2. Optionally write the release text in `docs/releases/v0.6.6.md` (used as the GitHub release body; otherwise the changelog section is used).
3. Commit, then tag: `git tag v0.6.6 && git push --tags`.
4. The **Release** workflow checks that the versions match, builds `FilterMakerPro-0.6.6.zip` and publishes a GitHub release.
