import * as THREE from "three";
import type { CameraPreset } from "./CameraPresets";

export type SceneObjectType = "camera" | "person";
export type SurfaceType = "floor" | "wall" | "ceiling" | "free";
export type PersonTargetType = "torso" | "head";

export interface CameraObjectSettings {
  mount_surface_type: SurfaceType;
  mount_surface_normal: [number, number, number];
  surface_offset_m: number;
  vertical_fov_deg: number;
  resolution: [number, number];
  near_m?: number;
  far_m?: number;
  frustum_visible: boolean;
  free_placement?: boolean;
}

export interface PersonObjectSettings {
  height_m: number;
  yaw_deg: number;
  front_direction: [number, number, number];
  target_type: PersonTargetType;
}

export interface SceneObject {
  id: string;
  type: SceneObjectType;
  name: string;
  enabled: boolean;
  position: [number, number, number];
  quaternion: [number, number, number, number];
  created_at: string;
  updated_at: string;
  camera: CameraObjectSettings | null;
  person: PersonObjectSettings | null;
}

export interface SceneObjectsDocument {
  schema_version: "1.0.0";
  scene_id: string;
  coordinate_space: "world";
  unit: "meter";
  up_axis: "+Y";
  objects: SceneObject[];
}

export const DEFAULT_PERSON_HEIGHT_M = 1.7;

const WORLD_UP = new THREE.Vector3(0, 1, 0);
const CAMERA_FORWARD = new THREE.Vector3(0, 0, -1);

function nowIso(): string {
  return new Date().toISOString();
}

function tuple(value: THREE.Vector3): [number, number, number] {
  return [value.x, value.y, value.z];
}

function quatTuple(value: THREE.Quaternion): [number, number, number, number] {
  return [value.x, value.y, value.z, value.w];
}

export function emptySceneObjects(sceneId: string): SceneObjectsDocument {
  return {
    schema_version: "1.0.0",
    scene_id: sceneId,
    coordinate_space: "world",
    unit: "meter",
    up_axis: "+Y",
    objects: []
  };
}

export function cameraQuaternionFromForward(forward: THREE.Vector3): THREE.Quaternion {
  const direction = forward.clone().normalize();
  if (!Number.isFinite(direction.x) || direction.lengthSq() < 1e-8) {
    return new THREE.Quaternion();
  }
  return new THREE.Quaternion().setFromUnitVectors(CAMERA_FORWARD, direction);
}

export function cameraForward(object: SceneObject): THREE.Vector3 {
  return CAMERA_FORWARD.clone().applyQuaternion(new THREE.Quaternion(...object.quaternion)).normalize();
}

export function personTargetPoint(object: SceneObject): THREE.Vector3 | null {
  if (object.type !== "person" || !object.person) return null;
  const localY = object.person.target_type === "head" ? object.person.height_m * 0.88 : object.person.height_m * 0.58;
  return new THREE.Vector3(...object.position).add(new THREE.Vector3(0, localY, 0).applyQuaternion(new THREE.Quaternion(...object.quaternion)));
}

export function sceneObjectToCameraPreset(object: SceneObject): CameraPreset | null {
  if (object.type !== "camera" || !object.camera) return null;
  const position = new THREE.Vector3(...object.position);
  const target = position.clone().add(cameraForward(object).multiplyScalar(1.0));
  return {
    id: object.id,
    name: object.name,
    position_m: object.position,
    look_at_m: tuple(target),
    up_world: [0, 1, 0],
    vertical_fov_deg: object.camera.vertical_fov_deg,
    resolution: object.camera.resolution,
    near_m: object.camera.near_m,
    far_m: object.camera.far_m
  };
}

export function createCameraObject(
  object: SceneObject,
  selected = false
): THREE.Group {
  const group = new THREE.Group();
  group.name = `scene-camera-${object.id}`;
  group.userData.sceneObjectId = object.id;
  group.position.set(...object.position);
  group.quaternion.set(...object.quaternion);
  group.visible = object.enabled;

  const color = selected ? 0xffaa00 : 0x00ccff;
  const bodyMaterial = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.9 });
  const lensMaterial = new THREE.MeshBasicMaterial({ color: 0x222222, transparent: true, opacity: 0.95 });
  const mountMaterial = new THREE.MeshBasicMaterial({ color: 0x777777 });

  const body = new THREE.Mesh(new THREE.BoxGeometry(0.18, 0.12, 0.14), bodyMaterial);
  group.add(body);
  const lens = new THREE.Mesh(new THREE.CylinderGeometry(0.045, 0.06, 0.1, 16), lensMaterial);
  lens.rotation.x = Math.PI / 2;
  lens.position.z = -0.12;
  group.add(lens);
  const mount = new THREE.Mesh(new THREE.CylinderGeometry(0.04, 0.05, 0.08, 12), mountMaterial);
  mount.position.y = -0.1;
  group.add(mount);
  const direction = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0, 0, -0.16), new THREE.Vector3(0, 0, -0.38)]),
    new THREE.LineBasicMaterial({ color: 0xffee55 })
  );
  group.add(direction);
  const selection = new THREE.Mesh(
    new THREE.SphereGeometry(0.14, 12, 8),
    new THREE.MeshBasicMaterial({ color, wireframe: true, transparent: true, opacity: selected ? 1 : 0.35 })
  );
  group.add(selection);
  return group;
}

export function createPersonObject(
  object: SceneObject,
  selected = false
): THREE.Group {
  const group = new THREE.Group();
  group.name = `scene-person-${object.id}`;
  group.userData.sceneObjectId = object.id;
  group.position.set(...object.position);
  group.quaternion.set(...object.quaternion);
  group.visible = object.enabled;
  const height = object.person?.height_m ?? DEFAULT_PERSON_HEIGHT_M;
  group.scale.setScalar(height / DEFAULT_PERSON_HEIGHT_M);

  const color = selected ? 0xffaa00 : 0xff55aa;
  const material = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.82 });
  const darkMaterial = new THREE.MeshBasicMaterial({ color: 0x332233, transparent: true, opacity: 0.9 });
  const torso = new THREE.Mesh(new THREE.BoxGeometry(0.34, 0.68, 0.22), material);
  torso.position.y = 0.98;
  group.add(torso);
  const head = new THREE.Mesh(new THREE.SphereGeometry(0.15, 16, 12), material);
  head.position.y = 1.48;
  group.add(head);
  for (const x of [-0.09, 0.09]) {
    const leg = new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.68, 0.12), darkMaterial);
    leg.position.set(x, 0.34, 0);
    group.add(leg);
  }
  const front = new THREE.Mesh(new THREE.ConeGeometry(0.06, 0.16, 12), new THREE.MeshBasicMaterial({ color: 0xffff00 }));
  front.rotation.x = -Math.PI / 2;
  front.position.set(0, 1.02, -0.2);
  group.add(front);
  const target = new THREE.Mesh(
    new THREE.SphereGeometry(0.045, 10, 8),
    new THREE.MeshBasicMaterial({ color: 0x00ff66 })
  );
  target.position.y = object.person?.target_type === "head" ? 1.496 : 0.986;
  group.add(target);
  const selection = new THREE.Mesh(
    new THREE.CylinderGeometry(0.24, 0.24, 1.72, 16, 1, true),
    new THREE.MeshBasicMaterial({ color, wireframe: true, transparent: true, opacity: selected ? 0.9 : 0.25 })
  );
  selection.position.y = 0.86;
  group.add(selection);
  return group;
}

export function createSceneObjectVisual(object: SceneObject, selected = false): THREE.Group {
  return object.type === "camera" ? createCameraObject(object, selected) : createPersonObject(object, selected);
}

export function makeCurrentViewAnchor(
  position: THREE.Vector3,
  forward: THREE.Vector3,
  verticalFovDeg = 50
): SceneObject {
  const id = `camera_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
  const created = nowIso();
  return {
    id,
    type: "camera",
    name: `View ${id.slice(-4)}`,
    enabled: true,
    position: tuple(position),
    quaternion: quatTuple(cameraQuaternionFromForward(forward)),
    created_at: created,
    updated_at: created,
    camera: {
      mount_surface_type: "free",
      mount_surface_normal: [0, 1, 0],
      surface_offset_m: 0.01,
      vertical_fov_deg: Number.isFinite(verticalFovDeg) && verticalFovDeg > 1 && verticalFovDeg < 179 ? verticalFovDeg : 50,
      resolution: [1600, 1000],
      near_m: 0.01,
      far_m: 1.5,
      frustum_visible: true,
      free_placement: true
    },
    person: null
  };
}

export function makePersonObject(position: THREE.Vector3): SceneObject {
  const id = `person_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
  const created = nowIso();
  return {
    id,
    type: "person",
    name: `Person ${id.slice(-4)}`,
    enabled: true,
    position: tuple(position),
    quaternion: [0, 0, 0, 1],
    created_at: created,
    updated_at: created,
    camera: null,
    person: {
      height_m: DEFAULT_PERSON_HEIGHT_M,
      yaw_deg: 0,
      front_direction: [0, 0, -1],
      target_type: "torso"
    }
  };
}

export function cloneSceneObject(object: SceneObject): SceneObject {
  const clone = structuredClone(object) as SceneObject;
  clone.id = `${object.type}_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
  clone.name = `${object.name} copy`;
  clone.created_at = nowIso();
  clone.updated_at = clone.created_at;
  return clone;
}

export function setPersonYaw(object: SceneObject, yawDeg: number): void {
  if (!object.person) return;
  object.person.yaw_deg = yawDeg;
  const radians = THREE.MathUtils.degToRad(yawDeg);
  const q = new THREE.Quaternion().setFromAxisAngle(WORLD_UP, radians);
  object.quaternion = quatTuple(q);
  object.person.front_direction = tuple(new THREE.Vector3(0, 0, -1).applyQuaternion(q));
}

export function setCameraForward(object: SceneObject, forward: THREE.Vector3): void {
  object.quaternion = quatTuple(cameraQuaternionFromForward(forward));
}

export function pickSceneObject(
  root: THREE.Group,
  event: MouseEvent,
  camera: THREE.Camera,
  renderer: THREE.WebGLRenderer
): string | null {
  const rect = renderer.domElement.getBoundingClientRect();
  const pointer = new THREE.Vector2(
    ((event.clientX - rect.left) / rect.width) * 2 - 1,
    -((event.clientY - rect.top) / rect.height) * 2 + 1
  );
  const raycaster = new THREE.Raycaster();
  raycaster.setFromCamera(pointer, camera);
  const hit = raycaster.intersectObject(root, true)[0];
  return typeof hit?.object.parent?.userData.sceneObjectId === "string"
    ? hit.object.parent.userData.sceneObjectId
    : typeof hit?.object.userData.sceneObjectId === "string"
      ? hit.object.userData.sceneObjectId
      : null;
}
