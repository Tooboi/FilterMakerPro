"""Build Filter Maker Lite (the free edition) from the Pro source.

    python tools/build_lite.py [output_dir]          (default: dist/FilterMakerLite)

Lite keeps: every source (new line, curve, edges, faces), pleat shaping, bevel,
shading, topology and a flat border (width only). Pro-only code is replaced by
small stubs, so it is not in the Lite file at all (and Lite never advertises Pro):
  wear (Clouds displace), hair & dust, border profiles / overhang / front-back,
  boolean cutter and presets.
Lite uses the same scene data as Pro, so a file made with Lite opens in Pro with
all its settings. Only one edition can be enabled at a time.
"""
import ast
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "__init__.py")
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "dist", "FilterMakerLite")
# Lite has no upgrade prompts in Blender's UI: the Blender Extensions platform does not allow
# promoting commercial versions inside the add-on (Terms of Service, section 6).

src = open(SRC, encoding="utf-8").read()


def rep(old, new, count=1):
    global src
    n = src.count(old)
    if n != count:
        raise SystemExit("build_lite: expected %d match(es), found %d for:\n%s" % (count, n, old[:200]))
    src = src.replace(old, new)


# ---- identity ---------------------------------------------------------------
rep('"name": "Filter Maker Pro",', '"name": "Filter Maker Lite",')
src = re.sub(r'"description": "[^"]*",',
             '"description": "Free pleated filter generator: line, curve, edge and face sources, '
             'pleat shaping, pleat bevel and a clean border.",', src, count=1)
rep('EDITION = "PRO" ', 'EDITION = "LITE"')
src = re.sub(r'bl_label = "Filter Maker Pro  ([0-9.]+)"', r'bl_label = "Filter Maker Lite  \1"', src, count=1)

# ---- replace Pro-only functions with stubs ----------------------------------
PRO_ONLY = "Not available in Filter Maker Lite."
STUBS = {
    "apply_noise": "return",
    "update_particles": ("_remove_child(p, \"particles_obj\")\n"
                         "_remove_lib(p, \"hair_lib\")\n"
                         "_remove_lib(p, \"dust_lib\")\n"
                         "p.hair_count = p.dust_count = 0"),
    "refresh_scatter": "return",
    "sync_scatter": "return None",
    "sync_boolean": "return",
    "build_cutter": "raise RuntimeError(%r)" % PRO_ONLY,
    "_build_scatter": "raise RuntimeError(%r)" % PRO_ONLY,
    "_hair_tubes": "raise RuntimeError(%r)" % PRO_ONLY,
    "_scatter_group": "raise RuntimeError(%r)" % PRO_ONLY,
    "_build_hair_lib": "raise RuntimeError(%r)" % PRO_ONLY,
    "_build_dust_lib": "raise RuntimeError(%r)" % PRO_ONLY,
    "_dust_mesh_piece": "raise RuntimeError(%r)" % PRO_ONLY,
    "_lint_curve": "raise RuntimeError(%r)" % PRO_ONLY,
    "_strand_points": "raise RuntimeError(%r)" % PRO_ONLY,
    "_sync_lib": "raise RuntimeError(%r)" % PRO_ONLY,
    "save_preset": "raise RuntimeError(%r)" % PRO_ONLY,
    "style_material": "return None",
    "_add_end_caps": "return",
    "_add_grille": "return",
    "_add_pull_tabs": "return",
    "update_wire": "_remove_child(p, \"wire_obj\")",
    "update_print": "_remove_child(p, \"print_obj\")",
}
# Pro-only definitions dropped from the Lite file entirely
REMOVE = {"_build_media", "_build_frame", "FILTER_OT_game_ready", "FILTER_PT_game", "_realized_copy",
          "_split_sheet_uvs", "_Override", "_bm_to_object", "_game_collection", "_image_node",
          "FILTER_OT_add_shape", "_shape_mesh", "_grille_island", "_grille_lines", "_grille_open",
          "_seg_dist2d", "_islands", "_outline_frames", "_tab_sides", "_plate", "build_wire_splines",
          "_curve_child", "wire_material", "_text_shape", "_auto_size_label", "print_material",
          "build_print", "_print_strip", "_furnace_settings", "FILTER_PT_diecut"}
tree = ast.parse(src)
lines = src.split("\n")
spans = []
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in REMOVE:
        start = node.lineno - 1 - len(node.decorator_list)
        spans.append((start, node.end_lineno, "-" + node.name))
        continue
    if isinstance(node, ast.FunctionDef) and node.name in STUBS:
        first = node.body[0]
        start = first.lineno - 1                       # first body line (0-based)
        if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) \
                and isinstance(first.value.value, str):
            start = first.end_lineno                   # keep the docstring
        spans.append((start, node.end_lineno, node.name))
missing = (set(STUBS) | {"-" + n for n in REMOVE}) - {s[2] for s in spans}
if missing:
    raise SystemExit("build_lite: functions not found: %s" % ", ".join(sorted(missing)))
for start, end, name in sorted(spans, reverse=True):
    if name.startswith("-"):
        lines[start:end] = []
        continue
    body = ["    # Filter Maker Pro only (removed from the Lite build)"]
    body += ["    " + ln for ln in STUBS[name].split("\n")]
    lines[start:end] = body
src = "\n".join(lines)

# ---- Pro settings are switched off whenever Lite rebuilds a filter ------------
rep('''def regenerate(obj):''', '''_LITE_OFF = (("use_hair", False), ("use_dust", False), ("use_boolean", False),
             ("noise_strength", 0.0), ("border_profile", 'FLAT'), ("border_overhang", 0.0),
             ("border_caps", 'NONE'), ("filter_style", 'CUSTOM'), ("frame_style", 'CUSTOM'),
             ("dirt", 0.0), ("grille_pattern", 'NONE'), ("use_wire", False), ("pull_tabs", 0),
             ("use_print", False))


def _lite_gate(p):
    """Lite: turn off Pro-only settings (a filter made in Pro keeps its pleats and
    border, without wear, hair, dust, border styles or boolean)."""
    prev = _busy[0]
    _busy[0] = True
    try:
        for key, value in _LITE_OFF:
            if getattr(p, key) != value:
                setattr(p, key, value)
    finally:
        _busy[0] = prev


def regenerate(obj):''')
rep('''    p.last_error = ""
    bm = rb = None
    tm = {}''', '''    p.last_error = ""
    _lite_gate(p)
    bm = rb = None
    tm = {}''')

# ---- UI ---------------------------------------------------------------------
rep('''
        col.operator_menu_enum("filter.add_shape", "shape", text="Add Filter Shape", icon=ic('MESH_GRID'))''', '')
rep('''        c.label(text="End caps (rings, cylinders)")
        c.prop(p, "border_caps", text="")
        sub = c.row(align=True)
        sub.active = p.border_caps != 'NONE'
        sub.prop(p, "border_cap_hole", slider=True)
        c.separator()
''', '')
rep('''        col = layout.column(align=True)
        col.prop(p, "filter_style")
        if p.filter_style == 'CUSTOM':
            col.template_ID(p, "filter_material", new="material.new")
        if p.use_border:
            col.separator()
            col.prop(p, "frame_style")
            if p.frame_style == 'CUSTOM':
                col.template_ID(p, "border_material", new="material.new")
        col.separator()
        row = col.row(align=True)
        row.active = p.filter_style != 'CUSTOM' or p.frame_style != 'CUSTOM'
        row.prop(p, "dirt", slider=True)
        col.separator()''', '''        col = layout.column(align=True)
        col.template_ID(p, "filter_material", new="material.new")
        if p.use_border:
            col.template_ID(p, "border_material", new="material.new")
        col.separator()''')
rep('''        row.operator("filter.randomize", text="Randomize", icon=ic('FORCE_VORTEX'))
''', '')
rep('''        layout.prop(p, "border_profile", expand=True)
        c = layout.column(align=True)
        c.label(text="Inward / outward")
        c.prop(p, "border_width", text="Width (inward)")
        c.prop(p, "border_overhang", text="Overhang (outward)")
        if p.border_profile != 'FLAT':
            c.prop(p, "border_bevel", text="Lip Step" if p.border_profile == 'LIP' else "Bevel Size")
            if p.border_profile == 'ROUND':
                c.prop(p, "border_cuts")
        c = layout.column(align=True)
        c.label(text="Front / back")
        c.prop(p, "border_fit")
        c.prop(p, "border_front")
        c.prop(p, "border_back")
        c.separator()''', '''        c = layout.column(align=True)
        c.prop(p, "border_width", text="Width")
        c.separator()''')

UPSELL = ''

rep('''

def _teardown():''', UPSELL + '''

def _teardown():''')

# classes: drop Pro panels / operators, add the upsell panel
m = re.search(r"classes = \((.*?)\)\n", src, re.S)
names = [n.strip() for n in m.group(1).replace("\n", " ").split(",") if n.strip()]
drop = {"FILTER_PT_presets", "FILTER_PT_wear", "FILTER_PT_hair", "FILTER_PT_dust", "FILTER_PT_boolean",
        "FILTER_PT_game", "FILTER_OT_game_ready", "FILTER_OT_add_shape", "FILTER_PT_diecut",
        "FILTER_OT_preset_apply", "FILTER_OT_preset_save", "FILTER_OT_preset_update",
        "FILTER_OT_preset_delete", "FILTER_OT_randomize", "FILTER_OT_use_reference"}
unknown = drop - set(names)
if unknown:
    raise SystemExit("build_lite: classes not found: %s" % ", ".join(sorted(unknown)))
keep = [n for n in names if n not in drop]
src = src[:m.start()] + "classes = (" + ", ".join(keep) + ")\n" + src[m.end():]
# the upsell panel is defined after `classes`; move the tuple below it
m = re.search(r"classes = \(.*?\)\n", src, re.S)
tuple_text = m.group(0)
src = src[:m.start()] + src[m.end():]
rep('''

def _teardown():''', "\n\n" + tuple_text + '''

def _teardown():''')

compile(src, "FilterMakerLite/__init__.py", "exec")

# ---- write the package ------------------------------------------------------
os.makedirs(OUT, exist_ok=True)
with open(os.path.join(OUT, "__init__.py"), "w", encoding="utf-8") as f:
    f.write(src)
ver = re.search(r'"version": \((\d+), (\d+), (\d+)\)', src).groups()
with open(os.path.join(OUT, "blender_manifest.toml"), "w", encoding="utf-8") as f:
    f.write('''schema_version = "1.0.0"

id = "filter_maker_lite"
version = "%s"
name = "Filter Maker Lite"
tagline = "Free pleated filter generator for lines, curves and faces"
maintainer = "Tooboi"
type = "add-on"
website = "https://github.com/Tooboi/FilterMakerLite"

tags = ["Modeling", "Mesh", "Object"]

blender_version_min = "4.2.0"

license = ["SPDX:GPL-3.0-or-later"]
copyright = ["2024-2026 Tooboi"]
''' % ".".join(ver))
shutil.copy(os.path.join(ROOT, "LICENSE"), os.path.join(OUT, "LICENSE"))
print("Filter Maker Lite %s written to %s (%d lines)" % (".".join(ver), OUT, src.count("\n")))
