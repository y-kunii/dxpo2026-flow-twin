import * as THREE from "three";
import "../src/style.css";
import { createFirstPersonNavigation } from "../src/camera/FirstPersonNavigation";
import type { FirstPersonNavigationSettings } from "../src/camera/FirstPersonNavigation";
import { mountDynamicCongestionOverlay } from "../src/dynamic/DynamicCongestionOverlay";

declare global {
  interface Window {
    __viewerHarness?: {
      restorePose(position: [number, number, number], target: [number, number, number]): void;
      setNavigationSettings(settings: Partial<FirstPersonNavigationSettings>): void;
    };
  }
}

const camera = new THREE.PerspectiveCamera(60, 1, 0.1, 100);
camera.position.set(0, 0, 5);
camera.lookAt(0, 0, 0);
const controls = {
  enabled: true,
  target: new THREE.Vector3(0, 0, 0),
  update: (): void => undefined,
};
const navigation = createFirstPersonNavigation(camera, controls, document.body, { speedMetersPerSecond: 3 });
const congestion = mountDynamicCongestionOverlay();
const position = document.querySelector<HTMLElement>("[data-testid='camera-position']");
const direction = document.querySelector<HTMLElement>("[data-testid='camera-direction']");
const forward = new THREE.Vector3();

window.__viewerHarness = {
  restorePose(position, target): void {
    camera.position.fromArray(position);
    controls.target.fromArray(target);
      camera.lookAt(controls.target);
      camera.updateMatrixWorld(true);
      navigation.syncFromCamera();
  },
  setNavigationSettings(settings): void {
    navigation.setSettings(settings);
  },
};

let previous = performance.now();
const tick = (now: number): void => {
  navigation.update(Math.min((now - previous) / 1000, 0.1));
  previous = now;
  if (position) position.textContent = camera.position.toArray().map((value) => value.toFixed(3)).join(",");
  if (direction) {
    camera.getWorldDirection(forward);
    direction.textContent = forward.toArray().map((value) => value.toFixed(3)).join(",");
  }
  requestAnimationFrame(tick);
};
requestAnimationFrame(tick);

for (const button of document.querySelectorAll<HTMLButtonElement>("button[data-count]")) {
  button.addEventListener("click", () => congestion.update(Number(button.dataset.count ?? 0)));
}
document.querySelector("[data-testid='toggle-viewer-ui']")?.addEventListener("click", () => {
  document.body.classList.toggle("viewer-ui-hidden");
});
