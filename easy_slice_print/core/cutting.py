# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Rafael Omodei and EasySlice Print contributors
"""High level cut pipeline: split a mesh by one or two cut surfaces, add
connectors, optional remesh, create the result objects."""

import math
import time
from dataclasses import dataclass, field

import bpy
from mathutils import Matrix, Vector

from . import connectors, diagnosis, mesh_utils, surfaces


class CutError(Exception):
    """A cut that could not be made. `diagnosis` says where, when that could be found."""

    def __init__(self, message, where=None):
        super().__init__(message)
        self.diagnosis = where


@dataclass
class ContactSpec:
    verts: list
    faces: list
    add_pin: bool = True
    pin_matrix: Matrix | None = None  # world; +Z points into the socket part
    shape: str = 'CYLINDER'
    custom_obj: object = None
    # The connector mesh as the plan shows it (unit space, edit-mode changes included).
    # When set it is the shape that gets built; `shape`/`custom_obj` only say how to
    # make one from scratch, which is all Quick mode has.
    pin_mesh: object = None
    regions_skipped: int = 0  # regions the plane crosses that this surface leaves uncut
    # The surface the user sees, as (verts, faces), when `verts`/`faces` is a separate cutter
    # (a plane's quad, a curve's flat ribbon, a loop's skirted membrane). A failed cut is
    # diagnosed on the cutter but painted on this: it is the mesh on screen, and the one edited.
    preview: tuple | None = None

    def shown_patch(self):
        return self.preview if self.preview is not None else (self.verts, self.faces)


@dataclass
class CutSpec:
    contacts: list[ContactSpec] = field(default_factory=list)
    gap: float = 0.0  # BU
    clearance: float = 0.0  # BU
    tip_extra: float = 0.0  # BU (socket depth extra at the tip)
    pin_side: str = 'A'  # 'A' -> part on the + side of the surface carries the pin
    solver: str = 'AUTO'
    remesh: bool = False
    remesh_voxel: float = 0.0  # BU
    remesh_adaptivity: float = 0.0
    remesh_smooth: bool = False
    diagnose: bool = True  # on a cut that does not split the part, map where it stays joined


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def drain(generator):
    """Run a step generator to the end and return its value (synchronous callers)."""
    while True:
        try:
            next(generator)
        except StopIteration as stop:
            return stop.value


def side_sign(point, bvh):
    loc, nor, _idx, _dist = bvh.find_nearest(point)
    if loc is None:
        return 1
    return 1 if (point - loc).dot(nor) >= 0.0 else -1


def mesh_side(mesh, bvh, samples=600, floor=0.0):
    """Which side of the cut surface a separated piece came off. -> +1 or -1

    Classify the piece using vertices near the new cut face. Weight each vertex by its
    distance from the surface so distant geometry has less influence. `floor`, a
    fraction of the kerf, limits the influence of small Boolean artifacts near the
    surface.
    """
    verts = mesh.vertices
    n = len(verts)
    if n == 0:
        return 1
    step = max(1, n // samples)
    total = 0.0
    for i in range(0, n, step):
        p = verts[i].co
        loc, nor, _idx, dist = bvh.find_nearest(p)
        if loc is None:
            continue
        d = max(dist, floor)
        w = 1.0 / (d * d + 1e-6)
        total += w if (p - loc).dot(nor) >= 0.0 else -w
    if total == 0.0:
        return side_sign(mesh_utils.mesh_centroid(mesh), bvh)
    return 1 if total > 0.0 else -1


def still_joined_message(spec):
    """Why the boolean removed material and the part still came out in one piece.

    The plane may cross regions outside the drawn stroke. Those regions are left
    uncut, but can keep the resulting parts connected. Report how many regions were
    skipped so the user can extend the stroke to include them.
    """
    skipped = max((c.regions_skipped for c in spec.contacts), default=0)
    if skipped:
        many = skipped > 1
        return (
            "The cut removed material but the part is still in one piece: the cut plane also crosses "
            f"{skipped} other region{'s' if many else ''} of the model that your "
            f"line did not run across, and the two halves stay joined through {'them' if many else 'it'}. "
            f"Draw the cut line across {'those regions' if many else 'that region'} too."
        )
    return "The cut surface does not split this part in two. Make the cut cross the whole part."


def side_labels(centroid_a, centroid_b):
    d = centroid_a - centroid_b
    ax = max(range(3), key=lambda i: abs(d[i]))
    if ax == 2:
        return ("UPPER", "LOWER") if d.z >= 0 else ("LOWER", "UPPER")
    if ax == 0:
        return ("RIGHT", "LEFT") if d.x >= 0 else ("LEFT", "RIGHT")
    return ("BACK", "FRONT") if d.y >= 0 else ("FRONT", "BACK")


def protrude_direction(surface_normal, pin_side):
    """Surface normal points to side A. The pin protrudes into the socket part."""
    n = Vector(surface_normal).normalized()
    return -n if pin_side == 'A' else n


# ----------------------------------------------------------------------------
# pin placement estimate (cheap, ray based; used for previews AND builds)
# ----------------------------------------------------------------------------
def estimate_pin_frame(obj, depsgraph, patch_bvh, diag, surface_point=None, through_dir=None, center_hint=None):
    """-> (center, surface_normal, inscribed_diameter) in world space.

    Provide either `center_hint`, a point inside the part, or `surface_point`
    and `through_dir`, describing where the stroke touched the model and its
    direction through the part.
    The centre is refined with in-plane rays so it sits in the middle of the
    cross-section; the shortest ray gives the inscribed diameter.
    """
    if center_hint is not None:
        center = Vector(center_hint)
    else:
        d = Vector(through_dir).normalized()
        start = Vector(surface_point) + d * (diag * 1e-4)
        hit, loc, _n, _dist = mesh_utils.object_ray_cast(obj, start, d, depsgraph, max_dist=diag * 4.0)
        center = (Vector(surface_point) + loc) * 0.5 if hit else Vector(surface_point)
    loc_p, nor_p, _i, _d = patch_bvh.find_nearest(center)
    if loc_p is None:
        return center, Vector((0.0, 0.0, 1.0)), diag * 0.1
    center = loc_p
    normal = nor_p.normalized()
    inscribed = None
    for _ in range(2):
        u, v = surfaces.plane_basis(normal)
        hits = []
        for k in range(16):
            ang = 2.0 * math.pi * k / 16.0
            rd = (u * math.cos(ang) + v * math.sin(ang)).normalized()
            h, hl, _hn, _hd = mesh_utils.object_ray_cast(obj, center, rd, depsgraph, max_dist=diag * 2.0)
            if h:
                hits.append(hl)
        if len(hits) < 8:
            break
        mn = Vector((min(h.x for h in hits), min(h.y for h in hits), min(h.z for h in hits)))
        mx = Vector((max(h.x for h in hits), max(h.y for h in hits), max(h.z for h in hits)))
        new_center = (mn + mx) * 0.5
        loc_p, nor_p, _i, _d = patch_bvh.find_nearest(new_center)
        if loc_p is not None:
            center = loc_p
            normal = nor_p.normalized()
        inscribed = min((h - center).length for h in hits) * 2.0
    if inscribed is None or inscribed <= 1e-9:
        inscribed = diag * 0.1
    return center, normal, inscribed


# ----------------------------------------------------------------------------
# pipeline
# ----------------------------------------------------------------------------
def split_mesh_steps(context, mesh, spec):
    """Generator form of `split_mesh`.

    Yields a short label BEFORE each heavy step and returns (mesh_a, mesh_b).
    Every yield hands control back to Blender's event loop, which is what keeps
    the window answering the compositor's "are you alive?" pings during a cut.
    """
    work = mesh
    orphans = []  # intermediates to drop if the job is cancelled part way through
    total = len(spec.contacts)
    try:
        for i, c in enumerate(spec.contacts):
            if not c.verts or not c.faces:
                continue
            yield f"cutting surface {i + 1}/{total}" if total > 1 else "cutting surface"
            slab_bm = surfaces.slab_from_patch(c.verts, c.faces, spec.gap)
            slab = mesh_utils.bmesh_to_mesh(slab_bm, "_esp_slab")
            slab_bm.free()
            res = mesh_utils.boolean_mesh(context, work, slab, 'DIFFERENCE', spec.solver)
            mesh_utils.remove_mesh(slab)
            mesh_utils.remove_mesh(work)
            work = res
        if len(work.polygons) == 0:
            raise CutError(
                "Boolean failed (empty result). Check that the cut reaches the model and the mesh is closed.",
                diagnosis.nothing_cut(*spec.contacts[0].shown_patch()) if spec.diagnose else None,
            )
        yield "separating the parts"
        orphans = mesh_utils.separate_loose_meshes(context, work)
        work = None
        c0 = spec.contacts[0]
        bvh = mesh_utils.bvh_from_pydata(c0.verts, c0.faces)
        side_a, side_b = [], []
        for p in orphans:
            (side_a if mesh_side(p, bvh, floor=spec.gap * 0.25) > 0 else side_b).append(p)
        if not side_a or not side_b:
            found = None
            if spec.diagnose:
                yield "finding where the halves stay joined"
                found = diagnosis.diagnose(orphans, c0.verts, c0.faces, spec.gap, shown=c0.shown_patch())
            raise CutError(still_joined_message(spec), found)
        out = mesh_utils.join_meshes(side_a, "_esp_part_a"), mesh_utils.join_meshes(side_b, "_esp_part_b")
        orphans = []
        return out
    finally:
        mesh_utils.remove_mesh(work)
        for me in orphans:
            mesh_utils.remove_mesh(me)


def split_mesh(context, mesh, spec):
    """Consume `mesh`, return (mesh_a, mesh_b). Side A is the + side of contact 0."""
    return drain(split_mesh_steps(context, mesh, spec))


def apply_connectors_steps(context, mesh_a, mesh_b, spec):
    total = sum(1 for c in spec.contacts if c.add_pin and c.pin_matrix is not None)
    n = 0
    half = None  # first half finished; dropped if the job is cancelled between the two booleans
    try:
        for c in spec.contacts:
            if not c.add_pin or c.pin_matrix is None:
                continue
            n += 1
            yield f"adding connector {n}/{total}" if total > 1 else "adding the connector"
            pin = connectors.connector_mesh(c.shape, c.custom_obj, c.pin_matrix, "_esp_pin", unit_mesh=c.pin_mesh)
            socket = connectors.connector_mesh(
                c.shape,
                c.custom_obj,
                c.pin_matrix,
                "_esp_socket",
                radial_extra=spec.clearance,
                tip_extra=spec.tip_extra,
                unit_mesh=c.pin_mesh,
            )
            pin_target, socket_target = (mesh_a, mesh_b) if spec.pin_side == 'A' else (mesh_b, mesh_a)
            half = mesh_utils.boolean_mesh(context, pin_target, pin, 'UNION', spec.solver)
            yield f"carving socket {n}/{total}" if total > 1 else "carving the socket"
            carved = mesh_utils.boolean_mesh(context, socket_target, socket, 'DIFFERENCE', spec.solver)
            new_a, new_b = (half, carved) if spec.pin_side == 'A' else (carved, half)
            half = None
            for m in (pin, socket, mesh_a, mesh_b):
                mesh_utils.remove_mesh(m)
            mesh_a, mesh_b = new_a, new_b
        return mesh_a, mesh_b
    finally:
        mesh_utils.remove_mesh(half)


def apply_connectors(context, mesh_a, mesh_b, spec):
    return drain(apply_connectors_steps(context, mesh_a, mesh_b, spec))


def remesh_mesh(context, mesh, voxel, adaptivity, smooth):
    scene = context.scene
    obj = mesh_utils.new_temp_object(scene, mesh, "_esp_remesh")
    mod = obj.modifiers.new("esp_remesh", 'REMESH')
    mod.mode = 'VOXEL'
    mod.voxel_size = max(voxel, 1e-5)
    mod.adaptivity = adaptivity
    mod.use_smooth_shade = smooth
    context.view_layer.update()
    out = mesh_utils.evaluated_mesh_copy(context, obj, mesh.name + "_rm")
    mesh_utils.remove_object(obj)
    return out


def perform_cut_steps(context, target_obj, spec, names, out_collection):
    """Generator form of `perform_cut`: yields a label before every heavy step.

    A single boolean can take seconds; run through a modal driver (see `jobs.py`)
    so Blender gets back to its event loop between steps instead of looking hung.
    """
    t0 = time.time()
    yield "preparing the mesh"
    mesh = mesh_utils.world_mesh_copy(context, target_obj, "_esp_work")
    made = []
    try:
        mesh_a, mesh_b = yield from split_mesh_steps(context, mesh, spec)
        made = [mesh_a, mesh_b]
        mesh_a, mesh_b = yield from apply_connectors_steps(context, mesh_a, mesh_b, spec)
        made = [mesh_a, mesh_b]
        if spec.remesh and spec.remesh_voxel > 0.0:
            yield "remeshing part 1/2"
            mesh_a = remesh_mesh(context, mesh_a, spec.remesh_voxel, spec.remesh_adaptivity, spec.remesh_smooth)
            made = [mesh_a, mesh_b]
            yield "remeshing part 2/2"
            mesh_b = remesh_mesh(context, mesh_b, spec.remesh_voxel, spec.remesh_adaptivity, spec.remesh_smooth)
            made = [mesh_a, mesh_b]
        made = []
    finally:
        mesh_utils.cleanup_temp(context.scene)
        # cancelled or failed part way through: drop the half finished meshes
        for me in made:
            mesh_utils.remove_mesh(me)
    mats = [m for m in target_obj.data.materials] if target_obj.type == 'MESH' else []
    result = []
    for me, name, side in ((mesh_a, names[0], 'A'), (mesh_b, names[1], 'B')):
        me.name = name
        for m in mats:
            me.materials.append(m)
        obj = bpy.data.objects.new(name, me)
        obj["esp_part"] = True
        obj["esp_side"] = side
        # Preserve the source model name when cutting an already-sliced part.
        root_source = target_obj.get("esp_source") or target_obj.get("esp_stem") or target_obj.name
        obj["esp_source"] = root_source
        obj["esp_stem"] = root_source
        out_collection.objects.link(obj)
        result.append(obj)
    return result[0], result[1], time.time() - t0


def perform_cut(context, target_obj, spec, names, out_collection):
    """Cut `target_obj` and create the two result objects.

    names: (name_a, name_b). Returns (obj_a, obj_b, seconds).
    """
    return drain(perform_cut_steps(context, target_obj, spec, names, out_collection))


def _bounds_overlap(me_a, me_b, pad):
    """Quick bounding box overlap check between two meshes with a safety padding `pad`."""
    if len(me_a.vertices) == 0 or len(me_b.vertices) == 0:
        return False
    min_a, max_a = mesh_utils.mesh_bounds(me_a)
    min_b, max_b = mesh_utils.mesh_bounds(me_b)
    return (
        min_a.x - pad <= max_b.x
        and max_a.x + pad >= min_b.x
        and min_a.y - pad <= max_b.y
        and max_a.y + pad >= min_b.y
        and min_a.z - pad <= max_b.z
        and max_a.z + pad >= min_b.z
    )


def _clean_mesh_for_bool(mesh):
    """Cleans a mesh from duplicate vertices and degenerate edges and recalculates normals before booleans."""
    import bmesh

    bm = bmesh.new()
    bm.from_mesh(mesh)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-5)
    bmesh.ops.dissolve_degenerate(bm, dist=1e-6, edges=bm.edges)
    boundary_edges = [e for e in bm.edges if e.is_boundary]
    if boundary_edges:
        try:
            bmesh.ops.holes_fill(bm, edges=boundary_edges, sides=0)
        except Exception:
            pass
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()


def perform_object_joint_steps(context, target_objs, cutter_objs, spec, out_collection):
    """Subtract inflated copies of `cutter_objs` from every intersecting part in
    `target_objs` with mating clearance `spec.gap`,
    then add calibrated connectors between them."""
    t0 = time.time()
    if not isinstance(target_objs, (list, tuple)):
        target_objs = [target_objs]
    if not target_objs or not cutter_objs:
        raise CutError("Object Joint needs at least one target and one cutter object.")

    yield "preparing meshes for object joint"
    target_meshes = [mesh_utils.world_mesh_copy(context, o, f"_esp_joint_target_{i}") for i, o in enumerate(target_objs)]
    part_meshes = [mesh_utils.world_mesh_copy(context, o, f"_esp_joint_part_{i}") for i, o in enumerate(cutter_objs)]
    for m in target_meshes + part_meshes:
        _clean_mesh_for_bool(m)
    total = len(part_meshes)

    try:
        # 1. Carve each cutter part from all intersecting target parts.
        for i, p_me in enumerate(part_meshes):
            yield f"carving mating clearance {i + 1}/{total}"
            inflated_bm = surfaces.inflate_bmesh(p_me, spec.gap)
            inflated_me = mesh_utils.bmesh_to_mesh(inflated_bm, "_esp_inflated_cutter")
            inflated_bm.free()

            for t_idx, t_me in enumerate(target_meshes):
                if not _bounds_overlap(t_me, inflated_me, max(spec.gap * 4.0, 0.05)):
                    continue
                res = mesh_utils.boolean_mesh(context, t_me, inflated_me, 'DIFFERENCE', spec.solver)
                # If the clearance-inflated cutter had self-intersections on sharp bevels and returned empty,
                # fall back to subtracting the uninflated original cutter mesh p_me directly:
                if len(res.polygons) == 0:
                    mesh_utils.remove_mesh(res)
                    res = mesh_utils.boolean_mesh(context, t_me, p_me, 'DIFFERENCE', spec.solver)
                if len(res.polygons) > 0:
                    _clean_mesh_for_bool(res)
                    mesh_utils.remove_mesh(t_me)
                    target_meshes[t_idx] = res
                else:
                    mesh_utils.remove_mesh(res)

            mesh_utils.remove_mesh(inflated_me)

        # 2. Add calibrated connectors between the closest target piece and cutter piece
        pins = [c for c in spec.contacts if c.add_pin and c.pin_matrix is not None]
        for idx, c in enumerate(pins, start=1):
            yield f"adding joint connector {idx}/{len(pins)}"
            pin_center = c.pin_matrix.translation

            best_t = 0
            best_td = 1e30
            for i, t_me in enumerate(target_meshes):
                if len(t_me.polygons) == 0:
                    continue
                bvh = mesh_utils.bvh_from_mesh(t_me)
                loc, _nor, _fidx, dist = bvh.find_nearest(pin_center)
                if loc is not None and dist < best_td:
                    best_td = dist
                    best_t = i

            best_c = 0
            best_cd = 1e30
            for i, p_me in enumerate(part_meshes):
                if len(p_me.polygons) == 0:
                    continue
                bvh = mesh_utils.bvh_from_mesh(p_me)
                loc, _nor, _fidx, dist = bvh.find_nearest(pin_center)
                if loc is not None and dist < best_cd:
                    best_cd = dist
                    best_c = i

            pin = connectors.connector_mesh(c.shape, c.custom_obj, c.pin_matrix, "_esp_pin", unit_mesh=c.pin_mesh)
            socket = connectors.connector_mesh(
                c.shape,
                c.custom_obj,
                c.pin_matrix,
                "_esp_socket",
                radial_extra=spec.clearance,
                tip_extra=spec.tip_extra,
                unit_mesh=c.pin_mesh,
            )

            t_me = target_meshes[best_t]
            c_me = part_meshes[best_c]
            if spec.pin_side == 'A':
                new_target = mesh_utils.boolean_mesh(context, t_me, pin, 'UNION', spec.solver)
                yield f"carving joint socket {idx}/{len(pins)}"
                new_part = mesh_utils.boolean_mesh(context, c_me, socket, 'DIFFERENCE', spec.solver)
            else:
                new_part = mesh_utils.boolean_mesh(context, c_me, pin, 'UNION', spec.solver)
                yield f"carving joint socket {idx}/{len(pins)}"
                new_target = mesh_utils.boolean_mesh(context, t_me, socket, 'DIFFERENCE', spec.solver)

            if len(new_target.polygons) > 0:
                _clean_mesh_for_bool(new_target)
                mesh_utils.remove_mesh(t_me)
                target_meshes[best_t] = new_target
            else:
                mesh_utils.remove_mesh(new_target)

            if len(new_part.polygons) > 0:
                _clean_mesh_for_bool(new_part)
                mesh_utils.remove_mesh(c_me)
                part_meshes[best_c] = new_part
            else:
                mesh_utils.remove_mesh(new_part)

            mesh_utils.remove_mesh(pin)
            mesh_utils.remove_mesh(socket)

        if spec.remesh and spec.remesh_voxel > 0.0:
            for i in range(len(target_meshes)):
                yield f"remeshing target part {i + 1}/{len(target_meshes)}"
                target_meshes[i] = remesh_mesh(
                    context, target_meshes[i], spec.remesh_voxel, spec.remesh_adaptivity, spec.remesh_smooth
                )
            for i in range(len(part_meshes)):
                yield f"remeshing cutter part {i + 1}/{total}"
                part_meshes[i] = remesh_mesh(
                    context, part_meshes[i], spec.remesh_voxel, spec.remesh_adaptivity, spec.remesh_smooth
                )
    except Exception:
        for m in target_meshes:
            mesh_utils.remove_mesh(m)
        for m in part_meshes:
            mesh_utils.remove_mesh(m)
        raise
    finally:
        mesh_utils.cleanup_temp(context.scene)

    def _make_built_obj(me, src_obj, side_tag):
        # Preserve original naming chain so subsequent cuts in the plan still find their target
        base_stem = src_obj.get("esp_stem") or src_obj.get("esp_source") or src_obj.get("esp_base") or src_obj.name
        name = f"{src_obj.name}_BUILT"
        me.name = name
        if src_obj.type == 'MESH':
            for mat in src_obj.data.materials:
                me.materials.append(mat)
        obj = bpy.data.objects.new(name, me)
        obj["esp_part"] = True
        obj["esp_side"] = side_tag
        obj["esp_source"] = base_stem
        obj["esp_stem"] = base_stem
        obj["esp_base"] = base_stem
        out_collection.objects.link(obj)
        return obj

    built_targets = [_make_built_obj(m, src, 'A') for m, src in zip(target_meshes, target_objs)]
    built_cutters = [_make_built_obj(m, src, 'B') for m, src in zip(part_meshes, cutter_objs)]
    return built_targets, built_cutters, time.time() - t0
