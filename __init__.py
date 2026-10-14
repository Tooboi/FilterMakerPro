bl_info = {
    "name": "Filter Maker Pro",
    "author": "Tooboi",
    "version": (0, 7, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar (N) > Filter Maker",
    "description": "Pleated filter generator with instanced hair/dust particle systems, borders and presets.",
    "category": "Object",
}

import bisect
import hashlib
import json
import math
import os
import random
import re
import time
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

try:
    import numpy as np
except Exception:                                  # numpy ships with Blender; just in case
    np = None

_busy = [False]
_KEEP = [None]
MAX_CUTS = 400
MAX_INST = 100000
BOOL_NAME = "Filter Boolean"
EDITION = "PRO"                     # "LITE" in the free build (tools/build_lite.py)
_EDITION_KEY = "filter_maker_edition"

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


def _curve_strip_np(bm, q3, wv, uacc, u_end, d, H, rows, cl):
    """Extrude the pleated path into a strip (numpy + foreach_set): vertex (i, k) is
    station i at height row k; UVs = (unrolled length, height) in metres."""
    n = len(q3)
    Q = np.asarray([tuple(q) for q in q3], dtype=np.float64)
    hs = H * np.arange(rows + 1) / rows
    coords = (Q[:, None, :] + np.asarray(tuple(d))[None, None, :] * hs[None, :, None]).reshape(-1, 3)
    ncol = n if cl else n - 1
    i = np.repeat(np.arange(ncol), rows)
    k = np.tile(np.arange(rows), ncol)
    j = (i + 1) % n
    R = rows + 1
    quads = np.stack([i * R + k, j * R + k, j * R + k + 1, i * R + k + 1], axis=1)
    U = np.asarray(uacc, dtype=np.float64)
    ui = U[i]
    uj = np.where(j > i, U[j], u_end)
    v0, v1 = hs[k], hs[k + 1]
    luv = np.stack([np.stack([ui, v0], 1), np.stack([uj, v0], 1),
                    np.stack([uj, v1], 1), np.stack([ui, v1], 1)], axis=1).reshape(-1, 2)
    me = bpy.data.meshes.new(".FM strip tmp")
    try:
        _mesh_from_arrays(me, coords, quads.tolist())
        uvl = me.uv_layers.new(name=UV_LAYER)
        if len(uvl.data) == len(luv):
            uvl.data.foreach_set("uv", luv.astype(np.float32).ravel())
        attr = me.attributes.new(PLEAT_ATTR, 'FLOAT', 'POINT')
        if len(attr.data) == n * R:
            attr.data.foreach_set("value", np.repeat(np.asarray(wv, dtype=np.float32), R))
        me.update()
        bm.from_mesh(me)
    finally:
        bpy.data.meshes.remove(me)


def _curve_strip_py(bm, q3, wv, uacc, u_end, d, H, rows, cl):
    n = len(q3)
    uvl = bm.loops.layers.uv.get(UV_LAYER) or bm.loops.layers.uv.new(UV_LAYER)
    pl = bm.verts.layers.float.get(PLEAT_ATTR) or bm.verts.layers.float.new(PLEAT_ATTR)
    cols = [[bm.verts.new(q3[i] + d * (H * k / rows)) for k in range(rows + 1)] for i in range(n)]
    for i in range(n):
        for vert in cols[i]:
            vert[pl] = wv[i]
    for i in range(n if cl else n - 1):
        j = (i + 1) % n
        ui, uj = uacc[i], (uacc[j] if j > i else u_end)
        for k in range(rows):
            try:
                f = bm.faces.new((cols[i][k], cols[j][k], cols[j][k + 1], cols[i][k + 1]))
            except ValueError:
                continue
            v0, v1 = H * k / rows, H * (k + 1) / rows
            for lp, uv in zip(f.loops, ((ui, v0), (uj, v0), (uj, v1), (ui, v1))):
                lp[uvl].uv = uv


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
        n, q3, wv = len(cp), [], []
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
            w = ev(poss[i] + p.pleat_phase)
            wv.append(w)
            q3.append(c + side * (depth * w))
        # UVs in metres: unrolled length along the pleated path x height (an extruded
        # strip unrolls flat, so this is stretch-free).
        uacc = [0.0]
        for i in range(1, n):
            uacc.append(uacc[-1] + (q3[i] - q3[i - 1]).length)
        u_end = uacc[-1] + ((q3[0] - q3[-1]).length if cl else 0.0)
        if np is not None:
            _curve_strip_np(bm, q3, wv, uacc, u_end, d, H, rows, cl)
        else:
            _curve_strip_py(bm, q3, wv, uacc, u_end, d, H, rows, cl)
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


def _surface_param(cos, tris, ua, ra):
    """Surface coordinates for a triangulated island.

    Returns (tri_uv, vnorm, ring) where tri_uv[t] holds the (u, v) of each corner of
    triangle t, measured ALONG the surface (u across the pleats, v along the ridges),
    vnorm are smooth area-weighted vertex normals, and ring is the loop length when the
    island wraps all the way round (else 0). A wrapping island is cut open along a seam;
    corners on the far side of the seam get the loop length added, so the strip is
    continuous in (u, v) and closes again in 3D.
    """
    nv = len(cos)
    tri_n, tri_dirs = [], []
    vnorm = [Vector((0.0, 0.0, 0.0)) for _ in range(nv)]
    for (i, j, k) in tris:
        n = (cos[j] - cos[i]).cross(cos[k] - cos[i])
        for idx in (i, j, k):
            vnorm[idx] = vnorm[idx] + n
        if n.length < 1e-15:
            tri_n.append(None)
            tri_dirs.append((ua.copy(), ra.copy()))
            continue
        n = n.normalized()
        tri_n.append(n)
        rt = ra - n * ra.dot(n)
        if rt.length < 1e-6:                        # face perpendicular to the ridge axis
            gu = ua - n * ua.dot(n)
            if gu.length < 1e-9:
                gu = ua.copy()
            gu = gu.normalized()
            rt = n.cross(gu)
        else:
            rt = rt.normalized()
            gu = rt.cross(n)
        tri_dirs.append((gu.normalized(), rt.normalized()))
    for idx in range(nv):
        vnorm[idx] = vnorm[idx].normalized() if vnorm[idx].length > 1e-15 else Vector((0.0, 0.0, 1.0))

    edge_dirs = {}
    for t, (i, j, k) in enumerate(tris):
        gu, gv = tri_dirs[t]
        for a, b in ((i, j), (j, k), (k, i)):
            key = (a, b) if a < b else (b, a)
            acc = edge_dirs.get(key)
            if acc is None:
                edge_dirs[key] = [gu.copy(), gv.copy()]
            else:
                acc[0] = acc[0] + gu
                acc[1] = acc[1] + gv
    edge_d = {}
    for (a, b), (gu, gv) in edge_dirs.items():
        gu = gu.normalized() if gu.length > 1e-9 else gu
        gv = gv.normalized() if gv.length > 1e-9 else gv
        d = cos[b] - cos[a]
        edge_d[(a, b)] = (gu.dot(d), gv.dot(d))     # change from a to b

    def delta(a, b):
        if (a, b) in edge_d:
            return edge_d[(a, b)]
        du, dv = edge_d[(b, a)]
        return -du, -dv

    nbrs = [[] for _ in range(nv)]
    for (a, b) in edge_d:
        nbrs[a].append(b)
        nbrs[b].append(a)

    best, anchor = -1.0, tris[0][0]
    for (i, j, k) in tris:
        ar = (cos[j] - cos[i]).cross(cos[k] - cos[i]).length
        if ar > best:
            best, anchor = ar, i
    u = [None] * nv
    v = [None] * nv
    for seed in [anchor] + list(range(nv)):
        if u[seed] is not None or (seed != anchor and not nbrs[seed]):
            continue
        u[seed], v[seed] = ua.dot(cos[seed]), ra.dot(cos[seed])
        queue = deque([seed])
        while queue:
            c = queue.popleft()
            for o in nbrs[c]:
                if u[o] is None:
                    du, dv = delta(c, o)
                    u[o], v[o] = u[c] + du, v[c] + dv
                    queue.append(o)
    for idx in range(nv):
        if u[idx] is None:
            u[idx], v[idx] = ua.dot(cos[idx]), ra.dot(cos[idx])

    # Seam detection: on an island that wraps round, the two integration fronts meet
    # with a mismatch of one whole loop length.
    span = max(u) - min(u) if u else 0.0
    seam = set()
    errs = []
    for (a, b) in edge_d:
        du, _dv = edge_d[(a, b)]
        err = u[b] - u[a] - du
        if span > 1e-9 and abs(err) > 0.5 * span:
            seam.add((a, b))
            errs.append(abs(err))
    ring = 0.0
    if seam:
        errs.sort()
        ring = errs[len(errs) // 2]
        # Keep the seam edges in the relaxation, but with a whole-loop offset, so the
        # two sides of the seam stay locked together (otherwise they drift and twist).
        for (a, b) in seam:
            du, dv = edge_d[(a, b)]
            err = u[b] - u[a] - du
            edge_d[(a, b)] = (du + round(err / ring) * ring, dv)

    # Gauss-Seidel relaxation (seam edges excluded) evens out curvature distortion.
    links = sum(len(nb) for nb in nbrs)
    iters = max(4, min(60, 400000 // max(1, links)))
    for _ in range(iters):
        for idx in range(nv):
            if idx == anchor or not nbrs[idx]:
                continue
            su = sv = 0.0
            for o in nbrs[idx]:
                du, dv = delta(o, idx)
                su += u[o] + du
                sv += v[o] + dv
            m = len(nbrs[idx])
            u[idx], v[idx] = su / m, sv / m

    def is_seam(a, b):
        return (a, b) in seam or (b, a) in seam

    tri_uv = []
    for t, tri in enumerate(tris):
        corners = [(u[x], v[x]) for x in tri]
        if ring > 0.0 and any(is_seam(tri[a], tri[(a + 1) % 3]) for a in range(3)):
            # Keep the vertices reachable from corner 0 without crossing the seam;
            # shift the rest by a whole loop so the triangle is continuous in (u, v).
            same = {0}
            for _ in range(2):
                for a in range(3):
                    for b in range(3):
                        if a in same and b not in same and not is_seam(tri[a], tri[b]):
                            same.add(b)
            r = tri[0]
            gu, gv = tri_dirs[t]
            for c in range(3):
                if c in same:
                    continue
                w = tri[c]
                est = u[r] + gu.dot(cos[w] - cos[r])
                shift = round((est - u[w]) / ring) * ring
                corners[c] = (u[w] + shift, v[w])
        tri_uv.append(corners)
    return tri_uv, vnorm, ring


def _clip_poly_halfplane(poly, a, b, eps):
    """Sutherland-Hodgman step: keep the left side of the directed line a->b.

    poly items are (u, v, sides) where sides is a frozenset of grid-line ids the
    point lies on. An intersection point lies on every line both endpoints share.
    """
    out = []
    n = len(poly)
    if n == 0:
        return out
    ex, ey = b[0] - a[0], b[1] - a[1]

    def side(pt):
        return ex * (pt[1] - a[1]) - ey * (pt[0] - a[0])

    for idx in range(n):
        P = poly[idx]
        Q = poly[(idx + 1) % n]
        sp, sq = side(P), side(Q)
        pin, qin = sp >= -eps, sq >= -eps
        if pin:
            out.append(P)
        if pin != qin:
            t = sp / (sp - sq) if abs(sp - sq) > 1e-300 else 0.0
            t = min(1.0, max(0.0, t))
            out.append((P[0] + (Q[0] - P[0]) * t, P[1] + (Q[1] - P[1]) * t, P[2] & Q[2]))
    return out


def _bary(px, py, A, B, C):
    d = (B[1] - C[1]) * (A[0] - C[0]) + (C[0] - B[0]) * (A[1] - C[1])
    if abs(d) < 1e-300:
        return None
    w0 = ((B[1] - C[1]) * (px - C[0]) + (C[0] - B[0]) * (py - C[1])) / d
    w1 = ((C[1] - A[1]) * (px - C[0]) + (A[0] - C[0]) * (py - C[1])) / d
    return w0, w1, 1.0 - w0 - w1


def _seg_hits_rect(p, q, u0, u1, v0, v1, tol):
    """Segment p-q touches the closed rectangle (Liang-Barsky with tolerance)."""
    u0, u1, v0, v1 = u0 - tol, u1 + tol, v0 - tol, v1 + tol
    t0, t1 = 0.0, 1.0
    dx, dy = q[0] - p[0], q[1] - p[1]
    for pp, qq in ((-dx, p[0] - u0), (dx, u1 - p[0]), (-dy, p[1] - v0), (dy, v1 - p[1])):
        if abs(pp) < 1e-300:
            if qq < 0:
                return False
        else:
            r = qq / pp
            if pp < 0:
                t0 = max(t0, r)
            else:
                t1 = min(t1, r)
            if t0 > t1:
                return False
    return True


def _grid_mesh(cos, vnorm, tris, tri_uv, UL, VL, qstep):
    """Quad-grid remesh of an island in (u, v) space.

    UL / VL: sorted grid lines covering the island. Cells fully inside become one
    quad made of the four grid corners; cells crossed by the island outline are
    clipped against the triangles, merged back into one polygon and stripped of
    any vertex that only existed because a source edge crossed the cell, so the
    whole result is free of T-junctions.

    Returns (verts, faces): verts maps key -> (position, normal, u);
    faces is a list of key lists (counter-clockwise in u, v).
    """
    nU, nV = len(UL) - 1, len(VL) - 1
    if nU < 1 or nV < 1:
        return {}, []
    tol = qstep * 10.0

    def cells_of(lo_u, hi_u, lo_v, hi_v):
        a0 = max(0, bisect.bisect_right(UL, lo_u - tol) - 1)
        a1 = min(nU - 1, bisect.bisect_left(UL, hi_u + tol))
        b0 = max(0, bisect.bisect_right(VL, lo_v - tol) - 1)
        b1 = min(nV - 1, bisect.bisect_left(VL, hi_v + tol))
        return a0, a1, b0, b1

    # Orient every triangle counter-clockwise in (u, v).
    tuv = []
    for t in range(len(tris)):
        A, B, C = tri_uv[t]
        area = (B[0] - A[0]) * (C[1] - A[1]) - (B[1] - A[1]) * (C[0] - A[0])
        tuv.append((A, B, C, area > 0.0, abs(area) > 1e-24))

    cell_tris = {}
    for t, (A, B, C, _ccw, ok) in enumerate(tuv):
        if not ok:
            continue
        us, vs = (A[0], B[0], C[0]), (A[1], B[1], C[1])
        a0, a1, b0, b1 = cells_of(min(us), max(us), min(vs), max(vs))
        for a in range(a0, a1 + 1):
            for b in range(b0, b1 + 1):
                cell_tris.setdefault((a, b), []).append(t)

    # Outline of the island in (u, v): triangle edges without a matching twin.
    edge_uv = {}
    for t, tri in enumerate(tris):
        if not tuv[t][4]:
            continue
        for c in range(3):
            a, b = tri[c], tri[(c + 1) % 3]
            key = (a, b) if a < b else (b, a)
            pa, pb = tri_uv[t][c], tri_uv[t][(c + 1) % 3]
            if a > b:
                pa, pb = pb, pa
            edge_uv.setdefault(key, []).append((pa, pb))
    complex_cells = set()
    cell_outline = {}

    def same(p, q):
        return abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol

    def on_grid_line(pa, pb):
        """Outline segment running exactly along a grid line: the cells beside it need no clipping."""
        for lines, ax in ((UL, 0), (VL, 1)):
            i = bisect.bisect_left(lines, pa[ax])
            for j in (i - 1, i):
                if 0 <= j < len(lines) and abs(lines[j] - pa[ax]) <= tol and abs(lines[j] - pb[ax]) <= tol:
                    return True
        return False

    for key, uses in edge_uv.items():
        if len(uses) == 2 and same(uses[0][0], uses[1][0]) and same(uses[0][1], uses[1][1]):
            continue
        for (pa, pb) in uses:
            if on_grid_line(pa, pb):
                continue
            a0, a1, b0, b1 = cells_of(min(pa[0], pb[0]), max(pa[0], pb[0]),
                                      min(pa[1], pb[1]), max(pa[1], pb[1]))
            for a in range(a0, a1 + 1):
                for b in range(b0, b1 + 1):
                    if _seg_hits_rect(pa, pb, UL[a], UL[a + 1], VL[b], VL[b + 1], tol):
                        complex_cells.add((a, b))
                        cell_outline.setdefault((a, b), []).append((pa, pb))

    verts = {}

    def put(key, t, pu, pv):
        if key in verts:
            return
        w = _bary(pu, pv, *tri_uv[t])
        if w is None:
            return
        i, j, k = tris[t]
        pos = cos[i] * w[0] + cos[j] * w[1] + cos[k] * w[2]
        nrm = vnorm[i] * w[0] + vnorm[j] * w[1] + vnorm[k] * w[2]
        nrm = nrm.normalized() if nrm.length > 1e-12 else vnorm[i].copy()
        verts[key] = (pos, nrm, pu, pv)

    # Grid corners: which triangle contains each one.
    corner_ok = {}

    def locate(a, b):
        pu, pv = UL[a], VL[b]
        seen = set()
        for ca in (a - 1, a):
            for cb in (b - 1, b):
                for t in cell_tris.get((ca, cb), ()):
                    if t in seen:
                        continue
                    seen.add(t)
                    w = _bary(pu, pv, *tri_uv[t])
                    if w is not None and min(w) >= -1e-9:
                        return t
        return None

    faces = []
    for (a, b), tlist in cell_tris.items():
        if (a, b) in complex_cells:
            continue
        ok = True
        for (ca, cb) in ((a, b), (a + 1, b), (a + 1, b + 1), (a, b + 1)):
            if (ca, cb) not in corner_ok:
                corner_ok[(ca, cb)] = locate(ca, cb)
            if corner_ok[(ca, cb)] is None:
                ok = False
        if not ok:
            complex_cells.add((a, b))
            continue
        quad = []
        for (ca, cb) in ((a, b), (a + 1, b), (a + 1, b + 1), (a, b + 1)):
            key = ('c', ca, cb)
            put(key, corner_ok[(ca, cb)], UL[ca], VL[cb])
            quad.append(key)
        faces.append(quad)

    def near_line(lines, x):
        i = bisect.bisect_left(lines, x)
        for j in (i - 1, i):
            if 0 <= j < len(lines) and abs(lines[j] - x) <= tol:
                return j
        return None

    def key_of(pt):
        """Canonical vertex name: snapped to grid corners / grid lines when on them."""
        pu, pv = pt[0], pt[1]
        a = near_line(UL, pu)
        b = near_line(VL, pv)
        if a is not None and b is not None:
            return ('c', a, b), UL[a], VL[b]
        if a is not None:
            return ('U', a, int(round(pv / qstep))), UL[a], pv
        if b is not None:
            return ('V', b, int(round(pu / qstep))), pu, VL[b]
        return ('P', int(round(pu / qstep)), int(round(pv / qstep))), pu, pv

    def key_sides(key):
        if key[0] == 'c':
            return {('U', key[1]), ('V', key[2])}
        if key[0] == 'U':
            return {('U', key[1])}
        if key[0] == 'V':
            return {('V', key[1])}
        return set()

    protected = set()

    def on_outline(key, pu, pv, segs):
        """Point lies on an outline segment that is not simply running along its grid line."""
        axis = 0 if key[0] == 'U' else 1
        line = UL[key[1]] if key[0] == 'U' else VL[key[1]]
        for (pa, pb) in segs:
            if abs(pa[axis] - line) <= tol and abs(pb[axis] - line) <= tol:
                continue
            dx, dy = pb[0] - pa[0], pb[1] - pa[1]
            ll = dx * dx + dy * dy
            tt = 0.0 if ll < 1e-300 else max(0.0, min(1.0, ((pu - pa[0]) * dx + (pv - pa[1]) * dy) / ll))
            ex, ey = pa[0] + dx * tt - pu, pa[1] + dy * tt - pv
            if ex * ex + ey * ey <= (tol * 10.0) ** 2:
                return True
        return False

    for (a, b) in sorted(complex_cells):
        tlist = cell_tris.get((a, b))
        if not tlist:
            continue
        segs = cell_outline.get((a, b), ())
        u0, u1, v0, v1 = UL[a], UL[a + 1], VL[b], VL[b + 1]
        rect = [(u0, v0, frozenset({('U', a), ('V', b)})),
                (u1, v0, frozenset({('U', a + 1), ('V', b)})),
                (u1, v1, frozenset({('U', a + 1), ('V', b + 1)})),
                (u0, v1, frozenset({('U', a), ('V', b + 1)}))]
        pieces = []
        for t in tlist:
            A, B, C, ccw, ok = tuv[t]
            if not ok:
                continue
            tri = (A, B, C) if ccw else (A, C, B)
            poly = rect
            for e in range(3):
                poly = _clip_poly_halfplane(poly, tri[e], tri[(e + 1) % 3], qstep)
                if len(poly) < 3:
                    break
            if len(poly) < 3:
                continue
            keys = []
            for pt in poly:
                key, pu, pv = key_of(pt)
                put(key, t, pu, pv)
                if key[0] in ('U', 'V') and key not in protected and segs and on_outline(key, pu, pv, segs):
                    protected.add(key)
                if key in verts and (not keys or keys[-1] != key):
                    keys.append(key)
            if len(keys) > 2 and keys[0] == keys[-1]:
                keys.pop()
            if len(keys) >= 3:
                pieces.append(keys)
        if not pieces:
            continue
        # Merge the pieces: interior edges appear twice in opposite directions.
        count = {}
        for keys in pieces:
            for x, y in zip(keys, keys[1:] + keys[:1]):
                count[(x, y)] = count.get((x, y), 0) + 1
        for (x, y) in list(count):
            if count.get((x, y), 0) > 0 and count.get((y, x), 0) > 0:
                m = min(count[(x, y)], count[(y, x)])
                count[(x, y)] -= m
                count[(y, x)] -= m
        nxt = {}
        for (x, y), c in count.items():
            for _ in range(c):
                nxt.setdefault(x, []).append(y)
        while nxt:
            start = next(iter(nxt))
            loop, cur = [start], start
            for _ in range(10000):
                outs = nxt.get(cur)
                if not outs:
                    break
                nx = outs.pop()
                if not outs:
                    del nxt[cur]
                if nx == start:
                    break
                loop.append(nx)
                cur = nx
            # Drop points that only sit on a cell side because a source edge crossed it.
            changed = True
            while changed and len(loop) > 3:
                changed = False
                for idx in range(len(loop)):
                    k = loop[idx]
                    if k[0] == 'c' or k[0] == 'P' or k in protected:
                        continue
                    pk, nk = loop[idx - 1], loop[(idx + 1) % len(loop)]
                    s = key_sides(k)
                    if s & key_sides(pk) and s & key_sides(nk):
                        del loop[idx]
                        changed = True
                        break
            if len(loop) >= 3:
                faces.append(loop)
    return verts, faces


def _grid_mesh_np(cos, vnorm, tris, tri_uv, UL, VL, qstep):
    """numpy version of _grid_mesh (same topology, same outline handling).

    Interior cells are found and built with array maths; only the cells crossed by
    the outline go through the clipping / merging path. Returns
    (positions (N,3), normals (N,3), u (N,), v (N,), faces as vertex-index lists).
    """
    nU, nV = len(UL) - 1, len(VL) - 1
    if nU < 1 or nV < 1 or not tris:
        return None
    tol = qstep * 10.0
    ULa = np.asarray(UL, dtype=np.float64)
    VLa = np.asarray(VL, dtype=np.float64)
    T = len(tris)
    TUV = np.asarray(tri_uv, dtype=np.float64)                  # (T, 3, 2)
    TI = np.asarray(tris, dtype=np.int64)                       # (T, 3)
    P = np.asarray([tuple(c) for c in cos], dtype=np.float64)   # (V, 3)
    NRM = np.asarray([tuple(n) for n in vnorm], dtype=np.float64)
    A, B, C = TUV[:, 0], TUV[:, 1], TUV[:, 2]
    area2 = (B[:, 0] - A[:, 0]) * (C[:, 1] - A[:, 1]) - (B[:, 1] - A[:, 1]) * (C[:, 0] - A[:, 0])
    valid = np.abs(area2) > 1e-24
    tmin = TUV.min(axis=1)
    tmax = TUV.max(axis=1)

    def cell_range(lo, hi, lines, n):
        a0 = max(0, bisect.bisect_right(lines, lo - tol) - 1)
        a1 = min(n - 1, bisect.bisect_left(lines, hi + tol))
        return a0, a1

    # ---- grid corners: which triangle holds each (vectorised per triangle) ----
    ctri = np.full((nU + 1, nV + 1), -1, dtype=np.int64)
    cw = np.zeros((nU + 1, nV + 1, 3), dtype=np.float64)
    for t in range(T):
        if not valid[t]:
            continue
        a0 = bisect.bisect_left(UL, tmin[t, 0] - tol)
        a1 = bisect.bisect_right(UL, tmax[t, 0] + tol)
        b0 = bisect.bisect_left(VL, tmin[t, 1] - tol)
        b1 = bisect.bisect_right(VL, tmax[t, 1] + tol)
        if a1 <= a0 or b1 <= b0:
            continue
        uu = ULa[a0:a1][:, None]
        vv = VLa[b0:b1][None, :]
        (ax, ay), (bx, by), (cx, cy) = TUV[t]
        d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        w0 = ((by - cy) * (uu - cx) + (cx - bx) * (vv - cy)) / d
        w1 = ((cy - ay) * (uu - cx) + (ax - cx) * (vv - cy)) / d
        w2 = 1.0 - w0 - w1
        inside = (w0 >= -1e-9) & (w1 >= -1e-9) & (w2 >= -1e-9)
        sub = ctri[a0:a1, b0:b1]
        take = inside & (sub < 0)
        if take.any():
            sub[take] = t
            cw[a0:a1, b0:b1][take] = np.stack([w0, w1, w2], axis=-1)[take]

    # ---- cells crossed by the island outline ----
    edge_uv = {}
    for t, tri in enumerate(tris):
        if not valid[t]:
            continue
        for c in range(3):
            a, b = tri[c], tri[(c + 1) % 3]
            key = (a, b) if a < b else (b, a)
            pa, pb = tri_uv[t][c], tri_uv[t][(c + 1) % 3]
            if a > b:
                pa, pb = pb, pa
            edge_uv.setdefault(key, []).append((pa, pb))
    complex_cells = set()
    cell_outline = {}

    def same(p_, q_):
        return abs(p_[0] - q_[0]) <= tol and abs(p_[1] - q_[1]) <= tol

    def on_grid_line(pa, pb):
        """Outline segment running exactly along a grid line: the cells beside it need no clipping."""
        for lines, ax in ((UL, 0), (VL, 1)):
            i = bisect.bisect_left(lines, pa[ax])
            for j in (i - 1, i):
                if 0 <= j < len(lines) and abs(lines[j] - pa[ax]) <= tol and abs(lines[j] - pb[ax]) <= tol:
                    return True
        return False

    for key, uses in edge_uv.items():
        if len(uses) == 2 and same(uses[0][0], uses[1][0]) and same(uses[0][1], uses[1][1]):
            continue
        for (pa, pb) in uses:
            if on_grid_line(pa, pb):
                continue
            a0, a1 = cell_range(min(pa[0], pb[0]), max(pa[0], pb[0]), UL, nU)
            b0, b1 = cell_range(min(pa[1], pb[1]), max(pa[1], pb[1]), VL, nV)
            for a in range(a0, a1 + 1):
                for b in range(b0, b1 + 1):
                    if _seg_hits_rect(pa, pb, UL[a], UL[a + 1], VL[b], VL[b + 1], tol):
                        complex_cells.add((a, b))
                        cell_outline.setdefault((a, b), []).append((pa, pb))

    located = ctri >= 0
    full = located[:-1, :-1] & located[1:, :-1] & located[1:, 1:] & located[:-1, 1:]
    cmask = np.zeros((nU, nV), dtype=bool)
    if complex_cells:
        ca = np.fromiter((c[0] for c in complex_cells), dtype=np.int64, count=len(complex_cells))
        cb = np.fromiter((c[1] for c in complex_cells), dtype=np.int64, count=len(complex_cells))
        cmask[ca, cb] = True
    simple = full & ~cmask
    # partly located cells that the outline does not cross still need clipping
    anyc = located[:-1, :-1] | located[1:, :-1] | located[1:, 1:] | located[:-1, 1:]
    for a, b in zip(*np.nonzero(anyc & ~full & ~cmask)):
        complex_cells.add((int(a), int(b)))

    # ---- corner vertices (only the ones a face will use are kept later) ----
    pos_list, nrm_list, u_list, v_list = [], [], [], []
    corner_vid = np.full((nU + 1, nV + 1), -1, dtype=np.int64)
    li, lj = np.nonzero(located)
    if len(li):
        tt = ctri[li, lj]
        w = cw[li, lj]
        tv = TI[tt]
        cpos = (P[tv] * w[..., None]).sum(axis=1)
        cn = (NRM[tv] * w[..., None]).sum(axis=1)
        ln = np.linalg.norm(cn, axis=1)
        bad = ln < 1e-12
        cn[~bad] /= ln[~bad, None]
        cn[bad] = NRM[tv[bad, 0]]
        corner_vid[li, lj] = np.arange(len(li))
        pos_list.append(cpos)
        nrm_list.append(cn)
        u_list.append(ULa[li])
        v_list.append(VLa[lj])
    n_corner = len(li)

    # interior quads, counter-clockwise in (u, v)
    si, sj = np.nonzero(simple)
    quads = np.stack([corner_vid[si, sj], corner_vid[si + 1, sj],
                      corner_vid[si + 1, sj + 1], corner_vid[si, sj + 1]], axis=1) if len(si) else \
        np.zeros((0, 4), dtype=np.int64)
    faces = quads.tolist()

    # ---- outline cells: clip against the triangles, merge, strip extra points ----
    extra_pos, extra_nrm, extra_u, extra_v = [], [], [], []
    extra_id = {}

    def near_line(lines, x):
        i = bisect.bisect_left(lines, x)
        for j in (i - 1, i):
            if 0 <= j < len(lines) and abs(lines[j] - x) <= tol:
                return j
        return None

    def key_of(pu, pv):
        a = near_line(UL, pu)
        b = near_line(VL, pv)
        if a is not None and b is not None:
            return ('c', a, b), UL[a], VL[b]
        if a is not None:
            return ('U', a, int(round(pv / qstep))), UL[a], pv
        if b is not None:
            return ('V', b, int(round(pu / qstep))), pu, VL[b]
        return ('P', int(round(pu / qstep)), int(round(pv / qstep))), pu, pv

    def vid_of(key, t, pu, pv):
        if key[0] == 'c':
            v = int(corner_vid[key[1], key[2]])
            if v >= 0:
                return v
        v = extra_id.get(key)
        if v is not None:
            return v
        w_ = _bary(pu, pv, *tri_uv[t])
        if w_ is None:
            return None
        i, j, k = tris[t]
        pos = P[i] * w_[0] + P[j] * w_[1] + P[k] * w_[2]
        nrm = NRM[i] * w_[0] + NRM[j] * w_[1] + NRM[k] * w_[2]
        ln_ = float(np.sqrt((nrm * nrm).sum()))
        nrm = nrm / ln_ if ln_ > 1e-12 else NRM[i]
        v = n_corner + len(extra_pos)
        extra_pos.append(pos)
        extra_nrm.append(nrm)
        extra_u.append(pu)
        extra_v.append(pv)
        extra_id[key] = v
        return v

    def key_sides(key):
        if key[0] == 'c':
            return {('U', key[1]), ('V', key[2])}
        if key[0] == 'U':
            return {('U', key[1])}
        if key[0] == 'V':
            return {('V', key[1])}
        return set()

    def on_outline(key, pu, pv, segs):
        axis = 0 if key[0] == 'U' else 1
        line = UL[key[1]] if key[0] == 'U' else VL[key[1]]
        for (pa, pb) in segs:
            if abs(pa[axis] - line) <= tol and abs(pb[axis] - line) <= tol:
                continue
            dx, dy = pb[0] - pa[0], pb[1] - pa[1]
            ll = dx * dx + dy * dy
            tt_ = 0.0 if ll < 1e-300 else max(0.0, min(1.0, ((pu - pa[0]) * dx + (pv - pa[1]) * dy) / ll))
            ex, ey = pa[0] + dx * tt_ - pu, pa[1] + dy * tt_ - pv
            if ex * ex + ey * ey <= (tol * 10.0) ** 2:
                return True
        return False

    protected = set()
    for (a, b) in sorted(complex_cells):
        u0, u1, v0, v1 = UL[a], UL[a + 1], VL[b], VL[b + 1]
        cand = np.nonzero(valid & (tmin[:, 0] <= u1 + tol) & (tmax[:, 0] >= u0 - tol)
                          & (tmin[:, 1] <= v1 + tol) & (tmax[:, 1] >= v0 - tol))[0]
        if not len(cand):
            continue
        segs = cell_outline.get((a, b), ())
        rect = [(u0, v0, frozenset()), (u1, v0, frozenset()), (u1, v1, frozenset()), (u0, v1, frozenset())]
        pieces = []
        for t in cand.tolist():
            tA, tB, tC = tri_uv[t]
            ccw = area2[t] > 0.0
            tri = (tA, tB, tC) if ccw else (tA, tC, tB)
            poly = rect
            for e in range(3):
                poly = _clip_poly_halfplane(poly, tri[e], tri[(e + 1) % 3], qstep)
                if len(poly) < 3:
                    break
            if len(poly) < 3:
                continue
            keys = []
            for pt in poly:
                key, pu, pv = key_of(pt[0], pt[1])
                if vid_of(key, t, pu, pv) is None:
                    continue
                if key[0] in ('U', 'V') and key not in protected and segs and on_outline(key, pu, pv, segs):
                    protected.add(key)
                if not keys or keys[-1] != key:
                    keys.append(key)
            if len(keys) > 2 and keys[0] == keys[-1]:
                keys.pop()
            if len(keys) >= 3:
                pieces.append(keys)
        if not pieces:
            continue
        count = {}
        for keys in pieces:
            for x, y in zip(keys, keys[1:] + keys[:1]):
                count[(x, y)] = count.get((x, y), 0) + 1
        for (x, y) in list(count):
            if count.get((x, y), 0) > 0 and count.get((y, x), 0) > 0:
                m = min(count[(x, y)], count[(y, x)])
                count[(x, y)] -= m
                count[(y, x)] -= m
        nxt = {}
        for (x, y), c in count.items():
            for _ in range(c):
                nxt.setdefault(x, []).append(y)
        while nxt:
            start = next(iter(nxt))
            loop, cur = [start], start
            for _ in range(10000):
                outs = nxt.get(cur)
                if not outs:
                    break
                nx = outs.pop()
                if not outs:
                    del nxt[cur]
                if nx == start:
                    break
                loop.append(nx)
                cur = nx
            changed = True
            while changed and len(loop) > 3:
                changed = False
                for idx in range(len(loop)):
                    k = loop[idx]
                    if k[0] == 'c' or k[0] == 'P' or k in protected:
                        continue
                    pk, nk = loop[idx - 1], loop[(idx + 1) % len(loop)]
                    s = key_sides(k)
                    if s & key_sides(pk) and s & key_sides(nk):
                        del loop[idx]
                        changed = True
                        break
            if len(loop) >= 3:
                ids = []
                for k in loop:
                    if k[0] == 'c' and corner_vid[k[1], k[2]] >= 0:
                        v = int(corner_vid[k[1], k[2]])
                    else:
                        v = extra_id.get(k)
                    if v is not None and (not ids or ids[-1] != v):
                        ids.append(v)
                if len(ids) > 2 and ids[0] == ids[-1]:
                    ids.pop()
                if len(ids) >= 3:
                    faces.append(ids)

    if extra_pos:
        pos_list.append(np.asarray(extra_pos))
        nrm_list.append(np.asarray(extra_nrm))
        u_list.append(np.asarray(extra_u))
        v_list.append(np.asarray(extra_v))
    if not pos_list:
        return None
    pos = np.concatenate(pos_list) if len(pos_list) > 1 else pos_list[0]
    nrm = np.concatenate(nrm_list) if len(nrm_list) > 1 else nrm_list[0]
    us = np.concatenate(u_list) if len(u_list) > 1 else u_list[0]
    vs = np.concatenate(v_list) if len(v_list) > 1 else v_list[0]
    return pos, nrm, us, vs, faces


UV_LAYER = "UVMap"
PLEAT_ATTR = "fm_pleat"          # pleat profile value per vertex: +1 ridge, -1 valley
EDGE_ATTR = "fm_edge"            # 0 at the border's inner edge -> 1 deep inside (for dirt masks)


def _arc_table(table, period, depth):
    """Cumulative surface length over one pleat period (the profile unrolled flat).
    Returns (xs, cumulative lengths, length of one period)."""
    pts = list(table) + [(table[0][0] + 1.0, table[0][1])]
    xs, cum, acc = [pts[0][0]], [0.0], 0.0
    for (xa, wa), (xb, wb) in zip(pts, pts[1:]):
        acc += math.hypot((xb - xa) * period, (wb - wa) * depth)
        xs.append(xb)
        cum.append(acc)
    return xs, cum, acc


def _arc_np(arc, x):
    """Unrolled length at pleat coordinate x (in periods), vectorised."""
    xs, cum, per = arc
    x0 = xs[0]
    k = np.floor(x - x0)
    return k * per + np.interp(x - x0 - k + x0, xs, cum)


def _arc_py(arc, x):
    xs, cum, per = arc
    x0 = xs[0]
    k = math.floor(x - x0)
    xm = x - x0 - k + x0
    i = max(0, min(len(xs) - 2, bisect.bisect_right(xs, xm) - 1))
    span = xs[i + 1] - xs[i]
    t = 0.0 if span < 1e-12 else (xm - xs[i]) / span
    return k * per + cum[i] + (cum[i + 1] - cum[i]) * t


def _profile_np(table, x):
    """Vectorised _make_eval: periodic piecewise-linear pleat profile."""
    xs = np.asarray([q[0] for q in table] + [table[0][0] + 1.0])
    ws = np.asarray([q[1] for q in table] + [table[0][1]])
    x0 = xs[0]
    return np.interp((x - x0) % 1.0 + x0, xs, ws)


def _grid_lines(lo, hi, positions, tol, ends=True, min_gap=0.0):
    """Sorted, de-duplicated grid lines.

    ends=True adds the exact extent lo / hi (clean outer cells) and drops lines that
    would leave a sliver narrower than min_gap next to them. ends=False keeps the
    pattern strictly periodic (needed to close a wrap-around seam exactly) and just
    makes sure the lines cover [lo, hi].
    """
    if ends:
        vals = [lo, hi] + [x for x in positions if lo + max(tol, min_gap) < x < hi - max(tol, min_gap)]
    else:
        vals = sorted(positions)
        inside = [x for x in vals if lo - tol <= x <= hi + tol]
        below = [x for x in vals if x < lo - tol]
        above = [x for x in vals if x > hi + tol]
        vals = ([below[-1]] if below else []) + inside + ([above[0]] if above else [])
    vals.sort()
    out = []
    for x in vals:
        if not out or x - out[-1] > tol:
            out.append(x)
    return out


def _pleat_grid_lines(umin, umax, vmin, vmax, base, period, xs, vstep, ring, qstep):
    """u lines at every pleat-profile cut, v lines every vstep."""
    upos = []
    k0 = int(math.floor((umin - base) / period)) - 1
    k1 = int(math.ceil((umax - base) / period)) + 2
    for kk in range(k0, k1):
        for x in xs:
            upos.append(base + (kk + x) * period)
    step = period / max(1, len(xs))
    vpos = [m * vstep for m in range(int(math.floor(vmin / vstep)) - 1, int(math.ceil(vmax / vstep)) + 2)]
    tol = qstep * 100
    if ring > 0.0:
        UL = _grid_lines(umin, umax, upos, tol, ends=False)
    else:
        UL = _grid_lines(umin, umax, upos, tol, ends=True, min_gap=0.3 * step)
    VL = _grid_lines(vmin, vmax, vpos, tol, ends=True, min_gap=0.3 * vstep)
    return UL, VL


def _face_component(bm, p, comp):
    """Pleat a connected face island as one continuous, cleanly quadded surface.

    - pleat spacing is measured along the surface (no stretching on angled faces)
    - topology is a structured grid: every pleat / bevel cut is an edge loop, cells
      inside the selection are perfect quads, only the outline gets clipped faces
    - pleats are pushed out along smooth (interpolated) normals, so there are no
      shading breaks where the selected faces meet
    - a selection that wraps all the way round is closed with a seamless pleat
    """
    if not comp:
        return
    f0 = max(comp, key=lambda f: f.calc_area())
    n0 = f0.normal.normalized()
    ref = Vector((1.0, 0.0, 0.0))
    if abs(n0.dot(ref)) > 0.95:
        ref = Vector((0.0, 1.0, 0.0))
    u0 = (ref - n0 * ref.dot(n0)).normalized()
    v0 = n0.cross(u0).normalized()
    angle = p.pleat_angle
    ua = (u0 * math.cos(angle) + v0 * math.sin(angle)).normalized()
    ra = n0.cross(ua).normalized()

    src = bmesh.new()
    try:
        vm = {}
        for f in comp:
            vl = []
            for vert in f.verts:
                if vert not in vm:
                    vm[vert] = src.verts.new(vert.co)
                vl.append(vm[vert])
            try:
                src.faces.new(vl)
            except ValueError:
                pass
        bmesh.ops.triangulate(src, faces=src.faces[:])
        src.verts.index_update()
        cos = [vert.co.copy() for vert in src.verts]
        tris = [tuple(vert.index for vert in f.verts) for f in src.faces if len(f.verts) == 3]
    finally:
        src.free()
    if not tris:
        return

    _t0 = time.perf_counter()
    tri_uv, vnorm, ring = _surface_param(cos, tris, ua, ra)
    _FACE_TIMINGS["param_ms"] = _FACE_TIMINGS.get("param_ms", 0.0) + (time.perf_counter() - _t0) * 1000.0
    all_u = [c[0] for tri in tri_uv for c in tri]
    all_v = [c[1] for tri in tri_uv for c in tri]
    umin, umax, vmin, vmax = min(all_u), max(all_u), min(all_v), max(all_v)
    size = max(umax - umin, vmax - vmin, 1e-6)
    qstep = size * 1e-9

    period = 1.0 / max(p.pleat_density, 1e-3)
    if ring > 0.0:                                 # close a wrap-around with a whole number of pleats
        period = ring / max(1, int(round(ring / period)))
    depth = p.pleat_depth
    rp, rv = _bevel_radii(p)
    table = _profile_table(rp, rv, p.bevel_cuts)
    ev = _make_eval(table)
    base = umin - period * p.pleat_phase

    maxlen = 1.0 / max(p.detail, 1e-3)
    xs = _cycle_xs(table, period, depth, maxlen)
    for _ in range(12):
        if (umax - umin) / period * len(xs) <= MAX_CUTS:
            break
        maxlen *= 1.5
        xs = _cycle_xs(table, period, depth, maxlen)
    vstep = 1.0 / max(p.detail, 1e-3)
    while (vmax - vmin) / vstep > MAX_CUTS:
        vstep *= 1.5
    UL, VL = _pleat_grid_lines(umin, umax, vmin, vmax, base, period, xs, vstep, ring, qstep)

    if np is not None:
        try:
            if _face_component_np(bm, p, cos, vnorm, tris, tri_uv, UL, VL, qstep, table, base,
                                  period, depth, size):
                return
        except Exception:
            traceback.print_exc()           # fall back to the pure-Python path below
    _t0 = time.perf_counter()
    verts, faces = _grid_mesh(cos, vnorm, tris, tri_uv, UL, VL, qstep)
    _FACE_TIMINGS["grid_ms"] = _FACE_TIMINGS.get("grid_ms", 0.0) + (time.perf_counter() - _t0) * 1000.0
    if not faces:
        return
    _t0 = time.perf_counter()

    used = {key for keys in faces for key in keys}
    new_verts, vuv = {}, {}
    arc = _arc_table(table, period, depth)
    uvl = bm.loops.layers.uv.get(UV_LAYER) or bm.loops.layers.uv.new(UV_LAYER)
    pl = bm.verts.layers.float.get(PLEAT_ATTR) or bm.verts.layers.float.new(PLEAT_ATTR)
    for key, (pos, nrm, pu, pv) in verts.items():
        if key in used:
            x = (pu - base) / period
            w = ev(x)
            vert = bm.verts.new(pos + nrm * (depth * w))
            vert[pl] = w
            new_verts[key] = vert
            vuv[vert] = (_arc_py(arc, x), pv)
    created = []
    for keys in faces:
        vl = []
        for key in keys:
            vert = new_verts.get(key)
            if vert is not None and (not vl or vl[-1] is not vert):
                vl.append(vert)
        if len(vl) > 2 and vl[0] is vl[-1]:
            vl.pop()
        if len(vl) < 3:
            continue
        try:
            f = bm.faces.new(vl)
        except ValueError:
            continue
        for lp in f.loops:
            lp[uvl].uv = vuv[lp.vert]
        created.append(f)
    _FACE_TIMINGS["assemble_ms"] = _FACE_TIMINGS.get("assemble_ms", 0.0) + (time.perf_counter() - _t0) * 1000.0
    _t0 = time.perf_counter()
    vlist = list(new_verts.values())
    # Close wrap-around seams and swallow slivers along the outline.
    outline = list({vert for f in created if f.is_valid for e in f.edges
                    if len(e.link_faces) == 1 for vert in e.verts})
    if outline:
        bmesh.ops.remove_doubles(bm, verts=outline, dist=size * 1e-4)
    vlist = [vert for vert in vlist if vert.is_valid]
    bmesh.ops.remove_doubles(bm, verts=vlist, dist=size * 1e-7)
    created = [f for f in created if f.is_valid]
    if p.mesh_type == 'TRIS':
        bmesh.ops.triangulate(bm, faces=created, quad_method='BEAUTY', ngon_method='BEAUTY')
    else:
        ngons = [f for f in created if len(f.verts) > 4]
        if ngons:
            bmesh.ops.triangulate(bm, faces=ngons, quad_method='BEAUTY', ngon_method='BEAUTY')
    _FACE_TIMINGS["cleanup_ms"] = _FACE_TIMINGS.get("cleanup_ms", 0.0) + (time.perf_counter() - _t0) * 1000.0


def _mesh_from_arrays(me, coords, faces):
    """Fill an empty mesh from vertex coordinates and polygon index lists with
    foreach_set (several times faster than from_pydata on big grids)."""
    nv = len(coords)
    sizes = np.fromiter((len(f) for f in faces), dtype=np.int32, count=len(faces))
    loops = np.fromiter((i for f in faces for i in f), dtype=np.int32, count=int(sizes.sum()))
    starts = np.zeros(len(faces), dtype=np.int32)
    if len(faces) > 1:
        starts[1:] = np.cumsum(sizes)[:-1]
    me.vertices.add(nv)
    me.vertices.foreach_set("co", np.asarray(coords, dtype=np.float32).ravel())
    me.loops.add(len(loops))
    me.loops.foreach_set("vertex_index", loops)
    me.polygons.add(len(faces))
    me.polygons.foreach_set("loop_start", starts)
    try:
        me.polygons.foreach_set("loop_total", sizes)
    except Exception:
        pass                                  # read-only in newer Blender (derived from loop_start)
    me.update(calc_edges=True)
    me.validate(clean_customdata=False)


def _face_component_np(bm, p, cos, vnorm, tris, tri_uv, UL, VL, qstep, table, base, period, depth, size):
    """Fast path: numpy grid, vectorised pleat displacement, C-speed mesh assembly."""
    _t0 = time.perf_counter()
    res = _grid_mesh_np(cos, vnorm, tris, tri_uv, UL, VL, qstep)
    _FACE_TIMINGS["grid_ms"] = _FACE_TIMINGS.get("grid_ms", 0.0) + (time.perf_counter() - _t0) * 1000.0
    if res is None:
        return True
    pos, nrm, us, vs, faces = res
    if not faces:
        return True
    _t0 = time.perf_counter()
    # keep only vertices that a face uses, and renumber
    used = np.zeros(len(pos), dtype=bool)
    for f in faces:
        used[f] = True
    remap = np.full(len(pos), -1, dtype=np.int64)
    remap[used] = np.arange(int(used.sum()))
    xq = (us[used] - base) / period
    prof = _profile_np(table, xq)
    coords = pos[used] + nrm[used] * (depth * prof)[:, None]
    # UVs in metres: unrolled length across the pleats x length along them, so the
    # texture keeps its proportions on every fold (no stretching on flanks or bevels).
    uv_u = _arc_np(_arc_table(table, period, depth), xq)
    uv_v = vs[used]
    faces = [remap[f].tolist() for f in faces]
    n_quads = 0
    for f in faces:
        if len(f) != 4:
            break
        n_quads += 1
    tmp_me = bpy.data.meshes.new(".FM grid tmp")
    try:
        _mesh_from_arrays(tmp_me, coords, faces)
        loop_v = np.empty(len(tmp_me.loops), dtype=np.int32)
        tmp_me.loops.foreach_get("vertex_index", loop_v)
        uvl = tmp_me.uv_layers.new(name=UV_LAYER)
        uvl.data.foreach_set("uv", np.stack([uv_u[loop_v], uv_v[loop_v]], axis=1).astype(np.float32).ravel())
        attr = tmp_me.attributes.new(PLEAT_ATTR, 'FLOAT', 'POINT')
        attr.data.foreach_set("value", prof.astype(np.float32))
        tmp_me.update()
        bm.verts.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        v0, f0 = len(bm.verts), len(bm.faces)
        bm.from_mesh(tmp_me)
    finally:
        bpy.data.meshes.remove(tmp_me)
    bm.verts.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    new_verts = bm.verts[v0:]
    created = bm.faces[f0:]
    edge_faces = created[n_quads:]
    _FACE_TIMINGS["assemble_ms"] = _FACE_TIMINGS.get("assemble_ms", 0.0) + (time.perf_counter() - _t0) * 1000.0
    _t0 = time.perf_counter()
    # Close wrap-around seams and swallow slivers along the outline (one C pass; the
    # distance stays well below the closest grid-line spacing).
    gaps = [b - a for a, b in zip(UL, UL[1:])] + [b - a for a, b in zip(VL, VL[1:])]
    weld = min(size * 1e-4, 0.1 * min(gaps)) if gaps else size * 1e-4
    bmesh.ops.remove_doubles(bm, verts=list(new_verts), dist=max(weld, size * 1e-7))
    loose = [v for v in new_verts if v.is_valid and not v.link_faces]
    if loose:
        bmesh.ops.delete(bm, geom=loose, context='VERTS')
    if p.mesh_type == 'TRIS':
        bmesh.ops.triangulate(bm, faces=[f for f in created if f.is_valid],
                              quad_method='BEAUTY', ngon_method='BEAUTY')
    else:
        ngons = [f for f in edge_faces if f.is_valid and len(f.verts) > 4]
        if ngons:
            bmesh.ops.triangulate(bm, faces=ngons, quad_method='BEAUTY', ngon_method='BEAUTY')
    _FACE_TIMINGS["cleanup_ms"] = _FACE_TIMINGS.get("cleanup_ms", 0.0) + (time.perf_counter() - _t0) * 1000.0
    return True


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

    # Outline handling: interior points are smoothed in 3D. Outline points slide
    # along the outline (1-D smoothing), so where the outline crosses a crease
    # between panels it is rounded exactly like the surface next to it (no pinched
    # hourglass corners). Corners of a flat face stay sharp: the outline keeps its shape.
    tmp.normal_update()
    boundary = {v for e in tmp.edges if len(e.link_faces) == 1 for v in e.verts}
    interior = [v for v in tmp.verts if v not in boundary]
    slide = []
    for v in boundary:
        nb = [e.other_vert(v) for e in v.link_edges if len(e.link_faces) == 1]
        if len(nb) != 2:
            continue
        a, b = (v.co - nb[0].co), (nb[1].co - v.co)
        turn = a.length > 1e-12 and b.length > 1e-12 and a.normalized().dot(b.normalized()) < 0.985
        normals = [f.normal for f in v.link_faces if f.normal.length > 1e-12]
        flat = all(abs(normals[0].dot(n)) > 0.985 for n in normals[1:]) if normals else True
        if turn and flat:
            continue                                   # a real corner of a flat face
        slide.append((v, nb[0], nb[1]))
    fac = float(p.face_smooth_factor)
    # Outline points copy the movement of the row just inside them, so a rounded
    # crease is rounded by the same amount all the way to the edge (no barrel).
    inner_nbrs = {v: [e.other_vert(v) for e in v.link_edges if e.other_vert(v) not in boundary]
                  for v, _a, _b in slide}
    for _ in range(2):
        if interior:
            before = {v: v.co.copy() for v in interior}
            bmesh.ops.smooth_vert(tmp, verts=interior, factor=fac,
                                  use_axis_x=True, use_axis_y=True, use_axis_z=True)
            for v, _n0, _n1 in slide:
                nb = inner_nbrs[v]
                if nb:
                    d = Vector((0.0, 0.0, 0.0))
                    for w in nb:
                        d += w.co - before[w]
                    v.co += d / len(nb)

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


_FACE_TIMINGS = {}


def build_face(bm, p, src, ref=None):
    _FACE_TIMINGS.clear()
    _t0 = time.perf_counter()
    tmp, comps = _faces_bmesh(src, smooth_p=p)
    _FACE_TIMINGS["source_ms"] = (time.perf_counter() - _t0) * 1000.0
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

_NOISE_CACHE = {}
_DIST_CACHE = {}


def apply_noise(bm, p, fade=None, cache_id=None, sheet_key=None):
    """Clouds-style displace. `fade` (one factor per vertex) lets the wear die out
    under the border so the pleat ends stay exactly on the reference edge.
    The raw noise is cached per filter, so dragging Strength only rescales it."""
    strength = p.noise_strength
    if strength <= 0 or p.noise_scale <= 0 or not bm.verts:
        return
    inv, sd = 1.0 / p.noise_scale, p.noise_seed
    off = Vector((sd * 37.1, sd * 17.3, sd * 7.9))
    hard, basis, depth = p.noise_type == 'HARD', p.noise_basis, p.noise_depth
    verts = list(bm.verts)
    turb = noise.turbulence
    key = (sheet_key, round(p.noise_scale, 9), sd, depth, hard, basis, p.noise_direction, len(verts))
    cached = _NOISE_CACHE.get(cache_id) if (cache_id and sheet_key) else None
    if p.noise_direction == 'XYZ':
        if cached is not None and cached[0] == key:
            vals, (mx, my, mz) = cached[1], cached[2]
        else:
            o1, o2 = off + Vector((31.7, 17.3, 5.1)), off + Vector((9.9, 43.1, 21.7))
            vals = []
            for v in verts:
                q = v.co * inv
                vals.append((turb(q + off, depth, hard, noise_basis=basis),
                             turb(q + o1, depth, hard, noise_basis=basis),
                             turb(q + o2, depth, hard, noise_basis=basis)))
            n = float(len(vals))
            mx = sum(v[0] for v in vals) / n
            my = sum(v[1] for v in vals) / n
            mz = sum(v[2] for v in vals) / n
            if cache_id and sheet_key:
                _NOISE_CACHE[cache_id] = (key, vals, (mx, my, mz))
        for i, v in enumerate(verts):
            a, b, c = vals[i]
            k = strength if fade is None else strength * fade[i]
            v.co += Vector(((a - mx) * k, (b - my) * k, (c - mz) * k))
    else:
        if cached is not None and cached[0] == key:
            vals, mean = cached[1], cached[2]
        else:
            vals = [turb(v.co * inv + off, depth, hard, noise_basis=basis) for v in verts]
            mean = sum(vals) / float(len(vals))
            if cache_id and sheet_key:
                _NOISE_CACHE[cache_id] = (key, vals, mean)
        for i, v in enumerate(verts):
            k = strength if fade is None else strength * fade[i]
            v.co += v.normal * ((vals[i] - mean) * k)




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
    # A seal past the reference edge keeps the pleat ends inside the frame. The shell
    # thickness and the wear can push the last pleat slightly past the edge, so the
    # seal covers those too.
    effective_thickness = p.thickness if getattr(p, "use_thickness", False) else 0.0
    O = (max(p.border_overhang, 0.0) + max(5e-4, 0.03 * W)
         + effective_thickness + 1.5 * max(p.noise_strength, 0.0))
    fit = (p.pleat_depth + effective_thickness + 1.5 * p.noise_strength) if p.border_fit else 0.0
    F, B = max(fit + p.border_front, 1e-6), max(fit + p.border_back, 1e-6)
    return W, O, F, B, fit


def _border_section(p, W, O, F, B, fit=0.0):
    prof, r = p.border_profile, max(0.0, p.border_bevel)
    # Chamfer / round only shape the outer edge: never cut into the space the pleats
    # need at the reference edge (height `fit` on both sides).
    edge_room = max(0.0, min(F, B) - fit + O - 1e-5)
    if prof == 'FLAT' or r < 1e-7:
        pts = [(-O, -B), (W, -B), (W, F), (-O, F)]
    elif prof == 'CHAMFER':
        r = min(r, (O + W) * 0.9, (F + B) * 0.45, edge_room)
        pts = [(-O + r, -B), (W, -B), (W, F), (-O + r, F), (-O, F - r), (-O, -B + r)]
    elif prof == 'ROUND':
        r = min(r, (O + W) * 0.9, (F + B) * 0.45, edge_room)
        n = max(1, int(p.border_cuts))
        pts = [(W, -B), (W, F)]
        pts += _arc2((-O, F), (-O + r, F), (-O, F - r), n)
        pts += _arc2((-O, -B), (-O, -B + r), (-O + r, -B), n)
    else:
        # Lip: the outer rim rises ABOVE the normal frame height. (It used to step the
        # inner part down to F - r, which let pleat ends poke through when the lip step
        # was larger than Front Extend.)
        r = min(r, (F + B) * 0.6)
        lw = (O + W) * 0.4
        pts = [(-O, -B), (W, -B), (W, F), (-O + lw, F), (-O + lw, F + r), (-O, F + r)]
    out = []
    for q in pts:
        if not out or abs(q[0] - out[-1][0]) + abs(q[1] - out[-1][1]) > 1e-9:
            out.append(q)
    if len(out) > 1 and abs(out[0][0] - out[-1][0]) + abs(out[0][1] - out[-1][1]) <= 1e-9:
        out.pop()
    return out


def _untangle_offsets(co, P):
    """Offset points of a closed loop (one section point per station). Where the loop has
    stations closer to a corner than the offset distance, the offset edge folds back on
    itself and the frame turns inside out there (gaps and clipping at the corners). Every
    segment that runs against the loop direction is collapsed onto the sharper corner."""
    n = len(co)
    if n < 3:
        return P
    turn = []
    for k in range(n):
        a = co[k] - co[k - 1]
        b = co[(k + 1) % n] - co[k]
        if a.length < 1e-12 or b.length < 1e-12:
            turn.append(0.0)
        else:
            turn.append(1.0 - a.normalized().dot(b.normalized()))
    P = [q.copy() for q in P]
    for _it in range(n):
        changed = False
        for k in range(n):
            k2 = (k + 1) % n
            base = co[k2] - co[k]
            seg = P[k2] - P[k]
            if base.length < 1e-12 or seg.length < 1e-12:
                continue
            if seg.dot(base) < 0.0:
                src, dst = (k, k2) if turn[k] >= turn[k2] else (k2, k)
                P[dst] = P[src].copy()
                turn[dst] = turn[src]
                changed = True
        if not changed:
            break
    return P


def _sweep_loop(co, nrm, inw_edge, sec, face_n=None):
    n, m_ = len(co), len(sec)
    miters, hvs = [], []
    for k in range(n):
        a, b = inw_edge[k - 1], inw_edge[k]
        m = a + b
        if m.length < 1e-6:
            m = b.copy()
        if m.length > 1e-9:
            m.normalize()
        c = max(m.dot(b), 0.3)
        miters.append(m / c)
        # Where the surface folds (box corners, L folds) the vertex normal is the average
        # of the two faces; offsetting along it would make the frame thinner than F / B
        # measured from each face, and the pleats poked through. Stretch it so the
        # frame keeps its full height above both faces.
        hv = nrm[k]
        if face_n is not None:
            dots = [abs(hv.dot(fn)) for fn in (face_n[k - 1], face_n[k]) if fn.length > 1e-9]
            if dots:
                hv = hv / max(min(dots), 0.3)
        hvs.append(hv)
    cols = []
    for s, h in sec:
        P = [co[k] + miters[k] * s + hvs[k] * h for k in range(n)]
        if abs(s) > 1e-12:
            P = _untangle_offsets(co, P)
        cols.append(P)
    verts = []
    for k in range(n):
        for j in range(m_):
            verts.append(cols[j][k])
    # UVs in metres: length along the loop x length around the cross-section
    ucum = [0.0]
    for k in range(1, n):
        ucum.append(ucum[-1] + (co[k] - co[k - 1]).length)
    u_end = ucum[-1] + (co[0] - co[-1]).length
    vcum = [0.0]
    for j in range(1, m_):
        vcum.append(vcum[-1] + math.hypot(sec[j][0] - sec[j - 1][0], sec[j][1] - sec[j - 1][1]))
    v_end = vcum[-1] + math.hypot(sec[0][0] - sec[-1][0], sec[0][1] - sec[-1][1])
    quads, uvs = [], []
    for k in range(n):
        k2 = (k + 1) % n
        ua, ub = ucum[k], (ucum[k2] if k2 > k else u_end)
        for j in range(m_):
            j2 = (j + 1) % m_
            va, vb = vcum[j], (vcum[j2] if j2 > j else v_end)
            quads.append((k * m_ + j, k2 * m_ + j, k2 * m_ + j2, k * m_ + j2))
            uvs.append(((ua, va), (ub, va), (ub, vb), (ua, vb)))
    return verts, quads, uvs


def build_border(p, rb):
    rb.verts.ensure_lookup_table()
    rb.edges.ensure_lookup_table()
    rb.faces.ensure_lookup_table()
    rb.verts.index_update()
    rb.normal_update()
    bedges = [(e.verts[0].index, e.verts[1].index) for e in rb.edges if len(e.link_faces) == 1]
    if not bedges:
        return None
    small = _rb_small(rb)
    W, O, F, B, _fit = _border_dims(p, small)
    sec = _border_section(p, W, O, F, B, _fit)
    if len(sec) < 3:
        return None
    fb = bmesh.new()
    loops_out = []
    for ids, cl in _chains(bedges):
        if not cl or len(ids) < 3:
            continue
        P = [rb.verts[i] for i in ids]
        n = len(P)
        inw, fns = [], []
        for k in range(n):
            a, b = P[k], P[(k + 1) % n]
            e = rb.edges.get((a, b))
            dk = b.co - a.co
            if e is None or not e.link_faces or dk.length < 1e-12:
                inw.append(Vector((0, 0, 0)))
                fns.append(Vector((0, 0, 0)))
                continue
            f = e.link_faces[0]
            fns.append(f.normal.copy())
            iv = f.normal.cross(dk.normalized())
            if iv.length < 1e-9:
                inw.append(Vector((0, 0, 0)))
                continue
            iv.normalize()
            if iv.dot(f.calc_center_median() - (a.co + b.co) * 0.5) < 0:
                iv = -iv
            inw.append(iv)
        nrm = [v.normal.copy() if v.normal.length > 1e-9 else Vector((0, 0, 1)) for v in P]
        verts, quads, quvs = _sweep_loop([v.co for v in P], nrm, inw, sec, fns)
        fv = [fb.verts.new(c) for c in verts]
        uvl = fb.loops.layers.uv.get(UV_LAYER) or fb.loops.layers.uv.new(UV_LAYER)
        for q, quv in zip(quads, quvs):
            try:
                f = fb.faces.new([fv[i] for i in q])
            except ValueError:
                continue
            for lp, uv in zip(f.loops, quv):
                lp[uvl].uv = uv
        loops_out.append((fv, len(sec), sec))
    if not fb.faces:
        fb.free()
        return None
    bmesh.ops.recalc_face_normals(fb, faces=fb.faces[:])
    caps = getattr(p, "border_caps", 'NONE')
    if caps != 'NONE' and len(loops_out) >= 2:
        _add_end_caps(fb, p, loops_out, caps)
    _add_grille(fb, p, rb, W, F, B)
    _add_pull_tabs(fb, p, rb, W, O, F, B)
    return fb


def _add_end_caps(fb, p, loops_out, caps):
    """Close the ends of a ring-shaped filter (cartridge / cylinder): a plate across
    each chosen border ring, optionally with a round hole in the middle."""
    uvl = fb.loops.layers.uv.get(UV_LAYER) or fb.loops.layers.uv.new(UV_LAYER)
    rings = []
    for fv, m_, sec in loops_out:
        # the section point at the very end of the frame, closest to the axis
        idx = min(range(m_), key=lambda j: (round(sec[j][0], 9), sec[j][1]))
        ring = [fv[k * m_ + idx] for k in range(len(fv) // m_)]
        if len(ring) >= 3:
            c = sum((v.co for v in ring), Vector()) / len(ring)
            rings.append((ring, c))
    if len(rings) < 2:
        return
    axis = rings[-1][1] - rings[0][1]
    if axis.length < 1e-9:
        return
    hd = _height_dir(p)
    if axis.dot(hd) < 0:
        axis = -axis
    axis.normalize()
    order = sorted(rings, key=lambda rc: rc[1].dot(axis))
    chosen = {'BOTH': order, 'BOTTOM': order[:1], 'TOP': order[-1:]}.get(caps, [])
    hole = max(0.0, min(0.95, getattr(p, "border_cap_hole", 0.0)))
    for ring, c in chosen:
        nrm = Vector()
        for a, b in zip(ring, ring[1:] + ring[:1]):
            nrm += (a.co - c).cross(b.co - c)
        if nrm.length < 1e-12:
            continue
        nrm.normalize()
        ref = Vector((1, 0, 0)) if abs(nrm.x) < 0.9 else Vector((0, 1, 0))
        ex = (ref - nrm * ref.dot(nrm)).normalized()
        ey = nrm.cross(ex)

        def uv_of(co):
            d = co - c
            return (d.dot(ex), d.dot(ey))
        new_faces = []
        if hole < 1e-4:
            try:
                new_faces.append(fb.faces.new(ring))
            except ValueError:
                pass
        else:
            inner = [fb.verts.new(c + (v.co - c) * hole) for v in ring]
            n = len(ring)
            for k in range(n):
                k2 = (k + 1) % n
                try:
                    new_faces.append(fb.faces.new((ring[k], ring[k2], inner[k2], inner[k])))
                except ValueError:
                    pass
        for f in new_faces:
            # face the plate away from the filter (outwards along the axis)
            f.normal_update()
            if f.normal.dot(c - sum((r[1] for r in rings), Vector()) / len(rings)) < 0:
                f.normal_flip()
            for lp in f.loops:
                lp[uvl].uv = uv_of(lp.vert.co)


# ---------------------------------------------------------------------------
# Boolean cutter
# ---------------------------------------------------------------------------

# ---- die-cut face, wire mesh support, pull tabs and printed graphics --------

GRILLE_PATTERNS = (
    ('NONE', "None", "Open frame, no face sheet"),
    ('DIAMOND', "Diamond", "Die-cut board face with diamond openings, like a furnace filter"),
    ('SQUARE', "Square", "Face sheet with a grid of rectangular openings"),
)
WIRE_PATTERNS = (
    ('DIAMOND', "Diamond", "Diagonal wires pleated with the media: scallops across the pleats"),
    ('SQUARE', "Square", "Wires along and across the pleats"),
)
PRINT_ATTR_MAT = "FM Print Ink"


def _islands(faces):
    seen, out = set(), []
    for f in faces:
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
        out.append(comp)
    return out


def _grille_lines(pattern, uc, vc, pu, pv, sh, cu, cv, box):
    """Cut lines (point, normal) of the opening pattern inside the (u, v) box."""
    u0, u1, v0, v1 = box
    corners = [(u - uc, v - vc) for u in (u0, u1) for v in (v0, v1)]
    lines = []
    if pattern == 'DIAMOND':
        g = math.sqrt(1.0 / pu ** 2 + 1.0 / pv ** 2)
        for sgn in (1.0, -1.0):
            vals = [x / pu + sgn * y / pv + 0.5 for x, y in corners]
            nrm = (1.0 / pu / g, sgn / pv / g)
            for k in range(int(math.floor(min(vals))) - 1, int(math.ceil(max(vals))) + 2):
                for off in (-sh, sh):
                    a = k + off
                    lines.append(((uc + (a - 0.5) * pu, vc), nrm))
        for n in range(int(math.floor(2 * (u0 - uc) / pu)) - 1, int(math.ceil(2 * (u1 - uc) / pu)) + 2):
            for off in (-cu, cu):
                lines.append(((uc + n * pu / 2.0 + off, vc), (1.0, 0.0)))
        for n in range(int(math.floor(2 * (v0 - vc) / pv)) - 1, int(math.ceil(2 * (v1 - vc) / pv)) + 2):
            for off in (-cv, cv):
                lines.append(((uc, vc + n * pv / 2.0 + off), (0.0, 1.0)))
    else:
        for n in range(int(math.floor((u0 - uc) / pu)) - 1, int(math.ceil((u1 - uc) / pu)) + 2):
            for off in (-sh, sh):
                lines.append(((uc + (n + 0.5) * pu + off, vc), (1.0, 0.0)))
        for n in range(int(math.floor((v0 - vc) / pv)) - 1, int(math.ceil((v1 - vc) / pv)) + 2):
            for off in (-sh, sh):
                lines.append(((uc, vc + (n + 0.5) * pv + off), (0.0, 1.0)))
    return lines


def _grille_open(pattern, x, y, pu, pv, sh, cu, cv):
    """True where (x, y) (relative to the pattern centre) is inside an opening."""
    if pattern == 'DIAMOND':
        a = x / pu + y / pv + 0.5
        b = x / pu - y / pv + 0.5
        if abs(a - round(a)) <= sh or abs(b - round(b)) <= sh:
            return False
        k, m = math.floor(a), math.floor(b)
        ul, ur = pu * (k + m - 1) / 2.0, pu * (k + m + 1) / 2.0
        vb, vt = pv * (k - m - 1) / 2.0, pv * (k - m + 1) / 2.0
        return x - ul > cu and ur - x > cu and y - vb > cv and vt - y > cv
    du = (0.5 - abs(x / pu - round(x / pu))) * pu     # struts run between the cells, at +-pu/2
    dv = (0.5 - abs(y / pv - round(y / pv))) * pv
    return du > sh and dv > sh


def _seg_dist2d(px, py, segs):
    """Distance from 2D points to a set of 2D segments ((x0, y0, x1, y1), ...)."""
    if not segs:
        return [1e9] * len(px)
    if np is not None:
        S = np.array(segs, dtype=np.float64)
        P = np.stack([np.asarray(px, dtype=np.float64), np.asarray(py, dtype=np.float64)], 1)
        A, D = S[:, :2], S[:, 2:] - S[:, :2]
        LL = np.maximum((D * D).sum(1), 1e-20)
        out = np.empty(len(P))
        chunk = max(1, 200000 // len(S))
        for s in range(0, len(P), chunk):
            Q = P[s:s + chunk, None, :]
            t = np.clip(((Q - A[None]) * D[None]).sum(2) / LL[None], 0.0, 1.0)
            V = Q - (A[None] + t[..., None] * D[None])
            out[s:s + chunk] = np.sqrt((V * V).sum(2)).min(1)
        return out.tolist()
    out = []
    for x, y in zip(px, py):
        best = 1e18
        for x0, y0, x1, y1 in segs:
            dx, dy = x1 - x0, y1 - y0
            ll = dx * dx + dy * dy
            t = 0.0 if ll < 1e-20 else max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / ll))
            ex, ey = x - x0 - dx * t, y - y0 - dy * t
            best = min(best, ex * ex + ey * ey)
        out.append(math.sqrt(best))
    return out


def _grille_island(fb, p, comp, W, heights):
    """Die-cut face sheets for one connected piece of the reference surface.

    The piece is unrolled to (u, v) surface coordinates (so the pattern keeps its size
    around folds and on curved panels), cut along the pattern lines in that flat space,
    the openings are removed, and the result is mapped back onto the surface, lifted to
    the frame's front / back face and given board thickness."""
    tmp = bmesh.new()
    try:
        vm = {}
        for f in comp:
            vl = []
            for v in f.verts:
                if v not in vm:
                    vm[v] = tmp.verts.new(v.co)
                vl.append(vm[v])
            try:
                tmp.faces.new(vl)
            except ValueError:
                pass
        if not tmp.faces:
            return
        tmp.verts.index_update()
        tmp.normal_update()
        bnd = set()
        for e in tmp.edges:
            if len(e.link_faces) == 1:
                bnd.add((min(e.verts[0].index, e.verts[1].index), max(e.verts[0].index, e.verts[1].index)))
        f0 = max(tmp.faces, key=lambda f: f.calc_area())
        n0 = f0.normal.normalized()
        if p.mode == 'CURVE':
            ra = _height_dir(p)
            ra = (ra - n0 * ra.dot(n0))
            ra = ra.normalized() if ra.length > 1e-9 else Vector((0.0, 0.0, 1.0))
            ua = ra.cross(n0).normalized()
        else:
            longest, ua = -1.0, None
            for e in tmp.edges:
                if len(e.link_faces) == 1:
                    d = e.verts[1].co - e.verts[0].co
                    d = d - n0 * d.dot(n0)
                    if d.length > longest:
                        longest, ua = d.length, d
            if ua is None or ua.length < 1e-9:
                ref = Vector((1.0, 0.0, 0.0)) if abs(n0.x) < 0.9 else Vector((0.0, 1.0, 0.0))
                ua = ref - n0 * ref.dot(n0)
            ua = ua.normalized()
            ra = n0.cross(ua).normalized()
        bmesh.ops.triangulate(tmp, faces=tmp.faces[:])
        tmp.verts.index_update()
        cos = [v.co.copy() for v in tmp.verts]
        tris = [tuple(v.index for v in f.verts) for f in tmp.faces if len(f.verts) == 3]
    finally:
        tmp.free()
    if not tris:
        return
    tri_uv, vnorm, ring = _surface_param(cos, tris, ua, ra)

    g2 = bmesh.new()
    try:
        L = [g2.verts.layers.float.new(n) for n in ("px", "py", "pz", "nx", "ny", "nz")]
        idl = g2.verts.layers.int.new("src")
        wm = {}
        for t, tri in enumerate(tris):
            vl = []
            for c in range(3):
                u, v = tri_uv[t][c]
                key = (tri[c], round(u * 1e6), round(v * 1e6))
                bv = wm.get(key)
                if bv is None:
                    bv = g2.verts.new((u, v, 0.0))
                    co, nn = cos[tri[c]], vnorm[tri[c]]
                    for lay, val in zip(L, (co.x, co.y, co.z, nn.x, nn.y, nn.z)):
                        bv[lay] = val
                    bv[idl] = tri[c]
                    wm[key] = bv
                vl.append(bv)
            try:
                g2.faces.new(vl)
            except ValueError:
                pass
        if not g2.faces:
            return
        # outline in (u, v): flat boundary edges that are also boundary edges of the piece
        segs, lines = [], {}
        for e in g2.edges:
            if len(e.link_faces) != 1:
                continue
            a, b = e.verts
            if (min(a[idl], b[idl]), max(a[idl], b[idl])) not in bnd:
                continue
            x0, y0, x1, y1 = a.co.x, a.co.y, b.co.x, b.co.y
            dx, dy = x1 - x0, y1 - y0
            ll = math.hypot(dx, dy)
            if ll < 1e-12:
                continue
            segs.append((x0, y0, x1, y1))
            nx, ny = -dy / ll, dx / ll
            c = e.link_faces[0].calc_center_median()
            if (c.x - x0) * nx + (c.y - y0) * ny < 0:
                nx, ny = -nx, -ny
            off = nx * x0 + ny * y0 + W
            key = (round(math.atan2(ny, nx), 4), round(off, 6))
            lines.setdefault(key, ((x0 + nx * W, y0 + ny * W), (nx, ny), (x0, y0, x1, y1)))
        us = [v.co.x for v in g2.verts]
        vs = [v.co.y for v in g2.verts]
        box = (min(us), max(us), min(vs), max(vs))
        span_u, span_v = box[1] - box[0], box[3] - box[2]
        uc, vc = 0.5 * (box[0] + box[1]), 0.5 * (box[2] + box[3])
        cells = max(1, int(p.grille_cells))
        aspect = max(0.05, p.grille_aspect)
        inner_u = (span_u - 2.0 * W) if ring <= 0.0 else 1e18
        inner_v = span_v - 2.0 * W
        if inner_u <= inner_v:
            pu = max(inner_u, 1e-5) / cells
            pv = pu * aspect
        else:
            pv = max(inner_v, 1e-5) / cells
            pu = pv / aspect
        if ring > 0.0:
            pu = ring / max(1, round(ring / pu))
        pattern = p.grille_pattern
        smin = min(pu, pv)
        s_half = 0.5 * max(0.0, min(0.9, p.grille_strut)) * smin
        if pattern == 'DIAMOND':
            g = math.sqrt(1.0 / pu ** 2 + 1.0 / pv ** 2)
            sh = s_half * g                                  # strut half width in lattice units
            cu = sh * pu + max(0.0, min(0.95, p.grille_chamfer)) * (pu / 2.0 - sh * pu)
            cv = sh * pv + max(0.0, min(0.95, p.grille_chamfer)) * (pv / 2.0 - sh * pv)
        else:
            sh, cu, cv = s_half, 0.0, 0.0
        for pt, nn in _grille_lines(pattern, uc, vc, pu, pv, sh, cu, cv, box):
            bmesh.ops.bisect_plane(g2, geom=g2.verts[:] + g2.edges[:] + g2.faces[:], dist=1e-7,
                                   plane_co=(pt[0], pt[1], 0.0), plane_no=(nn[0], nn[1], 0.0))
        # the inset frame band: cut along each outline edge moved inward by W (after the
        # pattern cuts, so the faces are small and every one near the edge gets cut)
        reach = W + max(pu, pv)
        for (pt, nn, seg) in lines.values():
            flist = list(g2.faces)
            if len(lines) <= 24:
                near = flist
            else:
                vlist = list(g2.verts)
                dv = dict(zip(vlist, _seg_dist2d([v.co.x for v in vlist], [v.co.y for v in vlist], [seg])))
                near = [f for f in flist if min(dv[v] for v in f.verts) < reach]
            if not near:
                continue
            geom = list({v for f in near for v in f.verts}) + list({e for f in near for e in f.edges}) + near
            bmesh.ops.bisect_plane(g2, geom=geom, dist=1e-7, plane_co=(pt[0], pt[1], 0.0),
                                   plane_no=(nn[0], nn[1], 0.0))
        faces = list(g2.faces)
        cents = [f.calc_center_median() for f in faces]
        dist = _seg_dist2d([c.x for c in cents], [c.y for c in cents], segs)
        dead = [f for f, c, d in zip(faces, cents, dist)
                if d > W * 0.999 and _grille_open(pattern, c.x - uc, c.y - vc, pu, pv, sh, cu, cv)]
        if dead:
            bmesh.ops.delete(g2, geom=dead, context='FACES')
        if not g2.faces:
            return
        uvl = g2.loops.layers.uv.new(UV_LAYER)
        for f in g2.faces:
            for lp in f.loops:
                lp[uvl].uv = (lp.vert.co.x, lp.vert.co.y)
        tmpme = bpy.data.meshes.new(".FM grille")
        try:
            for sign, h in heights:
                for v in g2.verts:
                    nrm = Vector((v[L[3]], v[L[4]], v[L[5]]))
                    nrm = nrm.normalized() if nrm.length > 1e-9 else Vector((0.0, 0.0, 1.0))
                    v.co = Vector((v[L[0]], v[L[1]], v[L[2]])) + nrm * h
                one = g2.copy()
                try:
                    bmesh.ops.remove_doubles(one, verts=one.verts[:], dist=1e-7)
                    t = max(p.board_thickness, 1e-6)
                    lay = [one.verts.layers.float.get(n) for n in ("nx", "ny", "nz")]
                    ret = bmesh.ops.extrude_face_region(one, geom=one.verts[:] + one.edges[:] + one.faces[:],
                                                        use_keep_orig=True)
                    for v in ret["geom"]:
                        if isinstance(v, bmesh.types.BMVert):
                            nrm = Vector((v[lay[0]], v[lay[1]], v[lay[2]]))
                            nrm = nrm.normalized() if nrm.length > 1e-9 else Vector((0.0, 0.0, 1.0))
                            v.co += nrm * (t * sign)
                    bmesh.ops.recalc_face_normals(one, faces=one.faces[:])
                    for n in ("px", "py", "pz", "nx", "ny", "nz"):
                        lay_ = one.verts.layers.float.get(n)
                        if lay_ is not None:
                            one.verts.layers.float.remove(lay_)
                    lay_ = one.verts.layers.int.get("src")
                    if lay_ is not None:
                        one.verts.layers.int.remove(lay_)
                    tmpme.clear_geometry()
                    one.to_mesh(tmpme)
                finally:
                    one.free()
                fb.from_mesh(tmpme)
        finally:
            bpy.data.meshes.remove(tmpme)
    finally:
        g2.free()


def _add_grille(fb, p, rb, W, F, B):
    """Die-cut face sheets over the front and / or back of the frame."""
    if getattr(p, "grille_pattern", 'NONE') == 'NONE' or not rb.faces:
        return
    side = p.grille_side
    heights = []
    if side in {'FRONT', 'BOTH'}:
        heights.append((1.0, F))
    if side in {'BACK', 'BOTH'}:
        heights.append((-1.0, -B))
    rb.faces.ensure_lookup_table()
    for comp in _islands(list(rb.faces)):
        try:
            _grille_island(fb, p, comp, W, heights)
        except Exception:
            traceback.print_exc()


def _outline_frames(rb, tol):
    """Straight outline runs of the reference surface: (start, end, normal, inward, length)."""
    rb.verts.ensure_lookup_table()
    rb.verts.index_update()
    rb.normal_update()
    pairs, edge_f = [], {}
    for e in rb.edges:
        if len(e.link_faces) == 1:
            a, b = e.verts[0].index, e.verts[1].index
            pairs.append((a, b))
            edge_f[(min(a, b), max(a, b))] = e.link_faces[0]
    cos = [v.co.copy() for v in rb.verts]
    out = []
    for ids, closed in _chains(pairs):
        if closed:
            ids = ids + [ids[0]]
        pts = [cos[i] for i in ids]
        keep = _dp_keep(pts, tol)
        for a, b in zip(keep, keep[1:]):
            i0, i1 = ids[a], ids[a + 1]
            f = edge_f.get((min(i0, i1), max(i0, i1)))
            d = pts[b] - pts[a]
            if f is None or d.length < 1e-9:
                continue
            n = f.normal.normalized()
            inw = n.cross(d.normalized())
            if inw.length < 1e-9:
                continue
            inw.normalize()
            if inw.dot(f.calc_center_median() - (pts[a] + pts[b]) * 0.5) < 0:
                inw = -inw
            out.append((pts[a].copy(), pts[b].copy(), n, inw, d.length))
    return out


def _tab_sides(frames, count):
    if not frames or count <= 0:
        return []
    order = sorted(frames, key=lambda s: -s[4])
    picked = [order[0]]
    if count > 1:
        opp = [s for s in order[1:] if s[3].dot(order[0][3]) < -0.9]
        picked.append(opp[0] if opp else (order[1] if len(order) > 1 else None))
    return [s for s in picked if s is not None]


def _plate(bm, poly, base, t_axis, o_axis, n_axis, h0, h1):
    """Extruded 2D polygon (t, o) between heights h0 and h1 along n_axis."""
    bot = [bm.verts.new(base + t_axis * t + o_axis * o + n_axis * h0) for t, o in poly]
    top = [bm.verts.new(base + t_axis * t + o_axis * o + n_axis * h1) for t, o in poly]
    made = []
    for ring in (bot, list(reversed(top))):
        try:
            made.append(bm.faces.new(ring))
        except ValueError:
            pass
    k = len(poly)
    for i in range(k):
        j = (i + 1) % k
        try:
            made.append(bm.faces.new((bot[i], bot[j], top[j], top[i])))
        except ValueError:
            pass
    return made


def _add_pull_tabs(fb, p, rb, W, O, F, B):
    count = int(getattr(p, "pull_tabs", 0))
    if count <= 0 or not rb.faces:
        return
    small = _rb_small(rb) or 1.0
    frames = _outline_frames(rb, max(W, 0.01 * small) * 0.05)
    t = max(p.board_thickness, 1e-5)
    front_face = p.grille_pattern != 'NONE' and p.grille_side in {'FRONT', 'BOTH'}
    h0 = F
    made = []
    for a, b, n, inw, length in _tab_sides(frames, count):
        d = (b - a) / length
        out_v = -inw
        w = min(p.tab_width, length * 0.8)
        lt = max(p.tab_length, 1e-5)
        base = a + d * (length * p.tab_position)
        o0 = 0.0 if front_face else -0.5 * W
        oe = O + lt
        rr = min(w * 0.5, oe - o0) * 0.6
        poly = [(-w / 2, o0), (w / 2, o0)]
        steps = 6
        for i in range(steps + 1):                     # outer right corner
            ang = 0.5 * math.pi * i / steps
            poly.append((w / 2 - rr + rr * math.cos(ang), oe - rr + rr * math.sin(ang)))
        for i in range(steps + 1):                     # outer left corner
            ang = 0.5 * math.pi + 0.5 * math.pi * i / steps
            poly.append((-w / 2 + rr + rr * math.cos(ang), oe - rr + rr * math.sin(ang)))
        clean = []
        for q in poly:
            if not clean or abs(q[0] - clean[-1][0]) + abs(q[1] - clean[-1][1]) > 1e-9:
                clean.append(q)
        made += _plate(fb, clean, base, d, out_v, n, h0, h0 + t)
    if made:
        bmesh.ops.recalc_face_normals(fb, faces=made)
        uvl = fb.loops.layers.uv.get(UV_LAYER) or fb.loops.layers.uv.new(UV_LAYER)
        for f in made:
            for lp in f.loops:
                c = lp.vert.co
                lp[uvl].uv = (c.x + c.z, c.y + c.z)


# ---- wire mesh support ----------------------------------------------------------

def build_wire_splines(bm, p):
    """Polylines of a wire mesh laid on the pleated sheet. The wires follow the unrolled
    (U, V) coordinates of the media, so they are pleated with it."""
    uvl = bm.loops.layers.uv.get(UV_LAYER)
    if uvl is None or not bm.faces:
        return []
    sp = max(p.wire_spacing, 1e-5)
    if p.wire_pattern == 'DIAMOND':
        ang = max(math.radians(5.0), min(math.radians(85.0), p.wire_angle))
        fams = [(math.cos(ang) / sp, math.sin(ang) / sp), (math.cos(ang) / sp, -math.sin(ang) / sp)]
    else:
        fams = [(1.0 / sp, 0.0), (0.0, 1.0 / sp)]
    side = 1.0 if p.wire_side == 'FRONT' else -1.0
    lift = max(p.wire_radius, 1e-6) * 1.05
    if side < 0 and p.use_thickness:
        lift += p.thickness
    bm.normal_update()
    tris = bm.calc_loop_triangles()
    pos = {}
    nxt = {}

    def point(la, lb, fa, fb_, k, fam):
        va, vb = la.vert, lb.vert
        key = (min(va.index, vb.index), max(va.index, vb.index), fam, k,
               round(min(fa, fb_) * 1e4), round(max(fa, fb_) * 1e4))
        if key not in pos:
            t = (k - fa) / (fb_ - fa)
            co = va.co.lerp(vb.co, t)
            nn = va.normal.lerp(vb.normal, t)
            if nn.length > 1e-9:
                nn.normalize()
            pos[key] = co + nn * (lift * side)
        return key

    bm.verts.index_update()
    for fi, (cu, cv) in enumerate(fams):
        for tri in tris:
            vals = [lp[uvl].uv.x * cu + lp[uvl].uv.y * cv for lp in tri]
            lo, hi = min(vals), max(vals)
            k0, k1 = math.floor(lo) + 1, math.floor(hi)
            if k1 < k0:
                continue
            for k in range(k0, k1 + 1):
                ends = []
                for a in range(3):
                    b = (a + 1) % 3
                    fa, fb_ = vals[a], vals[b]
                    if (fa < k) != (fb_ < k):
                        ends.append(point(tri[a], tri[b], fa, fb_, k, fi))
                if len(ends) == 2 and ends[0] != ends[1]:
                    nxt.setdefault(ends[0], []).append(ends[1])
                    nxt.setdefault(ends[1], []).append(ends[0])
    # chain the segments into polylines
    seen = set()
    lines = []
    starts = [k for k, v in nxt.items() if len(v) == 1] + list(nxt.keys())
    for s in starts:
        if s in seen:
            continue
        seen.add(s)
        line = [s]
        cur, prev = s, None
        while True:
            cand = [c for c in nxt.get(cur, ()) if c != prev and c not in seen]
            if not cand:
                break
            prev, cur = cur, cand[0]
            seen.add(cur)
            line.append(cur)
        if len(line) >= 2:
            lines.append([pos[k] for k in line])
    return lines


def _curve_child(obj, p, attr, suffix):
    ob = getattr(p, attr, None)
    if ob is None or ob.name not in bpy.data.objects or ob.parent != obj or ob.type != 'CURVE':
        ob = _find_child(obj, attr)
        if ob is not None and ob.type != 'CURVE':
            _remove_child(p, attr)
            ob = None
    if ob is None:
        name = obj.name + suffix
        cu = bpy.data.curves.new(name, 'CURVE')
        cu.dimensions = '3D'
        ob = bpy.data.objects.new(name, cu)
        cols = obj.users_collection
        (cols[0] if cols else bpy.context.scene.collection).objects.link(ob)
        ob.parent = obj
    setattr(p, attr, ob)
    return ob


def wire_material():
    mat = style_material('FRAME', 'GALVANIZED')
    return mat


def update_wire(obj, p, bm):
    if not getattr(p, "use_wire", False) or bm is None or not bm.faces:
        _remove_child(p, "wire_obj")
        return
    lines = build_wire_splines(bm, p)
    ob = _curve_child(obj, p, "wire_obj", "_Wire")
    cu = ob.data
    cu.splines.clear()
    cu.dimensions = '3D'
    cu.bevel_mode = 'ROUND'
    cu.bevel_depth = max(p.wire_radius, 1e-6)
    cu.bevel_resolution = 1
    cu.use_fill_caps = True
    cu.resolution_u = 1
    for pts in lines:
        s = cu.splines.new('POLY')
        s.points.add(len(pts) - 1)
        flat = []
        for c in pts:
            flat += (c.x, c.y, c.z, 1.0)
        s.points.foreach_set("co", flat)
    cu.materials.clear()
    m = wire_material()
    if m is not None:
        cu.materials.append(m)
    ob.hide_select = False


# ---- printed graphics ---------------------------------------------------------

_TEXT_CACHE = {}


def _text_shape(body):
    """Outline mesh of a text string: (verts2d, faces), cap height 1, origin bottom-left."""
    hit = _TEXT_CACHE.get(body)
    if hit is not None:
        return hit
    cu = bpy.data.curves.new(".FM text", 'FONT')
    cu.body = body
    cu.size = 1.0
    ob = bpy.data.objects.new(".FM text", cu)
    verts, faces = [], []
    linked = False
    try:
        me = None
        try:
            me = ob.to_mesh()
        except Exception:
            me = None
        if me is None or not len(me.polygons):
            if me is not None:
                ob.to_mesh_clear()
            bpy.context.scene.collection.objects.link(ob)
            linked = True
            dg = bpy.context.evaluated_depsgraph_get()
            dg.update()
            ev = ob.evaluated_get(dg)
            me = ev.to_mesh()
            verts = [(v.co.x, v.co.y) for v in me.vertices]
            faces = [tuple(pl.vertices) for pl in me.polygons]
            ev.to_mesh_clear()
        else:
            verts = [(v.co.x, v.co.y) for v in me.vertices]
            faces = [tuple(pl.vertices) for pl in me.polygons]
            ob.to_mesh_clear()
    except Exception:
        traceback.print_exc()
    finally:
        if linked:
            try:
                bpy.context.scene.collection.objects.unlink(ob)
            except Exception:
                pass
        bpy.data.objects.remove(ob)
        bpy.data.curves.remove(cu)
    if verts:
        xs = [v[0] for v in verts]
        ys = [v[1] for v in verts]
        h = max(max(ys) - min(ys), 1e-9)
        x0, y0 = min(xs), min(ys)
        verts = [((x - x0) / h, (y - y0) / h) for x, y in verts]
    res = (verts, faces)
    if verts:
        _TEXT_CACHE[body] = res
    return res


def _auto_size_label(frames, O, depth):
    if not frames:
        return ""
    order = sorted(frames, key=lambda s: -s[4])
    a = order[0][4] + 2 * O
    b = None
    for s in order[1:]:
        if abs(s[3].dot(order[0][3])) < 0.3:
            b = s[4] + 2 * O
            break
    if b is None:
        return ""
    def fmt(x):            # nominal inches, like real labels: 15.5 in actual size reads "16"
        return "%d" % max(1, math.ceil(x / 0.0254 - 0.25))
    w, h = sorted((a, b))
    return "%s x %s x %s" % (fmt(w), fmt(h), fmt(depth))


def print_material():
    mat = bpy.data.materials.get(PRINT_ATTR_MAT)
    if mat is not None and mat.get("fm_style_v") == STYLE_VERSION:
        return mat
    if mat is None:
        mat = bpy.data.materials.new(PRINT_ATTR_MAT)
    try:
        nodes, links, bsdf, _out = _mat_nodes(mat)
        _setin(bsdf, "Base Color", (0.03, 0.05, 0.09, 1.0))
        _setin(bsdf, "Roughness", 0.55)
        mat.diffuse_color = (0.03, 0.05, 0.09, 1.0)
        mat["fm_style_v"] = STYLE_VERSION
    except Exception:
        traceback.print_exc()
    return mat


def _print_strip(pb, origin, xdir, ydir, room, th, items, arrow_h, down):
    """Lay text / arrow items side by side, centred on `origin`, in the plane (xdir, ydir).
    xdir x ydir must point out of the surface so the print reads the right way round."""
    widths, shapes = [], []
    for kind, body in items:
        if kind == "text":
            vs2, fs2 = _text_shape(body)
            shapes.append((vs2, fs2))
            widths.append(max((v[0] for v in vs2), default=0.0) * th)
        else:
            shapes.append(None)
            widths.append(1.1 * arrow_h)
    if not widths:
        return
    gap = 0.6 * th
    total = sum(widths) + gap * (len(widths) - 1)
    scale = min(1.0, 0.8 * room / max(total, 1e-9))
    x = -0.5 * total * scale
    for (kind, _body), w, shp in zip(items, widths, shapes):
        if kind == "text" and shp and shp[0]:
            vs = [pb.verts.new(origin + xdir * (x + vx * th * scale) + ydir * ((vy - 0.5) * th * scale))
                  for vx, vy in shp[0]]
            for f in shp[1]:
                try:
                    pb.faces.new([vs[i] for i in f])
                except ValueError:
                    pass
        elif kind == "arrow":
            aw = w * scale
            hh = arrow_h * scale
            shaft, head, cx = 0.18 * aw, 0.5 * aw, x + 0.5 * aw
            tip, neck, tail = down * hh, down * (hh - 0.45 * aw), -down * hh
            poly = [(cx - shaft, tail), (cx + shaft, tail), (cx + shaft, neck), (cx + head, neck),
                    (cx, tip), (cx - head, neck), (cx - shaft, neck)]
            if down < 0:
                poly.reverse()
            vs = [pb.verts.new(origin + xdir * px + ydir * py) for px, py in poly]
            try:
                pb.faces.new(vs)
            except ValueError:
                pass
        x += (w + gap) * scale


def build_print(p, rb, W, O, F, B):
    """Printed size label, airflow text and arrow on the outer sides of the frame and / or
    label and text on the front face of the frame."""
    small = _rb_small(rb) or 1.0
    frames = _outline_frames(rb, max(W, 0.01 * small) * 0.05)
    if not frames:
        return None
    H = F + B
    longest = max(s[4] for s in frames)
    place = getattr(p, "print_place", 'SIDES')
    sides = [s for s in sorted(frames, key=lambda s: -s[4]) if s[4] > 0.6 * longest and s[4] > 4.0 * H][:2]
    if not sides and place != 'SIDES':
        sides = [s for s in sorted(frames, key=lambda s: -s[4]) if s[4] > 0.6 * longest][:2]
    if not sides:
        return None
    front_face = p.grille_pattern != 'NONE' and p.grille_side in {'FRONT', 'BOTH'}
    label = p.print_label.strip() or _auto_size_label(frames, O, H + (p.board_thickness if p.grille_pattern != 'NONE' else 0.0))
    flow = p.print_text.strip()
    front_text = getattr(p, "print_front_text", "").strip()
    down = -1.0 if p.print_flow == 'TO_BACK' else 1.0
    pb = bmesh.new()
    eps = max(2e-4, 0.002 * H)
    up = Vector((0.0, 0.0, 1.0))
    tab_sides = _tab_sides(frames, int(p.pull_tabs))

    def placed(seg):
        a, b, n, inw, length = seg
        tdir = (b - a) / length
        mid = (a + b) * 0.5
        room = length
        if any(seg is t for t in tab_sides):
            # keep the print clear of the pull tab: use the larger free part of the side
            tab_at = a + tdir * (length * p.tab_position)
            xt = (tab_at - mid).dot(tdir)
            mid = mid - tdir * (0.25 * length * (1.0 if xt >= 0.0 else -1.0))
            room = 0.5 * length
        return tdir, mid, room

    if place in {'SIDES', 'BOTH'}:
        for seg in sides:
            _a, _b, n, inw, length = seg
            if length <= 4.0 * H:
                continue
            tdir, mid, room = placed(seg)
            out_v = -inw
            xdir = tdir if tdir.cross(n).dot(out_v) > 0 else -tdir
            items = [("text", t) for t in (label, flow) if t] + [("arrow", None)]
            origin = mid + out_v * (O + eps) + n * (0.5 * (F - B))
            _print_strip(pb, origin, xdir, n, room, 0.28 * H, items, 0.4 * H, down)
    if place in {'FRONT', 'BOTH'}:
        # one strip on the frame band, on the top edge when there is one, reading upright
        top = max(frames, key=lambda s: (-s[3]).dot(up))
        seg = top if (-top[3]).dot(up) > 0.5 and top[4] > 0.3 * longest else sides[0]
        _a, _b, n, inw, length = seg
        tdir, mid, room = placed(seg)
        out_v = -inw
        if abs(out_v.dot(up)) > 0.5:
            ydir = out_v if out_v.dot(up) > 0 else -out_v
            xdir = ydir.cross(n)
        else:
            xdir = tdir if tdir.dot(up) >= 0 else -tdir
            ydir = n.cross(xdir)
        items = [("text", t) for t in ((front_text,) if front_text else (label, flow)) if t]
        h = F + (p.board_thickness if front_face else 0.0) + eps
        origin = mid + inw * (0.5 * (W - O)) + n * h
        _print_strip(pb, origin, xdir, ydir, room, 0.45 * (W + O), items, 0.0, down)
    if not pb.faces:
        pb.free()
        return None
    bmesh.ops.remove_doubles(pb, verts=pb.verts[:], dist=1e-9)
    uvl = pb.loops.layers.uv.new(UV_LAYER)
    for f in pb.faces:
        for lp in f.loops:
            c = lp.vert.co
            lp[uvl].uv = (c.x + c.y, c.z)
    return pb


def update_print(obj, p, rb, W, O, F, B):
    if not (getattr(p, "use_print", False) and p.use_border) or rb is None or not rb.faces:
        _remove_child(p, "print_obj")
        return
    pb = build_print(p, rb, W, O, F, B)
    if pb is None:
        _remove_child(p, "print_obj")
        return
    try:
        _write_child(obj, p, "print_obj", "_Print", pb, print_material())
    finally:
        pb.free()


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
# Regeneration / object children
# ---------------------------------------------------------------------------

CHILD_ATTRS = ("particles_obj", "hair_obj", "dust_obj", "border_obj", "cutter_obj", "fuzz_obj",
               "wire_obj", "print_obj")


_CHILD_SUFFIX = {
    "particles_obj": "_Particles",
    "hair_obj": "_Hair",
    "dust_obj": "_Dust",
    "border_obj": "_Border",
    "cutter_obj": "_Cutter",
    "fuzz_obj": "_Fuzz",
    "wire_obj": "_Wire",
    "print_obj": "_Print",
}


def _find_child(obj, attr):
    """Find an existing generated child even if its PointerProperty was lost."""
    suffix = _CHILD_SUFFIX.get(attr)
    if obj is None or not suffix:
        return None
    pattern = re.compile(re.escape(suffix) + r"(?:\.\d{3})?$")
    for child in obj.children:
        if child.type in {'MESH', 'CURVE'} and pattern.search(child.name):
            return child
    return None


def _remove_child(p, attr):
    ob = getattr(p, attr, None)
    owner = getattr(p, "id_data", None)
    if ob is None and isinstance(owner, bpy.types.Object):
        ob = _find_child(owner, attr)
    if ob:
        data = ob.data
        bpy.data.objects.remove(ob, do_unlink=True)
        if data is not None and data.users == 0:
            if isinstance(data, bpy.types.Mesh):
                bpy.data.meshes.remove(data)
            elif isinstance(data, bpy.types.Curve):
                bpy.data.curves.remove(data)
    try:
        setattr(p, attr, None)
    except Exception:
        pass


def _write_child(obj, p, attr, suffix, cbm, mat):
    ob = getattr(p, attr, None)
    if ob is None or ob.name not in bpy.data.objects or ob.parent != obj:
        ob = _find_child(obj, attr)
    if ob is not None and ob.type != 'MESH':
        _remove_child(p, attr)
        ob = None
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


# ---------------------------------------------------------------------------
# Particles: one emitter mesh, two editable systems, instanced from generated
# collections of hair-strand curves and dust pieces
# ---------------------------------------------------------------------------

LIB_ROOT = "Filter Maker Library"
EXPOSED_VG = "FM_Exposed"
HAIR_SYS = "Filter Maker Hair"
DUST_SYS = "Filter Maker Dust"
_LEGACY_EMITTER = re.compile(r"_(?:Hair|Dust)Particles(?:\.\d{3})?$")


def _setp(owner, name, value):
    """Set an RNA property if this Blender version has it; never raise."""
    try:
        setattr(owner, name, value)
        return True
    except Exception:
        return False


# ---- border coverage -------------------------------------------------------

# ---- distance to the reference outline (border falloff, noise fade) -------

DIST_ATTR = "fm_border_dist"


def _rb_small(rb):
    """Narrowest width of the reference surface: twice the largest distance from any of
    its points to the outline. (A world-space bounding box underestimates curved panels:
    a cylinder section is shallow along one axis but wide across its surface.)"""
    xs = [v.co.x for v in rb.verts]
    ys = [v.co.y for v in rb.verts]
    zs = [v.co.z for v in rb.verts]
    if not xs:
        return None
    ext = [max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)]
    mx = max(ext)
    nz = [e for e in ext if e > 1e-6 * max(mx, 1e-9)]
    bbox_small = min(nz) if nz else mx
    try:
        segs = _outline_segments(rb, max(mx, 1e-9) * 1e-4)
        if segs:
            inner = max(_outline_distance([tuple(v.co) for v in rb.verts], segs))
            if 1e-9 < inner < 1e8:
                return max(bbox_small, 2.0 * inner)
    except Exception:
        traceback.print_exc()
    return bbox_small


def _dp_keep(pts, tol):
    """Douglas-Peucker: indices of the polyline points needed within tolerance."""
    n = len(pts)
    if n <= 2:
        return list(range(n))
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    tol2 = tol * tol
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        pa, pb = pts[a], pts[b]
        ab = pb - pa
        ll = ab.length_squared
        best, idx = -1.0, -1
        for i in range(a + 1, b):
            q = pts[i] - pa
            if ll > 1e-20:
                t = max(0.0, min(1.0, q.dot(ab) / ll))
                d2 = (q - ab * t).length_squared
            else:
                d2 = q.length_squared
            if d2 > best:
                best, idx = d2, i
        if best > tol2:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [i for i in range(n) if keep[i]]


def _outline_segments(rb, tol):
    """Simplified outline of the reference surface as (start, end, normal) triples."""
    rb.verts.ensure_lookup_table()
    rb.verts.index_update()
    rb.normal_update()
    pairs, edge_n = [], {}
    for e in rb.edges:
        if len(e.link_faces) == 1:
            a, b = e.verts[0].index, e.verts[1].index
            pairs.append((a, b))
            edge_n[(min(a, b), max(a, b))] = e.link_faces[0].normal.copy()
    cos = [v.co.copy() for v in rb.verts]
    segs = []
    for ids, closed in _chains(pairs):
        if closed:
            ids = ids + [ids[0]]
        pts = [cos[i] for i in ids]
        keep = _dp_keep(pts, tol)
        for a, b in zip(keep, keep[1:]):
            i0, i1 = ids[a], ids[a + 1]
            n = edge_n.get((min(i0, i1), max(i0, i1)), Vector((0.0, 0.0, 1.0)))
            segs.append((pts[a], pts[b], n))
    return segs


def _outline_distance(points, segs):
    """In-plane distance from each point to the outline (pleat depth is ignored)."""
    if not segs:
        return [1e9] * len(points)
    if np is not None:
        P0 = np.array([tuple(s[0]) for s in segs], dtype=np.float64)
        P1 = np.array([tuple(s[1]) for s in segs], dtype=np.float64)
        NN = np.array([tuple(s[2]) for s in segs], dtype=np.float64)
        D = P1 - P0
        LL = np.maximum((D * D).sum(1), 1e-20)
        pts = np.array(points, dtype=np.float64).reshape(-1, 3)
        out = np.empty(len(pts))
        chunk = max(1, 300000 // len(segs))
        for s in range(0, len(pts), chunk):
            Q = pts[s:s + chunk, None, :]
            t = np.clip(((Q - P0[None]) * D[None]).sum(2) / LL[None], 0.0, 1.0)
            V = Q - (P0[None] + t[..., None] * D[None])
            V = V - (V * NN[None]).sum(2)[..., None] * NN[None]
            out[s:s + chunk] = np.sqrt((V * V).sum(2)).min(1)
        return out.tolist()
    out = []
    for q in points:
        q = Vector(q)
        best = 1e18
        for a, b, n in segs:
            ab = b - a
            ll = ab.length_squared
            t = 0.0 if ll < 1e-20 else max(0.0, min(1.0, (q - a).dot(ab) / ll))
            d = q - (a + ab * t)
            d = d - n * d.dot(n)
            best = min(best, d.length_squared)
        out.append(math.sqrt(best))
    return out


def _smoothstep(x):
    x = 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)
    return x * x * (3.0 - 2.0 * x)



# ---- generated instance collections --------------------------------------

def _find_layer_coll(lc, coll):
    if lc.collection == coll:
        return lc
    for ch in lc.children:
        found = _find_layer_coll(ch, coll)
        if found is not None:
            return found
    return None


LIB_COLOR = 'COLOR_08'                             # brown in the Outliner


def _library_root():
    scene = bpy.context.scene
    root = bpy.data.collections.get(LIB_ROOT)
    if root is None:
        root = bpy.data.collections.new(LIB_ROOT)
    _setp(root, "color_tag", LIB_COLOR)
    kids = scene.collection.children
    if root.name not in kids:
        try:
            kids.link(root)
        except Exception:
            pass
    elif len(kids) and kids[len(kids) - 1] != root:
        # keep the library as the last entry of the Scene Collection
        try:
            kids.unlink(root)
            kids.link(root)
        except Exception:
            pass
    # Excluded from every view layer: the pieces are only seen through the particles.
    for vl in scene.view_layers:
        lc = _find_layer_coll(vl.layer_collection, root)
        if lc is not None and not lc.exclude:
            lc.exclude = True
    return root


def _clear_collection(coll):
    for ob in list(coll.objects):
        data = ob.data
        bpy.data.objects.remove(ob, do_unlink=True)
        if data is not None and data.users == 0:
            if isinstance(data, bpy.types.Mesh):
                bpy.data.meshes.remove(data)
            elif isinstance(data, bpy.types.Curve):
                bpy.data.curves.remove(data)


# One shared hair collection and one shared dust collection for the whole file.
# Every filter instances the same pieces. The settings that shape the pieces
# (curl, strand shapes, dust types, piece variants, dust material) are therefore
# shared: changing them on one filter rebuilds the pieces and copies the values to
# every other filter. Density, length, width, hair material, lift, tilt, seeds etc.
# stay per filter (strands are plain curves; the scatter gives them their thickness).
HAIR_LIB_NAME = "FM Hair Strands"
DUST_LIB_NAME = "FM Dust Pieces"
_LIB_SOURCE = set()          # {(kind, object name)}: rebuild the shared pieces from this filter


def _all_filters():
    out = []
    for ob in bpy.data.objects:
        fp = getattr(ob, "filter_maker", None) if ob is not None else None
        if fp is not None and fp.is_filter:
            out.append(ob)
    return out


def _shared_coll(name):
    coll = bpy.data.collections.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
    root = _library_root()
    _setp(coll, "color_tag", LIB_COLOR)
    if coll.name not in root.children:
        try:
            root.children.link(coll)
        except Exception:
            pass
    return coll


def _lib_users(coll, exclude=None):
    """Filters (other than `exclude`) that still instance pieces from `coll`."""
    ex = exclude.as_pointer() if exclude is not None else None
    users = []
    for ob in bpy.data.objects:                 # baked (finished) filters still use them
        fp = getattr(ob, "filter_maker", None) if ob is not None else None
        if fp is None:
            continue
        if ex is not None and fp.as_pointer() == ex:
            continue
        if (fp.hair_lib == coll and fp.use_hair) or (fp.dust_lib == coll and fp.use_dust):
            users.append(ob)
    return users


def _drop_coll_if_unused(coll, exclude=None):
    if coll is None or coll.name not in bpy.data.collections:
        return
    if _lib_users(coll, exclude):
        return
    _clear_collection(coll)
    bpy.data.collections.remove(coll)
    root = bpy.data.collections.get(LIB_ROOT)
    if root is not None and not root.children and not root.objects:
        bpy.data.collections.remove(root)


def _remove_lib(p, attr):
    """This filter no longer needs its pieces; the shared collection is only deleted
    when no other filter uses it."""
    coll = getattr(p, attr, None)
    _setp(p, attr, None)
    _drop_coll_if_unused(coll, exclude=p)


def _lib_variants(kind, p):
    """Pieces in the shared collection: the most any filter using it asks for. Each filter
    picks from the first N of them (its own setting); the pieces are generated in a fixed
    order, so the first N are the same whatever the total."""
    attr, use = ("hair_variants", "use_hair") if kind == 'hair' else ("dust_variants", "use_dust")
    n = int(getattr(p, attr))
    for ob in _all_filters():
        fp = ob.filter_maker
        if getattr(fp, use):
            n = max(n, int(getattr(fp, attr)))
    return n


def _hair_params(p):
    return {"curl": round(float(p.hair_curl), 4), "variants": _lib_variants('hair', p), "v": 2}


def _dust_params(p):
    return {"types": sorted(p.dust_types), "variants": _lib_variants('dust', p),
            "mat": p.dust_material.name if p.dust_material else "", "v": 2}


def _apply_hair_params(fp, prm):
    if abs(fp.hair_curl - prm["curl"]) > 1e-6:
        fp.hair_curl = prm["curl"]


def _apply_dust_params(fp, prm):
    if sorted(fp.dust_types) != prm["types"]:
        fp.dust_types = set(prm["types"])
    mat = bpy.data.materials.get(prm["mat"]) if prm["mat"] else None
    if fp.dust_material != mat:
        fp.dust_material = mat


def _bezier_from_points(cu, pts, radii):
    """Smooth Bezier spline through pts (Catmull-Rom style handles)."""
    sp = cu.splines.new('BEZIER')
    sp.bezier_points.add(len(pts) - 1)
    n = len(pts)
    for i, bp in enumerate(sp.bezier_points):
        prev_p = pts[i - 1] if i > 0 else pts[i]
        next_p = pts[i + 1] if i < n - 1 else pts[i]
        tan = (next_p - prev_p) * (0.5 if 0 < i < n - 1 else 1.0)
        bp.co = pts[i]
        bp.handle_left_type = 'FREE'
        bp.handle_right_type = 'FREE'
        bp.handle_left = pts[i] - tan / 3.0
        bp.handle_right = pts[i] + tan / 3.0
        bp.radius = radii[i]
    return sp


HAIR_SHAPES = ('bend', 's', 'curl', 'straight', 'hook', 'kink', 'bend', 'curl')


def _strand_points(rng, curl, kind, n=8):
    """One unit-length strand along +X (root at the origin) that lies flat: its bends
    stay in the local XY plane (the filter surface); Z only gets a slight wobble."""
    side = 1.0 if rng.random() < 0.5 else -1.0
    e1 = Vector((0.0, side, 0.0))                  # sideways, along the surface
    e2 = Vector((0.0, 0.0, 1.0))                   # off the surface (kept small)
    c = 0.35 + max(0.0, curl)
    turns = 1.0 + 2.0 * min(curl, 2.0) + rng.uniform(0.0, 0.8)
    amp = (0.05 + 0.10 * min(curl, 2.0)) * rng.uniform(0.7, 1.2)
    lean = rng.uniform(0.04, 0.2)
    wobble = rng.uniform(0.0, 0.03)
    pts = []
    for i in range(n):
        t = i / (n - 1)
        off = e1 * (lean * t * t) + e2 * (wobble * math.sin(t * 5.0 + rng.random()))
        if kind == 'bend':
            off = off + e1 * (0.25 * c * t * t)
        elif kind == 's':
            off = off + e1 * (0.12 * c * math.sin(2.0 * math.pi * t))
        elif kind == 'curl':
            a = 2.0 * math.pi * turns * t
            off = off + (e1 * math.sin(a) + Vector((-1.0, 0.0, 0.0)) * (1.0 - math.cos(a)) * 0.5) * (amp * (0.3 + t))
        elif kind == 'kink':
            off = off + e1 * (0.08 * c * (1.0 if i % 2 else -1.0) * t)
        elif kind == 'hook':
            off = off + e1 * (0.4 * c * t ** 4)
        pts.append(Vector((t, 0.0, 0.0)) + off)
    length = sum((b - a).length for a, b in zip(pts, pts[1:]))
    return [q / max(length, 1e-9) for q in pts]


HAIR_TAPER = 0.18              # each end tapers over this share of the strand


def _strand_radii(n):
    """Constant thickness along the strand; both ends close to a point with a smooth
    taper over the first and last HAIR_TAPER of the length."""
    out = []
    for k in range(n):
        t = k / (n - 1)
        u = min(t, 1.0 - t) / HAIR_TAPER          # 0 at either end, >= 1 in the body
        out.append(1.0 if u >= 1.0 else math.sin(0.5 * math.pi * u))
    return out


def _build_hair_lib(coll, prm):
    """Unit-length strand curves without bevel. The scatter turns them into tubes of
    the filter's own Width, so the thickness never depends on the strand length."""
    rng = random.Random(1000)
    for i in range(max(1, int(prm["variants"]))):
        kind = HAIR_SHAPES[i % len(HAIR_SHAPES)]
        pts = _strand_points(rng, prm["curl"], kind, n=22)
        n = len(pts)
        radii = _strand_radii(n)
        name = "FM Strand %02d" % (i + 1)
        cu = bpy.data.curves.new(name, 'CURVE')
        cu.dimensions = '3D'
        cu.bevel_depth = 0.0
        cu.resolution_u = 4
        _setp(cu, "twist_mode", 'MINIMUM')
        _bezier_from_points(cu, pts, radii)
        ob = bpy.data.objects.new(name, cu)
        coll.objects.link(ob)


DUST_KINDS = (('FLAKE', "Flakes", "Thin irregular flakes lying on the surface"),
              ('CLUMP', "Clumps", "Soft lumpy dust balls"),
              ('GRIT', "Grit", "Small hard angular bits"),
              ('LINT', "Lint", "Short curly fibres"))


def _dust_mesh_piece(rng, kind, name):
    bm = bmesh.new()
    try:
        if kind == 'FLAKE':
            k = rng.randint(5, 9)
            ring = []
            for i in range(k):
                a = 2.0 * math.pi * (i + rng.uniform(-0.25, 0.25)) / k
                r = 0.5 * rng.uniform(0.65, 1.0)
                ring.append((r * math.cos(a), r * math.sin(a)))
            th = 0.06
            low = [bm.verts.new((0.0, y, z)) for y, z in ring]
            high = [bm.verts.new((th + 0.05 * (1.0 - (y * y + z * z) * 4.0), y, z)) for y, z in ring]
            bm.faces.new(list(reversed(low)))
            bm.faces.new(high)
            for i in range(k):
                j = (i + 1) % k
                bm.faces.new((low[i], low[j], high[j], high[i]))
        elif kind == 'CLUMP':
            try:
                bmesh.ops.create_icosphere(bm, subdivisions=2, radius=0.36)
            except TypeError:
                bmesh.ops.create_icosphere(bm, subdivisions=2, diameter=0.72)
            for v in bm.verts:
                v.co *= rng.uniform(0.78, 1.12)
                v.co.x = v.co.x * 0.7 + 0.22
            for f in bm.faces:
                f.smooth = True
        else:                                           # GRIT
            try:
                bmesh.ops.create_cube(bm, size=0.42)
            except TypeError:
                bmesh.ops.create_cube(bm)
            for v in bm.verts:
                v.co += Vector((rng.uniform(-0.09, 0.09), rng.uniform(-0.09, 0.09), rng.uniform(-0.09, 0.09)))
                v.co.x += 0.16
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
        me = bpy.data.meshes.new(name)
        bm.to_mesh(me)
        return me
    finally:
        bm.free()


def _lint_curve(rng, name):
    """A short curly fibre lying along the surface (in the YZ plane, slightly raised)."""
    phi = rng.uniform(0.0, 2.0 * math.pi)
    d = Vector((0.0, math.cos(phi), math.sin(phi)))
    s = Vector((0.0, -math.sin(phi), math.cos(phi)))
    pts = []
    for i in range(7):
        t = i / 6.0 - 0.5
        pts.append(d * (1.6 * t) + s * (0.18 * math.sin(t * 9.0 + phi)) + Vector((0.05 + 0.04 * math.cos(t * 6.0), 0, 0)))
    cu = bpy.data.curves.new(name, 'CURVE')
    cu.dimensions = '3D'
    _setp(cu, "fill_mode", 'FULL')
    cu.bevel_depth = 0.025
    cu.bevel_resolution = 1
    cu.resolution_u = 4
    _setp(cu, "use_fill_caps", True)
    _bezier_from_points(cu, pts, [1.0] * len(pts))
    return cu


def _build_dust_lib(coll, prm):
    rng = random.Random(2000)
    kinds = [k for k, _l, _d in DUST_KINDS if k in prm["types"]] or ['CLUMP']
    mat = bpy.data.materials.get(prm["mat"]) if prm["mat"] else None
    for i in range(max(1, int(prm["variants"]))):
        kind = kinds[i % len(kinds)]
        name = "FM Dust %02d %s" % (i + 1, kind.title())     # sorts in build order
        data = _lint_curve(rng, name) if kind == 'LINT' else _dust_mesh_piece(rng, kind, name)
        if mat is not None:
            data.materials.append(mat)
        ob = bpy.data.objects.new(name, data)
        coll.objects.link(ob)


_KINDS = {
    'hair': ("hair_lib", HAIR_LIB_NAME, _hair_params, _apply_hair_params, _build_hair_lib),
    'dust': ("dust_lib", DUST_LIB_NAME, _dust_params, _apply_dust_params, _build_dust_lib),
}


def _sync_lib(obj, p, kind):
    """Point this filter at the shared collection. If this filter just changed a shared
    setting (or the pieces do not exist yet) the pieces are rebuilt from its values and
    the values are copied to every other filter; otherwise the filter adopts the shared
    values."""
    attr, name, params_of, apply_to, builder = _KINDS[kind]
    old = getattr(p, attr, None)
    coll = _shared_coll(name)
    stored = coll.get("fm_params")
    source = (kind, obj.name) in _LIB_SOURCE
    _LIB_SOURCE.discard((kind, obj.name))
    prev = _busy[0]
    _busy[0] = True
    try:
        if source or not stored or not coll.objects:
            prm = params_of(p)
            key = json.dumps(prm, sort_keys=True)
            if key != stored or not coll.objects:
                _clear_collection(coll)
                builder(coll, prm)
                coll["fm_params"] = key
            for other in _all_filters():
                fp = other.filter_maker
                if fp.as_pointer() != p.as_pointer():
                    apply_to(fp, prm)
        else:
            st = json.loads(stored)
            apply_to(p, st)
            need = params_of(p)["variants"]
            if need > int(st.get("variants", 0)):
                # this filter wants more pieces than the shared set has: extend the set
                # (same look, the existing pieces stay the same)
                st["variants"] = need
                _clear_collection(coll)
                builder(coll, st)
                coll["fm_params"] = json.dumps(st, sort_keys=True)
        if old != coll:
            setattr(p, attr, coll)
    finally:
        _busy[0] = prev
    if old is not None and old != coll:
        _drop_coll_if_unused(old)          # per-filter collection from 0.6.5 and older
    return coll


# ---- emitter object and the two systems -----------------------------------

def _remove_legacy_emitters(obj, p):
    for attr in ("hair_obj", "dust_obj"):
        _remove_child(p, attr)
    for child in list(obj.children):
        if child.type == 'MESH' and _LEGACY_EMITTER.search(child.name):
            data = child.data
            bpy.data.objects.remove(child, do_unlink=True)
            if data is not None and data.users == 0:
                bpy.data.meshes.remove(data)


def _write_emitter(obj, p, surface, dists):
    ob = getattr(p, "particles_obj", None)
    if ob is None or ob.name not in bpy.data.objects or ob.parent != obj:
        ob = _find_child(obj, "particles_obj")
    if ob is None:
        name = obj.name + "_Particles"
        ob = bpy.data.objects.new(name, bpy.data.meshes.new(name))
        cols = obj.users_collection
        (cols[0] if cols else bpy.context.scene.collection).objects.link(ob)
        ob.parent = obj
    p.particles_obj = ob
    me = ob.data
    me.clear_geometry()
    surface.to_mesh(me)
    me.materials.clear()
    attr = me.attributes.get(DIST_ATTR)
    if attr is None or attr.domain != 'POINT' or attr.data_type != 'FLOAT':
        if attr is not None:
            me.attributes.remove(attr)
        attr = me.attributes.new(DIST_ATTR, 'FLOAT', 'POINT')
    attr.data.foreach_set("value", [float(d) for d in dists])
    me.update()
    ob.display_type = 'TEXTURED'
    ob.hide_render = False
    _setp(ob, "show_instancer_for_render", True)
    _setp(ob, "show_instancer_for_viewport", True)
    return ob




def _remove_psys(emitter, name):
    """Remove a particle system left over from 0.4.0."""
    for mod in list(emitter.modifiers):
        if mod.type == 'PARTICLE_SYSTEM' and mod.particle_system.name == name:
            settings = mod.particle_system.settings
            emitter.modifiers.remove(mod)
            if settings is not None and settings.users == 0:
                bpy.data.particles.remove(settings)


# ---------------------------------------------------------------------------
# Geometry Nodes scatter: points on the particle mesh, instanced from the
# generated hair / dust collections.
#
# Each filter gets its own node group and every setting lives on a named node
# inside it ("FM Hair Density", the Collection Info nodes, ...). The add-on
# writes the N-panel values straight into those nodes, so nothing has to be
# connected by hand and it does not depend on modifier input properties
# (which Blender 5.x no longer accepts from Python as item assignment).
# ---------------------------------------------------------------------------

SCATTER_VERSION = 12
SCATTER_MOD = "Filter Maker Scatter"
SCATTER_PREFIX = "FilterMaker Scatter "
DUST_SEED_OFFSET = 7919                    # hair and dust never share spawn points

SCATTER_VALUES = (
    # name, kind, default
    ("Hair Viewport", 'FLOAT', 1.0),
    ("Dust Viewport", 'FLOAT', 1.0),
    ("Side", 'FLOAT', 1.0),
    ("Hair Density", 'FLOAT', 0.0),
    ("Hair Size", 'FLOAT', 0.02),
    ("Hair Size Random", 'FLOAT', 0.5),
    ("Hair Lift", 'FLOAT', 0.2),
    ("Hair Seed", 'INT', 0),
    ("Dust Density", 'FLOAT', 0.0),
    ("Dust Size", 'FLOAT', 0.004),
    ("Dust Size Random", 'FLOAT', 0.6),
    ("Dust Tilt", 'FLOAT', 0.8),
    ("Dust Seed", 'INT', 0),
    ("Hair Pad", 'FLOAT', 0.0),
    ("Dust Pad", 'FLOAT', 0.0),
    ("Hair Falloff", 'FLOAT', 0.05),
    ("Dust Falloff", 'FLOAT', 0.05),
    ("Hair Width", 'FLOAT', 0.0005),
    ("Hair Variant Max", 'INT', 1000000),   # pick from the first N pieces only (N - 1)
    ("Dust Variant Max", 'INT', 1000000),
    ("Debug", 'FLOAT', 0.0),          # 1 = store per-strand attributes (used by the tests)
)
HAIR_SAMPLES = 24                     # points per strand after resampling
HAIR_SIDES = 6                        # sides of the strand tube


def _ng_sock(sockets, name, types=None):
    """Socket by name (and type); prefers enabled sockets, as some nodes keep hidden twins."""
    fallback = None
    for s in sockets:
        if s.name == name and (types is None or s.type in types):
            if getattr(s, "enabled", True):
                return s
            fallback = fallback or s
    if fallback is not None:
        return fallback
    raise KeyError("socket '%s' not found (has: %s)" % (name, ", ".join(s.name for s in sockets)))


def _build_scatter(group, ctx):
    if not hasattr(group, "interface"):
        raise RuntimeError("the hair/dust scatter needs Blender 4.0 or newer")
    group.interface.new_socket(name="Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    group.interface.new_socket(name="Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')

    nodes, links = group.nodes, group.links
    F, VEC, INT, ROT = {'VALUE'}, {'VECTOR'}, {'INT'}, {'ROTATION', 'VECTOR'}

    def N(idname, x, y, **props):
        ctx["at"] = "node " + idname
        node = nodes.new(idname)
        node.location = (x, y)
        for k, v in props.items():
            setattr(node, k, v)
        return node

    def L(out_sock, in_sock):
        ctx["at"] = "link %s -> %s.%s" % (out_sock.name, in_sock.node.bl_idname, in_sock.name)
        links.new(out_sock, in_sock)

    def math_node(op, x, y, a, b=None):
        node = N('ShaderNodeMath', x, y, operation=op)
        for i, v in enumerate((a, b)):
            if v is None:
                continue
            if isinstance(v, (int, float)):
                node.inputs[i].default_value = v
            else:
                L(v, node.inputs[i])
        return node.outputs[0]

    def vmath(op, x, y, a, b=None, scale=None, out=0):
        node = N('ShaderNodeVectorMath', x, y, operation=op)
        L(a, node.inputs[0])
        if b is not None:
            L(b, node.inputs[1])
        if scale is not None:
            if isinstance(scale, (int, float)):
                node.inputs[3].default_value = scale
            else:
                L(scale, node.inputs[3])
        return node.outputs[out]

    def random_value(kind, x, y, lo, hi, seed, offset):
        node = N('FunctionNodeRandomValue', x, y, data_type=kind)
        types = VEC if kind == 'FLOAT_VECTOR' else (INT if kind == 'INT' else F)
        for nm, v in (("Min", lo), ("Max", hi)):
            sock = _ng_sock(node.inputs, nm, types)
            if isinstance(v, (int, float, tuple)):
                sock.default_value = v
            else:
                L(v, sock)
        L(math_node('ADD', x - 180, y - 120, seed, float(offset)), _ng_sock(node.inputs, "Seed", INT))
        return _ng_sock(node.outputs, "Value", types)

    # ---- settings nodes (written by sync_scatter) ----
    val = {}
    for i, (name, kind, default) in enumerate(SCATTER_VALUES):
        if kind == 'INT':
            node = N('FunctionNodeInputInt', -1800, 900 - i * 90)
            node.integer = int(default)
        else:
            node = N('ShaderNodeValue', -1800, 900 - i * 90)
            node.outputs[0].default_value = float(default)
        node.name = node.label = "FM " + name
        val[name] = node.outputs[0]

    gin = N('NodeGroupInput', -1600, 0)
    gout = N('NodeGroupOutput', 4100, 0)
    geo_in = _ng_sock(gin.outputs, "Geometry")

    # Viewport thinning: factor = 1 - is_viewport * (1 - amount), separately per kind
    isvp = N('GeometryNodeIsViewport', -1400, 500)

    def vp_factor(kind, y):
        return math_node('SUBTRACT', -1100, y, 1.0,
                         math_node('MULTIPLY', -1250, y, isvp.outputs[0],
                                   math_node('SUBTRACT', -1400, y - 100, 1.0, val[kind + " Viewport"])))
    mask = N('GeometryNodeInputNamedAttribute', -1400, 250, data_type='FLOAT')
    _ng_sock(mask.inputs, "Name").default_value = DIST_ATTR
    border_dist = _ng_sock(mask.outputs, "Attribute", F)
    def border_factor(x, y, pad, falloff_val):
        falloff = math_node('MAXIMUM', x - 320, y - 120, falloff_val, 1e-6)
        """0 under / right next to the border, smooth ramp to 1 over the falloff."""
        node = N('ShaderNodeMapRange', x, y, data_type='FLOAT', interpolation_type='SMOOTHSTEP')
        try:
            node.clamp = True
        except Exception:
            pass
        L(border_dist, _ng_sock(node.inputs, "Value", F))
        L(pad, _ng_sock(node.inputs, "From Min", F))
        L(math_node('ADD', x - 160, y - 120, pad, falloff), _ng_sock(node.inputs, "From Max", F))
        _ng_sock(node.inputs, "To Min", F).default_value = 0.0
        _ng_sock(node.inputs, "To Max", F).default_value = 1.0
        return _ng_sock(node.outputs, "Result", F)

    new_rot = all(hasattr(bpy.types, n) for n in (
        'FunctionNodeAlignRotationToVector', 'FunctionNodeAxisAngleToRotation', 'FunctionNodeRotateRotation'))

    def align(x, y, rot_in, axis, pivot, vec):
        idn = 'FunctionNodeAlignRotationToVector' if new_rot else 'FunctionNodeAlignEulerToVector'
        node = N(idn, x, y, axis=axis)
        try:
            node.pivot_axis = pivot
        except Exception:
            pass
        if rot_in is not None:
            L(rot_in, _ng_sock(node.inputs, "Rotation", ROT))
        L(vec, _ng_sock(node.inputs, "Vector", VEC))
        return _ng_sock(node.outputs, "Rotation", ROT)

    def rotate_local(x, y, rot_in, axis, angle):
        if new_rot:
            aa = N('FunctionNodeAxisAngleToRotation', x, y - 160)
            _ng_sock(aa.inputs, "Axis", VEC).default_value = axis
            L(angle, _ng_sock(aa.inputs, "Angle", F))
            node = N('FunctionNodeRotateRotation', x + 200, y)
            try:
                node.rotation_space = 'LOCAL'
            except Exception:
                pass
            L(rot_in, _ng_sock(node.inputs, "Rotation", ROT))
            L(_ng_sock(aa.outputs, "Rotation", ROT), _ng_sock(node.inputs, "Rotate By", ROT))
            return _ng_sock(node.outputs, "Rotation", ROT)
        node = N('FunctionNodeRotateEuler', x + 200, y)
        for attr, v in (("type", 'AXIS_ANGLE'), ("rotation_type", 'AXIS_ANGLE'), ("space", 'LOCAL')):
            try:
                setattr(node, attr, v)
            except Exception:
                pass
        L(rot_in, _ng_sock(node.inputs, "Rotation"))
        _ng_sock(node.inputs, "Axis", VEC).default_value = axis
        L(angle, _ng_sock(node.inputs, "Angle", F))
        return _ng_sock(node.outputs, "Rotation")

    join = N('GeometryNodeJoinGeometry', 3900, 0)
    for row, kind in enumerate(("Hair", "Dust")):
        y = 700 - row * 1500
        seed = val[kind + " Seed"]
        dseed = seed if kind == "Hair" else math_node('ADD', -1000, y - 160, seed, float(DUST_SEED_OFFSET))
        dist = N('GeometryNodeDistributePointsOnFaces', -800, y)
        try:
            dist.distribute_method = 'RANDOM'
        except Exception:
            pass
        L(geo_in, _ng_sock(dist.inputs, "Mesh"))
        L(math_node('MULTIPLY', -1000, y - 60, val[kind + " Density"], vp_factor(kind, y + 600)),
          _ng_sock(dist.inputs, "Density", F))
        L(dseed, _ng_sock(dist.inputs, "Seed", INT))
        # Border falloff per point: the distance attribute is carried onto every
        # scattered point; keep it with probability = falloff factor (exact at the
        # edge, unlike a density factor blended across faces that straddle it).
        keep = border_factor(-650, y + 260, val[kind + " Pad"], val[kind + " Falloff"])
        cull = math_node('GREATER_THAN', -450, y + 260,
                         random_value('FLOAT', -650, y + 420, 0.0, 1.0, seed, 7), keep)
        delete = N('GeometryNodeDeleteGeometry', -250, y + 120)
        for attr, v in (("domain", 'POINT'), ("mode", 'ALL')):
            try:
                setattr(delete, attr, v)
            except Exception:
                pass
        L(_ng_sock(dist.outputs, "Points"), _ng_sock(delete.inputs, "Geometry"))
        L(cull, _ng_sock(delete.inputs, "Selection"))
        points = _ng_sock(delete.outputs, "Geometry")
        normal = _ng_sock(dist.outputs, "Normal", VEC)

        # Side: +1 front, -1 back, 0 = random per point.
        rs = math_node('SIGN', -550, y - 260,
                       math_node('SUBTRACT', -700, y - 260,
                                 random_value('FLOAT', -850, y - 260, 0.0, 1.0, seed, 3), 0.5))
        side = val["Side"]
        s_used = math_node('ADD', -250, y - 260, side,
                           math_node('MULTIPLY', -400, y - 260, rs,
                                     math_node('SUBTRACT', -550, y - 380, 1.0,
                                               math_node('ABSOLUTE', -700, y - 380, side))))
        n_side = vmath('SCALE', -100, y - 200, normal, scale=s_used)
        rnd = random_value('FLOAT_VECTOR', -400, y - 560, (-1.0, -1.0, -1.0), (1.0, 1.0, 1.0), seed, 1)

        if kind == "Hair":
            # Lying strands: Z = surface normal, X = random direction along the surface,
            # then lifted off the surface by a small random angle.
            tangent = vmath('NORMALIZE', 200, y - 560,
                            vmath('SUBTRACT', 50, y - 560, rnd, vmath('PROJECT', -100, y - 620, rnd, n_side)))
            r1 = align(350, y - 260, None, 'Z', 'AUTO', n_side)
            r2 = align(550, y - 320, r1, 'X', 'Z', tangent)
            lift = random_value('FLOAT', 550, y - 700, 0.0, 1.0, seed, 6)
            angle = math_node('MULTIPLY', 750, y - 700, lift, math_node('MULTIPLY', 600, y - 820, val["Hair Lift"], -1.0))
            rotation = rotate_local(800, y - 360, r2, (0.0, 1.0, 0.0), angle)
        else:
            # Dust: pieces stand on the surface (+X out of it), tumbled and spun.
            tilt = vmath('SCALE', -100, y - 420, rnd, scale=val["Dust Tilt"])
            direction = vmath('NORMALIZE', 200, y - 300, vmath('ADD', 50, y - 300, n_side, tilt))
            r1 = align(350, y - 260, None, 'X', 'AUTO', direction)
            spin = random_value('FLOAT', 350, y - 700, 0.0, 6.283185307, seed, 2)
            rotation = rotate_local(600, y - 360, r1, (1.0, 0.0, 0.0), spin)

        var = val[kind + " Size Random"]
        size_rand = random_value('FLOAT', 800, y - 900,
                                 math_node('SUBTRACT', 600, y - 860, 1.0, var),
                                 math_node('ADD', 600, y - 960, 1.0, var), seed, 4)
        scale = math_node('MULTIPLY', 1000, y - 900, size_rand, val[kind + " Size"])
        index = random_value('INT', 1000, y - 1100, 0, val[kind + " Variant Max"], seed, 5)

        coll = N('GeometryNodeCollectionInfo', 1000, y + 250)
        coll.name = coll.label = "FM %s Collection" % kind
        try:
            coll.transform_space = 'ORIGINAL'
        except Exception:
            pass
        _ng_sock(coll.inputs, "Separate Children").default_value = True
        _ng_sock(coll.inputs, "Reset Children").default_value = True

        inst = N('GeometryNodeInstanceOnPoints', 1350, y)
        L(points, _ng_sock(inst.inputs, "Points"))
        L(coll.outputs[0], _ng_sock(inst.inputs, "Instance"))
        _ng_sock(inst.inputs, "Pick Instance").default_value = True
        L(index, _ng_sock(inst.inputs, "Instance Index"))
        L(rotation, _ng_sock(inst.inputs, "Rotation"))
        L(scale, _ng_sock(inst.inputs, "Scale"))
        if kind == "Hair":
            L(_hair_tubes(N, L, math_node, val, inst.outputs[0], y), join.inputs[0])
        else:
            L(inst.outputs[0], join.inputs[0])

    L(join.outputs[0], _ng_sock(gout.inputs, "Geometry"))


def _hair_tubes(N, L, math_node, val, geo, y):
    """Strand curves -> realized tubes of constant width that taper only at the tip
    (the curve radius stored in the strand library), independent of strand length."""
    F, VEC, BOOL = {'VALUE'}, {'VECTOR'}, {'BOOLEAN'}
    x = 1550
    # optional per-strand attributes for the test suite (switched off by default)
    dbg = geo
    stores = []
    rot_in = N('GeometryNodeInputInstanceRotation', x, y - 500) if hasattr(bpy.types, 'GeometryNodeInputInstanceRotation') else None
    if rot_in is not None and hasattr(bpy.types, 'FunctionNodeRotateVector'):
        rv = N('FunctionNodeRotateVector', x + 150, y - 500)
        _ng_sock(rv.inputs, "Vector", VEC).default_value = (1.0, 0.0, 0.0)
        L(rot_in.outputs[0], _ng_sock(rv.inputs, "Rotation"))
        stores.append(("fm_xaxis", 'FLOAT_VECTOR', _ng_sock(rv.outputs, "Vector", VEC)))
    stores.append(("fm_root", 'FLOAT_VECTOR', _ng_sock(N('GeometryNodeInputPosition', x, y - 650).outputs, "Position", VEC)))
    stores.append(("fm_scale", 'FLOAT_VECTOR', _ng_sock(N('GeometryNodeInputInstanceScale', x, y - 800).outputs, "Scale", VEC)))
    for i, (name, dtype, value) in enumerate(stores):
        st = N('GeometryNodeStoreNamedAttribute', x + 350 + i * 180, y - 300, data_type=dtype, domain='INSTANCE')
        _ng_sock(st.inputs, "Name").default_value = name
        L(dbg, _ng_sock(st.inputs, "Geometry"))
        L(value, _ng_sock(st.inputs, "Value", VEC if dtype == 'FLOAT_VECTOR' else F))
        dbg = _ng_sock(st.outputs, "Geometry")
    sw = N('GeometryNodeSwitch', x + 1000, y, input_type='GEOMETRY')
    L(math_node('GREATER_THAN', x + 850, y - 200, val["Debug"], 0.5), _ng_sock(sw.inputs, "Switch", BOOL))
    L(geo, _ng_sock(sw.inputs, "False"))
    L(dbg, _ng_sock(sw.inputs, "True"))

    real = N('GeometryNodeRealizeInstances', x + 1200, y)
    L(_ng_sock(sw.outputs, "Output"), _ng_sock(real.inputs, "Geometry"))
    res = N('GeometryNodeResampleCurve', x + 1400, y)
    try:
        res.mode = 'COUNT'
    except Exception:
        pass
    L(_ng_sock(real.outputs, "Geometry"), _ng_sock(res.inputs, "Curve"))
    _ng_sock(res.inputs, "Count").default_value = HAIR_SAMPLES
    rad = N('GeometryNodeSetCurveRadius', x + 1600, y)
    L(_ng_sock(res.outputs, "Curve"), _ng_sock(rad.inputs, "Curve"))
    # Taper from the position along the strand (0 at the root, 1 at the tip), so the
    # thickness never depends on instance scale: sin(pi/2 * min(1, min(f, 1-f) / taper)).
    sp = N('GeometryNodeSplineParameter', x + 900, y - 450)
    f = _ng_sock(sp.outputs, "Factor", F)
    m = math_node('MINIMUM', x + 1050, y - 450, f, math_node('SUBTRACT', x + 1050, y - 560, 1.0, f))
    u = math_node('MINIMUM', x + 1350, y - 450, math_node('DIVIDE', x + 1200, y - 450, m, HAIR_TAPER), 1.0)
    taper = math_node('SINE', x + 1500, y - 450, math_node('MULTIPLY', x + 1400, y - 560, u, 0.5 * math.pi))
    radius = math_node('MULTIPLY', x + 1500, y - 200, taper,
                       math_node('MULTIPLY', x + 1400, y - 320, val["Hair Width"], 0.5))
    circle = N('GeometryNodeCurvePrimitiveCircle', x + 1600, y - 300)
    try:
        circle.mode = 'RADIUS'
    except Exception:
        pass
    _ng_sock(circle.inputs, "Resolution").default_value = HAIR_SIDES
    _ng_sock(circle.inputs, "Radius", F).default_value = 1.0
    tube = N('GeometryNodeCurveToMesh', x + 1800, y)
    L(_ng_sock(rad.outputs, "Curve"), _ng_sock(tube.inputs, "Curve"))
    L(_ng_sock(circle.outputs, "Curve"), _ng_sock(tube.inputs, "Profile Curve"))
    try:
        _ng_sock(tube.inputs, "Fill Caps").default_value = False   # ends already close to a point
    except KeyError:
        pass
    # Newer Blender versions size the profile with a "Scale" input instead of the
    # curve radius; feed the same field to it.
    try:
        L(radius, _ng_sock(tube.inputs, "Scale", F))
        _ng_sock(rad.inputs, "Radius", F).default_value = 1.0     # never applied twice
    except KeyError:
        L(radius, _ng_sock(rad.inputs, "Radius", F))
    mat = N('GeometryNodeSetMaterial', x + 2000, y)
    mat.name = mat.label = "FM Hair Material"
    L(_ng_sock(tube.outputs, "Mesh"), _ng_sock(mat.inputs, "Geometry"))
    smooth = N('GeometryNodeSetShadeSmooth', x + 2200, y)

    def geo_sock(sockets):              # "Geometry" in 4.x, "Mesh" in newer versions
        for nm in ("Geometry", "Mesh"):
            try:
                return _ng_sock(sockets, nm)
            except KeyError:
                pass
        return sockets[0]
    L(geo_sock(mat.outputs), geo_sock(smooth.inputs))
    return geo_sock(smooth.outputs)


def _scatter_group(emitter):
    name = emitter.get("fm_scatter_group") or (SCATTER_PREFIX + emitter.name)
    group = bpy.data.node_groups.get(name)
    if group is not None and group.get("fm_version") == SCATTER_VERSION:
        return group
    if group is None:
        group = bpy.data.node_groups.new(SCATTER_PREFIX + emitter.name, 'GeometryNodeTree')
    if "fm_version" in group:
        del group["fm_version"]
    group.nodes.clear()
    if hasattr(group, "interface"):
        group.interface.clear()
    ctx = {"at": "start"}
    try:
        _build_scatter(group, ctx)
    except Exception as exc:
        traceback.print_exc()
        raise RuntimeError("scatter setup failed at %s: %s" % (ctx["at"], exc))
    group["fm_version"] = SCATTER_VERSION
    emitter["fm_scatter_group"] = group.name
    return group


SIDE_VALUE = {'FRONT': 1.0, 'BACK': -1.0, 'BOTH': 0.0}


def sync_scatter(emitter, p, hair_coll, dust_coll):
    group = _scatter_group(emitter)
    mod = emitter.modifiers.get(SCATTER_MOD)
    if mod is None or mod.type != 'NODES':
        mod = emitter.modifiers.new(SCATTER_MOD, 'NODES')
    if mod.node_group != group:
        mod.node_group = group
    values = {
        "Hair Viewport": max(0.01, min(1.0, p.hair_viewport / 100.0)),
        "Dust Viewport": max(0.01, min(1.0, p.dust_viewport / 100.0)),
        "Side": SIDE_VALUE.get(p.particle_side, 1.0),
        "Hair Density": float(p.hair_density) if (p.use_hair and hair_coll is not None) else 0.0,
        "Hair Size": float(max(p.hair_length, 1e-6)),
        "Hair Size Random": float(min(1.0, max(0.0, p.hair_length_var))),
        "Hair Lift": float(max(0.0, p.hair_lift)),
        "Hair Seed": int(p.hair_seed),
        "Dust Density": float(p.dust_density) if (p.use_dust and dust_coll is not None) else 0.0,
        "Dust Size": float(max(p.dust_size, 1e-6)),
        "Dust Size Random": float(min(1.0, max(0.0, p.dust_size_var))),
        "Dust Tilt": float(max(0.0, p.dust_tilt)),
        "Dust Seed": int(p.dust_seed),
        # keep a gap of one full piece so nothing reaches into the frame
        "Hair Pad": float(p.hair_length * (1.0 + p.hair_length_var)),
        "Dust Pad": float(p.dust_size * (1.0 + p.dust_size_var) * 0.6),
        "Hair Falloff": float(max(0.0, p.hair_border_falloff)),
        "Hair Width": float(max(p.hair_width, 1e-7)),
        "Dust Falloff": float(max(0.0, p.dust_border_falloff)),
        "Hair Variant Max": max(0, min(int(p.hair_variants), len(hair_coll.objects) if hair_coll else 1) - 1),
        "Dust Variant Max": max(0, min(int(p.dust_variants), len(dust_coll.objects) if dust_coll else 1) - 1),
    }
    nodes = group.nodes
    for name, value in values.items():
        node = nodes.get("FM " + name)
        if node is None:
            continue
        try:
            if node.bl_idname == 'FunctionNodeInputInt':
                if node.integer != int(value):
                    node.integer = int(value)
            elif abs(node.outputs[0].default_value - float(value)) > 1e-12:
                node.outputs[0].default_value = float(value)
        except Exception:
            traceback.print_exc()
    mnode = nodes.get("FM Hair Material")
    if mnode is not None:
        msock = _ng_sock(mnode.inputs, "Material")
        if msock.default_value != p.hair_material:
            msock.default_value = p.hair_material
    for kind, coll in (("Hair", hair_coll), ("Dust", dust_coll)):
        node = nodes.get("FM %s Collection" % kind)
        if node is None:
            continue
        sock = _ng_sock(node.inputs, "Collection")
        if sock.default_value != coll:
            sock.default_value = coll
    try:
        mod.show_viewport = True
        mod.show_render = True
    except Exception:
        pass
    emitter.update_tag()
    return mod


def _remove_scatter_group_if_unused(name=None):
    for group in list(bpy.data.node_groups):
        if group.name.startswith(SCATTER_PREFIX) or group.name == "FilterMaker Scatter":
            if group.users == 0 and (name is None or group.name == name):
                bpy.data.node_groups.remove(group)


def update_particles(obj, p, surface, dist, W):
    """<filter>_Particles: the clean pleated skin (no thickness rims) with a Geometry
    Nodes scatter. Each vertex stores its distance from the border's inner edge, which
    the scatter turns into a smooth density falloff (no hair or dust under the frame)."""
    _remove_legacy_emitters(obj, p)
    if not (p.use_hair or p.use_dust) or surface is None or not len(surface.faces):
        _remove_child(p, "particles_obj")
        _remove_lib(p, "hair_lib")
        _remove_lib(p, "dust_lib")
        p.hair_count = p.dust_count = 0
        return
    far = 1e6
    if dist is not None and p.particles_avoid_border and len(dist) == len(surface.verts):
        verts = list(surface.verts)
        dmap = {v: d - W for v, d in zip(verts, dist)}
        dead = [f for f in surface.faces if all(dmap[v] < 0.0 for v in f.verts)]
        if dead:
            bmesh.ops.delete(surface, geom=dead, context='FACES')
        dists = [dmap.get(v, far) for v in surface.verts]
    else:
        dists = [far] * len(surface.verts)
    area = sum(f.calc_area() for f in surface.faces)
    if area <= 1e-12:
        _remove_child(p, "particles_obj")
        p.hair_count = p.dust_count = 0
        return
    emitter = _write_emitter(obj, p, surface, dists)
    emitter["fm_area"] = area
    for name in (HAIR_SYS, DUST_SYS):
        _remove_psys(emitter, name)
    refresh_scatter(obj, p, emitter)


def refresh_scatter(obj, p, emitter=None):
    """Push hair / dust settings into the scatter without rebuilding the filter."""
    emitter = emitter or p.particles_obj
    if emitter is None:
        return
    area = float(emitter.get("fm_area", 0.0))
    hair_coll = dust_coll = None
    if p.use_hair:
        hair_coll = _sync_lib(obj, p, 'hair')
        p.hair_count = int(round(area * max(0.0, p.hair_density)))
    else:
        _remove_lib(p, "hair_lib")
        p.hair_count = 0
    if p.use_dust:
        dust_coll = _sync_lib(obj, p, 'dust')
        p.dust_count = int(round(area * max(0.0, p.dust_density)))
    else:
        _remove_lib(p, "dust_lib")
        p.dust_count = 0
    sync_scatter(emitter, p, hair_coll, dust_coll)




# ---------------------------------------------------------------------------
# UV space, dirt masks and built-in materials
# ---------------------------------------------------------------------------
GAME_MAPS = (('NORMAL', "Normal", "Tangent-space normal map"),
             ('AO', "AO", "Ambient occlusion"),
             ('COLOR', "Base Color", "Diffuse colour of the materials"),
             ('ROUGHNESS', "Roughness", "Roughness of the materials"))
DIRT_ATTR = "fm_dirt"            # per-vertex dirt amount (0..1), read by the built-in materials
STYLE_VERSION = 2

FILTER_STYLES = (
    ('CUSTOM', "Custom", "Use the material in the Filter Material slot"),
    ('PAPER', "Paper", "White cellulose filter paper with fine fibres"),
    ('SYNTHETIC', "Synthetic", "Light blue synthetic media"),
    ('HEPA', "HEPA", "Bright white, very fine HEPA media"),
    ('FIBERGLASS', "Fibreglass", "Yellow fibreglass media"),
    ('CARBON', "Carbon", "Dark activated-carbon media with speckles"),
)
FRAME_STYLES = (
    ('CUSTOM', "Custom", "Use the material in the Border Material slot"),
    ('PAINTED', "Painted Metal", "Dark powder-coated metal"),
    ('GALVANIZED', "Galvanised", "Galvanised steel with zinc spangles"),
    ('ALUMINIUM', "Aluminium", "Brushed aluminium"),
    ('CARDBOARD', "Cardboard", "Brown corrugated cardboard"),
    ('PLASTIC', "Plastic", "Off-white moulded plastic"),
    ('RUBBER', "Rubber", "Black rubber / foam gasket"),
    ('WHITE_BOARD', "White Board", "White coated paperboard, like die-cut furnace filter frames"),
)
_MEDIA = {
    'PAPER': dict(color=(0.86, 0.84, 0.76), rough=0.88, fiber=1.0, speck=0.0, sheen=0.25),
    'SYNTHETIC': dict(color=(0.62, 0.74, 0.86), rough=0.8, fiber=0.6, speck=0.0, sheen=0.35),
    'HEPA': dict(color=(0.93, 0.94, 0.95), rough=0.9, fiber=0.35, speck=0.0, sheen=0.2),
    'FIBERGLASS': dict(color=(0.86, 0.7, 0.32), rough=0.75, fiber=1.4, speck=0.0, sheen=0.3),
    'CARBON': dict(color=(0.07, 0.07, 0.075), rough=0.92, fiber=0.4, speck=1.0, sheen=0.0),
}
_FRAMES = {
    'PAINTED': dict(color=(0.035, 0.037, 0.04), rough=0.55, metal=0.0, pattern='NOISE'),
    'GALVANIZED': dict(color=(0.62, 0.63, 0.65), rough=0.32, metal=1.0, pattern='SPANGLE'),
    'ALUMINIUM': dict(color=(0.8, 0.81, 0.83), rough=0.28, metal=1.0, pattern='BRUSHED'),
    'CARDBOARD': dict(color=(0.42, 0.3, 0.18), rough=0.92, metal=0.0, pattern='FIBER'),
    'PLASTIC': dict(color=(0.78, 0.78, 0.75), rough=0.42, metal=0.0, pattern='NOISE'),
    'RUBBER': dict(color=(0.025, 0.025, 0.027), rough=0.86, metal=0.0, pattern='NOISE'),
    'WHITE_BOARD': dict(color=(0.86, 0.86, 0.84), rough=0.82, metal=0.0, pattern='NOISE'),
}
_DIRT_COLOR = (0.2, 0.17, 0.13, 1.0)


def _apply_uv_space(me, p):
    """Turn the raw UVs (metres) into the chosen UV space."""
    uvl = me.uv_layers.get(UV_LAYER) if me is not None else None
    if uvl is None or not len(uvl.data) or np is None:
        return
    uv = np.empty(len(uvl.data) * 2, dtype=np.float32)
    uvl.data.foreach_get("uv", uv)
    uv = uv.reshape(-1, 2)
    if getattr(p, "uv_mode", 'WORLD') == 'FIT':
        lo = uv.min(axis=0)
        span = max(float((uv.max(axis=0) - lo).max()), 1e-9)
        uv = (uv - lo) / span
    else:
        uv = uv / max(getattr(p, "uv_tile", 0.1), 1e-6)
    uvl.data.foreach_set("uv", uv.astype(np.float32).ravel())


def _write_masks(bm, p, dist, W):
    """Per-vertex fm_edge (0 at the frame's inner edge -> 1 far inside) and fm_dirt
    (dirt amount x where dirt collects: away from the frame and in the folds)."""
    pl = bm.verts.layers.float.get(PLEAT_ATTR)
    el = bm.verts.layers.float.get(EDGE_ATTR) or bm.verts.layers.float.new(EDGE_ATTR)
    dl = bm.verts.layers.float.get(DIRT_ATTR) or bm.verts.layers.float.new(DIRT_ATTR)
    verts = list(bm.verts)
    amount = max(0.0, min(1.0, getattr(p, "dirt", 0.0)))
    if dist is not None and len(dist) == len(verts):
        inner = [d - W for d in dist]
        span = max(max(inner) if inner else 0.0, 1e-6)
        edge = [max(0.0, min(1.0, x / (0.6 * span))) for x in inner]
    else:
        edge = [1.0] * len(verts)
    for v, e in zip(verts, edge):
        w = v[pl] if pl is not None else 0.0
        fold = 0.5 * (1.0 - w)                      # 1 in the valleys, 0 on the ridges
        v[el] = e
        v[dl] = amount * _smoothstep(e) * (0.6 + 0.4 * fold)


def _set_frame_dirt(fb, p):
    dl = fb.verts.layers.float.get(DIRT_ATTR) or fb.verts.layers.float.new(DIRT_ATTR)
    amount = 0.45 * max(0.0, min(1.0, getattr(p, "dirt", 0.0)))
    for v in fb.verts:
        v[dl] = amount


def _mat_nodes(mat):
    """Fresh node tree; returns (nodes, links, principled, output)."""
    safe = getattr(mat, "use_nodes", None)
    if safe is not None and not mat.use_nodes:
        try:
            mat.use_nodes = True
        except Exception:
            pass
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (900, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (600, 0)
    nt.links.new(bsdf.outputs[0], out.inputs[0])
    return nt.nodes, nt.links, bsdf, out


_SOCK_ALIASES = {"Fac": ("Factor",), "Factor": ("Fac",), "Color": ("Colour",)}


def _sock(sockets, names, types=None):
    """Shader socket by name, tolerant of renames between Blender versions
    (5.x renamed the texture 'Fac' outputs to 'Factor')."""
    names = list(names if isinstance(names, (list, tuple)) else (names,))
    for nm in list(names):
        names += [a for a in _SOCK_ALIASES.get(nm, ()) if a not in names]
    for nm in names:
        try:
            return _ng_sock(sockets, nm, types)
        except KeyError:
            pass
    # outputs only: fall back to the first enabled socket of the right type
    if types and len(sockets) and getattr(sockets[0], "is_output", False):
        for s in sockets:
            if s.type in types and getattr(s, "enabled", True):
                return s
    return None


def _link(links, a, b):
    """links.new that tells which socket was missing instead of a bare TypeError."""
    if a is None or b is None:
        raise RuntimeError("material socket missing (%s -> %s)" % (a, b))
    return links.new(a, b)


def _setin(node, names, value, types=None):
    s = _sock(node.inputs, names, types)
    if s is not None:
        try:
            s.default_value = value
        except Exception:
            pass
    return s


def _mix_rgb(nodes, links, x, y, fac, a, b):
    """Colour mix (A -> B by fac). fac/a/b are sockets or values."""
    m = nodes.new("ShaderNodeMix")
    m.location = (x, y)
    m.data_type = 'RGBA'
    C = {'RGBA'}
    for names, v, types in ((("Factor", "Fac"), fac, {'VALUE'}), (("A",), a, C), (("B",), b, C)):
        s = _sock(m.inputs, names, types)
        if s is None:
            continue
        if hasattr(v, "is_output"):
            _link(links, v, s)
        else:
            s.default_value = v
    return _sock(m.outputs, "Result", C)


def _math(nodes, links, x, y, op, a, b=None):
    n = nodes.new("ShaderNodeMath")
    n.location = (x, y)
    n.operation = op
    for i, v in enumerate((a, b)):
        if v is None:
            continue
        if hasattr(v, "is_output"):
            _link(links, v, n.inputs[i])
        else:
            n.inputs[i].default_value = v
    return n.outputs[0]


def _attr(nodes, x, y, name):
    a = nodes.new("ShaderNodeAttribute")
    a.location = (x, y)
    a.attribute_name = name
    try:
        a.attribute_type = 'GEOMETRY'
    except Exception:
        pass
    return _sock(a.outputs, "Fac", {'VALUE'})


def _uv(nodes, x, y):
    n = nodes.new("ShaderNodeUVMap")
    n.location = (x, y)
    try:
        n.uv_map = UV_LAYER
    except Exception:
        pass
    return n.outputs[0]


def _tex(nodes, links, kind, x, y, vec, **inputs):
    n = nodes.new(kind)
    n.location = (x, y)
    for k, v in inputs.items():
        if k.startswith("_"):
            try:
                setattr(n, k[1:], v)
            except Exception:
                pass
        else:
            _setin(n, k.replace("_", " "), v)
    if vec is not None:
        s = _sock(n.inputs, "Vector", {'VECTOR'})
        if s is not None:
            _link(links, vec, s)
    return n


def _dirt_layer(nodes, links, uv, base_col, rough_in):
    """Mix dirt over a colour/roughness using the fm_dirt attribute and a breakup noise."""
    dirt = _attr(nodes, -900, -500, DIRT_ATTR)
    brk = _tex(nodes, links, "ShaderNodeTexNoise", -900, -700, uv, Scale=3.0, Detail=6.0, Roughness=0.6)
    rng = nodes.new("ShaderNodeMapRange")
    rng.location = (-650, -700)
    _link(links, _sock(brk.outputs, "Fac", {'VALUE'}), _sock(rng.inputs, "Value", {'VALUE'}))
    _setin(rng, "From Min", 0.3, {'VALUE'})
    _setin(rng, "From Max", 0.7, {'VALUE'})
    fac = _math(nodes, links, -400, -550, 'MULTIPLY', dirt, _sock(rng.outputs, "Result", {'VALUE'}))
    fac = _math(nodes, links, -250, -550, 'MULTIPLY', fac, 1.8)
    fac = _math(nodes, links, -100, -550, 'MINIMUM', fac, 1.0)
    col = _mix_rgb(nodes, links, 150, -200, fac, base_col, _DIRT_COLOR)
    rough = _math(nodes, links, 150, -450, 'ADD', rough_in, _math(nodes, links, 0, -450, 'MULTIPLY', fac, 0.12))
    return col, rough


def _build_media(mat, style):
    st = _MEDIA[style]
    nodes, links, bsdf, _out = _mat_nodes(mat)
    uv = _uv(nodes, -1300, 0)
    base = tuple(st["color"]) + (1.0,)
    dark = tuple(c * 0.82 for c in st["color"]) + (1.0,)
    fib = _tex(nodes, links, "ShaderNodeTexWave", -1050, 250, uv, Scale=18.0, Distortion=9.0,
               Detail=6.0, _wave_type='BANDS')
    fine = _tex(nodes, links, "ShaderNodeTexNoise", -1050, 0, uv, Scale=90.0, Detail=10.0, Roughness=0.65)
    f_fac = _math(nodes, links, -800, 200, 'MULTIPLY', _sock(fib.outputs, "Fac", {'VALUE'}), 0.35 * st["fiber"])
    f_fac = _math(nodes, links, -650, 200, 'ADD', f_fac,
                  _math(nodes, links, -800, 50, 'MULTIPLY', _sock(fine.outputs, "Fac", {'VALUE'}), 0.3))
    col = _mix_rgb(nodes, links, -450, 150, f_fac, base, dark)
    if st["speck"] > 0:
        vor = _tex(nodes, links, "ShaderNodeTexVoronoi", -1050, -250, uv, Scale=160.0)
        sp = _math(nodes, links, -800, -250, 'LESS_THAN', _sock(vor.outputs, "Distance", {'VALUE'}), 0.08)
        col = _mix_rgb(nodes, links, -300, 0, sp, col, (0.22, 0.22, 0.23, 1.0))
    col, rough = _dirt_layer(nodes, links, uv, col, st["rough"])
    _link(links, col, _sock(bsdf.inputs, "Base Color"))
    _link(links, rough, _sock(bsdf.inputs, "Roughness"))
    _setin(bsdf, ("Sheen Weight", "Sheen"), st["sheen"])
    _setin(bsdf, ("Specular IOR Level", "Specular"), 0.25)
    bump = nodes.new("ShaderNodeBump")
    bump.location = (300, -650)
    _setin(bump, "Strength", 0.25)
    _setin(bump, "Distance", 0.002)
    _link(links, f_fac, _sock(bump.inputs, "Height", {'VALUE'}))
    _link(links, _sock(bump.outputs, "Normal"), _sock(bsdf.inputs, "Normal"))
    mat.diffuse_color = base


def _build_frame(mat, style):
    st = _FRAMES[style]
    nodes, links, bsdf, _out = _mat_nodes(mat)
    uv = _uv(nodes, -1300, 0)
    base = tuple(st["color"]) + (1.0,)
    pat = st["pattern"]
    if pat == 'SPANGLE':
        t = _tex(nodes, links, "ShaderNodeTexVoronoi", -1050, 100, uv, Scale=12.0, _feature='F1')
        f = _math(nodes, links, -800, 100, 'MULTIPLY', _sock(t.outputs, "Distance", {'VALUE'}), 0.8)
    elif pat == 'BRUSHED':
        mp = nodes.new("ShaderNodeMapping")
        mp.location = (-1150, 100)
        _link(links, uv, _sock(mp.inputs, "Vector", {'VECTOR'}))
        _setin(mp, "Scale", (1.0, 120.0, 1.0), {'VECTOR'})
        t = _tex(nodes, links, "ShaderNodeTexNoise", -950, 100, _sock(mp.outputs, "Vector", {'VECTOR'}),
                 Scale=40.0, Detail=4.0)
        f = _sock(t.outputs, "Fac", {'VALUE'})
    elif pat == 'FIBER':
        t = _tex(nodes, links, "ShaderNodeTexWave", -1050, 100, uv, Scale=10.0, Distortion=6.0, Detail=5.0)
        f = _sock(t.outputs, "Fac", {'VALUE'})
    else:
        t = _tex(nodes, links, "ShaderNodeTexNoise", -1050, 100, uv, Scale=25.0, Detail=6.0)
        f = _sock(t.outputs, "Fac", {'VALUE'})
    var = _math(nodes, links, -600, 100, 'MULTIPLY', f, 0.25)
    col = _mix_rgb(nodes, links, -400, 150, var, base, tuple(c * 0.75 for c in st["color"]) + (1.0,))
    rough0 = _math(nodes, links, -400, -100, 'ADD', st["rough"], _math(nodes, links, -550, -100, 'MULTIPLY', var, 0.4))
    col, rough = _dirt_layer(nodes, links, uv, col, rough0)
    _link(links, col, _sock(bsdf.inputs, "Base Color"))
    _link(links, rough, _sock(bsdf.inputs, "Roughness"))
    _setin(bsdf, "Metallic", st["metal"])
    bump = nodes.new("ShaderNodeBump")
    bump.location = (300, -650)
    _setin(bump, "Strength", 0.15 if st["metal"] < 0.5 else 0.05)
    _setin(bump, "Distance", 0.001)
    _link(links, f, _sock(bump.inputs, "Height", {'VALUE'}))
    _link(links, _sock(bump.outputs, "Normal"), _sock(bsdf.inputs, "Normal"))
    mat.diffuse_color = base
    mat.metallic = st["metal"]


def style_material(kind, style):
    """Shared built-in material for a media ('MEDIA') or frame ('FRAME') style."""
    table = _MEDIA if kind == 'MEDIA' else _FRAMES
    if style not in table:
        return None
    label = {k: n for k, n, _d in (FILTER_STYLES if kind == 'MEDIA' else FRAME_STYLES)}[style]
    name = "FM %s %s" % ("Media" if kind == 'MEDIA' else "Frame", label)
    mat = bpy.data.materials.get(name)
    if mat is not None and mat.get("fm_style_v") == STYLE_VERSION:
        return mat
    if mat is None:
        mat = bpy.data.materials.new(name)
    try:
        (_build_media if kind == 'MEDIA' else _build_frame)(mat, style)
        mat["fm_style_v"] = STYLE_VERSION
        if "fm_style_error" in mat:
            del mat["fm_style_error"]
    except Exception as exc:
        traceback.print_exc()
        mat["fm_style_error"] = str(exc)
    return mat


def apply_styles(p):
    """Point the material slots at the chosen built-in styles (Custom keeps the slot)."""
    prev = _busy[0]
    _busy[0] = True
    try:
        if getattr(p, "filter_style", 'CUSTOM') != 'CUSTOM':
            m = style_material('MEDIA', p.filter_style)
            if m is not None and p.filter_material != m:
                p.filter_material = m
        if getattr(p, "frame_style", 'CUSTOM') != 'CUSTOM':
            m = style_material('FRAME', p.frame_style)
            if m is not None and p.border_material != m:
                p.border_material = m
    finally:
        _busy[0] = prev


# ---- the pleated sheet, cached between updates ------------------------------

LAST_TIMINGS = {}
_SHEET_KEYS = ("mode", "pleat_depth", "pleat_density", "pleat_phase", "detail", "height",
               "height_dir", "pleat_angle", "mesh_type", "face_smooth", "face_smooth_factor",
               "face_smooth_cuts", "bevel_ridge", "bevel_valley", "bevel_cuts")


def _sheet_key(p):
    vals = [p.src]
    for k in _SHEET_KEYS:
        v = getattr(p, k)
        vals.append([round(float(x), 9) for x in v] if k == "height_dir" else
                    (round(v, 9) if isinstance(v, float) else v))
    return hashlib.md5(json.dumps(vals).encode("utf-8")).hexdigest()


def _build_sheet(obj, p, src, bm):
    """Pleated sheet (before wear / thickness). Re-uses the cached copy when nothing
    that shapes the pleats has changed, so wear, thickness, border and hair / dust
    edits skip the expensive rebuild."""
    key = _sheet_key(p)
    name = obj.get("fm_sheet_cache")
    cm = bpy.data.meshes.get(name) if name else None
    if cm is not None and cm.get("fm_key") == key and len(cm.polygons):
        bm.from_mesh(cm)
        return True
    if p.mode == 'CURVE':
        build_curve(bm, p, src, None)
        if p.mesh_type == 'TRIS' and bm.faces:
            bmesh.ops.triangulate(bm, faces=bm.faces[:])
    else:
        build_face(bm, p, src, None)
    if cm is None:
        cm = bpy.data.meshes.new(".FM sheet " + obj.name)
        obj["fm_sheet_cache"] = cm.name
    cm.clear_geometry()
    bm.to_mesh(cm)
    cm["fm_key"] = key
    return False


def build_reference(rb, p, src):
    """Flat reference surface (the outline the border follows)."""
    if p.mode == 'CURVE':
        d, H = _height_dir(p), max(p.height, 1e-5)
        for path, cl in zip(src["paths"], src["closed"]):
            _wall_path(rb, [Vector(x) for x in path], cl, d, H, p.pleat_density)
        return
    tmp, _comps = _faces_bmesh(src, smooth_p=p)
    try:
        vm = {}
        for f in tmp.faces:
            vl = []
            for v in f.verts:
                if v not in vm:
                    vm[v] = rb.verts.new(v.co)
                vl.append(vm[v])
            try:
                rb.faces.new(vl)
            except ValueError:
                pass
    finally:
        tmp.free()




def regenerate(obj):
    p = getattr(obj, "filter_maker", None)
    if p is None or not p.is_filter or obj.mode != 'OBJECT':
        return
    p.last_error = ""
    bm = rb = None
    tm = {}
    t_start = t_last = time.perf_counter()

    def tick(name):
        nonlocal t_last
        now = time.perf_counter()
        tm[name] = round((now - t_last) * 1000.0, 2)
        t_last = now

    try:
        apply_styles(p)
        src = json.loads(p.src)
        bm = bmesh.new()
        tm["sheet_cached"] = _build_sheet(obj, p, src, bm)
        tick("sheet_ms")
        if not tm["sheet_cached"] and p.mode == 'FACE':
            tm.update({"face_" + k: round(v, 1) for k, v in _FACE_TIMINGS.items()})
        segs, W = None, 0.0
        want_masks = p.dirt > 0.0          # the built-in materials read fm_dirt (absent = clean)
        if p.use_border or want_masks:
            rb = bmesh.new()
            build_reference(rb, p, src)
            if len(rb.faces):
                small = _rb_small(rb)
                W = _border_dims(p, small)[0] if p.use_border else 0.0
                segs = _outline_segments(rb, max(W, 0.01 * (small or 1.0)) * 0.05)
        tick("reference_ms")
        bm.normal_update()
        fade = dist = None
        if segs:
            dkey = (_sheet_key(p), round(W, 9), len(bm.verts))
            cached = _DIST_CACHE.get(obj.name_full)
            if cached is not None and cached[0] == dkey:
                dist = cached[1]
            else:
                dist = _outline_distance([tuple(v.co) for v in bm.verts], segs)
                _DIST_CACHE[obj.name_full] = (dkey, dist)
            if p.noise_strength > 0 and p.use_border:
                ramp = max(W * 0.8, 1e-9)
                fade = [_smoothstep(d / ramp) for d in dist]
        tick("distance_ms")
        apply_noise(bm, p, fade, cache_id=obj.name_full, sheet_key=_sheet_key(p))
        bm.normal_update()
        if want_masks:
            _write_masks(bm, p, dist, W)
        tick("noise_ms")
        try:
            update_wire(obj, p, bm)
        except Exception as wire_exc:
            traceback.print_exc()
            p.last_error = "Wire mesh failed: %s" % wire_exc
        tick("wire_ms")
        # bm.copy() keeps the vertex order, so the distances above still line up
        surface = bm.copy() if (p.use_hair or p.use_dust) else None
        if p.use_thickness and p.thickness > 1e-7 and bm.faces:
            bmesh.ops.solidify(bm, geom=list(bm.faces), thickness=p.thickness)
        bm.normal_update()
        tick("thickness_ms")
        if p.use_boolean and p.boolean_source != 'OBJECT':
            cb, _keep = build_cutter(p, src)
            try:
                _write_child(obj, p, "cutter_obj", "_Cutter", cb, None)
            finally:
                cb.free()
            _style_cutter(p)
        else:
            _remove_child(p, "cutter_obj")
        if p.fuzz_obj:
            _remove_child(p, "fuzz_obj")
        tick("cutter_ms")
        try:
            update_particles(obj, p, surface, dist if p.use_border else None, W)
        except Exception as particle_exc:
            traceback.print_exc()
            p.last_error = "Hair / dust update failed: %s" % particle_exc
        finally:
            _KEEP[0] = None
            if surface is not None:
                surface.free()
        tick("particles_ms")
        if rb is not None and rb.faces and p.use_border:
            fb = build_border(p, rb)
            if fb is not None:
                try:
                    apply_border_shading(fb, p)
                    if want_masks:
                        _set_frame_dirt(fb, p)
                    bob = _write_child(obj, p, "border_obj", "_Border", fb, p.border_material)
                    _apply_uv_space(bob.data, p)
                finally:
                    fb.free()
            else:
                _remove_child(p, "border_obj")
            try:
                _W, _O, _F, _B, _fit = _border_dims(p, _rb_small(rb))
                update_print(obj, p, rb, _W, _O, _F, _B)
            except Exception as print_exc:
                traceback.print_exc()
                p.last_error = "Printed graphics failed: %s" % print_exc
        else:
            _remove_child(p, "border_obj")
            _remove_child(p, "print_obj")
        tick("border_ms")
        apply_shading(bm, p)
        me = obj.data
        me.clear_geometry()
        bm.to_mesh(me)
        _apply_uv_space(me, p)
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
        sync_boolean(obj, p)
        tick("write_ms")
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
        tm["total_ms"] = round((time.perf_counter() - t_start) * 1000.0, 2)
        LAST_TIMINGS.clear()
        LAST_TIMINGS.update(tm)
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
    """Follow edits of linked source curves. Only curves that actually changed in this
    update are re-read (the handler fires for every scene change)."""
    if _curve_sync_busy[0] or _busy[0]:
        return
    try:
        changed_objs, changed_curves = set(), set()
        for u in depsgraph.updates:
            idb = u.id
            if isinstance(idb, bpy.types.Object):
                if idb.type == 'CURVE' and (u.is_updated_geometry or u.is_updated_transform):
                    changed_objs.add(idb.original.name)
            elif isinstance(idb, bpy.types.Curve):
                changed_curves.add(idb.original.name)
        if not changed_objs and not changed_curves:
            return
    except Exception:
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
            if source.name not in changed_objs and source.data.name not in changed_curves:
                continue
            if refresh_curve_source(filter_obj):
                regenerate(filter_obj)
    except Exception:
        traceback.print_exc()
    finally:
        _curve_sync_busy[0] = False


def _upd_scatter(self, context):
    """Hair / dust settings: update the scatter only (no filter rebuild)."""
    if _busy[0] or not self.is_filter or not self.live:
        return
    ob = self.id_data
    if not isinstance(ob, bpy.types.Object):
        return
    em = self.particles_obj
    if em is None or em.name not in bpy.data.objects or not (self.use_hair or self.use_dust):
        regenerate(ob)
        return
    try:
        refresh_scatter(ob, self, em)
    except Exception:
        traceback.print_exc()
        regenerate(ob)
    redraw_all()


def _upd_shared_hair(self, context):
    """A setting that shapes the shared hair strands: rebuild them from this filter."""
    if _busy[0] or not self.is_filter:
        return
    ob = self.id_data
    if isinstance(ob, bpy.types.Object):
        _LIB_SOURCE.add(('hair', ob.name))
    _upd_scatter(self, context)


def _upd_shared_dust(self, context):
    """A setting that shapes the shared dust pieces: rebuild them from this filter."""
    if _busy[0] or not self.is_filter:
        return
    ob = self.id_data
    if isinstance(ob, bpy.types.Object):
        _LIB_SOURCE.add(('dust', ob.name))
    _upd_scatter(self, context)


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

def _len(name, default, desc, mn=0.0, upd=None):
    return FloatProperty(name=name, default=default, min=mn, description=desc,
                         subtype='DISTANCE', unit='LENGTH', precision=4, update=upd or _upd)


class FilterProps(PropertyGroup):
    is_filter: BoolProperty(default=False)
    mode: EnumProperty(items=[('CURVE', "Curve / Edges", ""), ('FACE', "Faces", "")], default='CURVE')
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
                                    subtype='XYZ', update=_upd,
                                    description="Direction the pleated sheet is extruded (X, Y, Z)")
    pleat_angle: FloatProperty(name="Pleat Angle", default=0.0, subtype='ANGLE', unit='ROTATION',
                               description="Rotate pleat direction on faces", update=_upd)
    mesh_type: EnumProperty(name="Topology", items=[
        ('QUADS', "Quads", "Quad topology; split n-gons only where needed"),
        ('TRIS', "Triangles", "Fully triangulated mesh")], default='QUADS', update=_upd)
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
    noise_type: EnumProperty(name="Noise", items=[('SOFT', "Soft", ""), ('HARD', "Hard", "")], default='SOFT', update=_upd)
    noise_basis: EnumProperty(name="Basis", items=[
        ('BLENDER', "Blender Original", ""), ('PERLIN_NEW', "Improved Perlin", ""),
        ('PERLIN_ORIGINAL', "Original Perlin", ""), ('VORONOI_F1', "Voronoi F1", ""),
        ('VORONOI_F2F1', "Voronoi F2-F1", ""), ('VORONOI_CRACKLE', "Voronoi Crackle", ""),
        ('CELLNOISE', "Cell Noise", "")], default='BLENDER', update=_upd)
    noise_direction: EnumProperty(name="Direction", items=[
        ('XYZ', "RGB to XYZ", "Displace in all directions"), ('NORMAL', "Normal", "Displace along normals")],
        default='XYZ', update=_upd)
    noise_seed: IntProperty(name="Seed", default=0, min=0, update=_upd)

    # Border
    use_border: BoolProperty(name="Border", default=False, update=_upd)
    border_width: _len("Width", 0.03, "Frame reach inward from reference edge", 0.0001)
    border_overhang: _len("Overhang", 0.0, "Outward frame overhang")
    border_profile: EnumProperty(name="Profile", items=[
        ('FLAT', "Flat", "Plain rectangular frame"), ('CHAMFER', "Chamfer", "Angled outer edges"),
        ('ROUND', "Round", "Rounded outer edges"), ('LIP', "Lip", "Raised outer rim with a lower inner step")],
        default='FLAT', update=_upd)
    border_bevel: _len("Bevel Size", 0.01, "Chamfer / round radius, or lip step height. Chamfer and "
                       "round are limited so they never uncover the pleat ends (raise Front / Back "
                       "Extend or Overhang for a bigger bevel)")
    border_cuts: IntProperty(name="Round Cuts", default=3, min=1, max=12, update=_upd)
    border_fit: BoolProperty(name="Fit To Pleats", default=True, update=_upd)
    border_smooth: BoolProperty(name="Shade Smooth", default=False, update=_upd,
                                description="Enable smooth shading on the border frame")
    border_auto_smooth: BoolProperty(name="Auto Smooth", default=True, update=_upd,
                                     description="Keep frame edges sharp when their angle exceeds the threshold")
    border_auto_smooth_angle: FloatProperty(name="Angle", default=math.radians(35), min=0, max=math.pi,
                                            subtype='ANGLE', unit='ROTATION', update=_upd)
    border_front: _len("Front Extend", 0.01, "Extra frame thickness on the front")
    border_back: _len("Back Extend", 0.01, "Extra frame thickness on the back")
    border_caps: EnumProperty(name="End Caps", items=[
        ('NONE', "None", "Open ends"),
        ('BOTH', "Both", "Close both ends of a ring-shaped filter (cartridge)"),
        ('BOTTOM', "Bottom", "Close the lower end only"),
        ('TOP', "Top", "Close the upper end only")], default='NONE', update=_upd,
        description="Plates across the ends of ring-shaped filters (cylinders, cones, box wraps)")
    border_cap_hole: FloatProperty(name="Cap Hole", default=0.0, min=0.0, max=0.95, subtype='FACTOR',
                                   update=_upd, description="Round hole in the middle of each end cap")

    # Die-cut face, wire mesh, pull tabs and printed graphics
    grille_pattern: EnumProperty(name="Face", items=GRILLE_PATTERNS, default='NONE', update=_upd,
                                 description="Board face over the pleats with die-cut openings (needs the border)")
    grille_side: EnumProperty(name="Face Side", items=[
        ('FRONT', "Front", "Face on the front only"),
        ('BACK', "Back", "Face on the back only"),
        ('BOTH', "Both", "Face on both sides, like most furnace filters")], default='BOTH', update=_upd)
    grille_cells: IntProperty(name="Openings Across", default=4, min=1, max=60, update=_upd,
                              description="Number of openings across the shorter side")
    grille_aspect: FloatProperty(name="Opening Shape", default=1.0, min=0.2, max=5.0, update=_upd,
                                 description="Height / width of each opening")
    grille_strut: FloatProperty(name="Strut Width", default=0.14, min=0.02, max=0.8, subtype='FACTOR',
                                update=_upd, description="Width of the board strips between openings "
                                "(share of the opening size)")
    grille_chamfer: FloatProperty(name="Corner Cut", default=0.15, min=0.0, max=0.95, subtype='FACTOR',
                                  update=_upd, description="Cut the pointed corners of the diamond openings")
    board_thickness: _len("Board Thickness", 0.0015, "Thickness of the die-cut face and pull tabs", 0.00001)
    use_wire: BoolProperty(name="Wire Mesh", default=False, update=_upd,
                           description="Wire support mesh pleated together with the media")
    wire_pattern: EnumProperty(name="Wire Pattern", items=WIRE_PATTERNS, default='DIAMOND', update=_upd)
    wire_spacing: _len("Wire Spacing", 0.03, "Distance between neighbouring wires, measured along the media",
                       0.0005)
    wire_angle: FloatProperty(name="Wire Angle", default=math.radians(50.0), min=math.radians(5.0),
                              max=math.radians(85.0), subtype='ANGLE', update=_upd,
                              description="Angle of the diagonal wires against the pleat ridges")
    wire_radius: _len("Wire Radius", 0.0006, "Radius of the wire", 0.00001)
    wire_side: EnumProperty(name="Wire Side", items=[
        ('FRONT', "Front", "Wire on the front of the media"),
        ('BACK', "Back", "Wire on the back of the media")], default='FRONT', update=_upd)
    pull_tabs: IntProperty(name="Pull Tabs", default=0, min=0, max=2, update=_upd,
                           description="Board tabs sticking out of the frame for pulling the filter out")
    tab_width: _len("Tab Width", 0.04, "Width of each pull tab", 0.001)
    tab_length: _len("Tab Length", 0.02, "How far each pull tab sticks out of the frame", 0.001)
    tab_position: FloatProperty(name="Tab Position", default=0.5, min=0.05, max=0.95, subtype='FACTOR',
                                update=_upd, description="Where along the side the tab sits")
    use_print: BoolProperty(name="Printed Graphics", default=False, update=_upd,
                            description="Size label, airflow text and arrow printed on the frame sides")
    print_label: StringProperty(name="Size Label", default="", update=_upd,
                                description="Text of the size label (empty: nominal size in inches)")
    print_text: StringProperty(name="Text", default="AIR FLOW", update=_upd,
                               description="Text printed next to the airflow arrow")
    print_place: EnumProperty(name="Print On", items=[
        ('SIDES', "Sides", "Label, text and airflow arrow on the outer sides of the frame"),
        ('FRONT', "Front", "Label and text on the front face of the frame"),
        ('BOTH', "Sides & Front", "Print on the sides and on the front face")], default='SIDES', update=_upd)
    print_front_text: StringProperty(name="Front Text", default="", update=_upd,
                                     description="Text on the front face (empty: the size label and text)")
    print_flow: EnumProperty(name="Airflow", items=[
        ('TO_BACK', "Front to Back", "The arrow points from the front to the back"),
        ('TO_FRONT', "Back to Front", "The arrow points from the back to the front")], default='TO_BACK',
        update=_upd)

    # Hair
    use_hair: BoolProperty(name="Hair", default=False, update=_upd,
                           description="Hair strands: a particle system instancing generated strand curves")
    hair_density: FloatProperty(name="Density", default=2000, min=0, soft_max=100000, update=_upd_scatter)
    hair_length: _len("Length", 0.02, "Strand length", upd=_upd_scatter)
    hair_width: _len("Width", 0.0005, "Strand thickness: the same along the whole strand and for "
                     "every length; both ends taper to a point", upd=_upd_scatter)
    hair_length_var: FloatProperty(name="Length Variation", default=0.5, min=0, max=1, update=_upd_scatter)
    hair_randomness: FloatProperty(name="Tilt Randomness", default=0.6, min=0, soft_max=3, update=_upd,
                                   description="How far strands lean away from the surface normal")
    hair_curl: FloatProperty(name="Curl", default=0.3, min=0, soft_max=2, update=_upd_shared_hair,
                             description="Bend and curl of the shared strand shapes (all filters)")
    hair_lift: FloatProperty(name="Lift", default=math.radians(12.0), min=0.0, max=math.radians(90.0),
                             subtype='ANGLE', unit='ROTATION', update=_upd_scatter,
                             description="Strands lie on the filter; this is the most they lift off it")
    hair_variants: IntProperty(name="Strand Shapes", default=8, min=1, max=32, update=_upd_shared_hair,
                               description="Strand shapes this filter uses. The shared collection holds the most "
                                           "any filter asks for; this filter picks from the first ones")
    hair_seed: IntProperty(name="Seed", default=0, min=0, update=_upd_scatter,
                           description="Placement of the strands on this filter")

    # Dust
    use_dust: BoolProperty(name="Dust", default=False, update=_upd,
                           description="Dust: a particle system instancing generated dust pieces")
    dust_density: FloatProperty(name="Density", default=3000, min=0, soft_max=100000, update=_upd_scatter)
    dust_size: _len("Size", 0.004, "Speck size", upd=_upd_scatter)
    dust_size_var: FloatProperty(name="Size Variation", default=0.6, min=0, max=1, update=_upd_scatter)
    dust_seed: IntProperty(name="Seed", default=0, min=0, update=_upd_scatter,
                           description="Placement of the dust on this filter")
    dust_tilt: FloatProperty(name="Tilt Randomness", default=0.8, min=0, soft_max=3, update=_upd_scatter,
                             description="How much the dust pieces tumble away from the surface normal")
    dust_types: EnumProperty(name="Dust Types", items=DUST_KINDS, options={'ENUM_FLAG'},
                             default={'FLAKE', 'CLUMP', 'GRIT', 'LINT'}, update=_upd_shared_dust,
                             description="Which kinds of dust pieces are in the shared dust collection")
    dust_variants: IntProperty(name="Piece Variants", default=10, min=1, max=32, update=_upd_shared_dust,
                               description="Dust pieces this filter uses. The shared collection holds the most "
                                           "any filter asks for; this filter picks from the first ones")

    # Shared particle emitter
    hair_border_falloff: _len("Border Falloff", 0.05, "Distance over which hair fades in after the "
                              "gap next to the border", upd=_upd_scatter)
    dust_border_falloff: _len("Border Falloff", 0.05, "Distance over which dust fades in after the "
                              "gap next to the border", upd=_upd_scatter)
    particles_avoid_border: BoolProperty(name="Avoid Border", default=True, update=_upd,
                                         description="No hair or dust where the border frame covers the filter")
    hair_viewport: IntProperty(name="Viewport Display", default=100, min=1, max=100,
                               subtype='PERCENTAGE', update=_upd_scatter,
                               description="Share of the hair drawn in the viewport (renders use all)")
    dust_viewport: IntProperty(name="Viewport Display", default=100, min=1, max=100,
                               subtype='PERCENTAGE', update=_upd_scatter,
                               description="Share of the dust drawn in the viewport (renders use all)")
    particle_side: EnumProperty(name="Side", items=[
        ('FRONT', "Front", "Hair and dust on the front of the filter"),
        ('BACK', "Back", "Hair and dust on the back of the filter"),
        ('BOTH', "Both", "Hair and dust on both sides")], default='FRONT', update=_upd_scatter)
    hair_count: IntProperty(default=0)
    dust_count: IntProperty(default=0)

    # Boolean
    use_boolean: BoolProperty(name="Boolean", default=False, update=_upd_bool)
    boolean_source: EnumProperty(name="Cutter", items=[
        ('BOX', "Bounding Box", "A clean box around the reference selection"),
        ('ENVELOPE', "Envelope", "Reference shape thickened to cover the pleats"),
        ('OBJECT', "Object", "Any mesh object")], default='ENVELOPE', update=_upd_bool)
    boolean_obj: PointerProperty(name="Object", type=bpy.types.Object, poll=_poll_mesh, update=_upd_bool)
    boolean_modifier_name: StringProperty(options={'HIDDEN'})
    boolean_op: EnumProperty(name="Operation", items=[
        ('INTERSECT', "Intersect", "Keep only filter inside cutter"),
        ('DIFFERENCE', "Difference", "Cut cutter out of filter"), ('UNION', "Union", "Merge with cutter")],
        default='DIFFERENCE', update=_upd_bool)
    boolean_solver: EnumProperty(name="Solver", items=[('EXACT', "Exact", "More robust"), ('FAST', "Fast", "Quick preview")],
                                 default='EXACT', update=_upd_bool)
    cutter_margin: FloatProperty(name="Trim Margin", default=0.0, soft_min=-1, soft_max=1,
                                 subtype='DISTANCE', unit='LENGTH', precision=4, update=_upd_bool)
    show_cutter: BoolProperty(name="Show Cutter", default=False, update=_upd_cutter_vis)

    # Game-ready export
    game_lod: EnumProperty(name="Low Poly", items=[
        ('PLEATED', "Low-Poly Pleats", "Keeps the zig-zag silhouette with few polygons; bevels, wear, hair and dust go into the maps"),
        ('FLAT', "Flat Card", "A flat panel and frame; all pleat detail goes into the normal map")], default='PLEATED')
    game_resolution: EnumProperty(name="Texture Size", items=[
        ('512', "512", ""), ('1024', "1024", ""), ('2048', "2048", ""), ('4096', "4096", "")], default='1024')
    game_maps: EnumProperty(name="Maps", items=GAME_MAPS, options={'ENUM_FLAG'},
                            default={'NORMAL', 'AO', 'COLOR'})
    game_particles: BoolProperty(name="Include Hair & Dust", default=True,
                                 description="Bake the hair and dust into the maps")
    game_export: EnumProperty(name="Export", items=[
        ('NONE', "Don't Export", "Only create the low-poly object and maps"),
        ('FBX', "FBX", "Export the low-poly object with embedded textures"),
        ('GLB', "glTF (.glb)", "Export the low-poly object as a binary glTF")], default='NONE')
    game_path: StringProperty(name="Export Path", subtype='FILE_PATH', default="",
                              description="File or folder for the export (empty: next to the .blend)")

    # Built-in materials, dirt and UVs
    filter_style: EnumProperty(name="Media", items=FILTER_STYLES, default='CUSTOM', update=_upd,
                               description="Built-in material for the filter media")
    frame_style: EnumProperty(name="Frame", items=FRAME_STYLES, default='CUSTOM', update=_upd,
                              description="Built-in material for the border frame")
    dirt: FloatProperty(name="Dirt", default=0.0, min=0.0, max=1.0, subtype='FACTOR', update=_upd,
                        description="Dirt build-up for the built-in materials: heaviest in the middle "
                                    "and in the folds, clean under the frame")
    uv_mode: EnumProperty(name="UVs", items=[
        ('WORLD', "Real-World Size", "Textures tile at the same size on every filter, fold and frame"),
        ('FIT', "Fit 0-1", "Each part is scaled into the 0-1 UV square")], default='WORLD', update=_upd)
    uv_tile: _len("Texture Size", 0.1, "Real-world size of one texture tile (Real-World Size UVs)", 0.0001)

    # Materials / generated children
    filter_material: PointerProperty(name="Filter Material", type=bpy.types.Material, update=_upd)
    border_material: PointerProperty(name="Border Material", type=bpy.types.Material, update=_upd)
    hair_material: PointerProperty(name="Hair Material", type=bpy.types.Material, update=_upd_scatter,
                                   description="Material of this filter's hair")
    dust_material: PointerProperty(name="Dust Material", type=bpy.types.Material, update=_upd_shared_dust,
                                   description="Material of the shared dust pieces (all filters)")
    particles_obj: PointerProperty(type=bpy.types.Object)
    hair_lib: PointerProperty(type=bpy.types.Collection)
    dust_lib: PointerProperty(type=bpy.types.Collection)
    hair_obj: PointerProperty(type=bpy.types.Object)       # legacy (0.3.x emitters)
    dust_obj: PointerProperty(type=bpy.types.Object)       # legacy (0.3.x emitters)
    border_obj: PointerProperty(type=bpy.types.Object)
    cutter_obj: PointerProperty(type=bpy.types.Object)
    fuzz_obj: PointerProperty(type=bpy.types.Object)
    wire_obj: PointerProperty(type=bpy.types.Object)
    print_obj: PointerProperty(type=bpy.types.Object)


# ---------------------------------------------------------------------------
# Presets (normalized to source dimensions)
# ---------------------------------------------------------------------------

PRESET_SUBDIR = "filter_maker_pro"
LEN_PROPS = {"thickness", "pleat_depth", "height", "noise_strength", "noise_scale", "border_width",
             "border_overhang", "border_bevel", "border_front", "border_back", "hair_length",
             "hair_width", "dust_size", "cutter_margin", "hair_border_falloff", "dust_border_falloff",
             "board_thickness", "wire_spacing", "wire_radius", "tab_width", "tab_length"}
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
    "border_smooth": False, "border_auto_smooth": True,
    "border_auto_smooth_angle": math.radians(35),
    "border_front": 0.01, "border_back": 0.01,
    "use_hair": False, "hair_density": 2000.0, "hair_length": 0.02, "hair_width": 0.0005,
    "hair_length_var": 0.5, "hair_randomness": 0.6, "hair_curl": 0.3, "hair_seed": 0,
    "use_dust": False, "dust_density": 3000.0, "dust_size": 0.004, "dust_size_var": 0.6, "dust_seed": 0,
    "hair_variants": 8, "dust_variants": 10, "dust_tilt": 0.8, "hair_lift": math.radians(12.0),
    "dust_types": ["CLUMP", "FLAKE", "GRIT", "LINT"], "particles_avoid_border": True,
    "hair_viewport": 100, "dust_viewport": 100, "particle_side": "FRONT",
    "hair_border_falloff": 0.05, "dust_border_falloff": 0.05,
    "use_boolean": False, "boolean_source": "ENVELOPE", "boolean_op": "DIFFERENCE", "boolean_solver": "EXACT",
    "cutter_margin": 0.0,
    "filter_style": "PAPER", "frame_style": "PAINTED", "dirt": 0.0,
    "grille_pattern": "NONE", "grille_side": "BOTH", "grille_cells": 4, "grille_aspect": 1.0,
    "grille_strut": 0.14, "grille_chamfer": 0.15, "board_thickness": 0.0015,
    "use_wire": False, "wire_pattern": "DIAMOND", "wire_spacing": 0.03, "wire_angle": math.radians(50.0),
    "wire_radius": 0.0006, "wire_side": "FRONT",
    "pull_tabs": 0, "tab_width": 0.04, "tab_length": 0.02, "tab_position": 0.5,
    "use_print": False, "print_flow": "TO_BACK", "print_place": "SIDES",
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
    "HEPA Fine Pleat": _mk(filter_style="HEPA", frame_style="ALUMINIUM",
                           pleat_density=80.0, pleat_depth=0.018, use_thickness=False, thickness=0.0,
                           bevel_ridge=0.25, bevel_valley=0.25, bevel_cuts=3,
                           noise_strength=0.0008, noise_scale=0.3, noise_depth=1, use_hair=False,
                           use_dust=False),
    "Coarse Furnace": _mk(filter_style="FIBERGLASS", frame_style="CARDBOARD",
                          pleat_density=38.0, pleat_depth=0.035, use_thickness=False, thickness=0.0,
                          bevel_ridge=0.55, bevel_valley=0.45, bevel_cuts=4,
                          noise_strength=0.0025, noise_scale=0.35, noise_depth=1,
                          use_hair=False, use_dust=False),
    "Sharp Paper Fold": _mk(pleat_density=60.0, bevel_ridge=0.0, bevel_valley=0.0, bevel_cuts=1,
                            use_thickness=False, thickness=0.0, noise_strength=0.0005,
                            use_hair=False, use_dust=False),
    "Die-Cut Furnace": _mk(filter_style="HEPA", frame_style="WHITE_BOARD",
                           pleat_density=28.0, pleat_depth=0.012, use_thickness=False, thickness=0.0,
                           bevel_ridge=0.45, bevel_valley=0.35, bevel_cuts=3,
                           noise_strength=0.0006, noise_scale=0.3, noise_depth=1,
                           use_border=True, border_width=0.035, border_profile="FLAT", border_fit=True,
                           border_front=0.002, border_back=0.002,
                           grille_pattern="DIAMOND", grille_side="BOTH", grille_cells=3, grille_aspect=1.0,
                           grille_strut=0.14, grille_chamfer=0.15, board_thickness=0.0025,
                           use_wire=True, wire_pattern="DIAMOND", wire_spacing=0.05,
                           wire_angle=math.radians(50.0), wire_radius=0.0009, wire_side="FRONT",
                           pull_tabs=1, tab_width=0.07, tab_length=0.03, use_print=True,
                           use_hair=False, use_dust=False),
    "Dusty Used": _mk(frame_style="GALVANIZED", dirt=0.6,
                      pleat_density=55.0, pleat_depth=0.025, noise_strength=0.003,
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
            elif k == "dust_types":
                v = set(v)
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
        elif isinstance(v, set):
            v = sorted(v)
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
    # a preset is an explicit choice: its strand / dust-piece look becomes the shared one
    _LIB_SOURCE.add(('hair', ob.name))
    _LIB_SOURCE.add(('dust', ob.name))
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
        if o is not None:
            o.select_set(False)
    ob.select_set(True)
    context.view_layer.objects.active = ob
    redraw_all()
    return ob


class FILTER_OT_new_line(Operator):
    """Create an editable curve at the 3D cursor and build a live filter from it"""
    bl_idname = "filter.new_line"
    bl_label = "New Filter Line"
    bl_options = {'REGISTER', 'UNDO'}
    length: FloatProperty(name="Length", default=1.0, min=0.01, subtype='DISTANCE')

    def execute(self, context):
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        h = self.length / 2.0
        cu = bpy.data.curves.new("Filter_Line", 'CURVE')
        cu.dimensions = '3D'
        sp = cu.splines.new('BEZIER')
        sp.bezier_points.add(1)
        for bp, x in zip(sp.bezier_points, (-h, h)):
            bp.co = (x, 0.0, 0.0)
            bp.handle_left_type = bp.handle_right_type = 'FREE'
            bp.handle_left = (x - self.length / 6.0, 0.0, 0.0)
            bp.handle_right = (x + self.length / 6.0, 0.0, 0.0)
            bp.handle_left_type = bp.handle_right_type = 'AUTO'
        line = bpy.data.objects.new("Filter_Line", cu)
        line.location = context.scene.cursor.location
        (context.collection or context.scene.collection).objects.link(line)
        context.view_layer.update()
        src = paths_from_curve(line)
        if not src["paths"]:
            self.report({'ERROR'}, "Could not create the line curve.")
            return {'CANCELLED'}
        ob = create_filter(context, 'CURVE', src, source=line)
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


FILTER_SHAPES = (
    ('PANEL', "Panel", "Rectangular panel filter in a frame"),
    ('ROUND', "Round Panel", "Circular panel filter with a round frame"),
    ('CARTRIDGE', "Cartridge", "Cylindrical cartridge with end caps"),
    ('VBANK', "V-Bank", "Media folded into V shapes inside one frame"),
    ('BOX', "Box Wrap", "Pleats running around four sides of a box"),
    ('FURNACE', "Die-Cut Furnace", "16 x 25 x 1 in furnace filter: white board face with diamond "
                                    "openings, wire mesh, pull tab and printed airflow label"),
)


def _shape_mesh(shape, o):
    """Source mesh (verts, faces) for a filter shape, sized by operator settings `o`."""
    if shape in {'PANEL', 'FURNACE'}:
        w, h = o.width / 2.0, o.height
        return [(-w, 0, 0), (w, 0, 0), (w, 0, h), (-w, 0, h)], [(0, 1, 2, 3)]
    if shape == 'ROUND':
        n, r = max(12, o.segments), o.radius
        verts = [(r * math.cos(2 * math.pi * i / n), 0.0, r + r * math.sin(2 * math.pi * i / n)) for i in range(n)]
        return verts, [tuple(range(n))]
    if shape == 'CARTRIDGE':
        n, r, h = max(12, o.segments), o.radius, o.height
        verts = [(r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n), z)
                 for z in (0.0, h) for i in range(n)]
        faces = [(i, (i + 1) % n, n + (i + 1) % n, n + i) for i in range(n)]
        return verts, faces
    if shape == 'VBANK':
        k, w, h, d = max(1, o.folds), o.width / 2.0, o.height, o.vdepth
        xs = [-w + o.width * i / (2 * k) for i in range(2 * k + 1)]
        bottom = [(x, (d if i % 2 else 0.0), 0.0) for i, x in enumerate(xs)]
        top = [(x, y, h) for x, y, _z in bottom]
        m = len(bottom)
        faces = [(i, i + 1, m + i + 1, m + i) for i in range(m - 1)]
        return bottom + top, faces
    if shape == 'BOX':
        w, h = o.width / 2.0, o.height
        verts = [(-w, -w, 0), (w, -w, 0), (w, w, 0), (-w, w, 0),
                 (-w, -w, h), (w, -w, h), (w, w, h), (-w, w, h)]
        faces = [(0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
        return verts, faces
    return [], []


def _furnace_settings(p, short):
    """Real-world sizes of a 1 in deep die-cut pleated furnace filter (short = shorter side)."""
    p.filter_style, p.frame_style = 'HEPA', 'WHITE_BOARD'
    p.pleat_density, p.pleat_depth = 45.0, 0.0095
    p.use_thickness = False
    p.bevel_ridge, p.bevel_valley, p.bevel_cuts = 0.45, 0.35, 3
    p.noise_strength, p.noise_scale = 0.0004, 0.12
    p.border_profile, p.border_fit = 'FLAT', True
    p.border_width = 0.022
    p.border_front = p.border_back = 0.001
    p.grille_pattern, p.grille_side = 'DIAMOND', 'BOTH'
    p.grille_cells, p.grille_aspect = 3, 1.0
    p.grille_strut, p.grille_chamfer = 0.14, 0.15
    p.board_thickness = 0.0012
    p.use_wire, p.wire_pattern, p.wire_side = True, 'DIAMOND', 'FRONT'
    p.wire_spacing, p.wire_radius, p.wire_angle = 0.03, 0.00045, math.radians(50.0)
    p.pull_tabs, p.tab_width, p.tab_length = 1, 0.05, 0.018
    p.use_print = True


class FILTER_OT_add_shape(Operator):
    """Add a ready-made filter shape at the 3D cursor"""
    bl_idname = "filter.add_shape"
    bl_label = "Add Filter Shape"
    bl_options = {'REGISTER', 'UNDO'}

    shape: EnumProperty(name="Shape", items=FILTER_SHAPES, default='PANEL')
    width: FloatProperty(name="Width", default=0.6, min=0.01, subtype='DISTANCE', unit='LENGTH')
    height: FloatProperty(name="Height", default=0.4, min=0.01, subtype='DISTANCE', unit='LENGTH')
    radius: FloatProperty(name="Radius", default=0.15, min=0.01, subtype='DISTANCE', unit='LENGTH')
    segments: IntProperty(name="Segments", default=64, min=12, max=256)
    folds: IntProperty(name="V Folds", default=3, min=1, max=16)
    vdepth: FloatProperty(name="V Depth", default=0.12, min=0.005, subtype='DISTANCE', unit='LENGTH')

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "shape")
        if self.shape in {'PANEL', 'VBANK', 'BOX', 'FURNACE'}:
            layout.prop(self, "width")
        if self.shape in {'ROUND', 'CARTRIDGE'}:
            layout.prop(self, "radius")
            layout.prop(self, "segments")
        if self.shape != 'ROUND':
            layout.prop(self, "height")
        if self.shape == 'VBANK':
            layout.prop(self, "folds")
            layout.prop(self, "vdepth")

    def invoke(self, context, event):
        if self.shape == 'CARTRIDGE':
            self.height = 0.3
        elif self.shape == 'FURNACE':
            self.width, self.height = 0.3937, 0.6223            # 15.5 x 24.5 in actual size
        return self.execute(context)

    def execute(self, context):
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        verts, faces = _shape_mesh(self.shape, self)
        if not faces:
            return {'CANCELLED'}
        label = dict((k, n) for k, n, _d in FILTER_SHAPES)[self.shape]
        me = bpy.data.meshes.new("FM Source " + label)
        me.from_pydata(verts, [], faces)
        me.update()
        src_ob = bpy.data.objects.new("FM Source " + label, me)
        src_ob.location = context.scene.cursor.location
        (context.collection or context.scene.collection).objects.link(src_ob)
        context.view_layer.update()
        src = faces_from_mesh(src_ob, selected_only=False)
        ob = create_filter(context, 'FACE', src, source=src_ob)
        src_ob.hide_render = True
        try:
            src_ob.hide_set(True)
        except Exception:
            pass
        p = ob.filter_maker
        p.live = False
        p.use_border = True
        if self.shape == 'CARTRIDGE':
            p.border_caps = 'BOTH'
            p.border_cap_hole = 0.55
            p.border_width = min(p.border_width, self.height * 0.08)
        elif self.shape == 'VBANK':
            p.face_smooth = False
        elif self.shape == 'BOX':
            p.border_caps = 'NONE'
        elif self.shape == 'FURNACE':
            _furnace_settings(p, min(self.width, self.height))
        p.live = True
        regenerate(ob)
        ob.name = "Filter " + label
        if p.last_error:
            self.report({'WARNING'}, p.last_error)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Game-ready workflow: low-poly copy, packed UVs, baked maps, optional export
# ---------------------------------------------------------------------------


class _Override:
    """Read-only view of filter settings with a few values replaced (for the low-poly build)."""

    def __init__(self, base, **values):
        self.__dict__["_base"] = base
        self.__dict__["_values"] = values

    def __getattr__(self, name):
        values = self.__dict__["_values"]
        if name in values:
            return values[name]
        return getattr(self.__dict__["_base"], name)


def _bm_to_object(bm, name, coll, mats=()):
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    for m in mats:
        me.materials.append(m)
    ob = bpy.data.objects.new(name, me)
    coll.objects.link(ob)
    return ob


def _game_collection(name):
    coll = bpy.data.collections.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(coll)
    return coll


def _realized_copy(src_ob, name, coll, dg):
    """Mesh copy of an object's evaluated geometry, with Geometry Nodes instances
    (dust pieces) realized."""
    tmp_group = None
    mod = None
    try:
        if src_ob.type == 'MESH' and any(m.type == 'NODES' for m in src_ob.modifiers):
            tmp_group = bpy.data.node_groups.new(".FM realize", 'GeometryNodeTree')
            tmp_group.interface.new_socket(name="Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
            tmp_group.interface.new_socket(name="Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
            gi = tmp_group.nodes.new("NodeGroupInput")
            go = tmp_group.nodes.new("NodeGroupOutput")
            rz = tmp_group.nodes.new("GeometryNodeRealizeInstances")
            tmp_group.links.new(gi.outputs[0], rz.inputs[0])
            tmp_group.links.new(rz.outputs[0], go.inputs[0])
            mod = src_ob.modifiers.new(".FM realize", 'NODES')
            mod.node_group = tmp_group
            dg.update()
        me = bpy.data.meshes.new_from_object(src_ob.evaluated_get(dg), preserve_all_data_layers=True,
                                             depsgraph=dg)
    finally:
        if mod is not None:
            src_ob.modifiers.remove(mod)
        if tmp_group is not None:
            bpy.data.node_groups.remove(tmp_group)
        dg.update()
    me.name = name
    ob = bpy.data.objects.new(name, me)
    ob.matrix_world = src_ob.matrix_world.copy()
    coll.objects.link(ob)
    return ob


def _split_sheet_uvs(bm, unrolled_period):
    """The unrolled pleat sheet is one long strip; cut it into near-square chunks
    (on ridge lines) and stack them, so packing into 0-1 wastes little space."""
    uvl = bm.loops.layers.uv.get(UV_LAYER)
    if uvl is None or not bm.faces:
        return
    us = [lp[uvl].uv.x for f in bm.faces for lp in f.loops]
    vs = [lp[uvl].uv.y for f in bm.faces for lp in f.loops]
    u0, u1, v0, v1 = min(us), max(us), min(vs), max(vs)
    ulen, vlen = u1 - u0, max(v1 - v0, 1e-9)
    n = max(1, int(round(math.sqrt(ulen / vlen))))
    if n <= 1:
        return
    chunk = ulen / n
    if unrolled_period > 1e-9:
        chunk = max(unrolled_period, round(chunk / unrolled_period) * unrolled_period)
    gap = vlen * 0.04
    for f in bm.faces:
        cu = sum(lp[uvl].uv.x for lp in f.loops) / len(f.loops)
        k = int((cu - u0) // chunk)
        if k <= 0:
            continue
        for lp in f.loops:
            uv = lp[uvl].uv
            lp[uvl].uv = (uv.x - k * chunk, uv.y + k * (vlen + gap))


def _cycles_gpu_ready():
    """True when the user has a Cycles GPU device switched on in the preferences."""
    try:
        cp = bpy.context.preferences.addons["cycles"].preferences
        if getattr(cp, "compute_device_type", 'NONE') in {'NONE', ''}:
            return False
        try:
            cp.refresh_devices()
        except Exception:
            pass
        return any(d.use and d.type != 'CPU' for d in cp.devices)
    except Exception:
        return False


def _image_node(nodes, x, y, img, non_color):
    n = nodes.new("ShaderNodeTexImage")
    n.location = (x, y)
    n.image = img
    if non_color:
        try:
            img.colorspace_settings.name = 'Non-Color'
        except Exception:
            pass
    return n


class FILTER_OT_game_ready(Operator):
    """Make a game-ready copy: low-poly mesh, packed UVs and maps baked from the full filter"""
    bl_idname = "filter.game_ready"
    bl_label = "Make Game-Ready"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        ob = active_filter(context)
        if ob is None:
            return {'CANCELLED'}
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        p = ob.filter_maker
        scene = context.scene
        state = dict(engine=scene.render.engine, active=context.view_layer.objects.active,
                     selected=[o for o in context.selected_objects])
        coll = _game_collection("%s Low" % ob.name)
        made_high = []
        try:
            low = self._build_low(ob, p, coll)
            maps = set(p.game_maps)
            images = {}
            if maps:
                dg = context.evaluated_depsgraph_get()
                high_coll = _game_collection(".FM bake high")
                srcs = [ob] + [o for o in (p.border_obj, p.wire_obj, p.print_obj) if o is not None]
                if p.game_particles and p.particles_obj is not None:
                    srcs.append(p.particles_obj)
                for s in srcs:
                    made_high.append(_realized_copy(s, s.name + "_high", high_coll, dg))
                images = self._bake(context, ob, p, low, made_high, maps)
            self._finish_material(ob, low, images)
            path = self._export(context, ob, p, low)
            tris = sum(len(pl.vertices) - 2 for pl in low.data.polygons)
            msg = "Game-ready: %s (%s tris)" % (low.name, format(tris, ","))
            if images:
                msg += ", baked %s" % ", ".join(sorted(images))
            if path:
                msg += ", exported %s" % bpy.path.basename(path)
            self.report({'INFO'}, msg)
        except Exception as exc:
            traceback.print_exc()
            self.report({'ERROR'}, "Game-ready failed: %s" % exc)
            return {'CANCELLED'}
        finally:
            for h in made_high:
                me = h.data
                bpy.data.objects.remove(h, do_unlink=True)
                if me is not None and me.users == 0:
                    bpy.data.meshes.remove(me)
            hc = bpy.data.collections.get(".FM bake high")
            if hc is not None and not hc.objects:
                bpy.data.collections.remove(hc)
            try:
                scene.render.engine = state["engine"]
            except Exception:
                pass
            for o in context.view_layer.objects:
                if o is not None:
                    o.select_set(o in state["selected"])
            if state["active"] is not None and state["active"].name in bpy.data.objects:
                context.view_layer.objects.active = state["active"]
        return {'FINISHED'}

    # -- low poly ---------------------------------------------------------------
    def _build_low(self, ob, p, coll):
        src = json.loads(p.src)
        # keep the pleat bevel (2 cuts at most): sharp low-poly ridges under rounded high-poly
        # ridges leave the bake rays nothing to hit there (white streaks along every ridge)
        lo = _Override(p, bevel_cuts=min(p.bevel_cuts, 2), face_smooth_cuts=1,
                       detail=max(0.5, min(p.detail, 4.0)), mesh_type='QUADS',
                       border_profile='FLAT', border_cuts=1)
        bm = bmesh.new()
        try:
            if p.game_lod == 'PLEATED':
                (build_curve if p.mode == 'CURVE' else build_face)(bm, lo, src, None)
            else:
                build_reference(bm, lo, src)
            for f in bm.faces:
                f.material_index = 0
            per = 0.0
            if p.game_lod == 'PLEATED':
                arc = _arc_table(_profile_table(0.0, 0.0, 1), 1.0 / max(p.pleat_density, 1e-3), p.pleat_depth)
                per = arc[2]
                _split_sheet_uvs(bm, per)
            if p.use_border:
                rb = bmesh.new()
                try:
                    build_reference(rb, lo, src)
                    fb = build_border(lo, rb) if len(rb.faces) else None
                finally:
                    rb.free()
                if fb is not None:
                    tmp = bpy.data.meshes.new(".FM game border")
                    try:
                        fb.to_mesh(tmp)
                        n0 = len(bm.faces)
                        bm.from_mesh(tmp)
                        bm.faces.ensure_lookup_table()
                        for f in bm.faces[n0:]:
                            f.material_index = 0
                    finally:
                        fb.free()
                        bpy.data.meshes.remove(tmp)
            for f in bm.faces:
                f.smooth = True          # smooth bake normals: no gaps between the rays at the folds
            name = "%s_low" % ob.name
            old = bpy.data.objects.get(name)
            if old is not None:
                me_old = old.data
                bpy.data.objects.remove(old, do_unlink=True)
                if me_old is not None and me_old.users == 0:
                    bpy.data.meshes.remove(me_old)
            low = _bm_to_object(bm, name, coll)
        finally:
            bm.free()
        low.matrix_world = ob.matrix_world.copy()
        self._pack_uvs(low, p.game_lod == 'FLAT')
        return low

    def _pack_uvs(self, low, project):
        me = low.data
        if UV_LAYER not in me.uv_layers:
            me.uv_layers.new(name=UV_LAYER)
        ctx = bpy.context
        for o in ctx.view_layer.objects:
            if o is not None:
                o.select_set(False)
        low.select_set(True)
        ctx.view_layer.objects.active = low
        bpy.ops.object.mode_set(mode='EDIT')
        try:
            bpy.ops.mesh.select_all(action='SELECT')
            if project:
                bpy.ops.uv.smart_project(angle_limit=math.radians(66.0), island_margin=0.01)
            try:
                bpy.ops.uv.select_all(action='SELECT')
            except Exception:
                pass
            bpy.ops.uv.pack_islands(rotate=True, margin=0.008)
        finally:
            bpy.ops.object.mode_set(mode='OBJECT')

    # -- baking -----------------------------------------------------------------
    def _bake(self, context, ob, p, low, highs, maps):
        scene = context.scene
        scene.render.engine = 'CYCLES'
        try:
            scene.cycles.device = 'GPU' if _cycles_gpu_ready() else 'CPU'
        except Exception:
            pass
        res = int(p.game_resolution)
        mat = bpy.data.materials.get("%s_game" % ob.name) or bpy.data.materials.new("%s_game" % ob.name)
        nodes, links, bsdf, _out = _mat_nodes(mat)
        low.data.materials.clear()
        low.data.materials.append(mat)
        images, img_nodes = {}, {}
        y = 300
        for key in ('COLOR', 'ROUGHNESS', 'AO', 'NORMAL'):
            if key not in maps:
                continue
            iname = "%s_%s" % (ob.name, key.lower())
            img = bpy.data.images.get(iname)
            if img is not None and (img.size[0] != res or img.size[1] != res):
                bpy.data.images.remove(img)
                img = None
            if img is None:
                img = bpy.data.images.new(iname, res, res, alpha=False)
            images[key] = img
            img_nodes[key] = _image_node(nodes, -700, y, img, key != 'COLOR')
            y -= 300
        F, B = _border_dims(p)[2:4] if p.use_border else (p.pleat_depth, p.pleat_depth)
        hair = p.hair_length * (1.0 + p.hair_length_var) if (p.use_hair and p.game_particles) else 0.0
        if p.game_lod == 'FLAT':
            ext = max(F, B, p.pleat_depth * 1.2) + hair
        else:
            ext = 0.5 * p.pleat_depth + 3.0 * p.noise_strength + (p.thickness if p.use_thickness else 0.0) + hair
        ext = max(ext, 1e-3)
        for o in context.view_layer.objects:
            if o is not None:
                o.select_set(False)
        for h in highs:
            h.select_set(True)
        low.select_set(True)
        context.view_layer.objects.active = low
        bake_args = dict(use_selected_to_active=True, cage_extrusion=ext, max_ray_distance=ext * 2.5,
                         margin=16, use_clear=True)
        for key, img_node in img_nodes.items():
            # the bake target must be the selected *and* active image node, and the
            # material must be re-evaluated so the bake sees those flags
            for n in nodes:
                n.select = False
            img_node.select = True
            nodes.active = img_node
            img_node.select = True
            mat.node_tree.update_tag()
            mat.update_tag()
            low.data.update_tag()
            context.view_layer.update()
            try:
                # normal / colour / roughness are noise-free: a few samples only anti-alias
                scene.cycles.samples = {'AO': 24}.get(key, 4)
                scene.cycles.use_denoising = False
                scene.cycles.use_adaptive_sampling = False
            except Exception:
                pass
            if key == 'NORMAL':
                res = bpy.ops.object.bake(type='NORMAL', normal_space='TANGENT', **bake_args)
            elif key == 'AO':
                res = bpy.ops.object.bake(type='AO', **bake_args)
            elif key == 'COLOR':
                res = bpy.ops.object.bake(type='DIFFUSE', pass_filter={'COLOR'}, **bake_args)
            else:
                res = bpy.ops.object.bake(type='ROUGHNESS', **bake_args)
            if 'FINISHED' not in res:
                state = ", ".join("%s(%s%s%s)" % (n.name, n.bl_idname, " sel" if n.select else "",
                                                  " active" if nodes.active == n else "") for n in nodes)
                raise RuntimeError("%s bake did not run (%s). Low-poly slots: %s. Nodes: %s" % (
                    key, ", ".join(res), [m.name if m else None for m in low.data.materials], state))
        self._save_images(images)
        return images

    def _save_images(self, images):
        blend = bpy.data.filepath
        folder = bpy.path.abspath("//textures") if blend else None
        for key, img in images.items():
            try:
                if folder:
                    os.makedirs(folder, exist_ok=True)
                    img.filepath_raw = os.path.join(folder, img.name + ".png")
                    img.file_format = 'PNG'
                    img.save()
                else:
                    img.pack()
            except Exception:
                traceback.print_exc()

    def _finish_material(self, ob, low, images):
        mat = bpy.data.materials.get("%s_game" % ob.name) or bpy.data.materials.new("%s_game" % ob.name)
        nodes, links, bsdf, _out = _mat_nodes(mat)
        y = 300
        if 'COLOR' in images:
            col = _image_node(nodes, -700, y, images['COLOR'], False)
            c_out = col.outputs[0]
            if 'AO' in images:
                ao = _image_node(nodes, -700, y - 300, images['AO'], True)
                c_out = _mix_rgb(nodes, links, -350, y, 1.0, col.outputs[0], ao.outputs[0])
                mixn = c_out.node
                try:
                    mixn.blend_type = 'MULTIPLY'
                except Exception:
                    pass
            links.new(c_out, _sock(bsdf.inputs, "Base Color"))
        elif 'AO' in images:
            ao = _image_node(nodes, -700, y - 300, images['AO'], True)
            links.new(ao.outputs[0], _sock(bsdf.inputs, "Base Color"))
        if 'ROUGHNESS' in images:
            r = _image_node(nodes, -700, -300, images['ROUGHNESS'], True)
            links.new(r.outputs[0], _sock(bsdf.inputs, "Roughness"))
        if 'NORMAL' in images:
            nimg = _image_node(nodes, -700, -600, images['NORMAL'], True)
            nm = nodes.new("ShaderNodeNormalMap")
            nm.location = (-300, -600)
            try:
                nm.uv_map = UV_LAYER
            except Exception:
                pass
            links.new(nimg.outputs[0], _sock(nm.inputs, "Color"))
            links.new(_sock(nm.outputs, "Normal"), _sock(bsdf.inputs, "Normal"))
        low.data.materials.clear()
        low.data.materials.append(mat)

    def _export(self, context, ob, p, low):
        if p.game_export == 'NONE':
            return None
        ext = ".fbx" if p.game_export == 'FBX' else ".glb"
        path = bpy.path.abspath(p.game_path) if p.game_path else ""
        if not path:
            base = bpy.path.abspath("//") if bpy.data.filepath else bpy.app.tempdir
            path = os.path.join(base, ob.name + "_game" + ext)
        elif os.path.isdir(path) or path.endswith(("/", "\\")):
            path = os.path.join(path, ob.name + "_game" + ext)
        elif not path.lower().endswith(ext):
            path += ext
        for o in context.view_layer.objects:
            if o is not None:
                o.select_set(False)
        low.select_set(True)
        context.view_layer.objects.active = low
        if p.game_export == 'FBX':
            bpy.ops.export_scene.fbx(filepath=path, use_selection=True, object_types={'MESH'},
                                     path_mode='COPY', embed_textures=True, mesh_smooth_type='FACE')
        else:
            bpy.ops.export_scene.gltf(filepath=path, use_selection=True, export_format='GLB')
        return path


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
                if item is not None:
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
            _remove_lib(p, "hair_lib")
            _remove_lib(p, "dust_lib")
            _remove_scatter_group_if_unused()
            cache = bpy.data.meshes.get(ob.get("fm_sheet_cache", "") or "")
            me = ob.data
            bpy.data.objects.remove(ob, do_unlink=True)
            if me and me.users == 0:
                bpy.data.meshes.remove(me)
            if cache is not None and cache.users == 0:
                bpy.data.meshes.remove(cache)
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
    for attr in ("particles_obj", "border_obj"):
        child = getattr(p, attr)
        if child is not None and child.type == 'MESH':
            ev += len(child.data.vertices)
            ef += len(child.data.polygons)
    return v, f, ev, ef


class FILTER_PT_main(Panel):
    bl_label = "Filter Maker Pro  0.7.0"
    bl_idname = "FILTER_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Filter Maker'

    def draw_header(self, context):
        _header_icon(self.layout, 'main', 'MOD_WIREFRAME')

    @safe_draw
    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.operator("filter.new_line", icon=ic('CURVE_PATH'))
        col.operator_menu_enum("filter.add_shape", "shape", text="Add Filter Shape", icon=ic('MESH_GRID'))
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
            box.label(text="+ border / emitter: %s verts" % format(ev, ","), icon=ic('MOD_SOLIDIFY'))
        if p.use_hair or p.use_dust:
            box.label(text="Instances: hair %s   dust %s" % (format(p.hair_count, ","), format(p.dust_count, ",")),
                      icon=ic('PARTICLES'))
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
        _header_icon(self.layout, getattr(self, "header_art", ""), getattr(self, "header_icon", "INFO"))

    def P(self, context):
        self.layout.use_property_split = True
        self.layout.use_property_decorate = False
        ob = active_filter(context)
        return ob.filter_maker if ob is not None else None


class FILTER_PT_presets(FILTER_SubPanel, Panel):
    header_art = "presets"
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
    header_art = "pleats"
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
    header_art = "bevel"
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
    header_art = "shading"
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
    header_art = "wear"
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
    header_art = "border"
    header_icon = "MOD_SOLIDIFY"
    bl_label = "Border"
    bl_idname = "FILTER_PT_border"
    bl_options = {'DEFAULT_CLOSED'}

    @safe_draw
    def draw_header(self, context):
        _header_icon(self.layout, 'border', 'MOD_SOLIDIFY')
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
        layout.label(text="white = pleats   orange = border   grey = bounds")
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
        c.label(text="End caps (rings, cylinders)")
        c.prop(p, "border_caps", text="")
        sub = c.row(align=True)
        sub.active = p.border_caps != 'NONE'
        sub.prop(p, "border_cap_hole", slider=True)
        c.separator()
        c.label(text="Shading")
        c.prop(p, "border_smooth")
        c.prop(p, "border_auto_smooth")
        row = c.row(align=True)
        row.active = p.border_auto_smooth
        row.prop(p, "border_auto_smooth_angle")


class FILTER_PT_diecut(FILTER_SubPanel, Panel):
    header_icon = "MESH_GRID"
    bl_label = "Face, Wire & Print"
    bl_idname = "FILTER_PT_diecut"
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon=ic('MESH_GRID'))

    @safe_draw
    def draw(self, context):
        p, layout = self.P(context), self.layout
        if p is None:
            return
        if not p.use_border:
            layout.label(text="The face, tabs and print need the Border", icon=ic('INFO'))
        c = layout.column(align=True)
        c.active = p.use_border
        c.label(text="Die-cut face")
        c.prop(p, "grille_pattern", text="Face")
        sub = c.column(align=True)
        sub.active = p.use_border and p.grille_pattern != 'NONE'
        sub.prop(p, "grille_side", text="Side")
        sub.prop(p, "grille_cells")
        sub.prop(p, "grille_aspect")
        sub.prop(p, "grille_strut", slider=True)
        if p.grille_pattern == 'DIAMOND':
            sub.prop(p, "grille_chamfer", slider=True)
        c.prop(p, "board_thickness")
        layout.separator()
        c = layout.column(align=True)
        c.prop(p, "use_wire")
        sub = c.column(align=True)
        sub.active = p.use_wire
        sub.prop(p, "wire_pattern", text="Pattern")
        sub.prop(p, "wire_spacing", text="Spacing")
        if p.wire_pattern == 'DIAMOND':
            sub.prop(p, "wire_angle", text="Angle")
        sub.prop(p, "wire_radius", text="Radius")
        sub.prop(p, "wire_side", text="Side")
        layout.separator()
        c = layout.column(align=True)
        c.active = p.use_border
        c.prop(p, "pull_tabs")
        sub = c.column(align=True)
        sub.active = p.use_border and p.pull_tabs > 0
        sub.prop(p, "tab_width")
        sub.prop(p, "tab_length")
        sub.prop(p, "tab_position", slider=True)
        layout.separator()
        c = layout.column(align=True)
        c.active = p.use_border
        c.prop(p, "use_print")
        sub = c.column(align=True)
        sub.active = p.use_border and p.use_print
        try:
            sub.prop(p, "print_label", placeholder="Auto (nominal inches)")
        except TypeError:
            sub.prop(p, "print_label")
        sub.prop(p, "print_place")
        sub.prop(p, "print_text")
        if p.print_place != 'SIDES':
            try:
                sub.prop(p, "print_front_text", placeholder="Size label + text")
            except TypeError:
                sub.prop(p, "print_front_text")
        sub.prop(p, "print_flow")


class FILTER_PT_hair(FILTER_SubPanel, Panel):
    header_art = "hair"
    header_icon = "STRANDS"
    bl_label = "Hair"
    bl_idname = "FILTER_PT_hair"
    bl_options = {'DEFAULT_CLOSED'}

    @safe_draw
    def draw_header(self, context):
        _header_icon(self.layout, 'hair', 'STRANDS')
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
        for prop in ("hair_density", "hair_length", "hair_length_var", "hair_width", "hair_lift", "hair_seed",
                     "hair_variants"):
            c.prop(p, prop)
        box = self.layout.box()
        box.label(text="Strand shape (shared by all filters)", icon=ic('LINKED'))
        sc = box.column(align=True)
        sc.prop(p, "hair_curl")
        if p.hair_lib is not None:
            sc.label(text="Shared set: %d strands" % len(p.hair_lib.objects))
        _particle_footer(self.layout, p, p.hair_lib, p.hair_count, "strands", "hair")


class FILTER_PT_dust(FILTER_SubPanel, Panel):
    header_art = "dust"
    header_icon = "PARTICLES"
    bl_label = "Dust"
    bl_idname = "FILTER_PT_dust"
    bl_options = {'DEFAULT_CLOSED'}

    @safe_draw
    def draw_header(self, context):
        _header_icon(self.layout, 'dust', 'PARTICLES')
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
        for prop in ("dust_density", "dust_size", "dust_size_var", "dust_tilt", "dust_seed", "dust_variants"):
            c.prop(p, prop)
        box = self.layout.box()
        box.label(text="Dust pieces (shared by all filters)", icon=ic('LINKED'))
        if p.dust_lib is not None:
            box.label(text="Shared set: %d pieces" % len(p.dust_lib.objects))
        row = box.row(align=True)
        row.prop(p, "dust_types", expand=True)
        _particle_footer(self.layout, p, p.dust_lib, p.dust_count, "pieces", "dust")


class FILTER_PT_boolean(FILTER_SubPanel, Panel):
    header_art = "boolean"
    header_icon = "MOD_BOOLEAN"
    bl_label = "Boolean"
    bl_idname = "FILTER_PT_boolean"
    bl_options = {'DEFAULT_CLOSED'}

    @safe_draw
    def draw_header(self, context):
        _header_icon(self.layout, 'boolean', 'MOD_BOOLEAN')
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
    header_art = "materials"
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
        col = layout.column(align=True)
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
        col.separator()
        col.prop(p, "uv_mode")
        sub = col.row(align=True)
        sub.active = p.uv_mode == 'WORLD'
        sub.prop(p, "uv_tile")
        if p.use_hair:
            layout.template_ID(p, "hair_material", new="material.new")
        if p.use_dust:
            layout.template_ID(p, "dust_material", new="material.new")
        if p.use_hair or p.use_dust:
            if p.use_dust:
                layout.label(text="The dust material is shared by all filters", icon=ic('LINKED'))


class FILTER_PT_game(FILTER_SubPanel, Panel):
    header_art = "finish"
    header_icon = "EXPORT"
    bl_label = "Game-Ready"
    bl_idname = "FILTER_PT_game"
    bl_options = {'DEFAULT_CLOSED'}

    @safe_draw
    def draw(self, context):
        p = self.P(context)
        if p is None:
            return
        layout = self.layout
        col = layout.column(align=True)
        col.prop(p, "game_lod")
        col.prop(p, "game_resolution")
        col.separator()
        col.label(text="Bake maps")
        row = col.row(align=True)
        row.prop(p, "game_maps", expand=True)
        col.prop(p, "game_particles")
        col.separator()
        col.prop(p, "game_export")
        sub = col.row(align=True)
        sub.active = p.game_export != 'NONE'
        sub.prop(p, "game_path", text="")
        layout.operator("filter.game_ready", icon=ic('EXPORT'))
        layout.label(text="Bakes with Cycles; this can take a minute.", icon=ic('INFO'))


class FILTER_PT_actions(FILTER_SubPanel, Panel):
    header_art = "finish"
    header_icon = "CHECKMARK"
    bl_label = "Finish"
    bl_idname = "FILTER_PT_actions"

    @safe_draw
    def draw(self, context):
        row = self.layout.row(align=True)
        row.operator("filter.finish", icon=ic('CHECKMARK'))
        row.operator("filter.delete", text="", icon=ic('TRASH'))


# ---------------------------------------------------------------------------
# Panel artwork: anti-aliased vector line drawings on a transparent background
# ---------------------------------------------------------------------------

ART_WHITE = (0.93, 0.95, 1.0)
ART_ORANGE = (1.0, 0.55, 0.12)
ART_GREY = (0.52, 0.53, 0.60)


class _Art:
    """Tiny RGBA rasteriser (y up, straight alpha). Strokes use distance-based anti-aliasing."""

    def __init__(self, w, h):
        self.W, self.H = w, h
        self.buf = [0.0] * (w * h * 4)

    def _blend(self, x, y, col, a):
        if a <= 0.0 or not (0 <= x < self.W and 0 <= y < self.H):
            return
        k = (y * self.W + x) * 4
        b = self.buf
        da = b[k + 3]
        oa = a + da * (1.0 - a)
        if oa <= 1e-9:
            return
        for i in range(3):
            b[k + i] = (col[i] * a + b[k + i] * da * (1.0 - a)) / oa
        b[k + 3] = oa

    def stroke(self, pts, width, col, closed=False, alpha=1.0):
        """Polyline with round joins. Coverage is merged per stroke so joints do not darken."""
        if len(pts) < 2:
            if pts:
                self.dot(pts[0][0], pts[0][1], width * 0.5, col, alpha)
            return
        segs = list(zip(pts, pts[1:]))
        if closed:
            segs.append((pts[-1], pts[0]))
        r = width * 0.5
        cov = {}
        for (x0, y0), (x1, y1) in segs:
            dx, dy = x1 - x0, y1 - y0
            ll = dx * dx + dy * dy
            xa = max(0, int(math.floor(min(x0, x1) - r - 1)))
            xb = min(self.W - 1, int(math.ceil(max(x0, x1) + r + 1)))
            ya = max(0, int(math.floor(min(y0, y1) - r - 1)))
            yb = min(self.H - 1, int(math.ceil(max(y0, y1) + r + 1)))
            for py in range(ya, yb + 1):
                cy = py + 0.5
                for px in range(xa, xb + 1):
                    cx = px + 0.5
                    if ll > 1e-12:
                        t = ((cx - x0) * dx + (cy - y0) * dy) / ll
                        t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
                    else:
                        t = 0.0
                    ex, ey = x0 + dx * t - cx, y0 + dy * t - cy
                    c = r + 0.5 - math.sqrt(ex * ex + ey * ey)
                    if c > 0.0:
                        c = 1.0 if c > 1.0 else c
                        key = (px, py)
                        if c > cov.get(key, 0.0):
                            cov[key] = c
        for (px, py), c in cov.items():
            self._blend(px, py, col, c * alpha)

    def dashed(self, p0, p1, width, col, on=5.0, off=4.0, alpha=1.0):
        (x0, y0), (x1, y1) = p0, p1
        length = math.hypot(x1 - x0, y1 - y0)
        if length < 1e-6:
            return
        pos = 0.0
        while pos < length:
            a, b = pos / length, min(pos + on, length) / length
            self.stroke([(x0 + (x1 - x0) * a, y0 + (y1 - y0) * a),
                         (x0 + (x1 - x0) * b, y0 + (y1 - y0) * b)], width, col, alpha=alpha)
            pos += on + off

    def dot(self, x, y, r, col, alpha=1.0):
        xa, xb = max(0, int(x - r - 1)), min(self.W - 1, int(x + r + 1))
        ya, yb = max(0, int(y - r - 1)), min(self.H - 1, int(y + r + 1))
        for py in range(ya, yb + 1):
            for px in range(xa, xb + 1):
                c = r + 0.5 - math.hypot(px + 0.5 - x, py + 0.5 - y)
                if c > 0.0:
                    self._blend(px, py, col, min(1.0, c) * alpha)

    def fill(self, poly, col, alpha=1.0):
        """Even-odd scanline fill with 4x vertical supersampling for soft edges."""
        if len(poly) < 3:
            return
        ys = [q[1] for q in poly]
        y0 = max(0, int(math.floor(min(ys))))
        y1 = min(self.H - 1, int(math.ceil(max(ys))))
        n = len(poly)
        for py in range(y0, y1 + 1):
            row = {}
            for sub in range(4):
                yc = py + (sub + 0.5) / 4.0
                xs = []
                for i in range(n):
                    (xa, ya), (xb, yb) = poly[i], poly[(i + 1) % n]
                    if (ya <= yc < yb) or (yb <= yc < ya):
                        xs.append(xa + (yc - ya) * (xb - xa) / (yb - ya))
                xs.sort()
                for i in range(0, len(xs) - 1, 2):
                    l, rr = xs[i], xs[i + 1]
                    for px in range(max(0, int(math.floor(l))), min(self.W - 1, int(math.ceil(rr))) + 1):
                        c = max(0.0, min(px + 1.0, rr) - max(float(px), l))
                        if c > 0:
                            row[px] = row.get(px, 0.0) + c * 0.25
            for px, c in row.items():
                self._blend(px, py, col, min(1.0, c) * alpha)

    def downsampled(self, f):
        """Box-filtered copy at 1/f resolution (premultiplied average)."""
        W, H = self.W // f, self.H // f
        out = [0.0] * (W * H * 4)
        b = self.buf
        inv = 1.0 / (f * f)
        for y in range(H):
            for x in range(W):
                r = g = bl = a = 0.0
                for yy in range(y * f, y * f + f):
                    for xx in range(x * f, x * f + f):
                        k = (yy * self.W + xx) * 4
                        aa = b[k + 3]
                        r += b[k] * aa
                        g += b[k + 1] * aa
                        bl += b[k + 2] * aa
                        a += aa
                k = (y * W + x) * 4
                if a > 1e-9:
                    out[k], out[k + 1], out[k + 2] = r / a, g / a, bl / a
                out[k + 3] = a * inv
        return W, H, out


# ---- header icons --------------------------------------------------------
# Each icon is drawn on a 0..1 square (y up) and rasterised at 32 x 32.

def _ic_zig(n=3, x0=0.12, x1=0.88, ylo=0.32, yhi=0.68):
    pts = []
    for i in range(2 * n + 1):
        pts.append((x0 + (x1 - x0) * i / (2 * n), yhi if i % 2 else ylo))
    return pts


def _ic_circle(cx, cy, r, a0=0.0, a1=2 * math.pi, n=40):
    return [(cx + r * math.cos(a0 + (a1 - a0) * i / n), cy + r * math.sin(a0 + (a1 - a0) * i / n))
            for i in range(n + 1)]


def _ic_round_zig(cuts=5):
    tab = _profile_table(0.17, 0.17, cuts)
    ev = _make_eval(tab)
    pts = []
    for i in range(61):
        x = i / 60.0
        pts.append((0.1 + 0.8 * x, 0.5 + 0.2 * ev(x * 2.0 - 0.25)))
    return pts


def _icon_shapes(name):
    """List of ('stroke'|'dot'|'dash', data...) in unit space."""
    S = []
    if name == 'main':
        S.append(('stroke', [(0.12, 0.18), (0.88, 0.18), (0.88, 0.82), (0.12, 0.82)], True))
        S.append(('stroke', _ic_zig(3, 0.24, 0.76, 0.32, 0.68), False))
    elif name == 'presets':
        for y, k in ((0.75, 0.30), (0.5, 0.66), (0.25, 0.44)):
            S.append(('stroke', [(0.12, y), (0.88, y)], False))
            S.append(('ring', (k, y, 0.085)))
    elif name == 'pleats':
        S.append(('stroke', _ic_zig(3), False))
        S.append(('stroke', [(0.12, 0.2), (0.88, 0.2)], False, 0.45))
    elif name == 'bevel':
        S.append(('stroke', _ic_round_zig(), False))
        S.append(('dash', (0.5, 0.86), (0.5, 0.14)))
    elif name == 'shading':
        S.append(('stroke', _ic_circle(0.5, 0.5, 0.34), True))
        S.append(('stroke', [(0.5 + 0.12 * math.cos(t), 0.5 + 0.34 * math.sin(t))
                             for t in [math.pi * (-0.5 + i / 20.0) for i in range(21)]], False))
        for k in range(3):                      # hatching on the shadow side
            y = 0.34 + k * 0.16
            S.append(('stroke', [(0.66, y - 0.05), (0.78, y + 0.05)], False, 0.75))
    elif name == 'wear':
        rng = random.Random(7)
        pts = [(0.1 + 0.8 * i / 14.0, 0.5 + (0.16 if i % 2 else -0.16) * (0.5 + rng.random() * 0.6))
               for i in range(15)]
        S.append(('stroke', pts, False))
        S.append(('dot', (0.28, 0.82, 0.04)))
        S.append(('dot', (0.66, 0.18, 0.035)))
        S.append(('dot', (0.80, 0.80, 0.03)))
    elif name == 'border':
        S.append(('stroke', [(0.1, 0.18), (0.24, 0.18), (0.24, 0.82), (0.1, 0.82)], True))
        S.append(('stroke', [(0.76, 0.18), (0.9, 0.18), (0.9, 0.82), (0.76, 0.82)], True))
        S.append(('stroke', _ic_zig(2, 0.24, 0.76, 0.34, 0.66), False))
    elif name == 'hair':
        for k, (x, bend) in enumerate(((0.28, -0.12), (0.5, 0.1), (0.72, -0.06))):
            pts = []
            for i in range(17):
                t = i / 16.0
                pts.append((x + bend * t * t + 0.05 * math.sin(t * 7.0 + k), 0.16 + 0.68 * t))
            S.append(('stroke', pts, False))
        S.append(('stroke', [(0.14, 0.14), (0.86, 0.14)], False))
    elif name == 'dust':
        for x, y, r in ((0.3, 0.68, 0.075), (0.62, 0.74, 0.05), (0.48, 0.42, 0.095),
                        (0.76, 0.40, 0.04), (0.24, 0.30, 0.045), (0.68, 0.18, 0.06)):
            S.append(('ring', (x, y, r)))
        S.append(('dot', (0.84, 0.62, 0.03)))
        S.append(('dot', (0.14, 0.52, 0.025)))
    elif name == 'boolean':
        S.append(('stroke', [(0.12, 0.36), (0.6, 0.36), (0.6, 0.86), (0.12, 0.86)], True))
        S.append(('stroke', _ic_circle(0.62, 0.38, 0.25), True))
    elif name == 'materials':
        S.append(('stroke', _ic_circle(0.5, 0.56, 0.3), True))
        S.append(('stroke', [(0.5 + 0.3 * math.cos(a), 0.56 + 0.09 * math.sin(a))
                             for a in [math.pi * (1 + i / 16.0) for i in range(17)]], False, 0.8))
        S.append(('dot', (0.4, 0.68, 0.045)))
        S.append(('stroke', [(0.26, 0.12), (0.74, 0.12)], False))
        S.append(('stroke', [(0.5, 0.26), (0.5, 0.12)], False))
    elif name == 'finish':
        S.append(('stroke', _ic_circle(0.5, 0.5, 0.36), True))
        S.append(('stroke', [(0.32, 0.5), (0.45, 0.36), (0.7, 0.64)], False))
    return S


ICON_NAMES = ('main', 'presets', 'pleats', 'bevel', 'shading', 'wear', 'border',
              'hair', 'dust', 'boolean', 'materials', 'finish')


def _render_icon(name, size=32, ss=2):
    """Returns (w, h, pixels) for one header icon, drawn at ss x size and box-filtered down."""
    N = size * ss
    art = _Art(N, N)
    lw = 2.1 * ss
    for item in _icon_shapes(name):
        kind = item[0]
        if kind == 'stroke':
            alpha = item[3] if len(item) > 3 else 1.0
            art.stroke([(x * N, y * N) for x, y in item[1]], lw, ART_WHITE, closed=item[2], alpha=alpha)
        elif kind == 'dash':
            art.dashed((item[1][0] * N, item[1][1] * N), (item[2][0] * N, item[2][1] * N),
                       lw * 0.6, ART_WHITE, on=3.0 * ss, off=2.5 * ss, alpha=0.6)
        elif kind == 'dot':
            x, y, r = item[1]
            art.dot(x * N, y * N, r * N, ART_WHITE)
        elif kind == 'ring':
            x, y, r = item[1]
            art.stroke([(x * N + r * N * math.cos(t), y * N + r * N * math.sin(t))
                        for t in [2 * math.pi * i / 24 for i in range(25)]], lw, ART_WHITE)
    return art.downsampled(ss)


# ---- the two diagram graphics -----------------------------------------------

def _paint_profile_art(rp, rv, cuts, N=160):
    """Two pleats: grey = sharp fold, white = bevelled result, orange = mesh cuts."""
    art = _Art(N, N)
    table = _profile_table(rp, rv, cuts)
    ev = _make_eval(table)
    m, cy, A = 12, N * 0.5, N * 0.30

    def to_px(X, w):
        return m + (X + 0.5) / 2.0 * (N - 2 * m), cy + w * A

    art.dashed((m, cy), (N - m, cy), 1.0, ART_GREY, on=3, off=3, alpha=0.8)
    ghost = [(-0.5, -1.0), (0.0, 1.0), (0.5, -1.0), (1.0, 1.0), (1.5, -1.0)]
    art.stroke([to_px(X, w) for X, w in ghost], 1.4, ART_GREY)
    seq = sorted({(k + x, w) for k in range(-1, 3) for x, w in table if -0.5 <= k + x <= 1.5})
    seq = [(-0.5, ev(-0.5))] + [s for s in seq if s[0] > -0.5 + 1e-9] + [(1.5, ev(1.5))]
    art.stroke([to_px(X, w) for X, w in seq], 2.6, ART_WHITE)
    for X, w in seq[1:-1]:
        x, y = to_px(X, w)
        art.dot(x, y, 2.6, ART_ORANGE)
    return art


def _paint_border_art(p, N=160):
    """Cross-section across the whole filter, after the sketch:
    white = pleats, orange = border frame, grey = bounding lines."""
    art = _Art(N, N)
    W, O, F, B, fit = _border_dims(p)
    depth = max(p.pleat_depth, 1e-6)
    top, bot = max(F, depth), -max(B, depth)
    my = 30.0
    ky = (N - 2 * my) / max(top - bot, 1e-9)
    side = (O + W) * ky
    max_side = N * 0.21
    kx = ky if side <= max_side else ky * max_side / max(side, 1e-9)
    mx = 14.0
    edge_l = mx + O * kx
    edge_r = N - mx - O * kx

    def Y(h):
        return my + (h - bot) * ky

    # Grey bounding lines (drawn first, like construction lines that overshoot).
    yb = Y(bot)
    art.stroke([(4, yb), (N - 4, yb)], 1.6, ART_GREY)
    for xe in (edge_l, edge_r):
        art.stroke([(xe, max(3.0, Y(bot) - 16)), (xe, min(N - 3.0, Y(top) + 16))], 1.6, ART_GREY)

    # White pleats with the real bevel profile, spanning edge to edge.
    table = _profile_table(*_bevel_radii(p), p.bevel_cuts)
    ev = _make_eval(table)
    span = max(edge_r - edge_l, 1.0)
    period_px = ky / max(p.pleat_density, 1e-6)
    period_px = min(max(period_px, span / 6.0), span / 2.5)
    xs = set(i * 1.5 for i in range(int(span / 1.5) + 1))
    xs.add(span)
    for k in range(-1, int(span / period_px) + 2):
        for tx, _w in table:
            x = (k + tx - p.pleat_phase) * period_px
            if 0.0 <= x <= span:
                xs.add(x)
    pts = [(edge_l + x, Y(depth * ev(x / period_px + p.pleat_phase))) for x in sorted(xs)]
    art.stroke(pts, 2.4, ART_WHITE)

    # Orange border frames (actual cross-section), mirrored on both edges.
    sec = _border_section(p, W, O, F, B, fit)
    left = [(edge_l + s * kx, Y(h)) for s, h in sec]
    right = [(edge_r - s * kx, Y(h)) for s, h in sec]
    for poly in (left, right):
        art.fill(poly, ART_ORANGE, alpha=0.16)
        art.stroke(poly, 2.2, ART_ORANGE, closed=True)
    return art


def _set_preview(pv, art, icon_px=32):
    """Fill both the large image and the small icon buffers of an ImagePreview."""
    pv.image_size = (art.W, art.H)
    pv.image_pixels_float = art.buf
    f = max(1, art.W // icon_px)
    w, h, small = art.downsampled(f)
    pv.icon_size = (w, h)
    pv.icon_pixels_float = small


def profile_icon(p):
    pv = _pv.get("profile")
    if pv is None:
        return 0
    rp, rv = _bevel_radii(p)
    key = (round(rp, 3), round(rv, 3), int(p.bevel_cuts))
    if _pv_key.get("profile") != key:
        try:
            _set_preview(pv, _paint_profile_art(rp, rv, p.bevel_cuts))
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
           round(p.border_bevel, 6), int(p.border_cuts), round(p.pleat_depth, 6),
           round(p.pleat_density, 3), round(p.pleat_phase, 3), round(p.bevel_ridge, 3),
           round(p.bevel_valley, 3), int(p.bevel_cuts))
    if _pv_key.get("border") != key:
        try:
            _set_preview(pv, _paint_border_art(p))
            _pv_key["border"] = key
        except Exception:
            traceback.print_exc()
            return 0
    return pv.icon_id


def art_icon(name):
    """icon_id of a custom header icon (painted once, lazily), or 0."""
    key = "icon_" + name
    pv = _pv.get(key)
    if pv is None and _pcoll[0] is not None and name in ICON_NAMES:
        try:
            pv = _pcoll[0].new(key)
            w, h, px = _render_icon(name)
            pv.icon_size = (w, h)
            pv.icon_pixels_float = px
            pv.image_size = (w, h)
            pv.image_pixels_float = px
            _pv[key] = pv
        except Exception:
            traceback.print_exc()
            return 0
    return pv.icon_id if pv is not None else 0


def _header_icon(layout, name, fallback):
    icon_id = art_icon(name) if name else 0
    if icon_id:
        layout.label(text="", icon_value=icon_id)
    else:
        layout.label(text="", icon=ic(fallback))


def _particle_footer(layout, p, coll, count, noun, kind):
    box = layout.box()
    col = box.column(align=True)
    col.label(text="~%s %s, instanced with Geometry Nodes" % (format(count, ","), noun), icon=ic('GEOMETRY_NODES'))
    if coll is not None:
        col.label(text="Pieces: " + coll.name, icon=ic('OUTLINER_COLLECTION'))
    row = col.row(align=True)
    row.prop(p, "particle_side", expand=True)
    col.prop(p, "particles_avoid_border")
    sub = col.row(align=True)
    sub.active = p.use_border and p.particles_avoid_border
    sub.prop(p, kind + "_border_falloff")
    col.prop(p, kind + "_viewport", slider=True)
    if p.particles_obj is not None:
        col.label(text="On: %s  (modifier: %s)" % (p.particles_obj.name, SCATTER_MOD), icon=ic('MODIFIER'))


classes = (FilterProps, FILTER_OT_new_line, FILTER_OT_add_shape, FILTER_OT_from_selection, FILTER_OT_edit_source_curve, FILTER_OT_regenerate,
           FILTER_OT_randomize, FILTER_OT_use_reference, FILTER_OT_preset_apply,
           FILTER_OT_preset_save, FILTER_OT_preset_update, FILTER_OT_preset_delete,
           FILTER_OT_finish, FILTER_OT_delete, FILTER_PT_main, FILTER_PT_presets,
           FILTER_PT_pleats, FILTER_PT_bevel, FILTER_PT_shading, FILTER_PT_wear,
           FILTER_PT_border, FILTER_PT_diecut, FILTER_PT_hair, FILTER_PT_dust, FILTER_PT_boolean,
           FILTER_PT_materials, FILTER_PT_game, FILTER_OT_game_ready, FILTER_PT_actions)


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


def _edition_conflict():
    """Pro and Lite share the same data (so files open in either); only one may be
    enabled at a time."""
    other = bpy.app.driver_namespace.get(_EDITION_KEY)
    if other and other[1] != __name__:
        return other[0]
    return None


def register():
    other = _edition_conflict()
    if other:
        raise RuntimeError("Filter Maker %s is already enabled. Disable it before enabling Filter Maker %s."
                           % (other.title(), EDITION.title()))
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
        for name in ICON_NAMES:
            art_icon(name)
        # One persistent callback makes curve-based filters follow source edits.
        for handler in list(bpy.app.handlers.depsgraph_update_post):
            if getattr(handler, "__name__", "") == "_sync_curve_sources":
                bpy.app.handlers.depsgraph_update_post.remove(handler)
        bpy.app.handlers.depsgraph_update_post.append(_sync_curve_sources)
        bpy.app.driver_namespace[_EDITION_KEY] = (EDITION, __name__)
    except Exception:
        traceback.print_exc()
        _teardown()
        raise


def unregister():
    _teardown()
    other = bpy.app.driver_namespace.get(_EDITION_KEY)
    if other and other[1] == __name__:
        del bpy.app.driver_namespace[_EDITION_KEY]


if __name__ == "__main__":
    register()
