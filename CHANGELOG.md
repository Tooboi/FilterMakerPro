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

## [0.6.6]

### Changed
- **One shared hair collection and one shared dust collection** for every filter in the file ("FM Hair Strands", "FM Dust Pieces"). New filters reuse them instead of adding their own copies; old per-filter collections are cleaned up automatically.
- Curl, Strand Shapes, Dust Types, Piece Variants and the dust material are shared by all filters (marked in the panel). Density, length, width, lift, tilt, seeds and the hair material stay per filter.
- Hair strands keep a **constant width** at any length and **close to a point at both ends**. The hair is now built as real geometry by the Geometry Nodes scatter.
- Hair and dust Seed only change placement; presets set the shared strand / dust-piece look.

### Fixed
- Pleat ends could poke through the border: the seal past the edge now covers shell thickness and wear, Chamfer / Round bevels are limited so they never uncover the pleats, the Lip's rim now rises above the frame instead of stepping the inner part down, frames keep their full height where they bend around box corners and folds, and the frame's inner edge no longer folds back on itself at corners when the outline is densely cut (gaps and clipping at the corners).
- Curved panels (e.g. a cylinder section) get their full border width; the width limit now measures the panel across its surface instead of its world bounding box.

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

[Unreleased]: https://github.com/Tooboi/FilterMakerPro/compare/v0.6.6...HEAD
[0.6.6]: https://github.com/Tooboi/FilterMakerPro/releases/tag/v0.6.6
[0.6.5]: https://github.com/Tooboi/FilterMakerPro/releases/tag/v0.6.5
[0.6.4]: https://github.com/Tooboi/FilterMakerPro/releases/tag/v0.6.4
[0.6.3]: https://github.com/Tooboi/FilterMakerPro/releases/tag/v0.6.3
[0.5.2]: https://github.com/Tooboi/FilterMakerPro/releases/tag/v0.5.2
