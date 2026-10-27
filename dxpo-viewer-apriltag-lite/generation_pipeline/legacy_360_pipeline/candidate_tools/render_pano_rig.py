from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from scipy.spatial.transform import Rotation

HFOV_DEG = 90.0
VFOV_DEG = 90.0
PITCHES_DEG = (-35.0, 0.0, 35.0)
YAW_STEPS = 4


def virtual_rotations() -> list[np.ndarray]:
    rotations = []
    yaws = np.linspace(0.0, 360.0, YAW_STEPS, endpoint=False)

    for pitch_deg in PITCHES_DEG:
        yaw_offset = 360.0 / YAW_STEPS / 2.0 if pitch_deg > 0 else 0.0
        for yaw_deg in yaws + yaw_offset:
            rotations.append(
                Rotation.from_euler(
                    "XY",
                    [-pitch_deg, -yaw_deg],
                    degrees=True,
                ).as_matrix()
            )
    return rotations


def create_rays(width: int, height: int, focal: float) -> np.ndarray:
    x, y = np.indices((width, height)).astype(np.float32)
    xy = np.column_stack([x.ravel(), y.ravel()])
    xy += 0.5
    xy[:, 0] = (xy[:, 0] - width / 2.0) / focal
    xy[:, 1] = (xy[:, 1] - height / 2.0) / focal

    rays = np.concatenate([xy, np.ones_like(xy[:, :1])], axis=-1)
    rays /= np.linalg.norm(rays, axis=-1, keepdims=True)
    return rays


def spherical_coordinates(
    pano_width: int,
    pano_height: int,
    rays_in_pano: np.ndarray,
) -> np.ndarray:
    r = rays_in_pano.T
    yaw = np.arctan2(r[0], r[2])
    pitch = -np.arctan2(r[1], np.linalg.norm(r[[0, 2]], axis=0))

    u = (1.0 + yaw / np.pi) / 2.0
    v = (1.0 - pitch * 2.0 / np.pi) / 2.0
    return np.stack([u, v], axis=-1) * np.array([pano_width, pano_height])


def rig_config(
    rotations: list[np.ndarray],
    camera_model: str,
    camera_params: list[float],
) -> list[dict]:
    cameras = [
        {
            "image_prefix": "pano_camera0/",
            "ref_sensor": True,
            "camera_model_name": camera_model,
            "camera_params": camera_params,
        }
    ]

    reference = rotations[0]

    for index, rotation in enumerate(rotations[1:], start=1):
        camera_from_rig = rotation @ reference.T
        x, y, z, w = Rotation.from_matrix(camera_from_rig).as_quat()

        cameras.append(
            {
                "image_prefix": f"pano_camera{index}/",
                "cam_from_rig_rotation": [float(w), float(x), float(y), float(z)],
                "cam_from_rig_translation": [0.0, 0.0, 0.0],
                "camera_model_name": camera_model,
                "camera_params": camera_params,
            }
        )

    return [{"cameras": cameras}]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_dir", type=Path, required=True)
    parser.add_argument("--image_dir", type=Path, required=True)
    parser.add_argument("--mask_dir", type=Path, required=True)
    parser.add_argument("--rig_config", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()

    panoramas = sorted(args.input_dir.glob("*.png"))
    if not panoramas:
        raise RuntimeError(f"PNGフレームが見つかりません: {args.input_dir}")

    first = np.asarray(Image.open(panoramas[0]).convert("RGB"))
    pano_height, pano_width = first.shape[:2]

    if pano_width != pano_height * 2:
        raise RuntimeError(
            f"入力画像は2:1の正距円筒画像である必要があります: {pano_width}x{pano_height}"
        )

    view_width = int(pano_width * HFOV_DEG / 360.0)
    view_height = int(pano_height * VFOV_DEG / 180.0)

    if view_width != view_height:
        raise RuntimeError("仮想カメラ画像が正方形になりません。入力解像度を確認してください。")

    focal = view_width / (2.0 * np.tan(np.deg2rad(HFOV_DEG) / 2.0))
    rotations = virtual_rotations()
    rays_in_camera = create_rays(view_width, view_height, focal)
    centers_in_pano = np.einsum(
        "nij,i->nj",
        np.stack(rotations),
        np.array([0.0, 0.0, 1.0]),
    )

    args.image_dir.mkdir(parents=True, exist_ok=True)
    args.mask_dir.mkdir(parents=True, exist_ok=True)

    for panorama_path in panoramas:
        panorama = np.asarray(Image.open(panorama_path).convert("RGB"))

        if panorama.shape[:2] != (pano_height, pano_width):
            raise RuntimeError(f"解像度が混在しています: {panorama_path.name}")

        for camera_index, camera_from_pano in enumerate(rotations):
            rays_in_pano = rays_in_camera @ camera_from_pano
            xy_in_pano = spherical_coordinates(
                pano_width,
                pano_height,
                rays_in_pano,
            )

            xy_in_pano = xy_in_pano.reshape(
                view_width,
                view_height,
                2,
            ).astype(np.float32)

            xy_in_pano -= 0.5

            x_coords, y_coords = np.moveaxis(
                xy_in_pano,
                [0, 1, 2],
                [2, 1, 0],
            )

            virtual_image = cv2.remap(
                panorama,
                x_coords,
                y_coords,
                cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_WRAP,
            )

            closest_camera = np.argmax(
                rays_in_pano @ centers_in_pano.T,
                axis=-1,
            )

            mask = (
                (closest_camera == camera_index).astype(np.uint8) * 255
            ).reshape(view_width, view_height).transpose()

            relative_path = Path(f"pano_camera{camera_index}") / panorama_path.name
            output_image = args.image_dir / relative_path
            output_mask = args.mask_dir / (str(relative_path) + ".png")

            output_image.parent.mkdir(parents=True, exist_ok=True)
            output_mask.parent.mkdir(parents=True, exist_ok=True)

            Image.fromarray(virtual_image).save(output_image)
            Image.fromarray(mask).save(output_mask)

    calibrated_params = [
        float(focal),
        float(view_width / 2.0),
        float(view_height / 2.0),
    ]

    args.rig_config.write_text(
        json.dumps(
            rig_config(
                rotations,
                "SIMPLE_PINHOLE",
                calibrated_params,
            ),
            indent=2,
        ),
        encoding="utf-8",
    )

    manifest = {
        "panorama_count": len(panoramas),
        "panorama_width": pano_width,
        "panorama_height": pano_height,
        "virtual_cameras_per_panorama": len(rotations),
        "virtual_camera_width": view_width,
        "virtual_camera_height": view_height,
        "camera_model": "SIMPLE_PINHOLE",
        "camera_params": f"{focal:.12f},{view_width / 2.0:.12f},{view_height / 2.0:.12f}",
        "hfov_deg": HFOV_DEG,
        "vfov_deg": VFOV_DEG,
        "pitches_deg": list(PITCHES_DEG),
        "yaw_steps": YAW_STEPS,
    }

    args.manifest.write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )

    print(f"Rendered {len(panoramas)} panoramas.")
    print(f"Created {len(panoramas) * len(rotations)} virtual-camera images.")


if __name__ == "__main__":
    main()
