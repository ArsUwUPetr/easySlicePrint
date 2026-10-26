# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Rafael Omodei and EasySlice Print contributors
"""Plan (draft) records, preview objects and the glue between records and the
core cut pipeline."""

import math

import bpy
from bpy.app.handlers import persistent
from mathutils import Matrix, Vector

from .core import connectors, cutting, mesh_utils, section, surfaces
from .core.units import mm_to_bu
from .props import connector_props

PLAN_COLLECTION = "_ESP_Plan"
BACKUP_COLLECTION = "ESP_Backup"
BUILT_PREFIX = "ESP_Built_"
GREEN = (0.30, 0.90, 0.40, 0.40)
ORANGE = (1.00, 0.55, 0.15, 0.85)
PREFIX = {
    'STRAIGHT': "Plane Cut",
    'CURVED': "Curve Cut",
    'FREEHAND': "Freehand Cut",
    'OBJECT': "Object Joint",
}

_suspend = 0


class ContactData:
    """What a cut tool produces for one contact (everything in world space)."""

    def __init__(self, kind):
        self.kind = kind
        self.verts = []
        self.faces = []
        self.points = []  # editable control points (curve / freehand)
        self.view_dir = Vector((0.0, 0.0, -1.0))
        self.depth_range = (0.0, 0.0)
        self.extend = 0.0
        self.margin = 0.0
        self.detail = surfaces.SURFACE_DETAIL  # spline samples per control point segment
        self.hit = None  # where the stroke touched the model
        self.through = None  # direction through the model at `hit`
        self.center_hint = None  # alternative: a point inside the part
        self.anchor = Vector((0.0, 0.0, 0.0))
        self.plane_co = None  # plane cuts: a point on the cut plane ...
        self.plane_no = None  # ... and its normal, so the section can be rebuilt
        self.span = None  # (axis, lo, hi): the stroke, measured from plane_co
        self.cutter = None  # what the boolean subtracts, when it is not the patch itself
        self.is_cut_face = False  # the patch is the real printed cut face, edge to edge
        self.regions_skipped = 0  # regions the plane crosses that the stroke missed


# ----------------------------------------------------------------------------
# preferences / units
# ----------------------------------------------------------------------------
def prefs(context):
    try:
        return context.preferences.addons[__package__].preferences
    except (KeyError, AttributeError):
        return None


def pref(context, name, default):
    p = prefs(context)
    return getattr(p, name, default) if p is not None else default


def mm(context):
    return mm_to_bu(context.scene, prefs(context))


# ----------------------------------------------------------------------------
# collections
# ----------------------------------------------------------------------------
def get_or_create_collection(scene, name, hide_render=True):
    col = bpy.data.collections.get(name)
    if col is None:
        col = bpy.data.collections.new(name)
    if col.name not in scene.collection.children:
        scene.collection.children.link(col)
    col.hide_render = hide_render
    return col


def plan_collection(scene):
    return get_or_create_collection(scene, PLAN_COLLECTION)


def backup_collection(scene):
    return get_or_create_collection(scene, BACKUP_COLLECTION)


def built_collection(scene, base_name):
    return get_or_create_collection(scene, BUILT_PREFIX + base_name, hide_render=False)


def move_to_backup(scene, obj):
    homes = [c.name for c in obj.users_collection]
    for c in obj.users_collection:
        c.objects.unlink(obj)
    backup_collection(scene).objects.link(obj)
    obj["esp_backup_home"] = homes
    obj.hide_set(True)
    obj.hide_render = True


def restore_from_backup(scene, obj):
    homes = list(obj.get("esp_backup_home", []))
    bc = bpy.data.collections.get(BACKUP_COLLECTION)
    if not homes and (bc is None or obj.name not in bc.objects):
        # never stashed (an approved part, say): leave its collections alone
        obj.hide_set(False)
        obj.hide_viewport = False
        obj.hide_render = False
        return
    if bc is not None and obj.name in bc.objects:
        bc.objects.unlink(obj)
    linked = False
    for name in homes:
        col = bpy.data.collections.get(name)
        if col is None and name == scene.collection.name:
            col = scene.collection
        if col is not None and obj.name not in col.objects:
            col.objects.link(obj)
            linked = True
    if not linked and obj.name not in scene.collection.objects:
        scene.collection.objects.link(obj)
    if "esp_backup_home" in obj:
        del obj["esp_backup_home"]
    obj.hide_set(False)
    obj.hide_viewport = False
    obj.hide_render = False


def ensure_evaluable(obj):
    """Make sure a (possibly hidden) object is evaluated by the depsgraph."""
    state = (obj.hide_viewport, obj.hide_get())
    if obj.hide_viewport:
        obj.hide_viewport = False
    if obj.hide_get():
        obj.hide_set(False)
    return state


def restore_visibility(obj, state):
    if obj.hide_viewport != state[0]:
        obj.hide_viewport = state[0]
    if obj.hide_get() != state[1]:
        obj.hide_set(state[1])


def resolve_target(context):
    """The mesh the cut tools act on: active mesh object (never a preview), else the plan's base."""
    obj = context.active_object
    if obj is not None and obj.type == 'MESH' and "esp_preview" not in obj and obj.visible_get():
        return obj
    settings = context.scene.esp
    base = bpy.data.objects.get(settings.base_object) if settings.base_object else None
    if base is not None and base.type == 'MESH':
        return base
    return None


def sync_record_pointers(rec):
    """Keep PointerProperty fields in sync with stored names if set by scripts or older files."""
    global _suspend
    _suspend += 1
    try:
        if rec.target_obj is None and rec.target:
            obj = bpy.data.objects.get(rec.target)
            if obj is not None and obj.type == 'MESH':
                rec.target_obj = obj
        elif rec.target_obj is not None and rec.target != rec.target_obj.name:
            rec.target = rec.target_obj.name

        raw_names = [n for n in rec.cutter_objects.split("\t") if n]
        if len(raw_names) == 1 and rec.cutter_obj is None:
            cobj = bpy.data.objects.get(raw_names[0])
            if cobj is not None and cobj.type == 'MESH':
                rec.cutter_obj = cobj
        elif rec.cutter_obj is not None and len(raw_names) <= 1:
            if rec.cutter_objects != rec.cutter_obj.name:
                rec.cutter_objects = rec.cutter_obj.name
    finally:
        _suspend -= 1


def record_target(rec):
    """Return the live Target Mesh object for `rec`, or None if it was deleted."""
    if getattr(rec, "target_obj", None) is not None:
        obj = rec.target_obj
        if obj.name in bpy.data.objects and obj.type == 'MESH':
            return obj
    if rec.target:
        obj = bpy.data.objects.get(rec.target)
        if obj is not None and obj.type == 'MESH':
            return obj
    return None


def cutter_names_raw(rec):
    """All stored cutter names on `rec` (including ones that may have been deleted)."""
    raw = [n for n in getattr(rec, "cutter_objects", "").split("\t") if n]
    if not raw and getattr(rec, "cutter_obj", None) is not None:
        return [rec.cutter_obj.name]
    if len(raw) == 1 and getattr(rec, "cutter_obj", None) is not None:
        return [rec.cutter_obj.name]
    return raw


def cutter_object_list(rec):
    """Return the list of live Mesh objects stored in `rec`."""
    out = []
    raw = cutter_names_raw(rec)
    if len(raw) <= 1 and getattr(rec, "cutter_obj", None) is not None:
        cobj = rec.cutter_obj
        if cobj.name in bpy.data.objects and cobj.type == 'MESH':
            return [cobj]
    for name in raw:
        obj = bpy.data.objects.get(name)
        if obj is not None and obj.type == 'MESH' and obj not in out:
            out.append(obj)
    return out


def missing_cutter_names(rec):
    """Return list of cutter object names that no longer exist in `bpy.data.objects`."""
    missing = []
    for name in cutter_names_raw(rec):
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != 'MESH':
            missing.append(name)
    return missing


def cutter_status_labels(rec):
    """Cutter names split into valid objects and missing object names."""
    valid, missing = [], []
    for name in cutter_names_raw(rec):
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != 'MESH':
            missing.append(name)
        else:
            valid.append(obj.name)
    return {"valid": valid, "missing": missing}


def plan_sources(context):
    """Every live model the enabled plan cuts into (strictly from active cut records)."""
    settings = context.scene.esp
    out = []
    for rec in settings.cuts:
        if not rec.enabled:
            continue
        obj = record_target(rec)
        if obj is not None and obj not in out:
            out.append(obj)
        if rec.cut_type == 'OBJECT':
            for cobj in cutter_object_list(rec):
                if cobj not in out:
                    out.append(cobj)
    return out


def validate_plan(context, paint_overlay=False):
    """Stage 1 of Build: fast pre-flight validation of all Ready cuts before any booleans run.

    Uses BVH overlap checks so cuts that do not touch the target mesh or cannot split it
    are caught in milliseconds before the user waits 5 minutes for a build.
    """
    from . import overlay
    from .core import diagnosis

    settings = context.scene.esp
    enabled = [r for r in settings.cuts if r.enabled]
    if not enabled:
        return ["No cuts are marked as Ready."]

    errors = []
    bvh_cache = {}

    def _get_target_bvh(obj):
        if obj.name not in bvh_cache:
            state = ensure_evaluable(obj)
            try:
                depsgraph = context.evaluated_depsgraph_get()
                ev = obj.evaluated_get(depsgraph)
                me = ev.to_mesh()
                try:
                    mw = obj.matrix_world
                    verts = [mw @ v.co for v in me.vertices]
                    faces = [tuple(p.vertices) for p in me.polygons]
                    bvh_cache[obj.name] = mesh_utils.bvh_from_pydata(verts, faces) if verts and faces else None
                finally:
                    ev.to_mesh_clear()
            finally:
                restore_visibility(obj, state)
        return bvh_cache[obj.name]

    for idx, rec in enumerate(enabled, start=1):
        label = f"Cut #{idx} '{rec.name}'"
        target = record_target(rec)
        if target is None:
            missing_name = rec.target or "None"
            errors.append(f"{label}: Target object '{missing_name}' does not exist. Pick a valid Target Object.")
            continue
        if len(target.data.polygons) == 0:
            errors.append(f"{label}: Target object '{target.name}' has no mesh faces.")
            continue

        if rec.cut_type == 'OBJECT':
            raw_c = cutter_names_raw(rec)
            missing_c = missing_cutter_names(rec)
            live_c = cutter_object_list(rec)
            if not raw_c or not live_c:
                errors.append(f"{label}: No valid Cutter object assigned.")
                continue
            if missing_c:
                errors.append(
                    f"{label}: Cutter object(s) missing from scene: {', '.join(missing_c)}. "
                    "Remove missing cutters or pick a valid Cutter."
                )
                continue
            if any(c == target for c in live_c):
                errors.append(f"{label}: Target object '{target.name}' cannot also be its own Cutter.")
                continue

            t_min, t_max = mesh_utils.object_world_bounds(target)
            pad = max(mesh_utils.object_world_diagonal(target) * 0.05, 1.0 * mm(context))
            cutter_overlaps = False
            for c in live_c:
                c_min, c_max = mesh_utils.object_world_bounds(c)
                if (
                    c_min.x - pad <= t_max.x
                    and c_max.x + pad >= t_min.x
                    and c_min.y - pad <= t_max.y
                    and c_max.y + pad >= t_min.y
                    and c_min.z - pad <= t_max.z
                    and c_max.z + pad >= t_min.z
                ):
                    cutter_overlaps = True
                    break
            if not cutter_overlaps:
                errors.append(
                    f"{label}: Target '{target.name}' and Cutter object(s) bounding boxes do not intersect."
                )
            continue

        for c_i in range(contact_count(rec)):
            sname = _contact_attr(rec, "surface", c_i)
            sobj = bpy.data.objects.get(sname) if sname else None
            if sobj is None or sobj.type != 'MESH' or len(sobj.data.polygons) == 0:
                errors.append(f"{label}: Cut preview surface is missing or empty.")
                continue

            # 1. Fast AABB check
            t_min, t_max = mesh_utils.object_world_bounds(target)
            s_min, s_max = mesh_utils.object_world_bounds(sobj)
            pad = max(rec.cut_gap_mm * mm(context) * 2.0, 0.5 * mm(context))
            overlaps_box = (
                s_min.x - pad <= t_max.x
                and s_max.x + pad >= t_min.x
                and s_min.y - pad <= t_max.y
                and s_max.y + pad >= t_min.y
                and s_min.z - pad <= t_max.z
                and s_max.z + pad >= t_min.z
            )
            if not overlaps_box:
                errors.append(
                    f"{label}: The cut surface does not touch '{target.name}' at all (bounding boxes do not overlap)."
                )
                if paint_overlay:
                    pv, pf = surface_world_patch(sobj, context, eval_modifiers=True)
                    overlay.show(diagnosis.nothing_cut(pv, pf), rec.name)
                continue

            # 2. Exact BVH surface-to-mesh intersection check (takes < 2 ms)
            if paint_overlay:
                t_bvh = _get_target_bvh(target)
                if t_bvh is not None:
                    if sobj.get("esp_custom_mesh"):
                        pv, pf = surface_world_patch(sobj, context, eval_modifiers=True)
                        diag = mesh_utils.object_world_diagonal(target)
                        margin = max(float(sobj.get("esp_margin", 0.0)), diag * 0.05, 2.0 * mm(context))
                        cv, cf = custom_mesh_cutter(context, target, pv, pf, margin)
                    else:
                        cv, cf = cutter_world_patch(sobj, context)
                    if cv and cf:
                        slab_bm = surfaces.slab_from_patch(cv, cf, max(rec.cut_gap_mm * mm(context), 1e-4))
                        slab_bm.verts.ensure_lookup_table()
                        s_verts = [v.co.copy() for v in slab_bm.verts]
                        s_faces = [tuple(v.index for v in f.verts) for f in slab_bm.faces]
                        slab_bm.free()
                        s_bvh = mesh_utils.bvh_from_pydata(s_verts, s_faces)
                        hits = t_bvh.overlap(s_bvh)
                        if not hits:
                            pv, pf = surface_world_patch(sobj, context, eval_modifiers=True)
                            overlay.show(diagnosis.nothing_cut(pv, pf), rec.name)
                            errors.append(
                                f"{label}: The cut surface does not split '{target.name}' "
                                "(none of the cut surface intersects the mesh)."
                            )
    return errors


# ----------------------------------------------------------------------------
# cross sections (plane cuts)
# ----------------------------------------------------------------------------
SECTION_MARGIN = 0.01  # of the section's own bounding box


def straight_section(context, target, plane_co, plane_no, span=None, reference=None):
    """The model's own cross section on a plane, grown by a hair. None if it fails.

    This is what a plane cut uses as its surface: the area the print will actually be
    cut through, taken from the model's geometry, instead of a rectangle drawn around
    the mouse stroke. `span` is the stroke - every region of the model the line ran
    across is cut, and only those, so one leg goes and the other stays.

    An open or non manifold mesh may not section at all; the caller falls back to the
    old rectangle in that case rather than leaving the user without a cut.
    """
    if target is None or target.type != 'MESH':
        return None
    s = context.scene.esp
    fill_mode = getattr(s, 'plane_fill_mode', 'RADIAL')
    rim_points = getattr(s, 'section_points', 32)
    radial_rings = getattr(s, 'section_rings', 4)

    state = ensure_evaluable(target)
    try:
        depsgraph = context.evaluated_depsgraph_get()
        res = section.plane_section(
            target, plane_co, plane_no,
            span=span, reference=reference, depsgraph=depsgraph,
            rim_points=rim_points,
            fill_mode=fill_mode,
            radial_rings=radial_rings,
        )
    except Exception:
        return None
    finally:
        restore_visibility(target, state)
    if res is None:
        return None
    verts, faces = res
    margin = max(section.section_extent(verts) * SECTION_MARGIN, mesh_utils.object_world_diagonal(target) * 0.002)
    grown_v, grown_f = section.grow_section(verts, faces, margin, plane_no)
    return section.SectionResult(grown_v, grown_f, islands=res.islands, kept=res.kept, cutter=res.cutter)


def fill_straight_contact(data, patch, span):
    """Put a section into a contact: the face the user sees *and* the quad the boolean gets.

    Both travel on the same `SectionResult`, and they are not interchangeable. Copying
    only the first leaves the boolean subtracting the section itself, whose rim lies on
    the model tangentially - the exact solver returns the mesh nearly untouched, so the
    cut reports success, names two parts and separates nothing.
    """
    data.verts, data.faces = patch
    data.cutter = patch.cutter
    data.span = span
    data.is_cut_face = True
    data.regions_skipped = patch.islands - patch.kept


# ----------------------------------------------------------------------------
# cut surfaces that follow the model (curve cuts)
# ----------------------------------------------------------------------------
def column_runs(context, target, columns, direction, ref):
    """Per column, the runs of material along `direction`, measured from `ref`.

    Every crossing along the ray is collected as entry/exit pairs.
    -> list of lists of (lo, hi).
    """
    diag = mesh_utils.object_world_diagonal(target)
    d = Vector(direction).normalized()
    eps = diag * 1e-4
    state = ensure_evaluable(target)
    out = []
    try:
        depsgraph = context.evaluated_depsgraph_get()
        for p in columns:
            start = Vector(p) - d * diag * 2.0
            base = (start - Vector(ref)).dot(d)
            hits = mesh_utils.object_ray_hits(target, start, d, eps, depsgraph, max_dist=diag * 4.0)
            out.append([(base + hits[i], base + hits[i + 1]) for i in range(0, len(hits) - 1, 2)])
    finally:
        restore_visibility(target, state)
    return out


def ribbon_surfaces(context, target, points, view, extend, detail, pad_factor=0.02):
    """The pair of surfaces a curve cut needs. -> (preview, cutter, band) or None.

    `preview` follows the model: every column of the ribbon runs exactly as deep as the
    material under it, and a column with nothing under it is dropped. `cutter` is what the boolean subtracts:
    the same ribbon at one depth for its whole length, reaching past the model on both
    sides - a rim in free space, for the same reason `section.clip_rect` exists.

    `band` is the run of material intersected by the stroke, widened partway into the
    adjacent empty gaps. This prevents a single range based on the furthest ray hit
    from extending through disconnected regions.
    """
    if target is None or target.type != 'MESH':
        return None
    d = Vector(view).normalized()
    try:
        columns = surfaces.ribbon_samples(points, extend, detail)
    except ValueError:
        return None
    ref = sum((Vector(p) for p in points), Vector((0.0, 0.0, 0.0))) / len(points)
    try:
        runs = column_runs(context, target, columns, d, ref)
    except Exception:
        return None
    flat = [r for col in runs for r in col]
    if not flat:
        return None
    diag = mesh_utils.object_world_diagonal(target)
    ts = sorted((Vector(p) - ref).dot(d) for p in points)
    anchor = ts[len(ts) // 2]
    band = section.band_around(flat, anchor, diag * pad_factor)
    if band is None:
        return None
    # `column_runs` measures along the ray from one shared reference; a ribbon column
    # is extruded from its own point, so every range has to be rebased onto it
    trimmed, flat_band = [], []
    for col, p in zip(runs, columns):
        offset = (Vector(p) - ref).dot(d)
        flat_band.append((band[0] - offset, band[1] - offset))
        within = [(max(lo, band[0]), min(hi, band[1])) for lo, hi in col if hi > band[0] and lo < band[1]]
        # Use one span from the first entry to the last exit so each column remains continuous.
        if within:
            trimmed.append((min(a for a, _b in within) - offset, max(b for _a, b in within) - offset))
        else:
            trimmed.append(None)
    try:
        preview = surfaces.ribbon_patch(points, d, 0.0, extend, detail=detail, depth_ranges=trimmed)
    except ValueError:
        return None
    cutter = surfaces.ribbon_patch(points, d, 0.0, extend, detail=detail, depth_ranges=flat_band)
    return preview, cutter, band


# ----------------------------------------------------------------------------
# preview objects
# ----------------------------------------------------------------------------
def _material(name, rgba):
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
        mat.use_nodes = False
    mat.diffuse_color = rgba
    try:
        mat.surface_render_method = 'BLENDED'
    except Exception:
        pass
    return mat


def _preview_object(scene, name, mesh, kind, color, mat_name):
    obj = bpy.data.objects.new(name, mesh)
    plan_collection(scene).objects.link(obj)
    obj.display_type = 'SOLID'
    obj.show_in_front = True
    obj.hide_render = True
    obj.color = color
    obj["esp_preview"] = kind
    mesh.materials.append(_material(mat_name, color))
    return obj


def surface_origin_point(context, data, target):
    """Where the origin of a generated cut surface goes.

    By default the surface's own median point, so R / S on it pivot on the surface
    instead of on some point out in the scene. 'OBJECT' puts it on the origin of the
    model being cut, which is what you want when several cut surfaces should share
    one pivot.
    """
    if context.scene.esp.surface_origin == 'OBJECT' and target is not None:
        return target.matrix_world.translation.copy()
    if data.verts:
        return surfaces.patch_center(data.verts)
    return Vector((0.0, 0.0, 0.0))


def create_surface_object(context, name, data, target=None):
    """The patch arrives in world space; it is stored local to `origin` so the preview
    object carries a real origin of its own (mesh AND the editable control points)."""
    origin = surface_origin_point(context, data, target)
    me = mesh_utils.mesh_from_pydata(name, [Vector(v) - origin for v in data.verts], data.faces)
    obj = _preview_object(context.scene, name, me, 'surface', GREEN, "ESP_Preview_Surface")
    obj.matrix_world = Matrix.Translation(origin)
    obj["esp_kind"] = data.kind
    flat_pts = [c for p in data.points for c in (Vector(p) - origin)]
    obj["esp_points"] = flat_pts
    obj["esp_points_orig"] = list(flat_pts)
    obj["esp_view"] = list(data.view_dir)
    obj["esp_depth"] = list(data.depth_range)
    obj["esp_extend"] = data.extend
    obj["esp_margin"] = data.margin
    obj["esp_detail"] = data.detail
    if data.plane_no is not None and data.plane_co is not None:
        # stored local, so G / R / S on the preview move the plane with the object and
        # the section can be taken again from wherever the user parked it
        obj["esp_plane_co"] = list(Vector(data.plane_co) - origin)
        obj["esp_plane_no"] = list(data.plane_no)
        obj["esp_ref"] = list(Vector(data.anchor) - origin)
        obj["esp_cut_face"] = data.is_cut_face
        obj["esp_skipped"] = data.regions_skipped
        if data.span is not None:
            obj["esp_span_u"] = list(data.span[0])
            obj["esp_span"] = [data.span[1], data.span[2]]
    if data.hit is not None:
        obj["esp_hit"] = list(data.hit)
        obj["esp_through"] = list(data.through)
    if data.center_hint is not None:
        obj["esp_center_hint"] = list(data.center_hint)
    obj["esp_cut_face"] = data.is_cut_face
    set_cutter(obj, [Vector(p) - origin for p in data.cutter[0]] if data.cutter else None, data.cutter)
    obj["esp_mw"] = flat(obj.matrix_basis)
    if not data.points and data.kind != 'STRAIGHT':
        obj["esp_custom_mesh"] = True
    return obj


def surface_points(obj):
    flat = list(obj.get("esp_points", []))
    return [Vector(flat[i : i + 3]) for i in range(0, len(flat) - 2, 3)]


def set_surface_points(obj, points):
    obj["esp_points"] = [c for p in points for c in p]


def _clear_point(target, p, margin, depsgraph):
    """`p` moved out of the model until it clears the surface by `margin`."""
    p = Vector(p)
    # Moving a point clear of one surface can place it inside another, so check again.
    for _ in range(3):
        found, loc, nor, depth = mesh_utils.object_surface_depth(target, p, depsgraph)
        if not found or depth >= margin:
            break
        p = loc + nor * margin
    return p


def clear_of_model(context, target, points, margin):
    """Move each point out of the model until it clears the surface by `margin`.

    The tool no longer does this to a rim: a point the user drew is where the cut goes,
    and lifting it off the surface - along one normal at a time, which in a crease
    bounces the point between the two faces that meet there and carries it millimetres
    out of the crease - is what put the cut face off the traced line. The clearance is
    a skirt on the cutter now (`skirt_ring`, which lifts skirt vertices with the same
    `_clear_point`). This is kept as the pipeline it replaced, so a test can measure
    the two against each other.
    """
    if target is None or context is None or margin <= 0.0:
        return [Vector(p) for p in points]
    state = ensure_evaluable(target)
    try:
        depsgraph = context.evaluated_depsgraph_get()
        return [_clear_point(target, p, margin, depsgraph) for p in points]
    finally:
        restore_visibility(target, state)


def settle_on_model(context, target, points, detail):
    """Adaptive rim subdivision: preserves drawn points, subdivides long stretched
    segments to uniform step, and snaps them to the surface."""
    if target is None or context is None:
        return surfaces.loop_boundary(points, detail)
    pts = surfaces.decimate_polyline([Vector(p) for p in points], surfaces.MAX_LOOP_SAMPLES)
    m = len(pts)
    if m < 3:
        raise ValueError("loop needs at least 3 points")

    total_len = surfaces.polyline_length(pts, closed=True)
    target_count = min(surfaces.MAX_LOOP_SAMPLES, max(m * max(1, detail), 96))
    max_seg_len = max(total_len / target_count, 1e-5)

    state = ensure_evaluable(target)
    try:
        depsgraph = context.evaluated_depsgraph_get()
        diag = mesh_utils.object_world_diagonal(target)
        hair = diag * 1e-5
        normals = []
        for p in pts:
            found, _loc, nor, _depth = mesh_utils.object_surface_depth(target, p, depsgraph)
            normals.append(nor if found else Vector((0.0, 0.0, 1.0)))

        out = []
        for i in range(m):
            a, b = pts[i], pts[(i + 1) % m]
            seg_len = (b - a).length
            # Subdivide segment proportionally to its length to eliminate stretching
            steps = max(1, min(32, int(math.ceil(seg_len / max_seg_len))))
            axis = normals[i] + normals[(i + 1) % m]
            if axis.length < 1e-6:
                axis = normals[i]
            axis.normalize()
            seg_max = max(seg_len * 0.75, hair * 10.0)
            for k in range(steps):
                q = a.lerp(b, k / steps)
                if k > 0:
                    found, loc, _nor, depth = mesh_utils.object_surface_depth(target, q, depsgraph)
                    if found and depth < -hair:
                        hits = []
                        for d in (axis, -axis):
                            hit, hloc, _n, dist = mesh_utils.object_ray_cast(
                                target, q, d, depsgraph, max_dist=seg_max
                            )
                            if hit:
                                hits.append((dist, hloc))
                        q = min(hits)[1] if hits else loc
                out.append(q)

        if len(out) > surfaces.MAX_LOOP_SAMPLES:
            out = surfaces.decimate_polyline(out, surfaces.MAX_LOOP_SAMPLES)
        out = surfaces.dedupe_polyline(out, 1e-9)
        if len(out) < 3:
            raise ValueError("loop is degenerate")
        return out
    finally:
        restore_visibility(target, state)


def _clearance(target, q, depsgraph):
    """Signed distance from `q` to the model: positive outside, negative inside."""
    found, loc, _nor, depth = mesh_utils.object_surface_depth(target, q, depsgraph)
    if not found:
        return 1e30
    dist = (Vector(q) - loc).length
    return dist if depth >= 0.0 else -dist


def _march_clear(target, p, d, margin, depsgraph, want):
    """`p` moved along `d`, at least by |d| and further until it clears the model by `want`.

    Growing the offset along its own direction is what keeps the skirt from stepping
    sideways: a sideways lift is what `_clear_point` does, and two neighbours lifted
    sideways along different surfaces are how a skirt folds.
    """
    length = d.length
    if length < 1e-9:
        return Vector(p)
    u = d / length
    best, best_clear = Vector(p) + d, -1e30
    t = length
    step = margin * 0.125
    while t <= margin * 3.0 + 1e-9:
        q = Vector(p) + u * t
        clear = _clearance(target, q, depsgraph)
        if clear >= want:
            return q
        if clear > best_clear:
            best, best_clear = q, clear
        t += step
    return best


def skirt_ring(context, target, verts, faces, rim, margin):
    """Extends the cutter beyond the boundary loop `rim` by `margin` distance along the collar plane."""
    out = surfaces.rim_outward(verts, faces, rim)
    pts = [Vector(verts[i]) for i in range(rim)]
    if target is None or context is None or margin <= 0.0:
        return [pts[i] + out[i] * margin for i in range(rim)]

    # In COLLAR and BIHARMONIC modes, the collar already exits the wall at ~90 degrees,
    # so the skirt must proceed strictly along vector `out[i]` without vertical offsets!
    s = getattr(context.scene, "esp", None)
    if s is not None and getattr(s, "loop_fill_mode", 'MEMBRANE') in ('COLLAR', 'BIHARMONIC'):
        ring = [pts[i] + out[i] * margin for i in range(rim)]
        return surfaces.unfold_skirt(pts, ring, passes=80)

    state = ensure_evaluable(target)
    try:
        depsgraph = context.evaluated_depsgraph_get()
        tol = margin * 0.3
        ring = []
        for i in range(rim):
            p = pts[i]
            found, _loc, nor, _depth = mesh_utils.object_surface_depth(target, p, depsgraph)
            ways = [out[i]]
            if found:
                lean = out[i] + nor
                if lean.length > 1e-6:
                    ways.append(lean.normalized())
                ways.append(nor)
            chosen = None
            for d in ways:
                if d.length < 1e-9:
                    continue
                q = _clear_point(target, p + d * margin, margin, depsgraph)
                back = p - q
                dist = back.length
                if dist <= 1e-9:
                    continue
                chosen = q
                hit, _l, _n, _d = mesh_utils.object_ray_cast(
                    target, q, back / dist, depsgraph, max_dist=max(dist - tol, 0.0)
                )
                if not hit:
                    break
            ring.append(chosen if chosen is not None else p + out[i] * margin)
        ring = surfaces.unfold_skirt(pts, ring, passes=200)
        want = margin * 0.5
        ring = [_march_clear(target, p, q - p, margin, depsgraph, want) for p, q in zip(pts, ring)]
        return surfaces.unfold_skirt(pts, ring, local=True)
    finally:
        restore_visibility(target, state)


def _boundary_inward_normals(context, target, boundary, patch_n):
    """Compute inward-pointing surface normals along `boundary`, projected perpendicular to the cut normal."""
    if target is None or context is None:
        return None
    state = ensure_evaluable(target)
    try:
        depsgraph = context.evaluated_depsgraph_get()
        inward = []
        for p in boundary:
            found, _loc, nor, _depth = mesh_utils.object_surface_depth(target, p, depsgraph)
            if not found:
                inward.append(Vector((0.0, 0.0, 0.0)))
                continue
            # Project inward normal (-nor) mostly onto the cut plane so the collar doesn't dive vertically
            d = -nor - patch_n * ((-nor).dot(patch_n) * 0.35)
            inward.append(d.normalized() if d.length > 1e-6 else -nor)
        return inward
    finally:
        restore_visibility(target, state)


def loop_surface(context, target, points, detail, margin, passes=None):
    """The freehand membrane and its cutter. -> (verts, faces, cutter)"""
    boundary = settle_on_model(context, target, points, detail)
    mode = 'MEMBRANE'
    collar_depth = 0.0
    collar_smooth = 0.65
    inward = None
    if context is not None and hasattr(context.scene, "esp"):
        s = context.scene.esp
        mode = getattr(s, "loop_fill_mode", 'MEMBRANE')
        collar_depth = getattr(s, "collar_depth_mm", 1.5) * mm(context)
        collar_smooth = getattr(s, "collar_smoothing", 0.65)
        if mode in ('COLLAR', 'BIHARMONIC') and target is not None:
            patch_n = surfaces.newell_normal(boundary)
            inward = _boundary_inward_normals(context, target, boundary, patch_n)

    verts, faces = surfaces.loop_patch(
        points,
        detail=detail,
        passes=passes,
        boundary=boundary,
        mode=mode,
        inward_normals=inward,
        collar_depth=collar_depth,
        collar_smooth=collar_smooth,
    )
    rim = len(boundary)
    if margin <= 0.0:
        return verts, faces, None
    ring = skirt_ring(context, target, verts, faces, rim, margin)
    return verts, faces, surfaces.with_skirt(verts, faces, rim, ring)


# A freehand cutter has to stand clear of the model by a real distance, not a hair: where
# the slab wall leaves the surface almost tangentially the boolean welds the two halves
# back together, and the part comes out in one piece with the cut face already carved
# into it.
#
# The clearance used to be paid for in traced detail: the rim itself was pushed out by
# it, so every millimetre of it was a millimetre the cut face had moved off the line.
# The rim remains on the drawn line, and the clearance extends beyond it as a skirt.
# This scale provides enough clearance for side classification; increase Surface Margin
# if a freehand cut still fails to split the model.
LOOP_MARGIN_SCALE = 1.5


def loop_margin(context, src, points, diag):
    """How far a freehand cutter has to reach past its rim, clear of the model.

    Scale the margin to the loop and model dimensions, with a minimum based on the kerf.
    """
    u = mm(context)
    radius = surfaces.polyline_length(points, closed=True) / (2.0 * math.pi)
    return max(
        radius * context.scene.esp.surface_margin * LOOP_MARGIN_SCALE,
        diag * 0.002,
        src.cut_gap_mm * u * 3.0,
        0.6 * u,
    )


def custom_mesh_cutter(context, target, verts, faces, margin):
    """Expands open boundary edges of a custom mesh outward by `margin` distance
    so that the boolean cutter is guaranteed to penetrate beyond the model walls."""
    if not verts or not faces or margin <= 0.0:
        return verts, faces
    import bmesh

    bm = bmesh.new()
    bverts = [bm.verts.new(Vector(v)) for v in verts]
    bm.verts.ensure_lookup_table()
    bm.verts.index_update()
    for f in faces:
        try:
            bm.faces.new([bverts[i] for i in f])
        except ValueError:
            pass
    if not bm.faces:
        bm.free()
        return verts, faces

    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    ref_n = surfaces.patch_normal(verts, faces)
    if sum(f.normal.dot(ref_n) * f.calc_area() for f in bm.faces) < 0.0:
        bmesh.ops.reverse_faces(bm, faces=list(bm.faces))

    # Compute outward vector (in the plane of boundary polygons) for each boundary vertex
    push = {}
    for f in bm.faces:
        fn = f.normal
        for loop in f.loops:
            if not loop.edge.is_boundary:
                continue
            a = loop.vert
            b = loop.link_loop_next.vert
            d = b.co - a.co
            if d.length < 1e-12:
                continue
            outward = d.normalized().cross(fn)
            if outward.length < 1e-12:
                continue
            outward.normalize()
            push[a.index] = push.get(a.index, Vector((0.0, 0.0, 0.0))) + outward
            push[b.index] = push.get(b.index, Vector((0.0, 0.0, 0.0))) + outward

    # Shift boundary vertices outward by margin
    out_v = [v.co.copy() for v in bm.verts]
    out_f = [tuple(v.index for v in f.verts) for f in bm.faces]
    bm.free()

    for idx, acc in push.items():
        if acc.length > 1e-12:
            out_v[idx] = out_v[idx] + acc.normalized() * margin

    return out_v, out_f


def reset_surface_to_procedural(context, rec, index=0):
    """Discard manual mesh edits and modifiers on a surface and rebuild it from its procedural parameters."""
    name = rec.surface_b if index == 1 and rec.two_contact else rec.surface_a
    sobj = bpy.data.objects.get(name) if name else None
    if sobj is None:
        return
    if "esp_custom_mesh" in sobj:
        del sobj["esp_custom_mesh"]
    sobj.modifiers.clear()
    target = bpy.data.objects.get(rec.target) or bpy.data.objects.get(context.scene.esp.base_object)
    rebuild_surface(sobj, draft=False, context=context, target=target)
    refresh_record_frames(context, rec)


def rebuild_surface(obj, draft=False, context=None, target=None):
    """Regenerate the patch from its control points (unless it is in custom mesh edit mode or has modifiers)."""
    if obj.get("esp_custom_mesh") or len(obj.modifiers) > 0:
        obj["esp_custom_mesh"] = True
        return

    kind = obj.get("esp_kind", 'STRAIGHT')
    pts = surface_points(obj)
    detail = int(obj.get("esp_detail", surfaces.SURFACE_DETAIL))
    if draft:
        detail = max(1, detail // 2)
    if kind == 'CURVED' and len(pts) >= 2:
        # the control points are local to the object; the stored view direction is world
        mw = obj.matrix_basis
        world_view = Vector(obj["esp_view"]).normalized()
        built = None
        if context is not None and target is not None:
            built = ribbon_surfaces(context, target, [mw @ p for p in pts], world_view, obj["esp_extend"], detail)
        if built is not None:
            preview, cutter, band = built
            inv = mw.inverted_safe()
            verts = [inv @ v for v in preview[0]]
            faces = preview[1]
            obj["esp_depth"] = list(band)
            set_cutter(obj, [inv @ Vector(p) for p in cutter[0]], cutter)
        else:
            # no model to measure against: one depth for the whole ribbon, as before
            view = (mw.to_3x3().inverted_safe() @ world_view).normalized()
            verts, faces = surfaces.ribbon_patch(
                pts, view, 0.0, obj["esp_extend"], depth_range=tuple(obj["esp_depth"]), detail=detail
            )
            set_cutter(obj, None, None)
    elif kind == 'FREEHAND' and len(pts) >= 3:
        # the skirt is measured against the model on the full rebuild; a drag keeps up
        # with the mouse instead, and the drag ends in a full rebuild anyway
        margin = 0.0 if draft else float(obj.get("esp_margin", 0.0))
        world = [obj.matrix_basis @ p for p in pts]
        if context is not None and hasattr(context.scene, "esp"):
            world = surfaces.smooth_polyline(world, context.scene.esp.loop_smoothing, closed=True)
        verts, faces, cutter = loop_surface(context, target, world, detail, margin, passes=6 if draft else None)
        inv = obj.matrix_basis.inverted_safe()
        verts = [inv @ Vector(v) for v in verts]
        if not draft:
            set_cutter(obj, [inv @ Vector(p) for p in cutter[0]] if cutter else None, cutter)
    elif kind == 'STRAIGHT':
        patch = rebuilt_section(obj, context, target)
        if patch is None:
            return
        verts, faces = patch
    else:
        return
    me = obj.data
    me.clear_geometry()
    me.from_pydata([tuple(v) for v in verts], [], [tuple(f) for f in faces])
    me.update()


def rebuilt_section(obj, context, target):
    """Section the model again on the preview's current plane. Local space, or None.

    A plane cut carries no control points; what it carries is the plane itself, so a
    moved or rotated preview re-slices the model rather than dragging a stale outline
    around. Scaling a section has no effect - the section is the model's own area, and
    there is nothing to scale it to.
    """
    if context is None or target is None or "esp_plane_no" not in obj:
        return None
    mw = obj.matrix_basis
    rot = mw.to_3x3()
    no = (rot.inverted_safe().transposed() @ Vector(obj["esp_plane_no"])).normalized()
    co = mw @ Vector(obj["esp_plane_co"])
    ref = mw @ Vector(obj["esp_ref"]) if "esp_ref" in obj else None
    span = None
    if "esp_span_u" in obj:
        lo, hi = obj["esp_span"]
        span = ((rot @ Vector(obj["esp_span_u"])).normalized(), lo, hi)
    patch = straight_section(context, target, co, no, span, ref)
    if patch is None:
        return None
    obj["esp_skipped"] = patch.islands - patch.kept
    inv = mw.inverted_safe()
    cutter = patch.cutter
    set_cutter(obj, [inv @ Vector(p) for p in cutter[0]] if cutter else None, cutter)
    verts, faces = patch
    return [inv @ v for v in verts], faces


def set_cutter(obj, local_verts, cutter):
    """Store what the boolean should subtract, in the preview's own space.

    Only a surface whose own rim would sit on the model needs one: a plane cut's
    section, a curve cut's silhouette hugging ribbon, and a freehand loop, whose
    membrane ends on the line drawn on the surface and whose cutter is that membrane
    with a skirt past the rim. Faces are stored triangulated, whatever they were.
    """
    if not local_verts or cutter is None:
        for key in ("esp_cutter", "esp_cutter_f", "esp_cutter_n"):
            if key in obj:
                del obj[key]
        return
    tris = []
    for f in cutter[1]:
        for k in range(1, len(f) - 1):
            tris.extend((f[0], f[k], f[k + 1]))
    obj["esp_cutter"] = [c for p in local_verts for c in p]
    obj["esp_cutter_f"] = tris
    obj["esp_cutter_n"] = 3


def cutter_world_patch(obj, context=None):
    """(verts, faces) the boolean should subtract, or the preview's own evaluated mesh."""
    flat_co = list(obj.get("esp_cutter", []))
    flat_f = list(obj.get("esp_cutter_f", []))
    n = int(obj.get("esp_cutter_n", 4))
    if len(flat_co) < 9 or len(flat_f) < n:
        return surface_world_patch(obj, context)
    mw = obj.matrix_basis
    verts = [mw @ Vector(flat_co[i : i + 3]) for i in range(0, len(flat_co) - 2, 3)]
    faces = [tuple(flat_f[i : i + n]) for i in range(0, len(flat_f) - n + 1, n)]
    return verts, faces


def surface_world_patch(obj, context=None, eval_modifiers=False):
    """World-space (verts, faces) of the surface preview.

    `eval_modifiers` is ONLY True during actual Build or explicit Refresh in Object Mode,
    preventing Blender's depsgraph from deadlocking when adding Subdivision modifiers live.
    """
    mw = obj.matrix_basis
    if eval_modifiers and context is not None and len(obj.modifiers) > 0 and obj.mode == 'OBJECT':
        try:
            depsgraph = context.evaluated_depsgraph_get()
            eval_obj = obj.evaluated_get(depsgraph)
            me = eval_obj.to_mesh()
            try:
                verts = [mw @ v.co for v in me.vertices]
                faces = [tuple(p.vertices) for p in me.polygons]
                if verts and faces:
                    return verts, faces
            finally:
                eval_obj.to_mesh_clear()
        except Exception:
            pass
    verts = [mw @ v.co for v in obj.data.vertices]
    faces = [tuple(p.vertices) for p in obj.data.polygons]
    return verts, faces


def create_pin_object(context, name, shape_id, matrix):
    shape, custom = connectors.resolve_shape(shape_id)
    bm = connectors.unit_connector_bmesh(shape, custom)
    me = mesh_utils.bmesh_to_mesh(bm, name)
    bm.free()
    obj = _preview_object(context.scene, name, me, 'pin', ORANGE, "ESP_Preview_Pin")
    obj.matrix_world = matrix
    obj["esp_shape"] = shape_id
    return obj


def update_pin_mesh(obj, shape_id):
    shape, custom = connectors.resolve_shape(shape_id)
    bm = connectors.unit_connector_bmesh(shape, custom)
    me = mesh_utils.bmesh_to_mesh(bm, obj.name)
    bm.free()
    me.materials.append(_material("ESP_Preview_Pin", ORANGE))
    old = obj.data
    obj.data = me
    mesh_utils.remove_mesh(old)
    obj["esp_shape"] = shape_id


def pin_unit_mesh(pin):
    """The connector geometry to build, taken from the preview pin itself.

    Whatever the user did to the pin in edit mode - reshaped it, scaled it, added a
    second lobe - is in this mesh and nowhere else, so the build has to read it here
    instead of regenerating the shape from its name. Only a pin left with nothing the
    boolean could use falls back to the stored shape.
    """
    if pin is None or pin.type != 'MESH' or pin.data is None:
        return None
    if not pin.data.polygons:
        return None
    return pin.data


def flat(m):
    return [m[i][j] for i in range(4) for j in range(4)]


def unflat(vals):
    return Matrix([[vals[i * 4 + j] for j in range(4)] for i in range(4)])


# ----------------------------------------------------------------------------
# frames & sizes
# ----------------------------------------------------------------------------
def contact_frame(
    context, target, verts, faces, hit=None, through=None, center_hint=None, is_cut_face=False, shrink=0.0
):
    """-> (centre, normal, inscribed diameter) for the connector on this cut surface.

    When the surface is the real printed cut face - a plane section, a ribbon trimmed
    to the model, a freehand loop drawn on it - the answer is exact and cheap: the
    largest circle that fits inside it. A surface that covers more than the printed
    face (a fallback rectangle, an untrimmed ribbon) says nothing about where the
    material is, and the ray based estimate is still the best available.

    `shrink` comes off the diameter, for a face that is known to reach a little past
    the material. No surface needs it today: a freehand membrane ends on the loop
    drawn on the surface, and its clearance lives in a separate cutter.
    """
    if is_cut_face:
        normal = surfaces.patch_normal(verts, faces)
        found = section.inscribed_circle(verts, faces, normal)
        if found is not None:
            return found[0], normal, max(found[1] - shrink, found[1] * 0.2)
    bvh = mesh_utils.bvh_from_pydata(verts, faces)
    diag = mesh_utils.object_world_diagonal(target)
    state = ensure_evaluable(target)
    try:
        depsgraph = context.evaluated_depsgraph_get()
        if center_hint is not None:
            return cutting.estimate_pin_frame(target, depsgraph, bvh, diag, center_hint=center_hint)
        return cutting.estimate_pin_frame(target, depsgraph, bvh, diag, surface_point=hit, through_dir=through)
    finally:
        restore_visibility(target, state)


def preset_size_mm(context, preset, inscribed_bu):
    factor = connectors.preset_factor(preset)
    width_mm = max(1.0, inscribed_bu * factor / mm(context))
    return width_mm, width_mm * connectors.HEIGHT_RATIO


def contact_size_bu(context, src, inscribed_bu):
    """(width, height) in BU for a settings source (record or scene defaults)."""
    if src.size_preset == 'CUSTOM':
        return src.pin_width_mm * mm(context), src.pin_height_mm * mm(context)
    w_mm, h_mm = preset_size_mm(context, src.size_preset, inscribed_bu)
    return w_mm * mm(context), h_mm * mm(context)


def _contact_attr(rec, base, i):
    return getattr(rec, f"{base}_{'ab'[i]}")


def _set_contact_attr(rec, base, i, value):
    setattr(rec, f"{base}_{'ab'[i]}", value)


def auto_pin_matrix(context, rec, i):
    center = Vector(_contact_attr(rec, "center", i))
    normal = Vector(_contact_attr(rec, "normal", i))
    w, h = contact_size_bu(context, rec, _contact_attr(rec, "inscribed", i))
    return connectors.connector_matrix(center, cutting.protrude_direction(normal, rec.pin_side), w, h)


def apply_preset(context, rec):
    """Write the preset size of contact A into the width/height fields."""
    if rec.size_preset == 'CUSTOM':
        return
    w_mm, h_mm = preset_size_mm(context, rec.size_preset, rec.inscribed_a)
    rec.pin_width_mm = w_mm
    rec.pin_height_mm = h_mm


def printer_clearance_mm(context):
    """The clearance this printer needs on each side of a pin, from the preferences."""
    return pref(context, "printer_clearance_mm", 0.10)


def preset_clearance_mm(context, fit_preset):
    return printer_clearance_mm(context) * connectors.fit_factor(fit_preset)


_applying_fit = False


def apply_fit(context, src):
    """Write the Fit preset into the gap field, so the number in the panel is the real one.

    `src` is a cut record or the scene defaults - both carry the connector settings. The
    write lands on a property that has an update callback of its own, which calls back in
    here, so the guard and the tolerance check keep it from looping.
    """
    global _applying_fit
    if src.fit_preset == 'CUSTOM' or _applying_fit:
        return
    target = preset_clearance_mm(context, src.fit_preset)
    if abs(src.clearance_mm - target) <= 1e-6:
        return
    _applying_fit = True
    try:
        src.clearance_mm = target
    finally:
        _applying_fit = False


def copy_connector_props(src, dst):
    for key in connector_props():
        try:
            setattr(dst, key, getattr(src, key))
        except TypeError:
            pass


def contact_count(rec):
    return 2 if rec.two_contact else 1


def extra_pin_objects(rec):
    """All additional connector pins added to this cut."""
    raw = rec.get("esp_extra_pins", "")
    if not raw:
        return []
    out = []
    for name in raw.split("\t"):
        if name:
            obj = bpy.data.objects.get(name)
            if obj is not None and obj.type == 'MESH':
                out.append(obj)
    return out


def record_objects(rec):
    out = []
    for name in (rec.surface_a, rec.surface_b, rec.pin_a, rec.pin_b):
        if name:
            obj = bpy.data.objects.get(name)
            if obj is not None:
                out.append(obj)
    out.extend(extra_pin_objects(rec))
    return out


def unique_record_name(settings, prefix):
    names = {r.name for r in settings.cuts}
    n = 1
    while f"{prefix} {n:03d}" in names:
        n += 1
    return f"{prefix} {n:03d}"


def object_joint_contact(context, target, cutter_objs, gap_bu=0.0):
    """Build a preview ContactData for an Object Joint between `target` and `cutter_objs`.

    Computes the intersection volume (or closest contact patch) between `target` and the
    combined `cutter_objs` so the user gets a green preview of the mating zone and an
    automatically positioned orange connector pin.
    """
    import bmesh

    t_state = ensure_evaluable(target)
    c_states = [ensure_evaluable(o) for o in cutter_objs]
    t_me = None
    c_meshes = []
    joined_c = None
    inter_me = None
    try:
        t_me = mesh_utils.world_mesh_copy(context, target, "_esp_jt_target")
        for i, o in enumerate(cutter_objs):
            raw = mesh_utils.world_mesh_copy(context, o, f"_esp_jt_c_{i}")
            if gap_bu > 1e-6:
                bm = surfaces.inflate_bmesh(raw, gap_bu)
                inf = mesh_utils.bmesh_to_mesh(bm, f"_esp_jt_cinf_{i}")
                bm.free()
                mesh_utils.remove_mesh(raw)
                c_meshes.append(inf)
            else:
                c_meshes.append(raw)
        joined_c = mesh_utils.join_meshes(c_meshes, "_esp_jt_c_all")
        c_meshes = []

        inter_me = mesh_utils.boolean_mesh(context, t_me, joined_c, 'INTERSECT', pref(context, "solver", 'AUTO'))

        t_center = mesh_utils.mesh_centroid(t_me)
        c_center = mesh_utils.mesh_centroid(joined_c)
        fallback_n = (t_center - c_center).normalized()
        if fallback_n.length < 1e-6:
            fallback_n = Vector((0.0, 0.0, 1.0))

        # Keep intersection faces belonging to the cutter surface and remove faces on the target surface.
        verts = []
        faces = []
        acc_n = Vector((0.0, 0.0, 0.0))
        if len(inter_me.polygons) > 0:
            bvh_t = mesh_utils.bvh_from_mesh(t_me)
            bm = bmesh.new()
            bm.from_mesh(inter_me)
            bm.faces.ensure_lookup_table()
            skin_tol = max(gap_bu * 0.25, 1e-4)
            drop_faces = []
            for f in bm.faces:
                fc = f.calc_center_median()
                loc, _nor, _i, dist = bvh_t.find_nearest(fc)
                if loc is not None and dist <= skin_tol:
                    # This face lies on the target surface and should not appear in the preview.
                    drop_faces.append(f)
                else:
                    acc_n += f.normal * f.calc_area()
            if len(drop_faces) < len(bm.faces):
                bmesh.ops.delete(bm, geom=drop_faces, context='FACES')
            bm.verts.index_update()
            verts = [v.co.copy() for v in bm.verts]
            faces = [tuple(v.index for v in f.verts) for f in bm.faces]
            bm.free()

        if len(verts) < 3 or len(faces) == 0:
            bvh_t = mesh_utils.bvh_from_mesh(t_me)
            best_p = t_center
            best_n = fallback_n
            best_d = 1e30
            step = max(1, len(joined_c.vertices) // 200)
            for idx in range(0, len(joined_c.vertices), step):
                v = joined_c.vertices[idx]
                loc, nor, _idx, d = bvh_t.find_nearest(v.co)
                if loc is not None and d < best_d:
                    best_d = d
                    best_p = (loc + v.co) * 0.5
                    best_n = -nor if nor.length > 1e-6 else fallback_n
            rad = max(mesh_utils.object_world_diagonal(target) * 0.08, 5.0 * mm(context))
            verts, faces = surfaces.plane_patch(best_p, best_n, surfaces.plane_basis(best_n)[0], rad)
            center_pt = best_p
            normal_vec = best_n
            inscribed = rad * 0.8
        else:
            center_pt = sum(verts, Vector((0.0, 0.0, 0.0))) / len(verts)
            normal_vec = acc_n.normalized() if acc_n.length > 1e-6 else fallback_n
            if normal_vec.dot(fallback_n) < 0.0:
                normal_vec = -normal_vec
            span = max((v - center_pt).length for v in verts) * 2.0
            inscribed = max(span * 0.5, 2.0 * mm(context))

        data = ContactData('OBJECT')
        data.verts = verts
        data.faces = faces
        data.center_hint = center_pt
        data.anchor = center_pt
        data.plane_no = normal_vec
        data.extend = inscribed
        return data
    finally:
        mesh_utils.remove_mesh(t_me)
        for m in c_meshes:
            mesh_utils.remove_mesh(m)
        mesh_utils.remove_mesh(joined_c)
        mesh_utils.remove_mesh(inter_me)
        restore_visibility(target, t_state)
        for o, st in zip(cutter_objs, c_states):
            restore_visibility(o, st)


# ----------------------------------------------------------------------------
# records
# ----------------------------------------------------------------------------
def add_record(context, target, cut_type, contacts, cutter_objs=None):
    global _suspend
    settings = context.scene.esp
    _suspend += 1
    try:
        if not settings.base_object or bpy.data.objects.get(settings.base_object) is None:
            # the object under the cut is the plan's source, even when it is a part of
            # an earlier build: its `esp_base` points at the model before those cuts,
            # and rooting the plan there would undo every cut already approved
            settings.base_object = target.name
        rec = settings.cuts.add()
        rec.name = unique_record_name(settings, "Base Split" if len(contacts) == 2 else PREFIX[cut_type])
        rec.cut_type = cut_type
        rec.target = target.name
        rec.target_obj = target
        if cutter_objs:
            rec.cutter_objects = "\t".join(o.name for o in cutter_objs)
            rec.cutter_obj = cutter_objs[0] if len(cutter_objs) == 1 else None
        rec.two_contact = len(contacts) == 2
        active = settings.cuts[settings.active_cut] if 0 <= settings.active_cut < len(settings.cuts) - 1 else None
        copy_connector_props(active if active is not None else settings, rec)
        for i, data in enumerate(contacts):
            sobj = create_surface_object(context, f"ESP_Surface_{rec.name}_{'AB'[i]}", data, target)
            _set_contact_attr(rec, "surface", i, sobj.name)
            if cut_type == 'OBJECT' and data.plane_no is not None:
                center = data.center_hint or surfaces.patch_center(data.verts)
                normal = data.plane_no
                inscribed = data.extend if data.extend > 0.0 else mesh_utils.object_world_diagonal(target) * 0.1
            else:
                center, normal, inscribed = contact_frame(
                    context,
                    target,
                    data.verts,
                    data.faces,
                    data.hit,
                    data.through,
                    data.center_hint,
                    data.is_cut_face,
                )
            _set_contact_attr(rec, "center", i, center)
            _set_contact_attr(rec, "normal", i, normal)
            _set_contact_attr(rec, "inscribed", i, inscribed)
        apply_preset(context, rec)
        apply_fit(context, rec)
        for i in range(len(contacts)):
            matrix = auto_pin_matrix(context, rec, i)
            pin = create_pin_object(context, f"ESP_Pin_{rec.name}_{'AB'[i]}", rec.shape, matrix)
            _set_contact_attr(rec, "pin", i, pin.name)
            _set_contact_attr(rec, "pin_auto", i, flat(matrix))
            pin.hide_viewport = not rec.add_pin
        rec.anchor = contacts[0].anchor
        settings.active_cut = len(settings.cuts) - 1
    finally:
        _suspend -= 1
    return rec


def on_record_settings_changed(context, rec):
    global _suspend
    if _suspend:
        return
    _suspend += 1
    try:
        apply_preset(context, rec)
        apply_fit(context, rec)
        for i in range(contact_count(rec)):
            pin = bpy.data.objects.get(_contact_attr(rec, "pin", i))
            if pin is None:
                continue
            if pin.get("esp_shape") != rec.shape:
                update_pin_mesh(pin, rec.shape)
            old_auto = unflat(_contact_attr(rec, "pin_auto", i))
            new_auto = auto_pin_matrix(context, rec, i)
            delta = user_delta(old_auto, pin.matrix_basis)
            pin.matrix_world = _frame(new_auto) @ delta @ _scale(new_auto)
            _set_contact_attr(rec, "pin_auto", i, flat(new_auto))
            pin.hide_viewport = not (rec.add_pin and rec.show)
        for ep in extra_pin_objects(rec):
            if ep.get("esp_shape") != rec.shape:
                update_pin_mesh(ep, rec.shape)
            ep.hide_viewport = not (rec.add_pin and rec.show)
    finally:
        _suspend -= 1


def _is_identity(m, tol=1e-5):
    ident = Matrix.Identity(4)
    return all(abs(m[i][j] - ident[i][j]) < tol for i in range(4) for j in range(4))


def _frame(m):
    """Rotation + translation part of a matrix (scale removed)."""
    return m.normalized()


def _scale(m):
    s = m.to_scale()
    return Matrix.Diagonal((s.x, s.y, s.z, 1.0))


def user_delta(auto, current):
    """What the user did to the pin, expressed in the pin's unscaled local frame.

    auto = F @ S  (frame @ scale); current = F @ D @ S  ->  D = F^-1 @ current @ S^-1
    Identity when the pin was never touched. Survives size/side/shape changes.
    """
    d = _frame(auto).inverted_safe() @ current @ _scale(auto).inverted_safe()
    return Matrix.Identity(4) if _is_identity(d) else d


def user_moved_pin(rec, i):
    pin = bpy.data.objects.get(_contact_attr(rec, "pin", i))
    if pin is None:
        return False
    return not _is_identity(user_delta(unflat(_contact_attr(rec, "pin_auto", i)), pin.matrix_basis))


def reset_pins(context, rec):
    """Reset primary connector to default transform and remove extra pins."""
    global _suspend
    _suspend += 1
    try:
        for ep in extra_pin_objects(rec):
            mesh_utils.remove_object(ep)
        if "esp_extra_pins" in rec:
            del rec["esp_extra_pins"]
        for i in range(contact_count(rec)):
            pin = bpy.data.objects.get(_contact_attr(rec, "pin", i))
            if pin is None:
                continue
            update_pin_mesh(pin, rec.shape)
            auto = auto_pin_matrix(context, rec, i)
            pin.matrix_world = auto
            _set_contact_attr(rec, "pin_auto", i, flat(auto))
    finally:
        _suspend -= 1


def refresh_record_frames(context, rec):
    """Re-estimate centre/normal/size from the (possibly edited) cut surfaces."""
    global _suspend
    if _suspend:
        return
    # Auto-heal if the user manually deleted the 2nd contact object in the viewport
    if rec.two_contact and (not rec.surface_b or bpy.data.objects.get(rec.surface_b) is None):
        pin_b = bpy.data.objects.get(rec.pin_b) if rec.pin_b else None
        if pin_b is not None:
            mesh_utils.remove_object(pin_b)
        rec.surface_b = ""
        rec.pin_b = ""
        rec.two_contact = False

    sync_record_pointers(rec)
    target = record_target(rec)
    if target is None:
        return
    _suspend += 1
    try:
        for i in range(contact_count(rec)):
            sobj = bpy.data.objects.get(_contact_attr(rec, "surface", i))
            if sobj is None:
                continue
            if rec.cut_type == 'OBJECT':
                cutters = cutter_object_list(rec)
                if cutters and target:
                    gap_bu = rec.cut_gap_mm * mm(context)
                    data = object_joint_contact(context, target, cutters, gap_bu)
                    if data.verts and data.faces:
                        origin = sobj.matrix_world.translation.copy()
                        me = sobj.data
                        me.clear_geometry()
                        me.from_pydata([tuple(Vector(v) - origin) for v in data.verts], [], [tuple(f) for f in data.faces])
                        me.update()
                        _set_contact_attr(rec, "center", i, data.center_hint or surfaces.patch_center(data.verts))
                        _set_contact_attr(rec, "normal", i, data.plane_no or Vector((0.0, 0.0, 1.0)))
                        _set_contact_attr(rec, "inscribed", i, data.extend if data.extend > 0.0 else 5.0 * mm(context))
                        rec.anchor = _contact_attr(rec, "center", i)
                else:
                    verts, faces = surface_world_patch(sobj, context, eval_modifiers=False)
                    if verts:
                        _set_contact_attr(rec, "center", i, surfaces.patch_center(verts))
                sobj["esp_mw"] = flat(sobj.matrix_basis)
                continue
            if len(sobj.modifiers) > 0:
                sobj["esp_custom_mesh"] = True
            is_custom = bool(sobj.get("esp_custom_mesh"))
            if not is_custom:
                rebuild_surface(sobj, context=context, target=target)
            # Do not call evaluated_get inside automatic scene updates to avoid triggering depsgraph!
            verts, faces = surface_world_patch(sobj, context, eval_modifiers=False)
            if is_custom:
                hit = None
                through = None
                hint = surfaces.patch_center(verts) if verts else None
                cut_face = True
            else:
                hit = Vector(sobj["esp_hit"]) if "esp_hit" in sobj else None
                through = Vector(sobj["esp_through"]) if "esp_through" in sobj else None
                hint = Vector(sobj["esp_center_hint"]) if "esp_center_hint" in sobj else None
                if hit is not None and sobj.get("esp_kind") == 'STRAIGHT':
                    hint, hit = _reanchor_plane(verts, faces, hit, through, hint)
                cut_face = bool(sobj.get("esp_cut_face", False))
            try:
                center, normal, inscribed = contact_frame(
                    context,
                    target,
                    verts,
                    faces,
                    hit,
                    through,
                    hint,
                    cut_face,
                )
            except Exception:
                continue
            _set_contact_attr(rec, "center", i, center)
            _set_contact_attr(rec, "normal", i, normal)
            _set_contact_attr(rec, "inscribed", i, inscribed)
            sobj["esp_mw"] = flat(sobj.matrix_basis)
    finally:
        _suspend -= 1
    on_record_settings_changed(context, rec)


def _reanchor_plane(verts, faces, hit, through, hint):
    n = surfaces.patch_normal(verts, faces)
    c = surfaces.patch_center(verts)
    d = Vector(through) if through is not None else n
    denom = d.dot(n)
    if abs(denom) < 1e-6:
        return c, None
    t = (c - hit).dot(n) / denom
    return None, hit + d * t


def set_record_visibility(rec, show):
    for i in range(contact_count(rec)):
        s = bpy.data.objects.get(_contact_attr(rec, "surface", i))
        if s is not None:
            s.hide_viewport = not show
        p = bpy.data.objects.get(_contact_attr(rec, "pin", i))
        if p is not None:
            p.hide_viewport = not (show and rec.add_pin)
    for ep in extra_pin_objects(rec):
        ep.hide_viewport = not (show and rec.add_pin)


def on_active_changed(context):
    settings = context.scene.esp
    if not (0 <= settings.active_cut < len(settings.cuts)):
        return
    rec = settings.cuts[settings.active_cut]
    sync_record_pointers(rec)
    col = bpy.data.collections.get(PLAN_COLLECTION)
    if col is None:
        return
    for obj in col.objects:
        if obj is None:
            continue
        kind = obj.get("esp_preview")
        if kind in ('pin', 'surface'):
            obj.color = ORANGE if kind == 'pin' else GREEN
    for obj in record_objects(rec):
        obj.color = (1.0, 0.85, 0.2, 0.9) if obj.get("esp_preview") == 'pin' else (0.5, 1.0, 0.55, 0.5)


def remove_record(context, index):
    settings = context.scene.esp
    if not (0 <= index < len(settings.cuts)):
        return
    rec = settings.cuts[index]
    for obj in record_objects(rec):
        mesh_utils.remove_object(obj)
    settings.cuts.remove(index)
    settings.active_cut = min(index, len(settings.cuts) - 1)


def remove_all_records(context):
    settings = context.scene.esp
    while len(settings.cuts):
        remove_record(context, len(settings.cuts) - 1)
    col = bpy.data.collections.get(PLAN_COLLECTION)
    if col is not None:
        for obj in list(col.objects):
            mesh_utils.remove_object(obj)
        bpy.data.collections.remove(col)
    settings.active_cut = -1


def set_plan_hidden(context, hidden):
    col = bpy.data.collections.get(PLAN_COLLECTION)
    if col is not None:
        col.hide_viewport = hidden


def record_spec(context, rec, settings, remesh=True):
    apply_fit(context, rec)
    contacts = []
    shape, custom = connectors.resolve_shape(rec.shape)
    target = record_target(rec)
    if target is None:
        raise cutting.CutError(f"Target object '{rec.target or 'None'}' of '{rec.name}' is missing")
    for i in range(contact_count(rec)):
        sobj = bpy.data.objects.get(_contact_attr(rec, "surface", i))
        if sobj is None:
            raise cutting.CutError(f"Preview surface of '{rec.name}' is missing")
        preview_patch = surface_world_patch(sobj, context, eval_modifiers=True)
        if sobj.get("esp_custom_mesh"):
            diag = mesh_utils.object_world_diagonal(target) if target is not None else 1.0
            margin = max(float(sobj.get("esp_margin", 0.0)), diag * 0.05, 2.0 * mm(context))
            verts, faces = custom_mesh_cutter(context, target, preview_patch[0], preview_patch[1], margin)
        else:
            verts, faces = cutter_world_patch(sobj, context)
        pin = bpy.data.objects.get(_contact_attr(rec, "pin", i))
        pm = pin.matrix_basis.copy() if (rec.add_pin and pin is not None) else None
        contacts.append(
            cutting.ContactSpec(
                verts,
                faces,
                rec.add_pin,
                pm,
                shape,
                custom,
                pin_mesh=pin_unit_mesh(pin) if pm is not None else None,
                regions_skipped=int(sobj.get("esp_skipped", 0)),
                preview=preview_patch,
            )
        )
    # Add all extra pins so Build carves matching sockets for them
    if rec.add_pin:
        for ep in extra_pin_objects(rec):
            contacts.append(
                cutting.ContactSpec(
                    [],
                    [],
                    True,
                    ep.matrix_basis.copy(),
                    shape,
                    custom,
                    pin_mesh=pin_unit_mesh(ep),
                )
            )
    u = mm(context)
    return cutting.CutSpec(
        contacts=contacts,
        gap=rec.cut_gap_mm * u,
        clearance=rec.clearance_mm * u,
        tip_extra=(rec.tip_extra_mm * u) if rec.asymmetric else 0.0,
        pin_side=rec.pin_side,
        solver=pref(context, "solver", 'AUTO'),
        remesh=remesh and settings.remesh_enable,
        remesh_voxel=settings.remesh_voxel_mm * u,
        remesh_adaptivity=settings.remesh_adaptivity,
        remesh_smooth=settings.remesh_smooth,
        diagnose=settings.diagnose_failed,
    )


def quick_spec(context, target, contacts):
    """CutSpec for Quick mode straight from the tool output and the scene defaults."""
    settings = context.scene.esp
    apply_fit(context, settings)
    shape, custom = connectors.resolve_shape(settings.shape)
    specs = []
    for data in contacts:
        pm = None
        if settings.add_pin:
            center, normal, inscribed = contact_frame(
                context,
                target,
                data.verts,
                data.faces,
                data.hit,
                data.through,
                data.center_hint,
                data.is_cut_face,
            )
            w, h = contact_size_bu(context, settings, inscribed)
            pm = connectors.connector_matrix(center, cutting.protrude_direction(normal, settings.pin_side), w, h)
        verts, faces = data.cutter if data.cutter is not None else (data.verts, data.faces)
        specs.append(
            cutting.ContactSpec(
                verts,
                faces,
                settings.add_pin,
                pm,
                shape,
                custom,
                regions_skipped=data.regions_skipped,
                preview=(data.verts, data.faces),
            )
        )
    u = mm(context)
    return cutting.CutSpec(
        contacts=specs,
        gap=settings.cut_gap_mm * u,
        clearance=settings.clearance_mm * u,
        tip_extra=(settings.tip_extra_mm * u) if settings.asymmetric else 0.0,
        pin_side=settings.pin_side,
        solver=pref(context, "solver", 'AUTO'),
        diagnose=settings.diagnose_failed,
    )


def mirror_matrix_world(context, target):
    """Build the world-space reflection matrix for the current mirror settings."""
    s = context.scene.esp
    axis_idx = {'X': 0, 'Y': 1, 'Z': 2}[s.mirror_axis]
    scale_vec = [1.0, 1.0, 1.0, 1.0]
    scale_vec[axis_idx] = -1.0
    refl = Matrix.Diagonal(scale_vec)

    pivot = s.mirror_pivot
    if pivot == 'TARGET' and target is not None:
        frame = Matrix.Translation(target.matrix_world.translation) @ target.matrix_world.to_3x3().normalized().to_4x4()
    elif pivot == 'OBJECT' and s.mirror_object is not None:
        mo = s.mirror_object
        frame = Matrix.Translation(mo.matrix_world.translation) @ mo.matrix_world.to_3x3().normalized().to_4x4()
    elif pivot == 'CURSOR':
        frame = Matrix.Translation(context.scene.cursor.location)
    else:
        frame = Matrix.Identity(4)
    return frame @ refl @ frame.inverted_safe()


def mirror_pin_matrix(M, pin_mat):
    """Accurately mirror connector world matrix across symmetry plane without negative scaling."""
    loc = M @ pin_mat.translation
    M3 = M.to_3x3()
    rot = pin_mat.to_3x3().normalized()
    sx, sy, sz = pin_mat.to_scale()

    # Reflect pin axis (+Z) and up axis (+Y), then reconstruct X via cross product
    # to maintain a right-handed basis (det = +1) and avoid inverted mesh normals during booleans:
    z_ax = (M3 @ rot.col[2]).normalized()
    y_ax = (M3 @ rot.col[1]).normalized()
    x_ax = y_ax.cross(z_ax)
    if x_ax.length < 1e-6:
        x_ax, _ = surfaces.plane_basis(z_ax)
    else:
        x_ax.normalize()
    y_ax = z_ax.cross(x_ax).normalized()

    frame = Matrix(
        (
            (x_ax.x, y_ax.x, z_ax.x, loc.x),
            (x_ax.y, y_ax.y, z_ax.y, loc.y),
            (x_ax.z, y_ax.z, z_ax.z, loc.z),
            (0.0, 0.0, 0.0, 1.0),
        )
    )
    return frame @ Matrix.Diagonal((abs(sx), abs(sy), abs(sz), 1.0))


def mirror_contact_from_surface(context, target, sobj):
    """Create a mirrored `ContactData` from an existing preview surface object `sobj`."""
    s = context.scene.esp
    M = mirror_matrix_world(context, target)
    M3 = M.to_3x3()
    kind = sobj.get("esp_kind", 'STRAIGHT')
    mw = sobj.matrix_basis
    data = ContactData(kind)
    data.detail = int(sobj.get("esp_detail", surfaces.SURFACE_DETAIL))
    data.extend = float(sobj.get("esp_extend", 0.0))
    data.margin = float(sobj.get("esp_margin", 0.0))
    data.view_dir = (M3 @ Vector(sobj.get("esp_view", (0.0, 0.0, -1.0)))).normalized()

    if "esp_hit" in sobj:
        data.hit = M @ Vector(sobj["esp_hit"])
        data.through = (M3 @ Vector(sobj["esp_through"])).normalized()
        data.anchor = data.hit
    if "esp_center_hint" in sobj:
        data.center_hint = M @ Vector(sobj["esp_center_hint"])
        data.anchor = data.center_hint

    if sobj.get("esp_custom_mesh"):
        verts, faces = surface_world_patch(sobj, context, eval_modifiers=True)
        data.verts = [M @ v for v in verts]
        data.faces = [tuple(reversed(f)) for f in faces]
        data.is_cut_face = True
        data.hit = None
        data.through = None
        diag = mesh_utils.object_world_diagonal(target) if target is not None else 1.0
        margin = max(float(sobj.get("esp_margin", 0.0)), diag * 0.05, 2.0 * mm(context))
        data.margin = margin
        if data.verts:
            data.center_hint = sum(data.verts, Vector((0.0, 0.0, 0.0))) / len(data.verts)
            data.anchor = data.center_hint
        data.cutter = custom_mesh_cutter(context, target, data.verts, data.faces, margin)
        return data

    pts_world = [M @ (mw @ p) for p in surface_points(sobj)]
    if s.mirror_snap_surface and target is not None and pts_world:
        state = ensure_evaluable(target)
        try:
            depsgraph = context.evaluated_depsgraph_get()
            snapped = []
            for p in pts_world:
                ok, loc, _nor, _d = mesh_utils.object_closest_point(target, p, depsgraph)
                snapped.append(loc if ok else p)
            pts_world = snapped
        finally:
            restore_visibility(target, state)

    if kind == 'FREEHAND':
        # Reflection flips loop winding; reverse to keep consistent normal orientation
        pts_world.reverse()
        n = surfaces.newell_normal(pts_world)
        if n.z < -1e-6 or (abs(n.z) <= 1e-6 and n.x < 0):
            pts_world.reverse()
        data.points = pts_world
        data.verts, data.faces, data.cutter = loop_surface(context, target, pts_world, data.detail, data.margin)
        data.is_cut_face = True
        data.center_hint = sum(pts_world, Vector((0.0, 0.0, 0.0))) / len(pts_world)
        data.anchor = pts_world[0]
    elif kind == 'CURVED':
        pts_world.reverse()
        data.points = pts_world
        built = ribbon_surfaces(context, target, pts_world, data.view_dir, data.extend, data.detail)
        if built is not None:
            data.verts, data.faces = built[0]
            data.cutter = built[1]
            data.depth_range = built[2]
            data.is_cut_face = True
        else:
            verts, faces = surface_world_patch(sobj)
            data.verts = [M @ v for v in verts]
            data.faces = [tuple(reversed(f)) for f in faces]
    else:  # STRAIGHT
        verts, faces = surface_world_patch(sobj, context, eval_modifiers=True)
        mir_verts = [M @ v for v in verts]
        mir_faces = [tuple(reversed(f)) for f in faces]

        if "esp_plane_no" in sobj and "esp_plane_co" in sobj:
            rot = mw.to_3x3()
            orig_no = (rot.inverted_safe().transposed() @ Vector(sobj["esp_plane_no"])).normalized()
            orig_co = mw @ Vector(sobj["esp_plane_co"])
            co = M @ orig_co
            no = (M3 @ orig_no).normalized()
            if no.z < -1e-6 or (abs(no.z) <= 1e-6 and no.x < 0):
                no = -no
            ref = M @ (mw @ Vector(sobj["esp_ref"])) if "esp_ref" in sobj else co
            span = None
            if "esp_span_u" in sobj and "esp_span" in sobj:
                lo, hi = sobj["esp_span"]
                orig_u = (rot @ Vector(sobj["esp_span_u"])).normalized()
                mir_u = (M3 @ orig_u).normalized()
                span = (mir_u, lo, hi)
            data.plane_co = co
            data.plane_no = no
            data.anchor = ref
            data.center_hint = co
            patch = straight_section(context, target, co, no, span, ref)
            if patch is not None:
                fill_straight_contact(data, patch, span)
                return data

        if mir_verts and mir_faces:
            co = surfaces.patch_center(mir_verts)
            no = surfaces.patch_normal(mir_verts, mir_faces)
            if no.z < -1e-6 or (abs(no.z) <= 1e-6 and no.x < 0):
                no = -no
                mir_faces = [tuple(reversed(f)) for f in mir_faces]
            data.plane_co = co
            data.plane_no = no
            data.anchor = co
            data.center_hint = co

            if "esp_cutter" in sobj:
                c_verts, c_faces = cutter_world_patch(sobj, context)
                mir_c_verts = [M @ v for v in c_verts]
                mir_c_faces = [tuple(reversed(f)) for f in c_faces]
                data.verts = mir_verts
                data.faces = mir_faces
                data.cutter = (mir_c_verts, mir_c_faces)
                data.is_cut_face = True
                return data

            diag = mesh_utils.object_world_diagonal(target) if target is not None else 1.0
            margin = max(diag * 0.05, 2.0 * mm(context))
            data.verts = mir_verts
            data.faces = mir_faces
            data.cutter = custom_mesh_cutter(context, target, mir_verts, mir_faces, margin)
            data.is_cut_face = True
            return data

        data.verts = mir_verts
        data.faces = mir_faces
        return data
    return data


# ----------------------------------------------------------------------------
# follow moved cut planes (G/R/S on the preview) -> re-place the pin
# ----------------------------------------------------------------------------
_dirty = {}  # surface object name -> the transform it carried when it was marked


def _refresh_dirty():
    """Re-place the pin (and, for a plane cut, re-take the section) once the drag stops.

    Re-sectioning the model is far too heavy to run on every mouse move of a G, so the
    timer keeps re-arming itself while the preview is still moving and only does the
    work once its transform has held still for a tick.
    """
    global _dirty
    scene = bpy.context.scene
    if scene is None:
        _dirty = {}
        return None
    moving = False
    for name, seen in list(_dirty.items()):
        obj = bpy.data.objects.get(name)
        if obj is None:
            continue
        cur = tuple(flat(obj.matrix_basis))
        if cur != seen:
            _dirty[name] = cur
            moving = True
    if moving:
        return 0.1
    names = list(_dirty)
    _dirty = {}
    for rec in scene.esp.cuts:
        if rec.surface_a in names or rec.surface_b in names:
            try:
                refresh_record_frames(bpy.context, rec)
            except Exception:
                pass
    return None


@persistent
def depsgraph_handler(scene, depsgraph):
    global _suspend
    if _suspend:
        return
    # Never trigger surface rebuilds while the user is in Edit/Sculpt mode or tweaking modifiers!
    active_obj = getattr(bpy.context, "active_object", None)
    if active_obj is not None and active_obj.mode != 'OBJECT':
        return
    settings = getattr(scene, "esp", None)
    if settings is None or not len(settings.cuts):
        return

    # Clicking preview in 3D viewport automatically activates corresponding cut
    if active_obj is not None and active_obj.get("esp_preview"):
        hit_idx = -1
        for idx, r in enumerate(settings.cuts):
            if active_obj.name in (r.surface_a, r.surface_b, r.pin_a, r.pin_b):
                hit_idx = idx
                break
        if hit_idx >= 0 and settings.active_cut != hit_idx:
            _suspend += 1
            try:
                settings.active_cut = hit_idx
            finally:
                _suspend -= 1

    changed = False
    for rec in settings.cuts:
        for name in (rec.surface_a, rec.surface_b):
            if not name:
                continue
            obj = bpy.data.objects.get(name)
            if obj is None or obj.get("esp_custom_mesh") or len(obj.modifiers) > 0:
                continue
            cur = flat(obj.matrix_basis)
            old = obj.get("esp_mw")
            if old is None or any(abs(a - b) > 1e-6 for a, b in zip(cur, old)):
                obj["esp_mw"] = cur
                if old is not None:
                    _dirty[name] = tuple(cur)
                    changed = True
    if changed and not bpy.app.timers.is_registered(_refresh_dirty):
        bpy.app.timers.register(_refresh_dirty, first_interval=0.05)


def _sync_all_scenes():
    try:
        scenes = getattr(bpy.data, "scenes", None)
        if scenes is None:
            return None
        for scene in scenes:
            settings = getattr(scene, "esp", None)
            if settings is not None:
                for rec in settings.cuts:
                    sync_record_pointers(rec)
    except (AttributeError, ReferenceError):
        pass
    return None


@persistent
def _load_post(*_args):
    _sync_all_scenes()


def register():
    if depsgraph_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(depsgraph_handler)
    if _load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_load_post)
    if not bpy.app.timers.is_registered(_sync_all_scenes):
        bpy.app.timers.register(_sync_all_scenes, first_interval=0.05)


def unregister():
    if depsgraph_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(depsgraph_handler)
    if _load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_post)
    if bpy.app.timers.is_registered(_sync_all_scenes):
        bpy.app.timers.unregister(_sync_all_scenes)
    section.free_cache()
