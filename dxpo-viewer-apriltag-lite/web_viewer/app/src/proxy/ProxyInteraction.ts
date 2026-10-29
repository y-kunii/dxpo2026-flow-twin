import * as THREE from "three";

interface ScreenLabel {
  element: HTMLDivElement;
  position: THREE.Vector3;
}

export interface ProxyInteractionLayer {
  group: THREE.Group;
  setVisible: (visible: boolean) => void;
  clearMeasurement: () => void;
  updateLabels: (camera: THREE.Camera, renderer: THREE.WebGLRenderer) => void;
  handleClick: (event: MouseEvent, camera: THREE.Camera, renderer: THREE.WebGLRenderer) => void;
}

function createLabel(text: string): HTMLDivElement {
  const label = document.createElement("div");
  label.className = "distance-label proxy-measurement";
  label.textContent = text;
  document.body.appendChild(label);
  return label;
}

export function createProxyInteractionLayer(
  proxy: THREE.Object3D,
  onSurfacePick?: (point: THREE.Vector3, normal: THREE.Vector3) => void
): ProxyInteractionLayer {
  const group = new THREE.Group();
  group.name = "Proxy measurement layer";
  const raycaster = new THREE.Raycaster();
  const points: THREE.Vector3[] = [];
  const labels: ScreenLabel[] = [];
  let selectedLine: THREE.Line | undefined;
  const measurementLabel = createLabel("Click two proxy surfaces to measure");
  let measurementActive = false;
  measurementLabel.style.display = "none";
  labels.push({ element: measurementLabel, position: new THREE.Vector3(0, 0, 0) });

  function clearLine(): void {
    if (!selectedLine) return;
    group.remove(selectedLine);
    selectedLine.geometry.dispose();
    (selectedLine.material as THREE.Material).dispose();
    selectedLine = undefined;
  }

  return {
    group,
    setVisible(visible) {
      proxy.visible = visible;
      group.visible = visible;
      if (!visible) {
        measurementLabel.style.display = "none";
      }
    },
    clearMeasurement() {
      points.length = 0;
      clearLine();
      measurementActive = false;
      measurementLabel.textContent = "Click two proxy surfaces to measure";
      measurementLabel.style.display = "none";
    },
    updateLabels(camera, renderer) {
      const rect = renderer.domElement.getBoundingClientRect();
      for (const label of labels) {
        const projected = label.position.clone().project(camera);
        const visible = group.visible && projected.z >= -1 && projected.z <= 1;
        label.element.style.display = visible && (label !== labels[0] || measurementActive) ? "block" : "none";
        label.element.style.left = `${rect.left + (projected.x * 0.5 + 0.5) * rect.width}px`;
        label.element.style.top = `${rect.top + (-projected.y * 0.5 + 0.5) * rect.height}px`;
      }
    },
    handleClick(event, camera, renderer) {
      if (!group.visible) return;
      const rect = renderer.domElement.getBoundingClientRect();
      const pointer = new THREE.Vector2(
        ((event.clientX - rect.left) / rect.width) * 2 - 1,
        -((event.clientY - rect.top) / rect.height) * 2 + 1
      );
      raycaster.setFromCamera(pointer, camera);
      const hit = raycaster.intersectObject(proxy, true)[0];
      if (!hit) return;
      const normal = hit.face?.normal.clone() ?? new THREE.Vector3(0, 1, 0);
      if (hit.object instanceof THREE.Mesh) {
        normal.transformDirection(hit.object.matrixWorld);
      }
      onSurfacePick?.(hit.point.clone(), normal.normalize());

      if (points.length === 2) points.length = 0;
      points.push(hit.point.clone());
      clearLine();
      measurementActive = true;
      if (points.length === 2) {
        selectedLine = new THREE.Line(
          new THREE.BufferGeometry().setFromPoints(points),
          new THREE.LineBasicMaterial({ color: 0x00ff66 })
        );
        selectedLine.renderOrder = 102;
        group.add(selectedLine);
        const distance = points[0].distanceTo(points[1]);
        measurementLabel.textContent = `proxy distance: ${distance.toFixed(6)} m`;
        measurementLabel.style.display = "block";
        labels[0].position.copy(points[0]).add(points[1]).multiplyScalar(0.5);
      } else {
        measurementLabel.textContent = "Proxy point selected; choose another surface";
        labels[0].position.copy(points[0]);
      }
    }
  };
}
