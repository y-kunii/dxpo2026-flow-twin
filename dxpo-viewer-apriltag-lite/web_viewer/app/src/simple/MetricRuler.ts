import * as THREE from "three";

export interface MetricRulerDimensions {
  lengthM: number;
  widthM: number;
  thicknessM: number;
}

export interface MetricRulerLayer {
  group: THREE.Group;
  dimensions: MetricRulerDimensions;
  setDimensions: (dimensions: MetricRulerDimensions) => void;
  setPose: (position: THREE.Vector3, rotation: THREE.Euler) => void;
  setVisible: (visible: boolean) => void;
  updateLabel: (camera: THREE.Camera, renderer: THREE.WebGLRenderer) => void;
  dispose: () => void;
}

const RULER_COLOR = 0xf4c542;
const RULER_EDGE_COLOR = 0xfff1a8;
const RULER_MARK_COLOR = 0x172033;
const MAX_TICKS = 120;

function disposeObject(root: THREE.Object3D): void {
  root.traverse((child) => {
    if (child instanceof THREE.Mesh || child instanceof THREE.Line || child instanceof THREE.LineSegments) {
      child.geometry.dispose();
      const materials = Array.isArray(child.material) ? child.material : [child.material];
      for (const material of materials) material.dispose();
    }
  });
}

function validatedDimensions(value: MetricRulerDimensions): MetricRulerDimensions {
  const dimensions = {
    lengthM: Number(value.lengthM),
    widthM: Number(value.widthM),
    thicknessM: Number(value.thicknessM),
  };
  if (!Number.isFinite(dimensions.lengthM) || dimensions.lengthM < 0.02 || dimensions.lengthM > 100) {
    throw new Error("定規の長さは0.02～100 mで指定してください。");
  }
  if (!Number.isFinite(dimensions.widthM) || dimensions.widthM < 0.005 || dimensions.widthM > 10) {
    throw new Error("定規の幅は0.005～10 mで指定してください。");
  }
  if (!Number.isFinite(dimensions.thicknessM) || dimensions.thicknessM < 0.0005 || dimensions.thicknessM > 1) {
    throw new Error("定規の厚みは0.0005～1 mで指定してください。");
  }
  return dimensions;
}

function tickSpacing(lengthM: number): number {
  if (lengthM <= 1.2) return 0.01;
  if (lengthM <= 5) return 0.05;
  return 0.1;
}

/** A movable, resizable scene-space ruler independent from proxy and dynamic objects. */
export function createMetricRulerLayer(): MetricRulerLayer {
  const group = new THREE.Group();
  group.name = "Simple Viewer metric ruler object";
  const dimensions: MetricRulerDimensions = {
    lengthM: 0.5,
    widthM: 0.06,
    thicknessM: 0.004,
  };
  const label = document.createElement("div");
  label.className = "simple-ruler-label";
  document.body.appendChild(label);

  const rebuild = (): void => {
    for (const child of [...group.children]) {
      group.remove(child);
      disposeObject(child);
    }
    const { lengthM, widthM, thicknessM } = dimensions;
    label.textContent = `定規 ${lengthM.toFixed(3)} m`;

    const board = new THREE.Mesh(
      new THREE.BoxGeometry(lengthM, thicknessM, widthM),
      new THREE.MeshBasicMaterial({
        color: RULER_COLOR,
        opacity: 0.78,
        transparent: true,
        depthTest: true,
        side: THREE.DoubleSide,
      }),
    );
    board.name = "Metric ruler body";
    board.renderOrder = 40;
    group.add(board);

    const edges = new THREE.LineSegments(
      new THREE.EdgesGeometry(board.geometry),
      new THREE.LineBasicMaterial({ color: RULER_EDGE_COLOR, depthTest: true }),
    );
    edges.name = "Metric ruler outline";
    edges.renderOrder = 41;
    group.add(edges);

    const spacing = tickSpacing(lengthM);
    const naturalTicks = Math.floor(lengthM / spacing + 1e-8);
    const stride = Math.max(1, Math.ceil(naturalTicks / MAX_TICKS));
    const tickPoints: THREE.Vector3[] = [];
    const topY = thicknessM * 0.5 + Math.max(0.00003, thicknessM * 0.0125);
    for (let index = 0; index <= naturalTicks; index += stride) {
      const distance = Math.min(index * spacing, lengthM);
      const x = -lengthM * 0.5 + distance;
      const logicalIndex = Math.round(distance / spacing);
      const majorEvery = spacing <= 0.011 ? 10 : 5;
      const mediumEvery = spacing <= 0.011 ? 5 : 1;
      const fraction = logicalIndex % majorEvery === 0
        ? 0.92
        : logicalIndex % mediumEvery === 0
          ? 0.62
          : 0.38;
      const zStart = -widthM * 0.5 + Math.max(widthM * 0.05, 0.001);
      tickPoints.push(
        new THREE.Vector3(x, topY, zStart),
        new THREE.Vector3(x, topY, zStart + widthM * fraction),
      );
    }
    if (tickPoints.length > 0) {
      const ticks = new THREE.LineSegments(
        new THREE.BufferGeometry().setFromPoints(tickPoints),
        new THREE.LineBasicMaterial({ color: RULER_MARK_COLOR, depthTest: true }),
      );
      ticks.name = "Metric ruler graduations";
      ticks.renderOrder = 42;
      group.add(ticks);
    }

    const centerLine = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints([
        new THREE.Vector3(-lengthM * 0.5, topY, widthM * 0.46),
        new THREE.Vector3(lengthM * 0.5, topY, widthM * 0.46),
      ]),
      new THREE.LineBasicMaterial({ color: RULER_MARK_COLOR, depthTest: true }),
    );
    centerLine.renderOrder = 42;
    group.add(centerLine);
  };

  rebuild();
  return {
    group,
    dimensions,
    setDimensions(nextDimensions) {
      Object.assign(dimensions, validatedDimensions(nextDimensions));
      rebuild();
    },
    setPose(position, rotation) {
      group.position.copy(position);
      group.rotation.copy(rotation);
      group.updateMatrixWorld(true);
    },
    setVisible(visible) {
      group.visible = visible;
      label.hidden = !visible;
    },
    updateLabel(camera, renderer) {
      if (!group.visible) {
        label.hidden = true;
        return;
      }
      label.textContent = `定規 ${(dimensions.lengthM * Math.abs(group.scale.x)).toFixed(3)} m`;
      const projected = group.getWorldPosition(new THREE.Vector3()).project(camera);
      const inView = projected.z >= -1 && projected.z <= 1;
      label.hidden = !inView;
      if (!inView) return;
      const rect = renderer.domElement.getBoundingClientRect();
      label.style.left = `${rect.left + (projected.x * 0.5 + 0.5) * rect.width}px`;
      label.style.top = `${rect.top + (-projected.y * 0.5 + 0.5) * rect.height}px`;
    },
    dispose() {
      disposeObject(group);
      group.clear();
      label.remove();
    },
  };
}
