# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Rafael Omodei and EasySlice Print contributors
"""Sidebar panels (3D Viewport > N panel > EasySlice tab)."""

import bpy

from . import overlay, plan, props
from .ops_plan import active_record
from .version import STAGE, VERSION

CATEGORY = "EasySlice"
TYPE_ICON = {
    'STRAIGHT': 'MESH_PLANE',
    'CURVED': 'CURVE_BEZCURVE',
    'FREEHAND': 'GREASEPENCIL',
    'OBJECT': 'MOD_BOOLEAN',
}
TYPE_LABEL = {
    'STRAIGHT': "Plane",
    'CURVED': "Curve",
    'FREEHAND': "Freehand",
    'OBJECT': "Object Joint",
}

STATE_ICON = {
    'WAIT': 'TIME',
    'ACTIVE': 'PLAY',
    'DONE': 'CHECKMARK',
    'SKIPPED': 'LOOP_FORWARDS',
    'ERROR': 'CANCEL',
}


class ESP_UL_cuts(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        target = plan.record_target(item)
        has_error = False
        if target is None:
            has_error = True
        elif item.cut_type == 'OBJECT':
            if not plan.cutter_object_list(item):
                has_error = True

        row = layout.row(align=True)
        # 1. Show the cut type icon or validation error to the left of the cut name.
        if has_error:
            row.label(text="", icon='ERROR')
        else:
            row.label(text="", icon=TYPE_ICON.get(item.cut_type, 'DOT'))

        # 2. Cut name.
        row.prop(item, "name", text="", emboss=False)

        # 3. Build-state icon before the enabled checkbox.
        state = getattr(item, "build_state", 'NONE')
        if state in STATE_ICON:
            row.label(text="", icon=STATE_ICON[state])
        elif item.built:
            row.label(text="", icon='CHECKMARK')

        # 4. Enabled, visibility, and delete controls.
        row.prop(item, "enabled", text="")
        row.prop(item, "show", text="", icon='HIDE_OFF' if item.show else 'HIDE_ON', emboss=False)
        op = row.operator("esp.delete_cut", text="", icon='X', emboss=False)
        op.index = index


class ESPPanel:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = CATEGORY


class ESP_PT_main(ESPPanel, bpy.types.Panel):
    bl_label = "EasySlice Print"
    bl_idname = "ESP_PT_main"

    def draw(self, context):
        layout = self.layout
        s = context.scene.esp
        row = layout.row(align=True)
        row.label(text=f"EasySlice Print {VERSION}", icon='MOD_BOOLEAN')
        row.operator("esp.check_mesh", text="", icon='CHECKMARK')
        row.operator("esp.open_docs", text="", icon='QUESTION')
        if STAGE:
            layout.label(text=f"{STAGE.capitalize()} - expect bugs, check parts", icon='ERROR')
        col = layout.column(align=True)
        col.label(text="Mode")
        col.row(align=True).prop(s, "mode", expand=True)
        if s.mode == 'QUICK':
            col.label(text="Immediate final cut", icon='TIME')
        else:
            col.label(text="Non-destructive plan history", icon='RECOVER_LAST')
        target = plan.resolve_target(context)
        if target is None:
            layout.label(text="Select a mesh object to cut", icon='ERROR')
        else:
            layout.label(text=f"Target: {target.name}", icon='OBJECT_DATA')
        if s.last_message:
            layout.label(text=s.last_message, icon='INFO')
        shown = overlay.shown()
        if shown is not None:
            box = layout.box()
            row = box.row(align=True)
            title = f"Why '{shown.label}' failed" if shown.label else "Why the cut failed"
            row.label(text=title, icon='COLORSET_01_VEC')
            row.operator("esp.hide_diagnosis", text="", icon='X', emboss=False)
            if shown.cut:
                box.label(text="Red: the halves stay joined here", icon='DOT')
                box.label(text="Green: the cut goes through", icon='DOT')
            else:
                box.label(text="Red: none of the surface cuts the model", icon='DOT')


class ESP_PT_tools(ESPPanel, bpy.types.Panel):
    bl_label = "Cut Tools"
    bl_parent_id = "ESP_PT_main"

    def draw(self, context):
        layout = self.layout
        s = context.scene.esp
        row = layout.row(align=True)
        row.scale_y = 1.4
        row.operator("esp.cut_straight", text="Plane", icon='MESH_PLANE', depress=s.tool == 'STRAIGHT')
        row.operator("esp.cut_curved", text="Curve", icon='CURVE_BEZCURVE', depress=s.tool == 'CURVED')
        row.operator("esp.cut_freehand", text="Freehand", icon='GREASEPENCIL', depress=s.tool == 'FREEHAND')
        row2 = layout.row(align=True)
        row2.scale_y = 1.3
        row2.operator(
            "esp.object_joint",
            text="Object Joint (Multi-Part Boolean + Pin)",
            icon='MOD_BOOLEAN',
        )
        col = layout.column(align=True)
        col.prop(s, "surface_margin", slider=True)
        if s.mode == 'PLAN':
            col.prop(s, "surface_origin", text="Origin")
        if s.tool == 'FREEHAND':
            col.prop(s, "loop_smoothing", slider=True)
            col.prop(s, "surface_detail")
        elif s.tool == 'STRAIGHT':
            col.prop(s, "plane_fill_mode", text="Fill")
            col.prop(s, "section_points", text="Rim Points")
            if s.plane_fill_mode == 'RADIAL':
                col.prop(s, "section_rings", text="Rings")
        else:
            col.prop(s, "stroke_smoothing", slider=True)
            col.prop(s, "control_points")
            col.prop(s, "surface_detail")
        layout.prop(s, "chain_cuts")


class ESP_PT_plan(ESPPanel, bpy.types.Panel):
    bl_label = "Planned Cuts"
    bl_idname = "ESP_PT_plan"
    bl_parent_id = "ESP_PT_main"

    @classmethod
    def poll(cls, context):
        return context.scene.esp.mode == 'PLAN'

    def draw(self, context):
        layout = self.layout
        s = context.scene.esp
        row = layout.row()
        row.template_list("ESP_UL_cuts", "", s, "cuts", s, "active_cut", rows=4)
        col = row.column(align=True)
        col.operator("esp.new_cut", text="", icon='ADD')
        col.operator("esp.delete_cut", text="", icon='REMOVE').index = s.active_cut
        col.separator()
        op_up = col.operator("esp.move_cut", text="", icon='TRIA_UP')
        op_up.direction = 'UP'
        op_down = col.operator("esp.move_cut", text="", icon='TRIA_DOWN')
        op_down.direction = 'DOWN'
        col.separator()
        col.operator("esp.refresh_pins", text="", icon='FILE_REFRESH')

        # Fast visual pre-flight check in list
        errs = plan.validate_plan(context, paint_overlay=False)
        if errs:
            box = layout.box()
            box.label(text=f"{len(errs)} issue(s) detected:", icon='ERROR')
            for e in errs[:3]:
                box.label(text=e)
            if len(errs) > 3:
                box.label(text=f"...and {len(errs) - 3} more")

        # Live Progress Bar right under the cut list (shows overall build progress)
        if s.build_in_progress or any(getattr(r, "build_state", 'NONE') != 'NONE' for r in s.cuts):
            pbox = layout.box()
            pcol = pbox.column(align=True)

            enabled_cuts = [r for r in s.cuts if r.enabled]
            tot_cnt = len(enabled_cuts)
            done_cnt = sum(
                1 for r in enabled_cuts
                if getattr(r, "build_state", 'NONE') in {'DONE', 'SKIPPED'} or r.built
            )

            # Show the build status and total number of completed cuts.
            if s.build_in_progress and s.build_status:
                pcol.label(text=s.build_status, icon='TIME')
            else:
                pcol.label(
                    text=f"Build finished: {done_cnt}/{tot_cnt} cuts completed",
                    icon='CHECKMARK' if done_cnt == tot_cnt and tot_cnt > 0 else 'INFO',
                )

            # Show overall build progress as a percentage and completed/total count.
            prow = pcol.row(align=True)
            prow.enabled = False
            prow.prop(
                s,
                "build_progress",
                text=f"Completed {done_cnt}/{tot_cnt}",
                slider=True,
            )

        if s.built:
            row = layout.row(align=True)
            row.scale_y = 1.2
            row.operator("esp.return_to_plan", icon='LOOP_BACK')
            row.operator("esp.approve", icon='CHECKMARK')
        row = layout.row(align=True)
        row.scale_y = 1.2
        row.operator("esp.build", text="Building..." if s.build_in_progress else "Build Plan", icon='MOD_BUILD')
        row.operator("esp.clear_plan", icon='TRASH')


class ESP_PT_cut_settings(ESPPanel, bpy.types.Panel):
    bl_label = "Cut Settings"
    bl_idname = "ESP_PT_cut_settings"
    bl_parent_id = "ESP_PT_plan"

    @classmethod
    def poll(cls, context):
        return context.scene.esp.mode == 'PLAN' and active_record(context) is not None

    def draw(self, context):
        layout = self.layout
        rec = active_record(context)

        row = layout.row(align=True)
        row.label(text=TYPE_LABEL.get(rec.cut_type, ""), icon=TYPE_ICON.get(rec.cut_type, 'DOT'))
        row.prop(rec, "name", text="")
        row.prop(rec, "enabled")

        # Target mesh picker with eye dropper
        t_box = layout.box()
        t_row = t_box.row(align=True)
        t_row.alert = plan.record_target(rec) is None
        t_row.prop(rec, "target_obj", text="Target")
        if plan.record_target(rec) is None:
            w_row = t_box.row(align=True)
            w_row.alert = True
            w_row.label(text=f"Missing: '{rec.target or 'None'}' (Pick a mesh)", icon='ERROR')

        # Cutter mesh picker (for OBJECT cuts)
        if rec.cut_type == 'OBJECT':
            c_box = layout.box()
            c_row = c_box.row(align=True)
            c_row.prop(rec, "cutter_obj", text="Cutter")

            status = plan.cutter_status_labels(rec)
            if isinstance(status, dict):
                valid_c = status.get("valid", [])
                missing_c = status.get("missing", [])
            else:
                valid_c = [o.name for o in plan.cutter_object_list(rec)]
                missing_c = plan.missing_cutter_names(rec) if hasattr(plan, "missing_cutter_names") else []

            if valid_c:
                c_box.label(text="Cutters: " + ", ".join(valid_c), icon='CHECKMARK')
            if missing_c:
                m_row = c_box.row(align=True)
                m_row.alert = True
                m_row.label(text="Missing: " + ", ".join(missing_c), icon='ERROR')
                m_row.operator("esp.clear_missing_cutters", text="Clean", icon='X')

            jrow = c_box.row(align=True)
            jrow.operator("esp.swap_joint_roles", text="Swap Target <-> Cutter", icon='ARROW_LEFTRIGHT')

        # Cut Gap belongs to the cut itself, not the connector
        layout.prop(rec, "cut_gap_mm")

        # Surface Geometry & Roles
        sobj_a = bpy.data.objects.get(rec.surface_a) if rec.surface_a else None
        is_custom_a = sobj_a is not None and bool(sobj_a.get("esp_custom_mesh"))

        s_box = layout.box()
        s_box.label(text="Surface Geometry", icon='SURFACE_DATA')
        row = s_box.row(align=True)
        if not is_custom_a and rec.cut_type != 'OBJECT':
            row.operator("esp.edit_surface", text="Edit Points/Plane", icon='EDITMODE_HLT').index = 0
        row.operator(
            "esp.edit_surface_mesh",
            text="Edit Mesh (Tab)" if is_custom_a else "Convert to Mesh",
            icon='MESH_DATA',
        ).index = 0
        if is_custom_a:
            row.operator("esp.reset_surface_mesh", text="", icon='LOOP_BACK').index = 0
        s_box.operator("esp.refresh_pins", text="Refresh Cut", icon='FILE_REFRESH')


class ESP_PT_connector(ESPPanel, bpy.types.Panel):
    bl_label = "Connectors"
    bl_idname = "ESP_PT_connector"
    bl_parent_id = "ESP_PT_plan"

    @classmethod
    def poll(cls, context):
        return context.scene.esp.mode == 'PLAN' and active_record(context) is not None

    def draw_header(self, context):
        rec = active_record(context)
        if rec is not None:
            self.layout.prop(rec, "add_pin", text="")

    def draw(self, context):
        # Content is organized inside child sub-panels:
        #   |-- Connector Settings
        #   |-- Fit & Clearance
        pass


class ESP_PT_connector_settings(ESPPanel, bpy.types.Panel):
    bl_label = "Connector Settings"
    bl_idname = "ESP_PT_connector_settings"
    bl_parent_id = "ESP_PT_connector"

    @classmethod
    def poll(cls, context):
        return context.scene.esp.mode == 'PLAN' and active_record(context) is not None

    def draw(self, context):
        layout = self.layout
        rec = active_record(context)
        layout.active = rec.add_pin

        col = layout.column()
        row = col.row(align=True)
        row.prop(rec, "shape", text="Shape")
        row.operator("esp.connector_library", text="", icon='ASSET_MANAGER')

        col.row(align=True).prop(rec, "size_preset", expand=True)
        row = col.row(align=True)
        row.enabled = rec.size_preset == 'CUSTOM'
        row.prop(rec, "pin_width_mm")
        row.prop(rec, "pin_height_mm")

        # Pin / Socket roles
        if rec.cut_type == 'OBJECT':
            pbox = col.box()
            row = pbox.row(align=True)
            row.prop(rec, "pin_side", expand=True)
            row.operator("esp.swap_pin_side", text="", icon='UV_SYNC_SELECT')

            info_col = pbox.column(align=True)
            t_obj = plan.record_target(rec)
            target_name = t_obj.name if t_obj else f"{rec.target or 'Target'} (missing)"

            status = plan.cutter_status_labels(rec)
            if isinstance(status, dict):
                c_parts = status.get("valid", []) + [f"{m} (missing)" for m in status.get("missing", [])]
            else:
                c_parts = list(status)
            c_name = ", ".join(c_parts) if c_parts else "None (missing)"

            if rec.pin_side == 'A':
                info_col.label(text=f"Pin: {target_name}", icon='EXPORT')
                info_col.label(text=f"Socket: {c_name}", icon='IMPORT')
            else:
                info_col.label(text=f"Pin: {c_name}", icon='EXPORT')
                info_col.label(text=f"Socket: {target_name}", icon='IMPORT')
        else:
            row = col.row(align=True)
            row.prop(rec, "pin_side", expand=True)
            row.operator("esp.swap_pin_side", text="", icon='ARROW_LEFTRIGHT')

        # Pin viewport operations
        p_box = col.box()
        p_box.label(text="Pin Operations", icon='ORIENTATION_LOCAL')
        prow = p_box.row(align=True)
        prow.operator("esp.select_pin", text="Select Pin", icon='RESTRICT_SELECT_OFF').index = 0
        prow.operator("esp.add_connector", text="Add Pin", icon='ADD')
        prow.operator("esp.reset_pin", text="", icon='LOOP_BACK')
        prow.operator("esp.refresh_pins", text="Refresh", icon='FILE_REFRESH')


class ESP_PT_fit(ESPPanel, bpy.types.Panel):
    bl_label = "Fit & Clearance"
    bl_idname = "ESP_PT_fit"
    bl_parent_id = "ESP_PT_connector"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return context.scene.esp.mode == 'PLAN' and active_record(context) is not None

    def draw(self, context):
        layout = self.layout
        rec = active_record(context)
        layout.active = rec.add_pin
        layout.label(text="How the printed parts go together")
        layout.row(align=True).prop(rec, "fit_preset", expand=True)
        row = layout.row()
        row.enabled = rec.fit_preset == 'CUSTOM'
        row.prop(rec, "clearance_mm")
        widened = rec.clearance_mm * 2.0
        layout.label(text=f"Socket is {widened:.2f} mm wider than pin", icon='DRIVER_DISTANCE')
        if rec.fit_preset != 'CUSTOM':
            layout.label(
                text=f"From Printer Clearance {plan.printer_clearance_mm(context):.2f} mm (Preferences)",
                icon='TOOL_SETTINGS',
            )
        layout.separator()
        layout.prop(rec, "asymmetric")
        row = layout.row()
        row.active = rec.asymmetric
        row.prop(rec, "tip_extra_mm")


class ESP_PT_mirror(ESPPanel, bpy.types.Panel):
    bl_label = "Mirror"
    bl_idname = "ESP_PT_mirror"
    bl_parent_id = "ESP_PT_plan"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return context.scene.esp.mode == 'PLAN' and active_record(context) is not None

    def draw(self, context):
        layout = self.layout
        s = context.scene.esp
        mrow = layout.row(align=True)
        mrow.label(text="Axis")
        mrow.prop(s, "mirror_axis", expand=True)
        layout.prop(s, "mirror_pivot", text="Center")
        if s.mirror_pivot == 'OBJECT':
            layout.prop(s, "mirror_object", text="Object")
        layout.prop(s, "mirror_snap_surface", text="Snap to Mesh")
        layout.operator("esp.mirror_cut", text=f"Mirror Cut ({s.mirror_axis})", icon='MOD_MIRROR')


class ESP_PT_quick_settings(ESPPanel, bpy.types.Panel):
    bl_label = "Quick Cut Settings"
    bl_idname = "ESP_PT_quick_settings"
    bl_parent_id = "ESP_PT_main"

    @classmethod
    def poll(cls, context):
        return context.scene.esp.mode == 'QUICK'

    def draw(self, context):
        layout = self.layout
        s = context.scene.esp
        layout.prop(s, "cut_gap_mm")
        layout.separator()
        layout.prop(s, "add_pin")
        col = layout.column()
        col.active = s.add_pin
        row = col.row(align=True)
        row.prop(s, "shape", text="Shape")
        row.operator("esp.connector_library", text="", icon='ASSET_MANAGER')
        col.row(align=True).prop(s, "size_preset", expand=True)
        row = col.row(align=True)
        row.enabled = s.size_preset == 'CUSTOM'
        row.prop(s, "pin_width_mm")
        row.prop(s, "pin_height_mm")
        col.row(align=True).prop(s, "pin_side", expand=True)
        col.separator()
        col.row(align=True).prop(s, "fit_preset", expand=True)
        row = col.row()
        row.enabled = s.fit_preset == 'CUSTOM'
        row.prop(s, "clearance_mm")
        col.prop(s, "asymmetric")
        row = col.row()
        row.active = s.asymmetric
        row.prop(s, "tip_extra_mm")


class ESP_PT_options(ESPPanel, bpy.types.Panel):
    bl_label = "Options"
    bl_parent_id = "ESP_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        s = context.scene.esp
        layout.prop(s, "keep_original")
        row = layout.row()
        row.active = s.mode == 'PLAN'
        row.prop(s, "skip_failed")
        layout.prop(s, "diagnose_failed")


class ESP_PT_remesh(ESPPanel, bpy.types.Panel):
    bl_label = "Remesh (Plan Mode only)"
    bl_parent_id = "ESP_PT_options"
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.prop(context.scene.esp, "remesh_enable", text="")

    def draw(self, context):
        layout = self.layout
        s = context.scene.esp
        layout.active = s.remesh_enable and s.mode == 'PLAN'
        layout.prop(s, "remesh_voxel_mm")
        layout.prop(s, "remesh_adaptivity", slider=True)
        layout.prop(s, "remesh_smooth")


class ESP_PT_explode(ESPPanel, bpy.types.Panel):
    bl_label = "Exploded View"
    bl_parent_id = "ESP_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        s = context.scene.esp
        layout.prop(s, "explode_distance_mm")
        if s.exploded:
            layout.operator("esp.collapse", icon='FULLSCREEN_EXIT')
        else:
            layout.operator("esp.explode", icon='FULLSCREEN_ENTER')


class ESP_PT_export(ESPPanel, bpy.types.Panel):
    bl_label = "Export"
    bl_parent_id = "ESP_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        s = context.scene.esp
        layout.prop(s, "export_folder")
        layout.prop(s, "export_format")
        row = layout.row()
        row.scale_y = 1.2
        row.operator("esp.export_parts", icon='EXPORT')


CLASSES = (
    ESP_UL_cuts,
    ESP_PT_main,
    ESP_PT_tools,
    ESP_PT_plan,
    ESP_PT_cut_settings,
    ESP_PT_connector,
    ESP_PT_connector_settings,
    ESP_PT_fit,
    ESP_PT_mirror,
    ESP_PT_quick_settings,
    ESP_PT_options,
    ESP_PT_remesh,
    ESP_PT_explode,
    ESP_PT_export,
)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
