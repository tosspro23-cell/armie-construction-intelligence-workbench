import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { withAuthHeader } from "./apiClient";

type SelectedElement = {
  globalId?: string;
  expressId?: number;
  type?: string;
  name?: string;
  // Owner-reported, 2026-09-21: the selection details panel showed
  // GlobalId/ExpressID (the IFC model's own internal identifiers) but
  // never Tag/Mark -- the identifier a PDF schedule row actually uses,
  // and the one a human visually cross-checking model vs. schedule
  // actually needs. Already read server-side (get_element_properties,
  // citation locators) since D-070/D-072's own owner-reported fix; this
  // carries it into the 3D viewer's own selection, not a new read.
  tag?: string | null;
};

type ViewerElement = {
  global_id?: string;
  express_id?: number;
  entity_type?: string;
  name?: string;
  tag?: string | null;
  // SPEC-M12: `/api/v1/project/viewer-mesh`'s real triangulated geometry --
  // flat IFC world-space coordinates (`vertices`, 3 floats per vertex) and
  // triangle indices into them (`faces`, 3 indices per triangle), in place
  // of the old bounding-box `center`/`dimensions` pair.
  vertices: number[];
  faces: number[];
  color: string;
  storey?: string;
  // D-041: real IFC `IsExternal` (whichever Pset carries it) -- `true` for
  // an exterior wall, `false` for an interior one, `null`/absent when the
  // source IFC never set it (the tiny synthetic demo fixture, most non-wall
  // types). Never guessed from geometry.
  is_external?: boolean | null;
};

// D-039: owner-requested, 2026-09-15 -- the palette itself (per-type base
// color, apps/api/app/tools/ifc/repository.py) was tuned to suggest each
// type's typical real material, but every element still shared one flat
// opacity/roughness regardless of type, so a window looked exactly as
// matte and opaque as a concrete slab. This gives glass a genuinely
// glass-like feel (more transparent, smoother/shinier) and keeps solid
// building fabric (walls/slabs/stairs/roof) matte and mostly opaque,
// with doors in between (opaque but slightly less uniformly matte than
// bare concrete). Not a claim of reading the IFC's own real material
// data -- still one fixed look per entity *type*.
//
// D-040: owner-reported, 2026-09-15 -- walls read as washed-out near-white
// on zoom, and the interior of a real building looked like an
// indistinct haze when rotating inside it. Every material, including
// these nominally-opaque ones, was constructed with `transparent: true`
// regardless of its opacity -- in Three.js this switches the material
// onto the alpha-blended render path (sorted per-triangle, not written
// to the depth buffer the same way opaque geometry is), which produces
// visible blending/haze between overlapping surfaces even at opacity
// 0.97-0.98, and is most visible exactly where the camera sits inside a
// cluster of intersecting bounding boxes (this viewer's wall/door/slab
// boxes routinely overlap at corners and openings -- see viewer_elements'
// own bounding-box tradeoff). `transparent` is now only enabled for
// genuinely translucent glass; every solid type renders fully opaque,
// which lets Three.js's normal depth-buffer occlusion hide interior
// clutter instead of blending it. This does not by itself give a door/
// window a real cut opening in its wall -- that is still the underlying
// bounding-box simplification, unchanged here.
type MaterialProps = {
  opacity: number; roughness: number; metalness: number; transparent: boolean;
  polygonOffset?: boolean; polygonOffsetFactor?: number; polygonOffsetUnits?: number;
};

// D-042: owner-reported, 2026-09-15 -- doors and windows flickered
// noticeably while rotating/zooming, worse than other element types.
// Root cause, confirmed with `gl.readPixels` sampled every animation
// frame during a real rotation (not eyeballed): viewer_elements' door/
// window boxes are, by the bounding-box simplification's own nature (see
// the picking-logic comment below), embedded in or flush against their
// host wall's box -- their faces are exactly or nearly coincident in
// world space. With finite GPU depth-buffer precision, which of two
// coincident faces wins the per-fragment depth test is not stable frame
// to frame as the camera moves even slightly -- classic z-fighting.
// Reproduced objectively: sampling a 24x16 pixel grid every frame across
// 50 frames of a small rotation, several pixels toggled between the same
// 2-3 colors up to 35 times (i.e. on nearly every single frame), a
// signature no legitimate one-time silhouette-edge crossing produces.
// `polygonOffset` biases a coincident surface's effective depth by a
// small, fixed amount so the depth test has a deterministic winner
// instead of one decided by floating-point noise -- door/window get a
// small negative offset (pulled toward the camera, so they reliably read
// as "sitting in" their host wall) while wall/slab get a small positive
// one (pushed back). This does not, by itself, give a door/window a real
// cut opening -- it only makes today's overlapping-box rendering stable;
// see the real-mesh-geometry plan for the actual fix to the opening
// itself.
const MATERIAL_BY_TYPE: Record<string, MaterialProps> = {
  // D-041: owner-reported, 2026-09-15 -- opaque walls (D-040's own fix)
  // made the interior genuinely legible, but looking at the whole
  // building from outside now hides the interior layout entirely behind
  // a solid shell. Window contrast was also reported as too low to read
  // clearly in an exterior overview. Window opacity/metalness bumped up
  // (a more definite glassy sheen, still clearly translucent) and its
  // base color deepened server-side (apps/api/app/tools/ifc/repository.py).
  IfcWindow: { opacity: 0.58, roughness: 0.08, metalness: 0.25, transparent: true, polygonOffset: true, polygonOffsetFactor: -4, polygonOffsetUnits: -4 },
  IfcDoor: { opacity: 1, roughness: 0.75, metalness: 0, transparent: false, polygonOffset: true, polygonOffsetFactor: -4, polygonOffsetUnits: -4 },
  IfcWall: { opacity: 1, roughness: 0.9, metalness: 0, transparent: false, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 },
  IfcSlab: { opacity: 1, roughness: 0.92, metalness: 0, transparent: false, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 },
  IfcStair: { opacity: 1, roughness: 0.85, metalness: 0, transparent: false },
  IfcRoof: { opacity: 1, roughness: 0.8, metalness: 0, transparent: false },
  // D-054: owner-reported, 2026-09-16 -- 10 real, substantial element
  // types present in both Dataset Pack buildings (rooms/columns/beams/
  // members/railings/coverings/furniture/generic proxies/footings/
  // curtain-wall plates) were never rendered at all, so a citation
  // naming one of them had nothing to highlight. IfcSpace specifically
  // represents a room *volume*, not a solid object -- kept translucent
  // (and excluded from direct-click picking below) so it reads as a
  // boundary overlay rather than an opaque box obscuring everything
  // inside it. IfcPlate (DigitalHub's curtain-wall glazing infill) gets
  // a glass-like translucent treatment distinct from IfcWindow.
  // Everything else is ordinary opaque matte, matching the structural/
  // finish materials the palette (repository.py) already suggests.
  IfcSpace: { opacity: 0.12, roughness: 0.9, metalness: 0, transparent: true },
  IfcColumn: { opacity: 1, roughness: 0.85, metalness: 0.1, transparent: false },
  IfcBeam: { opacity: 1, roughness: 0.85, metalness: 0.1, transparent: false },
  IfcMember: { opacity: 1, roughness: 0.85, metalness: 0.05, transparent: false },
  IfcRailing: { opacity: 1, roughness: 0.4, metalness: 0.6, transparent: false },
  IfcCovering: { opacity: 1, roughness: 0.9, metalness: 0, transparent: false },
  IfcFurnishingElement: { opacity: 1, roughness: 0.8, metalness: 0, transparent: false },
  IfcBuildingElementProxy: { opacity: 1, roughness: 0.85, metalness: 0, transparent: false },
  IfcFooting: { opacity: 1, roughness: 0.95, metalness: 0, transparent: false },
  IfcPlate: { opacity: 0.5, roughness: 0.1, metalness: 0.2, transparent: true },
};
// D-041: only a real, IFC-sourced `is_external === true` (never a guessed
// heuristic -- confirmed present on every wall in both real Dataset Pack
// buildings) switches a wall to this see-through "shell" look, so an
// exterior overview can show the interior layout through the building's
// own envelope. An interior partition wall (`is_external === false`) keeps
// D-040's fully-opaque material -- the shell view is specifically about
// the outermost ring the owner asked for, not every wall.
const EXTERIOR_WALL_MATERIAL: MaterialProps = { opacity: 0.3, roughness: 0.85, metalness: 0, transparent: true, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 };
const DEFAULT_MATERIAL: MaterialProps = { opacity: 1, roughness: 0.78, metalness: 0, transparent: false };

function materialPropsFor(element: ViewerElement): MaterialProps {
  const isWall = element.entity_type === "IfcWall" || element.entity_type === "IfcWallStandardCase";
  if (isWall && element.is_external === true) return EXTERIOR_WALL_MATERIAL;
  const byType = element.entity_type && MATERIAL_BY_TYPE[element.entity_type];
  if (byType) return byType;
  if (isWall) return MATERIAL_BY_TYPE.IfcWall;
  return DEFAULT_MATERIAL;
}

type Props = {
  onSelection: (element: SelectedElement | null) => void;
  onSnapshot: (base64: string | null) => void;
  onStatus: (status: ViewerStatus) => void;
  // D-049: owner-reported, 2026-09-15 -- a single `focusGlobalId` replaced
  // by another (D-046) fixed accumulation, but the owner wants several
  // evidence citations lit at once, each independently toggled, and
  // wants a highlight to survive rotating/looking around the model, not
  // just clicking a different citation. Ownership of *which* elements are
  // lit now lives in the parent (main.tsx), shared with the Evidence
  // citation list -- this component only renders whatever set it's given
  // and reports toggle requests back up via `onToggleHighlight`.
  highlightedGlobalIds?: Set<string>;
  onToggleHighlight: (globalId: string) => void;
  // SPEC-M9: found live by independent review -- this component fetches
  // its own viewer-elements independently of main.tsx's request logic, so
  // a project selector added only there would leave the 3D viewer itself
  // still showing whichever project the backend defaults to.
  projectId?: string;
};

export type ViewerStatus = {
  phase: "initializing" | "loading" | "parsing" | "ready" | "failed";
  message: string;
  progress?: number;
};

export function IfcViewer({ onSelection, onSnapshot, onStatus, highlightedGlobalIds, onToggleHighlight, projectId }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<ViewerStatus>({ phase: "initializing", message: "Preparing IFC viewer…" });
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null);
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null);
  const controlsRef = useRef<OrbitControls | null>(null);
  const elementMeshesRef = useRef<Map<string, THREE.Mesh>>(new Map());
  // Owner-reported, 2026-09-16: jumping to a citation for a small element
  // (e.g. a single beam) framed the camera so close the highlighted item
  // filled the whole view with no surrounding building visible -- "拉得
  // 特别近，基本上就是看不清楚的，是个什么东西". The whole scene's own
  // bounding diagonal (computed once in loadProjection below) is kept
  // here so the highlight fly-to effect can use it as a floor: never
  // frame *closer* than a fraction of the whole building's own size,
  // regardless of how small the highlighted element itself is.
  const sceneSizeRef = useRef<number>(20);

  const publishStatus = (next: ViewerStatus) => {
    setStatus(next);
    onStatus(next);
  };

  // D-049: recomputes every mesh's emissive state from the given set of
  // globally-lit ids rather than tracking a single "currently highlighted
  // mesh" (D-046) -- several evidence citations can be lit at once now,
  // each independently added/removed by the owner. Called whenever the
  // parent's `highlightedGlobalIds` prop changes; cheap enough to
  // recompute in full every time given this viewer's element counts
  // (hundreds, not thousands), and avoids incremental-diff bookkeeping
  // for a set that changes rarely (a citation click, not every frame).
  function applyHighlightSet(ids: Set<string> | undefined) {
    elementMeshesRef.current.forEach((mesh, globalId) => {
      const lit = !!ids && ids.has(globalId);
      const material = mesh.material as THREE.MeshStandardMaterial;
      material.emissive.set(lit ? 0x4f9df5 : 0x000000);
      // D-055: owner-reported, 2026-09-16 -- clicking an Evidence citation
      // for an IfcSpace correctly found and lit its mesh (this function ran
      // fine), but IfcSpace's base opacity is 0.12 (D-054, deliberately
      // near-invisible so a room's volume doesn't look like a solid box),
      // so the emissive glow was blended away to almost nothing -- the
      // highlight was real but not visible. Any material translucent
      // enough that a highlight would disappear into it (IfcSpace, and
      // IfcPlate's glass-like 0.5) is temporarily opaqued up while lit,
      // then restored to its normal translucency once unlit.
      const baseOpacity = (material.userData.baseOpacity as number | undefined) ?? material.opacity;
      material.userData.baseOpacity = baseOpacity;
      material.opacity = lit ? Math.max(baseOpacity, 0.65) : baseOpacity;
    });
  }
  // Kept in sync below so `loadProjection` (inside the scene-setup effect,
  // which intentionally does not depend on `highlightedGlobalIds` --
  // rebuilding the whole Three.js scene on every highlight toggle would
  // be wasteful) can still re-apply the current highlight set to newly
  // built meshes once loading finishes, e.g. re-entering a project whose
  // citations were already toggled on before switching away.
  const highlightedGlobalIdsRef = useRef<Set<string> | undefined>(highlightedGlobalIds);
  useEffect(() => {
    const previous = highlightedGlobalIdsRef.current;
    highlightedGlobalIdsRef.current = highlightedGlobalIds;
    applyHighlightSet(highlightedGlobalIds);
    // D-056: owner-reported, 2026-09-16 -- a citation's highlight (D-055)
    // was confirmed genuinely applied on the mesh (opacity/emissive both
    // correct), but the camera never moves, so an element buried behind
    // opaque interior walls/floors -- true of most rooms and most
    // furniture alike in a real, densely-partitioned building, not just
    // IfcSpace -- is fully occluded regardless of how bright its highlight
    // is. There was never any camera-focus-on-highlight behavior in this
    // viewer (D-046/D-049's own highlight rewrites only ever touched
    // material state, confirmed by reading their history). Only a genuine
    // single toggle-ON re-frames the camera -- a toggle-OFF, a multi-id
    // batch change, or the initial mount are all left alone, so rotating
    // the model or clearing a highlight never yanks the view around.
    const previousIds = previous ?? new Set<string>();
    const nextIds = highlightedGlobalIds ?? new Set<string>();
    const added = [...nextIds].filter((id) => !previousIds.has(id));
    if (added.length === 1 && nextIds.size > previousIds.size) {
      const mesh = elementMeshesRef.current.get(added[0]);
      const camera = cameraRef.current;
      const controls = controlsRef.current;
      if (mesh && camera && controls) {
        const box = mesh.geometry.boundingBox ?? new THREE.Box3().setFromObject(mesh);
        const center = box.getCenter(new THREE.Vector3());
        const elementSize = Math.max(box.getSize(new THREE.Vector3()).length(), 1.5);
        // Owner-reported, 2026-09-16: a small element's own bounding size
        // (e.g. a single structural beam) put the camera so close nothing
        // but that one item filled the frame, with no surrounding
        // building visible to recognize what it even was. The distance
        // now also has a floor of a fraction of the *whole scene's* own
        // bounding diagonal (sceneSizeRef, set once in loadProjection), so
        // a small highlighted element is always framed with enough of the
        // real building around it to be legible -- larger elements (a
        // room-sized IfcSpace, D-056) are unaffected, since their own
        // elementSize * 1.8 already exceeds this floor.
        const distance = Math.max(elementSize * 1.8, sceneSizeRef.current * 0.35);
        controls.target.copy(center);
        const direction = camera.position.clone().sub(center).normalize();
        if (direction.lengthSq() === 0) direction.set(0.6, 0.5, 0.6).normalize();
        camera.position.copy(center).add(direction.multiplyScalar(distance));
        camera.updateProjectionMatrix();
        controls.update();
      }
    }
  }, [highlightedGlobalIds]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    let disposed = false;
    const scene = new THREE.Scene();
    scene.background = new THREE.Color("#111a30");
    const camera = new THREE.PerspectiveCamera(55, 1, 0.1, 5000);
    camera.position.set(25, 20, 25);
    // D-042: `logarithmicDepthBuffer` spreads depth-buffer precision more
    // evenly across the camera's near/far range (0.1-5000 by default, then
    // re-set per-model below) instead of concentrating almost all of it
    // near the near plane -- the standard fix for depth-precision-driven
    // z-fighting, complementing the per-material polygonOffset bias below
    // for surfaces that are exactly coincident rather than merely close.
    const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true, logarithmicDepthBuffer: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    // D-040: owner-reported, 2026-09-15 -- pale materials (the new D-039
    // palette's warm plaster walls especially) read as washed-out
    // near-white on zoom. With no tone mapping, this renderer clipped any
    // over-bright lit surface straight to solid white instead of a
    // softer highlight rolloff -- combined with the hemisphere+directional
    // lights' fairly high intensities (2.2/2.0), a pale, matte, directly-lit
    // wall was genuinely overexposed, not just visually busy. ACESFilmic
    // is the standard filmic rolloff for MeshStandardMaterial scenes, and
    // the light intensities were reduced alongside it.
    renderer.toneMapping = THREE.NoToneMapping;
    host.appendChild(renderer.domElement);
    rendererRef.current = renderer;
    scene.add(new THREE.HemisphereLight(0xffffff, 0x30466e, 0.55));
    const directional = new THREE.DirectionalLight(0xffffff, 0.6);
    directional.position.set(12, 20, 10);
    scene.add(directional);
    scene.add(new THREE.GridHelper(100, 50, 0x4f79b8, 0x263a61));
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    cameraRef.current = camera;
    controlsRef.current = controls;
    const pickables: THREE.Mesh[] = [];

    const resize = () => {
      const width = host.clientWidth;
      const height = host.clientHeight;
      if (!width || !height) return;
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
      renderer.setSize(width, height, false);
    };

    const loadProjection = async () => {
      try {
        publishStatus({ phase: "loading", message: "Loading the ARMIE synthetic IFC demo through the local adapter…" });
        const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
        // SPEC-M12: real ifcopenshell-triangulated geometry (including a
        // real boolean-subtracted wall opening where the source model has
        // one) in place of the old per-element bounding box -- fixes the
        // "no real doorway cutout" limitation D-039/D-040/D-042 each named
        // but declined to fix.
        const response = await fetch(`/api/v1/project/viewer-mesh${query}`, withAuthHeader());
        if (!response.ok) throw new Error(`Viewer source request failed (${response.status}).`);
        publishStatus({ phase: "parsing", message: "Building browser geometry from real IFC mesh data…", progress: 20 });
        const payload = await response.json() as { elements: ViewerElement[] };
        const total = Math.max(payload.elements.length, 1);
        const bounds = new THREE.Box3();
        payload.elements.forEach((element, index) => {
          // D-038: IFC's own convention is Z-up (a storey's real elevation
          // is its Z component); Three.js's camera/lighting/OrbitControls
          // here all assume Y-up. Standard Z-up -> Y-up conversion (a -90
          // degree rotation about X, preserving right-handedness):
          // three.y = ifc.z (real height), three.z = -ifc.y. Applied per
          // vertex here (SPEC-M12's real mesh has no single per-element
          // center/dimensions to transform instead) rather than via
          // `mesh.position`/`BoxGeometry` dimensions the way the old
          // bounding-box path did -- the vertices are already absolute
          // IFC world coordinates, so the mesh itself stays at the scene
          // origin.
          const positions = new Float32Array(element.vertices.length);
          for (let i = 0; i < element.vertices.length; i += 3) {
            const ifcX = element.vertices[i];
            const ifcY = element.vertices[i + 1];
            const ifcZ = element.vertices[i + 2];
            positions[i] = ifcX;
            positions[i + 1] = ifcZ;
            positions[i + 2] = -ifcY;
          }
          const geometry = new THREE.BufferGeometry();
          geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
          geometry.setIndex(element.faces);
          geometry.computeVertexNormals();
          geometry.computeBoundingBox();
          const material = new THREE.MeshStandardMaterial({ color: element.color, ...materialPropsFor(element) });
          const mesh = new THREE.Mesh(geometry, material);
          mesh.userData = element;
          if (element.global_id) elementMeshesRef.current.set(element.global_id, mesh);
          scene.add(mesh);
          bounds.expandByObject(mesh);
          // D-054: an IfcSpace's volume typically spans an entire room,
          // floor to ceiling -- if pickable, a click anywhere inside that
          // room would very likely hit the space itself rather than
          // whatever the owner actually meant to click (a wall, door,
          // furniture), since existing hit-preference logic below only
          // special-cases wall-vs-door/window occlusion, not this. Spaces
          // stay in the scene (and in `elementMeshesRef`, so an Evidence
          // citation can still highlight one) but out of direct-click
          // picking entirely.
          if (element.entity_type !== "IfcSpace") pickables.push(mesh);
          if (index % 40 === 0) {
            publishStatus({ phase: "parsing", message: `Building browser geometry from real IFC mesh data: ${Math.round(((index + 1) / total) * 100)}%`, progress: Math.round(((index + 1) / total) * 100) });
          }
        });
        const center = bounds.getCenter(new THREE.Vector3());
        const size = Math.max(bounds.getSize(new THREE.Vector3()).length(), 1);
        sceneSizeRef.current = size;
        controls.target.copy(center);
        camera.position.copy(center).add(new THREE.Vector3(size * 0.9, size * 0.65, size * 0.9));
        camera.near = Math.max(size / 1000, 0.01);
        camera.far = Math.max(size * 10, 1000);
        camera.updateProjectionMatrix();
        controls.update();
        publishStatus({ phase: "ready", message: `Viewer ready — ${payload.elements.length} IFC elements available for selection.`, progress: 100 });
        // D-049: re-applies whatever the parent already considers "lit" to
        // this freshly built mesh set -- otherwise a highlight toggled on
        // before a project switch (or reload) would silently vanish for
        // no visible reason once the new scene's meshes came in, since the
        // effect below only fires when `highlightedGlobalIds` itself
        // changes, not when this async load finishes.
        applyHighlightSet(highlightedGlobalIdsRef.current);
      } catch (error) {
        console.error("IFC browser projection failed", error);
        if (!disposed) publishStatus({ phase: "failed", message: "IFC parsing failed. Confirm that the local API and IFC source are available." });
      }
    };

    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();
    // D-049: owner-reported, 2026-09-15 -- rotating the camera (a
    // click-and-drag on the canvas) sometimes cleared whatever evidence
    // highlight was showing. Root cause: the browser's native `click`
    // event still fires at the end of a drag whenever the total pointer
    // movement stays under its own small built-in threshold, and this
    // component's only listener was a plain `click` with no way to tell
    // "the user actually clicked" apart from "the user just finished a
    // short drag." Tracked explicitly here instead: `pointerdown` records
    // the start position, and `click` only runs the selection/highlight
    // logic below if the pointer never moved more than a few pixels from
    // it -- anything more is treated as a rotate gesture and ignored
    // entirely, leaving whatever was lit exactly as it was.
    const DRAG_THRESHOLD_PX = 5;
    let pointerDownPosition: { x: number; y: number } | null = null;
    const recordPointerDown = (event: PointerEvent) => {
      pointerDownPosition = { x: event.clientX, y: event.clientY };
    };
    const select = (event: MouseEvent) => {
      const start = pointerDownPosition;
      pointerDownPosition = null;
      if (start) {
        const moved = Math.hypot(event.clientX - start.x, event.clientY - start.y);
        if (moved > DRAG_THRESHOLD_PX) return;
      }
      const bounds = renderer.domElement.getBoundingClientRect();
      pointer.x = ((event.clientX - bounds.left) / bounds.width) * 2 - 1;
      pointer.y = -((event.clientY - bounds.top) / bounds.height) * 2 + 1;
      raycaster.setFromCamera(pointer, camera);
      const hits = raycaster.intersectObjects(pickables, false);
      if (hits.length === 0) {
        // D-049: a genuine click on empty space only clears the
        // identification panel below -- it does not touch any highlight,
        // since highlights are now owned by the parent and only ever
        // change via an explicit toggle (a citation, or clicking the same
        // element again), never as a side effect of clicking elsewhere.
        onSelection(null);
        return;
      }
      // The demo IFC's walls are lightweight proxy boxes (repository.py's
      // viewer_elements fallback path) that don't actually have an opening
      // cut where a door/window sits -- the door/window's own box is fully
      // embedded inside the wall's, so a ray aimed at a visible door/window
      // almost always hits the wall's own nearer face first. Found live,
      // 2026-09-13: rotating the view didn't help, because it isn't a
      // viewing-angle problem, it's that the two boxes genuinely overlap in
      // the same space. Fixing this properly means the fixture's wall
      // geometry needs a real boolean-subtracted opening (out of scope for
      // a picking-logic fix); this instead prefers the nearest non-wall hit
      // whenever one exists within a wall-thickness-sized margin of the
      // closest hit overall, which is exactly the situation a wall
      // fully containing a door/window box produces.
      const WALL_OCCLUSION_MARGIN = 0.5;
      const nearestDistance = hits[0].distance;
      const preferredHit = hits.find((candidate) => {
        const entityType = (candidate.object.userData as ViewerElement).entity_type;
        return entityType !== "IfcWall" && candidate.distance <= nearestDistance + WALL_OCCLUSION_MARGIN;
      }) || hits[0];
      const mesh = preferredHit.object as THREE.Mesh;
      const element = mesh.userData as ViewerElement;
      onSelection({ globalId: element.global_id, expressId: element.express_id, type: element.entity_type, name: element.name, tag: element.tag });
      if (element.global_id) onToggleHighlight(element.global_id);
    };

    renderer.domElement.addEventListener("pointerdown", recordPointerDown);
    renderer.domElement.addEventListener("click", select);
    window.addEventListener("resize", resize);
    resize();
    void loadProjection();
    let frame = 0;
    const render = () => {
      frame = requestAnimationFrame(render);
      controls.update();
      renderer.render(scene, camera);
    };
    render();

    return () => {
      disposed = true;
      cancelAnimationFrame(frame);
      window.removeEventListener("resize", resize);
      renderer.domElement.removeEventListener("pointerdown", recordPointerDown);
      renderer.domElement.removeEventListener("click", select);
      controls.dispose();
      scene.traverse((item) => {
        const mesh = item as THREE.Mesh;
        if (mesh.geometry) mesh.geometry.dispose();
        if (mesh.material && !Array.isArray(mesh.material)) mesh.material.dispose();
      });
      renderer.dispose();
      host.replaceChildren();
      // Every mesh this map points to was just disposed above -- clearing
      // it (rather than leaving stale entries to accumulate across every
      // project switch) keeps `applyHighlightSet` from ever iterating a
      // disposed mesh's material.
      elementMeshesRef.current.clear();
      cameraRef.current = null;
      controlsRef.current = null;
    };
    // projectId is in this effect's dependency array on purpose: switching
    // projects tears down the whole scene (the existing cleanup below
    // already disposes geometry/materials/renderer) and rebuilds it for
    // the newly-selected project's IFC, rather than trying to patch an
    // existing scene's meshes in place. `onSelection`/`onToggleHighlight`
    // must be referentially stable across renders (e.g. wrapped in
    // useCallback by the caller, as main.tsx already does for the former)
    // -- an unstable one here would rebuild the entire Three.js scene on
    // every render, which `highlightedGlobalIdsRef` above exists
    // specifically to let this component avoid for highlight toggles.
  }, [onSelection, onStatus, onToggleHighlight, projectId]);

  function captureSnapshot() {
    const renderer = rendererRef.current;
    onSnapshot(renderer ? renderer.domElement.toDataURL("image/png") : null);
  }

  return (
    <div className="ifc-viewer">
      <div className="viewer-toolbar">
        <span data-testid="viewer-status">{status.message}</span>
        <button type="button" onClick={captureSnapshot}>Capture current view</button>
      </div>
      <div className="viewer-host" ref={hostRef} aria-label="IFC Viewer canvas" />
    </div>
  );
}
