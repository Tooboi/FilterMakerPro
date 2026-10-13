"""Showcase renders for the Filter Maker Pro README.

Run from the repository root:
    blender -b --factory-startup --python tests/render_showcase.py
Images go to tests/results/showcase/ (transparent PNGs). Nothing is installed: the add-on is
loaded straight from the repository for this session only.
Renders use EEVEE with a transparent background so they can be placed on any card colour.
"""
import bpy
import bmesh
import importlib
import json
import math
import os
import sys
import time
import traceback
from mathutils import Vector

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
OUT = os.path.join(HERE, "results", "showcase")
os.makedirs(OUT, exist_ok=True)
_log = open(os.path.join(OUT, "showcase_log.txt"), "w", encoding="utf-8")
META = {"blender": bpy.app.version_string, "shots": {}}


def log(*a):
    msg = " ".join(str(x) for x in a)
    print(msg)
    _log.write(msg + "\n")
    _log.flush()


import importlib.util
_spec = importlib.util.spec_from_file_location("FilterMakerPro", os.path.join(REPO, "__init__.py"),
                                               submodule_search_locations=[REPO])
fm = importlib.util.module_from_spec(_spec)
sys.modules["FilterMakerPro"] = fm
_spec.loader.exec_module(fm)
fm.register()
log("Blender", bpy.app.version_string, "| add-on", fm.bl_info.get("version"))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def safe(obj, attr, value):
    try:
        setattr(obj, attr, value)
        return True
    except Exception:
        return False


def clear_scene():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)
    for me in list(bpy.data.meshes):
        if me.users == 0:
            bpy.data.meshes.remove(me)
    bpy.context.scene.cursor.location = (0, 0, 0)


def set_active(ob):
    for o in list(bpy.context.scene.objects):
        if o is not None:
            o.select_set(False)
    ob.select_set(True)
    bpy.context.view_layer.objects.active = ob


def make_mesh(name, verts, faces):
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.update()
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    return ob


def faces_filter(src_ob, select=None):
    set_active(src_ob)
    bpy.ops.object.mode_set(mode='EDIT')
    bm = bmesh.from_edit_mesh(src_ob.data)
    for f in bm.faces:
        f.select = select(f) if select else True
    bmesh.update_edit_mesh(src_ob.data)
    bpy.ops.filter.from_selection(source='FACE')
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    src_ob.hide_render = True
    src_ob.hide_set(True)
    return bpy.context.active_object


def configure(ob, **values):
    """Set several filter settings with one regenerate at the end."""
    p = ob.filter_maker
    p.live = False
    for k, v in values.items():
        setattr(p, k, v)
    p.live = True
    fm.regenerate(ob)
    if p.last_error:
        log("  filter error:", p.last_error)
    return p


def principled(mat, color, rough=0.6, metal=0.0, extra=None):
    if mat is None:
        return
    mat.diffuse_color = color                        # Workbench / viewport colour
    safe(mat, "use_nodes", True)
    nt = mat.node_tree
    if nt is None:
        return
    bsdf = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if bsdf is None:
        nt.nodes.clear()
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
        out = nt.nodes.new("ShaderNodeOutputMaterial")
        nt.links.new(bsdf.outputs[0], out.inputs[0])
    vals = {"Base Color": color, "Roughness": rough, "Metallic": metal}
    vals.update(extra or {})
    for k, v in vals.items():
        if k in bsdf.inputs:
            try:
                bsdf.inputs[k].default_value = v
            except Exception:
                pass


def style_materials(p, frame=(0.045, 0.047, 0.05, 1.0), paper=(0.86, 0.83, 0.74, 1.0)):
    principled(p.filter_material, paper, 0.85, 0.0, {"Sheen Weight": 0.3})
    principled(p.border_material, frame, 0.78, 0.0, {"Specular IOR Level": 0.2, "Coat Weight": 0.0})
    principled(p.hair_material, (0.82, 0.8, 0.76, 1.0), 0.55)
    principled(p.dust_material, (0.36, 0.31, 0.26, 1.0), 0.9)


def mat_report(p):
    out = {}
    for k in ("filter_material", "border_material"):
        m = getattr(p, k)
        if m is not None and m.node_tree:
            b = next((n for n in m.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
            out[k] = (m.name, tuple(round(v, 2) for v in b.inputs["Base Color"].default_value) if b else None)
    return out


def bounds(objs):
    lo = Vector((1e9, 1e9, 1e9))
    hi = Vector((-1e9, -1e9, -1e9))
    for ob in objs:
        if ob is None:
            continue
        for c in ob.bound_box:
            w = ob.matrix_world @ Vector(c)
            lo = Vector((min(lo.x, w.x), min(lo.y, w.y), min(lo.z, w.z)))
            hi = Vector((max(hi.x, w.x), max(hi.y, w.y), max(hi.z, w.z)))
    return lo, hi


def filter_objs(ob):
    p = ob.filter_maker
    return [ob] + [o for o in (p.border_obj,) if o is not None]


ENGINE = None


def pick_engine(scene):
    global ENGINE
    if ENGINE:
        scene.render.engine = ENGINE
        return ENGINE
    for eng in ('BLENDER_EEVEE', 'BLENDER_EEVEE_NEXT', 'CYCLES'):
        if safe(scene.render, "engine", eng):
            ENGINE = eng
            break
    log("render engine:", ENGINE)
    return ENGINE


def setup_world_and_lights(center, radius):
    scene = bpy.context.scene
    world = scene.world or bpy.data.worlds.new("FMShowcaseWorld")
    scene.world = world
    safe(world, "use_nodes", True)
    if world.node_tree:
        bg = next((n for n in world.node_tree.nodes if n.type == 'BACKGROUND'), None)
        if bg:
            bg.inputs["Color"].default_value = (0.32, 0.34, 0.38, 1.0)
            bg.inputs["Strength"].default_value = 0.35
    for ob in [o for o in bpy.data.objects if o.name.startswith("FMLight")]:
        bpy.data.objects.remove(ob, do_unlink=True)

    def area(name, direction, energy, size, color=(1, 1, 1)):
        ld = bpy.data.lights.get(name) or bpy.data.lights.new(name, 'AREA')
        ld.energy = energy * (radius * 4.0) ** 2
        ld.size = size * radius
        ld.color = color
        lo = bpy.data.objects.new(name, ld)
        scene.collection.objects.link(lo)
        d = Vector(direction).normalized()
        lo.location = center + d * radius * 4.0
        lo.rotation_euler = (-d).to_track_quat('-Z', 'Y').to_euler()

    area("FMLightKey", (1.2, -1.0, 1.4), 28, 2.0, (1.0, 0.96, 0.9))
    area("FMLightFill", (-1.4, -0.8, 0.4), 7, 3.0, (0.85, 0.9, 1.0))
    area("FMLightRim", (-0.3, 1.5, 0.9), 22, 1.5, (1.0, 1.0, 1.0))


def render(name, objs, direction=(1.0, -1.4, 0.9), lens=50, res=(1400, 900), close=1.0,
           target=None, workbench=False, transparent=True, samples=64):
    scene = bpy.context.scene
    bpy.context.view_layer.update()          # fresh bound boxes for objects made via bpy.data
    lo, hi = bounds(objs)
    META.setdefault("bounds", {})[name] = [list(map(lambda v: round(v, 3), lo)), list(map(lambda v: round(v, 3), hi))]
    center = (lo + hi) * 0.5 if target is None else Vector(target)
    radius = max((hi - lo).length * 0.5, 1e-3)
    cam_data = bpy.data.cameras.get("FMCam") or bpy.data.cameras.new("FMCam")
    cam_data.lens = lens
    cam_data.clip_start = radius * 0.002
    cam_data.clip_end = radius * 100
    cam = bpy.data.objects.get("FMCam")
    if cam is None:
        cam = bpy.data.objects.new("FMCam", cam_data)
        scene.collection.objects.link(cam)
    d = Vector(direction).normalized()
    aspect = res[0] / res[1]
    cam_data.sensor_fit = 'HORIZONTAL'
    th = math.tan(cam_data.angle_x * 0.5)
    tv = th / aspect
    up = Vector((0, 0, 1))
    right = d.cross(up)
    if right.length < 1e-6:
        right = Vector((1, 0, 0))
    right.normalize()
    upv = right.cross(d).normalized()
    m = 0.86 / close
    dist = 0.0
    corners = []
    for ob in objs:
        if ob is None:
            continue
        corners += [ob.matrix_world @ Vector(c) for c in ob.bound_box]
    for c in corners:
        p = c - center
        x, y, z = p.dot(right), p.dot(upv), p.dot(d)
        dist = max(dist, z + abs(x) / (th * m), z + abs(y) / (tv * m))
    if target is not None:
        dist = radius * 7.9 * close
    cam.location = center + d * dist
    cam.rotation_euler = (-d).to_track_quat('-Z', 'Y').to_euler()
    scene.camera = cam
    setup_world_and_lights(center, radius)

    if workbench:
        scene.render.engine = 'BLENDER_WORKBENCH'
        sh = scene.display.shading
        sh.light = 'STUDIO'
        sh.color_type = 'MATERIAL'
        safe(sh, "show_cavity", True)
        safe(sh, "cavity_type", 'BOTH')
        safe(sh, "show_specular_highlight", True)
        safe(scene.display, "render_aa", '16')
    else:
        eng = pick_engine(scene)
        if eng == 'CYCLES':
            scene.cycles.samples = max(32, samples)
            safe(scene.cycles, "use_denoising", True)
            safe(scene.cycles, "device", 'GPU')
        else:
            ee = scene.eevee
            safe(ee, "taa_render_samples", samples)
            safe(ee, "use_raytracing", True)
            safe(ee, "use_gtao", True)
            safe(ee, "use_shadows", True)
            safe(ee, "shadow_ray_count", 2)
            safe(ee, "shadow_step_count", 8)
    vs = scene.view_settings
    safe(vs, "view_transform", 'AgX')
    safe(vs, "look", 'AgX - Medium High Contrast')
    scene.render.resolution_x, scene.render.resolution_y = res
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = transparent
    scene.render.image_settings.file_format = 'PNG'
    safe(scene.render.image_settings, "color_mode", 'RGBA' if transparent else 'RGB')
    scene.render.filepath = os.path.join(OUT, name + ".png")
    t0 = time.time()
    try:
        bpy.ops.render.render(write_still=True)
        META["shots"][name] = {"seconds": round(time.time() - t0, 1), "engine": scene.render.engine}
        log("  rendered %s.png in %.1fs" % (name, time.time() - t0))
    except Exception:
        log("  render failed:", traceback.format_exc())


def timed_regen(ob):
    t0 = time.perf_counter()
    fm.regenerate(ob)
    return round((time.perf_counter() - t0) * 1000.0)


def mesh_info(ob):
    me = ob.data
    q = sum(1 for p in me.polygons if len(p.vertices) == 4)
    return {"verts": len(me.vertices), "faces": len(me.polygons),
            "quads_pct": round(100.0 * q / max(1, len(me.polygons)), 1)}


# ---------------------------------------------------------------------------
# shots
# ---------------------------------------------------------------------------
def shot_hero():
    clear_scene()
    src = make_mesh("RefPanel", [(0, 0, 0), (1, 0, 0), (1, 0, 0.62), (0, 0, 0.62)], [(0, 1, 2, 3)])
    ob = faces_filter(src)
    fm.apply_preset(ob, "B:Dusty Used")
    p = configure(ob, use_border=True, border_profile='LIP', border_width=0.035,
                  border_bevel=0.008, border_front=0.012, border_back=0.012,
                  use_thickness=True, thickness=0.0012, hair_density=1400.0, dust_density=6000.0)
    style_materials(p)
    META["hero_mesh"] = mesh_info(ob)
    META["hero_materials"] = mat_report(p)
    META["hero_slots"] = [ms.material.name if ms.material else None for ms in ob.material_slots] + \
        ([ms.material.name if ms.material else None for ms in p.border_obj.material_slots] if p.border_obj else [])
    META["hero_regen_ms"] = timed_regen(ob)
    objs = filter_objs(ob)
    render("hero", objs, direction=(0.95, -1.55, 0.42), lens=55, res=(1800, 1000), close=0.98)
    render("macro", [ob], direction=(0.35, -1.6, 0.32), lens=85, res=(1400, 900), close=0.13,
           target=(0.06, 0.0, 0.08), samples=96)
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(OUT, "showcase_hero.blend"), check_existing=False)


def shot_cylinder():
    clear_scene()
    bpy.ops.curve.primitive_bezier_circle_add(radius=0.3, location=(0, 0, 0))
    circ = bpy.context.active_object
    bpy.ops.filter.from_selection(source='CURVE')
    ob = bpy.context.active_object
    p = configure(ob, height=0.55, pleat_density=48.0, pleat_depth=0.03, use_border=True,
                  border_profile='ROUND', border_width=0.03, border_front=0.01, border_back=0.01,
                  noise_strength=0.0015)
    style_materials(p, frame=(0.07, 0.07, 0.075, 1.0))
    circ.hide_render = True
    META["cylinder_mesh"] = mesh_info(ob)
    render("cylinder", filter_objs(ob), direction=(0.9, -1.4, 0.95), lens=60, res=(1200, 900))


def shot_box():
    clear_scene()
    bpy.ops.mesh.primitive_cube_add(size=0.6)
    cube = bpy.context.active_object
    ob = faces_filter(cube, select=lambda f: abs(f.normal.z) < 0.5)
    p = configure(ob, use_border=True, border_profile='CHAMFER', border_width=0.025,
                  border_bevel=0.008, pleat_density=60.0)
    style_materials(p, frame=(0.012, 0.02, 0.035, 1.0))
    META["box_mesh"] = mesh_info(ob)
    render("box_wrap", filter_objs(ob), direction=(1.0, -1.25, 0.8), lens=60, res=(1200, 900))


def shot_topology():
    """Clean quad grid: the filter in light grey with its edges drawn over it (Workbench)."""
    clear_scene()
    verts = [(0, 0, 0), (0.5, 0, 0), (0.5, 0.5, 0), (0, 0.5, 0), (0.5, 0, 0.35), (0.5, 0.5, 0.35)]
    src = make_mesh("RefL", verts, [(0, 1, 2, 3), (1, 4, 5, 2)])
    ob = faces_filter(src)
    p = configure(ob, pleat_density=30.0, pleat_depth=0.02, noise_strength=0.0, detail=24.0)
    p.filter_material.diffuse_color = (0.78, 0.78, 0.8, 1.0)
    META["topology_mesh"] = mesh_info(ob)
    dg = bpy.context.evaluated_depsgraph_get()
    wme = bpy.data.meshes.new_from_object(ob.evaluated_get(dg))
    wire = bpy.data.objects.new("FMWire", wme)
    bpy.context.scene.collection.objects.link(wire)
    wire.matrix_world = ob.matrix_world.copy()
    wire.data.materials.clear()
    wm = bpy.data.materials.get("FMWireMat") or bpy.data.materials.new("FMWireMat")
    wm.diffuse_color = (0.07, 0.36, 0.95, 1.0)
    wire.data.materials.append(wm)
    mod = wire.modifiers.new("Wire", 'WIREFRAME')
    mod.thickness = 0.0016
    mod.use_replace = True
    mod.use_even_offset = True
    mod.offset = 0.0
    render("topology", [ob], direction=(1.1, -1.0, 0.85), lens=60, res=(1200, 900), workbench=True,
           transparent=True)


def shot_bevels():
    """Three short line filters: sharp, default and round pleat bevel."""
    clear_scene()
    objs = []
    for i, (ridge, cuts) in enumerate(((0.0, 1), (0.4, 3), (0.9, 6))):
        bpy.context.scene.cursor.location = (i * 0.36, 0.0, 0.0)
        bpy.ops.filter.new_line(length=0.28)
        ob = bpy.context.active_object
        p = configure(ob, height=0.14, pleat_density=36.0, pleat_depth=0.022, bevel_ridge=ridge,
                      bevel_valley=ridge, bevel_cuts=cuts, noise_strength=0.0,
                      use_thickness=True, thickness=0.0015, smooth=True)
        style_materials(p)
        if p.source_obj is not None:
            p.source_obj.hide_render = True
        objs.append(ob)
    render("bevels", objs, direction=(0.15, -1.0, 0.9), lens=70, res=(1500, 650))


def shot_borders():
    """The four border profiles on small panels, seen at their corner."""
    clear_scene()
    objs = []
    for i, prof in enumerate(('FLAT', 'CHAMFER', 'ROUND', 'LIP')):
        x = i * 0.42
        src = make_mesh("RefB%d" % i, [(x, 0, 0), (x + 0.32, 0, 0), (x + 0.32, 0.32, 0), (x, 0.32, 0)],
                        [(0, 1, 2, 3)])
        ob = faces_filter(src)
        p = configure(ob, pleat_density=60.0, pleat_depth=0.012, use_border=True, border_profile=prof,
                      border_width=0.035, border_bevel=0.012, border_front=0.014, border_back=0.006,
                      noise_strength=0.0, border_cuts=5)
        style_materials(p, frame=(0.08, 0.09, 0.1, 1.0))
        objs += filter_objs(ob)
    render("borders", objs, direction=(0.25, -1.0, 0.85), lens=60, res=(1600, 600))


def shot_presets():
    clear_scene()
    objs = []
    for i, name in enumerate(("Clean (no wear)", "HEPA Fine Pleat", "Coarse Furnace", "Dusty Used")):
        x = i * 0.44
        src = make_mesh("RefP%d" % i, [(x, 0, 0), (x + 0.34, 0, 0), (x + 0.34, 0, 0.46), (x, 0, 0.46)],
                        [(0, 1, 2, 3)])
        ob = faces_filter(src)
        fm.apply_preset(ob, "B:" + name)
        p = configure(ob, use_border=True, border_profile='FLAT', border_width=0.02)
        style_materials(p)
        objs += filter_objs(ob)
    render("presets", objs, direction=(0.3, -1.5, 0.3), lens=60, res=(1600, 620))


def label_layout(name, xs):
    META.setdefault("labels", {})[name] = xs


def shot_noise():
    """Wear: off, soft clouds, hard voronoi; grazing light shows the displacement."""
    clear_scene()
    objs = []
    looks = (dict(noise_strength=0.0),
             dict(noise_strength=0.0035, noise_scale=0.08, noise_depth=2, noise_type='SOFT',
                  noise_basis='BLENDER'),
             dict(noise_strength=0.009, noise_scale=0.04, noise_depth=3, noise_type='HARD',
                  noise_basis='VORONOI_F1'))
    for i, look in enumerate(looks):
        x = i * 0.46
        src = make_mesh("RefN%d" % i, [(x, 0, 0), (x + 0.36, 0, 0), (x + 0.36, 0, 0.48), (x, 0, 0.48)],
                        [(0, 1, 2, 3)])
        ob = faces_filter(src)
        p = configure(ob, pleat_density=40.0, pleat_depth=0.014, use_border=True, border_profile='FLAT',
                      border_width=0.018, use_hair=False, use_dust=False, smooth=True, **look)
        style_materials(p)
        objs += filter_objs(ob)
    render("noise", objs, direction=(0.55, -1.5, 0.25), lens=60, res=(1600, 640))


def shot_angle():
    """Pleat Angle 0, 30 and 60 degrees on the same square face."""
    clear_scene()
    objs = []
    for i, deg in enumerate((0, 30, 60)):
        x = i * 0.46
        src = make_mesh("RefA%d" % i, [(x, 0, 0), (x + 0.36, 0, 0), (x + 0.36, 0.36, 0), (x, 0.36, 0)],
                        [(0, 1, 2, 3)])
        ob = faces_filter(src)
        p = configure(ob, pleat_density=34.0, pleat_depth=0.012, pleat_angle=math.radians(deg),
                      use_border=True, border_profile='CHAMFER', border_width=0.02, border_bevel=0.006,
                      noise_strength=0.0, use_hair=False, use_dust=False)
        style_materials(p)
        objs += filter_objs(ob)
    render("angle", objs, direction=(0.2, -0.75, 1.0), lens=60, res=(1600, 640))


def _guide_from_points(name, pts, cyclic=False, r=0.0035):
    cu = bpy.data.curves.new(name, 'CURVE')
    cu.dimensions = '3D'
    sp = cu.splines.new('POLY')
    sp.points.add(len(pts) - 1)
    for pt, co in zip(sp.points, pts):
        pt.co = (co[0], co[1], co[2], 1.0)
    sp.use_cyclic_u = cyclic
    cu.bevel_depth = r
    cu.bevel_resolution = 3
    ob = bpy.data.objects.new(name, cu)
    bpy.context.scene.collection.objects.link(ob)
    ob.data.materials.append(_guide_mat())
    return ob


def _guide_mat():
    m = bpy.data.materials.get("FMGuide") or bpy.data.materials.new("FMGuide")
    principled(m, (1.0, 0.42, 0.05, 1.0), 0.5, 0.0, {"Emission Color": (1.0, 0.42, 0.05, 1.0),
                                                     "Emission Strength": 2.0})
    return m


def shot_sources():
    """The four ways to start: New Filter Line, a curve, mesh edges, mesh faces.
    Each source is drawn as an orange guide under its filter."""
    clear_scene()
    objs, guides = [], []
    gap = 0.75
    # 1. New Filter Line
    bpy.context.scene.cursor.location = (0.0, 0.0, 0.0)
    bpy.ops.filter.new_line(length=0.5)
    ob = bpy.context.active_object
    configure(ob, height=0.3, pleat_density=40.0, pleat_depth=0.014, use_border=True, border_width=0.015)
    objs.append(ob)
    guides.append(_guide_from_points("G1", [(-0.25, 0, 0), (0.25, 0, 0)]))
    # 2. Curve (open S-shaped Bezier)
    cu = bpy.data.curves.new("SCurve", 'CURVE')
    cu.dimensions = '3D'
    sp = cu.splines.new('BEZIER')
    sp.bezier_points.add(2)
    for bp, co in zip(sp.bezier_points, ((-0.25, -0.1, 0), (0.0, 0.05, 0), (0.25, -0.05, 0))):
        bp.co = co
        bp.handle_left_type = bp.handle_right_type = 'AUTO'
    cob = bpy.data.objects.new("SCurve", cu)
    cob.location = (gap, 0.0, 0.0)
    bpy.context.scene.collection.objects.link(cob)
    set_active(cob)
    bpy.ops.filter.from_selection(source='CURVE')
    ob = bpy.context.active_object
    configure(ob, height=0.3, pleat_density=40.0, pleat_depth=0.014, use_border=True, border_width=0.015)
    objs.append(ob)
    gcu = cu.copy()
    gcu.bevel_depth = 0.0035
    gcu.materials.append(_guide_mat())
    g = bpy.data.objects.new("G2", gcu)
    g.location = cob.location
    bpy.context.scene.collection.objects.link(g)
    guides.append(g)
    # 3. Mesh edges (a bent polyline)
    ex = 2 * gap
    epts = [(ex - 0.25, 0.08, 0), (ex - 0.08, -0.08, 0), (ex + 0.08, 0.06, 0), (ex + 0.25, -0.06, 0)]
    me = bpy.data.meshes.new("RefEdges")
    me.from_pydata(epts, [(0, 1), (1, 2), (2, 3)], [])
    eob = bpy.data.objects.new("RefEdges", me)
    bpy.context.scene.collection.objects.link(eob)
    set_active(eob)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.filter.from_selection(source='CURVE')
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    ob = bpy.context.active_object
    configure(ob, height=0.3, pleat_density=40.0, pleat_depth=0.014, use_border=True, border_width=0.015)
    eob.hide_render = True
    objs.append(ob)
    guides.append(_guide_from_points("G3", epts))
    # 4. Faces (an L of two faces)
    fx = 3 * gap - 0.2
    verts = [(fx, -0.15, 0), (fx + 0.3, -0.15, 0), (fx + 0.3, 0.15, 0), (fx, 0.15, 0),
             (fx + 0.3, -0.15, 0.3), (fx + 0.3, 0.15, 0.3)]
    src = make_mesh("RefL", verts, [(0, 1, 2, 3), (1, 4, 5, 2)])
    ob = faces_filter(src)
    configure(ob, pleat_density=40.0, pleat_depth=0.012, use_border=True, border_width=0.015)
    objs.append(ob)
    guides.append(_guide_from_points("G4a", [verts[0], verts[1], verts[4], verts[5], verts[2], verts[3]],
                                     cyclic=True))
    guides.append(_guide_from_points("G4b", [verts[1], verts[2]]))
    for o in objs:
        style_materials(o.filter_maker)
    allobjs = [o for ob in objs for o in filter_objs(ob)] + guides
    render("sources", allobjs, direction=(0.0, -1.35, 0.85), lens=70, res=(1800, 620))
    label_layout("sources", [0.0, gap, 2 * gap, 3 * gap - 0.05])


for name, fn in (("hero", shot_hero), ("cylinder", shot_cylinder), ("box", shot_box),
                 ("topology", shot_topology), ("bevels", shot_bevels), ("borders", shot_borders),
                 ("presets", shot_presets), ("noise", shot_noise), ("angle", shot_angle),
                 ("sources", shot_sources)):
    log("\n=== " + name)
    try:
        fn()
    except Exception:
        log("FAILED", name, traceback.format_exc())

with open(os.path.join(OUT, "showcase_meta.json"), "w", encoding="utf-8") as f:
    json.dump(META, f, indent=2, default=str)
log("\ndone")
_log.close()
