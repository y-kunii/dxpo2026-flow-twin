import * as THREE from "three";
import { createFirstPersonNavigation } from "../src/camera/FirstPersonNavigation";

const camera = new THREE.PerspectiveCamera(60, 1, 0.1, 100);
camera.position.set(0, 0, 5);
camera.lookAt(0, 0, 0);
const controls = {
  enabled: true,
  target: new THREE.Vector3(0, 0, 0),
  update: (): void => undefined,
};
const navigation = createFirstPersonNavigation(camera, controls, document.body, { speedMetersPerSecond: 3 });
const position = document.querySelector<HTMLElement>("[data-testid='camera-position']");
const direction = document.querySelector<HTMLElement>("[data-testid='camera-direction']");
const forward = new THREE.Vector3();

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
