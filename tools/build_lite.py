"""Build Filter Maker Lite (the free edition) from the Pro source.

    python tools/build_lite.py [output_dir]          (default: dist/FilterMakerLite)

Lite is a self-contained add-on. Its code holds only the features it exposes:
every source (new line, curve, mesh edges, faces), pleat shaping, pleat bevel,
shading, topology, UVs, a flat border, the Boolean tool and the material slots.
Nothing else is in the file: Pro-only code is cut out, not disabled.

How the Pro source is turned into Lite:
  1. Marked blocks:   # >>> PRO ... # <<< PRO   are removed. A block may hold a Lite
     alternative after   # --- LITE   as lines starting with   #|   (uncommented here).
  2. FilterProps keeps only the properties listed in LITE_PROPS.
  3. Every top-level function, class and constant that nothing references any more is
     removed (repeated until nothing changes).
  4. The result is checked: it must compile, every name must be defined, no removed
     property / operator may be referenced and no Pro feature words may remain.
"""
import ast
import builtins
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "__init__.py")
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "dist", "FilterMakerLite")

LITE_PROPS = {
    "is_filter", "mode", "src", "live", "last_error", "source_obj",
    "use_thickness", "thickness", "pleat_depth", "pleat_density", "pleat_phase", "detail", "height",
    "height_dir", "pleat_angle", "mesh_type", "face_smooth", "face_smooth_factor", "face_smooth_cuts",
    "bevel_ridge", "bevel_valley", "bevel_cuts", "smooth", "auto_smooth", "auto_smooth_angle",
    "use_border", "border_width", "border_smooth", "border_auto_smooth", "border_auto_smooth_angle",
    "use_boolean", "boolean_source", "boolean_obj", "boolean_modifier_name", "boolean_op", "boolean_solver",
    "cutter_margin", "show_cutter",
    "uv_mode", "uv_tile", "filter_material", "border_material", "border_obj", "cutter_obj",
}
# words that must not appear anywhere in the Lite file (case-insensitive, whole words)
FORBIDDEN = ("hair", "dust", "noise", "wear", "grille", "wire", "scatter", "particle", "particles",
             "preset", "presets", "dirt", "game", "Pro", "edition", "upgrade", "overhang", "chamfer",
             "lip", "die-cut", "furnace", "cartridge", "randomize")
# words allowed in these exact contexts (Blender API names etc.)
ALLOWED_CONTEXT = ("show_wire", "wireframe", "WIREFRAME", "'WIRE'")

src = open(SRC, encoding="utf-8").read()


def fail(msg):
    raise SystemExit("build_lite: " + msg)


# ---- 1. marked blocks --------------------------------------------------------
out, mode = [], None
for n, line in enumerate(src.split("\n"), 1):
    tag = line.strip()
    if tag == "# >>> PRO":
        if mode is not None:
            fail("nested PRO block at line %d" % n)
        mode = "pro"
        continue
    if tag == "# --- LITE":
        if mode != "pro":
            fail("LITE part outside a PRO block at line %d" % n)
        mode = "lite"
        continue
    if tag == "# <<< PRO":
        if mode is None:
            fail("unmatched end of PRO block at line %d" % n)
        mode = None
        continue
    if mode == "pro":
        continue
    if mode == "lite":
        m = re.match(r"^(\s*)#\| ?(.*)$", line)
        if not m:
            if tag:
                fail("LITE line must start with '#|' at line %d" % n)
            out.append("")
            continue
        out.append(m.group(1) + m.group(2))
        continue
    out.append(line)
if mode is not None:
    fail("unterminated PRO block")
src = "\n".join(out)

# ---- identity ----------------------------------------------------------------
# Lite can be released on its own schedule (e.g. a fix only the Extensions platform needs)
LITE_VERSION = (0, 7, 1)
src = re.sub(r'"version": \(\d+, \d+, \d+\)', '"version": (%d, %d, %d)' % LITE_VERSION, src, count=1)
src = re.sub(r'bl_label = "Filter Maker Pro  [0-9.]+"', 'bl_label = "Filter Maker Pro  %d.%d.%d"' % LITE_VERSION,
             src, count=1)
src = src.replace('"name": "Filter Maker Pro",', '"name": "Filter Maker Lite",', 1)
src = re.sub(r'"description": "[^"]*",',
             '"description": "Free pleated filter generator: line, curve, edge and face sources, '
             'pleat shaping, a clean border and the Boolean tool.",', src, count=1)
src = re.sub(r'bl_label = "Filter Maker Pro  ([0-9.]+)"', r'bl_label = "Filter Maker Lite  \1"', src, count=1)


# ---- 2. FilterProps: Lite properties only ------------------------------------
def drop_lines(text, spans):
    lines = text.split("\n")
    for a, b in sorted(spans, reverse=True):
        del lines[a:b]
    return "\n".join(lines)


tree = ast.parse(src)
spans = []
for node in tree.body:
    if isinstance(node, ast.ClassDef) and node.name == "FilterProps":
        for st in node.body:
            if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) \
                    and st.target.id not in LITE_PROPS:
                spans.append((st.lineno - 1, st.end_lineno))
src = drop_lines(src, spans)


# ---- 3. remove everything nothing refers to -----------------------------------
def top_names(node):
    if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return {t.id for t in targets if isinstance(t, ast.Name)}
    return set()


def used_names(node):
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", n.value))
    return out


KEEP_ALWAYS = {"bl_info", "register", "unregister", "classes"}
for _round in range(50):
    tree = ast.parse(src)
    defs = {}
    for node in tree.body:
        for nm in top_names(node):
            defs.setdefault(nm, []).append(node)
    refs = {}
    for node in tree.body:
        names = top_names(node)
        u = used_names(node)
        if names:
            for nm in names:
                refs.setdefault(nm, set()).update(u - {nm})
        else:
            refs.setdefault("<module>", set()).update(u)
    live = set(KEEP_ALWAYS) | {"<module>"}
    stack = list(live)
    while stack:
        nm = stack.pop()
        for r in refs.get(nm, ()):
            if r in defs and r not in live:
                live.add(r)
                stack.append(r)
    dead = []
    for node in tree.body:
        names = top_names(node)
        if names and not (names & live):
            dead.append((node.lineno - 1 - len(getattr(node, "decorator_list", [])), node.end_lineno))
    if not dead:
        break
    src = drop_lines(src, dead)
# imports nothing uses any more
for _round in range(3):
    tree = ast.parse(src)
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    edits = []
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)) or node.lineno != node.end_lineno \
                and isinstance(node, ast.Import):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            keep = [a for a in node.names if (a.asname or a.name).split(".")[0] in used
                    or (isinstance(node, ast.Import) and "." in a.name)]
            if len(keep) == len(node.names):
                continue
            if keep:
                node.names = keep
                edits.append((node.lineno - 1, node.end_lineno, ast.unparse(node)))
            else:
                edits.append((node.lineno - 1, node.end_lineno, None))
    if not edits:
        break
    lines = src.split("\n")
    for a, b2, text in sorted(edits, reverse=True):
        lines[a:b2] = [text] if text else []
    src = "\n".join(lines)

# comment blocks about removed features (section headers, notes) go too
_forbid = re.compile(r"(?<![A-Za-z])(%s)(?![A-Za-z])" % "|".join(re.escape(w) for w in FORBIDDEN if w != "Pro"),
                     re.I)
lines, out_lines, i = src.split("\n"), [], 0
while i < len(lines):
    if lines[i].lstrip().startswith("#"):
        j = i
        while j < len(lines) and lines[j].lstrip().startswith("#"):
            j += 1
        block = lines[i:j]
        if not any(_forbid.search(l) or re.search(r"(?<![A-Za-z])Pro(?![A-Za-z])", l) for l in block):
            out_lines += block
        i = j
        continue
    out_lines.append(lines[i])
    i += 1
src = "\n".join(out_lines)
src = re.sub(r"\n{4,}", "\n\n\n", src)

# ---- 4. checks ------------------------------------------------------------------
compile(src, "FilterMakerLite/__init__.py", "exec")
tree = ast.parse(src)
defined = set(dir(builtins))
for n in ast.walk(tree):
    if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
        defined.add(n.name)
    elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
        defined.add(n.id)
    elif isinstance(n, ast.arg):
        defined.add(n.arg)
    elif isinstance(n, (ast.Import, ast.ImportFrom)):
        for a in n.names:
            defined.add((a.asname or a.name).split(".")[0])
    elif isinstance(n, ast.ExceptHandler) and n.name:
        defined.add(n.name)
undefined = sorted({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
                    and isinstance(n.ctx, ast.Load) and n.id not in defined})
problems = []
if undefined:
    problems.append("undefined names: " + ", ".join(undefined))
pro_src = open(SRC, encoding="utf-8").read()
all_props = set()
for node in ast.parse(pro_src).body:
    if isinstance(node, ast.ClassDef) and node.name == "FilterProps":
        all_props = {st.target.id for st in node.body if isinstance(st, ast.AnnAssign)}
gone = all_props - LITE_PROPS
for n in ast.walk(tree):
    if isinstance(n, ast.Attribute) and n.attr in gone:
        problems.append("line %d uses removed property .%s" % (n.lineno, n.attr))
    if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in gone:
        problems.append("line %d names removed property %r" % (n.lineno, n.value))
idnames = set(re.findall(r'bl_idname = "(filter\.[a-z_]+)"', src))
for m in re.finditer(r'"(filter\.[a-z_]+)"', src):
    if m.group(1) not in idnames:
        problems.append("operator %s is used but not defined" % m.group(1))
text = src
for ok in ALLOWED_CONTEXT:
    text = text.replace(ok, "")
for w in FORBIDDEN:
    for m in re.finditer(r"(?<![A-Za-z])%s(?![A-Za-z])" % re.escape(w), text, re.I if w != "Pro" else 0):
        ln = text.count("\n", 0, m.start()) + 1
        problems.append("line %d contains %r: %s" % (ln, w, text.split("\n")[ln - 1].strip()[:100]))
if problems:
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "__init__.REJECTED.py"), "w", encoding="utf-8") as f:
        f.write(src)
    fail("%d problem(s):\n  " % len(problems) + "\n  ".join(problems[:200]))

# ---- write the package ------------------------------------------------------------
os.makedirs(OUT, exist_ok=True)
rej = os.path.join(OUT, "__init__.REJECTED.py")
if os.path.exists(rej):
    os.remove(rej)
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
