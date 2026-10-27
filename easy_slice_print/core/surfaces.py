# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Rafael Omodei and EasySlice Print contributors
"""Cut surfaces ("patches") and the kerf slab built from them.

A patch is a simple open mesh given as (verts, faces) in world space:
  * rect_patch    -> straight cut: one quad, sized independently on each axis
  * plane_patch   -> square special case of rect_patch
  * ribbon_patch  -> curved cut: the stroke, run through a spline, extruded along
                     the view direction
  * loop_patch    -> freehand cut: a closed loop drawn on the surface, spanned by a
                     relaxed membrane

A ribbon is built at a higher resolution than the handful of editable control points
behind it: the control points are put through a centripetal Catmull-Rom spline, so
the printed cut face is smooth instead of showing the facets of the polyline.

A loop is different. Its control points are every sample the stroke recorded, on the
surface, and the rim of the membrane runs through them and nowhere else: the segments
between them are only subdivided, never splined, so a corner drawn sharp stays sharp
and a point put on a crease stays on the crease. What is relaxed is the inside of the
membrane, until it is the smoothest surface that still ends exactly on that rim. The
clearance a boolean needs past the model is not the rim's business either - see
`with_skirt`, which continues the membrane outward past the rim for the cutter only.

`slab_from_patch` thickens a patch by the cut gap (kerf) into a closed solid
that is subtracted from the model with a boolean.
"""

import math
import bmesh
from mathutils import Vector
from mathutils.geometry import tessellate_polygon

SURFACE_DETAIL = 3  # spline samples generated per control point segment
RELAX_PASSES = 40  # membrane relaxation passes for a strongly non planar loop
MAX_LOOP_SAMPLES = 600  # rim vertices a filled loop is capped to, whatever the settings


# ----------------------------------------------------------------------------
# polyline helpers
# ----------------------------------------------------------------------------
def newell_normal(points):
    n = Vector((0.0, 0.0, 0.0))
    m = len(points)
    for i in range(m):
        a = points[i]
        b = points[(i + 1) % m]
        n.x += (a.y - b.y) * (a.z + b.z)
        n.y += (a.z - b.z) * (a.x + b.x)
        n.z += (a.x - b.x) * (a.y + b.y)
    if n.length < 1e-12:
        return Vector((0.0, 0.0, 1.0))
    return n.normalized()


def polyline_length(points, closed=False):
    total = 0.0
    m = len(points)
    rng = m if closed else m - 1
    for i in range(rng):
        total += (points[(i + 1) % m] - points[i]).length
    return total


def resample_polyline(points, count, closed=False):
    """Arc-length resample to `count` points (for closed loops the first point is not repeated)."""
    pts = [Vector(p) for p in points]
    if len(pts) < 2 or count < 2:
        return pts
    if closed:
        pts = pts + [pts[0]]
    seg = [(pts[i + 1] - pts[i]).length for i in range(len(pts) - 1)]
    total = sum(seg)
    if total <= 1e-12:
        return [pts[0].copy() for _ in range(count)]
    steps = count if closed else count - 1
    out = []
    target = 0.0
    i = 0
    acc = 0.0
    for k in range(steps + (0 if closed else 1)):
        target = total * k / steps
        while i < len(seg) - 1 and acc + seg[i] < target:
            acc += seg[i]
            i += 1
        t = 0.0 if seg[i] <= 1e-12 else (target - acc) / seg[i]
        t = max(0.0, min(1.0, t))
        out.append(pts[i].lerp(pts[i + 1], t))
    return out


def smooth_polyline(points, factor, closed=False, iterations=None):
    """Laplacian smoothing. factor in [0,1]. End points of open lines are pinned."""
    pts = [Vector(p) for p in points]
    m = len(pts)
    if m < 3 or factor <= 0.0:
        return pts
    if iterations is None:
        iterations = max(1, int(round(factor * 8)))
    f = min(1.0, factor)
    for _ in range(iterations):
        new = [p.copy() for p in pts]
        rng = range(m) if closed else range(1, m - 1)
        for i in rng:
            prev = pts[(i - 1) % m]
            nxt = pts[(i + 1) % m]
            avg = (prev + nxt) * 0.5
            new[i] = pts[i].lerp(avg, f * 0.5)
        pts = new
    return pts


def dedupe_polyline(points, min_dist):
    out = []
    for p in points:
        if not out or (p - out[-1]).length >= min_dist:
            out.append(Vector(p))
    return out


def subdivide_polyline(points, detail, closed=False):
    """`detail` - 1 extra points on every segment, on the straight line between its ends.

    The polyline itself does not move: every input point is in the output and nothing
    is added off the segments. It is the resolution a freehand rim gets - enough
    vertices for the fill and for the skirt to follow the surface between two drawn
    points, without a spline deciding where the line goes.
    """
    pts = [Vector(p) for p in points]
    m = len(pts)
    if m < 2 or detail <= 1:
        return pts
    out = []
    for i in range(m if closed else m - 1):
        a = pts[i]
        b = pts[(i + 1) % m]
        for k in range(detail):
            out.append(a.lerp(b, k / detail))
    if not closed:
        out.append(pts[-1])
    return out


def decimate_polyline(points, count):
    """At most `count` of the input points, evenly spaced, always the points themselves.

    Unlike `resample_polyline` nothing is interpolated: a loop cut down to the budget
    still runs through points that were drawn, only fewer of them.
    """
    pts = [Vector(p) for p in points]
    m = len(pts)
    if m <= count or count < 3:
        return pts
    return [pts[int(k * m / count)] for k in range(count)]


def plane_basis(normal):
    n = Vector(normal).normalized()
    helper = Vector((0.0, 0.0, 1.0)) if abs(n.z) < 0.9 else Vector((1.0, 0.0, 0.0))
    u = helper.cross(n).normalized()
    v = n.cross(u).normalized()
    return u, v


def _catmull_rom(p0, p1, p2, p3, steps, alpha=0.5):
    """Centripetal Catmull-Rom samples on the p1..p2 span (p2 itself not included).

    Centripetal (alpha=0.5) is the variant that never loops back on itself when two
    control points sit close together - which happens all the time in a hand drawn
    stroke resampled onto the model surface.
    """
    t0 = 0.0
    t1 = t0 + max((p1 - p0).length ** alpha, 1e-6)
    t2 = t1 + max((p2 - p1).length ** alpha, 1e-6)
    t3 = t2 + max((p3 - p2).length ** alpha, 1e-6)
    out = []
    for k in range(steps):
        t = t1 + (t2 - t1) * (k / steps)
        a1 = p0 * ((t1 - t) / (t1 - t0)) + p1 * ((t - t0) / (t1 - t0))
        a2 = p1 * ((t2 - t) / (t2 - t1)) + p2 * ((t - t1) / (t2 - t1))
        a3 = p2 * ((t3 - t) / (t3 - t2)) + p3 * ((t - t2) / (t3 - t2))
        b1 = a1 * ((t2 - t) / (t2 - t0)) + a2 * ((t - t0) / (t2 - t0))
        b2 = a2 * ((t3 - t) / (t3 - t1)) + a3 * ((t - t1) / (t3 - t1))
        out.append(b1 * ((t2 - t) / (t2 - t1)) + b2 * ((t - t1) / (t2 - t1)))
    return out


def spline_polyline(points, detail, closed=False):
    """`detail` spline samples per segment, passing through every input point.

    The control points stay exactly where the user put them; only the geometry
    between them stops being a straight line. Open lines get a mirrored end tangent,
    so the two tails leave the curve straight instead of curling.
    """
    pts = [Vector(p) for p in points]
    m = len(pts)
    if m < 3 or detail <= 1:
        return pts
    out = []
    for i in range(m if closed else m - 1):
        p1 = pts[i % m]
        p2 = pts[(i + 1) % m]
        if closed:
            p0, p3 = pts[(i - 1) % m], pts[(i + 2) % m]
        else:
            p0 = pts[i - 1] if i > 0 else p1 + (p1 - p2)
            p3 = pts[i + 2] if i + 2 < m else p2 + (p2 - p1)
        out.extend(_catmull_rom(p0, p1, p2, p3, detail))
    if not closed:
        out.append(pts[-1])
    return out


# ----------------------------------------------------------------------------
# patches
# ----------------------------------------------------------------------------
def rect_patch(center, normal, u_dir, half_u, v_range):
    """One quad on the plane (`center`, `normal`).

    Spans +-`half_u` along `u_dir` and `v_range` = (v0, v1) along `normal` x `u_dir`,
    both measured from `center`. Keeping the two axes independent is what lets a cut
    surface be exactly as wide as the stroke that was drawn while still reaching all
    the way through the model in depth.
    """
    n = Vector(normal).normalized()
    u = Vector(u_dir)
    u = u - n * u.dot(n)
    if u.length < 1e-9:
        u, _ = plane_basis(n)
    u.normalize()
    v = n.cross(u).normalized()
    c = Vector(center)
    a = c + v * v_range[0]
    b = c + v * v_range[1]
    verts = [a - u * half_u, a + u * half_u, b + u * half_u, b - u * half_u]
    return verts, [(0, 1, 2, 3)]


def plane_patch(center, normal, u_dir, size):
    """One quad of `size` x `size` centred on `center`, face normal == `normal`."""
    h = size * 0.5
    return rect_patch(center, normal, u_dir, h, (-h, h))


def ribbon_samples(points, extend, detail=SURFACE_DETAIL):
    """The columns a ribbon is built from: the splined stroke plus its two tails.

    Split out of `ribbon_patch` so a caller can measure the model under every column
    before deciding how deep that column should run.
    """
    pts = [Vector(p) for p in points]
    if len(pts) < 2:
        raise ValueError("ribbon needs at least 2 points")
    pts = spline_polyline(pts, detail)
    if extend > 0.0:
        e0 = pts[0] - pts[1]
        e1 = pts[-1] - pts[-2]
        if e0.length > 1e-9:
            pts.insert(0, pts[0] + e0.normalized() * extend)
        if e1.length > 1e-9:
            pts.append(pts[-1] + e1.normalized() * extend)
    return pts


def ribbon_patch(points, view_dir, depth, extend, depth_range=None, detail=SURFACE_DETAIL, depth_ranges=None):
    """Stroke `points` extruded along `view_dir`; both ends extended by `extend`.

    Without `depth_range` the ribbon spans -depth..+depth along the view
    direction; with `depth_range=(t0, t1)` it spans p + view*t0 .. p + view*t1.
    Face normals == tangent x view_dir (consistent along the ribbon).

    `depth_ranges` gives every column its own (t0, t1) instead - one per entry of
    `ribbon_samples(points, extend, detail)`. That is what lets the surface follow the
    model's silhouette instead of running to one flat depth for its whole length; a
    column whose range is empty (`None`) is dropped, so a stroke that overshoots the
    model does not drag the surface out with it.

    The stroke is splined to `detail` samples per segment before it is extruded, so
    the cut face is a smooth ruled surface rather than the flat facets of the control
    polyline. The extension tails are added afterwards and stay straight.
    """
    d = Vector(view_dir).normalized()
    pts = ribbon_samples(points, extend, detail)
    if depth_ranges is not None:
        if len(depth_ranges) != len(pts):
            raise ValueError("depth_ranges must have one entry per ribbon column")
        ranges = list(depth_ranges)
    else:
        ranges = [(-depth, depth) if depth_range is None else tuple(depth_range)] * len(pts)
    verts = []
    faces = []
    prev = None  # index of the previous column's first vertex, None across a gap
    for p, r in zip(pts, ranges):
        if r is None:
            prev = None
            continue
        here = len(verts)
        verts.append(p + d * r[0])
        verts.append(p + d * r[1])
        if prev is not None:
            faces.append((prev, here, here + 1, prev + 1))
        prev = here
    if not faces:
        raise ValueError("ribbon has no span over the model")
    return verts, faces


def loop_flatness(points):
    """Max distance from the loop's own best fit plane, relative to the loop radius."""
    pts = [Vector(p) for p in points]
    c = sum(pts, Vector((0.0, 0.0, 0.0))) / len(pts)
    radius = max((p - c).length for p in pts)
    if radius < 1e-12:
        return 0.0
    n = newell_normal(pts)
    return max(abs((p - c).dot(n)) for p in pts) / radius


def triangulate_face_indices(verts, f):
    """Triangulate polygon `f` (indices into `verts`), handling concave N-gons safely."""
    k = len(f)
    if k < 3:
        return []
    if k == 3:
        return [(f[0], f[1], f[2])]
    if k == 4:
        return [(f[0], f[1], f[2]), (f[0], f[2], f[3])]
    poly_pts = [Vector(verts[idx]) for idx in f]
    tris = tessellate_polygon([poly_pts])
    if not tris:
        return [(f[0], f[i], f[i + 1]) for i in range(1, k - 1)]
    return [(f[a], f[b], f[c]) for a, b, c in tris]


def _relax_cotan_like(verts, faces, fixed, passes):
    """Uniform + distance-weighted relaxation that prevents triangle folding in narrow tongues."""
    n = len(verts)
    if passes <= 0 or n <= fixed:
        return verts
    links = [set() for _ in range(n)]
    for f in faces:
        k = len(f)
        for i in range(k):
            a, b = f[i], f[(i + 1) % k]
            links[a].add(b)
            links[b].add(a)
    interior = [(i, tuple(links[i]), 1.0 / max(1, len(links[i]))) for i in range(fixed, n)]
    xs = [v.x for v in verts]
    ys = [v.y for v in verts]
    zs = [v.z for v in verts]
    for _ in range(passes):
        for i, nb, inv in interior:
            sx = sy = sz = 0.0
            for j in nb:
                sx += xs[j]
                sy += ys[j]
                sz += zs[j]
            xs[i] = sx * inv
            ys[i] = sy * inv
            zs[i] = sz * inv
    for i in range(fixed, n):
        verts[i] = Vector((xs[i], ys[i], zs[i]))
    return verts


_relax = _relax_cotan_like


def _arclength_smooth_scalar(pts_xy, values, radius_bu):
    """Discrete Heat Diffusion smoothing along the boundary curve.
    Tightly spaced vertices diffuse quickly without long segments dominating."""
    m = len(pts_xy)
    if m < 3 or radius_bu <= 1e-6:
        return list(values)
    seg_len = [(pts_xy[(i + 1) % m] - pts_xy[i]).length for i in range(m)]
    perimeter = max(sum(seg_len), 1e-6)
    avg_seg = perimeter / m

    # Diffusion step count proportional to smoothing radius over grid resolution
    steps = min(200, max(2, int(round((radius_bu / max(avg_seg, 1e-5)) * 2.2))))

    cur = list(values)
    for _ in range(steps):
        nxt = [0.0] * m
        for i in range(m):
            # Uniform kernel preventing clustering bias
            nxt[i] = 0.25 * cur[(i - 1) % m] + 0.50 * cur[i] + 0.25 * cur[(i + 1) % m]
        cur = nxt
    return cur


def _arclength_smooth_vec(pts_xy, vecs, radius_bu):
    """Physical smoothing of 3D vectors along the loop without locking on sharp corners."""
    xs = _arclength_smooth_scalar(pts_xy, [v.x for v in vecs], radius_bu)
    ys = _arclength_smooth_scalar(pts_xy, [v.y for v in vecs], radius_bu)
    zs = _arclength_smooth_scalar(pts_xy, [v.z for v in vecs], radius_bu)
    return [Vector((x, y, z)) for x, y, z in zip(xs, ys, zs)]


def _unfold_ring_2d(ring_xy, center_xy, u_axis, v_axis, strength=0.5):
    """Eliminates overlaps and pockets on non-convex sections of the ring
    by equalizing angular step of points relative to center and smoothing radius."""
    m = len(ring_xy)
    us = [(p - center_xy).dot(u_axis) for p in ring_xy]
    vs = [(p - center_xy).dot(v_axis) for p in ring_xy]
    rads = [math.hypot(u, v) for u, v in zip(us, vs)]
    avg_r = max(sum(rads) / m, 1e-5)

    # Smooth radii along the ring
    for _ in range(6):
        rads = [0.25 * rads[(i - 1) % m] + 0.5 * rads[i] + 0.25 * rads[(i + 1) % m] for i in range(m)]

    # Compute monotonic angular traversal to eliminate reverse loops and creases
    signed_area = sum(us[i] * vs[(i + 1) % m] - us[(i + 1) % m] * vs[i] for i in range(m))
    sign = 1.0 if signed_area >= 0.0 else -1.0

    seg_l = [math.hypot(us[(i + 1) % m] - us[i], vs[(i + 1) % m] - vs[i]) for i in range(m)]
    tot_l = max(sum(seg_l), 1e-6)
    cum_t = [0.0] * m
    acc = 0.0
    for i in range(m):
        cum_t[i] = acc / tot_l
        acc += seg_l[i]

    sin_s = cos_s = 0.0
    for i in range(m):
        ang = math.atan2(vs[i], us[i])
        diff = ang - sign * (2.0 * math.pi * cum_t[i])
        sin_s += math.sin(diff)
        cos_s += math.cos(diff)
    ang0 = math.atan2(sin_s, cos_s)

    out = []
    for i in range(m):
        ideal_ang = ang0 + sign * (2.0 * math.pi * cum_t[i])
        r = rads[i] * (1.0 - strength * 0.35) + avg_r * (strength * 0.35)
        ideal_pt = center_xy + u_axis * (r * math.cos(ideal_ang)) + v_axis * (r * math.sin(ideal_ang))
        out.append(ring_xy[i].lerp(ideal_pt, strength))
    return out


def membrane_fill(points, rings=None, passes=RELAX_PASSES):
    """Anisotropic harmonic membrane generator for 3D printing."""
    bnd = [Vector(p) for p in points]
    m = len(bnd)
    if m < 3:
        raise ValueError("loop needs at least 3 points")

    patch_n = newell_normal(bnd)
    u_axis, v_axis = plane_basis(patch_n)
    if rings is None:
        rings = max(22, min(36, int(round(m / 4.5))))

    # 1. Project onto the average cut plane (XY) and extract height (Z)
    raw_center = sum(bnd, Vector((0.0, 0.0, 0.0))) / m
    bnd_z = [(p - raw_center).dot(patch_n) for p in bnd]
    mean_z = sum(bnd_z) / m
    bnd_z = [z - mean_z for z in bnd_z]
    plane_center = raw_center + patch_n * mean_z
    bnd_xy = [p - patch_n * ((p - plane_center).dot(patch_n)) for p in bnd]

    avg_rad = max(sum((p - plane_center).length for p in bnd_xy) / m, 1e-4)
    eff_smooth = 0.65

    # 2. Build guaranteed NON-INTERSECTING rings in the XY plane
    ring_xy_list = [list(bnd_xy)]
    cur_xy = list(bnd_xy)

    for r in range(1, rings):
        t = r / rings
        cur_xy = _arclength_smooth_vec(bnd_xy, cur_xy, avg_rad * (0.08 + 0.30 * eff_smooth))
        unfold_w = min(1.0, (t ** 0.65) * (0.55 + 0.45 * eff_smooth))
        cur_xy = _unfold_ring_2d(cur_xy, plane_center, u_axis, v_axis, strength=unfold_w)
        shrink = t ** 1.05
        ring_xy_list.append([p.lerp(plane_center, shrink) for p in cur_xy])

    # 3. Anisotropic Z height calculation
    cur_z = list(bnd_z)
    ring_z_list = [list(bnd_z)]

    for r in range(1, rings):
        t = r / rings
        cur_z = _arclength_smooth_scalar(bnd_xy, cur_z, avg_rad * (0.10 + 0.40 * eff_smooth))
        dome_fade = (1.0 - t) ** 1.3
        ring_z_list.append([cur_z[i] * dome_fade for i in range(m)])

    # 4. Construct mesh
    verts = []
    ring_idx = []
    for r in range(rings):
        idx = []
        for i in range(m):
            idx.append(len(verts))
            verts.append(ring_xy_list[r][i] + patch_n * ring_z_list[r][i])
        ring_idx.append(idx)

    centre_idx = len(verts)
    verts.append(plane_center.copy())

    faces = []
    for r in range(rings - 1):
        outer, inner = ring_idx[r], ring_idx[r + 1]
        for i in range(m):
            nxt = (i + 1) % m
            faces.append([outer[i], outer[nxt], inner[nxt], inner[i]])
    last = ring_idx[-1]
    for i in range(m):
        faces.append([last[i], last[(i + 1) % m], centre_idx])

    # 5. Anisotropic 3D relaxation (Tangential-Dominant Smoothing)
    relax_iters = max(45, int(round(passes * 1.4)))
    for _ in range(relax_iters):
        nxt_verts = list(verts)
        for r in range(1, rings):
            t = r / rings
            stiffness = min(0.92, 0.65 + 0.30 * t)
            w_tan = 0.36
            w_rad = 0.14
            row = ring_idx[r]
            outer_row = ring_idx[r - 1]
            inner_row = ring_idx[r + 1] if r < rings - 1 else None

            for i in range(m):
                v_i = row[i]
                p_prev = verts[row[(i - 1) % m]]
                p_next = verts[row[(i + 1) % m]]
                p_out = verts[outer_row[i]]
                p_in = verts[centre_idx] if inner_row is None else verts[inner_row[i]]

                avg_v = (p_prev + p_next) * w_tan + (p_out + p_in) * w_rad
                nxt_verts[v_i] = verts[v_i].lerp(avg_v, stiffness)

        last_row = ring_idx[-1]
        nxt_verts[centre_idx] = sum((verts[j] for j in last_row), Vector((0.0, 0.0, 0.0))) / m
        verts = nxt_verts

    return verts, faces


def loop_boundary(points, detail=SURFACE_DETAIL):
    """Adaptive closed loop subdivision based on 3D segment length.
    Prevents elongated polygons from sparse brush strokes."""
    pts = [Vector(p) for p in points]
    if len(pts) < 3:
        raise ValueError("loop needs at least 3 points")

    pts = decimate_polyline(pts, MAX_LOOP_SAMPLES)
    m = len(pts)
    if m < 3:
        raise ValueError("loop is degenerate")

    total_len = sum((pts[(i + 1) % m] - pts[i]).length for i in range(m))
    target_count = min(MAX_LOOP_SAMPLES, max(m * max(1, detail), 96))
    max_seg_len = max(total_len / target_count, 1e-5)

    subdivided = []
    for i in range(m):
        p0 = pts[i]
        p1 = pts[(i + 1) % m]
        seg_len = (p1 - p0).length
        n_cuts = max(1, int(math.ceil(seg_len / max_seg_len)))
        for k in range(n_cuts):
            subdivided.append(p0.lerp(p1, k / n_cuts))

    pts = dedupe_polyline(subdivided, 1e-9)
    if len(pts) < 3:
        raise ValueError("loop is degenerate")
    return pts


def loop_patch(points, detail=SURFACE_DETAIL, passes=None, boundary=None):
    """Fill a closed loop with a smooth surface. Returns (verts, faces)."""
    pts = [Vector(p) for p in boundary] if boundary is not None else loop_boundary(points, detail)
    if len(pts) < 3:
        raise ValueError("loop needs at least 3 points")
    if passes is None:
        passes = max(8, int(round(RELAX_PASSES * min(1.0, 0.2 + loop_flatness(pts) * 4.0))))
    return membrane_fill(pts, passes=passes)


def rim_outward(verts, faces, rim):
    """Outward direction from patch at each rim point: strict linear continuation
    of the first collar ring (without kink or upward curl upon leaving the wall)."""
    n = len(verts)
    # If the mesh is ring-structured (where ring r=1 immediately follows rim with indices rim..2*rim),
    # take the exact outward projection vector: from inner point (rim + i) through rim point (i)!
    if n >= rim * 2:
        out = []
        for i in range(rim):
            d = Vector(verts[i]) - Vector(verts[rim + i])
            if d.length > 1e-12:
                out.append(d.normalized())
            else:
                out.append(Vector((0.0, 0.0, 0.0)))
        # Gently smooth directions along the rim to avoid self-crossing skirt
        for _ in range(4):
            out = [
                ((out[(i - 1) % rim] + out[i] * 2.0 + out[(i + 1) % rim]) * 0.25).normalized()
                for i in range(rim)
            ]
        return out

    # Fallback for non-ring patches
    links = [set() for _ in range(n)]
    normals = [Vector((0.0, 0.0, 0.0)) for _ in range(n)]
    for f in faces:
        k = len(f)
        fn = newell_normal([verts[i] for i in f]) * _poly_area(verts, f)
        for i in range(k):
            a, b = f[i], f[(i + 1) % k]
            links[a].add(b)
            links[b].add(a)
            normals[a] += fn
    fallback = newell_normal(verts[:rim])
    out = []
    for i in range(rim):
        p = verts[i]
        t = verts[(i + 1) % rim] - verts[(i - 1) % rim]
        nrm = normals[i] if normals[i].length > 1e-12 else fallback
        d = t.cross(nrm)
        if d.length < 1e-12:
            d = fallback.cross(t)
        if d.length < 1e-12:
            out.append(Vector((0.0, 0.0, 0.0)))
            continue
        d.normalize()
        inward = Vector((0.0, 0.0, 0.0))
        for j in links[i]:
            if j >= rim:
                inward += verts[j] - p
        if inward.length < 1e-12:
            inward = patch_center(verts) - p
        if d.dot(inward) > 0.0:
            d = -d
        out.append(d)
    return out


def unfold_skirt(rim, ring, slope=0.9, passes=60, local=False):
    """`ring` re-spaced along `rim` until no skirt quad can fold over its neighbour.

    Every skirt vertex is found on its own. At a transition between surface regions,
    two neighbours can be sent in different directions by more than the distance between them. The
    quads between them then overlap or turn inside out, the slab built on them is
    self intersecting, and the boolean leaves the halves joined along the fold.

    The offsets are smoothed along the rim (the rim itself never moves) until no two
    neighbouring offsets differ by more than `slope` of the rim segment between them,
    which is what keeps every quad a quad. `local` smooths only the vertices on either
    side of a segment that still breaks the rule, so the rest of the ring keeps the
    clearance it has. The skirt is the one part of the cutter that may be smoothed: it
    is outside the drawn line, and only the boolean ever sees it.
    """
    m = len(rim)
    if m < 3 or len(ring) != m:
        return [Vector(p) for p in ring]
    disp = [Vector(ring[i]) - Vector(rim[i]) for i in range(m)]
    seg = [(Vector(rim[(i + 1) % m]) - Vector(rim[i])).length for i in range(m)]
    floor = max(sum(seg) / m * 0.1, 1e-9)
    limit = [slope * max(l, floor) for l in seg]
    for _ in range(passes):
        bad = [i for i in range(m) if (disp[(i + 1) % m] - disp[i]).length > limit[i]]
        if not bad:
            break
        if local:
            touch = {j for i in bad for j in (i, (i + 1) % m)}
        else:
            touch = range(m)
        new = list(disp)
        for i in touch:
            new[i] = (disp[(i - 1) % m] + disp[(i + 1) % m]) * 0.25 + disp[i] * 0.5
        disp = new
    return [Vector(rim[i]) + disp[i] for i in range(m)]


def with_skirt(verts, faces, rim, ring):
    """The patch continued past its rim to `ring`, one vertex per rim vertex.

    This is the cutter a freehand loop hands the boolean. The membrane ends exactly
    on the drawn loop, which is on the model's surface - and a slab whose wall stands
    on the surface is the one thing an exact boolean cannot resolve: the two halves
    stay welded along it. The skirt carries the slab out into free air without moving
    the rim, so the cut face still ends on the line that was drawn and the boolean
    still gets a wall it can cut with. Returns a new (verts, faces).
    """
    if len(ring) != rim:
        raise ValueError("skirt needs one vertex per rim vertex")
    out_v = [Vector(v) for v in verts] + [Vector(p) for p in ring]
    base = len(verts)
    out_f = [tuple(f) for f in faces]
    for i in range(rim):
        j = (i + 1) % rim
        # winding opposite to the fill's (rim i -> i+1), so the skirt faces the same way
        out_f.append((j, i, base + i, base + j))
    return out_v, out_f


def patch_normal(verts, faces):
    """Area weighted average normal of a patch."""
    n = Vector((0.0, 0.0, 0.0))
    for f in faces:
        n += newell_normal([verts[i] for i in f]) * _poly_area(verts, f)
    if n.length < 1e-12:
        return Vector((0.0, 0.0, 1.0))
    return n.normalized()


def _poly_area(verts, f):
    pts = [verts[i] for i in f]
    total = Vector((0.0, 0.0, 0.0))
    for i in range(1, len(pts) - 1):
        total += (pts[i] - pts[0]).cross(pts[i + 1] - pts[0])
    return total.length * 0.5


def patch_area(verts, faces):
    """Total area of a patch. For a plane cut that is the real area of the printed face."""
    return sum(_poly_area(verts, f) for f in faces)


def patch_center(verts):
    c = Vector((0.0, 0.0, 0.0))
    for v in verts:
        c += v
    return c / max(1, len(verts))


def inflate_bmesh(mesh, offset):
    """Return a closed, manifold-friendly bmesh copy of `mesh` inflated outward by `offset`."""
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
    # Fill open boundary holes so Boolean Difference can process open shells.
    boundary_edges = [e for e in bm.edges if e.is_boundary]
    if boundary_edges:
        try:
            bmesh.ops.holes_fill(bm, edges=boundary_edges, sides=0)
        except Exception:
            pass
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.normal_update()
    if abs(offset) > 1e-7:
        # Pure vertex normal offset (no shell_factor spikes that cause self-intersections!)
        for v in bm.verts:
            v.co = v.co + v.normal * offset
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    return bm


# ----------------------------------------------------------------------------
# slab (kerf solid)
# ----------------------------------------------------------------------------
def slab_from_patch(verts, faces, thickness):
    """Closed manifold solid = patch offset +-thickness/2 along vertex normals."""
    t = max(thickness, 1e-5)
    src = bmesh.new()
    bverts = [src.verts.new(Vector(v)) for v in verts]
    for f in faces:
        try:
            src.faces.new([bverts[i] for i in f])
        except ValueError:
            pass
    bmesh.ops.recalc_face_normals(src, faces=src.faces)
    src.normal_update()

    # If the user supplied an already closed manifold volume, use it directly
    if src.faces and not any(e.is_boundary for e in src.edges):
        return src

    out = bmesh.new()
    top = []
    bot = []
    for v in src.verts:
        # Clamp shell factor so sharp 90-degree edited steps keep uniform thickness without spikes
        shell = min(max(v.calc_shell_factor(), 1.0), 2.5)
        offset = v.normal * (t * 0.5 * shell)
        top.append(out.verts.new(v.co + offset))
        bot.append(out.verts.new(v.co - offset))
    src.verts.index_update()
    for f in src.faces:
        idx = [v.index for v in f.verts]
        out.faces.new([top[i] for i in idx])
        out.faces.new([bot[i] for i in reversed(idx)])
    for f in src.faces:
        for loop in f.loops:
            if loop.edge.is_boundary:
                a = loop.vert.index
                b = loop.link_loop_next.vert.index
                out.faces.new([top[a], bot[a], bot[b], top[b]])
    src.free()
    bmesh.ops.remove_doubles(out, verts=out.verts, dist=1e-9)
    bmesh.ops.recalc_face_normals(out, faces=out.faces)
    return out
