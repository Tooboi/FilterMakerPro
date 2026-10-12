# FilterMakerPro
![Static Badge](https://img.shields.io/badge/version-0.3.11-blue)

FilterMakerPro is a Blender addon that generates pleated, zig-zag air filters from any shape. It is driven entirely from the N-panel in the 3D Viewport, requiring no node setup. You can create filters from lines, curves, mesh edges, or faces.

## Features

- **Versatile Source Geometry**: Generate filters from curves, selected mesh edges, or selected mesh faces.
- **Parametric Pleats**: Control pleat density, depth, thickness, and phase offset.
- **Customizable Folds**: Adjust the beveling on ridges and valleys, from sharp folds to fully rounded corners.
- **Procedural Wear**: Add realistic wear and tear using a configurable noise displacement texture.
- **Detailed Borders**: Create a frame around the filter with multiple profiles (Flat, Chamfer, Round, Lip) and sizing options.
- **Surface Debris**: Scatter procedural hair and dust particles across the filter surface for added realism.
- **Boolean Operations**: Trim the filter using a bounding box, an envelope of the source shape, or a custom mesh object.
- **Live Updates**: See changes in real-time as you adjust parameters. This can be toggled off for complex models to improve performance.
- **Presets**: Save and load your filter configurations to reuse them in other projects.

## Installation

1.  Download the latest release as a `.zip` file from this repository.
2.  In Blender, go to `Edit > Preferences... > Add-ons`.
3.  Click the `Install...` button at the top.
4.  Select the downloaded `.zip` file and click `Install Add-on`.
5.  Enable the addon by checking the box next to "Object: Filter Maker Pro".

## Usage

The addon's controls are located in the 3D Viewport's sidebar. Press the `N` key to open the sidebar and select the "Filter Maker" tab.

### Creating a Filter

There are two main ways to create a filter:

1.  **From Scratch**: Click `New Filter Line` to create a new filter from a simple straight line. You can adjust its initial length in the operator properties.
2.  **From an Existing Object**:
    -   Select a Curve or Mesh object.
    -   In the Filter Maker panel, click `From Curve / Edges` or `From Faces`.
    -   If you are in Edit Mode, the filter will be generated based on your selected vertices, edges, or faces.

### Customizing the Filter

Once a filter object is created and selected, its parameters can be adjusted in the N-panel. The filter will update automatically if "Live Update" is enabled. If not, click the "Update" button to apply your changes.

The panel is organized into several sections:

-   **Presets**: Save, load, and manage your settings configurations.
-   **Pleats**: Controls the fundamental shape of the filter media, including density, depth, thickness, and height (for curve-based filters).
-   **Pleat Bevel**: Adjusts the sharpness or roundness of the pleat folds.
-   **Shading**: Controls smooth shading and auto-smoothing angles.
-   **Wear**: Adds surface imperfections using a noise-based displacement.
-   **Border**: Toggles and configures a frame around the filter's perimeter.
-   **Hair / Dust**: Adds fine strands and particle debris to the surface.
-   **Boolean**: Use another object or a procedural shape to cut, intersect, or union with the filter.
-   **Materials**: Assign materials for the filter media, border, hair, and dust.
-   **Finish**: Tools to bake the filter into a static mesh or delete the filter and all its associated child objects.

### Finalizing a Filter

When you are satisfied with the filter's appearance, you can "bake" it. This converts the procedural filter into a standard, non-editable mesh.

1.  Select the main filter object.
2.  In the "Finish" section of the panel, click `Finish (Bake)`.

This operation will apply any active boolean modifiers, remove the addon's custom properties, and unparent any child objects, leaving you with regular mesh objects.

## License

This project is licensed under the GNU General Public License v3.0. See the `LICENSE` file for details.
