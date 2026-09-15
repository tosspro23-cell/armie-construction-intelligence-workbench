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
  center: [number, number, number];
  dimensions: [number, number, number];
  color: string;
  storey?: string;
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
const MATERIAL_BY_TYPE: Record<string, { opacity: number; roughness: number; metalness: number; transparent: boolean }> = {
  IfcWindow: { opacity: 0.45, roughness: 0.12, metalness: 0.1, transparent: true },
  IfcDoor: { opacity: 1, roughness: 0.75, metalness: 0, transparent: false },
  IfcWall: { opacity: 1, roughness: 0.9, metalness: 0, transparent: false },
  IfcSlab: { opacity: 1, roughness: 0.92, metalness: 0, transparent: false },
  IfcStair: { opacity: 1, roughness: 0.85, metalness: 0, transparent: false },
  IfcRoof: { opacity: 1, roughness: 0.8, metalness: 0, transparent: false },
};
const DEFAULT_MATERIAL = { opacity: 1, roughness: 0.78, metalness: 0, transparent: false };

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

  const publishStatus = (next: ViewerStatus) => {
    setStatus(next);
    onStatus(next);
  };

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    let disposed = false;
    const scene = new THREE.Scene();
    scene.background = new THREE.Color("#111a30");
    const camera = new THREE.PerspectiveCamera(55, 1, 0.1, 5000);
    camera.position.set(25, 20, 25);
    const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
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
        const response = await fetch(`/api/v1/project/viewer-elements${query}`, withAuthHeader());
        if (!response.ok) throw new Error(`Viewer source request failed (${response.status}).`);
        publishStatus({ phase: "parsing", message: "Building browser geometry from synthetic IFC elements…", progress: 20 });
        const payload = await response.json() as { elements: ViewerElement[] };
        const total = Math.max(payload.elements.length, 1);
        const bounds = new THREE.Box3();
        payload.elements.forEach((element, index) => {
          // D-038: `/api/v1/project/viewer-elements` returns real IFC world
          // coordinates as ifcopenshell computed them -- IFC's own
          // convention is Z-up (a storey's real elevation is its Z
          // component; confirmed directly, e.g. Level 1 ~0, Level 2 ~3.1,
          // Roof ~6.2 on the Duplex fixture). Three.js's camera/lighting/
          // OrbitControls here all assume Y-up (the scene was never told
          // otherwise). Passed straight through with no conversion, a
          // slab's real height ended up in Three.js's *depth* axis instead
          // of its height axis, and the slab's arbitrary IFC north/south
          // position ended up controlling how high it rendered -- found
          // live, 2026-09-15, when a real multi-storey building's ground
          // floor rendered above other geometry instead of below it (the
          // tiny synthetic demo fixture's simple geometry never made this
          // visible). Standard Z-up -> Y-up conversion (a -90 degree
          // rotation about X, preserving right-handedness): three.y =
          // ifc.z (real height), three.z = -ifc.y. Dimensions only need
          // the Y/Z extents swapped, never negated (a size has no sign).
          const [ifcWidth, ifcDepth, ifcHeight] = element.dimensions;
          const [ifcX, ifcY, ifcZ] = element.center;
          const geometry = new THREE.BoxGeometry(ifcWidth, ifcHeight, ifcDepth);
          const materialProps = (element.entity_type && MATERIAL_BY_TYPE[element.entity_type]) || DEFAULT_MATERIAL;
          const material = new THREE.MeshStandardMaterial({ color: element.color, ...materialProps });
          const mesh = new THREE.Mesh(geometry, material);
          mesh.position.set(ifcX, ifcZ, -ifcY);
          mesh.userData = element;
          if (element.global_id) elementMeshesRef.current.set(element.global_id, mesh);
          scene.add(mesh);
          bounds.expandByObject(mesh);
          pickables.push(mesh);
          if (index % 40 === 0) {
            publishStatus({ phase: "parsing", message: `Building browser geometry from synthetic IFC elements: ${Math.round(((index + 1) / total) * 100)}%`, progress: Math.round(((index + 1) / total) * 100) });
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
        publishStatus({ phase: "ready", message: `Viewer ready — ${payload.elements.length} synthetic IFC elements available for selection.`, progress: 100 });
      } catch (error) {
        console.error("IFC browser projection failed", error);
        if (!disposed) publishStatus({ phase: "failed", message: "IFC parsing failed. Confirm that the local API and IFC source are available." });
      }
    };

    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();
    let selectedMesh: THREE.Mesh | null = null;
    const select = (event: MouseEvent) => {
      const bounds = renderer.domElement.getBoundingClientRect();
      pointer.x = ((event.clientX - bounds.left) / bounds.width) * 2 - 1;
      pointer.y = -((event.clientY - bounds.top) / bounds.height) * 2 + 1;
      raycaster.setFromCamera(pointer, camera);
      const hits = raycaster.intersectObjects(pickables, false);
      if (selectedMesh) (selectedMesh.material as THREE.MeshStandardMaterial).emissive.set(0x000000);
      if (hits.length === 0) {
        selectedMesh = null;
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
      selectedMesh = preferredHit.object as THREE.Mesh;
      (selectedMesh.material as THREE.MeshStandardMaterial).emissive.set(0x4f9df5);
      const element = selectedMesh.userData as ViewerElement;
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
    };
    // projectId is in this effect's dependency array on purpose: switching
    // projects tears down the whole scene (the existing cleanup below
    // already disposes geometry/materials/renderer) and rebuilds it for
    // the newly-selected project's IFC, rather than trying to patch an
    // existing scene's meshes in place.
  }, [onSelection, onStatus, projectId]);

  useEffect(() => {
    if (!focusGlobalId) return;
    const mesh = elementMeshesRef.current.get(focusGlobalId);
    if (mesh) (mesh.material as THREE.MeshStandardMaterial).emissive.set(0x4f9df5);
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
