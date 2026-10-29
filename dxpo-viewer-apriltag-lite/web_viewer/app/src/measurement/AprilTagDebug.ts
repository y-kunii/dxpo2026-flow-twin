import * as THREE from "three";
import { aprilTagCornerSets, type AprilTagWorldCorners } from "../scene/SceneManifest";

interface DistanceLabel {
  element: HTMLDivElement;
  position: THREE.Vector3;
}

interface DebugCorner {
  key: string;
  label: string;
  point: THREE.Vector3;
  mesh: THREE.Mesh;
}

export interface AprilTagDebugLayer {
  group: THREE.Group;
  setVisible: (visible: boolean) => void;
  updateLabels: (camera: THREE.Camera, renderer: THREE.WebGLRenderer) => void;
  handleClick: (event: MouseEvent, camera: THREE.Camera, renderer: THREE.WebGLRenderer) => void;
}

const CORNER_RADIUS_M = 0.0025;
const SNAP_RADIUS_PX = 22;

function lineBetween(a: THREE.Vector3, b: THREE.Vector3, color: number, dashed = false): THREE.Line {
  const geometry = new THREE.BufferGeometry().setFromPoints([a, b]);
  const material = dashed
    ? new THREE.LineDashedMaterial({ color, dashSize: 0.012, gapSize: 0.008 })
    : new THREE.LineBasicMaterial({ color });
  const line = new THREE.Line(geometry, material);
  if (dashed) line.computeLineDistances();
  return line;
}

function createLabel(text: string, className: string): HTMLDivElement {
  const label = document.createElement("div");
  label.className = `distance-label ${className}`;
  label.textContent = text;
  document.body.appendChild(label);
  return label;
}

/** Display and measure every verified fixed tag carried by a v1.2 package. */
export function createAprilTagDebugLayer(data: AprilTagWorldCorners): AprilTagDebugLayer {
  const group = new THREE.Group();
  const tagSets = aprilTagCornerSets(data);
  group.name = `AprilTag fixed anchors (${tagSets.map((tag) => `${tag.family}:${tag.id}`).join(", ")})`;
  const corners: DebugCorner[] = [];
  const labels: DistanceLabel[] = [];

  for (const tag of tagSets) {
    const tagLabel = `${tag.family}:${tag.id}`;
    const points = [...tag.corners_world_m]
      .sort((a, b) => a.corner_index - b.corner_index)
      .map((corner) => new THREE.Vector3(...corner.xyz_world_m));
    if (points.length !== 4) continue;
    const center = points.reduce((sum, point) => sum.add(point), new THREE.Vector3()).multiplyScalar(0.25);
    const tagRight = points[1].clone().sub(points[0]).normalize();

    for (const [index, point] of points.entries()) {
      const mesh = new THREE.Mesh(
        new THREE.SphereGeometry(CORNER_RADIUS_M, 20, 12),
        new THREE.MeshBasicMaterial({ color: 0xff2bd6, depthTest: false }),
      );
      mesh.position.copy(point);
      mesh.renderOrder = 100;
      const key = `${tag.family}/${tag.id}/${index}`;
      corners.push({ key, label: `${tagLabel} corner ${index}`, point, mesh });
      group.add(mesh);
    }

    for (const item of tag.sides) {
      const [a, b] = item.corners;
      group.add(lineBetween(points[a], points[b], 0x00ffff));
      const midpoint = points[a].clone().add(points[b]).multiplyScalar(0.5);
      const outward = midpoint.clone().sub(center).normalize().multiplyScalar(0.014);
      labels.push({
        element: createLabel(`${tagLabel}: ${item.distance_m.toFixed(6)} m`, "side-distance"),
        position: midpoint.add(outward),
      });
    }
    for (const [index, item] of tag.diagonals.entries()) {
      const [a, b] = item.corners;
      group.add(lineBetween(points[a], points[b], 0xffcc00, true));
      labels.push({
        element: createLabel(`${tagLabel}: ${item.distance_m.toFixed(6)} m`, "diagonal-distance"),
        position: center.clone().addScaledVector(tagRight, index === 0 ? 0.018 : -0.018),
      });
    }
  }

  const measurementLabel = createLabel("Select two AprilTag corners", "snap-measurement");
  let selected: string[] = [];
  let selectedLine: THREE.Line | undefined;
  const cornerByKey = new Map(corners.map((corner) => [corner.key, corner]));

  function updateSelection(): void {
    for (const corner of corners) {
      (corner.mesh.material as THREE.MeshBasicMaterial).color.set(
        selected.includes(corner.key) ? 0x00ff66 : 0xff2bd6,
      );
    }
    if (selectedLine) {
      group.remove(selectedLine);
      selectedLine.geometry.dispose();
      (selectedLine.material as THREE.Material).dispose();
      selectedLine = undefined;
    }
    if (selected.length === 2) {
      const [left, right] = selected.map((key) => cornerByKey.get(key)!);
      selectedLine = lineBetween(left.point, right.point, 0x00ff66);
      selectedLine.renderOrder = 101;
      group.add(selectedLine);
      measurementLabel.textContent = `${left.label} ↔ ${right.label}: ${left.point.distanceTo(right.point).toFixed(6)} m`;
    } else if (selected.length === 1) {
      measurementLabel.textContent = `${cornerByKey.get(selected[0])!.label} selected; choose another corner`;
    } else {
      measurementLabel.textContent = "Select two AprilTag corners";
    }
  }

  return {
    group,
    setVisible(visible) {
      group.visible = visible;
      for (const label of labels) label.element.style.display = visible ? "block" : "none";
      measurementLabel.style.display = visible ? "block" : "none";
    },
    updateLabels(camera, renderer) {
      if (!group.visible) {
        for (const label of labels) label.element.style.display = "none";
        measurementLabel.style.display = "none";
        return;
      }
      const rect = renderer.domElement.getBoundingClientRect();
      for (const label of labels) {
        const projected = label.position.clone().project(camera);
        const visible = projected.z >= -1 && projected.z <= 1;
        label.element.style.display = visible ? "block" : "none";
        label.element.style.left = `${rect.left + (projected.x * 0.5 + 0.5) * rect.width}px`;
        label.element.style.top = `${rect.top + (-projected.y * 0.5 + 0.5) * rect.height}px`;
      }
      measurementLabel.style.display = "block";
    },
    handleClick(event, camera, renderer) {
      const rect = renderer.domElement.getBoundingClientRect();
      let closest: DebugCorner | undefined;
      let closestDistance = SNAP_RADIUS_PX;
      for (const corner of corners) {
        const projected = corner.point.clone().project(camera);
        if (projected.z < -1 || projected.z > 1) continue;
        const x = rect.left + (projected.x * 0.5 + 0.5) * rect.width;
        const y = rect.top + (-projected.y * 0.5 + 0.5) * rect.height;
        const distance = Math.hypot(event.clientX - x, event.clientY - y);
        if (distance < closestDistance) {
          closest = corner;
          closestDistance = distance;
        }
      }
      if (!closest) return;
      if (selected.length === 2) selected = [];
      if (!selected.includes(closest.key)) selected.push(closest.key);
      updateSelection();
    },
  };
}
