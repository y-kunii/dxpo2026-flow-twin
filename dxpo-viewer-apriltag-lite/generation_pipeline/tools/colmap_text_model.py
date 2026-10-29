from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation


@dataclass(frozen=True)
class ColmapCamera:
    camera_id: int
    model: str
    width: int
    height: int
    params: list[float]


@dataclass(frozen=True)
class ColmapImage:
    image_id: int
    qvec_wxyz: tuple[float, float, float, float]
    tvec: np.ndarray
    camera_id: int
    name: str

    @property
    def rotation_world_to_camera(self) -> np.ndarray:
        qw, qx, qy, qz = self.qvec_wxyz
        return Rotation.from_quat([qx, qy, qz, qw]).as_matrix()

    @property
    def camera_center_world(self) -> np.ndarray:
        r_cw = self.rotation_world_to_camera
        return -r_cw.T @ self.tvec


def parse_cameras_txt(path: Path) -> dict[int, ColmapCamera]:
    cameras: dict[int, ColmapCamera] = {}

    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        parts = line.split()
        camera_id = int(parts[0])
        model = parts[1]
        width = int(parts[2])
        height = int(parts[3])
        params = [float(v) for v in parts[4:]]

        cameras[camera_id] = ColmapCamera(
            camera_id=camera_id,
            model=model,
            width=width,
            height=height,
            params=params,
        )

    return cameras


def parse_images_txt(path: Path) -> dict[str, ColmapImage]:
    images: dict[str, ColmapImage] = {}
    lines = path.read_text(encoding="utf-8-sig").splitlines()

    index = 0

    while index < len(lines):
        line = lines[index].strip()

        if not line or line.startswith("#"):
            index += 1
            continue

        parts = line.split()

        image_id = int(parts[0])
        qw, qx, qy, qz = [float(v) for v in parts[1:5]]
        tx, ty, tz = [float(v) for v in parts[5:8]]
        camera_id = int(parts[8])
        name = " ".join(parts[9:])

        images[name] = ColmapImage(
            image_id=image_id,
            qvec_wxyz=(qw, qx, qy, qz),
            tvec=np.array([tx, ty, tz], dtype=float),
            camera_id=camera_id,
            name=name,
        )

        # COLMAP images.txt uses two lines per image.
        index += 2

    return images


def load_colmap_text_model(model_dir: Path) -> tuple[dict[int, ColmapCamera], dict[str, ColmapImage]]:
    cameras_path = model_dir / "cameras.txt"
    images_path = model_dir / "images.txt"

    if not cameras_path.exists():
        raise RuntimeError(f"cameras.txt not found: {cameras_path}")

    if not images_path.exists():
        raise RuntimeError(f"images.txt not found: {images_path}")

    cameras = parse_cameras_txt(cameras_path)
    images = parse_images_txt(images_path)

    return cameras, images


def camera_intrinsics_matrix(camera: ColmapCamera) -> np.ndarray:
    if camera.model == "SIMPLE_PINHOLE":
        f, cx, cy = camera.params
        return np.array(
            [
                [f, 0.0, cx],
                [0.0, f, cy],
                [0.0, 0.0, 1.0],
            ],
            dtype=float,
        )

    if camera.model == "PINHOLE":
        fx, fy, cx, cy = camera.params
        return np.array(
            [
                [fx, 0.0, cx],
                [0.0, fy, cy],
                [0.0, 0.0, 1.0],
            ],
            dtype=float,
        )

    raise RuntimeError(f"Unsupported camera model for intrinsics matrix: {camera.model}")
