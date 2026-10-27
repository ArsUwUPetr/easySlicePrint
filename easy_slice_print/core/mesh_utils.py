# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Rafael Omodei and EasySlice Print contributors
"""Low level mesh helpers: temp objects, booleans, loose parts, centroids."""

import bmesh
import bpy
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree

TEMP_COLLECTION = "_ESP_TEMP"


# ----------------------------------------------------------------------------
# temp objects
# ----------------------------------------------------------------------------
def temp_collection(scene):
    col = bpy.data.collections.get(TEMP_COLLECTION)
    if col is None:
        col = bpy.data.collections.new(TEMP_COLLECTION)
    if col.name not in scene.collection.children:
        scene.collection.children.link(col)
    return col


def new_temp_object(scene, mesh, name):
    obj = bpy.data.objects.new(name, mesh)
    temp_collection(scene).objects.link(obj)
    return obj


def remove_object(obj, remove_data=True):
    try:
        data = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
    except ReferenceError:
        return
    if remove_data and data is not None and data.users == 0 and isinstance(data, bpy.types.Mesh):
        bpy.data.meshes.remove(data)


def cleanup_temp(scene):
    col = bpy.data.collections.get(TEMP_COLLECTION)
    if col is None:
        return
    for obj in list(col.objects):
        remove_object(obj)
    bpy.data.collections.remove(col)


def remove_mesh(mesh):
    if mesh is None:
        return
    try:
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    except ReferenceError:
        # already gone (a cancelled cut cleans up meshes the pipeline consumed)
        pass


# ----------------------------------------------------------------------------
# evaluated copies
# ----------------------------------------------------------------------------
def evaluated_mesh_copy(context, obj, name):
    depsgraph = context.evaluated_depsgraph_get()
    eval_obj = obj.evaluated_get(depsgraph)
    me = bpy.data.meshes.new_from_object(eval_obj, preserve_all_data_layers=False, depsgraph=depsgraph)
    me.name = name
    return me


def world_mesh_copy(context, obj, name):
    """Evaluated mesh of `obj` with the world matrix applied."""
    me = evaluated_mesh_copy(context, obj, name)
    me.transform(obj.matrix_world)
    return me


def mesh_from_pydata(name, verts, faces):
    me = bpy.data.meshes.new(name)
    me.from_pydata([tuple(v) for v in verts], [], [tuple(f) for f in faces])
    me.update()
    return me


def bmesh_to_mesh(bm, name):
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    me.update()
    return me


# ----------------------------------------------------------------------------
# booleans
# ----------------------------------------------------------------------------
def available_solvers():
    try:
        prop = bpy.types.BooleanModifier.bl_rna.properties['solver']
        return [e.identifier for e in prop.enum_items]
    except Exception:
        return ['EXACT']


def resolve_solver(requested):
    ids = available_solvers()
    if requested == 'AUTO':
        return 'MANIFOLD' if 'MANIFOLD' in ids else 'EXACT'
    return requested if requested in ids else 'EXACT'


def boolean_mesh(context, base_mesh, cutter_mesh, operation, solver='AUTO'):
    """Return a NEW mesh = base_mesh <operation> cutter_mesh (both in the same space)."""
    scene = context.scene
    base = new_temp_object(scene, base_mesh, "_esp_bool_base")
    cutter = new_temp_object(scene, cutter_mesh, "_esp_bool_cutter")
    result = None
    try:
        ids = available_solvers()
        first = resolve_solver(solver)
        chain = [first]
        # Always add fallback solvers (EXACT, FLOAT/FAST, MANIFOLD) if the primary solver fails
        for fallback in ('EXACT', 'FLOAT', 'FAST', 'MANIFOLD'):
            if fallback in ids and fallback not in chain:
                chain.append(fallback)

        base_v = len(base_mesh.vertices)
        base_p = len(base_mesh.polygons)
        base_bounds = mesh_bounds(base_mesh) if base_v > 0 else None

        for sv in chain:
            mod = base.modifiers.new("esp_bool", 'BOOLEAN')
            mod.operation = operation
            mod.object = cutter
            mod.solver = sv
            if hasattr(mod, "use_self"):
                mod.use_self = False
            if hasattr(mod, "use_hole_tolerant"):
                mod.use_hole_tolerant = True
            context.view_layer.update()
            me = evaluated_mesh_copy(context, base, base_mesh.name + "_bool")
            base.modifiers.remove(mod)

            unchanged = len(me.polygons) == base_p and len(me.vertices) == base_v
            if unchanged and base_v > 0 and len(me.vertices) > 0:
                # In case vertex and polygon counts coincidentally match, check bounding box
                b_new = mesh_bounds(me)
                if (b_new[0] - base_bounds[0]).length > 1e-5 or (b_new[1] - base_bounds[1]).length > 1e-5:
                    unchanged = False

            if (len(me.polygons) > 0 and not unchanged) or (operation == 'INTERSECT' and not unchanged):
                result = me
                break
            remove_mesh(me)
        if result is None:
            result = bpy.data.meshes.new(base_mesh.name + "_bool")
    finally:
        remove_object(base, remove_data=False)
        remove_object(cutter, remove_data=False)
    return result


# ----------------------------------------------------------------------------
# loose parts / join
# ----------------------------------------------------------------------------
def separate_loose_meshes(context, mesh):
    """Split `mesh` into loose shells in O(N) without mode switching. Consumes `mesh`."""
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    n_faces = len(bm.faces)
    if n_faces == 0:
        bm.free()
        remove_mesh(mesh)
        return []

    seen = np.zeros(n_faces, dtype=bool)
    comps = []
    for f in bm.faces:
        if seen[f.index]:
            continue
        seen[f.index] = True
        stack = [f]
        comp = []
        while stack:
            cf = stack.pop()
            comp.append(cf)
            for e in cf.edges:
                for lf in e.link_faces:
                    if not seen[lf.index]:
                        seen[lf.index] = True
                        stack.append(lf)
        comps.append(comp)

    if len(comps) == 1:
        bm.free()
        return [mesh]

    base_name = mesh.name
    meshes = []
    for comp in comps:
        sub = bmesh.new()
        v_map = {}
        for f in comp:
            f_verts = []
            for v in f.verts:
                sv = v_map.get(v.index)
                if sv is None:
                    sv = sub.verts.new(v.co)
                    v_map[v.index] = sv
                f_verts.append(sv)
            try:
                nf = sub.faces.new(f_verts)
                nf.smooth = f.smooth
                nf.material_index = f.material_index
            except ValueError:
                pass
        meshes.append(bmesh_to_mesh(sub, base_name + "_part"))
        sub.free()

    bm.free()
    remove_mesh(mesh)
    return meshes


def join_meshes(meshes, name):
    """Join several meshes into one NEW mesh (inputs are removed)."""
    bm = bmesh.new()
    for me in meshes:
        bm.from_mesh(me)
    out = bmesh_to_mesh(bm, name)
    bm.free()
    for me in meshes:
        remove_mesh(me)
    return out


# ----------------------------------------------------------------------------
# measurements
# ----------------------------------------------------------------------------
def mesh_vertex_array(mesh):
    n = len(mesh.vertices)
    arr = np.empty(n * 3, dtype=np.float32)
    mesh.vertices.foreach_get('co', arr)
    return arr.reshape(n, 3)


def mesh_centroid(mesh):
    if len(mesh.vertices) == 0:
        return Vector((0.0, 0.0, 0.0))
    arr = mesh_vertex_array(mesh)
    return Vector(arr.mean(axis=0).tolist())


def mesh_bounds(mesh):
    arr = mesh_vertex_array(mesh)
    if len(arr) == 0:
        z = Vector((0, 0, 0))
        return z, z
    return Vector(arr.min(axis=0).tolist()), Vector(arr.max(axis=0).tolist())


def object_world_bounds(obj):
    pts = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    mn = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    mx = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return mn, mx


def object_world_diagonal(obj):
    mn, mx = object_world_bounds(obj)
    return max((mx - mn).length, 1e-6)


def mesh_volume(mesh):
    bm = bmesh.new()
    bm.from_mesh(mesh)
    v = bm.calc_volume(signed=False)
    bm.free()
    return v


def manifold_report(mesh, limit=None):
    """Return (non_manifold_edges, boundary_edges, total_edges)."""
    bm = bmesh.new()
    bm.from_mesh(mesh)
    non = 0
    boundary = 0
    for i, e in enumerate(bm.edges):
        if limit is not None and i >= limit:
            break
        if e.is_boundary:
            boundary += 1
        elif not e.is_manifold:
            non += 1
    total = len(bm.edges)
    bm.free()
    return non, boundary, total


def bvh_from_mesh(mesh):
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bvh = BVHTree.FromBMesh(bm)
    bm.free()
    return bvh


def bvh_from_pydata(verts, faces):
    return BVHTree.FromPolygons([Vector(v) for v in verts], [tuple(f) for f in faces])


# ----------------------------------------------------------------------------
# object ray helpers (C accelerated, work on the evaluated object)
# ----------------------------------------------------------------------------
def object_surface_depth(obj, point_w, depsgraph=None):
    """Where `point_w` sits relative to the object's surface, in WORLD space.

    -> (found, surface_point_w, outward_normal_w, signed_depth); the depth is positive
    outside the model and negative inside it, which is what tells a cut surface whether
    its rim actually leaves the material or is still buried in it.
    """
    ev = obj.evaluated_get(depsgraph) if depsgraph is not None else obj
    mw = ev.matrix_world
    inv = mw.inverted_safe()
    found, loc, nor, _idx = ev.closest_point_on_mesh(inv @ Vector(point_w))
    if not found:
        return False, None, None, 0.0
    loc_w = mw @ loc
    nor_w = (mw.to_3x3().inverted_safe().transposed() @ nor).normalized()
    return True, loc_w, nor_w, (Vector(point_w) - loc_w).dot(nor_w)


def object_ray_cast(obj, origin_w, direction_w, depsgraph=None, max_dist=1e30):
    """Ray cast against a single object in WORLD space -> (hit, loc_w, normal_w, dist)."""
    inv = obj.matrix_world.inverted_safe()
    o = inv @ origin_w
    d = inv.to_3x3() @ direction_w
    if d.length == 0:
        return False, None, None, 0.0
    d.normalize()
    if depsgraph is not None:
        hit, loc, nor, idx = obj.ray_cast(o, d, distance=max_dist, depsgraph=depsgraph)
    else:
        hit, loc, nor, idx = obj.ray_cast(o, d, distance=max_dist)
    if not hit:
        return False, None, None, 0.0
    loc_w = obj.matrix_world @ loc
    nor_w = (obj.matrix_world.to_3x3().inverted_safe().transposed() @ nor).normalized()
    return True, loc_w, nor_w, (loc_w - origin_w).length


def object_ray_hits(obj, origin_w, direction_w, eps, depsgraph=None, max_dist=1e30, limit=64):
    """Distance of EVERY surface crossing along a ray, in world space.

    `object_ray_cast` stops at the first hit; this marches `eps` past each hit to
    collect the whole set, which is what "how deep does the model go here" needs.
    """
    o = Vector(origin_w)
    d = Vector(direction_w).normalized()
    out = []
    start = o
    for _ in range(limit):
        travelled = (start - o).length
        if travelled >= max_dist:
            break
        hit, loc, _n, _dist = object_ray_cast(obj, start, d, depsgraph, max_dist=max_dist - travelled)
        if not hit:
            break
        out.append((loc - o).length)
        start = loc + d * eps
    return out


def object_closest_point(obj, point_w, depsgraph=None):
    inv = obj.matrix_world.inverted_safe()
    p = inv @ point_w
    if depsgraph is not None:
        ok, loc, nor, idx = obj.closest_point_on_mesh(p, depsgraph=depsgraph)
    else:
        ok, loc, nor, idx = obj.closest_point_on_mesh(p)
    if not ok:
        return False, None, None, 0.0
    loc_w = obj.matrix_world @ loc
    nor_w = (obj.matrix_world.to_3x3().inverted_safe().transposed() @ nor).normalized()
    return True, loc_w, nor_w, (loc_w - point_w).length
