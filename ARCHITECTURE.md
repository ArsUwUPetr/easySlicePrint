# Architecture

EasySlice Print is a Blender 4.2+ add-on. `__init__.py` registers the property,
preference, plan, overlay, operator, and UI modules in dependency order. The add-on
uses Blender's Python API and has no separate runtime dependencies.

## Modules

```text
easy_slice_print/
├── __init__.py             registration order
├── blender_manifest.toml   Blender extension metadata
├── props.py                Scene settings and persistent cut records
├── prefs.py                add-on preferences
├── plan.py                 plan state, previews, validation, and spec conversion
├── ops_tools.py            modal cut tools and Quick mode execution
├── ops_plan.py             plan, build, Object Joint, mirror, and edit operators
├── ops_misc.py             explode/collapse, export, connector library, mesh checks
├── jobs.py                 timer-driven modal job execution and UI redraws
├── ui.py                   sidebar panels and cut-list UI
├── draw.py                 viewport drawing helpers
├── overlay.py              failed-cut diagnosis overlay
└── core/
    ├── units.py            Blender-unit and millimetre conversion
    ├── mesh_utils.py       mesh copies, booleans, ray tests, and mesh operations
    ├── section.py          cross-section and material-interval queries
    ├── surfaces.py         cut patches, membranes, skirts, and connector surfaces
    ├── connectors.py       unit pin/socket meshes and connector library helpers
    ├── diagnosis.py        detection and scoring of material bridges
    └── cutting.py          cut specifications and geometry execution
```

`plan.py` connects Blender scene data and preview objects to the geometry
operations. The `core` modules contain geometry and mesh operations; they use
Blender data APIs where needed.

## Cut workflows

The modal tools create a `ContactData` preview. `plan.py` stores the preview,
control points, cutter geometry, connector transform, and settings, then converts
the record to a `CutSpec` / `ContactSpec` for execution. Quick mode runs the same
cut pipeline immediately; Plan mode stores the operation and defers execution to
Build.

### Plane cuts

`section.plane_section` bisects an evaluated mesh copy, fills the section loops,
splits the fill into connected islands, and selects islands overlapping the drawn
stroke span. This lets the stroke choose which intersected regions are cut without
using the stroke bounds as the cut surface.

The displayed section and the Boolean cutter can differ. `section.clip_rect`
builds a cutter with a rim clear of the model where possible, reducing difficult
near-tangent Boolean intersections. If no separating rectangle can represent the
selected regions, the section itself is used. If a section cannot be built, the
tool falls back to a rectangular patch around the stroke. In Plan mode, moving or
rotating a plane preview re-sections the target and rebuilds the cutter.

### Curve cuts

`plan.ribbon_surfaces` samples the drawn curve and ray-casts through each sample
to find material intervals. `section.band_around` selects the interval intersected
by the stroke and expands it into adjacent empty space. The preview ribbon follows
the sampled surface depth; the Boolean cutter uses a consistent depth range and
extends beyond the model.

### Freehand cuts

Freehand control points are stored in world space, allowing the user to continue
the loop from another view. `surfaces.loop_patch` builds a filled membrane from the
loop. The membrane is continued beyond its rim to create a Boolean cutter skirt.
Rim points are settled onto the model, and skirt offsets are smoothed to reduce
folding and self-intersection. Loop smoothing and surface detail are user settings.

### Split and diagnosis

`cutting.perform_cut_steps` thickens each patch by the configured cut gap, subtracts
the resulting slab, separates loose mesh components, classifies them against the
cut surface, and joins components on the same side. Classification samples vertices
near the new cut face and weights their vote by distance to the surface. Two-contact
cuts subtract both contact slabs in the same operation.

If the Boolean does not produce components on both sides, `diagnosis.diagnose`
searches the result mesh for connections between the two sides. The resulting
diagnosis scores points on the preview surface. `overlay.py` displays the score as
a color gradient without modifying the model.

### Connectors

Connector placement is represented by a transform whose local +Z axis points into
the socket side. `connectors.py` scales a unit connector mesh into a pin and a
clearance-adjusted socket. Fit settings determine radial clearance; axial tip
clearance is handled separately. Connector settings, custom connector meshes,
additional pins, and optional mirror settings are managed through plan records.
For flat patches, `section.inscribed_circle` estimates a connector position and
size from the available material.

## Object Joint

Object Joint subtracts one or more cutter objects from a target with a mating
clearance, then adds calibrated connectors. Its geometry path is
`cutting.perform_object_joint_steps`: it copies objects into world space, cleans
the meshes, rejects non-overlapping pairs with bounding-box checks, applies
clearance-inflated Boolean differences, and places connector pins and sockets.
The operation supports multiple target and cutter parts and preserves source
identity on generated objects so later plan cuts can find related parts.

Plan records store cutter object names and report missing references during
validation. Build resolves the current target and cutter parts before running the
Object Joint. The swap-roles operator reverses the carved and cutter roles.

## Plan data and Build

`Scene.esp.cuts` contains `ESP_CutRecord` items with operation type, target and
cutter references, enabled/visible state, preview object names, anchors, build
state, and per-contact connector settings. Preview objects live in the plan
collection; source objects are preserved in the backup collection while a build
is active; generated parts live in a build collection.

Surface previews store geometry and custom properties needed to rebuild procedural
patches. Their world-space patch is reconstructed from the preview transform.
Custom mesh edits and modifiers can be retained or reset to the procedural surface.
Pin edits are stored relative to the automatic transform so changing connector
settings does not discard the user's adjustment.

Before Build, `plan.validate_plan` checks enabled cuts and can report issues in the
plan panel. Build processes enabled cuts in order and updates each record's state
and the overall progress. Ordinary cuts select the current part nearest the cut
anchor; Object Joint resolves the current target and cutter parts by their stored
source identity. Failed cuts either stop the build or are skipped according to the
scene setting.

Build starts from the preserved source state and replaces any previous build.
**Back to Plan** removes generated parts and restores the source and previews.
**Approve** keeps the built result and finalizes the completed plan. Records that
were not built remain available for further editing.

## Other operations

Explode computes and caches per-part movement directions when first triggered,
then reuses them while the distance control changes. Direction calculation
accounts for overlapping parts and keeps the largest part as the stationary
reference. Export, connector-library management, and mesh checks are implemented
in `ops_misc.py`.

Long-running modal operations use `JobMixin` in `jobs.py` to yield progress through
Blender timers and redraw the UI between steps.

## Testing

`tests/test_core.py` exercises geometry operations and mesh invariants.
`tests/test_addon.py` registers the add-on and tests operator workflows, including
Plan and Quick modes, Object Joint, Build/return/approve, export, explode, and panel
drawing. Run both headlessly with `scripts/run_tests.sh` and a Blender executable
available on `PATH` or through the `BLENDER` environment variable.
