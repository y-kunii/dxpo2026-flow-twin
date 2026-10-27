import * as THREE from "three";

export interface WASDNavigationControls {
  enabled: boolean;
  target: THREE.Vector3;
  update(): void;
}

export interface WASDNavigationHandle {
  update(deltaSeconds: number): void;
  dispose(): void;
}

export interface WASDNavigationOptions {
  eventTarget?: Window;
  documentTarget?: Document;
  speedMetersPerSecond?: number;
}

const MOVEMENT_CODES = new Set(["KeyW", "KeyA", "KeyS", "KeyD", "KeyQ", "KeyE"]);
const WORLD_UP = new THREE.Vector3(0, 1, 0);

function isEditableKeyboardTarget(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && (
    target.isContentEditable ||
    Boolean(target.closest("input, textarea, select, button, [contenteditable='true']"))
  );
}

export function createWASDNavigation(
  camera: THREE.Camera,
  controls: WASDNavigationControls,
  options: WASDNavigationOptions = {},
): WASDNavigationHandle {
  const eventTarget = options.eventTarget ?? window;
  const documentTarget = options.documentTarget ?? document;
  const speed = options.speedMetersPerSecond ?? 1.5;
  const pressedKeys = new Set<string>();
  const forward = new THREE.Vector3();
  const right = new THREE.Vector3();
  const translation = new THREE.Vector3();

  const onKeyDown = (event: KeyboardEvent): void => {
    if (!MOVEMENT_CODES.has(event.code) || event.ctrlKey || event.metaKey || event.altKey ||
        isEditableKeyboardTarget(event.target)) {
      return;
    }
    pressedKeys.add(event.code);
    event.preventDefault();
  };
  const onKeyUp = (event: KeyboardEvent): void => {
    pressedKeys.delete(event.code);
  };
  const clearPressedKeys = (): void => pressedKeys.clear();
  const onVisibilityChange = (): void => {
    if (documentTarget.hidden) clearPressedKeys();
  };

  eventTarget.addEventListener("keydown", onKeyDown);
  eventTarget.addEventListener("keyup", onKeyUp);
  eventTarget.addEventListener("blur", clearPressedKeys);
  documentTarget.addEventListener("visibilitychange", onVisibilityChange);

  return {
    update(deltaSeconds: number): void {
      if (!controls.enabled || pressedKeys.size === 0 || !Number.isFinite(deltaSeconds) || deltaSeconds <= 0) {
        return;
      }
      camera.getWorldDirection(forward);
      forward.y = 0;
      if (forward.lengthSq() < 1e-8) return;
      forward.normalize();
      right.crossVectors(forward, THREE.Object3D.DEFAULT_UP).normalize();
      translation.set(0, 0, 0);
      if (pressedKeys.has("KeyW")) translation.add(forward);
      if (pressedKeys.has("KeyS")) translation.sub(forward);
      if (pressedKeys.has("KeyD")) translation.add(right);
      if (pressedKeys.has("KeyA")) translation.sub(right);
      if (pressedKeys.has("KeyE")) translation.add(WORLD_UP);
      if (pressedKeys.has("KeyQ")) translation.sub(WORLD_UP);
      if (translation.lengthSq() < 1e-8) return;
      translation.normalize().multiplyScalar(speed * deltaSeconds);
      camera.position.add(translation);
      controls.target.add(translation);
      controls.update();
    },
    dispose(): void {
      eventTarget.removeEventListener("keydown", onKeyDown);
      eventTarget.removeEventListener("keyup", onKeyUp);
      eventTarget.removeEventListener("blur", clearPressedKeys);
      documentTarget.removeEventListener("visibilitychange", onVisibilityChange);
      pressedKeys.clear();
    },
  };
}
