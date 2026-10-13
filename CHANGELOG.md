# Changelog

All notable changes to Filter Maker Pro are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- README with renders, diagrams and panel reference.
- `blender_manifest.toml` so the add-on installs as a Blender 4.2+ extension.
- Headless test suite and showcase renderer in `tests/`.
- Issue and pull request templates, and a release workflow that builds the zip on tag.

### Changed
- Minimum Blender version set to 4.2 (`bl_info` and manifest).

## [0.6.5]

### Changed
- Border shading defaults to **Auto Smooth** only (Shade Smooth is now opt-in).
- Hair and dust each have their own **Border Falloff** and **Viewport Display %**.
- The *Filter Maker Library* collections are coloured brown and kept at the bottom of the Outliner.

## [0.6.4]

### Fixed
- Pleat ends no longer clip through the border: a seal is added under the frame and the wear fades out beneath it.
- Hair and dust no longer spawn under the border (per-point culling with a falloff instead of face-blended density).

### Performance
- The pleated sheet is cached between edits; scatter-only sliders refresh in about 5 ms.
- numpy outline-distance pass on simplified outline segments.
- The curve-sync handler only re-reads curves that changed.

## [0.6.3]

### Changed
- Face mode is remeshed on a structured quad grid (99.8–100% quads) with welded seams and smooth normals across panel joins.
- Pleat period snaps to a whole number of pleats on closed rings.

### Added
- Geometry Nodes scatter for hair and dust on a shared emitter; the instance collections are wired up automatically.
- Hair lies mostly flat (Lift setting), and dust and hair never share spawn points.

## [0.5.2] and earlier

- Curve, edge and face sources; continuous pleats across adjacent faces.
- Pleat bevel with cuts, Clouds-style wear, borders with Flat/Chamfer/Round/Lip profiles and overhang.
- Boolean with a bounding-box or envelope cutter; presets; triangle/quad topology; custom line-art panel icons.

[Unreleased]: https://github.com/Tooboi/FilterMakerPro/compare/v0.6.5...HEAD
[0.6.5]: https://github.com/Tooboi/FilterMakerPro/releases/tag/v0.6.5
[0.6.4]: https://github.com/Tooboi/FilterMakerPro/releases/tag/v0.6.4
[0.6.3]: https://github.com/Tooboi/FilterMakerPro/releases/tag/v0.6.3
[0.5.2]: https://github.com/Tooboi/FilterMakerPro/releases/tag/v0.5.2
