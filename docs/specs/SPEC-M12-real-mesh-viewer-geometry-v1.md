# SPEC-M12 — Real mesh geometry for the 3D IFC viewer

## Objective

Replace the 3D viewer's per-element axis-aligned bounding box with the element's real,
`ifcopenshell`-computed triangulated geometry, so a door/window renders as its real shape
(including a real boolean-subtracted wall opening where the source model has one) instead
of an overlapping box — closing the specific limitation D-039/D-040/D-042 each named but
declined to fix, and giving the exterior-shell view (D-041) real interior structure to look
through instead of other bounding boxes.

## Rationale

`IfcRepository.viewer_elements` (`apps/api/app/tools/ifc/repository.py`) computes real
geometry via `ifcopenshell.geom.create_shape` but immediately discards everything except
the axis-aligned min/max of `shape.geometry.verts` — a deliberate, documented tradeoff
("avoids freezing the UI while a legacy browser parser reconstructs the complete 11 MB
source model"). Every downstream visual limitation traced to this same root cause across
four separate owner reports this session: washed-out interiors and haze (D-040, partially
— depth/opacity, not geometry), no real doorway cutout (named explicitly in D-040 and
D-042 as out of scope), and the door/window z-fighting D-042 fixed with a polygon-offset
bias rather than the geometry itself. This milestone fixes the geometry.

**Owner-approved scope, not a unilateral expansion.** The owner explicitly asked for this
plan before any code ("先出一个具体的实施计划"), reviewed the quantified numbers below, and
asked to proceed with both writing this spec and implementing it.

## Verified current-state assumptions

Re-measured directly against both real Dataset Pack buildings on 2026-09-15 (not assumed
from an earlier estimate), looping every supported type's *entire* `model.by_type(...)`
result (i.e. without `viewer_elements`'s own `per_type_limits` caps, to measure the true
upper bound):

| | Duplex | DigitalHub |
|---|---|---|
| Elements with real geometry | 116 | 313 |
| Real vertices / triangles | 3,088 / 5,704 | 73,704 / 144,024 |
| Cold `ifcopenshell.geom` compute time | 0.4s | 11.2s |
| Current bbox JSON payload | ~9 KB | ~23 KB |
| Naive real-mesh JSON payload (estimated: ~30 bytes/vertex, ~15 bytes/triangle) | ~174 KB | ~4.3 MB |

DigitalHub's 11-second cold compute is the fact that makes a caching layer non-optional,
not a nice-to-have: computing it per-request would make every viewer load on this building
feel broken.

**A cache already exists for this, verified by reading the code, not assumed new
infrastructure is needed.** `ServiceContainer.get_project` (`apps/api/app/services.py`)
already keeps one `ProjectResources` — and therefore one `IfcRepository` instance — alive
per `(project_id, source_set_id)` in `self._project_resources` for the life of the process,
for every non-`demo` project (SPEC-M9 §C). `IfcRepository.viewer_elements` is already a
`@cached_property` on that same long-lived instance, meaning it already only computes once
per process per project today. A second `@cached_property` on `IfcRepository` gets the
identical "compute once, serve from memory after" caching for free, with no new store, no
new persistence layer, and no new invalidation logic to design — reusing an architecture
this codebase already committed to and already trusts, rather than building a parallel one.
For the `demo` project specifically, `self.ifc_repository` is a single eagerly-built,
long-lived instance for the whole process (`ServiceContainer.__init__`), so the same
`@cached_property` mechanism applies identically there too.

## Allowed scope

### A. Backend: real per-element mesh geometry (own commit)

- `IfcRepository.mesh_elements` (`apps/api/app/tools/ifc/repository.py`), a new
  `@cached_property` alongside `viewer_elements`, not a replacement for it (`viewer_elements`
  keeps serving existing callers unchanged — see Invariants). For each element in the same
  `supported_types`/`per_type_limits` set `viewer_elements` already uses: call
  `ifcopenshell.geom.create_shape` (`USE_WORLD_COORDS = True`, matching `viewer_elements`)
  and keep the *real* `shape.geometry.verts` (flat float list, already world-space) and
  `shape.geometry.faces` (flat int list, already triangle triples — `ifcopenshell` triangulates
  internally, confirmed by inspecting `len(faces) % 3 == 0` against both real buildings) instead
  of collapsing them to a bounding box. Elements that raise on `create_shape` (the existing
  `except Exception` fallback path — public synthetic fixtures with lightweight semantic
  geometry) get a small synthetic box mesh (8 vertices, 12 triangles) built from the same
  `fallback_dimensions`/placement-origin logic `viewer_elements` already has, so every project
  — including the tiny synthetic `demo` fixture — returns a valid mesh payload with no
  special-casing at the API or frontend layer.
- Output shape per element: the same identity/metadata fields `viewer_elements` already
  returns (`express_id`, `global_id`, `entity_type`, `name`, `storey`, `color`, `is_external`),
  plus `vertices` (flat float array, world-space) and `faces` (flat int array, triangle
  indices) in place of `center`/`dimensions`.
- New endpoint `GET /api/v1/project/viewer-mesh` in `apps/api/app/main.py`, mirroring
  `project_viewer_elements` exactly (same `_resolve_project`/`repository.available` guard,
  same `asyncio.to_thread` deferral for the first, genuinely expensive, per-project compute).
  `viewer_elements`'s own endpoint is untouched.

### B. Frontend: `BufferGeometry` instead of `BoxGeometry` (own commit)

- `IfcViewer.tsx` fetches `/api/v1/project/viewer-mesh` instead of `/api/v1/project/viewer-elements`.
  Per element: build a `THREE.BufferGeometry` from a `Float32Array` position attribute (each
  vertex transformed with the same Z-up -> Y-up convention D-038 already established:
  `three.x = ifc.x, three.y = ifc.z, three.z = -ifc.y`) and a `Uint32Array` index from `faces`;
  call `geometry.computeVertexNormals()` (recomputed in Three.js's own convention rather than
  trusting IFC's own normal data, avoiding a second handedness conversion) and
  `geometry.computeBoundingBox()` (needed for `Box3.expandByObject`, which the existing
  camera-framing logic already calls). Mesh `position` stays at the origin — vertices are
  already absolute world coordinates, unlike the old bbox path's per-element `center` offset.
  All D-039/D-040/D-041/D-042 material work (palette, opacity, exterior-wall transparency,
  polygon-offset bias) carries over unchanged onto the new geometry — materials are
  orthogonal to geometry source, and D-042's polygon-offset bias is left in place as a
  harmless no-op safety margin in case a source model's own opening cut is imperfect or
  absent for a given door/window.
- The existing wall-occlusion-preference picking heuristic (`select()`'s
  `WALL_OCCLUSION_MARGIN` logic, D-024) is left in place, not removed — real geometry with a
  real opening should make it unnecessary in the common case, but it is a harmless fallback
  for any element whose source model does not model a real void/opening subtraction, not
  guaranteed dead code.

### C. Tests + docs (own commit)

- `tests/test_ifc_mesh_geometry.py`: `mesh_elements` returns real (non-bounding-box) vertex/
  triangle counts for the real Duplex fixture matching an independent direct `ifcopenshell`
  check (the same reproduction technique already used for D-041's `is_external` verification);
  confirms the synthetic `demo` fixture's fallback box path still returns a valid 8-vertex/
  12-triangle mesh per element with no exception; confirms `is_external`/`color` are present
  and match `viewer_elements`'s own values for the same element (the two cached properties
  must agree on everything except geometry representation).
- `docs/decisions/README.md` D-044 entry (root cause/fix/verification, matching this session's
  established format); `PROJECT_STATE.md` M12 milestone entry.

## Explicitly excluded scope

- **Binary transport.** This pass ships JSON only. DigitalHub's ~4.3 MB estimated JSON payload
  (vs. an estimated ~2.5 MB in a bespoke binary framing) is accepted for this milestone —
  real, but not large enough by itself to justify a second wire format before shipping the
  actually-requested geometry improvement. If real usage shows this payload size is a genuine
  problem, binary transport is a scoped, independent follow-on, not bundled here.
- **Reading the IFC's own real material/color/texture data** (`IfcStyledItem`/
  `IfcSurfaceStyle`). Out of scope since D-039 — this milestone changes geometry only, not the
  one-fixed-look-per-type palette.
- **A general BIM viewer replacement, glTF export, or any new external rendering
  dependency.** `THREE.BufferGeometry` built directly from `ifcopenshell`'s own triangulation
  is sufficient; no new library.
- **Precompute-at-ingest (e.g. at Dataset Pack fixture upload time).** The lazy,
  compute-on-first-request-then-cache-for-process-lifetime approach (Allowed Scope §A) is
  this milestone's only caching strategy. Precompute-at-ingest is a valid future optimization
  if DigitalHub's 11-second first-load proves to be a real problem in practice, not built
  speculatively here.
- **Concurrent-first-access de-duplication for the mesh computation specifically.** Two
  simultaneous first requests for the same uncached project could both trigger the expensive
  `mesh_elements` computation redundantly (wasted CPU, not incorrect data — the computation is
  pure/deterministic). `SPEC-M9`'s existing per-project `asyncio.Lock` guards *project
  resource loading* (the ADLS download), not this specific in-memory property computation;
  adding an equivalent lock for `mesh_elements` itself is a reasonable, small future
  hardening, named here as a known gap rather than silently left undiscoverable.

## Affected surfaces

- `apps/api/app/tools/ifc/repository.py` — new `mesh_elements` cached property.
- `apps/api/app/main.py` — new `GET /api/v1/project/viewer-mesh` route.
- `apps/web/src/IfcViewer.tsx` — fetch target, geometry construction, type additions.
- `tests/test_ifc_mesh_geometry.py` — new.
- `docs/decisions/README.md`, `PROJECT_STATE.md` — documentation only.

`viewer_elements` and its existing `/api/v1/project/viewer-elements` endpoint are not
modified — any other consumer of the bounding-box representation is unaffected.

## Invariants

- Zero model calls — this is, like `viewer_elements` before it, a pure deterministic
  geometry computation with no LLM involvement.
- `viewer_elements`/`/api/v1/project/viewer-elements` keep their exact current behavior and
  response shape — this milestone adds a new representation, it does not change or remove
  the old one.
- Every project, including the synthetic `demo` fixture, must return a valid mesh payload
  with no exception — no project-specific special-casing at the endpoint or frontend layer.
- No new external service, library, or wire format beyond what `Allowed scope` names.

## Acceptance criteria

- `mesh_elements` against the real Duplex fixture returns vertex/triangle counts matching an
  independent direct `ifcopenshell` check (same technique as D-041's `is_external`
  verification) — not merely "does not raise."
- `mesh_elements` against the synthetic `demo` fixture returns a valid box mesh (8 vertices,
  12 triangles) for every element, with no exception.
- The 3D viewer, loaded against the real Duplex building, visibly shows the interior
  structure through a real wall opening at a real door/window location (visually confirmed
  live in a browser, not only asserted from the payload) — the actual, falsifiable claim this
  milestone exists to prove.
- `PYTHONPATH=apps/api python3 -m pytest -q`, `ruff check --select F,E9,I,F401 apps/api tests
  scripts`, and `(cd apps/web && npm run build)` all clean after each lettered subsection.

## Documentation requirements

- `docs/decisions/README.md` D-044 (this milestone), following this session's established
  root-cause/fix/verification structure.
- `PROJECT_STATE.md` M12 milestone entry, matching the style of M8-M11's own entries.

## Git / stop conditions

- Branch `feat/m12-real-mesh-viewer-geometry`; one commit per lettered subsection (A, B, C).
- Stop and report rather than proceeding on own judgment if: DigitalHub's real cold-compute
  time or payload size, once actually measured end-to-end through the new endpoint (not just
  estimated as in Verified current-state assumptions above), differs meaningfully from the
  numbers here in a way that changes the go/no-go calculus; or if a real building's own
  opening/void modeling turns out not to produce a real cut in `ifcopenshell`'s computed
  shape (i.e. the geometry fix does not actually resolve the visual claim this spec exists to
  prove) — in that case the acceptance criterion fails honestly rather than being
  reinterpreted.

## Owner decisions

- **OD-46**: flagged for explicit owner sign-off, not assumed — shipping JSON-only transport
  for this milestone (Explicitly excluded scope), deferring binary transport to a future pass
  gated on real evidence it's needed. The owner approved this spec's overall direction and
  asked for both the write-up and an implementation to see the effect; this specific
  transport-format tradeoff was proposed in this document, not separately confirmed word for
  word, so it is named here rather than silently treated as settled.
- **OD-47**: flagged for explicit owner sign-off, not assumed — the lazy,
  compute-once-and-cache-on-the-existing-long-lived-`IfcRepository`-instance approach (no new
  external cache store, no precompute-at-ingest) as this milestone's caching strategy, for the
  same reason as OD-46.
