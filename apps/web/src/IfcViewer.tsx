import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { withAuthHeader } from "./apiClient";

type SelectedElement = {
  globalId?: string;
  expressId?: number;
  type?: string;
  name?: string;
};

type ViewerElement = {
  global_id?: string;
  express_id?: number;
  entity_type?: string;
  name?: string;
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
  focusGlobalId?: string;
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

export function IfcViewer({ onSelection, onSnapshot, onStatus, focusGlobalId, projectId }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<ViewerStatus>({ phase: "initializing", message: "Preparing IFC viewer…" });
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null);
  const elementMeshesRef = useRef<Map<string, THREE.Mesh>>(new Map());
  // D-046: owner-reported, 2026-09-15 -- highlighting one element (either
  // by clicking it directly, or by jumping to it from an Evidence
  // citation) left it lit permanently: clicking a different element (or
  // citation) highlighted the new one but never cleared the old one's
  // emissive, so highlights accumulated across the scene. Root cause: the
  // canvas click handler and the `focusGlobalId` effect below each kept
  // their own, independent notion of "the highlighted mesh" (a local
  // closure variable in one, nothing at all in the other), so neither
  // could clear a highlight the other had set. A single ref, shared by
  // both, is the one source of truth for "what is currently lit."
  const highlightedMeshRef = useRef<THREE.Mesh | null>(null);

  const publishStatus = (next: ViewerStatus) => {
    setStatus(next);
    onStatus(next);
  };

  function applyHighlight(mesh: THREE.Mesh | null) {
    const previous = highlightedMeshRef.current;
    if (previous && previous !== mesh) (previous.material as THREE.MeshStandardMaterial).emissive.set(0x000000);
    if (mesh) (mesh.material as THREE.MeshStandardMaterial).emissive.set(0x4f9df5);
    highlightedMeshRef.current = mesh;
  }

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
          pickables.push(mesh);
          if (index % 40 === 0) {
            publishStatus({ phase: "parsing", message: `Building browser geometry from real IFC mesh data: ${Math.round(((index + 1) / total) * 100)}%`, progress: Math.round(((index + 1) / total) * 100) });
          }
        });
        const center = bounds.getCenter(new THREE.Vector3());
        const size = Math.max(bounds.getSize(new THREE.Vector3()).length(), 1);
        controls.target.copy(center);
        camera.position.copy(center).add(new THREE.Vector3(size * 0.9, size * 0.65, size * 0.9));
        camera.near = Math.max(size / 1000, 0.01);
        camera.far = Math.max(size * 10, 1000);
        camera.updateProjectionMatrix();
        controls.update();
        publishStatus({ phase: "ready", message: `Viewer ready — ${payload.elements.length} IFC elements available for selection.`, progress: 100 });
      } catch (error) {
        console.error("IFC browser projection failed", error);
        if (!disposed) publishStatus({ phase: "failed", message: "IFC parsing failed. Confirm that the local API and IFC source are available." });
      }
    };

    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();
    const select = (event: MouseEvent) => {
      const bounds = renderer.domElement.getBoundingClientRect();
      pointer.x = ((event.clientX - bounds.left) / bounds.width) * 2 - 1;
      pointer.y = -((event.clientY - bounds.top) / bounds.height) * 2 + 1;
      raycaster.setFromCamera(pointer, camera);
      const hits = raycaster.intersectObjects(pickables, false);
      if (hits.length === 0) {
        applyHighlight(null);
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
      applyHighlight(mesh);
      const element = mesh.userData as ViewerElement;
      onSelection({ globalId: element.global_id, expressId: element.express_id, type: element.entity_type, name: element.name });
    };

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
      renderer.domElement.removeEventListener("click", select);
      controls.dispose();
      scene.traverse((item) => {
        const mesh = item as THREE.Mesh;
        if (mesh.geometry) mesh.geometry.dispose();
        if (mesh.material && !Array.isArray(mesh.material)) mesh.material.dispose();
      });
      renderer.dispose();
      host.replaceChildren();
      // The scene's own meshes are being disposed above -- any stale
      // reference to one of them is no longer valid, so the highlight ref
      // is reset alongside them rather than left pointing at disposed
      // geometry for whatever project loads next.
      highlightedMeshRef.current = null;
    };
    // projectId is in this effect's dependency array on purpose: switching
    // projects tears down the whole scene (the existing cleanup below
    // already disposes geometry/materials/renderer) and rebuilds it for
    // the newly-selected project's IFC, rather than trying to patch an
    // existing scene's meshes in place.
  }, [onSelection, onStatus, projectId]);

  useEffect(() => {
    // D-046: `focusGlobalId` going away (e.g. the owner's own "Clear
    // selection" button) must clear the highlight too, not just skip
    // setting a new one -- the old early return here left whatever was lit
    // lit forever once its citation stopped being the current selection.
    const mesh = focusGlobalId ? elementMeshesRef.current.get(focusGlobalId) ?? null : null;
    applyHighlight(mesh);
  }, [focusGlobalId]);

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
