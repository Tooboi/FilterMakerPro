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

## [0.7.0]

### Added
- **UVs on everything.** The pleated sheet is unrolled flat across the folds, so textures keep their proportions on flanks and bevels (no stretching); the frame and end caps get matching UVs. **Real-World Size** tiles textures at a fixed size on every filter (Texture Size), **Fit 0-1** scales each part into the UV square.
- **Built-in materials.** Media: Paper, Synthetic, HEPA, Fibreglass, Carbon. Frame: Painted Metal, Galvanised, Aluminium, Cardboard, Plastic, Rubber. All procedural and texture-free, driven by the new UVs.
- **Dirt.** One slider builds a dirt mask that is heaviest in the middle and in the folds and clean under the frame (mesh attributes `fm_dirt` and `fm_edge`, usable in your own shaders too).
- **Add Filter Shape:** Panel, Round Panel, Cartridge (with end caps), V-Bank and Box Wrap at the 3D cursor.
- **End caps** for ring-shaped filters (cartridges, box wraps), with an optional centre hole.
- **Die-cut face** (Face, Wire & Print panel): a board sheet over the front, back or both sides of the frame with **Diamond** or **Square** openings. Set openings across, opening shape, strut width, corner cut and board thickness. It works on flat panels and also on curved filters, folded filters and rings (cartridge liners, box wraps).
- **Wire mesh support:** wires pleated together with the media (Diamond or Square pattern, spacing, angle, radius, front or back), built as a light curve object.
- **Pull tabs:** one or two board tabs sticking out of the long sides of the frame.
- **Printed graphics:** a nominal size label (for example "16 x 25 x 1", worked out automatically or typed in), an airflow text and an arrow printed on the frame sides.
- **Printed text on the front face:** Print On *Sides*, *Front* or *Sides & Front*. The front strip sits on the top edge of the frame and reads upright, with optional Front Text of its own.
- **White Board** frame material, a **Die-Cut Furnace** preset, and a **Die-Cut Furnace** shape (a 16 x 25 x 1 in filter with all of the above).
- **Game-Ready panel:** one click builds a low-poly copy (low-poly pleats or a flat card), packs its UVs into 0-1, bakes Normal / AO / Base Color / Roughness from the full filter (including hair, dust, wire and print) with Cycles, and can export FBX or glTF. The die-cut face is baked into the maps (the furnace filter becomes a 132-triangle card). Bakes use the GPU when one is enabled, with few samples for the noise-free maps.

### Changed
- **Piece variants are per filter.** The shared hair / dust collection holds as many pieces as the filter that asks for the most; every filter picks from the first N of them (its own Strand Shapes / Piece Variants). Curl, dust types and the dust material stay shared.
- Game-ready objects go into a "<filter> Low" collection.
- New filters start with the Paper media and Painted Metal frame; presets set matching materials (HEPA, Coarse Furnace, Die-Cut Furnace, Dusty Used).
- Faster mesh assembly for face and curve filters (numpy + `foreach_set` instead of `from_pydata` / per-vertex creation).
- Built-in shader setups work with Blender 5.x node socket names (`Fac` renamed to `Factor`).
- Filter Maker Lite now includes the Boolean tool.

## [0.6.6]

### Added
- `tools/build_lite.py` builds **Filter Maker Lite**, the free edition (all sources, pleat shaping, flat border). Lite saves the same data, so its filters open in Pro unchanged.
- Pro and Lite refuse to be enabled at the same time, with a clear message.

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
