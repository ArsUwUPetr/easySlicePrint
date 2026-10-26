# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Rafael Omodei and EasySlice Print contributors
"""Exploded view, export, connector library, mesh check."""

import os
import re

import bpy
from mathutils import Vector

from . import plan
from .core import connectors, mesh_utils

DOCS_URL = "https://github.com/rafaelomodei/easySlicePrint#readme"


def part_objects(context):
    """Selected parts, else every part of the last build, else selected meshes."""
    sel = [o for o in context.selected_objects if o.type == 'MESH' and o.get("esp_part")]
    if sel:
        return sel
    s = context.scene.esp
    col = bpy.data.collections.get(s.built_collection) if s.built_collection else None
    if col is not None and len(col.objects):
        return [o for o in col.objects if o.type == 'MESH']
    parts = [o for o in context.scene.objects if o.type == 'MESH' and o.get("esp_part") and o.visible_get()]
    if parts:
        return parts
    return [o for o in context.selected_objects if o.type == 'MESH']


def _compute_explode_dirs(context, parts):
    """Compute and cache explode direction vectors once when Explode starts."""
    import numpy as np

    n_parts = len(parts)
    homes = []
    centers = []
    radii = []
    weights = []
    sampled_clouds = []
    base_bboxes = []

    for p in parts:
        home = Vector(p["esp_home"]) if "esp_home" in p else p.location.copy()
        p["esp_home"] = list(home)
        homes.append(home)

        mw_home = p.matrix_world.copy()
        mw_home.translation = home

        arr = mesh_utils.mesh_vertex_array(p.data)
        if len(arr) == 0:
            centers.append(home.copy())
            radii.append(1e-4)
            weights.append(1e-5)
            sampled_clouds.append(np.zeros((1, 3), dtype=np.float32))
            base_bboxes.append((home.copy(), home.copy()))
            continue

        rot_np = np.array(mw_home.to_3x3(), dtype=np.float32)
        loc_np = np.array(home, dtype=np.float32)
        world_arr = arr @ rot_np.T + loc_np

        c_world = Vector(world_arr.mean(axis=0).tolist())
        centers.append(c_world)

        b_min = Vector(world_arr.min(axis=0).tolist())
        b_max = Vector(world_arr.max(axis=0).tolist())
        base_bboxes.append((b_min, b_max))

        diag = b_max - b_min
        r = max(diag.length * 0.5, 1e-4)
        radii.append(r)

        centered = world_arr - world_arr.mean(axis=0)
        rms_sq = float(np.mean(np.sum(centered * centered, axis=1)))
        weights.append(max(rms_sq ** 1.5, diag.x * diag.y * diag.z * 0.1, 1e-5))

        step = max(1, len(world_arr) // 300)
        sampled_clouds.append(world_arr[::step])

    total_w = sum(weights)
    gc = sum((c * w for c, w in zip(centers, weights)), Vector((0.0, 0.0, 0.0))) / (total_w if total_w > 1e-6 else 1.0)
    max_dist = max(((c - gc).length for c in centers), default=1.0) or 1.0

    # 1. Base dimensionless explode directions (multipliers relative to a distance of 1.0 BU).
    dirs = [Vector((0.0, 0.0, 0.0)) for _ in parts]
    for i, c in enumerate(centers):
        d = c - gc
        if d.length > 1e-6:
            # Farther parts move slightly faster (factor 0.7 to 1.6) to create gaps between
            # neighboring parts in a chain.
            tier = 0.7 + 0.9 * (d.length / max_dist)
            dirs[i] = d.normalized() * tier

    # 2. Resolve directions for nested parts whose centroids and base directions are close.
    ref_step = max(max_dist * 0.15, 1e-4)
    for _iteration in range(10):
        any_overlap = False
        for i in range(n_parts):
            off_i = dirs[i] * ref_step
            bmin_i = base_bboxes[i][0] + off_i
            bmax_i = base_bboxes[i][1] + off_i

            for j in range(i + 1, n_parts):
                off_j = dirs[j] * ref_step
                bmin_j = base_bboxes[j][0] + off_j
                bmax_j = base_bboxes[j][1] + off_j

                if (
                    bmin_i.x > bmax_j.x or bmax_i.x < bmin_j.x
                    or bmin_i.y > bmax_j.y or bmax_i.y < bmin_j.y
                    or bmin_i.z > bmax_j.z or bmax_i.z < bmin_j.z
                ):
                    continue

                pts_i = sampled_clouds[i] + np.array(off_i, dtype=np.float32)
                pts_j = sampled_clouds[j] + np.array(off_j, dtype=np.float32)

                diff = pts_i[:, None, :] - pts_j[None, :, :]
                min_dist_sq = float(np.min(np.sum(diff * diff, axis=-1)))
                thresh = (radii[i] + radii[j]) * 0.08
                if min_dist_sq > thresh * thresh:
                    continue

                any_overlap = True
                ci = centers[i] + off_i
                cj = centers[j] + off_j
                sep_dir = cj - ci
                sep_dir = Vector((0.0, 0.0, 1.0)) if sep_dir.length < 1e-6 else sep_dir.normalized()

                wi, wj = weights[i], weights[j]
                w_sum = wi + wj
                share_i = wj / w_sum if w_sum > 1e-6 else 0.5
                share_j = wi / w_sum if w_sum > 1e-6 else 0.5

                # Adjust dimensionless direction vectors so overlapping parts move apart.
                dirs[i] -= sep_dir * (0.35 * share_i)
                dirs[j] += sep_dir * (0.35 * share_j)

        if not any_overlap:
            break

    # Keep the largest part stationary; all other directions are relative to it.
    anchor_idx = max(range(n_parts), key=lambda k: weights[k])
    base_dir = dirs[anchor_idx].copy()
    for p, d in zip(parts, dirs):
        rel_dir = d - base_dir
        p["esp_explode_dir"] = list(rel_dir)


def explode(context, parts, distance_bu, recompute=False):
    if len(parts) < 2:
        return
    # Compute expensive geometry only when the cache is missing or Explode is triggered.
    if recompute or any("esp_explode_dir" not in p or "esp_home" not in p for p in parts):
        _compute_explode_dirs(context, parts)

    # Slider updates only run this loop, keeping interactive movement responsive.
    for p in parts:
        home = Vector(p["esp_home"])
        d = Vector(p["esp_explode_dir"])
        p.location = home + d * distance_bu


def collapse(parts):
    for p in parts:
        if "esp_home" in p:
            p.location = Vector(p["esp_home"])
            del p["esp_home"]
        if "esp_explode_dir" in p:
            del p["esp_explode_dir"]


class ESP_OT_explode(bpy.types.Operator):
    bl_idname = "esp.explode"
    bl_label = "Explode"
    bl_description = "Move the parts apart so you can inspect the connectors"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.esp
        parts = part_objects(context)
        if len(parts) < 2:
            self.report({'WARNING'}, "Need at least two parts")
            return {'CANCELLED'}
        explode(context, parts, s.explode_distance_mm * plan.mm(context), recompute=True)
        s.exploded = True
        self.report({'INFO'}, f"Exploded {len(parts)} object(s)")
        return {'FINISHED'}


class ESP_OT_collapse(bpy.types.Operator):
    bl_idname = "esp.collapse"
    bl_label = "Collapse"
    bl_description = "Put the exploded parts back in place"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.esp
        parts = [o for o in context.scene.objects if "esp_home" in o]
        collapse(parts)
        s.exploded = False
        return {'FINISHED'}


def safe_filename(name):
    return re.sub(r'[^A-Za-z0-9._-]+', "_", name).strip("_") or "part"


class ESP_OT_export(bpy.types.Operator):
    bl_idname = "esp.export_parts"
    bl_label = "Export Parts"
    bl_description = "Write one file per part (selected parts, or every part of the last build) to the folder"
    bl_options = {'REGISTER'}

    def execute(self, context):
        s = context.scene.esp
        parts = part_objects(context)
        if not parts:
            self.report({'ERROR'}, "No parts to export. Select the parts or build first.")
            return {'CANCELLED'}
        folder = bpy.path.abspath(s.export_folder)
        if not folder:
            self.report({'ERROR'}, "Choose an export folder")
            return {'CANCELLED'}
        os.makedirs(folder, exist_ok=True)
        unit_mode = plan.pref(context, "unit_mode", 'SCENE')
        bu_to_mm = 1.0 if unit_mode == 'BU_IS_MM' else (context.scene.unit_settings.scale_length or 1.0) * 1000.0
        prev_sel = [o for o in context.selected_objects]
        prev_active = context.view_layer.objects.active
        exploded = [o for o in parts if "esp_home" in o]
        if exploded:
            collapse(exploded)
        written = []
        try:
            for p in parts:
                for o in context.view_layer.objects:
                    o.select_set(False)
                p.select_set(True)
                context.view_layer.objects.active = p
                ext = s.export_format.lower()
                path = os.path.join(folder, safe_filename(p.name) + "." + ext)
                if s.export_format == 'STL':
                    bpy.ops.wm.stl_export(
                        filepath=path,
                        export_selected_objects=True,
                        global_scale=bu_to_mm,
                        use_scene_unit=False,
                        apply_modifiers=True,
                    )
                elif s.export_format == 'OBJ':
                    bpy.ops.wm.obj_export(
                        filepath=path,
                        export_selected_objects=True,
                        global_scale=bu_to_mm,
                        apply_modifiers=True,
                        export_materials=False,
                    )
                else:
                    bpy.ops.export_scene.fbx(
                        filepath=path, use_selection=True, apply_unit_scale=True, object_types={'MESH'}
                    )
                written.append(path)
        finally:
            if exploded:
                explode(context, exploded, s.explode_distance_mm * plan.mm(context))
            for o in context.view_layer.objects:
                o.select_set(False)
            for o in prev_sel:
                try:
                    o.select_set(True)
                except RuntimeError:
                    pass
            if prev_active is not None:
                context.view_layer.objects.active = prev_active
        s.last_message = f"Exported {len(written)} file(s)"
        self.report({'INFO'}, f"Exported {len(written)} {s.export_format} file(s) to {folder}")
        return {'FINISHED'}


class ESP_OT_connector_library(bpy.types.Operator):
    bl_idname = "esp.connector_library"
    bl_label = "Connector Library"
    bl_description = (
        "Create the ESP_Connectors collection with editable template shapes. Any mesh you add "
        "there (fitting x/y in -0.5..0.5 and z in -1..1, +z = the pin tip) becomes a custom connector"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        col, created = connectors.ensure_library(context)
        col.hide_viewport = False
        self.report(
            {'INFO'}, f"Connector library ready ({len(col.objects)} shapes). Edit or add meshes in '{col.name}'."
        )
        return {'FINISHED'}


class ESP_OT_check_mesh(bpy.types.Operator):
    bl_idname = "esp.check_mesh"
    bl_label = "Check Mesh"
    bl_description = "Report boundary and non-manifold edges of the active object"

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        non, boundary, total = mesh_utils.manifold_report(obj.data)
        if non == 0 and boundary == 0:
            self.report({'INFO'}, f"'{obj.name}' is closed and manifold ({total} edges). Good to cut.")
        else:
            self.report(
                {'WARNING'},
                f"'{obj.name}': {boundary} boundary edge(s), {non} non-manifold edge(s). "
                "Fix them (3D-Print Toolbox / Mesh > Clean Up) before cutting.",
            )
        return {'FINISHED'}


class ESP_OT_open_docs(bpy.types.Operator):
    bl_idname = "esp.open_docs"
    bl_label = "Help"
    bl_description = "Open the EasySlice Print documentation"

    def execute(self, context):
        bpy.ops.wm.url_open(url=DOCS_URL)
        return {'FINISHED'}


CLASSES = (
    ESP_OT_explode,
    ESP_OT_collapse,
    ESP_OT_export,
    ESP_OT_connector_library,
    ESP_OT_check_mesh,
    ESP_OT_open_docs,
)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
