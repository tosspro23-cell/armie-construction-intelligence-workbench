import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";

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

type Props = {
  onSelection: (element: SelectedElement | null) => void;
  onSnapshot: (base64: string | null) => void;
  onStatus: (status: ViewerStatus) => void;
  focusGlobalId?: string;
};

export type ViewerStatus = {
  phase: "initializing" | "loading" | "parsing" | "ready" | "failed";
  message: string;
  progress?: number;
};

export function IfcViewer({ onSelection, onSnapshot, onStatus, focusGlobalId }: Props) {
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
    host.appendChild(renderer.domElement);
    rendererRef.current = renderer;
    scene.add(new THREE.HemisphereLight(0xffffff, 0x30466e, 2.2));
    const directional = new THREE.DirectionalLight(0xffffff, 2.0);
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
        const response = await fetch("/api/v1/project/viewer-elements");
        if (!response.ok) throw new Error(`Viewer source request failed (${response.status}).`);
        publishStatus({ phase: "parsing", message: "Building browser geometry from synthetic IFC elements…", progress: 20 });
        const payload = await response.json() as { elements: ViewerElement[] };
        const total = Math.max(payload.elements.length, 1);
        const bounds = new THREE.Box3();
        payload.elements.forEach((element, index) => {
          const geometry = new THREE.BoxGeometry(...element.dimensions);
          const material = new THREE.MeshStandardMaterial({ color: element.color, transparent: true, opacity: 0.82, roughness: 0.78 });
          const mesh = new THREE.Mesh(geometry, material);
          mesh.position.set(...element.center);
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
      const hit = raycaster.intersectObjects(pickables, false)[0];
      if (selectedMesh) (selectedMesh.material as THREE.MeshStandardMaterial).emissive.set(0x000000);
      if (!hit) {
        selectedMesh = null;
        onSelection(null);
        return;
      }
      selectedMesh = hit.object as THREE.Mesh;
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
  }, [onSelection, onStatus]);

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
