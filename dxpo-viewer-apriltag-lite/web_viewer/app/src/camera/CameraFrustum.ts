import * as THREE from "three";
import type { CameraPreset } from "../scene/CameraPresets";

export interface CameraFrustumObject {
  id: string;
  group: THREE.Group;
  setActive: (active: boolean) => void;
}

const WORLD_UP = new THREE.Vector3(0, 1, 0);

function tupleToVector3(value: [number, number, number]): THREE.Vector3 {
  return new THREE.Vector3(value[0], value[1], value[2]);
}

export function createCameraFrustumObject(
  preset: CameraPreset,
  farDistance = preset.far_m ?? 1.5
): CameraFrustumObject {
  const position = tupleToVector3(preset.position_m);
  const target = tupleToVector3(preset.look_at_m);

  const forward = target.clone().sub(position).normalize();

  let right = new THREE.Vector3().crossVectors(forward, WORLD_UP).normalize();

  if (right.lengthSq() < 0.0001) {
    right = new THREE.Vector3(1, 0, 0);
  }

  const up = new THREE.Vector3().crossVectors(right, forward).normalize();

  const aspect = preset.resolution[0] / preset.resolution[1];
  const verticalFovRad = THREE.MathUtils.degToRad(preset.vertical_fov_deg);
  const halfHeight = Math.tan(verticalFovRad / 2) * farDistance;
  const halfWidth = halfHeight * aspect;

  const farCenter = position.clone().add(forward.clone().multiplyScalar(farDistance));

  const topLeft = farCenter.clone().add(up.clone().multiplyScalar(halfHeight)).sub(right.clone().multiplyScalar(halfWidth));
  const topRight = farCenter.clone().add(up.clone().multiplyScalar(halfHeight)).add(right.clone().multiplyScalar(halfWidth));
  const bottomRight = farCenter.clone().sub(up.clone().multiplyScalar(halfHeight)).add(right.clone().multiplyScalar(halfWidth));
  const bottomLeft = farCenter.clone().sub(up.clone().multiplyScalar(halfHeight)).sub(right.clone().multiplyScalar(halfWidth));

  const frustumPoints = [
    position, topLeft,
    position, topRight,
    position, bottomRight,
    position, bottomLeft,

    topLeft, topRight,
    topRight, bottomRight,
    bottomRight, bottomLeft,
    bottomLeft, topLeft,

    position, target
  ];

  const lineGeometry = new THREE.BufferGeometry().setFromPoints(frustumPoints);
  const lineMaterial = new THREE.LineBasicMaterial({
    color: 0x00ccff,
    transparent: true,
    opacity: 0.65
  });

  const frustumLines = new THREE.LineSegments(lineGeometry, lineMaterial);

  const markerGeometry = new THREE.SphereGeometry(0.06, 16, 12);
  const markerMaterial = new THREE.MeshBasicMaterial({
    color: 0x00ccff
  });

  const marker = new THREE.Mesh(markerGeometry, markerMaterial);
  marker.position.copy(position);

  const group = new THREE.Group();
  group.name = `camera-frustum-${preset.id}`;
  group.add(frustumLines);
  group.add(marker);

  function setActive(active: boolean): void {
    lineMaterial.color.set(active ? 0xffaa00 : 0x00ccff);
    lineMaterial.opacity = active ? 1.0 : 0.55;

    markerMaterial.color.set(active ? 0xffaa00 : 0x00ccff);
    marker.scale.setScalar(active ? 1.6 : 1.0);
  }

  return {
    id: preset.id,
    group,
    setActive
  };
}
