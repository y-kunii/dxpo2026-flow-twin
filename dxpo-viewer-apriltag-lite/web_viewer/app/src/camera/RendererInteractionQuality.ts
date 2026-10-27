import type { WebGLRenderer } from "three";

export interface RendererInteractionQuality {
  readonly interacting: boolean;
  begin(): void;
  end(): void;
  reset(): void;
  resize(): void;
  dispose(): void;
}

const MAX_IDLE_RENDER_PIXELS = 1_600 * 900;
const INTERACTION_PIXEL_RATIO = 0.9;

export function createRendererInteractionQuality(
  renderer: WebGLRenderer,
): RendererInteractionQuality {
  let restoreTimer: number | undefined;
  let interacting = false;

  const qualityPixelRatio = (): number => Math.max(0.65, Math.min(
    window.devicePixelRatio,
    1.25,
    Math.sqrt(MAX_IDLE_RENDER_PIXELS / Math.max(1, window.innerWidth * window.innerHeight)),
  ));
  const cancelRestore = (): void => {
    if (restoreTimer === undefined) return;
    window.clearTimeout(restoreTimer);
    restoreTimer = undefined;
  };
  const resize = (): void => {
    renderer.setPixelRatio(interacting
      ? Math.min(INTERACTION_PIXEL_RATIO, qualityPixelRatio())
      : qualityPixelRatio());
    renderer.setSize(window.innerWidth, window.innerHeight);
  };

  return {
    get interacting(): boolean {
      return interacting;
    },
    begin(): void {
      interacting = true;
      cancelRestore();
      resize();
    },
    end(): void {
      cancelRestore();
      restoreTimer = window.setTimeout(() => {
        interacting = false;
        restoreTimer = undefined;
        resize();
      }, 300);
    },
    reset(): void {
      cancelRestore();
      interacting = false;
      resize();
    },
    resize,
    dispose(): void {
      cancelRestore();
      interacting = false;
    },
  };
}
