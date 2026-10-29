import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import {
  boxGeometryDimensions,
  createRuntimeDynamicBoxConfig,
  isRuntimeDynamicBoxId,
  normalizeBoxDimensions,
  type BoxDimensionsM,
  type DynamicObjectConfig,
  type DynamicObjectRegistry,
  type ResolvedDynamicPose,
} from "./DynamicTrackingContract";

export interface DynamicObjectLayer {
  readonly group: THREE.Group;
  readonly objectIds: readonly string[];
  setPose(pose: ResolvedDynamicPose): boolean;
  setRuntimeCubeDimensions(objectId: string, dimensions: BoxDimensionsM): boolean;
  hide(objectId: string): boolean;
  hideAll(): void;
  dispose(): void;
}

function readGlbHeader(bytes: Uint8Array): boolean {
  return bytes.length >= 4 && bytes[0] === 0x67 && bytes[1] === 0x6c && bytes[2] === 0x54 && bytes[3] === 0x46;
}

async function sha256Hex(bytes: Uint8Array): Promise<string> {
  if (!globalThis.crypto?.subtle) {
    throw new Error("Web Crypto SHA-256 is unavailable");
  }
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes as unknown as BufferSource);
  return Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("");
}

function parseGlb(loader: GLTFLoader, bytes: ArrayBuffer, basePath: string): Promise<THREE.Group> {
  return new Promise((resolve, reject) => {
    loader.parse(bytes, basePath, (gltf) => resolve(gltf.scene), reject);
  });
}

function createCubeRoot(dimensions: BoxDimensionsM, colorHex = "#d58a46"): THREE.Group {
  const group = new THREE.Group();
  const color = /^#[0-9a-fA-F]{6}$/.test(colorHex) ? colorHex : "#d58a46";
  const [width, height, depth] = boxGeometryDimensions(dimensions);
  const geometry = new THREE.BoxGeometry(width, height, depth);
  // Dynamic overlays cannot rely on Scene lighting: the splat renderer does
  // not add Three.js lights. Render a clearly coloured surface above the
  // splat depth buffer; a wireframe-only box is too easy to lose in dense
  // Gaussian splats.
  const mesh = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({
    color,
    transparent: true,
    opacity: 0.84,
    depthTest: false,
    depthWrite: false,
  }));
  const outline = new THREE.LineSegments(
    new THREE.EdgesGeometry(geometry),
    new THREE.LineBasicMaterial({ color: "#fff3b0", transparent: true, opacity: 0.92, depthTest: false }),
  );
  mesh.castShadow = false;
  mesh.receiveShadow = false;
  mesh.renderOrder = 10;
  outline.renderOrder = 11;
  group.add(mesh);
  group.add(outline);
  return group;
}

function disposeObject(root: THREE.Object3D): void {
  root.traverse((child) => {
    if (!(child instanceof THREE.Mesh) && !(child instanceof THREE.LineSegments)) return;
    child.geometry.dispose();
    const materials = Array.isArray(child.material) ? child.material : [child.material];
    for (const material of materials) {
      for (const value of Object.values(material)) {
        if (value instanceof THREE.Texture) value.dispose();
      }
      material.dispose();
    }
  });
}

/** Load and hash-check all package-provided dynamic GLBs before exposing any of them.
 *
 * The registered overrides are intentionally all-or-nothing: a missing or
 * mismatched model disables dynamic tracking while the static SPZ/Proxy scene
 * keeps loading.  Generic runtime cubes do not need package assets.
 */
export async function createDynamicObjectLayer(
  baseUrl: string,
  registry: DynamicObjectRegistry,
): Promise<DynamicObjectLayer> {
  const group = new THREE.Group();
  group.name = "Dynamic tracked objects";
  const roots = new Map<string, THREE.Object3D>();
  const packageObjectIds = new Set(registry.objects.map((object) => object.id));
  const runtimeDimensions = new Map<string, BoxDimensionsM>();
  const loader = new GLTFLoader();

  const addCube = (config: DynamicObjectConfig): THREE.Group => {
    const fallback = createRuntimeDynamicBoxConfig(config.id).model;
    if (fallback.format !== "cube") throw new Error("Runtime dynamic fallback must be a cube.");
    const cube = createCubeRoot(
      config.model.format === "cube" ? config.model.dimensions_m : fallback.dimensions_m,
      config.model.format === "cube" ? config.model.color_hex : undefined,
    );
    cube.name = `Dynamic object ${config.id}`;
    cube.visible = false;
    roots.set(config.id, cube);
    group.add(cube);
    return cube;
  };

  try {
    for (const config of registry.objects) {
      let root: THREE.Group;
      if (config.model.format === "cube") {
        addCube(config);
        continue;
      } else {
        const assetUrl = new URL(config.model.uri, window.location.origin + baseUrl);
        const packageBase = new URL(".", window.location.origin + baseUrl).pathname;
        if (assetUrl.origin !== window.location.origin || !assetUrl.pathname.startsWith(packageBase)) {
          throw new Error(`Dynamic model must be local to the Scene Package: ${config.id}`);
        }
        const response = await fetch(assetUrl.pathname);
        if (!response.ok) throw new Error(`Dynamic model fetch failed (${response.status}): ${config.id}`);
        const bytes = new Uint8Array(await response.arrayBuffer());
        if (!readGlbHeader(bytes)) throw new Error(`Dynamic model is not a GLB: ${config.id}`);
        const actualHash = await sha256Hex(bytes);
        if (actualHash !== config.model.sha256.toLowerCase()) {
          throw new Error(`Dynamic model hash mismatch: ${config.id}`);
        }
        root = await parseGlb(loader, bytes.buffer, assetUrl.pathname.slice(0, assetUrl.pathname.lastIndexOf("/") + 1));
      }
      root.name = `Dynamic object ${config.id}`;
      root.visible = false;
      roots.set(config.id, root);
      group.add(root);
    }
  } catch (error) {
    disposeObject(group);
    throw error;
  }

  return {
    group,
    get objectIds() { return [...roots.keys()]; },
    setPose: (pose) => {
      let root = roots.get(pose.object_id);
      if (!root && isRuntimeDynamicBoxId(pose.object_id)) {
        const runtimeConfig = createRuntimeDynamicBoxConfig(pose.object_id);
        const dimensions = runtimeDimensions.get(pose.object_id);
        if (dimensions && runtimeConfig.model.format === "cube") runtimeConfig.model.dimensions_m = dimensions;
        root = addCube(runtimeConfig);
      }
      if (!root) return false;
      root.position.set(...pose.translation_m);
      root.quaternion.set(...pose.rotation_xyzw);
      root.visible = true;
      return true;
    },
    setRuntimeCubeDimensions: (objectId, value) => {
      if (!isRuntimeDynamicBoxId(objectId) || packageObjectIds.has(objectId)) return false;
      const dimensions = normalizeBoxDimensions(value);
      if (!dimensions) return false;
      runtimeDimensions.set(objectId, dimensions);
      const previous = roots.get(objectId);
      if (!previous) return true;
      const replacement = createCubeRoot(dimensions);
      replacement.name = previous.name;
      replacement.position.copy(previous.position);
      replacement.quaternion.copy(previous.quaternion);
      replacement.scale.copy(previous.scale);
      replacement.visible = previous.visible;
      group.remove(previous);
      disposeObject(previous);
      roots.set(objectId, replacement);
      group.add(replacement);
      return true;
    },
    hide: (objectId) => {
      const root = roots.get(objectId);
      if (!root) return false;
      root.visible = false;
      return true;
    },
    hideAll: () => {
      for (const root of roots.values()) root.visible = false;
    },
    dispose: () => {
      disposeObject(group);
      group.clear();
      roots.clear();
    },
  };
}
