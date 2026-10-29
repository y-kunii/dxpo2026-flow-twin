# Third-party notices

This repository and its generated Viewer bundles depend on third-party software. The identifiers below are taken from the pinned package metadata or the upstream container declaration. They do not replace the complete license texts distributed by those projects.

## Viewer runtime

|Component|Pinned version|Declared license|
|---|---:|---|
|`@sparkjsdev/spark`|2.1.0|MIT|
|`three`|0.180.0|MIT|

## Viewer build and test tooling

|Component|Pinned version|Declared license|
|---|---:|---|
|Vite|7.3.7|MIT|
|TypeScript|5.9.3|Apache-2.0|
|`ws`|8.21.0|MIT|
|Playwright Test|1.62.1|Apache-2.0|

## App A Python and model runtime

|Component|Pinned version|Declared license|
|---|---:|---|
|Pillow|12.3.0|MIT-CMU|
|SciPy|1.15.3|BSD-3-Clause (binary wheels may carry additional notices)|
|Transformers|5.14.1|Apache-2.0|
|Accelerate|1.14.0|Apache-2.0|
|OpenCV Python headless|5.0.0.93|Apache-2.0|
|pupil-apriltags|1.0.4.post11|MIT|
|MediaPipe|0.10.35|Apache-2.0|
|PyTorch|2.7.1+cu128|BSD-3-Clause|
|TorchVision|0.22.1+cu128|BSD-3-Clause|
|MediaPipe selfie segmenter model|float16 revision 1|Apache-2.0 project asset; SHA-256 pinned in `runtime_environment.lock.json`|

The generation image is based on NVIDIA CUDA 12.8.1 cuDNN for Ubuntu 22.04 and is governed by the NVIDIA Deep Learning Container License included in that image. Ubuntu packages include COLMAP and FFmpeg and retain their respective upstream and Ubuntu package notices. Models downloaded through Hugging Face must retain the license and usage conditions published with the exact model revision.

## Scene and customer assets

SPZ, GLB, images, videos, calibration data, Scene Packages and MRP content are not automatically licensed by this repository. Each distributed Scene Package must retain its own `licenses/` content and provenance. The distribution builder never treats a software dependency license as permission to redistribute scene data.

## Repository license status

The repository owner has not yet selected a repository-wide license. Until a root `LICENSE` file is approved and added, generated ZIPs are for internal evaluation only and must not be represented as a public software release.
