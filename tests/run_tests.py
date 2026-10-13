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
        group.nodes["FM Debug"].outputs[0].default_value = 1.0     # per-strand attributes on
        emitter.update_tag()
        n, kinds = count_instances(emitter)
        dgh = bpy.context.evaluated_depsgraph_get()
        dgh.update()
        ev = emitter.evaluated_get(dgh)
        ev_me = ev.to_mesh()
        hair_verts = len(ev_me.vertices)
        roots_attr = ev_me.attributes.get("fm_root")
        xax_attr = ev_me.attributes.get("fm_xaxis")
        scl_attr = ev_me.attributes.get("fm_scale")
        hair_roots, hair_x, hair_s = {}, {}, {}
        if roots_attr is not None:
            import array
            nv = len(ev_me.vertices)
            buf = [0.0] * (3 * nv)
            roots_attr.data.foreach_get("vector", buf)
            xb = [0.0] * (3 * nv)
            if xax_attr is not None:
                xax_attr.data.foreach_get("vector", xb)
            sb = [0.0] * (3 * nv)
            if scl_attr is not None:
                scl_attr.data.foreach_get("vector", sb)
            for i in range(0, 3 * nv, 3):
                k = (round(buf[i], 6), round(buf[i + 1], 6), round(buf[i + 2], 6))
                if k not in hair_roots:
                    hair_roots[k] = Vector(buf[i:i + 3])
                    hair_x[k] = Vector(xb[i:i + 3])
                    hair_s[k] = sb[i]
        record("hair + dust generated", n > 0 and hair_verts > 0 and len(hair_roots) > 0,
               dust_instances=n, hair_strands=len(hair_roots), hair_verts=hair_verts,
               estimate_hair=p.hair_count, estimate_dust=p.dust_count)
        # constant width: tube ring edges equal Width/2 (hexagon), whatever the length
        def ring_share(me, w):
            target = w * 0.5
            good = tot = 0
            lens = []
            for e in me.edges:
                d = (me.vertices[e.vertices[0]].co - me.vertices[e.vertices[1]].co).length
                tot += 1
                if len(lens) < 200000:
                    lens.append(d)
                if abs(d - target) < target * 0.02:
                    good += 1
            lens.sort()
            if lens:
                log("  hair edge lengths: target %.6f  p10 %.6f p25 %.6f p50 %.6f p75 %.6f" % (
                    target, lens[len(lens) // 10], lens[len(lens) // 4], lens[len(lens) // 2], lens[3 * len(lens) // 4]))
            return good / max(1, tot)
        share1 = ring_share(ev_me, p.hair_width)
        ev.to_mesh_clear()
        old_len = p.hair_length
        p.hair_length = old_len * 3.0
        dgh.update()
        ev = emitter.evaluated_get(dgh)
        me2 = ev.to_mesh()
        share2 = ring_share(me2, p.hair_width)
        ev.to_mesh_clear()
        p.hair_length = old_len
        dgh.update()
        record("hair width constant for any length", share1 > 0.15 and share2 > 0.15,
               share_len1=round(share1, 3), share_len3x=round(share2, 3), width=p.hair_width)
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
            if nm in dust_names:
                dust_pts.append(m.translation.copy())
                dust_scales.append(m.to_scale().x)
        mw = emitter.matrix_world
        for k, v in hair_roots.items():
            wv = mw @ v
            hair_pts.append(wv)
            hair_scales.append(hair_s[k])
            if len(lie) < 3000 and hair_x[k].length > 0.5:
                loc, nrm, _idx, _d = bvh.find_nearest(wv)
                if loc is not None:
                    x_axis = (mw.to_3x3() @ hair_x[k]).normalized()
                    lie.append(math.degrees(math.asin(min(1.0, abs(x_axis.dot(nrm.normalized()))))))
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


def t_shared_library():
    clear_scene()
    made = []
    for i in range(3):
        x = i * 1.5
        src = make_mesh("RefS%d" % i, [(x, 0, 0), (x + 1, 0, 0), (x + 1, 0, 0.6), (x, 0, 0.6)], [(0, 1, 2, 3)])
        ob, _t = faces_filter(src)
        p = ob.filter_maker
        p.live = False
        p.use_border = True
        p.use_hair = True
        p.use_dust = True
        p.hair_length = 0.01 * (i + 1)          # different lengths per filter
        p.live = True
        fm.regenerate(ob)
        made.append(ob)
    root = bpy.data.collections.get(fm.LIB_ROOT)
    kids = [c.name for c in root.children] if root else []
    record("one hair + one dust collection for all filters", sorted(kids) == sorted([fm.HAIR_LIB_NAME, fm.DUST_LIB_NAME]),
           collections=kids)
    hl = {o.filter_maker.hair_lib.name if o.filter_maker.hair_lib else None for o in made}
    dl = {o.filter_maker.dust_lib.name if o.filter_maker.dust_lib else None for o in made}
    record("all filters reference the same pieces", len(hl) == 1 and len(dl) == 1 and None not in hl | dl,
           hair=list(hl), dust=list(dl))
    # emitters point at the shared collections
    ok = True
    for o in made:
        g = o.filter_maker.particles_obj.modifiers.get(fm.SCATTER_MOD).node_group
        ok &= g.nodes["FM Hair Collection"].inputs["Collection"].default_value.name == fm.HAIR_LIB_NAME
        ok &= g.nodes["FM Dust Collection"].inputs["Collection"].default_value.name == fm.DUST_LIB_NAME
    record("scatter nodes use the shared collections", ok)
    # change a shared setting on filter 0 -> others follow, pieces rebuilt once
    a, b = made[0].filter_maker, made[1].filter_maker
    n_before = len(bpy.data.collections[fm.HAIR_LIB_NAME].objects)
    a.hair_variants = 5
    a.hair_curl = 1.1
    a.dust_variants = 4
    hc = bpy.data.collections[fm.HAIR_LIB_NAME]
    dc = bpy.data.collections[fm.DUST_LIB_NAME]
    record("shared settings propagate", b.hair_variants == 5 and abs(b.hair_curl - 1.1) < 1e-6
           and b.dust_variants == 4 and len(hc.objects) == 5 and len(dc.objects) == 4,
           b_variants=b.hair_variants, b_curl=round(b.hair_curl, 3), hair_pieces=len(hc.objects),
           dust_pieces=len(dc.objects), before=n_before)
    # width and material stay per filter
    a.hair_width = 0.0004
    record("hair width is per filter", abs(b.hair_width - 0.0004) > 1e-7 or b.hair_width != a.hair_width,
           a=a.hair_width, b=b.hair_width)
    # new filter adopts the shared look
    src = make_mesh("RefS9", [(0, 3, 0), (1, 3, 0), (1, 3, 0.6), (0, 3, 0.6)], [(0, 1, 2, 3)])
    ob, _t = faces_filter(src)
    p = ob.filter_maker
    p.live = False
    p.use_hair = True
    p.live = True
    fm.regenerate(ob)
    record("new filter adopts the shared pieces", p.hair_variants == 5 and abs(p.hair_curl - 1.1) < 1e-6
           and p.hair_lib == hc and len(hc.objects) == 5, variants=p.hair_variants)
    made.append(ob)
    # strand profile: constant, tapering only at the tip
    st = hc.objects[0].data.splines[0].bezier_points
    radii = [round(bp.radius, 3) for bp in st]
    n = len(radii)
    body = radii[int(n * 0.25): int(n * 0.75)]
    record("strands keep a constant profile and close to a point at both ends",
           all(abs(r - 1.0) < 1e-6 for r in body) and radii[0] < 1e-6 and radii[-1] < 1e-6
           and radii[1] < 1.0 and radii[-2] < 1.0, radii=radii)
    # deleting / disabling: collections survive while used, go away when unused
    set_active(made[0])
    bpy.ops.filter.delete()
    record("deleting one filter keeps the shared pieces", fm.HAIR_LIB_NAME in bpy.data.collections
           and fm.DUST_LIB_NAME in bpy.data.collections)
    for o in made[1:]:
        o.filter_maker.use_hair = False
        o.filter_maker.use_dust = False
    record("pieces removed when no filter uses them", fm.HAIR_LIB_NAME not in bpy.data.collections
           and fm.DUST_LIB_NAME not in bpy.data.collections and fm.LIB_ROOT not in bpy.data.collections,
           left=[c.name for c in bpy.data.collections])


def t_border_covers_pleats():
    """Every pleat vertex under the frame must be inside the frame volume: every profile
    on a flat panel, plus a box wrap and an L fold (frames bending around folds)."""
    from mathutils.bvhtree import BVHTree

    def check(label, ob, under):
        p = ob.filter_maker
        dg = bpy.context.evaluated_depsgraph_get()
        dg.update()
        fr = p.border_obj.evaluated_get(dg)
        fme = fr.to_mesh()
        mwf = p.border_obj.matrix_world
        bvh = BVHTree.FromPolygons([mwf @ v.co for v in fme.vertices], [list(pl.vertices) for pl in fme.polygons])
        fr.to_mesh_clear()
        d = Vector((0.137, 0.981, 0.113)).normalized()

        def inside(pt):
            n = 0
            o = pt.copy()
            for _i in range(64):
                hit, _n, _idx, dist = bvh.ray_cast(o, d)
                if hit is None:
                    break
                n += 1
                o = hit + d * 1e-6
            return n % 2 == 1
        ev = ob.evaluated_get(dg)
        me = ev.to_mesh()
        mw = ob.matrix_world
        bad, checked = [], 0
        for v in me.vertices:
            w = mw @ v.co
            if under(w):
                checked += 1
                if not inside(w):
                    bad.append((round(w.x, 4), round(w.y, 4), round(w.z, 4)))
        ev.to_mesh_clear()
        record("border covers every pleat end: " + label, checked > 0 and not bad, checked=checked,
               outside=len(bad), sample=bad[:5])

    def setup(ob, prof):
        p = ob.filter_maker
        p.live = False
        p.use_border = True
        p.border_profile = prof
        p.border_bevel = 0.02            # larger than Front Extend on purpose
        p.border_front = 0.005
        p.use_thickness = True
        p.thickness = 0.002
        p.noise_strength = 0.003
        p.live = True
        fm.regenerate(ob)
        return p

    for prof in ('FLAT', 'CHAMFER', 'ROUND', 'LIP'):
        clear_scene()
        src = make_mesh("RefB", [(0, 0, 0), (1, 0, 0), (1, 0, 0.8), (0, 0, 0.8)], [(0, 1, 2, 3)])
        ob, _t = faces_filter(src)
        p = setup(ob, prof)
        W = p.border_width
        check(prof + " panel", ob, lambda w: min(w.x, 1.0 - w.x, w.z, 0.8 - w.z) < W * 0.95)
    for prof in ('FLAT', 'LIP'):
        clear_scene()
        bpy.ops.mesh.primitive_cube_add(size=1.0)
        cube = bpy.context.active_object
        ob, _t = faces_filter(cube, select=lambda f: abs(f.normal.z) < 0.5)
        p = setup(ob, prof)
        W = p.border_width
        check(prof + " box wrap", ob, lambda w: min(w.z + 0.5, 0.5 - w.z) < W * 0.95)
    clear_scene()
    verts = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (1, 0, 0.7), (1, 1, 0.7)]
    src = make_mesh("RefL", verts, [(0, 1, 2, 3), (1, 4, 5, 2)])
    ob, _t = faces_filter(src)
    p = setup(ob, 'FLAT')
    W = p.border_width

    def under_l(w):
        if w.z < 0.05:                      # floor face: x 0..1, y 0..1
            dd = min(w.x, w.y, 1.0 - w.y)
        else:                               # wall face: z 0..0.7, y 0..1
            dd = min(0.7 - w.z, w.y, 1.0 - w.y)
        return dd < W * 0.95
    check("FLAT L fold", ob, under_l)

    # Curved selection with a wide frame (the case from BorderTest.blend): the outline is
    # densely cut near the corners, which used to fold the frame's inner edge back on itself.
    clear_scene()
    bpy.ops.mesh.primitive_cylinder_add(vertices=32, radius=1.0, depth=2.0)
    cyl = bpy.context.active_object
    ob, _t = faces_filter(cyl, select=lambda f: f.normal.y < -0.8 and abs(f.normal.z) < 0.5)
    p = ob.filter_maker
    p.live = False
    p.use_border = True
    p.border_profile = 'FLAT'
    p.border_width = 0.109
    p.border_front = p.border_back = 0.02
    p.pleat_depth = 0.05
    p.pleat_density = 27.5
    p.noise_strength = 0.003
    p.live = True
    fm.regenerate(ob)
    dg = bpy.context.evaluated_depsgraph_get()
    dg.update()
    bme = p.border_obj.data
    vs = [p.border_obj.matrix_world @ v.co for v in bme.vertices]
    m_ = 4
    st = [vs[i:i + m_] for i in range(0, len(vs) - m_ + 1, m_)]
    rev = 0
    n = len(st)
    for k in range(n):
        a, b = st[k], st[(k + 1) % n]
        base = b[0] - a[0]
        seg = b[1] - a[1]
        if base.length > 1e-9 and seg.length > 1e-9 and seg.dot(base) < 0:
            rev += 1
    record("frame inner edge never folds back at corners", rev == 0, stations=n, folded_segments=rev)
    spans = sorted((s_[1] - s_[0]).length for s_ in st)
    span = spans[len(spans) // 2]                 # median: corners are mitred (longer)
    record("curved panel keeps the full frame width", span > 0.109, frame_width_plus_seal=round(span, 4),
           width_setting=p.border_width)

# ---------------------------------------------------------------------------
# 0.7.0: UVs, materials, shapes, end caps, game-ready
# ---------------------------------------------------------------------------

def _uv_stats(me, tile):
    """Ratio of UV area to surface area (scaled by the tile) per face: 1.0 = no stretch."""
    uvl = me.uv_layers.get(fm.UV_LAYER)
    if uvl is None:
        return None
    ratios, lo, hi = [], [1e9, 1e9], [-1e9, -1e9]
    for poly in me.polygons:
        a3 = poly.area
        if a3 < 1e-10:
            continue
        uvs = [uvl.data[i].uv.copy() for i in poly.loop_indices]
        for uv in uvs:
            lo = [min(lo[0], uv.x), min(lo[1], uv.y)]
            hi = [max(hi[0], uv.x), max(hi[1], uv.y)]
        a2 = 0.0
        for k in range(len(uvs)):
            x0, y0 = uvs[k]
            x1, y1 = uvs[(k + 1) % len(uvs)]
            a2 += x0 * y1 - x1 * y0
        ratios.append(abs(a2) * 0.5 * tile * tile / a3)
    ratios.sort()
    n = len(ratios)
    return dict(faces=n, median=round(ratios[n // 2], 4), p05=round(ratios[n // 20], 4),
                p95=round(ratios[(19 * n) // 20], 4), lo=[round(x, 3) for x in lo], hi=[round(x, 3) for x in hi])


def t_uvs():
    clear_scene()
    src = make_mesh("RefUV", [(0, 0, 0), (1, 0, 0), (1, 0, 0.6), (0, 0, 0.6)], [(0, 1, 2, 3)])
    ob, _t = faces_filter(src)
    p = ob.filter_maker
    p.live = False
    p.noise_strength = 0.0
    p.use_border = True
    p.uv_mode = 'WORLD'
    p.uv_tile = 0.1
    p.live = True
    fm.regenerate(ob)
    st = _uv_stats(ob.data, 0.1)
    record("face filter has UVs with no stretch (real-world size)",
           st is not None and abs(st["median"] - 1.0) < 0.02 and st["p05"] > 0.9 and st["p95"] < 1.1, **(st or {}))
    bst = _uv_stats(p.border_obj.data, 0.1) if p.border_obj else None
    record("border has UVs at the same texel density",
           bst is not None and abs(bst["median"] - 1.0) < 0.15, **(bst or {}))
    p.pleat_angle = math.radians(35)
    st = _uv_stats(ob.data, 0.1)
    record("UVs stay stretch-free with a pleat angle",
           st is not None and abs(st["median"] - 1.0) < 0.02 and st["p05"] > 0.85, **(st or {}))
    p.uv_mode = 'FIT'
    st = _uv_stats(ob.data, 1.0)
    record("Fit 0-1 keeps UVs inside the unit square",
           st is not None and min(st["lo"]) > -1e-4 and max(st["hi"]) < 1.0 + 1e-4, lo=st and st["lo"], hi=st and st["hi"])
    clear_scene()
    bpy.ops.filter.new_line()
    ob = bpy.context.active_object
    p = ob.filter_maker
    p.noise_strength = 0.0
    st = _uv_stats(ob.data, p.uv_tile)
    record("curve filter has stretch-free UVs",
           st is not None and abs(st["median"] - 1.0) < 0.02 and st["p05"] > 0.9, **(st or {}))
    clear_scene()
    bpy.ops.mesh.primitive_cube_add(size=1.0)
    cube = bpy.context.active_object
    ob, _t = faces_filter(cube, select=lambda f: abs(f.normal.z) < 0.5)
    ob.filter_maker.noise_strength = 0.0
    st = _uv_stats(ob.data, ob.filter_maker.uv_tile)
    record("box wrap UVs are stretch-free around the corners",
           st is not None and abs(st["median"] - 1.0) < 0.05 and st["p05"] > 0.8, **(st or {}))


def t_materials():
    clear_scene()
    src = make_mesh("RefMat", [(0, 0, 0), (1, 0, 0), (1, 0, 0.6), (0, 0, 0.6)], [(0, 1, 2, 3)])
    ob, _t = faces_filter(src)
    p = ob.filter_maker
    p.use_border = True
    bad = []
    for key, _n, _d in fm.FILTER_STYLES:
        if key == 'CUSTOM':
            continue
        p.filter_style = key
        m = p.filter_material
        ok = m is not None and m.name.startswith("FM Media") and m.node_tree is not None and \
            any(n.type == 'BSDF_PRINCIPLED' for n in m.node_tree.nodes) and ob.data.materials[0] == m \
            and m.get("fm_style_v") is not None and len(m.node_tree.links) > 4
        if not ok:
            bad.append((key, m.get("fm_style_error") if m else None))
    for key, _n, _d in fm.FRAME_STYLES:
        if key == 'CUSTOM':
            continue
        p.frame_style = key
        m = p.border_material
        ok = m is not None and m.name.startswith("FM Frame") and p.border_obj.data.materials[0] == m \
            and m.get("fm_style_v") is not None and len(m.node_tree.links) > 2
        if not ok:
            bad.append((key, m.get("fm_style_error") if m else None))
    record("all built-in materials build without errors and get assigned", not bad, failed=bad,
           media=len(fm.FILTER_STYLES) - 1, frames=len(fm.FRAME_STYLES) - 1)
    p.dirt = 0.7
    a = ob.data.attributes.get(fm.DIRT_ATTR)
    vals = [0.0] * len(ob.data.vertices)
    if a is not None:
        a.data.foreach_get("value", vals)
    W = p.border_width
    near = [v for v, vert in zip(vals, ob.data.vertices) if min(vert.co.x, 1 - vert.co.x, vert.co.z, 0.6 - vert.co.z) < W]
    record("dirt mask: clean under the frame, dirty inside",
           a is not None and max(vals) > 0.3 and (max(near) if near else 0.0) < 0.05,
           max=round(max(vals), 3), under_frame_max=round(max(near), 3) if near else None)
    render("09_materials_dirt", [ob] + [o for o in (p.border_obj,) if o], direction=(0.6, -1.5, 0.4))


def t_shapes():
    bad, info = [], {}
    for shape, _n, _d in fm.FILTER_SHAPES:
        clear_scene()
        res = bpy.ops.filter.add_shape(shape=shape)
        ob = bpy.context.active_object
        p = ob.filter_maker if ob else None
        ok = 'FINISHED' in res and p is not None and p.is_filter and not p.last_error and len(ob.data.polygons) > 0
        info[shape] = dict(faces=len(ob.data.polygons) if ob else 0, error=p.last_error if p else "?",
                           border=len(p.border_obj.data.polygons) if (p and p.border_obj) else 0)
        if not ok:
            bad.append(shape)
        if ok:
            render("10_shape_%s" % shape.lower(), [ob] + [o for o in (p.border_obj,) if o], direction=(1.0, -1.3, 0.8))
    record("every Add Filter Shape builds", not bad, failed=bad, **info)


def t_caps():
    clear_scene()
    bpy.ops.filter.add_shape(shape='CARTRIDGE')
    ob = bpy.context.active_object
    p = ob.filter_maker
    p.border_caps = 'NONE'
    n_none = len(p.border_obj.data.polygons)
    p.border_caps = 'BOTH'
    p.border_cap_hole = 0.0
    n_both = len(p.border_obj.data.polygons)
    p.border_cap_hole = 0.5
    n_hole = len(p.border_obj.data.polygons)
    p.border_caps = 'TOP'
    n_top = len(p.border_obj.data.polygons)
    record("end caps: solid, with a hole, one end",
           n_both == n_none + 2 and n_hole > n_both and n_none < n_top < n_hole,
           none=n_none, both_solid=n_both, both_hole=n_hole, top_hole=n_top)
    clear_scene()
    src = make_mesh("RefCapPanel", [(0, 0, 0), (1, 0, 0), (1, 0, 0.6), (0, 0, 0.6)], [(0, 1, 2, 3)])
    ob, _t = faces_filter(src)
    p = ob.filter_maker
    p.use_border = True
    n0 = len(p.border_obj.data.polygons)
    p.border_caps = 'BOTH'
    record("end caps never cover a flat panel", len(p.border_obj.data.polygons) == n0, before=n0,
           after=len(p.border_obj.data.polygons))


def t_game_ready():
    clear_scene()
    src = make_mesh("RefGame", [(0, 0, 0), (0.6, 0, 0), (0.6, 0, 0.4), (0, 0, 0.4)], [(0, 1, 2, 3)])
    ob, _t = faces_filter(src)
    p = ob.filter_maker
    p.live = False
    p.use_border = True
    p.use_dust = True
    p.use_hair = True
    p.hair_density = 400.0
    p.dust_density = 800.0
    p.game_resolution = '512'
    p.game_maps = {'NORMAL', 'AO', 'COLOR'}
    p.game_export = 'GLB'
    p.game_path = os.path.join(OUT, "game_export.glb")
    p.live = True
    fm.regenerate(ob)
    high_tris = sum(len(pl.vertices) - 2 for pl in ob.data.polygons)
    for lod in ('PLEATED', 'FLAT'):
        p.game_lod = lod
        t0 = time.time()
        set_active(ob)
        res = bpy.ops.filter.game_ready()
        low = bpy.data.objects.get(ob.name + "_low")
        tris = sum(len(pl.vertices) - 2 for pl in low.data.polygons) if low else 0
        uvl = low.data.uv_layers.get(fm.UV_LAYER) if low else None
        inside = uvl is not None and all(-1e-4 <= d.uv.x <= 1 + 1e-4 and -1e-4 <= d.uv.y <= 1 + 1e-4 for d in uvl.data)
        nimg = bpy.data.images.get(ob.name + "_normal")
        spread = 0.0
        if nimg is not None and nimg.size[0]:
            px = list(nimg.pixels[:])
            reds = px[0::4][:: max(1, len(px) // 40000)]
            mean = sum(reds) / len(reds)
            spread = (sum((r - mean) ** 2 for r in reds) / len(reds)) ** 0.5
        record("game-ready %s: low poly, packed UVs, baked maps" % lod,
               'FINISHED' in res and low is not None and tris < high_tris * 0.25 and inside and spread > 0.01,
               seconds=round(time.time() - t0, 1), low_tris=tris, high_tris=high_tris, uvs_in_0_1=inside,
               normal_map_variation=round(spread, 4))
        if lod == 'FLAT' and low is not None:
            render("11_game_ready_flat", [low], direction=(0.6, -1.5, 0.4))
    record("game-ready export written", os.path.exists(p.game_path), path=p.game_path)


for name, fn in (("new line", t_new_line), ("plane", t_plane), ("L fold", t_lfold),
                 ("box wrap", t_box_wrap), ("full featured", t_full_featured),
                 ("presets", t_presets), ("icons", t_icons), ("shared library", t_shared_library),
                 ("border coverage", t_border_covers_pleats), ("uvs", t_uvs), ("materials", t_materials),
                 ("shapes", t_shapes), ("caps", t_caps), ("game ready", t_game_ready)):
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
