bl_info = {
    "name": "Filter Maker Pro",
    "author": "Tooboi",
    "version": (0, 3, 11),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar (N) > Filter Maker",
    "description": "Pleated filter generator with curve-collection hair instancing and border-aware density masking.",
    "category": "Object",
}

import bisect
import json
import math
import os
import random
import re
import traceback
from collections import deque

import bpy
import bpy.utils.previews
import bmesh
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty,
                       IntProperty, PointerProperty, StringProperty)
from bpy.types import Operator, Panel, PropertyGroup
from bpy.app.handlers import persistent
from mathutils import Euler, Vector, geometry, noise
from mathutils.kdtree import KDTree

_busy = [False]
_KEEP = [None]
MAX_CUTS = 400
MAX_INST = 100000
BOOL_NAME = "Filter Boolean"

# Reserved preview state. Kept initialized even when preview creation fails.
_ICONS = [None]
_pcoll = [None]
_pv = {}
_pv_key = {}


def ic(name):
    """Return a supported UI icon for this Blender version, or NONE."""
    if _ICONS[0] is None:
        try:
            _ICONS[0] = set(bpy.types.UILayout.bl_rna.functions['prop'].parameters['icon'].enum_items.keys())
        except Exception:
            _ICONS[0] = set()
    return name if name in _ICONS[0] else 'NONE'


# ---------------------------------------------------------------------------
# UI safety helpers
# ---------------------------------------------------------------------------

def redraw_all():
    try:
        wm = bpy.context.window_manager
        for win in wm.windows:
            for area in win.screen.areas:
                area.tag_redraw()
    except Exception:
        pass


def safe_draw(fn):
    def wrapper(self, context):
        try:
            fn(self, context)
        except Exception as exc:
            traceback.print_exc()
            try:
                self.layout.label(text="UI error: %s" % exc, icon=ic('ERROR'))
            except Exception:
                pass
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# ---------------------------------------------------------------------------
# Pleat profile
# ---------------------------------------------------------------------------

def _bevel_radii(p):
    return (min(0.25, max(0.0, p.bevel_ridge) * 0.25),
            min(0.25, max(0.0, p.bevel_valley) * 0.25))


def _profile_table(rp, rv, cuts):
    n = max(1, int(cuts))

    def arc(c, p0, p1):
        out = []
        for i in range(n + 1):
            u = i / n
            a, b, d = (1 - u) ** 2, 2 * u * (1 - u), u * u
            out.append((a * p0[0] + b * c[0] + d * p1[0],
                        a * p0[1] + b * c[1] + d * p1[1]))
        return out

    ridge = [(0.0, 1.0)] if rp < 1e-6 else arc((0.0, 1.0), (-rp, 1.0 - 4 * rp), (rp, 1.0 - 4 * rp))
    valley = [(0.5, -1.0)] if rv < 1e-6 else arc((0.5, -1.0), (0.5 - rv, -1.0 + 4 * rv),
                                                 (0.5 + rv, -1.0 + 4 * rv))
    tab = ridge + valley
    out = [tab[0]]
    for q in tab[1:]:
        if q[0] - out[-1][0] > 1e-9:
            out.append(q)
    if len(out) > 1 and abs(out[-1][0] - (out[0][0] + 1.0)) < 1e-9:
        out.pop()
    return out


def _make_eval(table):
    xs, ws = [t[0] for t in table], [t[1] for t in table]
    x0, n = xs[0], len(xs)

    def f(x):
        xm = (x - x0) % 1.0 + x0
        i = max(0, bisect.bisect_right(xs, xm) - 1)
        xa, wa = xs[i], ws[i]
        if i + 1 < n:
            xb, wb = xs[i + 1], ws[i + 1]
        else:
            xb, wb = xs[0] + 1.0, ws[0]
        if xb - xa < 1e-12:
            return wa
        return wa + (wb - wa) * (xm - xa) / (xb - xa)
    return f


def _cycle_xs(table, period_len, depth, maxlen):
    pts = table + [(table[0][0] + 1.0, table[0][1])]
    xs = []
    for (xa, wa), (xb, wb) in zip(pts, pts[1:]):
        if xb - xa < 1e-9:
            continue
        m = 1
        if maxlen > 0:
            seg = math.hypot((xb - xa) * period_len, (wb - wa) * depth)
            m = max(1, int(math.ceil(seg / maxlen - 1e-9)))
        m = min(m, 64)
        for i in range(m):
            xs.append(xa + (xb - xa) * i / m)
    return xs


# ---------------------------------------------------------------------------
# Path and source helpers
# ---------------------------------------------------------------------------

def _polyline_length(pts, closed):
    total = sum((b - a).length for a, b in zip(pts, pts[1:]))
    if closed and len(pts) > 1:
        total += (pts[0] - pts[-1]).length
    return total


def _resample_at(pts, ss, closed):
    P = pts + [pts[0]] if closed else list(pts)
    lens = [0.0]
    for a, b in zip(P, P[1:]):
        lens.append(lens[-1] + (b - a).length)
    total = lens[-1]
    out, seg = [], 0
    for s in ss:
        if total < 1e-9:
            out.append(pts[0].copy())
            continue
        s = min(max(s, 0.0), total)
        while seg < len(P) - 2 and lens[seg + 1] < s:
            seg += 1
        l0, l1 = lens[seg], lens[seg + 1]
        fac = 0.0 if l1 == l0 else (s - l0) / (l1 - l0)
        out.append(P[seg].lerp(P[seg + 1], fac))
    return out


def _resample(pts, n, closed):
    total = _polyline_length(pts, closed)
    ss = [total * i / n if closed else total * i / (n - 1) for i in range(n)]
    return _resample_at(pts, ss, closed)


def _chains(edges):
    adj = {}
    for ei, (a, b) in enumerate(edges):
        if a == b:
            continue
        adj.setdefault(a, []).append(ei)
        adj.setdefault(b, []).append(ei)
    used, res = set(), []

    def walk(start):
        path, v = [start], start
        while True:
            nxt = next((ei for ei in adj[v] if ei not in used), None)
            if nxt is None:
                break
            used.add(nxt)
            a, b = edges[nxt]
            v = b if a == v else a
            path.append(v)
        return path

    for v, es in list(adj.items()):
        if len(es) != 2:
            while any(e not in used for e in adj[v]):
                res.append((walk(v), False))
    for v in list(adj):
        while any(e not in used for e in adj[v]):
            path = walk(v)
            if len(path) > 2 and path[0] == path[-1]:
                res.append((path[:-1], True))
            else:
                res.append((path, False))
    return res


def _r(v):
    return [round(v[0], 5), round(v[1], 5), round(v[2], 5)]


def paths_from_curve(obj):
    mw, paths, closed = obj.matrix_world, [], []
    for sp in obj.data.splines:
        if sp.type == 'BEZIER':
            bp, n = sp.bezier_points, len(sp.bezier_points)
            if n < 2:
                continue
            seg = n if sp.use_cyclic_u else n - 1
            res = max(sp.resolution_u, 8)
            pts = []
            for i in range(seg):
                a, b = bp[i], bp[(i + 1) % n]
                pts += list(geometry.interpolate_bezier(a.co, a.handle_right, b.handle_left, b.co, res))[:-1]
            if not sp.use_cyclic_u:
                pts.append(bp[-1].co.copy())
        else:
            pts = [Vector(pt.co[:3]) for pt in sp.points]
            if len(pts) < 2:
                continue
        paths.append([_r(mw @ point) for point in pts])
        closed.append(bool(sp.use_cyclic_u) and len(pts) > 2)
    return {"paths": paths, "closed": closed}


def paths_from_mesh(obj, selected_only):
    mw = obj.matrix_world
    if obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(obj.data)
        edges = [(e.verts[0].index, e.verts[1].index) for e in bm.edges if e.select or not selected_only]
        coords = {v.index: mw @ v.co for v in bm.verts}
    else:
        me = obj.data
        edges = [(e.vertices[0], e.vertices[1]) for e in me.edges]
        coords = {v.index: mw @ v.co for v in me.vertices}
    paths, closed = [], []
    for ids, cl in _chains(edges):
        if len(ids) >= 2:
            paths.append([_r(coords[i]) for i in ids])
            closed.append(cl)
    return {"paths": paths, "closed": closed}


def faces_from_mesh(obj, selected_only):
    mw = obj.matrix_world
    if obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(obj.data)
        faces = [[v.index for v in f.verts] for f in bm.faces if f.select or not selected_only]
        coords = {v.index: mw @ v.co for v in bm.verts}
    else:
        me = obj.data
        faces = [list(poly.vertices) for poly in me.polygons]
        coords = {v.index: mw @ v.co for v in me.vertices}
    used = sorted({i for f in faces for i in f})
    remap = {old: new for new, old in enumerate(used)}
    return {"verts": [_r(coords[i]) for i in used], "faces": [[remap[i] for i in f] for f in faces]}


def _src_size(mode, src):
    pts = [p for path in src["paths"] for p in path] if mode == 'CURVE' else src["verts"]
    if not pts:
        return 1.0
    xs, ys, zs = zip(*pts)
    return max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs), 1e-3)


# ---------------------------------------------------------------------------
# Mesh builders
# ---------------------------------------------------------------------------

def _wall_path(rb, pts, cl, d, H, density):
    L = _polyline_length(pts, cl)
    n = min(2000, max(4, int(round(L * density * 2)))) if cl else min(2000, max(2, int(round(L * density * 2)) + 1))
    rcp = _resample(pts, n, cl)
    rc = [[rb.verts.new(c), rb.verts.new(c + d * H)] for c in rcp]
    for i in range(n if cl else n - 1):
        j = (i + 1) % n
        try:
            rb.faces.new((rc[i][0], rc[j][0], rc[j][1], rc[i][1]))
        except ValueError:
            pass


def _height_dir(p):
    d = Vector(p.height_dir)
    if d.length < 1e-6:
        d = Vector((0, 0, 1))
    d.normalize()
    return d


def build_curve(bm, p, src, ref=None):
    d, H = _height_dir(p), max(p.height, 1e-5)
    rows = max(1, min(200, int(round(H * p.detail))))
    table = _profile_table(*_bevel_radii(p), p.bevel_cuts)
    ev, depth = _make_eval(table), p.pleat_depth
    for path, cl in zip(src["paths"], src["closed"]):
        pts = [Vector(x) for x in path]
        L = _polyline_length(pts, cl)
        nhalf = max(2, int(round(L * p.pleat_density * 2)))
        if cl and nhalf % 2:
            nhalf += 1
        Lp = nhalf / 2.0
        period = L / Lp if L > 1e-9 else 1.0 / max(p.pleat_density, 1e-3)
        maxlen = 1.0 / max(p.detail, 1e-3)
        xs = _cycle_xs(table, period, depth, maxlen)
        for _ in range(8):
            if len(xs) * Lp <= 6000:
                break
            maxlen *= 2.0
            xs = _cycle_xs(table, period, depth, maxlen)
        poss = []
        for k in range(-1, int(math.ceil(Lp)) + 2):
            for x in xs:
                pos = k + x - p.pleat_phase
                if -1e-9 <= pos < Lp - 1e-9:
                    poss.append(max(pos, 0.0))
        poss.sort()
        ded = []
        for q in poss:
            if not ded or q - ded[-1] > 1e-9:
                ded.append(q)
        poss = ded
        if not cl:
            if not poss or poss[0] > 1e-9:
                poss.insert(0, 0.0)
            poss.append(Lp)
        cp = _resample_at(pts, [q * period for q in poss], cl)
        n, q3 = len(cp), []
        for i, c in enumerate(cp):
            prev = cp[i - 1] if (i > 0 or cl) else c
            nxt = cp[(i + 1) % n] if (i < n - 1 or cl) else c
            t = nxt - prev
            side = t.cross(d)
            if side.length < 1e-9:
                side = t.cross(Vector((1, 0, 0)))
                if side.length < 1e-9:
                    side = t.cross(Vector((0, 1, 0)))
            if side.length < 1e-9:
                side = Vector((1, 0, 0))
            side.normalize()
            q3.append(c + side * (depth * ev(poss[i] + p.pleat_phase)))
        cols = [[bm.verts.new(q3[i] + d * (H * k / rows)) for k in range(rows + 1)] for i in range(n)]
        for i in range(n if cl else n - 1):
            j = (i + 1) % n
            for k in range(rows):
                try:
                    bm.faces.new((cols[i][k], cols[j][k], cols[j][k + 1], cols[i][k + 1]))
                except ValueError:
                    pass
        if ref is not None:
            _wall_path(ref, pts, cl, d, H, p.pleat_density)


def _bisect_all(g, co, no):
    bmesh.ops.bisect_plane(g, geom=g.verts[:] + g.edges[:] + g.faces[:], dist=1e-6,
                           plane_co=co, plane_no=no)


def _face_components(tmp):
    seen, comps = set(), []
    for f in tmp.faces:
        if f.index in seen:
            continue
        seen.add(f.index)
        stack, comp = [f], []
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for e in cur.edges:
                for nf in e.link_faces:
                    if nf.index not in seen:
                        seen.add(nf.index)
                        stack.append(nf)
        comps.append(comp)
    return comps


def _quadify(g):
    big = [f for f in g.faces if len(f.verts) > 4]
    if not big:
        return
    res = bmesh.ops.triangulate(g, faces=big)
    tris = [f for f in res.get("faces", []) if f.is_valid]
    if tris:
        try:
            bmesh.ops.join_triangles(g, faces=tris, angle_face_threshold=0.02, angle_shape_threshold=1.2)
        except Exception:
            pass


def _face_component(bm, p, comp):
    """Pleat a connected face island with one shared world-space phase field.

    Every face is cut by the same planes. This keeps intersections exactly aligned
    across shared edges, including when Pleat Angle is changed.
    """
    if not comp:
        return

    # Use an area-weighted component normal rather than letting a single large
    # polygon dictate the pleat frame. This is more stable on connected, curved
    # selections and reduces apparent spacing drift as the pattern crosses faces.
    weighted_normal = Vector((0.0, 0.0, 0.0))
    for face in comp:
        if face.normal.length > 1e-9:
            weighted_normal += face.normal.normalized() * max(face.calc_area(), 1e-9)
    if weighted_normal.length < 1e-8:
        f0 = max(comp, key=lambda f: f.calc_area())
        weighted_normal = f0.normal.copy()
    n0 = weighted_normal.normalized() if weighted_normal.length > 1e-9 else Vector((0, 0, 1))
    ref = Vector((1.0, 0.0, 0.0))
    if abs(n0.dot(ref)) > 0.95:
        ref = Vector((0.0, 1.0, 0.0))
    u0 = (ref - n0 * ref.dot(n0)).normalized()
    v0 = n0.cross(u0).normalized()

    # A single across-pleat axis is used for the whole connected selection.
    # Per-face axes cause adjacent faces to cut at different positions.
    angle = p.pleat_angle
    ua = (u0 * math.cos(angle) + v0 * math.sin(angle)).normalized()
    ra = n0.cross(ua).normalized()

    rp, rv = _bevel_radii(p)
    table = _profile_table(rp, rv, p.bevel_cuts)
    ev = _make_eval(table)
    period = 1.0 / max(p.pleat_density, 1e-3)
    depth = p.pleat_depth

    points = [v.co for f in comp for v in f.verts]
    uvals = [ua.dot(co) for co in points]
    umin, umax = min(uvals), max(uvals)
    base = umin - period * p.pleat_phase

    maxlen = 1.0 / max(p.detail, 1e-3)
    xs = _cycle_xs(table, period, depth, maxlen)
    for _ in range(12):
        if (umax - umin) / period * len(xs) <= MAX_CUTS:
            break
        maxlen *= 1.5
        xs = _cycle_xs(table, period, depth, maxlen)

    vstep = 1.0 / max(p.detail, 1e-3)
    cb = bmesh.new()
    lay = cb.verts.layers.float.new("u")
    try:
        for f in comp:
            g = bmesh.new()
            try:
                nv = [g.verts.new(v.co) for v in f.verts]
                try:
                    g.faces.new(nv)
                except ValueError:
                    continue

                fus = [ua.dot(v.co) for v in f.verts]
                u_lo, u_hi = min(fus), max(fus)
                k0 = int(math.floor((u_lo - base) / period)) - 1
                k1 = int(math.ceil((u_hi - base) / period)) + 1
                for k in range(k0, k1 + 1):
                    for x in xs:
                        ut = base + (k + x) * period
                        if u_lo + 1e-6 < ut < u_hi - 1e-6:
                            _bisect_all(g, ua * ut, ua)

                # Use one common ridge-axis grid as well, so shared edge topology
                # is split at matching coordinates on both sides.
                fvs = [ra.dot(v.co) for v in f.verts]
                v_lo, v_hi = min(fvs), max(fvs)
                vs = vstep
                if (v_hi - v_lo) / vs > MAX_CUTS:
                    vs = (v_hi - v_lo) / MAX_CUTS
                m = int(math.floor(v_lo / vs)) + 1
                while m * vs < v_hi - 1e-6:
                    if m * vs > v_lo + 1e-6:
                        _bisect_all(g, ra * (m * vs), ra)
                    m += 1

                if p.mesh_type == 'TRIS':
                    bmesh.ops.triangulate(g, faces=g.faces[:])
                else:
                    _quadify(g)

                vm = {}
                for gf in g.faces:
                    vl = []
                    for v in gf.verts:
                        if v not in vm:
                            nvv = cb.verts.new(v.co)
                            nvv[lay] = ua.dot(v.co)
                            vm[v] = nvv
                        vl.append(vm[v])
                    try:
                        cb.faces.new(vl)
                    except ValueError:
                        pass
            finally:
                g.free()

        size = max(umax - umin, 1e-3)
        bmesh.ops.remove_doubles(cb, verts=cb.verts[:], dist=max(1e-5, size * 2e-6))
        cb.normal_update()
        for v in cb.verts:
            v.co += v.normal * (depth * ev((v[lay] - base) / period))

        vmap = {v: bm.verts.new(v.co) for v in cb.verts}
        for f in cb.faces:
            try:
                bm.faces.new([vmap[v] for v in f.verts])
            except ValueError:
                pass
    finally:
        cb.free()


def _smooth_face_source(tmp, p):
    """Subdivide and soften internal face transitions while preserving the outer boundary.

    Smoothing is done on the source surface before the pleat profile is applied, so
    the pleat peaks/valleys are not blurred by a generic post-process on the result.
    Selection boundaries are held in place to keep the source outline predictable.
    """
    if not getattr(p, "face_smooth", True) or p.face_smooth_factor <= 1e-6 or not tmp.faces:
        return

    edges = list(tmp.edges)
    if not edges:
        return

    cuts = max(1, min(4, int(p.face_smooth_cuts)))
    try:
        bmesh.ops.subdivide_edges(
            tmp, edges=edges, cuts=cuts, use_grid_fill=True,
            use_single_edge=False, use_only_quads=False)
    except TypeError:
        # Compatibility fallback for Blender builds whose BMesh signature differs.
        bmesh.ops.subdivide_edges(tmp, edges=edges, cuts=cuts, use_grid_fill=True)

    tmp.verts.ensure_lookup_table()
    tmp.edges.ensure_lookup_table()
    tmp.faces.ensure_lookup_table()

    # The current one-face edges outline the selected region. Do not move these
    # vertices, including newly inserted points along the outer boundary.
    boundary = {v for e in tmp.edges if len(e.link_faces) == 1 for v in e.verts}
    interior = [v for v in tmp.verts if v not in boundary]
    if interior:
        # Two moderate passes round sharp transitions without visibly shrinking
        # the entire selection the way unrestricted subdivision smoothing would.
        for _ in range(2):
            bmesh.ops.smooth_vert(
                tmp, verts=interior, factor=float(p.face_smooth_factor),
                use_axis_x=True, use_axis_y=True, use_axis_z=True)

    tmp.verts.index_update()
    tmp.edges.index_update()
    tmp.faces.index_update()
    comps = _face_components(tmp)
    for comp in comps:
        if len(comp) > 1:
            bmesh.ops.recalc_face_normals(tmp, faces=comp)
    tmp.normal_update()


def _faces_bmesh(src, smooth_p=None):
    tmp = bmesh.new()
    tv = [tmp.verts.new(v) for v in src["verts"]]
    for f in src["faces"]:
        try:
            tmp.faces.new([tv[i] for i in f])
        except ValueError:
            pass
    tmp.verts.index_update()
    tmp.edges.index_update()
    tmp.faces.index_update()
    if smooth_p is not None:
        _smooth_face_source(tmp, smooth_p)
    comps = _face_components(tmp)
    for comp in comps:
        if len(comp) > 1:
            bmesh.ops.recalc_face_normals(tmp, faces=comp)
    tmp.normal_update()
    return tmp, comps


def build_face(bm, p, src, ref=None):
    tmp, comps = _faces_bmesh(src, smooth_p=p)
    try:
        for comp in comps:
            _face_component(bm, p, comp)
        if ref is not None:
            vm = {}
            for f in tmp.faces:
                vl = []
                for v in f.verts:
                    if v not in vm:
                        vm[v] = ref.verts.new(v.co)
                    vl.append(vm[v])
                try:
                    ref.faces.new(vl)
                except ValueError:
                    pass
    finally:
        tmp.free()


# ---------------------------------------------------------------------------
# Wear and shading
# ---------------------------------------------------------------------------

def apply_noise(bm, p):
    strength = p.noise_strength
    if strength <= 0 or p.noise_scale <= 0 or not bm.verts:
        return
    inv, sd = 1.0 / p.noise_scale, p.noise_seed
    off = Vector((sd * 37.1, sd * 17.3, sd * 7.9))
    hard, basis, depth = p.noise_type == 'HARD', p.noise_basis, p.noise_depth
    verts = list(bm.verts)
    xyz = p.noise_direction == 'XYZ'
    offs = (Vector((0, 0, 0)), Vector((31.7, 17.3, 5.1)), Vector((9.9, 43.1, 21.7)))
    vals = []
    for v in verts:
        q = v.co * inv + off
        if xyz:
            vals.append(Vector([noise.turbulence(q + o, depth, hard, noise_basis=basis) for o in offs]))
        else:
            vals.append(noise.turbulence(q, depth, hard, noise_basis=basis))
    mean = sum(vals, Vector((0, 0, 0)) if xyz else 0.0) / len(vals)
    for v, val in zip(verts, vals):
        if xyz:
            v.co += (val - mean) * strength
        else:
            v.co += v.normal * ((val - mean) * strength)


def apply_shading(bm, p):
    smooth = p.smooth or p.auto_smooth
    for f in bm.faces:
        f.smooth = smooth
    for e in bm.edges:
        sharp = p.auto_smooth and len(e.link_faces) == 2 and e.calc_face_angle(0.0) > p.auto_smooth_angle
        e.smooth = not sharp


def apply_border_shading(bm, p):
    """Smooth-shade the separately generated frame and retain hard edges by angle."""
    smooth = p.border_smooth or p.border_auto_smooth
    for face in bm.faces:
        face.smooth = smooth
    for edge in bm.edges:
        sharp = (p.border_auto_smooth and len(edge.link_faces) == 2
                 and edge.calc_face_angle(0.0) > p.border_auto_smooth_angle)
        edge.smooth = not sharp


# ---------------------------------------------------------------------------
# Border
# ---------------------------------------------------------------------------

def _arc2(c, a, b, n):
    out = []
    for i in range(n + 1):
        u = i / n
        w0, w1, w2 = (1 - u) ** 2, 2 * u * (1 - u), u * u
        out.append((w0 * a[0] + w1 * c[0] + w2 * b[0], w0 * a[1] + w1 * c[1] + w2 * b[1]))
    return out


def _border_dims(p, small=None):
    W = max(p.border_width, 1e-6)
    if small:
        W = min(W, small * 0.48)
    O = max(p.border_overhang, 0.0)
    effective_thickness = p.thickness if getattr(p, "use_thickness", False) else 0.0
    fit = (p.pleat_depth + effective_thickness + 1.5 * p.noise_strength) if p.border_fit else 0.0
    F, B = max(fit + p.border_front, 1e-6), max(fit + p.border_back, 1e-6)
    return W, O, F, B, fit


def _border_section(p, W, O, F, B):
    prof, r = p.border_profile, max(0.0, p.border_bevel)
    if prof == 'FLAT' or r < 1e-7:
        pts = [(-O, -B), (W, -B), (W, F), (-O, F)]
    elif prof == 'CHAMFER':
        r = min(r, (O + W) * 0.9, (F + B) * 0.45)
        pts = [(-O + r, -B), (W, -B), (W, F), (-O + r, F), (-O, F - r), (-O, -B + r)]
    elif prof == 'ROUND':
        r = min(r, (O + W) * 0.9, (F + B) * 0.45)
        n = max(1, int(p.border_cuts))
        pts = [(W, -B), (W, F)]
        pts += _arc2((-O, F), (-O + r, F), (-O, F - r), n)
        pts += _arc2((-O, -B), (-O, -B + r), (-O + r, -B), n)
    else:
        r = min(r, (F + B) * 0.6)
        lw = (O + W) * 0.4
        pts = [(-O, -B), (W, -B), (W, F - r), (-O + lw, F - r), (-O + lw, F), (-O, F)]
    out = []
    for q in pts:
        if not out or abs(q[0] - out[-1][0]) + abs(q[1] - out[-1][1]) > 1e-9:
            out.append(q)
    if len(out) > 1 and abs(out[0][0] - out[-1][0]) + abs(out[0][1] - out[-1][1]) <= 1e-9:
        out.pop()
    return out


def _sweep_loop(co, nrm, inw_edge, sec, closed=True):
    """Sweep a closed border profile along a closed loop or open boundary chain."""
    n, m_ = len(co), len(sec)
    verts = []
    edge_count = n if closed else n - 1
    if n < 2 or edge_count < 1 or not inw_edge:
        return [], []

    for k in range(n):
        if closed:
            a = inw_edge[(k - 1) % edge_count]
            b = inw_edge[k % edge_count]
        elif k == 0:
            a = b = inw_edge[0]
        elif k == n - 1:
            a = b = inw_edge[-1]
        else:
            a, b = inw_edge[k - 1], inw_edge[k]
        miter_dir = a + b
        if miter_dir.length < 1e-6:
            miter_dir = b.copy() if b.length > 1e-6 else a.copy()
        if miter_dir.length > 1e-9:
            miter_dir.normalize()
        denom = max(miter_dir.dot(b) if b.length > 1e-9 else 1.0, 0.3)
        miter = miter_dir / denom if miter_dir.length > 1e-9 else Vector((0, 0, 0))
        for offset, height in sec:
            verts.append(co[k] + miter * offset + nrm[k] * height)

    quads = []
    for k in range(edge_count):
        k2 = (k + 1) % n
        for j in range(m_):
            j2 = (j + 1) % m_
            quads.append((k * m_ + j, k2 * m_ + j,
                          k2 * m_ + j2, k * m_ + j2))
    return verts, quads


def build_border(p, rb):
    """Build a frame along every boundary loop, including open boundary chains."""
    rb.verts.ensure_lookup_table()
    rb.edges.ensure_lookup_table()
    rb.faces.ensure_lookup_table()
    rb.verts.index_update()
    rb.normal_update()
    bedges = [(e.verts[0].index, e.verts[1].index)
              for e in rb.edges if len(e.link_faces) == 1]
    if not bedges:
        return None

    xs = [v.co.x for v in rb.verts]
    ys = [v.co.y for v in rb.verts]
    zs = [v.co.z for v in rb.verts]
    ext = [max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)]
    mx = max(ext)
    nz = [value for value in ext if value > 1e-6 * max(mx, 1e-9)]
    small = min(nz) if nz else mx
    W, O, F, B, _fit = _border_dims(p, small)
    sec = _border_section(p, W, O, F, B)
    if len(sec) < 3:
        return None

    fb = bmesh.new()
    for ids, closed in _chains(bedges):
        if len(ids) < (3 if closed else 2):
            continue
        P = [rb.verts[i] for i in ids]
        n = len(P)
        segment_count = n if closed else n - 1
        inw = []
        for k in range(segment_count):
            a, b = P[k], P[(k + 1) % n]
            edge = rb.edges.get((a, b))
            tangent = b.co - a.co
            if edge is None or not edge.link_faces or tangent.length < 1e-12:
                inw.append(Vector((0, 0, 0)))
                continue
            face = edge.link_faces[0]
            inward = face.normal.cross(tangent.normalized())
            if inward.length < 1e-9:
                inw.append(Vector((0, 0, 0)))
                continue
            inward.normalize()
            if inward.dot(face.calc_center_median() - (a.co + b.co) * 0.5) < 0:
                inward.negate()
            inw.append(inward)

        normals = [v.normal.copy() if v.normal.length > 1e-9 else Vector((0, 0, 1)) for v in P]
        verts, quads = _sweep_loop([v.co for v in P], normals, inw, sec, closed=closed)
        if not verts:
            continue
        fv = [fb.verts.new(co) for co in verts]
        for q in quads:
            try:
                fb.faces.new([fv[i] for i in q])
            except ValueError:
                pass

        # A non-cyclic boundary needs caps so its frame is a closed, printable mesh.
        if not closed:
            try:
                fb.faces.new(fv[:len(sec)])
            except ValueError:
                pass
            try:
                fb.faces.new(fv[(n - 1) * len(sec):n * len(sec)])
            except ValueError:
                pass

    if not fb.faces:
        fb.free()
        return None
    bmesh.ops.recalc_face_normals(fb, faces=fb.faces[:])
    return fb


# ---------------------------------------------------------------------------
# Boolean cutter
# ---------------------------------------------------------------------------

def _cut_pad(p):
    effective_thickness = p.thickness if getattr(p, "use_thickness", False) else 0.0
    return p.pleat_depth + effective_thickness + 1.5 * p.noise_strength + 1e-5


def _newell(vs):
    n = Vector((0, 0, 0))
    for i in range(len(vs)):
        a, b = vs[i], vs[(i + 1) % len(vs)]
        n += Vector(((a.y - b.y) * (a.z + b.z), (a.z - b.z) * (a.x + b.x), (a.x - b.x) * (a.y + b.y)))
    return n


def _frame_face(p, src):
    vs = [Vector(v) for v in src["verts"]]
    best, bn = -1.0, Vector((0, 0, 1))
    for f in src["faces"]:
        n = _newell([vs[i] for i in f])
        if n.length > best:
            best, bn = n.length, n
    n0 = bn.normalized() if bn.length > 1e-12 else Vector((0, 0, 1))
    ref = Vector((1, 0, 0))
    if abs(n0.dot(ref)) > 0.95:
        ref = Vector((0, 1, 0))
    u0 = (ref - n0 * ref.dot(n0)).normalized()
    v0 = n0.cross(u0)
    a = p.pleat_angle
    ua = u0 * math.cos(a) + v0 * math.sin(a)
    ra = n0.cross(ua)
    return [ua, ra, n0], vs, 2


def _frame_curve(p, src):
    d, H = _height_dir(p), max(p.height, 1e-5)
    pts = [Vector(x) for path in src["paths"] for x in path]
    longest, bl = None, -1.0
    for path, cl in zip(src["paths"], src["closed"]):
        pp = [Vector(x) for x in path]
        L = _polyline_length(pp, cl)
        if L > bl:
            bl, longest = L, pp
    if not longest:
        raise ValueError("No valid source path found for the boolean cutter.")
    t = longest[-1] - longest[0]
    t = t - d * t.dot(d)
    if t.length < 1e-6 * max(bl, 1e-9):
        t = Vector((1, 0, 0)) - d * d.x
        if t.length < 1e-6:
            t = Vector((0, 1, 0)) - d * d.y
    a1 = t.normalized()
    a3 = a1.cross(d)
    if a3.length < 1e-9:
        a3 = d.cross(Vector((1, 0, 0)))
    a3.normalize()
    return [a1, d, a3], pts, 2, H


def _box_geometry(ax, rng):
    verts = []
    for i in (0, 1):
        for j in (0, 1):
            for k in (0, 1):
                verts.append(ax[0] * rng[0][i] + ax[1] * rng[1][j] + ax[2] * rng[2][k])
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    return verts, quads


def _box_ranges(p, ax, pts, thick_axis, extra_hi=None):
    rng, pad, margin = [], _cut_pad(p), p.cutter_margin
    for i in range(3):
        cs = [q.dot(ax[i]) for q in pts]
        lo, hi = min(cs), max(cs)
        if extra_hi is not None and i == extra_hi[0]:
            hi += extra_hi[1]
        if i == thick_axis:
            lo, hi = lo - pad, hi + pad
        else:
            lo, hi = lo - margin, hi + margin
            if hi - lo < 1e-6:
                mid = (hi + lo) * 0.5
                lo, hi = mid - 5e-4, mid + 5e-4
        rng.append((lo, hi))
    return rng


def _make_keep(p, ax, rng):
    eps, op = 1e-6, p.boolean_op
    def keep(pt):
        inside = all(rng[i][0] - eps <= pt.dot(ax[i]) <= rng[i][1] + eps for i in range(3))
        if op == 'INTERSECT':
            return inside
        if op == 'DIFFERENCE':
            return not inside
        return True
    return keep


def _slab(src_bm, pad):
    src_bm.verts.index_update()
    src_bm.normal_update()
    out, top, bot = bmesh.new(), {}, {}
    for v in src_bm.verts:
        n = v.normal.copy()
        if n.length < 1e-9:
            n = Vector((0, 0, 1))
        top[v] = out.verts.new(v.co + n * pad)
        bot[v] = out.verts.new(v.co - n * pad)
    def mk(vs):
        try:
            out.faces.new(vs)
        except ValueError:
            pass
    for f in src_bm.faces:
        vl = list(f.verts)
        mk([top[v] for v in vl])
        mk([bot[v] for v in reversed(vl)])
    for e in src_bm.edges:
        if len(e.link_faces) == 1:
            a, b = e.verts
            mk([bot[a], bot[b], top[b], top[a]])
    bmesh.ops.recalc_face_normals(out, faces=out.faces[:])
    return out


def build_cutter(p, src):
    if p.boolean_source == 'ENVELOPE':
        pad = _cut_pad(p)
        if p.mode == 'CURVE':
            rb = bmesh.new()
            try:
                d, H = _height_dir(p), max(p.height, 1e-5)
                for path, cl in zip(src["paths"], src["closed"]):
                    _wall_path(rb, [Vector(x) for x in path], cl, d, H, p.pleat_density)
                out = _slab(rb, pad)
            finally:
                rb.free()
        else:
            tmp, _comps = _faces_bmesh(src)
            try:
                out = _slab(tmp, pad)
            finally:
                tmp.free()
        return out, None
    if p.mode == 'CURVE':
        ax, pts, tk, H = _frame_curve(p, src)
        rng = _box_ranges(p, ax, pts, tk, extra_hi=(1, H))
    else:
        ax, pts, tk = _frame_face(p, src)
        rng = _box_ranges(p, ax, pts, tk)
    cb = bmesh.new()
    verts, quads = _box_geometry(ax, rng)
    vs = [cb.verts.new(c) for c in verts]
    for q in quads:
        try:
            cb.faces.new([vs[i] for i in q])
        except ValueError:
            pass
    bmesh.ops.recalc_face_normals(cb, faces=cb.faces[:])
    return cb, _make_keep(p, ax, rng)


# ---------------------------------------------------------------------------
# Curve-instanced hair, emitted directly from the filter object
# ---------------------------------------------------------------------------

_HAIR_PROFILES = (
    # Each profile is normalized to a one-unit strand. Local +Z is the root-to-tip axis.
    ("Soft Arc", ((0.0, 0.0, 0.0), (0.22, 0.05, 0.25), (0.78, 0.10, 0.68), (0.20, 0.0, 1.0))),
    ("Lazy S", ((0.0, 0.0, 0.0), (-0.68, 0.18, 0.22), (0.72, -0.28, 0.55), (-0.18, 0.10, 0.80), (0.0, 0.0, 1.0))),
    ("Hook", ((0.0, 0.0, 0.0), (0.72, 0.10, 0.24), (0.92, -0.18, 0.53), (0.12, 0.05, 0.82), (0.0, 0.0, 1.0))),
    ("Wave", ((0.0, 0.0, 0.0), (-0.68, -0.20, 0.22), (0.48, 0.32, 0.47), (0.68, -0.18, 0.73), (0.0, 0.0, 1.0))),
    ("Kink", ((0.0, 0.0, 0.0), (0.12, 0.62, 0.22), (-0.48, 0.48, 0.50), (0.56, -0.15, 0.76), (0.04, 0.0, 1.0))),
    ("Corkscrew", ((0.0, 0.0, 0.0), (0.62, 0.42, 0.20), (-0.52, 0.54, 0.42), (-0.50, -0.46, 0.64), (0.45, -0.30, 0.84), (0.0, 0.0, 1.0))),
    ("Fine Straight", ((0.0, 0.0, 0.0), (0.14, -0.02, 0.30), (-0.10, 0.06, 0.65), (0.0, 0.0, 1.0))),
)


def _hair_collection_for(obj, p):
    """Return the per-filter collection of tapered Curve objects used as hair instances."""
    coll = getattr(p, "hair_collection", None)
    if coll is None or coll.name not in bpy.data.collections:
        coll = bpy.data.collections.get(obj.name + " Hair Shapes")
    if coll is None:
        coll = bpy.data.collections.new(obj.name + " Hair Shapes")
    try:
        bpy.context.scene.collection.children.link(coll)
    except Exception:
        # Already linked to this scene, or linked through another collection.
        pass
    p.hair_collection = coll

    curl_amp = max(0.035, min(0.42, 0.055 + max(0.0, p.hair_curl) * 0.18))
    random_amp = max(0.0, min(0.16, p.hair_randomness * 0.035))
    width_ratio = p.hair_width / max(p.hair_length, 1e-6)
    bevel = max(0.00035, min(0.15, width_ratio * 0.5))
    shape_params = (round(float(p.hair_curl), 4), round(float(p.hair_randomness), 4))
    saved_params = (coll.get("fm_hair_curl", None), coll.get("fm_hair_randomness", None))
    rebuild_profiles = (not coll.get("fm_hair_shapes_initialized", False)
                        or saved_params != shape_params)

    for shape_index, (shape_name, profile) in enumerate(_HAIR_PROFILES):
        shape = next((o for o in coll.objects
                      if o.type == 'CURVE' and o.get("fm_hair_shape_index") == shape_index), None)
        created = shape is None
        if created:
            curve_data = bpy.data.curves.new("FM Hair Curve %02d" % (shape_index + 1), 'CURVE')
            shape = bpy.data.objects.new("FM Hair Shape %02d" % (shape_index + 1), curve_data)
            coll.objects.link(shape)
            shape["fm_hair_shape_index"] = shape_index
        curve_data = shape.data
        curve_data.dimensions = '3D'
        curve_data.resolution_u = 10
        curve_data.render_resolution_u = 12
        curve_data.bevel_depth = bevel
        curve_data.bevel_resolution = 3
        try:
            curve_data.twist_smooth = 8
        except Exception:
            pass

        # Preserve manual edits to curve control points during unrelated property
        # changes. Rebuild profiles only on first generation or when curl/randomness
        # controls change; width and material can safely update every time.
        if created or rebuild_profiles or len(curve_data.splines) == 0:
            for old_spline in list(curve_data.splines):
                curve_data.splines.remove(old_spline)
            spline = curve_data.splines.new('BEZIER')
            spline.bezier_points.add(len(profile) - 1)
            total = max(1, len(profile) - 1)
            for i, (x, y, z) in enumerate(profile):
                point = spline.bezier_points[i]
                point.co = (x * curl_amp, y * (curl_amp * 0.70 + random_amp), z)
                point.handle_left_type = 'AUTO'
                point.handle_right_type = 'AUTO'
                t = i / total
                point.radius = max(0.018, (1.0 - t) ** 1.25)
            spline.resolution_u = 10

        curve_data.materials.clear()
        if p.hair_material is not None:
            curve_data.materials.append(p.hair_material)

        # Keep source curves away from the model/camera. The particle system instances
        # them in local coordinates, so their scene-space location is not inherited.
        shape.location = (100000.0 + shape_index * 2.0, 100000.0, 100000.0)
        shape.rotation_euler = (0.0, 0.0, 0.0)
        shape.scale = (1.0, 1.0, 1.0)
        shape.hide_render = False
        try:
            shape.hide_set(True)
        except Exception:
            pass

    coll["fm_hair_shapes_initialized"] = True
    coll["fm_hair_curl"] = shape_params[0]
    coll["fm_hair_randomness"] = shape_params[1]
    return coll


def _hair_border_mask_weights(obj, p):
    """Create vertex weights that suppress hair roots within the border's covered band."""
    mesh = obj.data
    count = len(mesh.vertices)
    if count == 0:
        return None

    # Particle-system density groups are owned by the emitting object. Recreate our
    # generated mask so stale assignments cannot survive a topology change.
    mask_name = "FM Hair Border Mask"
    old_group = obj.vertex_groups.get(mask_name)
    if old_group is not None:
        obj.vertex_groups.remove(old_group)
    group = obj.vertex_groups.new(name=mask_name)

    edge_uses = {}
    for poly in mesh.polygons:
        ids = list(poly.vertices)
        for i, a in enumerate(ids):
            b = ids[(i + 1) % len(ids)]
            key = (a, b) if a < b else (b, a)
            edge_uses[key] = edge_uses.get(key, 0) + 1
    boundary_edges = [edge for edge, uses in edge_uses.items() if uses == 1]

    weights = [1.0] * count
    should_mask = bool(p.use_border and p.border_obj is not None and boundary_edges)
    if should_mask:
        # Include boundary vertices and segment midpoints so long edges do not leave
        # gaps in the distance mask between vertices.
        boundary_points = []
        mw = obj.matrix_world
        for a, b in boundary_edges:
            pa = mw @ mesh.vertices[a].co
            pb = mw @ mesh.vertices[b].co
            boundary_points.append(pa)
            boundary_points.append(pb)
            boundary_points.append((pa + pb) * 0.5)
        tree = KDTree(len(boundary_points))
        for i, point in enumerate(boundary_points):
            tree.insert(point, i)
        tree.balance()

        band = max(0.0, p.border_width) + max(0.0, p.border_overhang) + max(0.0, p.hair_width)
        transition = max(band * 0.30, max(0.0, p.hair_width) * 2.0, 1e-5)
        for vert in mesh.vertices:
            _co, _index, dist = tree.find(mw @ vert.co)
            if dist <= band:
                weight = 0.0
            elif dist >= band + transition:
                weight = 1.0
            else:
                t = (dist - band) / transition
                weight = t * t * (3.0 - 2.0 * t)
            # Quantization batches vertex-group writes into a small number of calls.
            weights[vert.index] = round(weight * 20.0) / 20.0

    buckets = {}
    for index, weight in enumerate(weights):
        if weight > 0.0:
            buckets.setdefault(weight, []).append(index)
    for weight, indices in buckets.items():
        group.add(indices, float(weight), 'REPLACE')
    return group if should_mask else None


def _update_hair_particles_on_filter(obj, p):
    """Instance a collection of Curve strands directly on the generated filter mesh."""
    # Remove legacy duplicate hair-emitter/detail meshes from previous addon versions.
    _remove_child(p, "hair_obj")
    _remove_child(p, "hair_geo_obj")

    psys = next((item for item in obj.particle_systems if item.name == "Filter Maker Hair"), None)
    if not p.use_hair:
        if psys is not None:
            psys.settings.count = 0
            psys.vertex_group_density = ""
        return

    collection = _hair_collection_for(obj, p)
    if psys is None:
        psys = _ensure_particle_system(obj, "Filter Maker Hair")
    settings = psys.settings
    settings.type = 'HAIR'
    settings.count = 0
    settings.emit_from = 'FACE'
    settings.distribution = 'JIT'
    settings.use_even_distribution = True
    settings.render_type = 'COLLECTION'
    settings.instance_collection = collection
    settings.use_whole_collection = False
    settings.use_collection_pick_random = True
    settings.use_global_instance = False
    settings.use_rotation_instance = True
    settings.use_scale_instance = True
    settings.particle_size = max(0.001, p.hair_length)
    settings.size_random = max(0.0, min(1.0, p.hair_length_var))
    settings.display_percentage = 100
    settings.display_method = 'RENDER'
    settings.draw_step = 2
    settings.hair_step = 4
    settings.use_hair_bspline = True
    try:
        settings.use_rotations = True
        settings.rotation_mode = 'NOR'
        settings.phase_factor_random = 1.0
    except Exception:
        pass
    try:
        settings.use_modifier_stack = False
    except Exception:
        pass
    psys.seed = int(p.hair_seed)

    mask_group = _hair_border_mask_weights(obj, p)
    psys.vertex_group_density = mask_group.name if mask_group is not None else ""
    psys.invert_vertex_group_density = False

    area = sum(poly.area for poly in obj.data.polygons)
    settings.count = max(0, min(MAX_INST, int(round(area * max(0.0, p.hair_density) * 0.65))))
    obj.update_tag(refresh={'DATA', 'OBJECT'})
    try:
        bpy.context.view_layer.update()
    except Exception:
        pass


def _remove_hair_curve_collection(p):
    """Delete this filter's generated curve-instance library when the filter is deleted."""
    coll = getattr(p, "hair_collection", None)
    if coll is None:
        return
    for source in list(coll.objects):
        curve_data = source.data if source.type == 'CURVE' else None
        bpy.data.objects.remove(source, do_unlink=True)
        if curve_data is not None and curve_data.users == 0:
            bpy.data.curves.remove(curve_data)
    for parent in list(bpy.data.collections):
        try:
            if coll.name in parent.children:
                parent.children.unlink(coll)
        except Exception:
            pass
    try:
        if coll.name in bpy.context.scene.collection.children:
            bpy.context.scene.collection.children.unlink(coll)
    except Exception:
        pass
    try:
        if coll.users == 0:
            bpy.data.collections.remove(coll)
    except Exception:
        pass
    try:
        p.hair_collection = None
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Hair and dust
# ---------------------------------------------------------------------------

def _surface_samples(bm):
    tris = bm.calc_loop_triangles()
    areas = []
    for t in tris:
        a, b, c = t[0].vert.co, t[1].vert.co, t[2].vert.co
        areas.append((b - a).cross(c - a).length * 0.5)
    return tris, areas, sum(areas)


def _scatter(tris, areas, total, density, rng):
    count = min(int(total * density), MAX_INST)
    if count <= 0 or not tris:
        return []
    keep, out = _KEEP[0], []
    for ti in rng.choices(range(len(tris)), weights=areas, k=count):
        t = tris[ti]
        a, b, c = t[0].vert.co, t[1].vert.co, t[2].vert.co
        r1, r2 = math.sqrt(rng.random()), rng.random()
        pt = a * (1 - r1) + b * (r1 * (1 - r2)) + c * (r1 * r2)
        if keep is not None and not keep(pt):
            continue
        out.append((pt, t[0].face.normal.copy()))
    return out


def _rvec(rng):
    while True:
        v = Vector((rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(-1, 1)))
        if 0.01 < v.length_squared <= 1.0:
            return v.normalized()


def build_hair(fbm, surf, p, density_scale=1.0):
    """Build a smooth, tapered visual strand layer for use beside editable hair particles."""
    tris, areas, total = surf
    if not tris or total < 1e-12:
        return
    rng = random.Random(p.hair_seed)
    density = max(0.0, p.hair_density * density_scale)
    segments = 5
    for pt, n in _scatter(tris, areas, total, density, rng):
        length = max(p.hair_length * (1 + p.hair_length_var * rng.uniform(-1, 1)), 1e-6)
        normal = n.normalized() if n.length > 1e-9 else Vector((0, 0, 1))
        dirv = (normal + _rvec(rng) * (p.hair_randomness * 0.55)).normalized()
        bend = _rvec(rng)
        bend = bend - dirv * bend.dot(dirv)
        bend = bend.normalized() * (p.hair_curl * length * 0.65) if bend.length > 1e-6 else Vector((0, 0, 0))

        side_a = dirv.cross(_rvec(rng))
        if side_a.length < 1e-6:
            side_a = dirv.cross(Vector((1, 0, 0)))
        side_a.normalize()
        side_b = dirv.cross(side_a).normalized()
        # Two crossed, tapered ribbons catch light from more viewing angles than
        # a single flat strip while keeping the accessory mesh reasonably light.
        for side in (side_a, side_b):
            rows = []
            for i in range(segments):
                t = i / segments
                pos = pt + dirv * (length * t) + bend * (t * t)
                taper = (1.0 - t) ** 1.25
                half_width = max(p.hair_width * 0.5 * taper, p.hair_width * 0.012)
                rows.append((fbm.verts.new(pos - side * half_width),
                             fbm.verts.new(pos + side * half_width)))
            tip = fbm.verts.new(pt + dirv * length + bend)
            for i in range(segments - 1):
                try:
                    face = fbm.faces.new((rows[i][0], rows[i][1], rows[i + 1][1], rows[i + 1][0]))
                    face.smooth = True
                except ValueError:
                    pass
            try:
                face = fbm.faces.new((rows[-1][0], rows[-1][1], tip))
                face.smooth = True
            except ValueError:
                pass


def build_dust(fbm, surf, p, density_scale=1.0):
    """Build rounded, shaded dust specks as a visual layer beside editable particles."""
    tris, areas, total = surf
    if not tris or total < 1e-12:
        return
    rng = random.Random(p.dust_seed)
    phi = (1.0 + math.sqrt(5.0)) * 0.5
    ico = [Vector(v) for v in (
        (-1, phi, 0), (1, phi, 0), (-1, -phi, 0), (1, -phi, 0),
        (0, -1, phi), (0, 1, phi), (0, -1, -phi), (0, 1, -phi),
        (phi, 0, -1), (phi, 0, 1), (-phi, 0, -1), (-phi, 0, 1))]
    for v in ico:
        v.normalize()
    ico_faces = (
        (0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11),
        (1, 5, 9), (5, 11, 4), (11, 10, 2), (10, 7, 6), (7, 1, 8),
        (3, 9, 4), (3, 4, 2), (3, 2, 6), (3, 6, 8), (3, 8, 9),
        (4, 9, 5), (2, 4, 11), (6, 2, 10), (8, 6, 7), (9, 8, 1))
    density = max(0.0, p.dust_density * density_scale)
    for pt, n in _scatter(tris, areas, total, density, rng):
        r = max(p.dust_size * (1 + p.dust_size_var * rng.uniform(-1, 1)), 1e-6) * 0.5
        rot = Euler((rng.uniform(0, 6.283), rng.uniform(0, 6.283), rng.uniform(0, 6.283)))
        vs = []
        for base in ico:
            q = base.copy()
            q.rotate(rot)
            vs.append(fbm.verts.new(pt + n.normalized() * (r * 0.12) + q * r))
        for idx in ico_faces:
            try:
                face = fbm.faces.new([vs[i] for i in idx])
                face.smooth = True
            except ValueError:
                pass
    if fbm.faces:
        bmesh.ops.recalc_face_normals(fbm, faces=fbm.faces[:])


# ---------------------------------------------------------------------------
# Regeneration / object children
# ---------------------------------------------------------------------------

CHILD_ATTRS = ("hair_obj", "dust_obj", "hair_geo_obj", "dust_geo_obj", "border_obj", "cutter_obj", "fuzz_obj")


_CHILD_SUFFIX = {
    "hair_obj": "_HairParticles",
    "dust_obj": "_DustParticles",
    "hair_geo_obj": "_Hair",
    "dust_geo_obj": "_Dust",
    "border_obj": "_Border",
    "cutter_obj": "_Cutter",
    "fuzz_obj": "_Fuzz",
}


def _find_child(obj, attr):
    """Find an existing generated child even if its PointerProperty was lost."""
    suffix = _CHILD_SUFFIX.get(attr)
    if obj is None or not suffix:
        return None
    pattern = re.compile(re.escape(suffix) + r"(?:\.\d{3})?$")
    for child in obj.children:
        if child.type == 'MESH' and pattern.search(child.name):
            return child
    return None


def _remove_child(p, attr):
    ob = getattr(p, attr, None)
    owner = getattr(p, "id_data", None)
    if ob is None and isinstance(owner, bpy.types.Object):
        ob = _find_child(owner, attr)
    if ob:
        me = ob.data
        bpy.data.objects.remove(ob, do_unlink=True)
        if me and me.users == 0:
            bpy.data.meshes.remove(me)
    try:
        setattr(p, attr, None)
    except Exception:
        pass


def _write_child(obj, p, attr, suffix, cbm, mat):
    ob = getattr(p, attr, None)
    if ob is None or ob.name not in bpy.data.objects or ob.parent != obj:
        ob = _find_child(obj, attr)
    if ob is None:
        name = obj.name + suffix
        me = bpy.data.meshes.new(name)
        ob = bpy.data.objects.new(name, me)
        cols = obj.users_collection
        (cols[0] if cols else bpy.context.scene.collection).objects.link(ob)
        ob.parent = obj
    setattr(p, attr, ob)
    me = ob.data
    me.clear_geometry()
    cbm.to_mesh(me)
    me.materials.clear()
    if mat:
        me.materials.append(mat)
    me.update()
    return ob


def _layer(obj, p, surf, enabled, attr, suffix, builder, mat):
    if not enabled or not surf[0]:
        _remove_child(p, attr)
        return
    cbm = bmesh.new()
    try:
        builder(cbm, surf, p)
        _write_child(obj, p, attr, suffix, cbm, mat)
    finally:
        cbm.free()


def _style_cutter(p):
    co = p.cutter_obj
    if co is None:
        return
    co.display_type = 'WIRE'
    co.hide_render = True
    try:
        co.hide_set(not p.show_cutter)
    except Exception:
        pass


def _boolean_mod_name(filter_obj, p):
    name = getattr(p, "boolean_modifier_name", "")
    if not name:
        name = "%s [%s]" % (BOOL_NAME, filter_obj.name)
        try:
            p.boolean_modifier_name = name
        except Exception:
            pass
    return name


def _remove_source_boolean(filter_obj, p):
    source = getattr(p, "source_obj", None)
    if source is not None and source.type == 'MESH':
        name = _boolean_mod_name(filter_obj, p)
        mod = source.modifiers.get(name)
        if mod is not None:
            source.modifiers.remove(mod)
    # Clean up the old v3.5 behavior if this filter file was upgraded in-place.
    legacy = filter_obj.modifiers.get(BOOL_NAME)
    if legacy is not None and legacy.type == 'BOOLEAN':
        filter_obj.modifiers.remove(legacy)


def sync_boolean(obj, p):
    """Cut the original source mesh with the generated bounds, not the filter mesh."""
    source = getattr(p, "source_obj", None)
    host = source if source is not None and source.type == 'MESH' else None
    target = p.boolean_obj if p.boolean_source == 'OBJECT' else p.cutter_obj
    name = _boolean_mod_name(obj, p)
    mod = host.modifiers.get(name) if host is not None else None

    # Remove any legacy modifier previously placed on the generated filter itself.
    legacy = obj.modifiers.get(BOOL_NAME)
    if legacy is not None and legacy.type == 'BOOLEAN':
        obj.modifiers.remove(legacy)

    valid = (p.use_boolean and host is not None and target is not None and target != host
             and target != obj and target.name in bpy.data.objects and target.type == 'MESH')
    if not valid:
        if mod is not None:
            host.modifiers.remove(mod)
        return

    if mod is None:
        mod = host.modifiers.new(name, 'BOOLEAN')
    mod.operation = p.boolean_op
    mod.solver = p.boolean_solver
    try:
        mod.operand_type = 'OBJECT'
    except Exception:
        pass
    mod.object = target

    # The source may have been hidden by the source-generation operator.
    try:
        host.hide_set(False)
    except Exception:
        pass


def _source_normals_for_faces(src):
    """Normals of the reference faces, used to exclude shell side-walls from emitters."""
    tmp = None
    try:
        tmp, _comps = _faces_bmesh(src)
        return [f.normal.normalized().copy() for f in tmp.faces if f.normal.length > 1e-8]
    except Exception:
        return []
    finally:
        if tmp is not None:
            tmp.free()


def _outside_surface_faces(bm, p, src):
    """Return exposed pleat-skin faces, excluding solidified perimeter/thickness walls."""
    bm.normal_update()
    if p.mode == 'CURVE':
        # The pleated sheet runs along height_dir. Its face normals are perpendicular
        # to that axis; solidify end caps point mostly along it.
        d = _height_dir(p)
        return [f for f in bm.faces if f.normal.length > 1e-8
                and abs(f.normal.normalized().dot(d)) < 0.68]

    ref_normals = _source_normals_for_faces(src)
    if not ref_normals:
        # Fallback: use all faces whose normal is not dominated by the shell edge axis.
        return [f for f in bm.faces if f.normal.length > 1e-8]
    out = []
    for f in bm.faces:
        if f.normal.length < 1e-8:
            continue
        fn = f.normal.normalized()
        # A skin face stays broadly aligned with at least one selected source face.
        # Side-wall faces are tangent to the source faces and usually fail this test.
        if max((abs(fn.dot(n)) for n in ref_normals), default=0.0) >= 0.48:
            out.append(f)
    return out


def _particle_surface_bmesh(bm, selected_faces):
    """Copy selected faces into a standalone emitter mesh without shell side-walls."""
    out = bmesh.new()
    vmap = {}
    for f in selected_faces:
        verts = []
        for v in f.verts:
            if v not in vmap:
                vmap[v] = out.verts.new(v.co)
            verts.append(vmap[v])
        try:
            nf = out.faces.new(verts)
            nf.smooth = f.smooth
            nf.material_index = f.material_index
        except ValueError:
            pass
    out.normal_update()
    return out


def _ensure_particle_system(emitter, system_name):
    """Get or add a classic editable particle system on a mesh object."""
    for psys in emitter.particle_systems:
        if psys.name == system_name:
            return psys

    context = bpy.context
    view_layer = context.view_layer
    previous_active = view_layer.objects.active
    previous_selected = list(context.selected_objects)
    try:
        for item in previous_selected:
            try:
                item.select_set(False)
            except Exception:
                pass
        emitter.hide_set(False)
        emitter.select_set(True)
        view_layer.objects.active = emitter
        with context.temp_override(
                object=emitter,
                active_object=emitter,
                selected_objects=[emitter],
                selected_editable_objects=[emitter]):
            result = bpy.ops.object.particle_system_add()
        if 'FINISHED' not in result or not emitter.particle_systems:
            raise RuntimeError("Blender could not add the %s particle system." % system_name)
        psys = emitter.particle_systems[-1]
        psys.name = system_name
        try:
            psys.settings.name = system_name + " Settings"
        except Exception:
            pass
        return psys
    finally:
        try:
            emitter.select_set(False)
        except Exception:
            pass
        for item in previous_selected:
            try:
                if item.name in bpy.data.objects:
                    item.select_set(True)
            except Exception:
                pass
        try:
            view_layer.objects.active = previous_active
        except Exception:
            pass


def _dust_instance(material):
    """Hidden viewport-only icosphere used as the instanced dust speck."""
    name = "Filter Maker Dust Instance"
    inst = bpy.data.objects.get(name)
    if inst is None or inst.type != 'MESH':
        mesh = bpy.data.meshes.get(name + " Mesh")
        if mesh is None:
            mesh = bpy.data.meshes.new(name + " Mesh")
            mb = bmesh.new()
            try:
                bmesh.ops.create_icosphere(mb, subdivisions=2, radius=0.5)
                for face in mb.faces:
                    face.smooth = True
                mb.to_mesh(mesh)
            finally:
                mb.free()
        inst = bpy.data.objects.new(name, mesh)
        (bpy.context.scene.collection or bpy.context.collection).objects.link(inst)
    inst.location = (100000.0, 100000.0, 100000.0)
    inst.hide_render = False
    try:
        inst.hide_set(True)
    except Exception:
        pass
    mesh = inst.data
    mesh.materials.clear()
    if material is not None:
        mesh.materials.append(material)
    return inst


def _configure_particle_system(psys, p, count, kind, material):
    settings = psys.settings
    settings.type = 'HAIR'
    settings.count = max(0, min(MAX_INST, int(count)))
    try:
        psys.seed = int(p.hair_seed if kind == 'HAIR' else p.dust_seed)
    except Exception:
        pass
    if hasattr(settings, "emit_from"):
        settings.emit_from = 'FACE'
    if hasattr(settings, "distribution"):
        settings.distribution = 'JIT'
    if hasattr(settings, "use_even_distribution"):
        settings.use_even_distribution = True
    if hasattr(settings, "display_percentage"):
        settings.display_percentage = 100
    if hasattr(settings, "display_method"):
        settings.display_method = 'RENDER'
    if hasattr(settings, "draw_step"):
        settings.draw_step = 3
    if hasattr(settings, "hair_step"):
        # More interpolation steps keep short hairs smooth without a large cost.
        settings.hair_step = 4 if kind == 'HAIR' else 2
    if hasattr(settings, "use_hair_bspline"):
        settings.use_hair_bspline = True
    # Property spelling differs across Blender versions; disable the source skin
    # while keeping the particle instances/paths visible.
    for attr in ("use_render_emitter", "show_instancer_for_render"):
        if hasattr(settings, attr):
            try:
                setattr(settings, attr, False)
            except Exception:
                pass
    for attr in ("show_instancer_for_viewport", "show_emitter"):
        if hasattr(settings, attr):
            try:
                setattr(settings, attr, False)
            except Exception:
                pass
    if hasattr(settings, "use_modifier_stack"):
        settings.use_modifier_stack = False

    if kind == 'HAIR':
        if hasattr(settings, "render_type"):
            settings.render_type = 'PATH'
        settings.hair_length = max(p.hair_length, 1e-6)
        if hasattr(settings, "length_random"):
            settings.length_random = min(1.0, max(0.0, p.hair_length_var))
        if hasattr(settings, "particle_size"):
            settings.particle_size = 1.0
        if hasattr(settings, "radius_scale"):
            # Width should scale against strand length. The old width*20 mapping
            # produced almost invisible filaments for normal-sized filters.
            ratio = p.hair_width / max(p.hair_length, 1e-6)
            settings.radius_scale = min(0.5, max(0.015, ratio * 3.0))
        if hasattr(settings, "root_radius"):
            settings.root_radius = 1.0
        if hasattr(settings, "tip_radius"):
            settings.tip_radius = 0.08
        if hasattr(settings, "normal_factor"):
            settings.normal_factor = max(0.0, p.hair_randomness) * 0.15
        if hasattr(settings, "kink"):
            settings.kink = 'CURL' if p.hair_curl > 1e-4 else 'NO'
        if hasattr(settings, "kink_amplitude"):
            settings.kink_amplitude = max(0.0, p.hair_curl) * max(p.hair_length, 1e-6) * 0.45
        if hasattr(settings, "kink_frequency"):
            settings.kink_frequency = 2.5
    else:
        if hasattr(settings, "render_type"):
            settings.render_type = 'OBJECT'
        if hasattr(settings, "instance_object"):
            settings.instance_object = _dust_instance(material)
        if hasattr(settings, "hair_length"):
            settings.hair_length = 1e-5
        if hasattr(settings, "particle_size"):
            settings.particle_size = max(p.dust_size, 1e-6)
        if hasattr(settings, "size_random"):
            settings.size_random = min(1.0, max(0.0, p.dust_size_var))
        if hasattr(settings, "use_rotations"):
            settings.use_rotations = True
        if hasattr(settings, "use_scale_instance"):
            settings.use_scale_instance = True
        if hasattr(settings, "rotation_mode"):
            settings.rotation_mode = 'NOR'
        if hasattr(settings, "phase_factor_random"):
            settings.phase_factor_random = 1.0

    # Particles use the assigned mesh material slot when this property is supported.
    if material is not None and hasattr(settings, "material"):
        try:
            settings.material = 1
        except Exception:
            pass


def _update_particle_emitter(obj, p, src, bm, attr, suffix, system_name, kind, enabled, material):
    if not enabled:
        _remove_child(p, attr)
        return
    selected = _outside_surface_faces(bm, p, src)
    if not selected:
        _remove_child(p, attr)
        return

    surface = _particle_surface_bmesh(bm, selected)
    try:
        area = sum(f.calc_area() for f in surface.faces)
        if not surface.faces or area <= 1e-12:
            _remove_child(p, attr)
            return
        emitter = _write_child(obj, p, attr, suffix, surface, material)
        emitter.name = obj.name + suffix
        emitter.display_type = 'WIRE'
        emitter.hide_render = False
        # Blender exposes the "Show Emitter" controls on Object in newer APIs.
        # Keep the duplicate skin hidden while leaving its particle instances visible.
        for attr_name in ("show_instancer_for_render", "show_instancer_for_viewport"):
            if hasattr(emitter, attr_name):
                try:
                    setattr(emitter, attr_name, False)
                except Exception:
                    pass
        try:
            emitter.hide_set(False)
        except Exception:
            pass
        psys = _ensure_particle_system(emitter, system_name)
        density = (p.hair_density * 0.65) if kind == 'HAIR' else (p.dust_density * 0.70)
        count = min(MAX_INST, max(0, int(round(area * max(0.0, density)))))
        _configure_particle_system(psys, p, count, kind, material)
        try:
            emitter.data.update()
        except Exception:
            pass
    finally:
        surface.free()


def _update_detail_geometry(obj, p, src, bm, attr, suffix, builder, enabled, material, density_scale):
    """Refresh high-quality visible strand/speck geometry on the outer filter skin."""
    if not enabled:
        _remove_child(p, attr)
        return
    selected = _outside_surface_faces(bm, p, src)
    if not selected:
        _remove_child(p, attr)
        return
    surface = _particle_surface_bmesh(bm, selected)
    detail_bm = bmesh.new()
    try:
        samples = _surface_samples(surface)
        builder(detail_bm, samples, p, density_scale=density_scale)
        if not detail_bm.faces:
            _remove_child(p, attr)
            return
        detail_bm.normal_update()
        accessory = _write_child(obj, p, attr, suffix, detail_bm, material)
        accessory.name = obj.name + suffix
        accessory.display_type = 'TEXTURED'
        accessory.hide_render = False
        try:
            accessory.hide_set(False)
        except Exception:
            pass
    finally:
        detail_bm.free()
        surface.free()


def regenerate(obj):
    p = getattr(obj, "filter_maker", None)
    if p is None or not p.is_filter or obj.mode != 'OBJECT':
        return
    p.last_error = ""
    bm = rb = None
    try:
        src = json.loads(p.src)
        bm = bmesh.new()
        rb = bmesh.new() if p.use_border else None
        if p.mode == 'CURVE':
            build_curve(bm, p, src, rb)
            if p.mesh_type == 'TRIS' and bm.faces:
                bmesh.ops.triangulate(bm, faces=bm.faces[:])
        else:
            build_face(bm, p, src, rb)
        if p.use_thickness and p.thickness > 1e-7 and bm.faces:
            bmesh.ops.solidify(bm, geom=list(bm.faces), thickness=p.thickness)
        bm.normal_update()
        apply_noise(bm, p)
        bm.normal_update()
        keep = None
        if p.use_boolean and p.boolean_source != 'OBJECT':
            cb, keep = build_cutter(p, src)
            try:
                _write_child(obj, p, "cutter_obj", "_Cutter", cb, None)
            finally:
                cb.free()
            _style_cutter(p)
        else:
            _remove_child(p, "cutter_obj")
        if p.fuzz_obj:
            _remove_child(p, "fuzz_obj")
        # Use actual editable Blender particle systems on emitter meshes made from
        # the outside skin only. These child objects remain after Finish (baking).
        try:
            # The accessory meshes restore smooth, tapered strands and rounded dust
            # specks. The separate particle emitters remain editable in Blender and
            # use the same exposed-surface selection after Finish (baking).
            mesh_layers = (
                ("dust_geo_obj", "_Dust", build_dust, p.use_dust, p.dust_material, 0.30),
            )
            for attr, suffix, builder, enabled, material, detail_density in mesh_layers:
                try:
                    _update_detail_geometry(obj, p, src, bm, attr, suffix,
                                            builder, enabled, material, detail_density)
                except Exception as detail_exc:
                    traceback.print_exc()
                    p.last_error = "Hair/dust geometry update failed: %s" % detail_exc

            for args in (
                (obj, p, src, bm, "dust_obj", "_DustParticles", "Filter Maker Dust",
                 "DUST", p.use_dust, p.dust_material),
            ):
                try:
                    _update_particle_emitter(*args)
                except Exception as particle_exc:
                    # A particle API/context issue must not prevent the filter mesh
                    # itself from regenerating. Report it in the panel for diagnosis.
                    traceback.print_exc()
                    p.last_error = "Particle system update failed: %s" % particle_exc
        finally:
            _KEEP[0] = None
        if rb is not None and rb.faces:
            fb = build_border(p, rb)
            if fb is not None:
                try:
                    apply_border_shading(fb, p)
                    _write_child(obj, p, "border_obj", "_Border", fb, p.border_material)
                finally:
                    fb.free()
            else:
                _remove_child(p, "border_obj")
        else:
            _remove_child(p, "border_obj")
        apply_shading(bm, p)
        me = obj.data
        me.clear_geometry()
        bm.to_mesh(me)
        me.materials.clear()
        if p.filter_material:
            me.materials.append(p.filter_material)
        if hasattr(me, "use_auto_smooth"):
            try:
                me.use_auto_smooth = bool(p.auto_smooth)
                if p.auto_smooth:
                    me.auto_smooth_angle = p.auto_smooth_angle
            except Exception:
                pass
        me.update()
        try:
            _update_hair_particles_on_filter(obj, p)
        except Exception as hair_exc:
            traceback.print_exc()
            p.last_error = "Curve hair update failed: %s" % hair_exc
        sync_boolean(obj, p)
    except Exception as exc:
        traceback.print_exc()
        try:
            p.last_error = str(exc)
        except Exception:
            pass
    finally:
        if bm is not None:
            bm.free()
        if rb is not None:
            rb.free()
        redraw_all()


_curve_sync_busy = [False]


def refresh_curve_source(filter_obj):
    """Refresh a generated filter's stored path from its linked editable curve."""
    p = getattr(filter_obj, "filter_maker", None)
    if p is None or p.mode != 'CURVE':
        return False
    source = getattr(p, "source_obj", None)
    if source is None or source.type != 'CURVE':
        return False
    try:
        new_src = paths_from_curve(source)
        try:
            old_src = json.loads(p.src) if p.src else None
        except Exception:
            old_src = None
        if new_src == old_src:
            return False
        p.src = json.dumps(new_src)
        return True
    except Exception:
        traceback.print_exc()
        return False


@persistent
def _sync_curve_sources(scene, depsgraph):
    """Refresh linked curve filters during curve edits without creating new objects.

    Comparing evaluated dependency-graph pointers alone is unreliable across Blender
    versions. Instead, resample each live-linked curve; refresh_curve_source compares
    the compact source data and regenerates only when its actual path has changed.
    """
    if _curve_sync_busy[0] or _busy[0]:
        return
    _curve_sync_busy[0] = True
    try:
        for filter_obj in list(bpy.data.objects):
            if filter_obj.type != 'MESH' or filter_obj.mode != 'OBJECT':
                continue
            p = getattr(filter_obj, "filter_maker", None)
            if p is None or not p.is_filter or not p.live or p.mode != 'CURVE':
                continue
            source = getattr(p, "source_obj", None)
            if source is None or source.type != 'CURVE':
                continue
            if refresh_curve_source(filter_obj):
                regenerate(filter_obj)
    except Exception:
        traceback.print_exc()
    finally:
        _curve_sync_busy[0] = False


def _upd(self, context):
    if _busy[0] or not self.is_filter or not self.live:
        return
    ob = self.id_data
    if isinstance(ob, bpy.types.Object):
        regenerate(ob)


def _upd_thickness_enabled(self, context):
    """Enable thickness with a useful starting amount instead of a zero-width shell."""
    if _busy[0]:
        return
    if getattr(self, "use_thickness", False) and getattr(self, "thickness", 0.0) <= 1e-8:
        scale = 1.0
        try:
            if self.src:
                scale = _src_size(self.mode, json.loads(self.src))
        except Exception:
            scale = 1.0
        _busy[0] = True
        try:
            self.thickness = 0.004 * max(scale, 1e-3)
        finally:
            _busy[0] = False
    _upd(self, context)


def _upd_bool(self, context):
    if _busy[0] or not self.is_filter:
        return
    ob = self.id_data
    if isinstance(ob, bpy.types.Object):
        regenerate(ob)


def _upd_cutter_vis(self, context):
    _style_cutter(self)


def _poll_mesh(self, obj):
    try:
        return obj.type == 'MESH' and obj != self.id_data and not getattr(getattr(obj, "filter_maker", None), "is_filter", False)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

def _len(name, default, desc, mn=0.0):
    return FloatProperty(name=name, default=default, min=mn, description=desc,
                         subtype='DISTANCE', unit='LENGTH', precision=4, update=_upd)


class FilterProps(PropertyGroup):
    is_filter: BoolProperty(default=False)
    mode: EnumProperty(name="Source Type", items=[
        ('CURVE', "Curve / Edges", "Pleat along a curve or selected edge chain", ic('CURVE_DATA'), 0),
        ('FACE', "Faces", "Pleat a selected surface", ic('UV_FACESEL'), 1)], default='CURVE')
    src: StringProperty(options={'HIDDEN'})
    live: BoolProperty(name="Live Update", default=True,
                       description="Regenerate while dragging sliders (turn off for heavy filters)")
    last_error: StringProperty(options={'HIDDEN'})
    source_obj: PointerProperty(type=bpy.types.Object)

    # Pleats
    use_thickness: BoolProperty(name="Use Thickness", default=False, update=_upd_thickness_enabled,
                                description="Give the filter media a solid shell; off = zero thickness")
    thickness: _len("Thickness", 0.0, "Thickness of the filter media", 0.0)
    pleat_depth: _len("Pleat Depth", 0.025, "Distance from centre line to each ridge / valley")
    pleat_density: FloatProperty(name="Pleat Density", default=55.0, min=0.05, soft_max=200.0,
                                 description="Pleats (one ridge + one valley) per unit length", update=_upd)
    pleat_phase: FloatProperty(name="Pleat Offset", default=0.0, min=0.0, max=1.0,
                               description="Slide the pleat pattern by a fraction of one pleat", update=_upd)
    detail: FloatProperty(name="Surface Detail", default=20.0, min=0.05, soft_max=200.0,
                          description="Cuts per unit length", update=_upd)
    height: _len("Height", 1.0, "Extrusion height", 0.001)
    height_dir: FloatVectorProperty(name="Height Direction", size=3, default=(0, 0, 1),
                                    subtype='DIRECTION', update=_upd)
    pleat_angle: FloatProperty(name="Pleat Angle", default=0.0, subtype='ANGLE', unit='ROTATION',
                               description="Rotate pleat direction on faces", update=_upd)
    mesh_type: EnumProperty(name="Topology", items=[
        ('QUADS', "Quads", "Quad topology; split n-gons only where needed", ic('MESH_GRID'), 0),
        ('TRIS', "Triangles", "Fully triangulated mesh", ic('MOD_TRIANGULATE'), 1)], default='QUADS', update=_upd)
    face_smooth: BoolProperty(
        name="Smooth Face Transitions", default=True, update=_upd,
        description="Subdivide and soften internal transitions in connected face selections before pleating")
    face_smooth_factor: FloatProperty(
        name="Smoothing", default=0.45, min=0.0, max=1.0, soft_max=0.8, update=_upd,
        description="Strength of the source-surface smoothing; outer selection boundary stays fixed")
    face_smooth_cuts: IntProperty(
        name="Smoothing Cuts", default=2, min=1, max=4, update=_upd,
        description="Extra source-surface subdivisions used to round faceted transitions")

    # Pleat bevel
    bevel_ridge: FloatProperty(name="Ridge Bevel", default=0.4, min=0, max=1, update=_upd)
    bevel_valley: FloatProperty(name="Valley Bevel", default=0.4, min=0, max=1, update=_upd)
    bevel_cuts: IntProperty(name="Bevel Cuts", default=3, min=1, max=16, update=_upd)

    # Shading
    smooth: BoolProperty(name="Smooth Shading", default=False, update=_upd)
    auto_smooth: BoolProperty(name="Auto Smooth", default=True, update=_upd)
    auto_smooth_angle: FloatProperty(name="Angle", default=math.radians(35), min=0, max=math.pi,
                                     subtype='ANGLE', unit='ROTATION', update=_upd)

    # Noise / wear
    noise_strength: _len("Strength", 0.0015, "Displacement strength")
    noise_scale: _len("Size", 0.25, "Noise size", 0.0001)
    noise_depth: IntProperty(name="Depth", default=2, min=0, max=10, update=_upd)
    noise_type: EnumProperty(name="Noise", items=[
        ('SOFT', "Soft", "Smooth, natural displacement", ic('SHADING_RENDERED'), 0),
        ('HARD', "Hard", "Sharper, more broken-up displacement", ic('MOD_WIREFRAME'), 1)], default='SOFT', update=_upd)
    noise_basis: EnumProperty(name="Basis", items=[
        ('BLENDER', "Blender Original", "Classic Blender texture", ic('TEXTURE'), 0),
        ('PERLIN_NEW', "Improved Perlin", "Smooth improved Perlin field", ic('MOD_NOISE'), 1),
        ('PERLIN_ORIGINAL', "Original Perlin", "Original Perlin field", ic('MOD_DISPLACE'), 2),
        ('VORONOI_F1', "Voronoi F1", "Cellular distance field", ic('MESH_ICOSPHERE'), 3),
        ('VORONOI_F2F1', "Voronoi F2-F1", "Cell edge detail", ic('MESH_UVSPHERE'), 4),
        ('VORONOI_CRACKLE', "Voronoi Crackle", "Cracked cellular pattern", ic('MESH_CIRCLE'), 5),
        ('CELLNOISE', "Cell Noise", "Discrete cellular noise", ic('MESH_GRID'), 6)], default='BLENDER', update=_upd)
    noise_direction: EnumProperty(name="Direction", items=[
        ('XYZ', "RGB to XYZ", "Displace in all directions", ic('ORIENTATION_GLOBAL'), 0),
        ('NORMAL', "Normal", "Displace along normals", ic('ORIENTATION_NORMAL'), 1)],
        default='XYZ', update=_upd)
    noise_seed: IntProperty(name="Seed", default=0, min=0, update=_upd)

    # Border
    use_border: BoolProperty(name="Border", default=False, update=_upd)
    border_width: _len("Width", 0.03, "Frame reach inward from reference edge", 0.0001)
    border_overhang: _len("Overhang", 0.0, "Outward frame overhang")
    border_profile: EnumProperty(name="Profile", items=[
        ('FLAT', "Flat", "Plain rectangular frame", ic('MESH_PLANE'), 0),
        ('CHAMFER', "Chamfer", "Angled outer edges", ic('MOD_BEVEL'), 1),
        ('ROUND', "Round", "Rounded outer edges", ic('MESH_CIRCLE'), 2),
        ('LIP', "Lip", "Tall outer rim with lower inner step", ic('MOD_SOLIDIFY'), 3)],
        default='FLAT', update=_upd)
    border_bevel: _len("Bevel Size", 0.01, "Chamfer / round radius, or lip step height")
    border_cuts: IntProperty(name="Round Cuts", default=3, min=1, max=12, update=_upd)
    border_fit: BoolProperty(name="Fit To Pleats", default=True, update=_upd)
    border_smooth: BoolProperty(name="Shade Smooth", default=True, update=_upd,
                                description="Enable smooth shading on the border frame")
    border_auto_smooth: BoolProperty(name="Auto Smooth", default=True, update=_upd,
                                     description="Keep frame edges sharp when their angle exceeds the threshold")
    border_auto_smooth_angle: FloatProperty(name="Angle", default=math.radians(35), min=0, max=math.pi,
                                            subtype='ANGLE', unit='ROTATION', update=_upd)
    border_front: _len("Front Extend", 0.01, "Extra frame thickness on the front")
    border_back: _len("Back Extend", 0.01, "Extra frame thickness on the back")

    # Hair
    use_hair: BoolProperty(name="Hair", default=False, update=_upd,
                           description="Generate editable hair particles on the exposed filter skin")
    hair_density: FloatProperty(name="Density", default=2000, min=0, soft_max=100000, update=_upd)
    hair_length: _len("Length", 0.02, "Strand length")
    hair_width: _len("Width", 0.0005, "Strand base width")
    hair_length_var: FloatProperty(name="Length Variation", default=0.5, min=0, max=1, update=_upd)
    hair_randomness: FloatProperty(name="Direction Randomness", default=0.6, min=0, soft_max=3, update=_upd)
    hair_curl: FloatProperty(name="Curl", default=0.3, min=0, soft_max=2, update=_upd)
    hair_seed: IntProperty(name="Seed", default=0, min=0, update=_upd)

    # Dust
    use_dust: BoolProperty(name="Dust", default=False, update=_upd,
                           description="Generate editable dust particles on the exposed filter skin")
    dust_density: FloatProperty(name="Density", default=3000, min=0, soft_max=100000, update=_upd)
    dust_size: _len("Size", 0.004, "Speck size")
    dust_size_var: FloatProperty(name="Size Variation", default=0.6, min=0, max=1, update=_upd)
    dust_seed: IntProperty(name="Seed", default=0, min=0, update=_upd)

    # Boolean
    use_boolean: BoolProperty(name="Boolean", default=False, update=_upd_bool)
    boolean_source: EnumProperty(name="Cutter", items=[
        ('BOX', "Bounding Box", "A clean box around the reference selection", ic('CUBE'), 0),
        ('ENVELOPE', "Envelope", "Reference shape thickened to cover the pleats", ic('MOD_ENVELOPE'), 1),
        ('OBJECT', "Object", "Any mesh object", ic('OUTLINER_DATA_MESH'), 2)], default='ENVELOPE', update=_upd_bool)
    boolean_obj: PointerProperty(name="Object", type=bpy.types.Object, poll=_poll_mesh, update=_upd_bool)
    boolean_modifier_name: StringProperty(options={'HIDDEN'})
    boolean_op: EnumProperty(name="Operation", items=[
        ('INTERSECT', "Intersect", "Keep only filter inside cutter", ic('SELECT_INTERSECT'), 0),
        ('DIFFERENCE', "Difference", "Cut cutter out of filter", ic('SELECT_SUBTRACT'), 1),
        ('UNION', "Union", "Merge with cutter", ic('SELECT_EXTEND'), 2)],
        default='DIFFERENCE', update=_upd_bool)
    boolean_solver: EnumProperty(name="Solver", items=[
        ('EXACT', "Exact", "More robust handling of overlapping geometry", ic('CHECKMARK'), 0),
        ('FAST', "Fast", "Quick preview solver", ic('MOD_TIME'), 1)], default='EXACT', update=_upd_bool)
    cutter_margin: FloatProperty(name="Trim Margin", default=0.0, soft_min=-1, soft_max=1,
                                 subtype='DISTANCE', unit='LENGTH', precision=4, update=_upd_bool)
    show_cutter: BoolProperty(name="Show Cutter", default=False, update=_upd_cutter_vis)

    # Materials / generated children
    filter_material: PointerProperty(name="Filter Material", type=bpy.types.Material, update=_upd)
    border_material: PointerProperty(name="Border Material", type=bpy.types.Material, update=_upd)
    hair_material: PointerProperty(name="Hair Material", type=bpy.types.Material, update=_upd)
    dust_material: PointerProperty(name="Dust Material", type=bpy.types.Material, update=_upd)
    hair_collection: PointerProperty(name="Hair Curve Library", type=bpy.types.Collection)
    hair_obj: PointerProperty(type=bpy.types.Object)  # legacy emitter child; cleaned up on regeneration
    dust_obj: PointerProperty(type=bpy.types.Object)  # editable particle emitter
    hair_geo_obj: PointerProperty(type=bpy.types.Object)  # smooth visible strand mesh
    dust_geo_obj: PointerProperty(type=bpy.types.Object)  # rounded visible dust mesh
    border_obj: PointerProperty(type=bpy.types.Object)
    cutter_obj: PointerProperty(type=bpy.types.Object)
    fuzz_obj: PointerProperty(type=bpy.types.Object)


# ---------------------------------------------------------------------------
# Presets (normalized to source dimensions)
# ---------------------------------------------------------------------------

PRESET_SUBDIR = "filter_maker_pro"
LEN_PROPS = {"thickness", "pleat_depth", "height", "noise_strength", "noise_scale", "border_width",
             "border_overhang", "border_bevel", "border_front", "border_back", "hair_length",
             "hair_width", "dust_size", "cutter_margin"}
INV_LEN = {"pleat_density", "detail"}
INV_AREA = {"hair_density", "dust_density"}
VEC_PROPS = {"height_dir"}
DEFAULTS_NORM = {
    "use_thickness": False, "thickness": 0.0, "pleat_depth": 0.025, "pleat_density": 55.0, "pleat_phase": 0.0,
    "detail": 20.0, "height": 1.0, "height_dir": [0, 0, 1], "pleat_angle": 0.0, "mesh_type": "QUADS",
    "face_smooth": True, "face_smooth_factor": 0.45, "face_smooth_cuts": 2,
    "bevel_ridge": 0.4, "bevel_valley": 0.4, "bevel_cuts": 3,
    "smooth": True, "auto_smooth": False, "auto_smooth_angle": math.radians(35),
    "noise_strength": 0.0015, "noise_scale": 0.25, "noise_depth": 1, "noise_type": "SOFT",
    "noise_basis": "BLENDER", "noise_direction": "XYZ", "noise_seed": 0,
    "use_border": False, "border_width": 0.03, "border_overhang": 0.0, "border_profile": "FLAT",
    "border_bevel": 0.01, "border_cuts": 3, "border_fit": True,
    "border_smooth": True, "border_auto_smooth": True,
    "border_auto_smooth_angle": math.radians(35),
    "border_front": 0.01, "border_back": 0.01,
    "use_hair": False, "hair_density": 2000.0, "hair_length": 0.02, "hair_width": 0.0005,
    "hair_length_var": 0.5, "hair_randomness": 0.6, "hair_curl": 0.3, "hair_seed": 0,
    "use_dust": False, "dust_density": 3000.0, "dust_size": 0.004, "dust_size_var": 0.6, "dust_seed": 0,
    "use_boolean": False, "boolean_source": "ENVELOPE", "boolean_op": "DIFFERENCE", "boolean_solver": "EXACT",
    "cutter_margin": 0.0,
}
PRESET_KEYS = tuple(DEFAULTS_NORM)


def _mk(**kw):
    d = dict(DEFAULTS_NORM)
    d.update(kw)
    return d


BUILTIN_PRESETS = {
    "Standard Pleated": _mk(pleat_density=55.0, pleat_depth=0.025, noise_strength=0.0015,
                             noise_scale=0.25, noise_depth=1),
    "Clean (no wear)": _mk(pleat_density=55.0, noise_strength=0.0, use_hair=False, use_dust=False),
    "HEPA Fine Pleat": _mk(pleat_density=80.0, pleat_depth=0.018, use_thickness=False, thickness=0.0,
                           bevel_ridge=0.25, bevel_valley=0.25, bevel_cuts=3,
                           noise_strength=0.0008, noise_scale=0.3, noise_depth=1, use_hair=False,
                           use_dust=False),
    "Coarse Furnace": _mk(pleat_density=38.0, pleat_depth=0.035, use_thickness=False, thickness=0.0,
                          bevel_ridge=0.55, bevel_valley=0.45, bevel_cuts=4,
                          noise_strength=0.0025, noise_scale=0.35, noise_depth=1,
                          use_hair=False, use_dust=False),
    "Sharp Paper Fold": _mk(pleat_density=60.0, bevel_ridge=0.0, bevel_valley=0.0, bevel_cuts=1,
                            use_thickness=False, thickness=0.0, noise_strength=0.0005,
                            use_hair=False, use_dust=False),
    "Dusty Used": _mk(pleat_density=55.0, pleat_depth=0.025, noise_strength=0.003,
                      noise_scale=0.3, noise_depth=1, use_hair=True, use_dust=True,
                      hair_density=2000.0, hair_length=0.015, hair_randomness=0.25,
                      hair_curl=0.12, dust_density=8000.0, dust_size=0.003,
                      dust_size_var=0.35),
}
_preset_busy = [False]
_preset_cache = []


def apply_values(p, data, sc):
    sc = max(sc, 1e-6)
    for k in PRESET_KEYS:
        if k not in data:
            continue
        v = data[k]
        try:
            if k in LEN_PROPS:
                v = v * sc
            elif k in INV_LEN:
                v = v / sc
            elif k in INV_AREA:
                v = v / (sc * sc)
            if k in VEC_PROPS:
                v = tuple(v)
            setattr(p, k, v)
        except Exception:
            traceback.print_exc()


def collect_values(p, sc):
    sc = max(sc, 1e-6)
    d = {}
    for k in PRESET_KEYS:
        v = getattr(p, k)
        if k in VEC_PROPS:
            v = [float(x) for x in v]
        elif isinstance(v, float):
            if k in LEN_PROPS:
                v /= sc
            elif k in INV_LEN:
                v *= sc
            elif k in INV_AREA:
                v *= sc * sc
            v = float(v)
        d[k] = v
    return d


def _user_dir(create=False):
    try:
        return bpy.utils.user_resource('SCRIPTS', path="presets/" + PRESET_SUBDIR, create=create)
    except Exception:
        return ""


def user_presets():
    d, out = _user_dir(), {}
    if d and os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.lower().endswith(".json"):
                out[os.path.splitext(fn)[0]] = os.path.join(d, fn)
    return out


def load_preset(ident):
    kind, _, name = ident.partition(":")
    if kind == "B":
        return BUILTIN_PRESETS.get(name)
    path = user_presets().get(name)
    if not path:
        return None
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("values", data)


def save_preset(name, values):
    safe = re.sub(r"[^\w\- ]", "_", name).strip() or "Preset"
    d = _user_dir(create=True)
    if not d:
        raise RuntimeError("Cannot locate Blender presets folder")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, safe + ".json"), "w", encoding="utf-8") as fh:
        json.dump({"name": safe, "version": 1, "values": values}, fh, indent=2)
    return safe


def preset_items(self, context):
    items = [("B:" + n, n, "Built-in preset", 'PRESET', i) for i, n in enumerate(BUILTIN_PRESETS)]
    base = len(items)
    for j, n in enumerate(user_presets()):
        items.append(("U:" + n, n, "Your saved preset", 'USER', base + j))
    _preset_cache[:] = items
    if not items:
        items.append(("B:Standard Pleated", "Standard Pleated", "Built-in preset", 'PRESET', 0))
    return _preset_cache


def _set_selected_preset(ident):
    _preset_busy[0] = True
    try:
        bpy.context.window_manager.filter_maker_preset = ident
    except Exception:
        pass
    finally:
        _preset_busy[0] = False


def apply_preset(ob, ident):
    data = load_preset(ident)
    if not data:
        return False
    p = ob.filter_maker
    refresh_curve_source(ob)
    sc = _src_size(p.mode, json.loads(p.src))
    _busy[0] = True
    try:
        apply_values(p, data, sc)
    finally:
        _busy[0] = False
    regenerate(ob)
    return True


def _preset_changed(self, context):
    if _preset_busy[0]:
        return
    ob = active_filter(context)
    if ob is not None:
        # Presets only mutate the current filter's properties and mesh in place.
        # Never call create_filter() from this callback.
        apply_preset(ob, self.filter_maker_preset)


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

def _default_mat(name, color):
    m = bpy.data.materials.get(name)
    if m is None:
        m = bpy.data.materials.new(name)
        m.diffuse_color = color
    return m


def create_filter(context, mode, src, source=None):
    sc = _src_size(mode, src)
    me = bpy.data.meshes.new("Filter")
    ob = bpy.data.objects.new("Filter", me)
    (context.collection or context.scene.collection).objects.link(ob)
    _busy[0] = True
    try:
        p = ob.filter_maker
        p.is_filter = True
        p.mode = mode
        p.src = json.dumps(src)
        apply_values(p, DEFAULTS_NORM, sc)
        p.filter_material = _default_mat("Filter_Material", (0.85, 0.83, 0.75, 1.0))
        p.border_material = _default_mat("Border_Material", (0.25, 0.27, 0.3, 1.0))
        p.hair_material = _default_mat("Hair_Material", (0.9, 0.9, 0.9, 1.0))
        p.dust_material = _default_mat("Dust_Material", (0.45, 0.4, 0.35, 1.0))
        if source is not None:
            p.source_obj = source
            # The source mesh is the Boolean target, not the Object-mode cutter.
            # Leave boolean_obj empty until the user chooses a separate operand.
    finally:
        _busy[0] = False
    regenerate(ob)
    for o in context.view_layer.objects:
        o.select_set(False)
    ob.select_set(True)
    context.view_layer.objects.active = ob
    redraw_all()
    return ob


class FILTER_OT_new_line(Operator):
    """Create a straight line at the 3D cursor and build a filter."""
    bl_idname = "filter.new_line"
    bl_label = "New Filter Line"
    bl_options = {'REGISTER', 'UNDO'}
    length: FloatProperty(name="Length", default=1.0, min=0.01, subtype='DISTANCE')

    def execute(self, context):
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        c, h = context.scene.cursor.location, self.length / 2
        src = {"paths": [[_r((c.x - h, c.y, c.z)), _r((c.x + h, c.y, c.z))]], "closed": [False]}
        ob = create_filter(context, 'CURVE', src)
        if ob.filter_maker.last_error:
            self.report({'ERROR'}, ob.filter_maker.last_error)
        return {'FINISHED'}


class FILTER_OT_from_selection(Operator):
    """Build a filter from the active curve or mesh selection."""
    bl_idname = "filter.from_selection"
    bl_label = "Filter From Selection"
    bl_options = {'REGISTER', 'UNDO'}
    source: EnumProperty(name="Source", items=[('CURVE', "Curve / Edges", ""), ('FACE', "Faces", "")], default='CURVE')
    hide_source: BoolProperty(name="Hide Source", default=True)

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type in {'MESH', 'CURVE'}

    def execute(self, context):
        obj = context.active_object
        try:
            if self.source == 'FACE':
                if obj.type != 'MESH':
                    self.report({'ERROR'}, "Faces mode needs a mesh.")
                    return {'CANCELLED'}
                src = faces_from_mesh(obj, selected_only=True)
                if not src["faces"]:
                    self.report({'ERROR'}, "Select one or more faces first (Edit Mode).")
                    return {'CANCELLED'}
            elif obj.type == 'CURVE':
                src = paths_from_curve(obj)
                if not src["paths"]:
                    self.report({'ERROR'}, "No usable curve splines found.")
                    return {'CANCELLED'}
            else:
                src = paths_from_mesh(obj, selected_only=True)
                if not src["paths"]:
                    self.report({'ERROR'}, "No edges found. Select edges in Edit Mode first.")
                    return {'CANCELLED'}
        except Exception as exc:
            traceback.print_exc()
            self.report({'ERROR'}, "Could not read source: %s" % exc)
            return {'CANCELLED'}
        was_edit = obj.mode == 'EDIT'
        if was_edit:
            bpy.ops.object.mode_set(mode='OBJECT')
        new = create_filter(context, self.source, src, source=obj)
        # Curves remain visible/editable as live guides. The hide option applies
        # only to mesh references, where curve-style editability is not available.
        if self.hide_source and not was_edit and obj.type == 'MESH':
            obj.hide_set(True)
        elif obj.type == 'CURVE':
            try:
                obj.hide_set(False)
            except Exception:
                pass
        if new.filter_maker.last_error:
            self.report({'ERROR'}, new.filter_maker.last_error)
        return {'FINISHED'}


class FILTER_OT_edit_source_curve(Operator):
    """Select the linked source curve and enter Edit Mode to shape the live filter."""
    bl_idname = "filter.edit_source_curve"
    bl_label = "Edit Source Curve"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        ob = active_filter(context)
        return bool(ob and ob.filter_maker.source_obj and ob.filter_maker.source_obj.type == 'CURVE')

    def execute(self, context):
        ob = active_filter(context)
        if not ob:
            return {'CANCELLED'}
        source = ob.filter_maker.source_obj
        if source is None or source.type != 'CURVE':
            self.report({'ERROR'}, "This filter is not linked to a curve source.")
            return {'CANCELLED'}
        try:
            if context.object and context.object.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            source.hide_set(False)
            for item in context.view_layer.objects:
                item.select_set(False)
            source.select_set(True)
            context.view_layer.objects.active = source
            bpy.ops.object.mode_set(mode='EDIT')
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, "Could not enter curve Edit Mode: %s" % exc)
            return {'CANCELLED'}


class FILTER_OT_regenerate(Operator):
    bl_idname = "filter.regenerate"
    bl_label = "Update"

    def execute(self, context):
        ob = active_filter(context)
        if ob:
            refresh_curve_source(ob)
            regenerate(ob)
        return {'FINISHED'}


class FILTER_OT_randomize(Operator):
    bl_idname = "filter.randomize"
    bl_label = "Randomize"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        ob = active_filter(context)
        if ob:
            p = ob.filter_maker
            _busy[0] = True
            try:
                p.noise_seed = random.randint(0, 9999)
                p.hair_seed = random.randint(0, 9999)
                p.dust_seed = random.randint(0, 9999)
            finally:
                _busy[0] = False
            regenerate(ob)
        return {'FINISHED'}


class FILTER_OT_use_reference(Operator):
    bl_idname = "filter.use_reference"
    bl_label = "Use Reference"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        ob = active_filter(context)
        if ob:
            p = ob.filter_maker
            if p.source_obj and p.source_obj.type == 'MESH':
                self.report({'WARNING'}, "The reference is the object being cut. Choose a different mesh as the Object cutter.")
            else:
                self.report({'WARNING'}, "The reference is not a mesh object.")
        return {'FINISHED'}


class FILTER_OT_preset_apply(Operator):
    bl_idname = "filter.preset_apply"
    bl_label = "Apply Preset"

    def execute(self, context):
        ob = active_filter(context)
        if not ob:
            return {'CANCELLED'}
        if not apply_preset(ob, context.window_manager.filter_maker_preset):
            self.report({'ERROR'}, "Preset could not be loaded.")
            return {'CANCELLED'}
        return {'FINISHED'}


class FILTER_OT_preset_save(Operator):
    bl_idname = "filter.preset_save"
    bl_label = "Save Preset"
    name: StringProperty(name="Name", default="My Preset")

    @classmethod
    def poll(cls, context):
        return active_filter(context) is not None

    def invoke(self, context, event):
        ident = context.window_manager.filter_maker_preset
        cur = ident.partition(":")[2]
        self.name = cur if ident.startswith("U:") else (cur + " Custom" if cur else "My Preset")
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        self.layout.prop(self, "name")
        safe = re.sub(r"[^\w\- ]", "_", self.name).strip()
        if safe in user_presets():
            self.layout.label(text="A preset with this name exists; it will be overwritten", icon=ic('ERROR'))

    def execute(self, context):
        ob = active_filter(context)
        if not ob:
            return {'CANCELLED'}
        p = ob.filter_maker
        try:
            sc = _src_size(p.mode, json.loads(p.src))
            safe = save_preset(self.name, collect_values(p, sc))
        except Exception as exc:
            traceback.print_exc()
            self.report({'ERROR'}, "Could not save preset: %s" % exc)
            return {'CANCELLED'}
        preset_items(None, context)
        _set_selected_preset("U:" + safe)
        self.report({'INFO'}, "Saved preset '%s'" % safe)
        return {'FINISHED'}


class FILTER_OT_preset_update(Operator):
    bl_idname = "filter.preset_update"
    bl_label = "Overwrite Preset"

    @classmethod
    def poll(cls, context):
        try:
            return active_filter(context) is not None and context.window_manager.filter_maker_preset.startswith("U:")
        except Exception:
            return False

    def execute(self, context):
        ob = active_filter(context)
        if not ob:
            return {'CANCELLED'}
        p = ob.filter_maker
        name = context.window_manager.filter_maker_preset.partition(":")[2]
        try:
            sc = _src_size(p.mode, json.loads(p.src))
            save_preset(name, collect_values(p, sc))
        except Exception as exc:
            self.report({'ERROR'}, "Could not save preset: %s" % exc)
            return {'CANCELLED'}
        self.report({'INFO'}, "Updated preset '%s'" % name)
        return {'FINISHED'}


class FILTER_OT_preset_delete(Operator):
    bl_idname = "filter.preset_delete"
    bl_label = "Delete Preset"

    @classmethod
    def poll(cls, context):
        try:
            return context.window_manager.filter_maker_preset.startswith("U:")
        except Exception:
            return False

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        name = context.window_manager.filter_maker_preset.partition(":")[2]
        path = user_presets().get(name)
        if path:
            try:
                os.remove(path)
            except Exception as exc:
                self.report({'ERROR'}, "Could not delete: %s" % exc)
                return {'CANCELLED'}
        preset_items(None, context)
        _set_selected_preset("B:" + next(iter(BUILTIN_PRESETS)))
        return {'FINISHED'}


class FILTER_OT_finish(Operator):
    """Bake the source Boolean and leave the generated meshes as regular objects."""
    bl_idname = "filter.finish"
    bl_label = "Finish (Bake)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        ob = active_filter(context)
        if not ob:
            return {'CANCELLED'}
        p = ob.filter_maker
        source = getattr(p, "source_obj", None)
        mod_name = _boolean_mod_name(ob, p)
        source_mod = source.modifiers.get(mod_name) if source is not None and source.type == 'MESH' else None
        if source_mod is not None:
            try:
                with context.temp_override(
                    object=source,
                    active_object=source,
                    selected_objects=[source],
                    selected_editable_objects=[source],
                ):
                    bpy.ops.object.modifier_apply(modifier=source_mod.name)
            except Exception as exc:
                self.report({'WARNING'}, "Could not bake source Boolean: %s" % exc)
                return {'CANCELLED'}

        legacy = ob.modifiers.get(BOOL_NAME)
        if legacy is not None and legacy.type == 'BOOLEAN':
            try:
                with context.temp_override(object=ob, active_object=ob,
                                           selected_objects=[ob], selected_editable_objects=[ob]):
                    bpy.ops.object.modifier_apply(modifier=legacy.name)
            except Exception as exc:
                self.report({'WARNING'}, "Could not bake legacy filter Boolean: %s" % exc)
                return {'CANCELLED'}

        _remove_child(p, "cutter_obj")
        p.is_filter = False
        redraw_all()
        self.report({'INFO'}, "Filter and source Boolean baked into normal meshes.")
        return {'FINISHED'}


class FILTER_OT_delete(Operator):
    bl_idname = "filter.delete"
    bl_label = "Delete Filter"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        ob = active_filter(context)
        if ob:
            p = ob.filter_maker
            _remove_source_boolean(ob, p)
            for attr in CHILD_ATTRS:
                _remove_child(p, attr)
            _remove_hair_curve_collection(p)
            me = ob.data
            bpy.data.objects.remove(ob, do_unlink=True)
            if me and me.users == 0:
                bpy.data.meshes.remove(me)
        redraw_all()
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

def active_filter(context):
    """Return active filter or its generated child, safe during addon reloads."""
    try:
        # `Object.filter_maker` can be absent while registering or tearing down.
        if not hasattr(bpy.types.Object, "filter_maker"):
            return None
        obj = context.active_object
        if obj is None:
            return None
        props = getattr(obj, "filter_maker", None)
        if props is not None and props.is_filter:
            return obj
        parent = obj.parent
        if parent is not None:
            parent_props = getattr(parent, "filter_maker", None)
            if parent_props is not None and parent_props.is_filter:
                return parent
        # When the source curve is selected for editing, keep its linked filter
        # panel alive so the user can adjust and regenerate the result in place.
        for candidate in bpy.data.objects:
            candidate_props = getattr(candidate, "filter_maker", None)
            if (candidate_props is not None and candidate_props.is_filter
                    and candidate_props.source_obj == obj):
                return candidate
    except (AttributeError, ReferenceError, RuntimeError):
        return None
    return None


def _counts(ob):
    p = ob.filter_maker
    v, f, ev, ef = len(ob.data.vertices), len(ob.data.polygons), 0, 0
    for attr in ("hair_obj", "dust_obj", "hair_geo_obj", "dust_geo_obj", "border_obj"):
        child = getattr(p, attr)
        if child is not None and child.type == 'MESH':
            ev += len(child.data.vertices)
            ef += len(child.data.polygons)
    return v, f, ev, ef


class FILTER_PT_main(Panel):
    bl_label = "Filter Maker Pro"
    bl_idname = "FILTER_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Filter Maker'

    @safe_draw
    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.operator("filter.new_line", icon=ic('CURVE_PATH'))
        obj = context.active_object
        if obj and obj.type in {'MESH', 'CURVE'}:
            col.operator("filter.from_selection", text="From Curve / Edges", icon=ic('CURVE_DATA')).source = 'CURVE'
            if obj.type == 'MESH':
                col.operator("filter.from_selection", text="From Faces", icon=ic('FACESEL')).source = 'FACE'
        else:
            col.label(text="Select a curve or mesh as the source", icon=ic('INFO'))
        if obj and obj.mode == 'EDIT':
            col.label(text="Edit Mode: uses your selection", icon=ic('RESTRICT_SELECT_OFF'))
        ob = active_filter(context)
        if ob is None:
            return
        p = ob.filter_maker
        if p.source_obj is not None and p.source_obj.type == 'CURVE' and p.mode == 'CURVE':
            row = layout.row(align=True)
            row.label(text="Live curve guide: " + p.source_obj.name, icon=ic('CURVE_DATA'))
            row.operator("filter.edit_source_curve", text="Edit", icon=ic('GREASEPENCIL'))
        layout.separator()
        row = layout.row(align=True)
        row.prop(p, "live", text="Live", toggle=True, icon=ic('PLAY'))
        row.operator("filter.regenerate", text="", icon=ic('FILE_REFRESH'))
        row.operator("filter.randomize", text="Randomize", icon=ic('FORCE_VORTEX'))
        v, f, ev, ef = _counts(ob)
        box = layout.box()
        box.label(text="Verts %s   Faces %s" % (format(v, ","), format(f, ",")), icon=ic('MESH_DATA'))
        if ev:
            box.label(text="+ border / particle emitters: %s verts" % format(ev, ","), icon=ic('PARTICLES'))
        if p.last_error:
            layout.label(text="Error: " + p.last_error, icon=ic('ERROR'))


class FILTER_SubPanel:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Filter Maker'
    bl_parent_id = 'FILTER_PT_main'

    @classmethod
    def poll(cls, context):
        return active_filter(context) is not None

    def draw_header(self, context):
        # Use Blender's own monochrome line icon set for a native-looking UI.
        self.layout.label(text="", icon=ic(getattr(self, "header_icon", "INFO")))

    def P(self, context):
        self.layout.use_property_split = True
        self.layout.use_property_decorate = False
        ob = active_filter(context)
        return ob.filter_maker if ob is not None else None


class FILTER_PT_presets(FILTER_SubPanel, Panel):
    header_icon = "PRESET"
    bl_label = "Presets"
    bl_idname = "FILTER_PT_presets"

    @safe_draw
    def draw(self, context):
        layout, wm = self.layout, context.window_manager
        row = layout.row(align=True)
        row.prop(wm, "filter_maker_preset", text="")
        row.operator("filter.preset_apply", text="", icon=ic('CHECKMARK'))
        row = layout.row(align=True)
        row.operator("filter.preset_save", text="Save As New", icon=ic('ADD'))
        sub = row.row(align=True)
        sub.enabled = wm.filter_maker_preset.startswith("U:")
        sub.operator("filter.preset_update", text="", icon=ic('FILE_TICK'))
        sub.operator("filter.preset_delete", text="", icon=ic('TRASH'))
        layout.label(text="Saved in your Blender presets folder", icon=ic('INFO'))


class FILTER_PT_pleats(FILTER_SubPanel, Panel):
    header_icon = "MOD_WIREFRAME"
    bl_label = "Pleats"
    bl_idname = "FILTER_PT_pleats"

    @safe_draw
    def draw(self, context):
        p, c = self.P(context), self.layout.column(align=True)
        if p is None:
            return
        for prop in ("pleat_density", "pleat_depth", "pleat_phase", "detail"):
            c.prop(p, prop)
        row = c.row(align=True)
        row.prop(p, "use_thickness", text="Thickness", toggle=True, icon=ic('MOD_SOLIDIFY'))
        amount = c.row(align=True)
        amount.enabled = p.use_thickness
        amount.prop(p, "thickness", text="Amount")
        c.separator()
        if p.mode == 'CURVE':
            c.prop(p, "height")
            c.prop(p, "height_dir")
        else:
            c.prop(p, "pleat_angle")
            c.separator()
            box = c.box()
            box.prop(p, "face_smooth", text="Smooth Face Transitions")
            sub = box.column(align=True)
            sub.enabled = p.face_smooth
            sub.prop(p, "face_smooth_factor", slider=True)
            sub.prop(p, "face_smooth_cuts")
            box.label(text="Rounds internal face seams before pleating", icon=ic('INFO'))
        c.separator()
        c.label(text="Topology")
        c.prop(p, "mesh_type", expand=True)


class FILTER_PT_bevel(FILTER_SubPanel, Panel):
    header_icon = "MOD_BEVEL"
    bl_label = "Pleat Bevel"
    bl_idname = "FILTER_PT_bevel"

    @safe_draw
    def draw(self, context):
        p = self.P(context)
        if p is None:
            return
        icon_id = profile_icon(p)
        if icon_id:
            row = self.layout.row()
            row.alignment = 'CENTER'
            row.template_icon(icon_value=icon_id, scale=7.0)
            self.layout.label(text="white = result   orange = cuts   grey = sharp")
        c = self.layout.column(align=True)
        c.prop(p, "bevel_ridge", slider=True)
        c.prop(p, "bevel_valley", slider=True)
        c.prop(p, "bevel_cuts")


class FILTER_PT_shading(FILTER_SubPanel, Panel):
    header_icon = "SHADING_RENDERED"
    bl_label = "Shading"
    bl_idname = "FILTER_PT_shading"

    @safe_draw
    def draw(self, context):
        p = self.P(context)
        if p is None:
            return
        c = self.layout.column(align=True)
        c.prop(p, "smooth")
        c.prop(p, "auto_smooth")
        row = c.row(align=True)
        row.active = p.auto_smooth
        row.prop(p, "auto_smooth_angle")


class FILTER_PT_wear(FILTER_SubPanel, Panel):
    header_icon = "MOD_NOISE"
    bl_label = "Wear (Clouds Displace)"
    bl_idname = "FILTER_PT_wear"
    bl_options = {'DEFAULT_CLOSED'}

    @safe_draw
    def draw(self, context):
        p = self.P(context)
        if p is None:
            return
        c = self.layout.column(align=True)
        for prop in ("noise_strength", "noise_scale", "noise_depth", "noise_type", "noise_basis", "noise_direction", "noise_seed"):
            c.prop(p, prop)


class FILTER_PT_border(FILTER_SubPanel, Panel):
    header_icon = "MOD_SOLIDIFY"
    bl_label = "Border"
    bl_idname = "FILTER_PT_border"
    bl_options = {'DEFAULT_CLOSED'}

    @safe_draw
    def draw_header(self, context):
        self.layout.label(text="", icon=ic(self.header_icon))
        ob = active_filter(context)
        if ob:
            self.layout.prop(ob.filter_maker, "use_border", text="")

    @safe_draw
    def draw(self, context):
        p, layout = self.P(context), self.layout
        if p is None:
            return
        layout.active = p.use_border
        icon_id = border_icon(p)
        if icon_id:
            row = layout.row()
            row.alignment = 'CENTER'
            row.template_icon(icon_value=icon_id, scale=7.0)
        layout.label(text="White = result   orange = profile points   grey = bounds")
        layout.prop(p, "border_profile", expand=True)
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
        c.separator()
        c.label(text="Shading")
        c.prop(p, "border_smooth")
        c.prop(p, "border_auto_smooth")
        row = c.row(align=True)
        row.active = p.border_auto_smooth
        row.prop(p, "border_auto_smooth_angle")


class FILTER_PT_hair(FILTER_SubPanel, Panel):
    header_icon = "STRANDS"
    bl_label = "Hair"
    bl_idname = "FILTER_PT_hair"
    bl_options = {'DEFAULT_CLOSED'}

    @safe_draw
    def draw_header(self, context):
        self.layout.label(text="", icon=ic(self.header_icon))
        ob = active_filter(context)
        if ob:
            self.layout.prop(ob.filter_maker, "use_hair", text="")

    @safe_draw
    def draw(self, context):
        p = self.P(context)
        if p is None:
            return
        self.layout.active = p.use_hair
        c = self.layout.column(align=True)
        for prop in ("hair_density", "hair_length", "hair_width", "hair_length_var", "hair_randomness", "hair_curl", "hair_seed"):
            c.prop(p, prop)
        c.separator()
        c.label(text="Random curve shapes instanced directly on the filter", icon=ic('INFO'))
        c.label(text="Border-covered edges are masked automatically")
        c.label(text="Edit strand variants in the Hair Shapes collection")


class FILTER_PT_dust(FILTER_SubPanel, Panel):
    header_icon = "PARTICLES"
    bl_label = "Dust"
    bl_idname = "FILTER_PT_dust"
    bl_options = {'DEFAULT_CLOSED'}

    @safe_draw
    def draw_header(self, context):
        self.layout.label(text="", icon=ic(self.header_icon))
        ob = active_filter(context)
        if ob:
            self.layout.prop(ob.filter_maker, "use_dust", text="")

    @safe_draw
    def draw(self, context):
        p = self.P(context)
        if p is None:
            return
        self.layout.active = p.use_dust
        c = self.layout.column(align=True)
        for prop in ("dust_density", "dust_size", "dust_size_var", "dust_seed"):
            c.prop(p, prop)
        c.separator()
        c.label(text="Rounded specks + editable instanced particles", icon=ic('INFO'))
        c.label(text="After Finish: select *_DustParticles to edit density/size")


class FILTER_PT_boolean(FILTER_SubPanel, Panel):
    header_icon = "MOD_BOOLEAN"
    bl_label = "Boolean"
    bl_idname = "FILTER_PT_boolean"
    bl_options = {'DEFAULT_CLOSED'}

    @safe_draw
    def draw_header(self, context):
        self.layout.label(text="", icon=ic(self.header_icon))
        ob = active_filter(context)
        if ob:
            self.layout.prop(ob.filter_maker, "use_boolean", text="")

    @safe_draw
    def draw(self, context):
        p, layout = self.P(context), self.layout
        if p is None:
            return
        layout.active = p.use_boolean
        layout.label(text="Cutter")
        layout.prop(p, "boolean_source", expand=True)
        c = layout.column(align=True)
        if p.boolean_source == 'OBJECT':
            row = c.row(align=True)
            row.prop(p, "boolean_obj", text="")
            row.operator("filter.use_reference", text="", icon=ic('INFO'))
            if p.boolean_obj is None:
                c.label(text="Pick an object (or use the reference)", icon=ic('ERROR'))
        else:
            c.prop(p, "cutter_margin")
            c.prop(p, "show_cutter")
            c.label(text="Box or envelope cuts the complete pleated form", icon=ic('INFO'))
        c.prop(p, "boolean_op", text="Operation")
        c.prop(p, "boolean_solver", text="Solver")
        source = p.source_obj
        if p.use_boolean and (source is None or source.type != 'MESH'):
            layout.label(text="A mesh source is required to cut the source shape", icon=ic('ERROR'))
        else:
            layout.label(text="Boolean cuts the source mesh with the selected cutter", icon=ic('INFO'))
        layout.label(text="Baked on Finish. Border is not cut.", icon=ic('INFO'))


class FILTER_PT_materials(FILTER_SubPanel, Panel):
    header_icon = "MATERIAL"
    bl_label = "Materials"
    bl_idname = "FILTER_PT_materials"
    bl_options = {'DEFAULT_CLOSED'}

    @safe_draw
    def draw(self, context):
        p = self.P(context)
        if p is None:
            return
        layout = self.layout
        layout.template_ID(p, "filter_material", new="material.new")
        if p.use_border:
            layout.template_ID(p, "border_material", new="material.new")
        if p.use_hair:
            layout.template_ID(p, "hair_material", new="material.new")
        if p.use_dust:
            layout.template_ID(p, "dust_material", new="material.new")


class FILTER_PT_actions(FILTER_SubPanel, Panel):
    header_icon = "CHECKMARK"
    bl_label = "Finish"
    bl_idname = "FILTER_PT_actions"

    @safe_draw
    def draw(self, context):
        row = self.layout.row(align=True)
        row.operator("filter.finish", icon=ic('CHECKMARK'))
        row.operator("filter.delete", text="", icon=ic('TRASH'))


# ---------------------------------------------------------------------------
# Tiny software-rendered panel illustrations (pleat bevel + border cross-section)
# ---------------------------------------------------------------------------

class _Canvas:
    def __init__(self, w, h, bg=(0.11, 0.11, 0.125, 1.0)):
        self.W, self.H = w, h
        self.buf = list(bg) * (w * h)

    def put(self, x, y, col):
        if 0 <= x < self.W and 0 <= y < self.H:
            k = (y * self.W + x) * 4
            self.buf[k:k + 4] = col

    def stamp(self, x, y, col, r):
        ix, iy = int(round(x)), int(round(y))
        rr = int(math.ceil(r))
        for dx in range(-rr, rr + 1):
            for dy in range(-rr, rr + 1):
                if dx * dx + dy * dy <= r * r + 0.25:
                    self.put(ix + dx, iy + dy, col)

    def line(self, x0, y0, x1, y1, col, r):
        steps = max(1, int(max(abs(x1 - x0), abs(y1 - y0)) * 2))
        for i in range(steps + 1):
            t = i / steps
            self.stamp(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, col, r)

    def dashed(self, x0, y0, x1, y1, col, r=0.7, on=5, off=4):
        length = math.hypot(x1 - x0, y1 - y0)
        if length < 1e-6:
            return
        pos = 0.0
        while pos < length:
            a, b = pos / length, min(pos + on, length) / length
            self.line(x0 + (x1 - x0) * a, y0 + (y1 - y0) * a,
                      x0 + (x1 - x0) * b, y0 + (y1 - y0) * b, col, r)
            pos += on + off

    def fill(self, poly, col):
        if len(poly) < 3:
            return
        ys = [q[1] for q in poly]
        y0, y1 = max(0, int(math.floor(min(ys)))), min(self.H - 1, int(math.ceil(max(ys))))
        for y in range(y0, y1 + 1):
            yc, xs = y + 0.5, []
            for i in range(len(poly)):
                (xa, ya), (xb, yb) = poly[i], poly[(i + 1) % len(poly)]
                if (ya <= yc < yb) or (yb <= yc < ya):
                    xs.append(xa + (yc - ya) * (xb - xa) / (yb - ya))
            xs.sort()
            for i in range(0, len(xs) - 1, 2):
                for x in range(int(math.ceil(xs[i] - 0.5)), int(math.floor(xs[i + 1] - 0.5)) + 1):
                    self.put(x, y, col)


def _paint_profile(pv, rp, rv, cuts):
    N = 160
    cv = _Canvas(N, N)
    table = _profile_table(rp, rv, cuts)
    ev = _make_eval(table)
    m, cy, A = 12, N // 2, N * 0.30

    def to_px(X, w):
        return m + (X + 0.5) / 2.0 * (N - 2 * m), cy + w * A

    for px in range(m, N - m, 4):
        cv.put(px, cy, (0.3, 0.3, 0.34, 1.0))
        cv.put(px + 1, cy, (0.3, 0.3, 0.34, 1.0))
    ghost = _make_eval(_profile_table(0.0, 0.0, 1))
    pts = [(-0.5, ghost(-0.5)), (0.0, 1.0), (0.5, -1.0), (1.0, 1.0), (1.5, -1.0)]
    for (xa, wa), (xb, wb) in zip(pts, pts[1:]):
        x0, y0 = to_px(xa, wa)
        x1, y1 = to_px(xb, wb)
        cv.line(x0, y0, x1, y1, (0.42, 0.42, 0.48, 1.0), 0.8)
    seq = sorted({(k + x, w) for k in range(-1, 3) for x, w in table if -0.5 <= k + x <= 1.5})
    seq = [(-0.5, ev(-0.5))] + [s for s in seq if s[0] > -0.5 + 1e-9] + [(1.5, ev(1.5))]
    for (xa, wa), (xb, wb) in zip(seq, seq[1:]):
        x0, y0 = to_px(xa, wa)
        x1, y1 = to_px(xb, wb)
        cv.line(x0, y0, x1, y1, (0.92, 0.94, 1.0, 1.0), 1.4)
    for X, w in seq[1:-1]:
        x, y = to_px(X, w)
        cv.stamp(x, y, (1.0, 0.55, 0.12, 1.0), 2.2)
    pv.image_size = (N, N)
    pv.image_pixels_float = cv.buf


def _paint_border(pv, p):
    """Draw a clear border/pleat schematic using the same palette as Pleat Bevel."""
    N = 160
    cv = _Canvas(N, N)  # Same dark neutral background as the bevel graphic.
    white = (0.92, 0.94, 1.0, 1.0)       # Actual result geometry
    grey = (0.42, 0.42, 0.48, 1.0)       # Reference/bounding guides
    orange = (1.0, 0.55, 0.12, 1.0)      # Profile transitions / control points
    rail_fill = (0.22, 0.23, 0.27, 1.0)

    W, O, F, B, fit = _border_dims(p)
    left_x, right_x = 27.0, 133.0
    ratio_w = min(1.0, max(0.0, W / max(W + O, 1e-6)))
    rail_w = 8.0 + 11.0 * ratio_w
    inner_left, inner_right = left_x + rail_w, right_x - rail_w

    # Let the front/back settings alter the rail height, but clamp the drawing to
    # readable bounds so extreme user values do not push the graphic off-canvas.
    front_ratio = F / max(F + B, 1e-6)
    back_ratio = B / max(F + B, 1e-6)
    mid_y = 80.0
    rail_top = max(43.0, mid_y - (14.0 + 17.0 * front_ratio))
    rail_bottom = min(104.0, mid_y + (8.0 + 14.0 * back_ratio))
    baseline = 111.0

    # Quiet datum and vertical guides are deliberately drawn behind the result.
    cv.line(12, baseline, 148, baseline, grey, 0.9)
    cv.line(inner_left - 2.0, 22, inner_left - 2.0, 138, grey, 0.9)
    cv.line(inner_right + 2.0, 22, inner_right + 2.0, 138, grey, 0.9)

    def rail_polygon(x0, x1, y0, y1, left_rail):
        profile = p.border_profile
        bevel = max(2.0, min(6.0, p.border_bevel / max(W + O + F + B, 1e-6) * 24.0))
        if profile == 'CHAMFER':
            c = min(bevel, (x1 - x0) * 0.28, (y1 - y0) * 0.16)
            return [(x0 + c, y0), (x1 - c, y0), (x1, y0 + c),
                    (x1, y1 - c), (x1 - c, y1), (x0 + c, y1),
                    (x0, y1 - c), (x0, y0 + c)]
        if profile == 'ROUND':
            r = min(bevel, (x1 - x0) * 0.34, (y1 - y0) * 0.22)
            pts = []
            for cx, cy, a0 in ((x0 + r, y0 + r, math.pi),
                               (x1 - r, y0 + r, 1.5 * math.pi),
                               (x1 - r, y1 - r, 0.0),
                               (x0 + r, y1 - r, 0.5 * math.pi)):
                for j in range(5):
                    ang = a0 + j * (math.pi / 2.0) / 4.0
                    pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
            return pts
        if profile == 'LIP':
            step = min(bevel + 2.0, (x1 - x0) * 0.30, (y1 - y0) * 0.28)
            if left_rail:
                return [(x0, y1), (x1, y1), (x1, y0 + step),
                        (x1 + step, y0 + step), (x1 + step, y0), (x0, y0)]
            return [(x0, y1), (x1, y1), (x1, y0), (x0 - step, y0),
                    (x0 - step, y0 + step), (x0, y0 + step)]
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]

    for is_left, x0, x1 in (
            (True, left_x, inner_left), (False, inner_right, right_x)):
        poly = rail_polygon(x0, x1, rail_top, rail_bottom, is_left)
        cv.fill(poly, rail_fill)
        for a, b in zip(poly, poly[1:] + poly[:1]):
            cv.line(a[0], a[1], b[0], b[1], white, 1.2)
        # Orange marker at the inner contact edge, like the cut points in the bevel preview.
        contact_x = inner_left if is_left else inner_right
        cv.stamp(contact_x, rail_bottom - 5.0, orange, 1.5)

    # The pleat wave stays one continuous line between the two rails.
    span = inner_right - inner_left
    amp_ratio = min(1.0, max(0.0, p.pleat_depth / max(W + O, 1e-6)))
    amplitude = 14.0 + 8.0 * amp_ratio
    low = baseline - 19.0
    high = max(rail_top + 8.0, low - amplitude)
    waves = 6
    pts = [(inner_left + 1.0, low)]
    for i in range(1, waves * 2):
        x = inner_left + 1.0 + (span - 2.0) * i / (waves * 2)
        y = high if i % 2 else low
        pts.append((x, y))
    pts.append((inner_right - 1.0, low))
    for a, b in zip(pts, pts[1:]):
        cv.line(a[0], a[1], b[0], b[1], white, 1.45)
    for x, y in pts[1:-1]:
        cv.stamp(x, y, orange, 1.25)

    # Small grey ticks reinforce the two boundary positions without adding color noise.
    for x in (inner_left - 2.0, inner_right + 2.0):
        cv.line(x - 3.0, 30.0, x + 3.0, 30.0, grey, 0.85)
    pv.image_size = (N, N)
    pv.image_pixels_float = cv.buf


def profile_icon(p):
    pv = _pv.get("profile")
    if pv is None:
        return 0
    rp, rv = _bevel_radii(p)
    key = (round(rp, 3), round(rv, 3), int(p.bevel_cuts))
    if _pv_key.get("profile") != key:
        try:
            _paint_profile(pv, rp, rv, p.bevel_cuts)
            _pv_key["profile"] = key
        except Exception:
            traceback.print_exc()
            return 0
    return pv.icon_id


def border_icon(p):
    pv = _pv.get("border")
    if pv is None:
        return 0
    W, O, F, B, fit = _border_dims(p)
    key = (p.border_profile, round(W, 6), round(O, 6), round(F, 6), round(B, 6), round(fit, 6),
           round(p.border_bevel, 6), int(p.border_cuts), round(p.pleat_depth, 6))
    if _pv_key.get("border") != key:
        try:
            _paint_border(pv, p)
            _pv_key["border"] = key
        except Exception:
            traceback.print_exc()
            return 0
    return pv.icon_id


classes = (FilterProps, FILTER_OT_new_line, FILTER_OT_from_selection, FILTER_OT_edit_source_curve, FILTER_OT_regenerate,
           FILTER_OT_randomize, FILTER_OT_use_reference, FILTER_OT_preset_apply,
           FILTER_OT_preset_save, FILTER_OT_preset_update, FILTER_OT_preset_delete,
           FILTER_OT_finish, FILTER_OT_delete, FILTER_PT_main, FILTER_PT_presets,
           FILTER_PT_pleats, FILTER_PT_bevel, FILTER_PT_shading, FILTER_PT_wear,
           FILTER_PT_border, FILTER_PT_hair, FILTER_PT_dust, FILTER_PT_boolean,
           FILTER_PT_materials, FILTER_PT_actions)


# ---------------------------------------------------------------------------
# Robust registration / reload cleanup
# ---------------------------------------------------------------------------

def _teardown():
    """Unregister classes, properties, previews and stale source-sync handlers."""
    for handler in list(bpy.app.handlers.depsgraph_update_post):
        if getattr(handler, "__name__", "") == "_sync_curve_sources":
            try:
                bpy.app.handlers.depsgraph_update_post.remove(handler)
            except Exception:
                pass
    if _pcoll[0] is not None:
        try:
            bpy.utils.previews.remove(_pcoll[0])
        except Exception:
            pass
    _pcoll[0] = None
    _pv.clear()
    _pv_key.clear()

    # Resolve the registered RNA type by name: classes from previous script
    # executions are different Python objects and cannot be unregistered by the
    # current module's class references alone.
    for cls in reversed(classes):
        try:
            registered = getattr(bpy.types, cls.__name__, None)
            if registered is not None:
                bpy.utils.unregister_class(registered)
            else:
                bpy.utils.unregister_class(cls)
        except Exception:
            pass

    for owner, attr in ((bpy.types.WindowManager, "filter_maker_preset"),
                        (bpy.types.Object, "filter_maker")):
        try:
            if hasattr(owner, attr):
                delattr(owner, attr)
        except Exception:
            pass


def register():
    _teardown()
    try:
        # Register the property group and pointer before any panel can poll/draw.
        bpy.utils.register_class(FilterProps)
        bpy.types.Object.filter_maker = PointerProperty(type=FilterProps)
        bpy.types.WindowManager.filter_maker_preset = EnumProperty(
            name="Preset", items=preset_items, update=_preset_changed)
        for cls in classes:
            if cls is not FilterProps:
                bpy.utils.register_class(cls)
        _pcoll[0] = bpy.utils.previews.new()
        _pv["profile"] = _pcoll[0].new("profile")
        _pv["border"] = _pcoll[0].new("border")
        _pv_key.clear()
        # One persistent callback makes curve-based filters follow source edits.
        for handler in list(bpy.app.handlers.depsgraph_update_post):
            if getattr(handler, "__name__", "") == "_sync_curve_sources":
                bpy.app.handlers.depsgraph_update_post.remove(handler)
        bpy.app.handlers.depsgraph_update_post.append(_sync_curve_sources)
    except Exception:
        traceback.print_exc()
        _teardown()
        raise


def unregister():
    _teardown()


if __name__ == "__main__":
    register()
