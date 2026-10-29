import * as THREE from "three";
import { distanceM, type Vec3 } from "./DynamicTrackingContract";

export interface DynamicFixedTagDistanceOverlay {
  readonly group: THREE.Group;
  /** Updates one verified fixed-tag-centre to rendered-box-centre measurement. */
  update(objectId: string, fixedTagPosition: Vec3, boxPosition: Vec3, labelSuffix?: string): number;
  setVisible(visible: boolean): void;
  updateLabel(camera: THREE.Camera, renderer: THREE.WebGLRenderer): void;
  clear(objectId?: string): void;
  dispose(): void;
}

interface DistanceEntry {
  readonly group: THREE.Group;
  readonly geometry: THREE.BufferGeometry;
  readonly fixedTagMarker: THREE.Mesh<THREE.SphereGeometry, THREE.MeshBasicMaterial>;
  readonly boxMarker: THREE.Mesh<THREE.SphereGeometry, THREE.MeshBasicMaterial>;
  readonly label: HTMLDivElement;
  readonly midpoint: THREE.Vector3;
  hasMeasurement: boolean;
}

const DISTANCE_COLORS = [0x00e5ff, 0x8cff4f, 0xffc800, 0xb895ff];

/**
 * Presentation-only measurements. Both inputs must already be in the Metric
 * Scene render world. Entries are keyed by dynamic object ID so one box timing
 * out never removes another box's independently accepted measurement.
 */
export function createDynamicFixedTagDistanceOverlay(): DynamicFixedTagDistanceOverlay {
  const group = new THREE.Group();
  group.name = "Dynamic fixed-tag to box distance";
  group.visible = false;
  const entries = new Map<string, DistanceEntry>();
  let requestedVisible = false;

  const syncVisibility = (): void => {
    group.visible = requestedVisible && [...entries.values()].some((entry) => entry.hasMeasurement);
    for (const entry of entries.values()) {
      entry.group.visible = requestedVisible && entry.hasMeasurement;
      if (!entry.group.visible) entry.label.style.display = "none";
    }
  };

  const createEntry = (objectId: string): DistanceEntry => {
    const positions = new Float32Array(6);
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    const color = DISTANCE_COLORS[entries.size % DISTANCE_COLORS.length];
    const entryGroup = new THREE.Group();
    entryGroup.name = `Distance: ${objectId}`;
    const line = new THREE.Line(geometry, new THREE.LineBasicMaterial({
      color,
      transparent: true,
      opacity: 0.95,
      depthTest: false,
      depthWrite: false,
    }));
    line.renderOrder = 120;
    const markerGeometry = new THREE.SphereGeometry(0.016, 16, 10);
    const fixedTagMarker = new THREE.Mesh(markerGeometry, new THREE.MeshBasicMaterial({ color: 0xff2bd6, depthTest: false }));
    const boxMarker = new THREE.Mesh(markerGeometry, new THREE.MeshBasicMaterial({ color, depthTest: false }));
    fixedTagMarker.renderOrder = 121;
    boxMarker.renderOrder = 121;
    entryGroup.add(line, fixedTagMarker, boxMarker);
    group.add(entryGroup);
    const label = document.createElement("div");
    label.className = "distance-label dynamic-fixed-tag-distance";
    label.style.display = "none";
    document.body.appendChild(label);
    const entry: DistanceEntry = {
      group: entryGroup,
      geometry,
      fixedTagMarker,
      boxMarker,
      label,
      midpoint: new THREE.Vector3(),
      hasMeasurement: false,
    };
    entries.set(objectId, entry);
    return entry;
  };

  const entryFor = (objectId: string): DistanceEntry => entries.get(objectId) ?? createEntry(objectId);

  return {
    group,
    update(objectId, fixedTagPosition, boxPosition, labelSuffix = ""): number {
      const entry = entryFor(objectId);
      const distance = distanceM(fixedTagPosition, boxPosition);
      entry.fixedTagMarker.position.set(...fixedTagPosition);
      entry.boxMarker.position.set(...boxPosition);
      const positions = entry.geometry.getAttribute("position") as THREE.BufferAttribute;
      positions.setXYZ(0, ...fixedTagPosition);
      positions.setXYZ(1, ...boxPosition);
      positions.needsUpdate = true;
      entry.geometry.computeBoundingSphere();
      entry.midpoint.set(
        (fixedTagPosition[0] + boxPosition[0]) * 0.5,
        (fixedTagPosition[1] + boxPosition[1]) * 0.5,
        (fixedTagPosition[2] + boxPosition[2]) * 0.5,
      );
      entry.label.textContent = `${objectId}: 固定タグ中心 ↔ 箱中心 ${distance.toFixed(3)} m${labelSuffix}`;
      entry.hasMeasurement = true;
      syncVisibility();
      return distance;
    },
    setVisible(visible): void {
      requestedVisible = visible;
      syncVisibility();
    },
    updateLabel(camera, renderer): void {
      if (!group.visible) return;
      const rect = renderer.domElement.getBoundingClientRect();
      for (const entry of entries.values()) {
        if (!entry.group.visible) continue;
        const projected = entry.midpoint.clone().project(camera);
        const visible = projected.z >= -1 && projected.z <= 1;
        entry.label.style.display = visible ? "block" : "none";
        if (visible) {
          entry.label.style.left = `${rect.left + (projected.x * 0.5 + 0.5) * rect.width}px`;
          entry.label.style.top = `${rect.top + (-projected.y * 0.5 + 0.5) * rect.height}px`;
        }
      }
    },
    clear(objectId): void {
      if (objectId === undefined) {
        for (const entry of entries.values()) entry.hasMeasurement = false;
      } else {
        const entry = entries.get(objectId);
        if (entry) entry.hasMeasurement = false;
      }
      syncVisibility();
    },
    dispose(): void {
      for (const entry of entries.values()) {
        entry.label.remove();
        entry.geometry.dispose();
        entry.fixedTagMarker.geometry.dispose();
        entry.fixedTagMarker.material.dispose();
        entry.boxMarker.material.dispose();
      }
      entries.clear();
      group.clear();
    },
  };
}
