# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Rafael Omodei and EasySlice Print contributors
"""Run a long cut as a modal job instead of blocking Blender's main thread."""

import time
import bpy

JOB_TICK = 0.02  # timer interval: gives Blender's UI loop time to redraw N-panel

RUNNING = 'RUNNING'
DONE = 'DONE'
ERROR = 'ERROR'
CANCELLED = 'CANCELLED'


def redraw_ui(context):
    """Force 3D Viewport and Sidebar (N-panel) to redraw immediately."""
    try:
        wm = context.window_manager
        for win in wm.windows:
            for area in win.screen.areas:
                if area.type in {'VIEW_3D', 'STATUSBAR'}:
                    area.tag_redraw()
                    for reg in area.regions:
                        if reg.type in {'UI', 'WINDOW', 'HEADER'}:
                            reg.tag_redraw()
    except Exception:
        pass


class JobMixin:
    """Mixin for modal operators that need to run a step generator."""

    _job = None
    _job_timer = None
    _job_window = None
    _job_title = ""
    _job_label = ""
    _job_t0 = 0.0

    # -- lifecycle ----------------------------------------------------------
    def job_start(self, context, generator, title, add_handler=False):
        self._job = generator
        self._job_title = title
        self._job_label = "starting"
        self._job_t0 = time.time()
        self._job_window = context.window
        wm = context.window_manager
        self._job_timer = wm.event_timer_add(JOB_TICK, window=context.window)
        if add_handler:
            wm.modal_handler_add(self)
        if self._job_window is not None:
            try:
                self._job_window.cursor_modal_set('WAIT')
            except Exception:
                pass
        self.job_status(context)
        redraw_ui(context)
        return {'RUNNING_MODAL'}

    def job_running(self):
        return self._job is not None

    def job_stop(self, context):
        if self._job_timer is not None:
            try:
                context.window_manager.event_timer_remove(self._job_timer)
            except Exception:
                pass
            self._job_timer = None
        if self._job is not None:
            self._job.close()
            self._job = None
        if self._job_window is not None:
            try:
                self._job_window.cursor_modal_restore()
            except Exception:
                pass
            self._job_window = None
        try:
            context.workspace.status_text_set(None)
        except Exception:
            pass
        redraw_ui(context)

    # -- driving ------------------------------------------------------------
    def job_step(self, context, event):
        """Advance the job by exactly ONE step per timer tick so Blender redraws the UI."""
        if event.type in {'ESC'} and event.value == 'PRESS':
            self.job_stop(context)
            return CANCELLED, None
        if event.type != 'TIMER':
            return RUNNING, None

        try:
            label = next(self._job)
        except StopIteration as stop:
            self.job_stop(context)
            return DONE, stop.value
        except Exception as exc:
            self.job_stop(context)
            return ERROR, exc

        if label:
            self._job_label = label
        self.job_status(context)
        redraw_ui(context)
        return RUNNING, None

    def job_status(self, context):
        elapsed = time.time() - self._job_t0
        text = f"EasySlice {self._job_title}: {self._job_label}...  ({elapsed:.1f}s)  |  Esc: cancel"
        try:
            context.workspace.status_text_set(text)
        except Exception:
            pass
