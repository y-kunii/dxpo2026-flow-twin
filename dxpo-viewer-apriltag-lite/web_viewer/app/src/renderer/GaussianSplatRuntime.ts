import { SparkRenderer, SplatMesh } from "@sparkjsdev/spark";
import type * as THREE from "three";

export interface GaussianSplatRuntime {
  createSplat(options: { url: string; paged: boolean }): SplatMesh;
}

/**
 * Load the large Spark/WASM runtime only after a Scene Package manifest has
 * been accepted.  Landing pages and non-Viewer applications therefore avoid
 * downloading or parsing the renderer bundle.
 */
export function mountGaussianSplatRuntime(
  scene: THREE.Scene,
  renderer: THREE.WebGLRenderer,
): GaussianSplatRuntime {
  const spark = new SparkRenderer({
    renderer,
    lodSplatCount: 3_500_000,
    lodRenderScale: 0.85,
  });
  scene.add(spark);
  return {
    createSplat: (options) => new SplatMesh(options),
  };
}
