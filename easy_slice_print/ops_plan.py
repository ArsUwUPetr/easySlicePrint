# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Rafael Omodei and EasySlice Print contributors
"""Plan mode operators: manage records, edit surfaces, build/approve."""

import time

import bpy
from bpy.props import EnumProperty, IntProperty
from bpy_extras import view3d_utils
from mathutils import Vector

from . import draw, jobs, overlay, plan
from .core import cutting, mesh_utils, surfaces
from .ops_tools import CURSOR_DRAW, NAV_EVENTS, dist2d, restore_cursor, set_cursor, window_region


def active_record(context):
    s = context.scene.esp
    if 0 <= s.active_cut < len(s.cuts):
        return s.cuts[s.active_cut]
    return None


class ESP_OT_new_cut(bpy.types.Operator):
    bl_idname = "esp.new_cut"
    bl_label = "New Cut"
    bl_description = "Start the last used cut tool"

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and plan.resolve_target(context) is not None

    def invoke(self, context, event):
        tool = context.scene.esp.tool
        op = {
            'STRAIGHT': bpy.ops.esp.cut_straight,
            'CURVED': bpy.ops.esp.cut_curved,
            'FREEHAND': bpy.ops.esp.cut_freehand,
            'OBJECT': bpy.ops.esp.object_joint,
        }[tool]
        return op('INVOKE_DEFAULT')


class ESP_OT_delete_cut(bpy.types.Operator):
    bl_idname = "esp.delete_cut"
    bl_label = "Delete Cut"
    bl_description = "Remove this planned cut and its preview"
    bl_options = {'REGISTER', 'UNDO'}
    index: IntProperty(default=-1)

    def execute(self, context):
        s = context.scene.esp
        idx = self.index if self.index >= 0 else s.active_cut
        if not (0 <= idx < len(s.cuts)):
            return {'CANCELLED'}
        name = s.cuts[idx].name
        plan.remove_record(context, idx)
        overlay.clear()
        self.report({'INFO'}, f"Removed '{name}'")
        return {'FINISHED'}


class ESP_OT_move_cut(bpy.types.Operator):
    bl_idname = "esp.move_cut"
    bl_label = "Move Cut"
    bl_description = "Move the selected cut up or down in the build execution order"
    bl_options = {'REGISTER', 'UNDO'}

    direction: EnumProperty(
        items=[
            ('UP', "Up", "Move up in the execution order"),
            ('DOWN', "Down", "Move down in the execution order"),
        ],
        default='UP',
    )

    @classmethod
    def poll(cls, context):
        s = getattr(context.scene, "esp", None)
        return s is not None and len(s.cuts) > 1 and 0 <= s.active_cut < len(s.cuts)

    def execute(self, context):
        s = context.scene.esp
        idx = s.active_cut
        new_idx = idx - 1 if self.direction == 'UP' else idx + 1
        if not (0 <= new_idx < len(s.cuts)):
            return {'CANCELLED'}
        s.cuts.move(idx, new_idx)
        s.active_cut = new_idx
        plan.on_active_changed(context)
        return {'FINISHED'}


class ESP_OT_swap_pin_side(bpy.types.Operator):
    bl_idname = "esp.swap_pin_side"
    bl_label = "Swap Pin Side"
    bl_description = "Move the pin to the other part (the socket goes to this one)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return active_record(context) is not None

    def execute(self, context):
        rec = active_record(context)
        rec.pin_side = 'B' if rec.pin_side == 'A' else 'A'
        return {'FINISHED'}


class ESP_OT_reset_pin(bpy.types.Operator):
    bl_idname = "esp.reset_pin"
    bl_label = "Reset Connector"
    bl_description = (
        "Put the connector back to its stock shape at the automatic position, orientation and size, "
        "discarding any edit mode reshaping"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return active_record(context) is not None

    def execute(self, context):
        rec = active_record(context)
        plan.refresh_record_frames(context, rec)
        plan.reset_pins(context, rec)
        return {'FINISHED'}


class ESP_OT_select_pin(bpy.types.Operator):
    bl_idname = "esp.select_pin"
    bl_label = "Select Connector"
    bl_description = (
        "Select the connector preview so you can move (G), rotate (R) or scale (S) it in the viewport, "
        "or Tab into edit mode and reshape it - the build uses the shape you leave behind"
    )
    bl_options = {'REGISTER', 'UNDO'}
    index: IntProperty(default=0)

    @classmethod
    def poll(cls, context):
        return active_record(context) is not None and context.mode == 'OBJECT'

    def execute(self, context):
        rec = active_record(context)
        name = rec.pin_a if self.index == 0 else rec.pin_b
        pin = bpy.data.objects.get(name)
        if pin is None:
            self.report({'WARNING'}, "This cut has no connector preview")
            return {'CANCELLED'}
        for o in context.view_layer.objects:
            o.select_set(False)
        pin.hide_viewport = False
        pin.select_set(True)
        context.view_layer.objects.active = pin
        self.report({'INFO'}, "Connector selected: G move, R rotate, S scale. Reset Pin puts it back.")
        return {'FINISHED'}


class ESP_OT_refresh_pins(bpy.types.Operator):
    bl_idname = "esp.refresh_pins"
    bl_label = "Refresh Selected Cut"
    bl_description = "Re-estimate connector position and size from the current cut surfaces"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return active_record(context) is not None and context.mode == 'OBJECT'

    def execute(self, context):
        rec = active_record(context)
        if rec:
            plan.refresh_record_frames(context, rec)
        return {'FINISHED'}


# ----------------------------------------------------------------------------
def edit_surface_of(rec, index):
    """The preview surface of contact `index` and whether it is edited by its points.

    A plane is moved as an object (G/R/S); a curve or freehand surface is reshaped
    point by point. Each contact of a two-contact cut has a surface of its own, and
    is edited on its own.
    """
    name = rec.surface_b if index == 1 and rec.two_contact else rec.surface_a
    sobj = bpy.data.objects.get(name) if name else None
    if sobj is None:
        return None, False
    return sobj, sobj.get("esp_kind", 'STRAIGHT') != 'STRAIGHT'


class ESP_OT_edit_surface(bpy.types.Operator):
    bl_idname = "esp.edit_surface"
    bl_label = "Edit Cut Surface"
    bl_description = (
        "Plane cut: selects the cut plane (G/R/S). Curve/Freehand: drag the control points; "
        "Ctrl+LMB add, X delete, G slide all, R reset, Ctrl+Z undo, Enter/Esc finish. "
        "A two-contact cut edits one contact at a time"
    )
    bl_options = {'REGISTER', 'UNDO'}
    HOVER_PX = 12.0
    index: IntProperty(default=0, description="Which contact of a two-contact cut to edit")

    @classmethod
    def poll(cls, context):
        return active_record(context) is not None and context.mode == 'OBJECT'

    def invoke(self, context, event):
        rec = active_record(context)
        self.rec = rec
        sobj, by_points = edit_surface_of(rec, self.index)
        if sobj is None:
            self.report({'ERROR'}, "Preview surface not found")
            return {'CANCELLED'}
        rec.show = True
        if not by_points:
            for o in context.view_layer.objects:
                o.select_set(False)
            sobj.select_set(True)
            context.view_layer.objects.active = sobj
            self.report({'INFO'}, "Cut surface selected: G move, R rotate, S scale. The connector follows.")
            return {'FINISHED'}
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'ERROR'}, "Run this from the 3D Viewport")
            return {'CANCELLED'}
        self.area = context.area
        self.window = context.window
        self.region = window_region(self.area)
        self.rv3d = self.region.data
        self.sobj = sobj
        self.kind = sobj.get("esp_kind", 'CURVED')
        self.closed = self.kind == 'FREEHAND'
        self.target = bpy.data.objects.get(rec.target) or bpy.data.objects.get(context.scene.esp.base_object)
        self.mw = sobj.matrix_world.copy()
        self.inv = self.mw.inverted_safe()
        self.points = plan.surface_points(sobj)
        self.orig = [p.copy() for p in self.points]
        self.history = [[p.copy() for p in self.points]]
        self.redo = []
        self.hover = None
        self.drag = None
        self.slide = None
        self.mouse = (0, 0)
        self.changed = False
        self._handle = bpy.types.SpaceView3D.draw_handler_add(self._draw_cb, (context,), 'WINDOW', 'POST_PIXEL')
        context.window_manager.modal_handler_add(self)
        set_cursor(self.window, CURSOR_DRAW)
        context.workspace.status_text_set(
            "Drag point: LMB | Add: Ctrl+LMB | Delete: X | Slide all: G | Reset: R | Undo: Ctrl+Z | Finish: Enter / Esc"
        )
        return {'RUNNING_MODAL'}

    # -- helpers ----------------------------------------------------------------
    def world_points(self):
        return [self.mw @ p for p in self.points]

    def to2d(self, w):
        return view3d_utils.location_3d_to_region_2d(self.region, self.rv3d, w)

    def surface_pos(self, context, coord, fallback_world):
        origin = view3d_utils.region_2d_to_origin_3d(self.region, self.rv3d, coord)
        direction = view3d_utils.region_2d_to_vector_3d(self.region, self.rv3d, coord).normalized()
        if self.target is not None:
            state = plan.ensure_evaluable(self.target)
            try:
                depsgraph = context.evaluated_depsgraph_get()
                hit, loc, _nor, _d = mesh_utils.object_ray_cast(self.target, origin, direction, depsgraph)
            finally:
                plan.restore_visibility(self.target, state)
            if hit:
                # a freehand point goes where it was put, on the surface; the cutter's
                # clearance is a skirt past the rim, not a push on the point
                return loc
        return view3d_utils.region_2d_to_location_3d(self.region, self.rv3d, coord, fallback_world)

    def commit(self, context, draft=False):
        plan.set_surface_points(self.sobj, self.points)
        plan.rebuild_surface(self.sobj, draft, context=context, target=self.target)
        self.changed = True

    def push_history(self):
        self.history.append([p.copy() for p in self.points])
        self.redo = []

    def nearest_point(self, coord):
        best, best_d = None, self.HOVER_PX
        for i, w in enumerate(self.world_points()):
            p = self.to2d(w)
            if p is None:
                continue
            d = dist2d(p, coord)
            if d < best_d:
                best, best_d = i, d
        return best

    def nearest_segment(self, coord):
        pts = [self.to2d(w) for w in self.world_points()]
        best, best_d = None, 1e9
        n = len(pts)
        rng = range(n) if self.closed else range(n - 1)
        for i in rng:
            a, b = pts[i], pts[(i + 1) % n]
            if a is None or b is None:
                continue
            ab = (b[0] - a[0], b[1] - a[1])
            l2 = ab[0] ** 2 + ab[1] ** 2
            t = 0.0 if l2 < 1e-9 else max(0.0, min(1.0, ((coord[0] - a[0]) * ab[0] + (coord[1] - a[1]) * ab[1]) / l2))
            proj = (a[0] + ab[0] * t, a[1] + ab[1] * t)
            d = dist2d(proj, coord)
            if d < best_d:
                best, best_d = i, d
        return best

    def end(self, context):
        if self._handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._handle, 'WINDOW')
            self._handle = None
        restore_cursor(getattr(self, "window", None))
        context.workspace.status_text_set(None)
        self.area.tag_redraw()

    # -- modal ------------------------------------------------------------------
    def modal(self, context, event):
        self.mouse = (event.mouse_x - self.region.x, event.mouse_y - self.region.y)
        if event.type in NAV_EVENTS:
            return {'PASS_THROUGH'}
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            if self.slide is not None:
                self.points = self.slide[1]
                self.slide = None
                self.commit(context)
                return {'RUNNING_MODAL'}
            self.points = self.orig
            self.commit(context)
            self.end(context)
            plan.refresh_record_frames(context, self.rec)
            return {'CANCELLED'}
        if event.type in {'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            if self.slide is not None:
                self.slide = None
                self.commit(context)
                self.push_history()
                return {'RUNNING_MODAL'}
            self.end(context)
            plan.refresh_record_frames(context, self.rec)
            self.report({'INFO'}, "Cut surface updated")
            return {'FINISHED'}
        if event.type == 'MOUSEMOVE':
            if self.drag is not None:
                w = self.surface_pos(context, self.mouse, self.mw @ self.points[self.drag])
                self.points[self.drag] = self.inv @ w
                self.commit(context, draft=True)
            elif self.slide is not None:
                start, base = self.slide
                ref = self.mw @ base[0]
                delta = view3d_utils.region_2d_to_location_3d(
                    self.region, self.rv3d, self.mouse, ref
                ) - view3d_utils.region_2d_to_location_3d(self.region, self.rv3d, start, ref)
                self.points = [self.inv @ ((self.mw @ p) + delta) for p in base]
                self.commit(context, draft=True)
            else:
                self.hover = self.nearest_point(self.mouse)
        elif event.type == 'LEFTMOUSE':
            if event.value == 'PRESS':
                if self.slide is not None:
                    self.slide = None
                    self.commit(context)
                    self.push_history()
                elif event.ctrl:
                    seg = self.nearest_segment(self.mouse)
                    if seg is not None:
                        w = self.surface_pos(context, self.mouse, self.mw @ self.points[seg])
                        self.points.insert(seg + 1, self.inv @ w)
                        self.drag = seg + 1
                        self.commit(context)
                elif self.hover is not None:
                    self.drag = self.hover
            elif event.value == 'RELEASE' and self.drag is not None:
                self.drag = None
                self.commit(context)  # drag over: rebuild at full resolution
                self.push_history()
        elif event.type in {'X', 'DEL'} and event.value == 'PRESS':
            min_pts = 3 if self.closed else 2
            if self.hover is not None and len(self.points) > min_pts:
                self.points.pop(self.hover)
                self.hover = None
                self.commit(context)
                self.push_history()
        elif event.type == 'G' and event.value == 'PRESS':
            self.slide = (self.mouse, [p.copy() for p in self.points])
        elif event.type == 'R' and event.value == 'PRESS':
            self.points = [p.copy() for p in self.orig]
            self.commit(context)
            self.push_history()
        elif event.type == 'Z' and event.value == 'PRESS' and event.ctrl:
            if event.shift:
                if self.redo:
                    self.history.append(self.redo.pop())
                    self.points = [p.copy() for p in self.history[-1]]
                    self.commit(context)
            elif len(self.history) > 1:
                self.redo.append(self.history.pop())
                self.points = [p.copy() for p in self.history[-1]]
                self.commit(context)
        self.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def _draw_cb(self, context):
        if bpy.context.region is None or bpy.context.region.as_pointer() != self.region.as_pointer():
            return
        pts = [self.to2d(w) for w in self.world_points()]
        pts2 = [p for p in pts if p is not None]
        if len(pts2) >= 2:
            draw.lines_2d(pts2, draw.GREEN, 2.0, closed=self.closed)
        draw.points_2d(pts2, draw.ORANGE, 8.0)
        idx = self.drag if self.drag is not None else self.hover
        if idx is not None and idx < len(pts) and pts[idx] is not None:
            draw.points_2d([pts[idx]], (1.0, 1.0, 0.2, 1.0), 12.0)


# ----------------------------------------------------------------------------
def _remove_built_parts(context):
    s = context.scene.esp
    col = bpy.data.collections.get(s.built_collection) if s.built_collection else None
    if col is not None:
        for obj in list(col.objects):
            mesh_utils.remove_object(obj)
        bpy.data.collections.remove(col)
    s.built_collection = ""
    s.built = False
    s.exploded = False
    for rec in s.cuts:
        rec.built = False
        rec.build_state = 'NONE'
    s.build_progress = 0.0
    s.build_status = ""


def _labelled(generator, prefix):
    """Forward a step generator, tagging each label with the cut it belongs to."""
    try:
        while True:
            try:
                label = next(generator)
            except StopIteration as stop:
                return stop.value
            yield f"{prefix} - {label}" if label else prefix
    finally:
        generator.close()


def _surface_world_points(surface_obj):
    if surface_obj is None or surface_obj.data is None or len(surface_obj.data.vertices) == 0:
        return []
    mw = surface_obj.matrix_world
    verts = surface_obj.data.vertices
    step = max(1, len(verts) // 64)
    return [mw @ verts[i].co for i in range(0, len(verts), step)]


def _object_world_bvh(obj):
    if obj is None or obj.data is None or len(obj.data.polygons) == 0:
        return None
    mw = obj.matrix_world
    verts = [mw @ v.co for v in obj.data.vertices]
    faces = [tuple(p.vertices) for p in obj.data.polygons]
    return mesh_utils.bvh_from_pydata(verts, faces)


def _pick_target(parts, anchor, prefer_name=None, surface_obj=None):
    # Exclude cutter parts
    candidates = [p for p in parts if not p.get("esp_is_cutter_part")] or parts
    if prefer_name:
        exact = [
            p
            for p in candidates
            if p.name == prefer_name
            or p.get("esp_source") == prefer_name
            or p.get("esp_stem") == prefer_name
            or p.get("esp_base") == prefer_name
        ]
        if exact:
            candidates = exact

    # 1. Fast BVH overlap check: which candidate piece does this cut surface actually intersect?
    if surface_obj is not None:
        pv, pf = plan.surface_world_patch(surface_obj)
        if pv and pf:
            s_bvh = mesh_utils.bvh_from_pydata(pv, pf)
            if s_bvh is not None:
                for p in candidates:
                    p_bvh = _object_world_bvh(p)
                    if p_bvh is not None and p_bvh.overlap(s_bvh):
                        return p

    # 2. Surface points closest point check
    surf_pts = _surface_world_points(surface_obj)
    if surf_pts:
        best, best_score = None, 1e30
        for p in candidates:
            # Compute average distance from cut points to surface of part p
            dists = []
            for pt in surf_pts:
                ok, _loc, _nor, d = mesh_utils.object_closest_point(p, pt)
                if ok:
                    dists.append(d)
            if not dists:
                continue
            # Take minimum distances (cut boundary should lie directly on the matching part)
            dists.sort()
            top_k = dists[: max(1, len(dists) // 3)]
            score = sum(top_k) / len(top_k)
            if score < best_score:
                best, best_score = p, score
        if best is not None:
            return best

    # 3. Fallback to anchor point
    search_pt = surface_obj.matrix_world.translation if surface_obj is not None else anchor
    best, best_d = None, 1e30
    for p in candidates:
        ok, _loc, _nor, d = mesh_utils.object_closest_point(p, search_pt)
        if ok and d < best_d:
            best, best_d = p, d
    return best or candidates[0]


def _pick_all_targets(parts, anchor, prefer_name=None, surface_obj=None):
    """Return all model parts originating from the target object."""
    candidates = [p for p in parts if not p.get("esp_is_cutter_part")] or parts
    if prefer_name:
        exact = [
            p
            for p in candidates
            if p.name == prefer_name
            or p.get("esp_source") == prefer_name
            or p.get("esp_stem") == prefer_name
            or p.get("esp_base") == prefer_name
        ]
        if exact:
            return exact
    return candidates


class ESP_OT_build(jobs.JobMixin, bpy.types.Operator):
    bl_idname = "esp.build"
    bl_label = "Build Plan"
    bl_description = "Apply every ready cut to the model and create the final parts (the plan stays editable)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = context.scene.esp
        return context.mode == 'OBJECT' and any(r.enabled for r in s.cuts)

    def build_steps(self, context):
        """Run the whole plan, yielding a label before each heavy step.

        Every cut is several seconds of boolean work; a three cut plan easily
        passes ten. Yielding lets the modal driver return to Blender's event
        loop so the window never looks frozen to the desktop.
        """
        s = context.scene.esp
        scene = context.scene
        t0 = time.time()
        enabled = [r for r in s.cuts if r.enabled]
        total_cuts = max(1, len(enabled))
        for r in s.cuts:
            r.built = False
            r.build_state = 'WAIT' if r.enabled else 'NONE'

        s.build_in_progress = True
        s.build_progress = 0.0
        s.build_status = f"Building cut 1/{total_cuts}..."

        errs = plan.validate_plan(context, paint_overlay=True)
        if errs:
            s.build_in_progress = False
            raise cutting.CutError("Build aborted: " + "; ".join(errs))
        base = bpy.data.objects.get(s.base_object)
        sources = plan.plan_sources(context)
        if not sources and base is None:
            s.build_in_progress = False
            raise cutting.CutError("The plan's source objects no longer exist. Clear the plan.")
        overlay.clear()
        self.failed = None  # (record name, diagnosis) of the first cut that did not split its part
        if s.built:
            _remove_built_parts(context)
        # after an approve the cuts can sit on different parts: each one is a source
        for src in sources:
            plan.restore_from_backup(scene, src)
        plan.set_plan_hidden(context, False)
        col_base_name = base.name if base is not None else (sources[0].name if sources else "Model")
        col = plan.built_collection(scene, col_base_name)
        parts = list(sources)
        made = []
        counter = 0
        done = 0
        skipped = 0
        try:
            for i, rec in enumerate(enabled, start=1):
                rec.build_state = 'ACTIVE'
                done_so_far = i - 1
                s.build_progress = (done_so_far / total_cuts) * 100.0
                s.build_status = f"Building cut {i}/{total_cuts} ('{rec.name}')..."
                prefix = f"cut {i}/{total_cuts} ({rec.name})"
                yield f"{prefix} - preparing"
                rec_target_obj = plan.record_target(rec)
                target_pref_name = rec_target_obj.name if rec_target_obj is not None else rec.target
                if rec.cut_type == 'OBJECT':
                    try:
                        spec = plan.record_spec(context, rec, s)
                        sobj = bpy.data.objects.get(rec.surface_a)
                        raw_cutters = plan.cutter_object_list(rec)
                        raw_cutter_names = {co.name for co in raw_cutters}
                        # Find current cutter parts (even if they already went through a previous Object Joint!)
                        active_cutters = []
                        for co in raw_cutters:
                            if co in parts:
                                active_cutters.append(co)
                            else:
                                match = [
                                    p
                                    for p in parts
                                    if p.get("esp_source") == co.name
                                    or p.get("esp_stem") == co.name
                                    or p.get("esp_base") == co.name
                                ]
                                for m_obj in (match if match else [co]):
                                    if m_obj not in active_cutters:
                                        active_cutters.append(m_obj)

                        # All model parts (excluding cutter parts)
                        targets = [
                            p
                            for p in _pick_all_targets(parts, Vector(rec.anchor), prefer_name=target_pref_name, surface_obj=sobj)
                            if p not in active_cutters and p.name not in raw_cutter_names
                        ]
                        if not targets:
                            targets = [p for p in parts if p not in active_cutters and p.name not in raw_cutter_names]
                        if not targets or not active_cutters:
                            raise cutting.CutError(f"Missing target ({len(targets)}) or cutter ({len(active_cutters)}) objects for '{rec.name}'")

                        t_states = [plan.ensure_evaluable(t) for t in targets]
                        c_states = [plan.ensure_evaluable(c) for c in active_cutters]
                        try:
                            built_targets, built_c, _secs = yield from _labelled(
                                cutting.perform_object_joint_steps(context, targets, active_cutters, spec, col), prefix
                            )
                        finally:
                            for t, st in zip(targets, t_states):
                                plan.restore_visibility(t, st)
                            for c, st in zip(active_cutters, c_states):
                                plan.restore_visibility(c, st)
                    except Exception as e:
                        rec.build_state = 'SKIPPED' if s.skip_failed else 'ERROR'
                        if s.skip_failed:
                            skipped += 1
                            s.build_progress = (i / total_cuts) * 100.0
                            self.report({'WARNING'}, f"'{rec.name}' skipped: {e}")
                            continue
                        for o in made:
                            mesh_utils.remove_object(o)
                        bpy.data.collections.remove(col)
                        raise cutting.CutError(f"'{rec.name}' failed: {e}") from e

                    for old_obj in targets + active_cutters:
                        if old_obj in parts:
                            parts.remove(old_obj)
                        if old_obj not in sources and old_obj in made:
                            made.remove(old_obj)
                            mesh_utils.remove_object(old_obj)
                    new_objs = built_targets + built_c
                    parts.extend(new_objs)
                    made.extend(new_objs)
                    rec.built = True
                    rec.build_state = 'DONE'
                    done += 1
                    s.build_progress = (i / total_cuts) * 100.0
                    continue
                try:
                    spec = plan.record_spec(context, rec, s)
                    sobj = bpy.data.objects.get(rec.surface_a)
                    target = _pick_target(parts, Vector(rec.anchor), prefer_name=target_pref_name, surface_obj=sobj)
                    stem = target.get("esp_stem", target.name)
                    counter += 2
                    names = (f"{stem}_PART_{counter - 1:03d}", f"{stem}_PART_{counter:03d}")
                    state = plan.ensure_evaluable(target)
                    try:
                        a, b, _secs = yield from _labelled(
                            cutting.perform_cut_steps(context, target, spec, names, col), prefix
                        )
                    finally:
                        plan.restore_visibility(target, state)
                except cutting.CutError as e:
                    rec.build_state = 'SKIPPED' if s.skip_failed else 'ERROR'
                    if self.failed is None and e.diagnosis is not None:
                        self.failed = (rec.name, e.diagnosis)
                    if s.skip_failed:
                        skipped += 1
                        s.build_progress = (i / total_cuts) * 100.0
                        self.report({'WARNING'}, f"'{rec.name}' skipped: {e}")
                        continue
                    for o in made:
                        mesh_utils.remove_object(o)
                    bpy.data.collections.remove(col)
                    raise cutting.CutError(f"'{rec.name}' failed: {e}", e.diagnosis) from e
                parts.remove(target)
                if target not in sources:
                    made.remove(target)
                    mesh_utils.remove_object(target)
                for o in (a, b):
                    o["esp_stem"] = stem
                parts.extend((a, b))
                made.extend((a, b))
                rec.built = True
                rec.build_state = 'DONE'
                done += 1
                s.build_progress = (i / total_cuts) * 100.0
            if not made:
                bpy.data.collections.remove(col)
                raise cutting.CutError("Nothing was cut")
            s.build_progress = 100.0
            yield "finishing"
            numbers = {}
            for o in sorted(made, key=lambda o: o.name):
                stem = o.get("esp_stem", o.get("esp_source", (base.name if base else "Model")))
                n = numbers[stem] = numbers.get(stem, 0) + 1
                o.name = f"{stem}_PART_{n:03d}"
                o.data.name = o.name
                o["esp_base"] = stem
                o["esp_source"] = stem
            for src in sources:
                if src not in parts:  # it was cut: the model itself goes out of the way
                    plan.move_to_backup(scene, src)
            plan.set_plan_hidden(context, True)
            s.built = True
            s.built_collection = col.name
            s.exploded = False
            for o in context.view_layer.objects:
                o.select_set(False)
            for o in made:
                o.select_set(True)
            context.view_layer.objects.active = made[0]
            secs = time.time() - t0
            msg = f"Built {len(made)} part(s) from {done} cut(s) in {secs:.2f}s"
            if skipped:
                msg += f" ({skipped} skipped)"
            return msg
        finally:
            s.build_in_progress = False
            s.build_status = ""

    def show_failed(self):
        """Paint where the first failed cut stays joined; -> a note for the message, or ''."""
        failed = getattr(self, "failed", None)
        if failed is None:
            return ""
        name, diag = failed
        return overlay.show_with_note(diag, name)

    # -- modal driver -------------------------------------------------------
    def invoke(self, context, event):
        errs = plan.validate_plan(context, paint_overlay=True)
        if errs:
            for e in errs:
                self.report({'ERROR'}, e)
            return {'CANCELLED'}
        if context.window is None:
            return self.execute(context)
        return self.job_start(context, self.build_steps(context), "Build", add_handler=True)

    def modal(self, context, event):
        state, payload = self.job_step(context, event)
        if state == jobs.RUNNING:
            return {'RUNNING_MODAL'}
        return self.job_done(context, state, payload)

    def cancel(self, context):
        """Blender aborted the modal: stop the timer and clear the status line."""
        self.job_stop(context)

    def job_done(self, context, state, payload):
        s = context.scene.esp
        s.build_in_progress = False
        s.build_status = ""
        if state == jobs.CANCELLED:
            for r in s.cuts:
                if r.build_state in {'WAIT', 'ACTIVE'}:
                    r.build_state = 'NONE'
            s.last_message = "Build cancelled"
            self.report({'WARNING'}, "Build cancelled - the parts made so far were kept")
            return {'CANCELLED'}
        if state == jobs.ERROR:
            self.report({'ERROR'}, str(payload) + self.show_failed())
            return {'CANCELLED'}
        s.last_message = payload
        self.report({'INFO'}, payload + ". Plan preserved; use Back to Plan to edit." + self.show_failed())
        return {'FINISHED'}

    def execute(self, context):
        """Blocking build, for scripts and background runs."""
        errs = plan.validate_plan(context, paint_overlay=True)
        if errs:
            for e in errs:
                self.report({'ERROR'}, e)
            return {'CANCELLED'}
        try:
            msg = cutting.drain(self.build_steps(context))
        except cutting.CutError as e:
            self.report({'ERROR'}, str(e) + self.show_failed())
            return {'CANCELLED'}
        context.scene.esp.last_message = msg
        self.report({'INFO'}, msg + ". Plan preserved; use Back to Plan to edit." + self.show_failed())
        return {'FINISHED'}


class ESP_OT_return_to_plan(bpy.types.Operator):
    bl_idname = "esp.return_to_plan"
    bl_label = "Back to Plan"
    bl_description = "Remove the built parts, restore the source model and show the planned cuts again"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene.esp.built

    def execute(self, context):
        s = context.scene.esp
        _remove_built_parts(context)
        sources = plan.plan_sources(context)
        for src in sources:
            plan.restore_from_backup(context.scene, src)
        if sources:
            context.view_layer.objects.active = sources[0]
        plan.set_plan_hidden(context, False)
        s.last_message = "Plan restored"
        self.report({'INFO'}, "Plan restored: cuts are editable again")
        return {'FINISHED'}


class ESP_OT_approve(bpy.types.Operator):
    bl_idname = "esp.approve"
    bl_label = "Approve"
    bl_description = (
        "Keep the built parts as final and drop the cuts they came from; cuts that were not built "
        "stay in the plan, on the part they sit on (the source stays in ESP_Backup when Keep Original is on)"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene.esp.built

    def execute(self, context):
        s = context.scene.esp
        # only what this build actually consumed - a source it cut is in the backup
        consumed = [o for o in plan.plan_sources(context) if o.get("esp_backup_home") is not None]
        col = bpy.data.collections.get(s.built_collection) if s.built_collection else None
        parts = [o for o in col.objects if o.type == 'MESH'] if col is not None else []
        # a built cut is done: its part exists, cutting it again would only split the part
        for i in reversed(range(len(s.cuts))):
            if s.cuts[i].built:
                plan.remove_record(context, i)
        # what was not built (not Ready, or skipped) waits on the part it was drawn over:
        # its source is going to the backup or away, and the next build must cut the part
        for rec in s.cuts:
            plan.sync_record_pointers(rec)
            target = plan.record_target(rec)
            if parts and (target is None or target in consumed):
                sobj = bpy.data.objects.get(rec.surface_a)
                new_target = _pick_target(parts, Vector(rec.anchor), surface_obj=sobj)
                rec.target = new_target.name
                rec.target_obj = new_target
                plan.refresh_record_frames(context, rec)
        if not s.keep_original:
            for o in consumed:
                mesh_utils.remove_object(o)
        s.built = False
        s.base_object = s.cuts[0].target if len(s.cuts) else ""
        s.built_collection = ""
        if len(s.cuts):
            plan.set_plan_hidden(context, False)
        else:
            plan.remove_all_records(context)  # drops the empty plan collection
        overlay.clear()
        left = len(s.cuts)
        msg = "Parts approved" + (f", {left} unbuilt cut(s) kept in the plan" if left else "")
        s.last_message = msg
        self.report({'INFO'}, msg + ". Use Export to write the files.")
        return {'FINISHED'}


class ESP_OT_clear_plan(bpy.types.Operator):
    bl_idname = "esp.clear_plan"
    bl_label = "Clear Plan"
    bl_description = "Delete every planned cut and preview (built parts are kept)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return len(context.scene.esp.cuts) > 0

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        s = context.scene.esp
        stashed = [o for o in plan.plan_sources(context) if o.get("esp_backup_home") is not None]
        plan.remove_all_records(context)
        if not s.built:
            for o in stashed:
                plan.restore_from_backup(context.scene, o)
            s.base_object = ""
        s.built = False
        s.built_collection = ""
        overlay.clear()
        s.last_message = "Plan cleared"
        return {'FINISHED'}


class ESP_OT_hide_diagnosis(bpy.types.Operator):
    bl_idname = "esp.hide_diagnosis"
    bl_label = "Hide Diagnosis"
    bl_description = "Remove the red/green map of the failed cut from the viewport"

    @classmethod
    def poll(cls, context):
        return overlay.active()

    def execute(self, context):
        overlay.clear()
        return {'FINISHED'}


class ESP_OT_edit_surface_mesh(bpy.types.Operator):
    bl_idname = "esp.edit_surface_mesh"
    bl_label = "Edit as Mesh"
    bl_description = (
        "Switch this cut surface to custom mesh mode and enter Blender Edit Mode. "
        "You can freely move/extrude vertices, use Sculpt Mode, or add modifiers (Subdivision, Shrinkwrap, etc.)"
    )
    bl_options = {'REGISTER', 'UNDO'}
    index: IntProperty(default=0, description="Which contact of a two-contact cut to edit as mesh")

    @classmethod
    def poll(cls, context):
        return active_record(context) is not None and context.mode == 'OBJECT'

    def execute(self, context):
        rec = active_record(context)
        name = rec.surface_b if self.index == 1 and rec.two_contact else rec.surface_a
        sobj = bpy.data.objects.get(name) if name else None
        if sobj is None:
            self.report({'ERROR'}, "Preview surface not found")
            return {'CANCELLED'}
        rec.show = True
        sobj["esp_custom_mesh"] = True
        sobj.hide_viewport = False
        sobj.hide_set(False)
        for o in context.view_layer.objects:
            o.select_set(False)
        sobj.select_set(True)
        context.view_layer.objects.active = sobj
        bpy.ops.object.mode_set(mode='EDIT')
        self.report(
            {'INFO'},
            "Editing cut mesh: modify geometry or add modifiers. Exit to Object Mode and press Build when ready.",
        )
        return {'FINISHED'}


class ESP_OT_reset_surface_mesh(bpy.types.Operator):
    bl_idname = "esp.reset_surface_mesh"
    bl_label = "Reset to Procedural"
    bl_description = "Discard manual mesh edits and modifiers, restoring the procedural cut surface"
    bl_options = {'REGISTER', 'UNDO'}
    index: IntProperty(default=0)

    @classmethod
    def poll(cls, context):
        rec = active_record(context)
        if rec is None or context.mode != 'OBJECT':
            return False
        for name in (rec.surface_a, rec.surface_b if rec.two_contact else None):
            sobj = bpy.data.objects.get(name) if name else None
            if sobj is not None and bool(sobj.get("esp_custom_mesh")):
                return True
        return False

    def execute(self, context):
        rec = active_record(context)
        plan.reset_surface_to_procedural(context, rec, self.index)
        self.report({'INFO'}, "Cut surface restored to procedural mode")
        return {'FINISHED'}


class ESP_OT_object_joint(bpy.types.Operator):
    bl_idname = "esp.object_joint"
    bl_label = "Object Joint"
    bl_description = (
        "Select the target and cutter objects, then run Object Joint to create a clearance fit and connector"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and context.active_object is not None and context.active_object.type == 'MESH'

    def execute(self, context):
        s = context.scene.esp
        active = context.active_object
        others = [
            o
            for o in context.selected_objects
            if o != active and o.type == 'MESH' and "esp_preview" not in o and o.visible_get()
        ]
        if not others:
            self.report(
                {'WARNING'},
                "Select a target and at least one cutter object, then run Object Joint",
            )
            return {'CANCELLED'}

        if len(others) == 1:
            target = others[0]
            cutters = [active]
        else:
            target = active
            cutters = others

        gap_bu = s.cut_gap_mm * plan.mm(context)
        data = plan.object_joint_contact(context, target, cutters, gap_bu)
        contacts = [data]

        if s.mode == 'PLAN':
            rec = plan.add_record(context, target, 'OBJECT', contacts, cutter_objs=cutters)
            rec.pin_side = 'B'
            plan.on_record_settings_changed(context, rec)
            s.last_message = f"'{rec.name}': carving {cutters[0].name} out of {target.name}"
            self.report(
                {'INFO'},
                f"Carving {[o.name for o in cutters]} out of '{target.name}'. Use 'Swap Carved / Cutter' if reversed.",
            )
            return {'FINISHED'}

        spec = plan.quick_spec(context, target, contacts)
        base_name = target.get("esp_base", target.name) if target.get("esp_part") else target.name
        col = plan.built_collection(context.scene, base_name)
        try:
            built_targets, built_c, secs = cutting.drain(
                cutting.perform_object_joint_steps(context, [target], cutters, spec, col)
            )
        except Exception as e:
            self.report({'ERROR'}, f"Object Joint failed: {e}")
            return {'CANCELLED'}

        all_src = [target] + cutters
        if s.keep_original:
            for src in all_src:
                plan.move_to_backup(context.scene, src)
        else:
            for src in all_src:
                mesh_utils.remove_object(src)
        for o in context.view_layer.objects:
            o.select_set(False)
        for o in built_targets + built_c:
            o.select_set(True)
        context.view_layer.objects.active = built_targets[0]
        self.report({'INFO'}, f"Object Joint completed in {secs:.2f}s")
        return {'FINISHED'}


class ESP_OT_swap_joint_roles(bpy.types.Operator):
    bl_idname = "esp.swap_joint_roles"
    bl_label = "Swap Carved / Cutter"
    bl_description = "Swap the object being carved and the cutter object"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        rec = active_record(context)
        return rec is not None and rec.cut_type == 'OBJECT' and context.mode == 'OBJECT'

    def execute(self, context):
        rec = active_record(context)
        plan.sync_record_pointers(rec)
        old_target = plan.record_target(rec)
        old_cutters = plan.cutter_object_list(rec)
        if old_target is None or not old_cutters:
            self.report({'ERROR'}, "Missing joint objects")
            return {'CANCELLED'}

        # Swap target with the first cutter object
        new_target = old_cutters[0]
        new_cutters = [old_target] + old_cutters[1:]
        with plan.suspend_updates():
            rec.target = new_target.name
            rec.target_obj = new_target
            rec.cutter_objects = "\t".join(o.name for o in new_cutters)
            rec.cutter_obj = new_cutters[0] if len(new_cutters) == 1 else None
        plan.invalidate_validation_cache()
        if context.scene.esp.base_object == old_target.name:
            context.scene.esp.base_object = new_target.name

        # Rebuild the green preview so it now shows the new cutter surface inside the new target
        gap_bu = rec.cut_gap_mm * plan.mm(context)
        data = plan.object_joint_contact(context, new_target, new_cutters, gap_bu)
        sobj = bpy.data.objects.get(rec.surface_a)
        if sobj is not None and data.verts and data.faces:
            origin = sobj.matrix_world.translation.copy()
            me = sobj.data
            me.clear_geometry()
            me.from_pydata([tuple(Vector(v) - origin) for v in data.verts], [], [tuple(f) for f in data.faces])
            me.update()
            rec.center_a = data.center_hint or surfaces.patch_center(data.verts)
            rec.normal_a = data.plane_no or Vector((0.0, 0.0, 1.0))
            rec.inscribed_a = data.extend if data.extend > 0.0 else 5.0 * plan.mm(context)
            rec.anchor = rec.center_a
            plan.reset_pins(context, rec)

        self.report({'INFO'}, f"Swapped: now carving '{new_cutters[0].name}' out of '{new_target.name}'")
        return {'FINISHED'}


class ESP_OT_set_selected_cutters(bpy.types.Operator):
    bl_idname = "esp.set_selected_cutters"
    bl_label = "Set Cutters from Selection"
    bl_description = "Assign currently selected mesh objects (except the target) as cutters for this cut"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        rec = active_record(context)
        return rec is not None and rec.cut_type == 'OBJECT' and context.mode == 'OBJECT'

    def execute(self, context):
        rec = active_record(context)
        plan.sync_record_pointers(rec)
        target = plan.record_target(rec)
        sel = [
            o for o in context.selected_objects
            if o.type == 'MESH' and o != target and not o.get("esp_preview")
        ]
        if not sel:
            self.report({'WARNING'}, "Select at least one mesh object other than the target")
            return {'CANCELLED'}
        with plan.suspend_updates():
            rec.cutter_objects = "\t".join(o.name for o in sel)
            rec.cutter_obj = sel[0] if len(sel) == 1 else None
        plan.invalidate_validation_cache()
        plan.refresh_record_frames(context, rec)
        self.report({'INFO'}, f"Set {len(sel)} cutter object(s) for '{rec.name}'")
        return {'FINISHED'}


class ESP_OT_clear_missing_cutters(bpy.types.Operator):
    bl_idname = "esp.clear_missing_cutters"
    bl_label = "Remove Missing Cutters"
    bl_description = "Remove names of objects that no longer exist in the scene from this cut"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        rec = active_record(context)
        return rec is not None and rec.cut_type == 'OBJECT' and bool(plan.missing_cutter_names(rec))

    def execute(self, context):
        rec = active_record(context)
        plan.sync_record_pointers(rec)
        valid = [o.name for o in plan.cutter_object_list(rec)]
        with plan.suspend_updates():
            rec.cutter_objects = "\t".join(valid)
            rec.cutter_obj = bpy.data.objects.get(valid[0]) if len(valid) == 1 else None
        plan.invalidate_validation_cache()
        plan.refresh_record_frames(context, rec)
        self.report({'INFO'}, f"Cleaned missing cutters from '{rec.name}'")
        return {'FINISHED'}


class ESP_OT_add_connector(bpy.types.Operator):
    bl_idname = "esp.add_connector"
    bl_label = "Add Connector"
    bl_description = "Add another independent connector pin to this cut (move/rotate/scale it in Object Mode)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return active_record(context) is not None and context.mode == 'OBJECT'

    def execute(self, context):
        rec = active_record(context)
        rec.add_pin = True
        rec.show = True
        base_pin = bpy.data.objects.get(rec.pin_a)
        matrix = base_pin.matrix_world.copy() if base_pin is not None else plan.auto_pin_matrix(context, rec, 0)

        existing = plan.extra_pin_objects(rec)
        idx = len(existing) + 2
        # Shift the new pin slightly along local X axis to avoid overlapping the old pin
        offset = matrix.to_3x3().col[0].normalized() * (rec.pin_width_mm * plan.mm(context) * 1.5)
        matrix.translation = matrix.translation + offset

        new_pin = plan.create_pin_object(context, f"ESP_Pin_{rec.name}_{idx}", rec.shape, matrix)
        if base_pin is not None and base_pin.data is not None:
            # Copy mesh of active pin if the user modified it
            old_me = new_pin.data
            new_pin.data = base_pin.data.copy()
            mesh_utils.remove_mesh(old_me)

        names = [o.name for o in existing] + [new_pin.name]
        rec["esp_extra_pins"] = "\t".join(names)

        for o in context.view_layer.objects:
            o.select_set(False)
        new_pin.select_set(True)
        context.view_layer.objects.active = new_pin
        return {'FINISHED'}


class ESP_OT_mirror_cut(bpy.types.Operator):
    bl_idname = "esp.mirror_cut"
    bl_label = "Mirror Cut"
    bl_description = "Mirror the active planned cut across the chosen symmetry plane as a new cut record"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and active_record(context) is not None

    def execute(self, context):
        s = context.scene.esp
        rec = active_record(context)
        if rec is None:
            return {'CANCELLED'}
        orig_name = rec.name
        target = plan.record_target(rec)
        if target is None:
            self.report({'ERROR'}, "Target object not found")
            return {'CANCELLED'}
        sobj_a = bpy.data.objects.get(rec.surface_a)
        if sobj_a is None:
            self.report({'ERROR'}, "Active cut has no surface preview")
            return {'CANCELLED'}

        M = plan.mirror_matrix_world(context, target)

        try:
            mirrored_a = plan.mirror_contact_from_surface(context, target, sobj_a)
        except Exception as e:
            self.report({'ERROR'}, f"Could not mirror cut: {e}")
            return {'CANCELLED'}

        target_name = f"{orig_name} (Mirror {s.mirror_axis})"
        existing_idx = next((i for i, r in enumerate(s.cuts) if r.name == target_name), None)
        if existing_idx is not None:
            plan.remove_record(context, existing_idx)
            rec = next((r for r in s.cuts if r.name == orig_name), active_record(context))

        new_rec = plan.add_record(context, target, rec.cut_type, [mirrored_a])
        plan.copy_connector_props(rec, new_rec)
        new_rec.name = target_name

        orig_pin = bpy.data.objects.get(rec.pin_a)
        new_pin = bpy.data.objects.get(new_rec.pin_a)

        # Preserve connector mesh if it was modified or custom
        if orig_pin and new_pin and orig_pin.data:
            if rec.shape == 'CUSTOM' or rec.shape.startswith("CUSTOM_"):
                new_pin.data = orig_pin.data
            else:
                old_me = new_pin.data
                new_pin.data = orig_pin.data.copy()
                mesh_utils.remove_mesh(old_me)

        # Check polygon normal flip when mirroring
        new_sobj_a = bpy.data.objects.get(new_rec.surface_a)
        if sobj_a and new_sobj_a and len(sobj_a.data.polygons) > 0 and len(new_sobj_a.data.polygons) > 0:
            orig_poly_n = (sobj_a.matrix_world.to_3x3() @ sobj_a.data.polygons[0].normal).normalized()
            reflected_orig_n = (M.to_3x3() @ orig_poly_n).normalized()
            new_poly_n = (new_sobj_a.matrix_world.to_3x3() @ new_sobj_a.data.polygons[0].normal).normalized()
            if reflected_orig_n.dot(new_poly_n) < 0.0:
                new_rec.pin_side = 'B' if rec.pin_side == 'A' else 'A'

        # Exact transfer of primary pin without auto circle search
        if orig_pin and new_pin:
            new_pin.matrix_world = plan.mirror_pin_matrix(M, orig_pin.matrix_world)
            new_rec.pin_auto_a = plan.flat(new_pin.matrix_world)

        # Store exact mirrored center_a, normal_a and esp_plane_co / esp_plane_no
        new_rec.center_a = M @ Vector(rec.center_a)
        new_rec.normal_a = (M.to_3x3() @ Vector(rec.normal_a)).normalized()
        if rec.two_contact:
            new_rec.center_b = M @ Vector(rec.center_b)
            new_rec.normal_b = (M.to_3x3() @ Vector(rec.normal_b)).normalized()

        if new_sobj_a and sobj_a:
            new_sobj_a["esp_center_hint"] = list(new_rec.center_a)
            if "esp_plane_co" in sobj_a and "esp_plane_no" in sobj_a:
                mw_orig = sobj_a.matrix_world
                mw_new = new_sobj_a.matrix_world
                orig_co = mw_orig @ Vector(sobj_a["esp_plane_co"])
                rot_orig = mw_orig.to_3x3()
                orig_no = (rot_orig.inverted_safe().transposed() @ Vector(sobj_a["esp_plane_no"])).normalized()
                mir_co = M @ orig_co
                mir_no = (M.to_3x3() @ orig_no).normalized()
                new_sobj_a["esp_plane_co"] = list(mw_new.inverted_safe() @ mir_co)
                new_sobj_a["esp_plane_no"] = list(new_sobj_a.matrix_world.to_3x3().inverted_safe().transposed() @ mir_no)

        # Mirror all extra pins
        new_ep_names = []
        for i, old_ep_obj in enumerate(plan.extra_pin_objects(rec), start=2):
            new_ep_mat = plan.mirror_pin_matrix(M, old_ep_obj.matrix_world)
            new_ep_name = f"ESP_Pin_{new_rec.name}_{i}"
            new_ep_obj = plan.create_pin_object(context, new_ep_name, rec.shape, new_ep_mat)
            if old_ep_obj.data is not None:
                old_me = new_ep_obj.data
                new_ep_obj.data = old_ep_obj.data.copy()
                mesh_utils.remove_mesh(old_me)
            new_ep_obj.hide_viewport = not (new_rec.add_pin and new_rec.show)
            new_ep_names.append(new_ep_obj.name)

        if new_ep_names:
            new_rec["esp_extra_pins"] = "\t".join(new_ep_names)

        self.report({'INFO'}, f"Created mirrored cut '{new_rec.name}'")
        return {'FINISHED'}


CLASSES = (
    ESP_OT_new_cut,
    ESP_OT_delete_cut,
    ESP_OT_move_cut,
    ESP_OT_swap_pin_side,
    ESP_OT_reset_pin,
    ESP_OT_select_pin,
    ESP_OT_add_connector,
    ESP_OT_refresh_pins,
    ESP_OT_edit_surface,
    ESP_OT_edit_surface_mesh,
    ESP_OT_reset_surface_mesh,
    ESP_OT_object_joint,
    ESP_OT_swap_joint_roles,
    ESP_OT_set_selected_cutters,
    ESP_OT_clear_missing_cutters,
    ESP_OT_mirror_cut,
    ESP_OT_build,
    ESP_OT_return_to_plan,
    ESP_OT_approve,
    ESP_OT_clear_plan,
    ESP_OT_hide_diagnosis,
)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
