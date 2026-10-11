bl_info = {
    "name": "Filter Maker Pro",
    "author": "Tooboi",
    "version": (3, 5, 1),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar (N) > Filter Maker",
    "description": "Pleated zig-zag filter generator driven entirely from the N-panel "
                   "(no node setup): from a line, curve, mesh edges or faces.",
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
from mathutils import Euler, Vector, geometry, noise

_busy = [False]          # suppress live updates while we set properties in code
_KEEP = [None]           # optional "keep this hair/dust point" test (set while generating)
MAX_CUTS = 400
MAX_INST = 100000
BOOL_NAME = "Filter Boolean"


# ---------------------------------------------------------------------------
#  UI safety helpers
# ---------------------------------------------------------------------------

def redraw_all():
    """Tag every area for redraw so panels always reflect the current state."""
    try:
        for win in bpy.context.window_manager.windows:
            for area in win.screen.areas:
                area.tag_redraw()
    except Exception:
        pass


def safe_draw(fn):
    """A draw error is shown in the panel instead of silently hiding it."""
    def wrapper(self, context):
        try:
            fn(self, context)
        except Exception as e:
            traceback.print_exc()
            try:
                self.layout.label(text="UI error: %s" % e, icon='ERROR')
            except Exception:
                pass
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# ---------------------------------------------------------------------------
#  Pleat profile (bevel) helpers
# ---------------------------------------------------------------------------

def _bevel_radii(p):
    """Bevel half-widths of ridge / valley in phase units (0 sharp .. 0.25 fully round)."""
    return (min(0.25, max(0.0, p.bevel_ridge) * 0.25),
            min(0.25, max(0.0, p.bevel_valley) * 0.25))


def _profile_table(rp, rv, cuts):
    """One pleat period as sorted (x, w) vertices. Ridge (+1) at x=0, valley (-1) at x=0.5.

    Every corner is replaced by the same bevel (quadratic arc cut into `cuts`
    segments), so every pleat in the filter has an identical profile.
    """
    n = max(1, int(cuts))

    def arc(c, p0, p1):
        out = []
        for i in range(n + 1):
            u = i / n
            a, b, d = (1 - u) * (1 - u), 2 * u * (1 - u), u * u
            out.append((a * p0[0] + b * c[0] + d * p1[0], a * p0[1] + b * c[1] + d * p1[1]))
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
    """Periodic piecewise-linear evaluation of the profile."""
    xs = [t[0] for t in table]
    ws = [t[1] for t in table]
    x0 = xs[0]
    n = len(xs)

    def f(x):
        xm = (x - x0) % 1.0 + x0
        i = bisect.bisect_right(xs, xm) - 1
        if i < 0:
            i = 0
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
    """Profile vertices of one period plus extra points so no segment is longer than maxlen."""
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
#  Path helpers
# ---------------------------------------------------------------------------

def _polyline_length(pts, closed):
    total = sum((b - a).length for a, b in zip(pts, pts[1:]))
    if closed and len(pts) > 1:
        total += (pts[0] - pts[-1]).length
    return total


def _resample_at(pts, ss, closed):
    """Points at the given (ascending) arc-length positions."""
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
        f = 0.0 if l1 == l0 else (s - l0) / (l1 - l0)
        out.append(P[seg].lerp(P[seg + 1], f))
    return out


def _resample(pts, n, closed):
    total = _polyline_length(pts, closed)
    ss = [total * i / n if closed else total * i / (n - 1) for i in range(n)]
    return _resample_at(pts, ss, closed)


def _chains(edges):
    """edges: list of (a, b) vertex ids -> list of (id_path, closed)."""
    adj = {}
    for ei, (a, b) in enumerate(edges):
        if a == b:
            continue
        adj.setdefault(a, []).append(ei)
        adj.setdefault(b, []).append(ei)
    used = set()

    def walk(start):
        path, v = [start], start
        while True:
            nxt = None
            for ei in adj[v]:
                if ei not in used:
                    nxt = ei
                    break
            if nxt is None:
                break
            used.add(nxt)
            a, b = edges[nxt]
            v = b if a == v else a
            path.append(v)
        return path

    res = []
    for v in [v for v, es in adj.items() if len(es) != 2]:
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


# ---------------------------------------------------------------------------
#  Source extraction
# ---------------------------------------------------------------------------

def _r(v):
    return [round(v[0], 5), round(v[1], 5), round(v[2], 5)]


def paths_from_curve(obj):
    mw = obj.matrix_world
    paths, closed = [], []
    for sp in obj.data.splines:
        if sp.type == 'BEZIER':
            bp = sp.bezier_points
            n = len(bp)
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
        paths.append([_r(mw @ p) for p in pts])
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
        if len(ids) < 2:
            continue
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
        faces = [list(p.vertices) for p in me.polygons]
        coords = {v.index: mw @ v.co for v in me.vertices}
    used = sorted({i for f in faces for i in f})
    remap = {old: new for new, old in enumerate(used)}
    return {"verts": [_r(coords[i]) for i in used],
            "faces": [[remap[i] for i in f] for f in faces]}


def _src_size(mode, src):
    pts = [p for path in src["paths"] for p in path] if mode == 'CURVE' else src["verts"]
    if not pts:
        return 1.0
    xs, ys, zs = zip(*pts)
    return max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs), 1e-3)


# ---------------------------------------------------------------------------
#  Mesh builders
# ---------------------------------------------------------------------------

def _wall_path(rb, pts, cl, d, H, density):
    """Flat reference wall (path x height) used for the border and the envelope cutter."""
    L = _polyline_length(pts, cl)
    if cl:
        n = min(2000, max(4, int(round(L * density * 2))))
    else:
        n = min(2000, max(2, int(round(L * density * 2)) + 1))
    rcp = _resample(pts, n, cl)
    rc = [[rb.verts.new(c), rb.verts.new(c + d * H)] for c in rcp]
    segs = n if cl else n - 1
    for i in range(segs):
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
    d = _height_dir(p)
    H = max(p.height, 1e-5)
    rows = max(1, min(200, int(round(H * p.detail))))
    rp, rv = _bevel_radii(p)
    table = _profile_table(rp, rv, p.bevel_cuts)
    ev = _make_eval(table)
    depth = p.pleat_depth

    for path, cl in zip(src["paths"], src["closed"]):
        pts = [Vector(x) for x in path]
        L = _polyline_length(pts, cl)
        nhalf = max(2, int(round(L * p.pleat_density * 2)))
        if cl and nhalf % 2:
            nhalf += 1
        Lp = nhalf / 2.0                                  # whole pleats along this path
        period = L / Lp if L > 1e-9 else 1.0 / max(p.pleat_density, 1e-3)

        maxlen = 1.0 / max(p.detail, 1e-3)
        xs = _cycle_xs(table, period, depth, maxlen)
        for _ in range(8):                                # keep the vertex count sane
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
        n = len(cp)
        q3 = []
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
        segs = n if cl else n - 1
        for i in range(segs):
            j = (i + 1) % n
            for k in range(rows):
                try:
                    bm.faces.new((cols[i][k], cols[j][k], cols[j][k + 1], cols[i][k + 1]))
                except ValueError:
                    pass

        if ref is not None:
            _wall_path(ref, pts, cl, d, H, p.pleat_density)


def _bisect_all(g, co, no):
    geom = g.verts[:] + g.edges[:] + g.faces[:]
    bmesh.ops.bisect_plane(g, geom=geom, dist=1e-6, plane_co=co, plane_no=no)


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
    """Triangulate only n-gons, then re-join the triangles into quads where possible."""
    big = [f for f in g.faces if len(f.verts) > 4]
    if not big:
        return
    res = bmesh.ops.triangulate(g, faces=big)
    tris = [f for f in res.get("faces", []) if f.is_valid]
    if not tris:
        return
    try:
        bmesh.ops.join_triangles(g, faces=tris, angle_face_threshold=0.02, angle_shape_threshold=1.2)
    except Exception:
        pass


def _face_component(bm, p, comp):
    """Pleat a connected set of faces as ONE continuous surface.

    The faces are 'unfolded': a single across-ridge coordinate u runs through
    every face (continued over hinges and coplanar neighbours), the pleat
    profile is evaluated on that coordinate, and the pieces are welded.
    """
    f0 = max(comp, key=lambda f: f.calc_area())
    n0 = f0.normal.normalized()
    ref = Vector((1, 0, 0))
    if abs(n0.dot(ref)) > 0.95:
        ref = Vector((0, 1, 0))
    u0 = (ref - n0 * ref.dot(n0)).normalized()
    v0 = n0.cross(u0)
    a = p.pleat_angle
    ua0 = u0 * math.cos(a) + v0 * math.sin(a)
    ra = n0.cross(ua0)                                    # ridge direction

    fr = {}
    for f in comp:
        nf = f.normal.normalized()
        raf = ra - nf * ra.dot(nf)
        if raf.length < 1e-6:
            raf = ua0 - nf * ua0.dot(nf)
        if raf.length < 1e-6:
            raf = nf.cross(Vector((0, 0, 1)))
        if raf.length < 1e-6:
            raf = Vector((1, 0, 0))
        raf.normalize()
        fr[f.index] = [raf.cross(nf), raf, 0.0]           # across-ridge dir, ridge dir, u offset

    uv = {}
    done = {f0.index}
    ua, _, _o = fr[f0.index]
    fr[f0.index][2] = -ua.dot(f0.verts[0].co)
    for v in f0.verts:
        uv[v.index] = ua.dot(v.co) + fr[f0.index][2]
    queue = deque([f0])
    while queue:
        cur = queue.popleft()
        for e in cur.edges:
            for nf in e.link_faces:
                if nf.index in done:
                    continue
                anc = next((v for v in nf.verts if v.index in uv), None)
                if anc is None:
                    continue
                ua = fr[nf.index][0]
                fr[nf.index][2] = uv[anc.index] - ua.dot(anc.co)
                for v in nf.verts:
                    if v.index not in uv:
                        uv[v.index] = ua.dot(v.co) + fr[nf.index][2]
                done.add(nf.index)
                queue.append(nf)

    rp, rv = _bevel_radii(p)
    table = _profile_table(rp, rv, p.bevel_cuts)
    ev = _make_eval(table)
    period = 1.0 / max(p.pleat_density, 1e-3)
    depth = p.pleat_depth
    uvals = list(uv.values())
    umin, umax = min(uvals), max(uvals)
    base = umin - period * p.pleat_phase                  # ridges sit at base + k * period

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
    for f in comp:
        ua, raf, off = fr[f.index]
        g = bmesh.new()
        nv = [g.verts.new(v.co) for v in f.verts]
        try:
            g.faces.new(nv)
        except ValueError:
            g.free()
            continue
        fus = [ua.dot(v.co) + off for v in f.verts]
        u_lo, u_hi = min(fus), max(fus)
        k0 = int(math.floor((u_lo - base) / period)) - 1
        k1 = int(math.ceil((u_hi - base) / period)) + 1
        for k in range(k0, k1 + 1):
            for x in xs:
                ut = base + (k + x) * period
                if u_lo + 1e-6 < ut < u_hi - 1e-6:
                    _bisect_all(g, ua * (ut - off), ua)
        fvs = [raf.dot(v.co) for v in f.verts]
        v_lo, v_hi = min(fvs), max(fvs)
        vs = vstep
        if (v_hi - v_lo) / vs > MAX_CUTS:
            vs = (v_hi - v_lo) / MAX_CUTS
        m = int(math.floor(v_lo / vs)) + 1
        while m * vs < v_hi - 1e-6:
            if m * vs > v_lo + 1e-6:
                _bisect_all(g, raf * (m * vs), raf)
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
                    nvv[lay] = ua.dot(v.co) + off
                    vm[v] = nvv
                vl.append(vm[v])
            try:
                cb.faces.new(vl)
            except ValueError:
                pass
        g.free()

    size = max(umax - umin, 1e-3)
    bmesh.ops.remove_doubles(cb, verts=cb.verts[:], dist=max(1e-5, size * 2e-6))
    cb.normal_update()
    for v in cb.verts:
        x = (v[lay] - base) / period
        v.co += v.normal * (depth * ev(x))

    vmap = {}
    for v in cb.verts:
        vmap[v] = bm.verts.new(v.co)
    for f in cb.faces:
        try:
            bm.faces.new([vmap[v] for v in f.verts])
        except ValueError:
            pass
    cb.free()


def _faces_bmesh(src):
    """Reference faces as a bmesh with consistent normals and valid indices."""
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
    comps = _face_components(tmp)
    for comp in comps:
        if len(comp) > 1:
            bmesh.ops.recalc_face_normals(tmp, faces=comp)
    tmp.normal_update()
    return tmp, comps


def build_face(bm, p, src, ref=None):
    tmp, comps = _faces_bmesh(src)
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
#  Wear, shading
# ---------------------------------------------------------------------------

def apply_noise(bm, p):
    """Clouds-texture displace: local coordinates, RGB->XYZ (or along normals)."""
    strength = p.noise_strength
    if strength <= 0 or p.noise_scale <= 0 or not bm.verts:
        return
    inv = 1.0 / p.noise_scale
    sd = p.noise_seed
    off = Vector((sd * 37.1, sd * 17.3, sd * 7.9))
    hard = p.noise_type == 'HARD'
    basis, depth = p.noise_basis, p.noise_depth
    verts = list(bm.verts)
    xyz = p.noise_direction == 'XYZ'
    offs = (Vector((0.0, 0.0, 0.0)), Vector((31.7, 17.3, 5.1)), Vector((9.9, 43.1, 21.7)))

    vals = []
    for v in verts:
        q = v.co * inv + off
        if xyz:
            vals.append(Vector([noise.turbulence(q + o, depth, hard, noise_basis=basis) for o in offs]))
        else:
            vals.append(noise.turbulence(q, depth, hard, noise_basis=basis))
    mean = sum(vals, Vector((0.0, 0.0, 0.0)) if xyz else 0.0) / len(vals)   # = the "midlevel"
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
        sharp = False
        if p.auto_smooth and len(e.link_faces) == 2:
            sharp = e.calc_face_angle(0.0) > p.auto_smooth_angle
        e.smooth = not sharp


# ---------------------------------------------------------------------------
#  Border (swept cross-section around the reference outline)
# ---------------------------------------------------------------------------

def _arc2(c, a, b, n):
    out = []
    for i in range(n + 1):
        u = i / n
        w0, w1, w2 = (1 - u) ** 2, 2 * u * (1 - u), u * u
        out.append((w0 * a[0] + w1 * c[0] + w2 * b[0], w0 * a[1] + w1 * c[1] + w2 * b[1]))
    return out


def _border_dims(p, small=None):
    """(inward width W, outward overhang O, front F, back B, pleat reach). All distances."""
    W = max(p.border_width, 1e-6)
    if small:
        W = min(W, small * 0.48)
    O = max(p.border_overhang, 0.0)
    fit = (p.pleat_depth + p.thickness + 1.5 * p.noise_strength) if p.border_fit else 0.0
    F = max(fit + p.border_front, 1e-6)
    B = max(fit + p.border_back, 1e-6)
    return W, O, F, B, fit


def _border_section(p, W, O, F, B):
    """Cross-section polygon as (s, h): s grows inward from the reference edge, h along the normal."""
    prof = p.border_profile
    r = max(0.0, p.border_bevel)
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
    else:                                                  # LIP: tall outer rim, lower inner step
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


def _sweep_loop(co, nrm, inw_edge, sec):
    """Sweep the section around a closed loop with mitred corners.

    co[k] / nrm[k]: loop vertex and its normal; inw_edge[k]: unit vector pointing into the
    surface from edge k (vertex k -> k+1). Returns (vertex coords, quad index tuples).
    """
    n = len(co)
    m_ = len(sec)
    verts = []
    for k in range(n):
        a, b = inw_edge[k - 1], inw_edge[k]
        m = a + b
        if m.length < 1e-6:
            m = b.copy()
        if m.length > 1e-9:
            m = m.normalized()
        c = max(m.dot(b), 0.3)
        miter = m / c
        for s, h in sec:
            verts.append(co[k] + miter * s + nrm[k] * h)
    quads = []
    for k in range(n):
        k2 = (k + 1) % n
        for j in range(m_):
            j2 = (j + 1) % m_
            quads.append((k * m_ + j, k2 * m_ + j, k2 * m_ + j2, k * m_ + j2))
    return verts, quads


def build_border(p, rb):
    """Build the frame as its own bmesh from the flat reference surface `rb`."""
    rb.verts.ensure_lookup_table()
    rb.edges.ensure_lookup_table()
    rb.faces.ensure_lookup_table()
    rb.verts.index_update()
    rb.normal_update()
    bedges = [(e.verts[0].index, e.verts[1].index) for e in rb.edges if len(e.link_faces) == 1]
    if not bedges:
        return None
    xs = [v.co.x for v in rb.verts]
    ys = [v.co.y for v in rb.verts]
    zs = [v.co.z for v in rb.verts]
    ext = [max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)]
    mx = max(ext)
    nz = [e for e in ext if e > 1e-6 * max(mx, 1e-9)]
    small = min(nz) if nz else mx
    W, O, F, B, _fit = _border_dims(p, small)
    sec = _border_section(p, W, O, F, B)
    if len(sec) < 3:
        return None

    fb = bmesh.new()
    for ids, cl in _chains(bedges):
        if not cl or len(ids) < 3:
            continue
        P = [rb.verts[i] for i in ids]
        n = len(P)
        inw = []
        for k in range(n):
            a, b = P[k], P[(k + 1) % n]
            e = rb.edges.get((a, b))
            dk = b.co - a.co
            if e is None or not e.link_faces or dk.length < 1e-12:
                inw.append(Vector((0.0, 0.0, 0.0)))
                continue
            f = e.link_faces[0]
            iv = f.normal.cross(dk.normalized())
            if iv.length < 1e-9:
                inw.append(Vector((0.0, 0.0, 0.0)))
                continue
            iv.normalize()
            if iv.dot(f.calc_center_median() - (a.co + b.co) * 0.5) < 0:
                iv = -iv
            inw.append(iv)
        nrm = []
        for v in P:
            nv = v.normal.copy()
            nrm.append(nv if nv.length > 1e-9 else Vector((0.0, 0.0, 1.0)))
        verts, quads = _sweep_loop([v.co for v in P], nrm, inw, sec)
        fv = [fb.verts.new(c) for c in verts]
        for q in quads:
            try:
                fb.faces.new([fv[i] for i in q])
            except ValueError:
                pass
    if not fb.faces:
        fb.free()
        return None
    bmesh.ops.recalc_face_normals(fb, faces=fb.faces[:])
    return fb


# ---------------------------------------------------------------------------
#  Boolean cutter (clean bounding volume of the whole reference)
# ---------------------------------------------------------------------------

def _cut_pad(p):
    """How far the pleated surface can stray from the flat reference."""
    return p.pleat_depth + p.thickness + 1.5 * p.noise_strength + 1e-5


def _newell(vs):
    n = Vector((0.0, 0.0, 0.0))
    for i in range(len(vs)):
        a, b = vs[i], vs[(i + 1) % len(vs)]
        n += Vector((
            (a.y - b.y) * (a.z + b.z),
            (a.z - b.z) * (a.x + b.x),
            (a.x - b.x) * (a.y + b.y)))
    return n


def _frame_face(p, src):
    """Orthonormal frame (across-ridge, ridge, normal) of a face selection."""
    vs = [Vector(v) for v in src["verts"]]
    best, bn = -1.0, Vector((0.0, 0.0, 1.0))
    for f in src["faces"]:
        n = _newell([vs[i] for i in f])
        if n.length > best:
            best, bn = n.length, n
    n0 = bn.normalized() if bn.length > 1e-12 else Vector((0.0, 0.0, 1.0))
    ref = Vector((1.0, 0.0, 0.0))
    if abs(n0.dot(ref)) > 0.95:
        ref = Vector((0.0, 1.0, 0.0))
    u0 = (ref - n0 * ref.dot(n0)).normalized()
    v0 = n0.cross(u0)
    a = p.pleat_angle
    ua = u0 * math.cos(a) + v0 * math.sin(a)
    ra = n0.cross(ua)
    return [ua, ra, n0], vs, 2          # index of the "thickness" axis


def _frame_curve(p, src):
    d = _height_dir(p)
    H = max(p.height, 1e-5)
    pts = [Vector(x) for path in src["paths"] for x in path]
    longest, bl = None, -1.0
    for path, cl in zip(src["paths"], src["closed"]):
        pp = [Vector(x) for x in path]
        L = _polyline_length(pp, cl)
        if L > bl:
            bl, longest = L, pp
    t = longest[-1] - longest[0]
    t = t - d * t.dot(d)
    if t.length < 1e-6 * max(bl, 1e-9):
        t = Vector((1.0, 0.0, 0.0)) - d * d.x
        if t.length < 1e-6:
            t = Vector((0.0, 1.0, 0.0)) - d * d.y
    a1 = t.normalized()
    a3 = a1.cross(d)
    if a3.length < 1e-9:
        a3 = d.cross(Vector((1.0, 0.0, 0.0)))
    a3 = a3.normalized()
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
    """Axis ranges: flush with the reference in-plane (+ trim margin), padded across the pleats."""
    rng = []
    pad = _cut_pad(p)
    m = p.cutter_margin
    for i in range(3):
        cs = [q.dot(ax[i]) for q in pts]
        lo, hi = min(cs), max(cs)
        if extra_hi is not None and i == extra_hi[0]:
            hi += extra_hi[1]
        if i == thick_axis:
            lo, hi = lo - pad, hi + pad
        else:
            lo, hi = lo - m, hi + m
            if hi - lo < 1e-6:
                mid = (hi + lo) * 0.5
                lo, hi = mid - 5e-4, mid + 5e-4
        rng.append((lo, hi))
    return rng


def _make_keep(p, ax, rng):
    eps = 1e-6
    op = p.boolean_op

    def keep(pt):
        inside = True
        for i in range(3):
            c = pt.dot(ax[i])
            if c < rng[i][0] - eps or c > rng[i][1] + eps:
                inside = False
                break
        if op == 'INTERSECT':
            return inside
        if op == 'DIFFERENCE':
            return not inside
        return True
    return keep


def _slab(src_bm, pad):
    """Solid slab of half-thickness `pad` around a (possibly bent) reference surface."""
    src_bm.verts.index_update()
    src_bm.normal_update()
    out = bmesh.new()
    top, bot = {}, {}
    for v in src_bm.verts:
        n = v.normal.copy()
        if n.length < 1e-9:
            n = Vector((0.0, 0.0, 1.0))
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
    """Returns (bmesh of the cutter solid, keep(pt) test or None)."""
    cb = bmesh.new()
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
        cb.free()
        return out, None

    if p.mode == 'CURVE':
        ax, pts, tk, H = _frame_curve(p, src)
        rng = _box_ranges(p, ax, pts, tk, extra_hi=(1, H))
    else:
        ax, pts, tk = _frame_face(p, src)
        rng = _box_ranges(p, ax, pts, tk)
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
#  Hair / dust
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
    if count <= 0:
        return []
    keep = _KEEP[0]
    out = []
    for ti in rng.choices(range(len(tris)), weights=areas, k=count):
        t = tris[ti]
        a, b, c = t[0].vert.co, t[1].vert.co, t[2].vert.co
        r1 = math.sqrt(rng.random())
        r2 = rng.random()
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


def build_hair(fbm, surf, p):
    tris, areas, total = surf
    if not tris or total < 1e-12:
        return
    rng = random.Random(p.hair_seed)
    for pt, n in _scatter(tris, areas, total, p.hair_density, rng):
        length = max(p.hair_length * (1.0 + p.hair_length_var * rng.uniform(-1, 1)), 1e-6)
        dirv = (n + _rvec(rng) * p.hair_randomness).normalized()
        bend = _rvec(rng)
        bend = bend - dirv * bend.dot(dirv)
        bend = bend.normalized() * (p.hair_curl * length) if bend.length > 1e-6 else Vector((0, 0, 0))
        side = dirv.cross(_rvec(rng))
        if side.length < 1e-6:
            side = dirv.cross(Vector((1, 0, 0)))
        side.normalize()
        S = 3
        rows = []
        for i in range(S):
            tt = i / S
            pos = pt + dirv * (length * tt) + bend * (tt * tt)
            w = p.hair_width * 0.5 * (1.0 - tt)
            rows.append((fbm.verts.new(pos - side * w), fbm.verts.new(pos + side * w)))
        tip = fbm.verts.new(pt + dirv * length + bend)
        try:
            for i in range(S - 1):
                fbm.faces.new((rows[i][0], rows[i][1], rows[i + 1][1], rows[i + 1][0]))
            fbm.faces.new((rows[S - 1][0], rows[S - 1][1], tip))
        except ValueError:
            pass


def build_dust(fbm, surf, p):
    tris, areas, total = surf
    if not tris or total < 1e-12:
        return
    rng = random.Random(p.dust_seed)
    tet = [Vector(c) / math.sqrt(3.0) for c in ((1, 1, 1), (-1, -1, 1), (-1, 1, -1), (1, -1, -1))]
    for pt, n in _scatter(tris, areas, total, p.dust_density, rng):
        r = max(p.dust_size * (1.0 + p.dust_size_var * rng.uniform(-1, 1)), 1e-6) * 0.5
        rot = Euler((rng.uniform(0, 6.283), rng.uniform(0, 6.283), rng.uniform(0, 6.283)))
        vs = []
        for q in tet:
            q = q.copy()
            q.rotate(rot)
            vs.append(fbm.verts.new(pt + n * (r * 0.3) + q * r))
        for idx in ((0, 1, 2), (0, 3, 1), (0, 2, 3), (1, 3, 2)):
            try:
                fbm.faces.new([vs[i] for i in idx])
            except ValueError:
                pass
    bmesh.ops.recalc_face_normals(fbm, faces=fbm.faces[:])


# ---------------------------------------------------------------------------
#  Regeneration
# ---------------------------------------------------------------------------

CHILD_ATTRS = ("hair_obj", "dust_obj", "border_obj", "cutter_obj", "fuzz_obj")


def _remove_child(p, attr):
    ob = getattr(p, attr)
    if ob:
        me = ob.data
        bpy.data.objects.remove(ob, do_unlink=True)
        if me and me.users == 0:
            bpy.data.meshes.remove(me)


def _write_child(obj, p, attr, suffix, cbm, mat):
    ob = getattr(p, attr)
    if ob is None or ob.name not in bpy.data.objects:
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


def sync_boolean(obj, p):
    mod = obj.modifiers.get(BOOL_NAME)
    target = p.boolean_obj if p.boolean_source == 'OBJECT' else p.cutter_obj
    if (p.use_boolean and target is not None and target != obj
            and target.name in bpy.data.objects and target.type == 'MESH'):
        if mod is None:
            mod = obj.modifiers.new(BOOL_NAME, 'BOOLEAN')
        mod.operation = p.boolean_op
        mod.solver = p.boolean_solver
        try:
            mod.operand_type = 'OBJECT'
        except Exception:
            pass
        mod.object = target
    elif mod is not None:
        obj.modifiers.remove(mod)


def regenerate(obj):
    p = obj.filter_maker
    if not p.is_filter or obj.mode != 'OBJECT':
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
        if p.thickness > 1e-7 and bm.faces:
            bmesh.ops.solidify(bm, geom=list(bm.faces), thickness=p.thickness)
        bm.normal_update()
        apply_noise(bm, p)
        bm.normal_update()

        # boolean cutter (a clean volume of the whole reference, not the folded mesh)
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

        if p.fuzz_obj:                      # leftover from v3.0
            _remove_child(p, "fuzz_obj")
        surf = _surface_samples(bm) if (p.use_hair or p.use_dust) and bm.faces else ([], [], 0.0)
        _KEEP[0] = keep
        try:
            _layer(obj, p, surf, p.use_hair, "hair_obj", "_Hair", build_hair, p.hair_material)
            _layer(obj, p, surf, p.use_dust, "dust_obj", "_Dust", build_dust, p.dust_material)
        finally:
            _KEEP[0] = None

        if rb is not None and len(rb.faces):
            fb = build_border(p, rb)
            if fb is not None:
                try:
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
        if hasattr(me, "use_auto_smooth"):            # Blender < 4.1
            try:
                me.use_auto_smooth = bool(p.auto_smooth)
                if p.auto_smooth:
                    me.auto_smooth_angle = p.auto_smooth_angle
            except Exception:
                pass
        me.update()
        sync_boolean(obj, p)
    except Exception as e:
        traceback.print_exc()
        p.last_error = str(e)
    finally:
        if bm is not None:
            bm.free()
        if rb is not None:
            rb.free()
        redraw_all()


def _upd(self, context):
    if _busy[0] or not self.is_filter or not self.live:
        return
    ob = self.id_data
    if isinstance(ob, bpy.types.Object):
        regenerate(ob)


def _upd_bool(self, context):
    if _busy[0] or not self.is_filter:
        return
    ob = self.id_data
    if isinstance(ob, bpy.types.Object):
        regenerate(ob)


def _upd_cutter_vis(self, context):
    _style_cutter(self)


def _poll_mesh(self, obj):
    return obj.type == 'MESH' and obj != self.id_data and not obj.filter_maker.is_filter


# ---------------------------------------------------------------------------
#  Properties
# ---------------------------------------------------------------------------

def _len(name, default, desc, mn=0.0):
    return FloatProperty(name=name, default=default, min=mn, description=desc,
                         subtype='DISTANCE', unit='LENGTH', precision=4, update=_upd)


class FilterProps(PropertyGroup):
    is_filter: BoolProperty(default=False)
    mode: EnumProperty(items=[('CURVE', "Curve / Edges", ""), ('FACE', "Faces", "")], default='CURVE')
    src: StringProperty(options={'HIDDEN'})
    live: BoolProperty(name="Live Update", default=True,
                       description="Regenerate while dragging sliders (turn off for heavy filters)")
    last_error: StringProperty(options={'HIDDEN'})
    source_obj: PointerProperty(type=bpy.types.Object)

    # ---- pleats
    thickness: _len("Thickness", 0.004, "Thickness of the filter media")
    pleat_depth: _len("Pleat Depth", 0.025, "Distance from the centre line to each ridge / valley")
    pleat_density: FloatProperty(name="Pleat Density", default=20.0, min=0.05, soft_max=200.0,
                                 description="Pleats (one ridge + one valley) per unit length", update=_upd)
    pleat_phase: FloatProperty(name="Pleat Offset", default=0.0, min=0.0, max=1.0,
                               description="Slide the pleat pattern along the filter (fraction of one pleat)",
                               update=_upd)
    detail: FloatProperty(name="Surface Detail", default=20.0, min=0.05, soft_max=200.0,
                          description="Cuts per unit length (more detail = smoother noise, heavier mesh)",
                          update=_upd)
    height: _len("Height", 1.0, "Extrusion height that fills the space", 0.001)
    height_dir: FloatVectorProperty(name="Height Direction", size=3, default=(0.0, 0.0, 1.0),
                                    subtype='DIRECTION', update=_upd)
    pleat_angle: FloatProperty(name="Pleat Angle", default=0.0, subtype='ANGLE', unit='ROTATION',
                               description="Rotate the pleat direction on the faces", update=_upd)
    mesh_type: EnumProperty(name="Topology", items=[
        ('QUADS', "Quads", "Quad topology (only n-gons at cut edges are split)"),
        ('TRIS', "Triangles", "Fully triangulated mesh")], default='QUADS', update=_upd)

    # ---- bevel
    bevel_ridge: FloatProperty(name="Ridge Bevel", default=0.4, min=0.0, max=1.0,
                               description="Bevel width on every ridge (outer fold). 0 = sharp", update=_upd)
    bevel_valley: FloatProperty(name="Valley Bevel", default=0.4, min=0.0, max=1.0,
                                description="Bevel width on every valley (inner fold). 0 = sharp", update=_upd)
    bevel_cuts: IntProperty(name="Bevel Cuts", default=3, min=1, max=16,
                            description="Segments in each bevel (1 = flat chamfer, more = rounder)",
                            update=_upd)

    # ---- shading
    smooth: BoolProperty(name="Smooth Shading", default=False, update=_upd)
    auto_smooth: BoolProperty(name="Auto Smooth", default=True,
                              description="Smooth shading, but keep edges sharper than the angle", update=_upd)
    auto_smooth_angle: FloatProperty(name="Angle", default=math.radians(35.0), min=0.0, max=math.pi,
                                     subtype='ANGLE', unit='ROTATION', update=_upd)

    # ---- wear (Clouds texture displace)
    noise_strength: _len("Strength", 0.04, "Displace strength")
    noise_scale: _len("Size", 0.25, "Clouds noise size (same as the texture's Size)", 0.0001)
    noise_depth: IntProperty(name="Depth", default=2, min=0, max=10, update=_upd)
    noise_type: EnumProperty(name="Noise", items=[('SOFT', "Soft", ""), ('HARD', "Hard", "")],
                             default='SOFT', update=_upd)
    noise_basis: EnumProperty(name="Basis", items=[
        ('BLENDER', "Blender Original", ""), ('PERLIN_NEW', "Improved Perlin", ""),
        ('PERLIN_ORIGINAL', "Original Perlin", ""), ('VORONOI_F1', "Voronoi F1", ""),
        ('VORONOI_F2F1', "Voronoi F2-F1", ""), ('VORONOI_CRACKLE', "Voronoi Crackle", ""),
        ('CELLNOISE', "Cell Noise", "")], default='BLENDER', update=_upd)
    noise_direction: EnumProperty(name="Direction", items=[
        ('XYZ', "RGB to XYZ", "Displace in all directions, like the Displace modifier"),
        ('NORMAL', "Normal", "Displace along the surface normals")], default='XYZ', update=_upd)
    noise_seed: IntProperty(name="Seed", default=0, min=0, update=_upd)

    # ---- border
    use_border: BoolProperty(name="Border", default=False, update=_upd)
    border_width: _len("Width", 0.03, "How far the frame reaches inward from the reference edge", 0.0001)
    border_overhang: _len("Overhang", 0.0, "How far the frame grows outward past the reference edge, in every direction")
    border_profile: EnumProperty(name="Profile", items=[
        ('FLAT', "Flat", "Plain rectangular frame"),
        ('CHAMFER', "Chamfer", "Flat frame with angled outer edges"),
        ('ROUND', "Round", "Rounded outer edges"),
        ('LIP', "Lip", "Tall outer rim with a lower inner step")], default='FLAT', update=_upd)
    border_bevel: _len("Bevel Size", 0.01, "Chamfer / round radius, or lip step height")
    border_cuts: IntProperty(name="Round Cuts", default=3, min=1, max=12,
                             description="Segments in each rounded corner", update=_upd)
    border_fit: BoolProperty(name="Fit To Pleats", default=True,
                             description="Automatically cover the pleat height (depth + thickness + noise)",
                             update=_upd)
    border_front: _len("Front Extend", 0.01, "Extra frame thickness on the front (normal side)")
    border_back: _len("Back Extend", 0.01, "Extra frame thickness on the back")

    # ---- hair
    use_hair: BoolProperty(name="Hair", default=True, update=_upd)
    hair_density: FloatProperty(name="Density", default=2000.0, min=0.0, soft_max=100000.0,
                                description="Strands per unit area (capped at 100k)", update=_upd)
    hair_length: _len("Length", 0.02, "Strand length")
    hair_width: _len("Width", 0.0005, "Strand base width")
    hair_length_var: FloatProperty(name="Length Variation", default=0.5, min=0.0, max=1.0, update=_upd)
    hair_randomness: FloatProperty(name="Direction Randomness", default=0.6, min=0.0, soft_max=3.0,
                                   update=_upd)
    hair_curl: FloatProperty(name="Curl", default=0.3, min=0.0, soft_max=2.0, update=_upd)
    hair_seed: IntProperty(name="Seed", default=0, min=0, update=_upd)

    # ---- dust
    use_dust: BoolProperty(name="Dust", default=True, update=_upd)
    dust_density: FloatProperty(name="Density", default=3000.0, min=0.0, soft_max=100000.0,
                                description="Specks per unit area (capped at 100k)", update=_upd)
    dust_size: _len("Size", 0.004, "Speck size")
    dust_size_var: FloatProperty(name="Size Variation", default=0.6, min=0.0, max=1.0, update=_upd)
    dust_seed: IntProperty(name="Seed", default=0, min=0, update=_upd)

    # ---- boolean
    use_boolean: BoolProperty(name="Boolean", default=False, update=_upd_bool)
    boolean_source: EnumProperty(name="Cutter", items=[
        ('BOX', "Bounding Box", "A clean box around the whole reference selection"),
        ('ENVELOPE', "Envelope", "The reference shape itself, thickened to cover the pleats"),
        ('OBJECT', "Object", "Any mesh object you pick")], default='BOX', update=_upd_bool)
    boolean_obj: PointerProperty(name="Object", type=bpy.types.Object, poll=_poll_mesh, update=_upd_bool)
    boolean_op: EnumProperty(name="Operation", items=[
        ('INTERSECT', "Intersect", "Keep only the part of the filter inside the cutter"),
        ('DIFFERENCE', "Difference", "Cut the cutter out of the filter"),
        ('UNION', "Union", "Merge with the cutter")], default='INTERSECT', update=_upd_bool)
    boolean_solver: EnumProperty(name="Solver", items=[
        ('EXACT', "Exact", "Slower, handles overlaps"), ('FAST', "Fast", "Quick preview")],
        default='EXACT', update=_upd_bool)
    cutter_margin: FloatProperty(name="Trim Margin", default=0.0, soft_min=-1.0, soft_max=1.0,
                                 subtype='DISTANCE', unit='LENGTH', precision=4,
                                 description="Grow (+) or shrink (-) the cutter in-plane. 0 = flush with the reference edges",
                                 update=_upd_bool)
    show_cutter: BoolProperty(name="Show Cutter", default=False, update=_upd_cutter_vis,
                              description="Show the cutter volume in the viewport")

    # ---- materials / children
    filter_material: PointerProperty(name="Filter Material", type=bpy.types.Material, update=_upd)
    border_material: PointerProperty(name="Border Material", type=bpy.types.Material, update=_upd)
    hair_material: PointerProperty(name="Hair Material", type=bpy.types.Material, update=_upd)
    dust_material: PointerProperty(name="Dust Material", type=bpy.types.Material, update=_upd)
    hair_obj: PointerProperty(type=bpy.types.Object)
    dust_obj: PointerProperty(type=bpy.types.Object)
    border_obj: PointerProperty(type=bpy.types.Object)
    cutter_obj: PointerProperty(type=bpy.types.Object)
    fuzz_obj: PointerProperty(type=bpy.types.Object)      # legacy (v3.0)


# ---------------------------------------------------------------------------
#  Presets
# ---------------------------------------------------------------------------

PRESET_SUBDIR = "filter_maker_pro"

# values are stored relative to the size of the source so a preset fits any filter
LEN_PROPS = {"thickness", "pleat_depth", "height", "noise_strength", "noise_scale", "border_width",
             "border_overhang", "border_bevel", "border_front", "border_back", "hair_length",
             "hair_width", "dust_size", "cutter_margin"}
INV_LEN = {"pleat_density", "detail"}
INV_AREA = {"hair_density", "dust_density"}
VEC_PROPS = {"height_dir"}

DEFAULTS_NORM = {
    "thickness": 0.004, "pleat_depth": 0.025, "pleat_density": 20.0, "pleat_phase": 0.0,
    "detail": 20.0, "height": 1.0, "height_dir": [0.0, 0.0, 1.0], "pleat_angle": 0.0,
    "mesh_type": "QUADS",
    "bevel_ridge": 0.4, "bevel_valley": 0.4, "bevel_cuts": 3,
    "smooth": False, "auto_smooth": True, "auto_smooth_angle": math.radians(35.0),
    "noise_strength": 0.04, "noise_scale": 0.25, "noise_depth": 2, "noise_type": "SOFT",
    "noise_basis": "BLENDER", "noise_direction": "XYZ", "noise_seed": 0,
    "use_border": False, "border_width": 0.03, "border_overhang": 0.0, "border_profile": "FLAT",
    "border_bevel": 0.01, "border_cuts": 3, "border_fit": True, "border_front": 0.01,
    "border_back": 0.01,
    "use_hair": True, "hair_density": 2000.0, "hair_length": 0.02, "hair_width": 0.0005,
    "hair_length_var": 0.5, "hair_randomness": 0.6, "hair_curl": 0.3, "hair_seed": 0,
    "use_dust": True, "dust_density": 3000.0, "dust_size": 0.004, "dust_size_var": 0.6,
    "dust_seed": 0,
    "use_boolean": False, "boolean_source": "BOX", "boolean_op": "INTERSECT",
    "boolean_solver": "EXACT", "cutter_margin": 0.0,
}
PRESET_KEYS = tuple(DEFAULTS_NORM)


def _mk(**kw):
    d = dict(DEFAULTS_NORM)
    d.update(kw)
    return d


BUILTIN_PRESETS = {
    "Standard Pleated": _mk(),
    "Clean (no wear)": _mk(noise_strength=0.0, use_hair=False, use_dust=False),
    "HEPA Fine Pleat": _mk(pleat_density=34.0, pleat_depth=0.018, thickness=0.003, bevel_ridge=0.25,
                           bevel_valley=0.25, bevel_cuts=2, noise_strength=0.012, use_hair=False,
                           dust_density=1500.0),
    "Coarse Furnace": _mk(pleat_density=9.0, pleat_depth=0.045, thickness=0.007, bevel_ridge=0.6,
                          bevel_valley=0.5, bevel_cuts=4, noise_strength=0.035, noise_scale=0.3,
                          hair_density=1500.0),
    "Sharp Paper Fold": _mk(bevel_ridge=0.0, bevel_valley=0.0, bevel_cuts=1, thickness=0.002,
                            noise_strength=0.01, use_hair=False, use_dust=False),
    "Dusty Used": _mk(noise_strength=0.07, noise_scale=0.18, hair_density=4000.0, hair_length=0.03,
                      dust_density=14000.0, dust_size=0.005),
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
                v = v / sc
            elif k in INV_LEN:
                v = v * sc
            elif k in INV_AREA:
                v = v * sc * sc
            v = float(v)
        d[k] = v
    return d


def _user_dir(create=False):
    try:
        return bpy.utils.user_resource('SCRIPTS', path="presets/" + PRESET_SUBDIR, create=create)
    except Exception:
        return ""


def user_presets():
    d = _user_dir()
    out = {}
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
        raise RuntimeError("Cannot locate the Blender presets folder")
    with open(os.path.join(d, safe + ".json"), "w", encoding="utf-8") as fh:
        json.dump({"name": safe, "version": 1, "values": values}, fh, indent=2)
    return safe


def preset_items(self, context):
    items = []
    for i, n in enumerate(BUILTIN_PRESETS):
        items.append(("B:" + n, n, "Built-in preset", 'PRESET', i))
    base = len(items)
    for j, n in enumerate(user_presets()):
        items.append(("U:" + n, n, "Your saved preset", 'USER', base + j))
    _preset_cache[:] = items
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
    if ob:
        apply_preset(ob, self.filter_maker_preset)


# ---------------------------------------------------------------------------
#  Operators
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
            if source.type == 'MESH':
                p.boolean_obj = source
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
    """Create a straight line at the 3D cursor and build a filter from it"""
    bl_idname = "filter.new_line"
    bl_label = "New Filter Line"
    bl_options = {'REGISTER', 'UNDO'}

    length: FloatProperty(name="Length", default=1.0, min=0.01, subtype='DISTANCE')

    def execute(self, context):
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        c = context.scene.cursor.location
        h = self.length / 2
        src = {"paths": [[_r((c.x - h, c.y, c.z)), _r((c.x + h, c.y, c.z))]], "closed": [False]}
        ob = create_filter(context, 'CURVE', src)
        if ob.filter_maker.last_error:
            self.report({'ERROR'}, ob.filter_maker.last_error)
        return {'FINISHED'}


class FILTER_OT_from_selection(Operator):
    """Build a filter from the active curve / mesh (uses the selection in Edit Mode)"""
    bl_idname = "filter.from_selection"
    bl_label = "Filter From Selection"
    bl_options = {'REGISTER', 'UNDO'}

    source: EnumProperty(name="Source", items=[('CURVE', "Curve / Edges", ""), ('FACE', "Faces", "")],
                         default='CURVE')
    hide_source: BoolProperty(name="Hide Source", default=True,
                              description="Hide the original curve/mesh (Object Mode only)")

    @classmethod
    def poll(cls, context):
        o = context.active_object
        return o is not None and o.type in {'MESH', 'CURVE'}

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
            else:
                if obj.type == 'CURVE':
                    src = paths_from_curve(obj)
                else:
                    src = paths_from_mesh(obj, selected_only=True)
                if not src["paths"]:
                    self.report({'ERROR'}, "No edges/curve found. In Edit Mode select some edges first.")
                    return {'CANCELLED'}
        except Exception as e:
            traceback.print_exc()
            self.report({'ERROR'}, "Could not read source: %s" % e)
            return {'CANCELLED'}

        was_edit = obj.mode == 'EDIT'
        if was_edit:
            bpy.ops.object.mode_set(mode='OBJECT')
        new = create_filter(context, self.source, src, source=obj)
        if self.hide_source and not was_edit:
            obj.hide_set(True)
        if new.filter_maker.last_error:
            self.report({'ERROR'}, new.filter_maker.last_error)
        return {'FINISHED'}


class FILTER_OT_regenerate(Operator):
    """Rebuild the filter now"""
    bl_idname = "filter.regenerate"
    bl_label = "Update"

    def execute(self, context):
        ob = active_filter(context)
        if ob:
            regenerate(ob)
        return {'FINISHED'}


class FILTER_OT_randomize(Operator):
    """New random seeds for noise, hair and dust"""
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
    """Use the original reference object as the boolean object"""
    bl_idname = "filter.use_reference"
    bl_label = "Use Reference"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        ob = active_filter(context)
        if ob:
            p = ob.filter_maker
            if p.source_obj and p.source_obj.type == 'MESH':
                p.boolean_obj = p.source_obj
            else:
                self.report({'WARNING'}, "The reference is not a mesh object.")
        return {'FINISHED'}


class FILTER_OT_preset_apply(Operator):
    """Apply the selected preset to this filter"""
    bl_idname = "filter.preset_apply"
    bl_label = "Apply Preset"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        ob = active_filter(context)
        if not ob:
            return {'CANCELLED'}
        if not apply_preset(ob, context.window_manager.filter_maker_preset):
            self.report({'ERROR'}, "Preset could not be loaded.")
            return {'CANCELLED'}
        return {'FINISHED'}


class FILTER_OT_preset_save(Operator):
    """Save the current settings as a new preset (or overwrite one with the same name)"""
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
            self.layout.label(text="A preset with this name exists - it will be overwritten", icon='ERROR')

    def execute(self, context):
        ob = active_filter(context)
        if not ob:
            return {'CANCELLED'}
        p = ob.filter_maker
        try:
            sc = _src_size(p.mode, json.loads(p.src))
            safe = save_preset(self.name, collect_values(p, sc))
        except Exception as e:
            traceback.print_exc()
            self.report({'ERROR'}, "Could not save preset: %s" % e)
            return {'CANCELLED'}
        preset_items(None, context)
        _set_selected_preset("U:" + safe)
        self.report({'INFO'}, "Saved preset '%s'" % safe)
        return {'FINISHED'}


class FILTER_OT_preset_update(Operator):
    """Overwrite the selected user preset with the current settings"""
    bl_idname = "filter.preset_update"
    bl_label = "Overwrite Preset"

    @classmethod
    def poll(cls, context):
        return (active_filter(context) is not None
                and context.window_manager.filter_maker_preset.startswith("U:"))

    def execute(self, context):
        ob = active_filter(context)
        p = ob.filter_maker
        name = context.window_manager.filter_maker_preset.partition(":")[2]
        try:
            sc = _src_size(p.mode, json.loads(p.src))
            save_preset(name, collect_values(p, sc))
        except Exception as e:
            traceback.print_exc()
            self.report({'ERROR'}, "Could not save preset: %s" % e)
            return {'CANCELLED'}
        self.report({'INFO'}, "Updated preset '%s'" % name)
        return {'FINISHED'}


class FILTER_OT_preset_delete(Operator):
    """Delete the selected user preset"""
    bl_idname = "filter.preset_delete"
    bl_label = "Delete Preset"

    @classmethod
    def poll(cls, context):
        return context.window_manager.filter_maker_preset.startswith("U:")

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        name = context.window_manager.filter_maker_preset.partition(":")[2]
        path = user_presets().get(name)
        if path:
            try:
                os.remove(path)
            except Exception as e:
                self.report({'ERROR'}, "Could not delete: %s" % e)
                return {'CANCELLED'}
        preset_items(None, context)
        _set_selected_preset("B:" + next(iter(BUILTIN_PRESETS)))
        return {'FINISHED'}


class FILTER_OT_finish(Operator):
    """Stop editing: keep the meshes (applies the boolean), drop the filter settings"""
    bl_idname = "filter.finish"
    bl_label = "Finish (Bake)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        ob = active_filter(context)
        if not ob:
            return {'CANCELLED'}
        p = ob.filter_maker
        if ob.modifiers.get(BOOL_NAME):
            try:
                with context.temp_override(object=ob, active_object=ob):
                    bpy.ops.object.modifier_apply(modifier=BOOL_NAME)
            except Exception as e:
                self.report({'WARNING'}, "Could not apply the boolean: %s" % e)
        _remove_child(p, "cutter_obj")
        p.is_filter = False
        redraw_all()
        self.report({'INFO'}, "Filter baked into normal meshes.")
        return {'FINISHED'}


class FILTER_OT_delete(Operator):
    """Delete the filter and its hair / dust / border / cutter"""
    bl_idname = "filter.delete"
    bl_label = "Delete Filter"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        ob = active_filter(context)
        if ob:
            p = ob.filter_maker
            for attr in CHILD_ATTRS:
                _remove_child(p, attr)
            me = ob.data
            bpy.data.objects.remove(ob, do_unlink=True)
            if me and me.users == 0:
                bpy.data.meshes.remove(me)
        redraw_all()
        return {'FINISHED'}


# ---------------------------------------------------------------------------
#  Panel graphics
# ---------------------------------------------------------------------------

class _Canvas:
    """Tiny software canvas for the preview graphics."""

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
        n = len(poly)
        if n < 3:
            return
        ys = [q[1] for q in poly]
        y0, y1 = max(0, int(math.floor(min(ys)))), min(self.H - 1, int(math.ceil(max(ys))))
        for y in range(y0, y1 + 1):
            yc = y + 0.5
            xs = []
            for i in range(n):
                (xa, ya), (xb, yb) = poly[i], poly[(i + 1) % n]
                if (ya <= yc < yb) or (yb <= yc < ya):
                    xs.append(xa + (yc - ya) * (xb - xa) / (yb - ya))
            xs.sort()
            for i in range(0, len(xs) - 1, 2):
                for x in range(int(math.ceil(xs[i] - 0.5)), int(math.floor(xs[i + 1] - 0.5)) + 1):
                    self.put(x, y, col)


_ICONS = [None]
_pcoll = [None]
_pv = {}
_pv_key = {}


def ic(name):
    """Icon name if it exists in this Blender version, else no icon (never breaks the panel)."""
    if _ICONS[0] is None:
        try:
            _ICONS[0] = set(bpy.types.UILayout.bl_rna.functions['prop'].parameters['icon'].enum_items.keys())
        except Exception:
            _ICONS[0] = set()
    return name if name in _ICONS[0] else 'NONE'


def _paint_profile(pv, rp, rv, cuts):
    """Pleat profile (two pleats): grey = sharp, white = bevelled, orange squares = the mesh cuts."""
    N = 160
    cv = _Canvas(N, N)
    table = _profile_table(rp, rv, cuts)
    ev = _make_eval(table)
    m = 12
    cy = N // 2
    A = N * 0.30

    def to_px(X, w):
        return m + (X + 0.5) / 2.0 * (N - 2 * m), cy + w * A

    for px in range(m, N - m, 4):                         # zero line
        cv.put(px, cy, (0.3, 0.3, 0.34, 1.0))
        cv.put(px + 1, cy, (0.3, 0.3, 0.34, 1.0))

    ghost = _make_eval(_profile_table(0.0, 0.0, 1))       # sharp reference
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
    """Border cross-section: filled = frame, orange dashed = reference edge, teal = pleat reach."""
    N = 160
    cv = _Canvas(N, N)
    W, O, F, B, fit = _border_dims(p)
    sec = _border_section(p, W, O, F, B)
    span = max((W + O) * 1.5, (F + B) * 1.5, 1e-9)
    sc = 130.0 / span
    cs, ch = (W - O) / 2.0, (F - B) / 2.0

    def px(s, h):
        return N / 2 + (s - cs) * sc, N / 2 + (h - ch) * sc

    x0, y0 = px(0.0, 0.0)
    cv.dashed(x0, y0, N - 4, y0, (0.35, 0.35, 0.4, 1.0))                     # reference surface
    if fit > 0:
        for h in (fit, -fit):                                                 # pleat reach
            xa, ya = px(-O - 0.4 * span, h)
            cv.dashed(4, ya, N - 4, ya, (0.2, 0.62, 0.62, 1.0), 0.6, 3, 4)
    xe, ye0 = px(0.0, -(B + 0.35 * (F + B)))
    _, ye1 = px(0.0, F + 0.35 * (F + B))
    cv.dashed(xe, max(2, ye0), xe, min(N - 3, ye1), (1.0, 0.55, 0.12, 1.0), 0.8, 4, 3)   # reference edge

    poly = [px(s, h) for s, h in sec]
    cv.fill(poly, (0.34, 0.4, 0.55, 1.0))
    for i in range(len(poly)):
        a, b = poly[i], poly[(i + 1) % len(poly)]
        cv.line(a[0], a[1], b[0], b[1], (0.92, 0.94, 1.0, 1.0), 1.3)
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
           round(p.border_bevel, 6), int(p.border_cuts))
    if _pv_key.get("border") != key:
        try:
            _paint_border(pv, p)
            _pv_key["border"] = key
        except Exception:
            traceback.print_exc()
            return 0
    return pv.icon_id


# ---------------------------------------------------------------------------
#  Panels
# ---------------------------------------------------------------------------

def active_filter(context):
    try:
        o = context.active_object
        if o is None:
            return None
        fm = getattr(o, "filter_maker", None)
        if fm is not None and fm.is_filter:
            return o
        par = o.parent
        pfm = getattr(par, "filter_maker", None) if par else None
        if pfm is not None and pfm.is_filter:
            return par
    except Exception:
        pass
    return None


def _counts(ob):
    p = ob.filter_maker
    v = len(ob.data.vertices)
    f = len(ob.data.polygons)
    ev = ef = 0
    for attr in ("hair_obj", "dust_obj", "border_obj"):
        c = getattr(p, attr)
        if c is not None and c.type == 'MESH':
            ev += len(c.data.vertices)
            ef += len(c.data.polygons)
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
        col.operator("filter.new_line", icon=ic('IPO_LINEAR'))
        o = context.active_object
        if o and o.type in {'MESH', 'CURVE'}:
            col.operator("filter.from_selection", text="From Curve / Edges",
                         icon=ic('CURVE_DATA')).source = 'CURVE'
            if o.type == 'MESH':
                col.operator("filter.from_selection", text="From Faces",
                             icon=ic('FACESEL')).source = 'FACE'
        else:
            col.label(text="Select a curve or mesh as the source", icon=ic('INFO'))
        if o and o.mode == 'EDIT':
            col.label(text="Edit Mode: uses your selection", icon=ic('RESTRICT_SELECT_OFF'))

        ob = active_filter(context)
        if ob is None:
            return
        p = ob.filter_maker
        layout.separator()
        row = layout.row(align=True)
        row.prop(p, "live", text="Live", toggle=True, icon=ic('PLAY'))
        row.operator("filter.regenerate", text="", icon=ic('FILE_REFRESH'))
        row.operator("filter.randomize", text="Randomize", icon=ic('FORCE_VORTEX'))
        v, f, ev, ef = _counts(ob)
        box = layout.box()
        box.label(text="Verts %s   Faces %s" % (format(v, ","), format(f, ",")), icon=ic('MESH_DATA'))
        if ev:
            box.label(text="+ border/hair/dust: %s verts" % format(ev, ","), icon=ic('PARTICLES'))
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

    def P(self, context):
        self.layout.use_property_split = True
        self.layout.use_property_decorate = False
        return active_filter(context).filter_maker


class FILTER_PT_presets(FILTER_SubPanel, Panel):
    bl_label = "Presets"
    bl_idname = "FILTER_PT_presets"

    @safe_draw
    def draw_header(self, context):
        self.layout.label(text="", icon=ic('PRESET'))

    @safe_draw
    def draw(self, context):
        layout = self.layout
        wm = context.window_manager
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
    bl_label = "Pleats"
    bl_idname = "FILTER_PT_pleats"

    @safe_draw
    def draw_header(self, context):
        self.layout.label(text="", icon=ic('MOD_WIREFRAME'))

    @safe_draw
    def draw(self, context):
        p = self.P(context)
        c = self.layout.column(align=True)
        c.prop(p, "pleat_density", icon=ic('MOD_ARRAY'))
        c.prop(p, "pleat_depth", icon=ic('ARROW_LEFTRIGHT'))
        c.prop(p, "pleat_phase", icon=ic('FORWARD'))
        c.prop(p, "thickness", icon=ic('MOD_SOLIDIFY'))
        c.prop(p, "detail", icon=ic('MOD_SUBSURF'))
        c.separator()
        if p.mode == 'CURVE':
            c.prop(p, "height", icon=ic('EMPTY_SINGLE_ARROW'))
            c.prop(p, "height_dir", icon=ic('ORIENTATION_NORMAL'))
        else:
            c.prop(p, "pleat_angle", icon=ic('DRIVER_ROTATIONAL_DIFFERENCE'))
        c.separator()
        c.label(text="Topology", icon=ic('MESH_GRID'))
        c.prop(p, "mesh_type", expand=True)


class FILTER_PT_bevel(FILTER_SubPanel, Panel):
    bl_label = "Pleat Bevel"
    bl_idname = "FILTER_PT_bevel"

    @safe_draw
    def draw_header(self, context):
        self.layout.label(text="", icon=ic('MOD_BEVEL'))

    @safe_draw
    def draw(self, context):
        p = self.P(context)
        layout = self.layout
        icon_id = profile_icon(p)
        if icon_id:
            row = layout.row()
            row.alignment = 'CENTER'
            row.template_icon(icon_value=icon_id, scale=7.0)
            layout.label(text="white = result   orange = cuts   grey = sharp")
        c = layout.column(align=True)
        c.prop(p, "bevel_ridge", slider=True, icon=ic('TRIA_UP'))
        c.prop(p, "bevel_valley", slider=True, icon=ic('TRIA_DOWN'))
        c.prop(p, "bevel_cuts", icon=ic('MOD_EDGESPLIT'))


class FILTER_PT_shading(FILTER_SubPanel, Panel):
    bl_label = "Shading"
    bl_idname = "FILTER_PT_shading"

    @safe_draw
    def draw_header(self, context):
        self.layout.label(text="", icon=ic('MOD_SMOOTH'))

    @safe_draw
    def draw(self, context):
        p = self.P(context)
        c = self.layout.column(align=True)
        c.prop(p, "smooth", icon=ic('SHADING_SOLID'))
        c.prop(p, "auto_smooth", icon=ic('SHADING_RENDERED'))
        row = c.row(align=True)
        row.active = p.auto_smooth
        row.prop(p, "auto_smooth_angle", icon=ic('DRIVER_ROTATIONAL_DIFFERENCE'))


class FILTER_PT_wear(FILTER_SubPanel, Panel):
    bl_label = "Wear  (Clouds displace)"
    bl_idname = "FILTER_PT_wear"
    bl_options = {'DEFAULT_CLOSED'}

    @safe_draw
    def draw_header(self, context):
        self.layout.label(text="", icon=ic('MOD_NOISE'))

    @safe_draw
    def draw(self, context):
        p = self.P(context)
        c = self.layout.column(align=True)
        c.prop(p, "noise_strength", icon=ic('MOD_DISPLACE'))
        c.prop(p, "noise_scale", icon=ic('FULLSCREEN_ENTER'))
        c.prop(p, "noise_depth")
        c.prop(p, "noise_type")
        c.prop(p, "noise_basis")
        c.prop(p, "noise_direction")
        c.prop(p, "noise_seed", icon=ic('FILE_REFRESH'))


class FILTER_PT_border(FILTER_SubPanel, Panel):
    bl_label = "Border"
    bl_idname = "FILTER_PT_border"
    bl_options = {'DEFAULT_CLOSED'}

    @safe_draw
    def draw_header(self, context):
        p = active_filter(context).filter_maker
        self.layout.prop(p, "use_border", text="")

    @safe_draw
    def draw(self, context):
        p = self.P(context)
        layout = self.layout
        layout.active = p.use_border
        icon_id = border_icon(p)
        if icon_id:
            row = layout.row()
            row.alignment = 'CENTER'
            row.template_icon(icon_value=icon_id, scale=7.0)
            layout.label(text="orange = reference edge   teal = pleat reach")
        layout.prop(p, "border_profile", expand=True)
        c = layout.column(align=True)
        c.label(text="Inward / outward", icon=ic('FULLSCREEN_ENTER'))
        c.prop(p, "border_width", text="Width (inward)")
        c.prop(p, "border_overhang", text="Overhang (outward)")
        if p.border_profile != 'FLAT':
            c.prop(p, "border_bevel", text="Lip Step" if p.border_profile == 'LIP' else "Bevel Size")
            if p.border_profile == 'ROUND':
                c.prop(p, "border_cuts")
        c = layout.column(align=True)
        c.label(text="Front / back", icon=ic('ARROW_LEFTRIGHT'))
        c.prop(p, "border_fit", icon=ic('SNAP_ON'))
        c.prop(p, "border_front")
        c.prop(p, "border_back")


class FILTER_PT_hair(FILTER_SubPanel, Panel):
    bl_label = "Hair"
    bl_idname = "FILTER_PT_hair"
    bl_options = {'DEFAULT_CLOSED'}

    @safe_draw
    def draw_header(self, context):
        p = active_filter(context).filter_maker
        self.layout.prop(p, "use_hair", text="")

    @safe_draw
    def draw(self, context):
        p = self.P(context)
        self.layout.active = p.use_hair
        c = self.layout.column(align=True)
        c.prop(p, "hair_density", icon=ic('STRANDS'))
        c.prop(p, "hair_length")
        c.prop(p, "hair_width")
        c.prop(p, "hair_length_var")
        c.prop(p, "hair_randomness")
        c.prop(p, "hair_curl")
        c.prop(p, "hair_seed")


class FILTER_PT_dust(FILTER_SubPanel, Panel):
    bl_label = "Dust"
    bl_idname = "FILTER_PT_dust"
    bl_options = {'DEFAULT_CLOSED'}

    @safe_draw
    def draw_header(self, context):
        p = active_filter(context).filter_maker
        self.layout.prop(p, "use_dust", text="")

    @safe_draw
    def draw(self, context):
        p = self.P(context)
        self.layout.active = p.use_dust
        c = self.layout.column(align=True)
        c.prop(p, "dust_density", icon=ic('PARTICLES'))
        c.prop(p, "dust_size")
        c.prop(p, "dust_size_var")
        c.prop(p, "dust_seed")


class FILTER_PT_boolean(FILTER_SubPanel, Panel):
    bl_label = "Boolean"
    bl_idname = "FILTER_PT_boolean"
    bl_options = {'DEFAULT_CLOSED'}

    @safe_draw
    def draw_header(self, context):
        p = active_filter(context).filter_maker
        self.layout.prop(p, "use_boolean", text="")

    @safe_draw
    def draw(self, context):
        p = self.P(context)
        layout = self.layout
        layout.active = p.use_boolean
        layout.label(text="Cutter", icon=ic('MOD_BOOLEAN'))
        layout.prop(p, "boolean_source", expand=True)
        c = layout.column(align=True)
        if p.boolean_source == 'OBJECT':
            row = c.row(align=True)
            row.prop(p, "boolean_obj", text="")
            row.operator("filter.use_reference", text="", icon=ic('EYEDROPPER'))
            if p.boolean_obj is None:
                c.label(text="Pick an object (or use the reference)", icon=ic('ERROR'))
        else:
            c.prop(p, "cutter_margin")
            c.prop(p, "show_cutter", icon=ic('HIDE_OFF'))
            if p.boolean_source == 'BOX':
                c.label(text="Box around the whole selection, flush at its edges", icon=ic('INFO'))
            else:
                c.label(text="Reference shape thickened to cover the pleats", icon=ic('INFO'))
        c = layout.column(align=True)
        c.prop(p, "boolean_op", text="Operation")
        c.prop(p, "boolean_solver", text="Solver")
        layout.label(text="Baked on Finish. Border is not cut.", icon=ic('INFO'))


class FILTER_PT_materials(FILTER_SubPanel, Panel):
    bl_label = "Materials"
    bl_idname = "FILTER_PT_materials"
    bl_options = {'DEFAULT_CLOSED'}

    @safe_draw
    def draw_header(self, context):
        self.layout.label(text="", icon=ic('MATERIAL'))

    @safe_draw
    def draw(self, context):
        p = self.P(context)
        layout = self.layout
        layout.template_ID(p, "filter_material", new="material.new")
        if p.use_border:
            layout.template_ID(p, "border_material", new="material.new")
        if p.use_hair:
            layout.template_ID(p, "hair_material", new="material.new")
        if p.use_dust:
            layout.template_ID(p, "dust_material", new="material.new")


class FILTER_PT_actions(FILTER_SubPanel, Panel):
    bl_label = "Finish"
    bl_idname = "FILTER_PT_actions"

    @safe_draw
    def draw_header(self, context):
        self.layout.label(text="", icon=ic('CHECKMARK'))

    @safe_draw
    def draw(self, context):
        row = self.layout.row(align=True)
        row.operator("filter.finish", icon=ic('CHECKMARK'))
        row.operator("filter.delete", text="", icon=ic('TRASH'))


classes = (FilterProps, FILTER_OT_new_line, FILTER_OT_from_selection, FILTER_OT_regenerate,
           FILTER_OT_randomize, FILTER_OT_use_reference, FILTER_OT_preset_apply,
           FILTER_OT_preset_save, FILTER_OT_preset_update, FILTER_OT_preset_delete,
           FILTER_OT_finish, FILTER_OT_delete,
           FILTER_PT_main, FILTER_PT_presets, FILTER_PT_pleats, FILTER_PT_bevel, FILTER_PT_shading,
           FILTER_PT_wear, FILTER_PT_border, FILTER_PT_hair, FILTER_PT_dust, FILTER_PT_boolean,
           FILTER_PT_materials, FILTER_PT_actions)


def _teardown():
    """Remove everything this add-on registers; safe to call on a half-registered or stale state."""
    if _pcoll[0] is not None:
        try:
            bpy.utils.previews.remove(_pcoll[0])
        except Exception:
            pass
    _pcoll[0] = None
    _pv.clear()
    for c in reversed(classes):                 # panels / operators first, data last
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass
    for owner, attr in ((bpy.types.WindowManager, "filter_maker_preset"),
                        (bpy.types.Object, "filter_maker")):
        try:
            delattr(owner, attr)
        except Exception:
            pass


def register():
    _teardown()                                 # clears a stale copy left by a reload / re-enable
    try:
        # the property group and the Object property must exist BEFORE any panel can draw or poll
        bpy.utils.register_class(FilterProps)
        bpy.types.Object.filter_maker = PointerProperty(type=FilterProps)
        bpy.types.WindowManager.filter_maker_preset = EnumProperty(
            name="Preset", items=preset_items, update=_preset_changed)
        for c in classes:
            if c is not FilterProps:
                bpy.utils.register_class(c)
    except Exception:
        traceback.print_exc()
        _teardown()                             # never leave panels behind without their property
        raise
    try:
        _pcoll[0] = bpy.utils.previews.new()
        _pv["profile"] = _pcoll[0].new("profile")
        _pv["border"] = _pcoll[0].new("border")
        _pv_key.clear()
    except Exception:
        traceback.print_exc()
        _pcoll[0] = None
        _pv.clear()


def unregister():
    _teardown()


if __name__ == "__main__":
    register()
