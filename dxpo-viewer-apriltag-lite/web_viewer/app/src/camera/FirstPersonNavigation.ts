import * as THREE from "three";

export interface FirstPersonNavigationControls {
  enabled: boolean;
  target: THREE.Vector3;
  update(): void;
}

export interface FirstPersonNavigationOptions {
  eventTarget?: Window;
  documentTarget?: Document;
  speedMetersPerSecond?: number;
  lookRadiansPerPixel?: number;
  panMetersPerPixel?: number;
  wheelMetersPerStep?: number;
  onInteractionStart?: () => void;
  onInteractionEnd?: () => void;
}

export interface FirstPersonNavigationSettings {
  /** Horizontal and vertical key movement in Viewer-world metres per second. */
  speedMetersPerSecond: number;
  /** Yaw/pitch sensitivity for a pointer movement, in radians per pixel. */
  lookRadiansPerPixel: number;
  /** Camera-plane translation for a right-button pointer movement. */
  panMetersPerPixel: number;
  /** Forward/backward movement for one wheel step, in Viewer-world metres. */
  wheelMetersPerStep: number;
}

export interface FirstPersonNavigationHandle {
  update(deltaSeconds: number): void;
  syncFromCamera(): void;
  getSettings(): Readonly<FirstPersonNavigationSettings>;
  setSettings(settings: Partial<FirstPersonNavigationSettings>): void;
  requestPointerLock(): void;
  exitPointerLock(): void;
  isPointerLocked(): boolean;
  dispose(): void;
}

const MOVEMENT_CODES = new Set([
  "KeyW",
  "KeyA",
  "KeyS",
  "KeyD",
  "KeyQ",
  "KeyE",
  "Space",
  "ShiftLeft",
  "ShiftRight",
]);
const WORLD_UP = new THREE.Vector3(0, 1, 0);
const MIN_FORWARD_LENGTH_SQ = 1e-8;
const MAX_PITCH_RADIANS = THREE.MathUtils.degToRad(89);

export const DEFAULT_FIRST_PERSON_NAVIGATION_SETTINGS: Readonly<FirstPersonNavigationSettings> = {
  speedMetersPerSecond: 1.5,
  lookRadiansPerPixel: 0.0025,
  panMetersPerPixel: 0.003,
  wheelMetersPerStep: 0.25,
};

function isInteractiveTarget(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && (
    target.isContentEditable ||
    Boolean(target.closest("input, textarea, select, button, [contenteditable='true']"))
  );
}

/**
 * First-person navigation for the full Viewer.
 *
 * The camera rotates around its own position. `controls.target` is maintained
 * only as a point one metre in front of the camera so existing Viewer features
 * that consume the target continue to work without turning navigation back
 * into an orbit controller.
 */
export function createFirstPersonNavigation(
  camera: THREE.PerspectiveCamera,
  controls: FirstPersonNavigationControls,
  element: HTMLElement,
  options: FirstPersonNavigationOptions = {},
): FirstPersonNavigationHandle {
  const eventTarget = options.eventTarget ?? window;
  const documentTarget = options.documentTarget ?? document;
  const settings: FirstPersonNavigationSettings = {
    speedMetersPerSecond: options.speedMetersPerSecond ?? DEFAULT_FIRST_PERSON_NAVIGATION_SETTINGS.speedMetersPerSecond,
    lookRadiansPerPixel: options.lookRadiansPerPixel ?? DEFAULT_FIRST_PERSON_NAVIGATION_SETTINGS.lookRadiansPerPixel,
    panMetersPerPixel: options.panMetersPerPixel ?? DEFAULT_FIRST_PERSON_NAVIGATION_SETTINGS.panMetersPerPixel,
    wheelMetersPerStep: options.wheelMetersPerStep ?? DEFAULT_FIRST_PERSON_NAVIGATION_SETTINGS.wheelMetersPerStep,
  };
  const pressedKeys = new Set<string>();
  const forward = new THREE.Vector3();
  const planarForward = new THREE.Vector3();
  const right = new THREE.Vector3();
  const screenUp = new THREE.Vector3();
  const translation = new THREE.Vector3();
  let yawRadians = 0;
  let pitchRadians = 0;
  let activePointerId: number | undefined;
  let activePointerMode: "look" | "pan" | undefined;
  let pointerLocked = false;
  let interactionActive = false;

  const syncFromCamera = (): void => {
    camera.getWorldDirection(forward);
    if (forward.lengthSq() < MIN_FORWARD_LENGTH_SQ) forward.set(0, 0, -1);
    forward.normalize();
    yawRadians = Math.atan2(forward.x, -forward.z);
    pitchRadians = Math.asin(THREE.MathUtils.clamp(forward.y, -1, 1));
    controls.target.copy(camera.position).add(forward);
  };

  const updateInteractionState = (): void => {
    const active = activePointerId !== undefined || pointerLocked || pressedKeys.size > 0;
    if (active === interactionActive) return;
    interactionActive = active;
    if (active) options.onInteractionStart?.();
    else options.onInteractionEnd?.();
  };

  const applyLookDirection = (): void => {
    forward.set(
      Math.sin(yawRadians) * Math.cos(pitchRadians),
      Math.sin(pitchRadians),
      -Math.cos(yawRadians) * Math.cos(pitchRadians),
    ).normalize();
    camera.up.copy(WORLD_UP);
    controls.target.copy(camera.position).add(forward);
    camera.lookAt(controls.target);
    camera.updateMatrixWorld(true);
  };

  const translateCamera = (offset: THREE.Vector3): void => {
    camera.position.add(offset);
    controls.target.add(offset);
    camera.updateMatrixWorld(true);
  };

  const onKeyDown = (event: KeyboardEvent): void => {
    if (!MOVEMENT_CODES.has(event.code) || event.ctrlKey || event.metaKey || event.altKey ||
        isInteractiveTarget(event.target)) {
      return;
    }
    if (!controls.enabled) return;
    pressedKeys.add(event.code);
    updateInteractionState();
    event.preventDefault();
  };

  const onKeyUp = (event: KeyboardEvent): void => {
    if (!pressedKeys.delete(event.code)) return;
    updateInteractionState();
  };

  const clearPressedKeys = (): void => {
    if (pressedKeys.size === 0) return;
    pressedKeys.clear();
    updateInteractionState();
  };

  const onVisibilityChange = (): void => {
    if (documentTarget.hidden) clearPressedKeys();
  };

  const endPointerLook = (event?: PointerEvent): void => {
    if (activePointerId === undefined) return;
    if (event && event.pointerId !== activePointerId) return;
    const pointerId = activePointerId;
    activePointerId = undefined;
    activePointerMode = undefined;
    if (element.hasPointerCapture(pointerId)) element.releasePointerCapture(pointerId);
    element.style.cursor = "";
    updateInteractionState();
  };

  const onPointerDown = (event: PointerEvent): void => {
    if (!controls.enabled || pointerLocked || (event.button !== 0 && event.button !== 2) || activePointerId !== undefined ||
        isInteractiveTarget(event.target)) {
      return;
    }
    syncFromCamera();
    activePointerId = event.pointerId;
    activePointerMode = event.button === 2 ? "pan" : "look";
    element.setPointerCapture(event.pointerId);
    element.style.cursor = "grabbing";
    updateInteractionState();
    event.preventDefault();
  };

  const onPointerMove = (event: PointerEvent): void => {
    if (!controls.enabled || (!pointerLocked && event.pointerId !== activePointerId)) return;
    if (!pointerLocked && activePointerMode === "pan") {
      camera.getWorldDirection(forward);
      if (forward.lengthSq() < MIN_FORWARD_LENGTH_SQ) return;
      forward.normalize();
      right.crossVectors(forward, WORLD_UP);
      if (right.lengthSq() < MIN_FORWARD_LENGTH_SQ) {
        right.set(1, 0, 0).applyQuaternion(camera.quaternion);
      }
      right.normalize();
      screenUp.crossVectors(right, forward).normalize();
      translation.copy(right).multiplyScalar(-event.movementX * settings.panMetersPerPixel);
      translation.addScaledVector(screenUp, event.movementY * settings.panMetersPerPixel);
      translateCamera(translation);
      event.preventDefault();
      return;
    }
    yawRadians += event.movementX * settings.lookRadiansPerPixel;
    pitchRadians = THREE.MathUtils.clamp(
      pitchRadians - event.movementY * settings.lookRadiansPerPixel,
      -MAX_PITCH_RADIANS,
      MAX_PITCH_RADIANS,
    );
    applyLookDirection();
    event.preventDefault();
  };

  const onWheel = (event: WheelEvent): void => {
    if (!controls.enabled || event.deltaY === 0) return;
    camera.getWorldDirection(forward);
    if (forward.lengthSq() < MIN_FORWARD_LENGTH_SQ) return;
    const direction = event.deltaY < 0 ? 1 : -1;
    translateCamera(forward.normalize().multiplyScalar(direction * settings.wheelMetersPerStep));
    event.preventDefault();
  };

  const onPointerLockChange = (): void => {
    pointerLocked = documentTarget.pointerLockElement === element;
    if (!pointerLocked) element.style.cursor = "";
    updateInteractionState();
  };

  eventTarget.addEventListener("keydown", onKeyDown);
  eventTarget.addEventListener("keyup", onKeyUp);
  eventTarget.addEventListener("blur", clearPressedKeys);
  documentTarget.addEventListener("visibilitychange", onVisibilityChange);
  documentTarget.addEventListener("pointerlockchange", onPointerLockChange);
  element.addEventListener("pointerdown", onPointerDown);
  element.addEventListener("pointermove", onPointerMove);
  element.addEventListener("pointerup", endPointerLook);
  element.addEventListener("pointercancel", endPointerLook);
  element.addEventListener("wheel", onWheel, { passive: false });

  syncFromCamera();

  return {
    update(deltaSeconds: number): void {
      if (!controls.enabled || pressedKeys.size === 0 || !Number.isFinite(deltaSeconds) || deltaSeconds <= 0) {
        return;
      }

      camera.getWorldDirection(forward);
      planarForward.copy(forward).setY(0);
      if (planarForward.lengthSq() < MIN_FORWARD_LENGTH_SQ) {
        planarForward.set(Math.sin(yawRadians), 0, -Math.cos(yawRadians));
      }
      planarForward.normalize();
      right.crossVectors(planarForward, WORLD_UP).normalize();
      translation.set(0, 0, 0);
      if (pressedKeys.has("KeyW")) translation.add(planarForward);
      if (pressedKeys.has("KeyS")) translation.sub(planarForward);
      if (pressedKeys.has("KeyD")) translation.add(right);
      if (pressedKeys.has("KeyA")) translation.sub(right);
      if (pressedKeys.has("KeyE") || pressedKeys.has("Space")) translation.add(WORLD_UP);
      if (pressedKeys.has("KeyQ") || pressedKeys.has("ShiftLeft") || pressedKeys.has("ShiftRight")) {
        translation.sub(WORLD_UP);
      }
      if (translation.lengthSq() < MIN_FORWARD_LENGTH_SQ) return;
      translation.normalize().multiplyScalar(settings.speedMetersPerSecond * deltaSeconds);
      translateCamera(translation);
    },
    syncFromCamera,
    getSettings(): Readonly<FirstPersonNavigationSettings> {
      return { ...settings };
    },
    setSettings(nextSettings: Partial<FirstPersonNavigationSettings>): void {
      if (typeof nextSettings.speedMetersPerSecond === "number" &&
          Number.isFinite(nextSettings.speedMetersPerSecond) && nextSettings.speedMetersPerSecond > 0) {
        settings.speedMetersPerSecond = nextSettings.speedMetersPerSecond;
      }
      if (typeof nextSettings.lookRadiansPerPixel === "number" &&
          Number.isFinite(nextSettings.lookRadiansPerPixel) && nextSettings.lookRadiansPerPixel > 0) {
        settings.lookRadiansPerPixel = nextSettings.lookRadiansPerPixel;
      }
      if (typeof nextSettings.panMetersPerPixel === "number" &&
          Number.isFinite(nextSettings.panMetersPerPixel) && nextSettings.panMetersPerPixel > 0) {
        settings.panMetersPerPixel = nextSettings.panMetersPerPixel;
      }
      if (typeof nextSettings.wheelMetersPerStep === "number" &&
          Number.isFinite(nextSettings.wheelMetersPerStep) && nextSettings.wheelMetersPerStep > 0) {
        settings.wheelMetersPerStep = nextSettings.wheelMetersPerStep;
      }
    },
    requestPointerLock(): void {
      if (documentTarget.pointerLockElement === element) return;
      void element.requestPointerLock?.();
    },
    exitPointerLock(): void {
      if (documentTarget.pointerLockElement === element) documentTarget.exitPointerLock?.();
    },
    isPointerLocked(): boolean {
      return documentTarget.pointerLockElement === element;
    },
    dispose(): void {
      endPointerLook();
      clearPressedKeys();
      if (documentTarget.pointerLockElement === element) documentTarget.exitPointerLock?.();
      eventTarget.removeEventListener("keydown", onKeyDown);
      eventTarget.removeEventListener("keyup", onKeyUp);
      eventTarget.removeEventListener("blur", clearPressedKeys);
      documentTarget.removeEventListener("visibilitychange", onVisibilityChange);
      documentTarget.removeEventListener("pointerlockchange", onPointerLockChange);
      element.removeEventListener("pointerdown", onPointerDown);
      element.removeEventListener("pointermove", onPointerMove);
      element.removeEventListener("pointerup", endPointerLook);
      element.removeEventListener("pointercancel", endPointerLook);
      element.removeEventListener("wheel", onWheel);
    },
  };
}
