"""Headless test run for Filter Maker Pro.

Run from the repository root:
    blender -b --factory-startup --python tests/run_tests.py
Results go to tests/results/ (git-ignored). Nothing is installed: the add-on is loaded
straight from the repository for this one session only.
Set FM_COMPARE=/path/to/older/__init__.py to benchmark against an older build.
The process exits with code 1 if any test fails (use --python-exit-code 1 in CI).
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
OUT = os.path.join(HERE, "results")
os.makedirs(OUT, exist_ok=True)
LOG_PATH = os.path.join(OUT, "test_log.txt")
_log = open(LOG_PATH, "w", encoding="utf-8")
SUMMARY = {"blender": bpy.app.version_string, "tests": []}


def log(*args):
    msg = " ".join(str(a) for a in args)
    print(msg)
    _log.write(msg + "\n")
    _log.flush()


def record(name, ok, **info):
    SUMMARY["tests"].append(dict(name=name, ok=bool(ok), **info))
    log(("PASS " if ok else "FAIL ") + name + "  " + json.dumps(info, default=str))


log("Blender", bpy.app.version_string, "| Python", sys.version.split()[0])

# ---------------------------------------------------------------------------
# load the add-on from this folder
# ---------------------------------------------------------------------------
try:
    import importlib.util
    _spec = importlib.util.spec_from_file_location("FilterMakerPro", os.path.join(REPO, "__init__.py"),
                                                   submodule_search_locations=[REPO])
    fm = importlib.util.module_from_spec(_spec)
    sys.modules["FilterMakerPro"] = fm
    _spec.loader.exec_module(fm)
    fm.register()
    record("register add-on", True, version=getattr(fm, "bl_info", {}).get("version"))
except Exception:
    record("register add-on", False, error=traceback.format_exc())
    _log.close()
    raise SystemExit(1)


def clear_scene():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)
    for me in list(bpy.data.meshes):
        if me.users == 0:
            bpy.data.meshes.remove(me)


def set_active(ob):
    for o in list(bpy.context.scene.objects):
        if o is not None:
            o.select_set(False)
    ob.select_set(True)
    bpy.context.view_layer.objects.active = ob


def mesh_stats(ob):
    me = ob.data
    sizes = {}
    for poly in me.polygons:
        sizes[len(poly.vertices)] = sizes.get(len(poly.vertices), 0) + 1
    bm = bmesh.new()
    bm.from_mesh(me)
    nonmanifold = sum(1 for e in bm.edges if len(e.link_faces) > 2)
    boundary = sum(1 for e in bm.edges if len(e.link_faces) == 1)
    loose = sum(1 for v in bm.verts if not v.link_faces)
    # duplicate (unwelded) vertices = shading seams
    bm2 = bm.copy()
    before = len(bm2.verts)
    bmesh.ops.remove_doubles(bm2, verts=bm2.verts[:], dist=1e-6)
    dupes = before - len(bm2.verts)
    bm2.free()
    bm.free()
    total = max(1, len(me.polygons))
    return dict(verts=len(me.vertices), faces=len(me.polygons),
                quads_pct=round(100.0 * sizes.get(4, 0) / total, 1),
                tris=sizes.get(3, 0), ngons=sum(c for k, c in sizes.items() if k > 4),
                nonmanifold_edges=nonmanifold, boundary_edges=boundary,
                loose_verts=loose, duplicate_verts=dupes)


def count_instances(emitter):
    dg = bpy.context.evaluated_depsgraph_get()
    dg.update()
    n = 0
    names = {}
    for inst in dg.object_instances:
        if inst.is_instance and inst.parent is not None and inst.parent.original == emitter:
            n += 1
            key = inst.object.original.name.rsplit(" ", 2)[-2] if " " in inst.object.original.name else inst.object.name
            names[key] = names.get(key, 0) + 1
    return n, names


def bounds(objs):
    lo = Vector((1e9, 1e9, 1e9))
    hi = Vector((-1e9, -1e9, -1e9))
    for ob in objs:
        for c in ob.bound_box:
            w = ob.matrix_world @ Vector(c)
            lo = Vector((min(lo.x, w.x), min(lo.y, w.y), min(lo.z, w.z)))
            hi = Vector((max(hi.x, w.x), max(hi.y, w.y), max(hi.z, w.z)))
    return lo, hi


def render(name, objs, direction=(1.0, -1.4, 0.9), lens=50, res=(900, 650), close=1.0):
    scene = bpy.context.scene
    lo, hi = bounds(objs)
    center = (lo + hi) * 0.5
    radius = max((hi - lo).length * 0.5, 1e-3)
    cam_data = bpy.data.cameras.get("FMTestCam") or bpy.data.cameras.new("FMTestCam")
    cam_data.lens = lens
    cam = bpy.data.objects.get("FMTestCam")
    if cam is None:
        cam = bpy.data.objects.new("FMTestCam", cam_data)
        scene.collection.objects.link(cam)
    d = Vector(direction).normalized()
    dist = radius / math.tan(cam_data.angle * 0.5) * 1.15 * close
    cam.location = center + d * dist
    cam.rotation_euler = (-d).to_track_quat('-Z', 'Y').to_euler()
    scene.camera = cam
    scene.render.engine = 'BLENDER_WORKBENCH'
    shading = scene.display.shading
    shading.light = 'STUDIO'
    shading.color_type = 'MATERIAL'
    try:
        shading.show_cavity = True
        shading.cavity_type = 'BOTH'
    except Exception:
        pass
    shading.show_specular_highlight = True
    scene.render.resolution_x, scene.render.resolution_y = res
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = 'PNG'
    scene.render.filepath = os.path.join(OUT, name + ".png")
    try:
        bpy.ops.render.render(write_still=True)
        log("  rendered", name + ".png")
    except Exception:
        log("  render failed:", traceback.format_exc())


def filter_report(name, ob, t0):
    p = ob.filter_maker
    info = mesh_stats(ob)
    info["seconds"] = round(time.time() - t0, 2)
    info["error"] = p.last_error
    record(name, not p.last_error and info["faces"] > 0, **info)
    return info


def run(name, fn):
    log("\n=== " + name)
    try:
        fn()
    except Exception:
        record(name, False, error=traceback.format_exc())


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def t_new_line():
    clear_scene()
    t0 = time.time()
    res = bpy.ops.filter.new_line()
    ob = bpy.context.active_object
    record("new_line operator", 'FINISHED' in res and ob is not None and ob.filter_maker.is_filter,
           active=ob.name if ob else None)
    filter_report("curve filter (new line)", ob, t0)
    src = ob.filter_maker.source_obj
    record("new line is an editable curve source", src is not None and src.type == 'CURVE',
           source=src.name if src else None)
    render("01_curve_line", [ob])


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
    t0 = time.time()
    bpy.ops.filter.from_selection(source='FACE')
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    src_ob.hide_render = True                      # keep the reference out of the renders
    src_ob.hide_set(True)
    return bpy.context.active_object, t0


def t_plane():
    clear_scene()
    src = make_mesh("RefPlane", [(0, 0, 0), (1, 0, 0), (1, 0.6, 0), (0, 0.6, 0)], [(0, 1, 2, 3)])
    ob, t0 = faces_filter(src)
    filter_report("faces: flat plane", ob, t0)
    render("02_plane", [ob])
    p = ob.filter_maker
    p.pleat_angle = math.radians(30)
    filter_report("faces: plane, pleat angle 30", ob, time.time())
    render("03_plane_angle30", [ob])


def t_lfold():
    clear_scene()
    verts = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (1, 0, 0.7), (1, 1, 0.7)]
    src = make_mesh("RefL", verts, [(0, 1, 2, 3), (1, 4, 5, 2)])
    ob, t0 = faces_filter(src)
    filter_report("faces: L fold (2 panels)", ob, t0)
    render("04_lfold", [ob], direction=(1.2, -1.0, 0.8))


def t_box_wrap():
    clear_scene()
    bpy.ops.mesh.primitive_cube_add(size=1.0)
    cube = bpy.context.active_object
    cube.name = "RefBox"
    ob, t0 = faces_filter(cube, select=lambda f: abs(f.normal.z) < 0.5)
    info = filter_report("faces: box sides wrap (4 panels)", ob, t0)
    record("box wrap closes (no open seam)", info["boundary_edges"] > 0 and info["nonmanifold_edges"] == 0,
           boundary_edges=info["boundary_edges"])
    render("05_box_wrap", [ob], direction=(1.0, -1.3, 0.7))


def t_full_featured():
    clear_scene()
    src = make_mesh("RefPanel", [(0, 0, 0), (1, 0, 0), (1, 0, 0.8), (0, 0, 0.8)], [(0, 1, 2, 3)])
    ob, t0 = faces_filter(src)
    p = ob.filter_maker
    p.live = False
    p.use_border = True
    p.use_hair = True
    p.use_dust = True
    p.use_thickness = True
    p.live = True
    fm.regenerate(ob)
    filter_report("faces: border + hair + dust + thickness", ob, t0)
    emitter = p.particles_obj
    record("particle emitter exists", emitter is not None, emitter=emitter.name if emitter else None)
    if emitter is not None:
        mod = emitter.modifiers.get(fm.SCATTER_MOD)
        record("scatter modifier on emitter", mod is not None and mod.node_group is not None,
               modifiers=[m.name for m in emitter.modifiers])
        group = mod.node_group if mod else None
        hcoll = group.nodes.get("FM Hair Collection") if group else None
        dcoll = group.nodes.get("FM Dust Collection") if group else None
        record("collections connected automatically",
               hcoll is not None and dcoll is not None
               and hcoll.inputs["Collection"].default_value == p.hair_lib
               and dcoll.inputs["Collection"].default_value == p.dust_lib,
               hair=getattr(hcoll.inputs["Collection"].default_value, "name", None) if hcoll else None,
               dust=getattr(dcoll.inputs["Collection"].default_value, "name", None) if dcoll else None)
        n, kinds = count_instances(emitter)
        record("hair + dust instances generated", n > 0, instances=n, estimate_hair=p.hair_count,
               estimate_dust=p.dust_count)
        hair_names = {o.name for o in (p.hair_lib.objects if p.hair_lib else [])}
        dust_names = {o.name for o in (p.dust_lib.objects if p.dust_lib else [])}
        record("instance collections built", len(hair_names) > 0 and len(dust_names) > 0,
               hair=len(hair_names), dust=len(dust_names))
        dg = bpy.context.evaluated_depsgraph_get()
        hair_pts, dust_pts, hair_scales, dust_scales, lie = [], [], [], [], []
        from mathutils.bvhtree import BVHTree
        _me = emitter.data
        _mw = emitter.matrix_world
        bvh = BVHTree.FromPolygons([_mw @ v.co for v in _me.vertices], [list(pl.vertices) for pl in _me.polygons])
        for inst in dg.object_instances:
            if not (inst.is_instance and inst.parent is not None and inst.parent.original == emitter):
                continue
            m = inst.matrix_world
            nm = inst.object.original.name
            if nm in hair_names:
                hair_pts.append(m.translation.copy())
                hair_scales.append(m.to_scale().x)
                if len(lie) < 3000:
                    loc, nrm, _idx, _d = bvh.find_nearest(m.translation)
                    if loc is not None:
                        x_axis = (m.to_3x3() @ Vector((1.0, 0.0, 0.0))).normalized()
                        lie.append(math.degrees(math.asin(min(1.0, abs(x_axis.dot(nrm.normalized()))))))
            elif nm in dust_names:
                dust_pts.append(m.translation.copy())
                dust_scales.append(m.to_scale().x)
        hk = {(round(v.x, 6), round(v.y, 6), round(v.z, 6)) for v in hair_pts}
        shared = sum(1 for v in dust_pts if (round(v.x, 6), round(v.y, 6), round(v.z, 6)) in hk)
        record("hair and dust use separate spawn points", shared == 0, hair=len(hair_pts),
               dust=len(dust_pts), shared=shared)
        if hair_scales:
            record("hair size matches setting", abs(sum(hair_scales) / len(hair_scales) - p.hair_length) < p.hair_length * 0.6,
                   mean=round(sum(hair_scales) / len(hair_scales), 5), setting=round(p.hair_length, 5))
        if dust_scales:
            record("dust size matches setting", abs(sum(dust_scales) / len(dust_scales) - p.dust_size) < p.dust_size * 0.7,
                   mean=round(sum(dust_scales) / len(dust_scales), 5), setting=round(p.dust_size, 5))
        if lie:
            lie.sort()
            record("hair lies on the surface", lie[len(lie) // 2] < math.degrees(p.hair_lift) + 5,
                   median_lift_deg=round(lie[len(lie) // 2], 1), max_lift_deg=round(lie[-1], 1),
                   lift_setting_deg=round(math.degrees(p.hair_lift), 1))
        # nothing inside the border frame: in-plane distance from the panel outline
        W = p.border_width
        def outline_dist(v):                       # panel is x 0..1, z 0..0.8 in the XZ plane
            return min(v.x, 1.0 - v.x, v.z, 0.8 - v.z)
        inside = [outline_dist(v) for v in hair_pts + dust_pts]
        if inside:
            record("no hair / dust under the border", min(inside) > W,
                   closest=round(min(inside), 5), border_width=round(W, 5),
                   falloff=round(p.hair_border_falloff, 5),
                   n_inside=sum(1 for d in inside if d < W))
            # diagnostics for anything that is too close
            me = emitter.data
            attr = me.attributes.get("fm_border_dist")
            vals = [0.0] * len(me.vertices)
            if attr is not None:
                attr.data.foreach_get("value", vals)
                log("  emitter fm_border_dist: verts=%d min=%.4f max=%.4f below0=%d" % (
                    len(vals), min(vals), max(vals), sum(1 for x in vals if x < 0)))
                ev_dg = emitter.evaluated_get(dg)
                log("  evaluated emitter attributes:", [a.name for a in emitter.data.attributes])
            else:
                log("  emitter has NO fm_border_dist attribute; attributes:", [a.name for a in me.attributes])
            grp = mod.node_group if mod else None
            if grp is not None:
                log("  scatter group:", grp.name, "version", grp.get("fm_version"),
                    {n.name: round(n.outputs[0].default_value, 5) for n in grp.nodes
                     if n.name in ("FM Hair Pad", "FM Dust Pad", "FM Falloff")})
                attr_nodes = [n for n in grp.nodes if n.bl_idname == 'GeometryNodeInputNamedAttribute']
                log("  named attribute nodes:", [(n.inputs[0].default_value, n.data_type) for n in attr_nodes])
            close = sorted(((outline_dist(v), v) for v in hair_pts + dust_pts), key=lambda x: x[0])[:6]
            from mathutils.kdtree import KDTree
            kd = KDTree(len(me.vertices))
            for i, vtx in enumerate(me.vertices):
                kd.insert(emitter.matrix_world @ vtx.co, i)
            kd.balance()
            for d, v in close:
                co, idx, dd = kd.find(v)
                log("  close instance at %s  outline dist %.5f  nearest emitter vert %d value %.4f (%.5f away)" % (
                    tuple(round(c, 4) for c in v), d, idx, vals[idx], dd))
        # library collections: brown and the last entry of the Scene Collection
        root = bpy.data.collections.get("Filter Maker Library")
        kids = bpy.context.scene.collection.children
        record("library collection brown and at the bottom",
               root is not None and kids[len(kids) - 1] == root and root.color_tag == 'COLOR_08'
               and all(c.color_tag == 'COLOR_08' for c in root.children),
               last=kids[len(kids) - 1].name if len(kids) else None,
               colors=[root.color_tag] + [c.color_tag for c in root.children] if root else None)
        # hair and dust viewport / falloff are independent
        if group is not None:
            p.hair_viewport = 40
            p.dust_border_falloff = p.dust_border_falloff * 3.0
            hv = group.nodes["FM Hair Viewport"].outputs[0].default_value
            dv = group.nodes["FM Dust Viewport"].outputs[0].default_value
            hf = group.nodes["FM Hair Falloff"].outputs[0].default_value
            df = group.nodes["FM Dust Falloff"].outputs[0].default_value
            record("hair and dust viewport / falloff independent",
                   abs(hv - 0.4) < 1e-6 and abs(dv - 1.0) < 1e-6 and abs(df - 3 * hf) < 1e-6 * max(1, df),
                   hair_viewport=hv, dust_viewport=dv, hair_falloff=hf, dust_falloff=df)
            p.hair_viewport = 100
            p.dust_border_falloff = p.dust_border_falloff / 3.0
        record("border shading defaults to auto smooth only",
               (not p.border_smooth) and p.border_auto_smooth,
               smooth=p.border_smooth, auto_smooth=p.border_auto_smooth)
        # live update: a slider change must reach the node tree
        before = group.nodes["FM Hair Density"].outputs[0].default_value if group else None
        p.hair_density = p.hair_density * 2.0
        after = group.nodes["FM Hair Density"].outputs[0].default_value if group else None
        record("slider changes reach the scatter nodes", before is not None and abs(after - 2 * before) < 1e-6 * max(1, after),
               before=before, after=after)
        p.hair_density = p.hair_density / 2.0
    border = p.border_obj
    record("border object exists", border is not None and len(border.data.polygons) > 0)
    objs = [ob] + ([border] if border else [])
    render("06_full_featured", objs, direction=(0.7, -1.6, 0.5))
    render("07_full_closeup", [ob], direction=(0.4, -1.6, 0.3), close=0.22)
    render("08_full_macro", [ob], direction=(0.25, -1.6, 0.15), close=0.08)
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(OUT, "test_scene.blend"), check_existing=False)


def t_presets():
    clear_scene()
    bpy.ops.filter.new_line()
    ob = bpy.context.active_object
    bad = []
    for name in fm.BUILTIN_PRESETS:
        ok = fm.apply_preset(ob, "B:" + name)
        if not ok or ob.filter_maker.last_error:
            bad.append((name, ob.filter_maker.last_error))
    record("all built-in presets apply", not bad, failures=bad)


def t_icons():
    # Background mode has no GPU previews, so icon ids can legitimately be 0 here.
    ids = {n: fm.art_icon(n) for n in fm.ICON_NAMES}
    log("INFO header icon ids (0 is normal without a window):", ids)


for name, fn in (("new line", t_new_line), ("plane", t_plane), ("L fold", t_lfold),
                 ("box wrap", t_box_wrap), ("full featured", t_full_featured),
                 ("presets", t_presets), ("icons", t_icons)):
    run(name, fn)

# ---------------------------------------------------------------------------
# benchmark: previous build vs this build on identical edits
# ---------------------------------------------------------------------------

def bench(mod, label):
    res = {}
    clear_scene()
    bpy.ops.mesh.primitive_cube_add(size=1.0)
    cube = bpy.context.active_object
    t0 = time.perf_counter()
    ob, _t = faces_filter(cube, select=lambda f: abs(f.normal.z) < 0.5)
    res["create box wrap"] = time.perf_counter() - t0
    p = ob.filter_maker
    p.live = False
    p.use_border = True
    p.use_hair = True
    p.use_dust = True
    p.live = True
    t0 = time.perf_counter()
    mod.regenerate(ob)
    res["enable border+hair+dust"] = time.perf_counter() - t0
    dg = bpy.context.evaluated_depsgraph_get()

    def timed(name, fn, reps=4):
        times = []
        for i in range(reps):
            t0 = time.perf_counter()
            fn(i)
            dg.update()
            times.append(time.perf_counter() - t0)
        times.sort()
        res[name] = times[len(times) // 2]
        if hasattr(mod, "LAST_TIMINGS"):
            res[name + " (stages)"] = dict(mod.LAST_TIMINGS)

    base = (p.noise_strength, p.hair_density, p.dust_size, p.pleat_density)
    timed("wear strength slider", lambda i: setattr(p, "noise_strength", base[0] * (1.1 + 0.1 * i)))
    timed("hair density slider", lambda i: setattr(p, "hair_density", base[1] * (1.1 + 0.1 * i)))
    timed("dust size slider", lambda i: setattr(p, "dust_size", base[2] * (1.1 + 0.1 * i)))
    timed("pleat density slider", lambda i: setattr(p, "pleat_density", base[3] * (1.02 + 0.02 * i)))
    timed("thickness toggle", lambda i: setattr(p, "use_thickness", i % 2 == 0))
    log("\nBENCH %s" % label)
    for k, v in res.items():
        log("  %-32s %s" % (k, ("%.0f ms" % (v * 1000)) if isinstance(v, float) else v))
    SUMMARY.setdefault("bench", {})[label] = {k: (round(v * 1000, 1) if isinstance(v, float) else v) for k, v in res.items()}


try:
    bench(fm, "new " + ".".join(str(x) for x in fm.bl_info["version"]))
    cmp_path = os.environ.get("FM_COMPARE")
    if cmp_path and os.path.isfile(cmp_path):
        fm.unregister()
        _cspec = importlib.util.spec_from_file_location("FilterMakerPro_old", cmp_path,
                                                        submodule_search_locations=[os.path.dirname(cmp_path)])
        old = importlib.util.module_from_spec(_cspec)
        sys.modules["FilterMakerPro_old"] = old
        _cspec.loader.exec_module(old)
        old.register()
        bench(old, "old " + ".".join(str(x) for x in old.bl_info["version"]))
        old.unregister()
        fm.register()
except Exception:
    log("benchmark failed:", traceback.format_exc())

passed = sum(1 for t in SUMMARY["tests"] if t["ok"])
log("\n%d / %d passed" % (passed, len(SUMMARY["tests"])))
with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as fh:
    json.dump(SUMMARY, fh, indent=2, default=str)
_log.close()

if passed < len(SUMMARY["tests"]):
    sys.exit(1)
