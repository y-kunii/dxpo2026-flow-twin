import argparse
import json
from pathlib import Path
import numpy as np
from scipy.spatial.transform import Rotation as R

def rodrigues_from_to(a, b):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    a /= np.linalg.norm(a)
    b /= np.linalg.norm(b)

    v = np.cross(a, b)
    c = np.clip(np.dot(a, b), -1.0, 1.0)

    if c > 0.999999:
        return np.eye(3)

    if c < -0.999999:
        # 180-degree rotation: choose any orthogonal axis
        axis = np.array([1.0, 0.0, 0.0])
        if abs(a[0]) > 0.9:
            axis = np.array([0.0, 1.0, 0.0])
        v = np.cross(a, axis)
        v /= np.linalg.norm(v)
        K = np.array([
            [0, -v[2], v[1]],
            [v[2], 0, -v[0]],
            [-v[1], v[0], 0]
        ])
        return np.eye(3) + 2 * (K @ K)

    s = np.linalg.norm(v)
    K = np.array([
        [0, -v[2], v[1]],
        [v[2], 0, -v[0]],
        [-v[1], v[0], 0]
    ])
    return np.eye(3) + K + (K @ K) * ((1 - c) / (s ** 2))

def parse_images_txt(path: Path):
    lines = path.read_text(encoding='utf-8').splitlines()
    header = []
    images = []

    i = 0
    while i < len(lines):
        line = lines[i]
        if (not line.strip()) or line.startswith('#'):
            header.append(line)
            i += 1
            continue

        parts = line.split()
        image_id = int(parts[0])
        qw, qx, qy, qz = map(float, parts[1:5])
        tx, ty, tz = map(float, parts[5:8])
        camera_id = int(parts[8])
        name = ' '.join(parts[9:])
        points2d_line = lines[i + 1] if i + 1 < len(lines) else ''

        rot = R.from_quat([qx, qy, qz, qw]).as_matrix()
        tvec = np.array([tx, ty, tz], dtype=float)

        images.append({
            'image_id': image_id,
            'Rcw': rot,          # world -> camera
            'tvec': tvec,
            'camera_id': camera_id,
            'name': name,
            'points2d_line': points2d_line,
        })
        i += 2

    return header, images

def write_images_txt(path: Path, header, images):
    out = []
    if header:
        out.extend(header)
    else:
        out.append('# Image list with two lines of data per image:')
        out.append('# IMAGE_ID, QW, QX, QY, QZ, TX, TY, TZ, CAMERA_ID, IMAGE_NAME')
        out.append('# POINTS2D[] as (X, Y, POINT3D_ID)')

    for img in images:
        qx, qy, qz, qw = R.from_matrix(img['Rcw']).as_quat()
        q = [qw, qx, qy, qz]
        t = img['tvec']
        line1 = (
            f"{img['image_id']} "
            + " ".join(f"{v:.17g}" for v in q)
            + " "
            + " ".join(f"{v:.17g}" for v in t)
            + f" {img['camera_id']} {img['name']}"
        )
        out.append(line1)
        out.append(img['points2d_line'])

    path.write_text('\n'.join(out) + '\n', encoding='utf-8')

def parse_points3d_txt(path: Path):
    lines = path.read_text(encoding='utf-8').splitlines()
    header = []
    points = []

    for line in lines:
        if (not line.strip()) or line.startswith('#'):
            header.append(line)
            continue

        parts = line.split()
        pid = int(parts[0])
        xyz = np.array(list(map(float, parts[1:4])), dtype=float)
        rgb = parts[4:7]
        error = parts[7]
        track = parts[8:]

        points.append({
            'id': pid,
            'xyz': xyz,
            'rgb': rgb,
            'error': error,
            'track': track,
        })

    return header, points

def write_points3d_txt(path: Path, header, points):
    out = []
    if header:
        out.extend(header)
    else:
        out.append('# 3D point list')
    for p in points:
        line = (
            f"{p['id']} "
            + " ".join(f"{v:.17g}" for v in p['xyz'])
            + " "
            + " ".join(p['rgb'])
            + f" {p['error']}"
        )
        if p['track']:
            line += " " + " ".join(p['track'])
        out.append(line)
    path.write_text('\n'.join(out) + '\n', encoding='utf-8')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', required=True, help='Input COLMAP TXT model folder')
    ap.add_argument('--output', required=True, help='Output COLMAP TXT model folder')
    ap.add_argument('--up_axis', default='y', choices=['x', 'y', 'z'])
    args = ap.parse_args()

    in_dir = Path(args.input)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    cameras_in = in_dir / 'cameras.txt'
    images_in = in_dir / 'images.txt'
    points_in = in_dir / 'points3D.txt'

    if not cameras_in.exists() or not images_in.exists() or not points_in.exists():
        raise RuntimeError('cameras.txt / images.txt / points3D.txt が見つかりません。')

    header_i, images = parse_images_txt(images_in)
    header_p, points = parse_points3d_txt(points_in)

    if len(images) < 3:
        raise RuntimeError('カメラ数が少なすぎます。')

    centers = []
    for img in images:
        C = -img['Rcw'].T @ img['tvec']
        centers.append(C)
    centers = np.stack(centers, axis=0)

    centroid = centers.mean(axis=0)
    _, _, vh = np.linalg.svd(centers - centroid, full_matrices=False)
    normal = vh[-1]

    target_map = {
        'x': np.array([1.0, 0.0, 0.0]),
        'y': np.array([0.0, 1.0, 0.0]),
        'z': np.array([0.0, 0.0, 1.0]),
    }
    target = target_map[args.up_axis]

    if np.dot(normal, target) < 0:
        normal = -normal

    Rw = rodrigues_from_to(normal, target)
    angle_deg = float(np.degrees(np.arccos(np.clip(np.dot(normal, target), -1.0, 1.0))))

    # rotate world points
    for p in points:
        p['xyz'] = Rw @ p['xyz']

    # update camera rotations (world rotated; translation unchanged)
    for img in images:
        img['Rcw'] = img['Rcw'] @ Rw.T

    # copy cameras.txt as-is
    (out_dir / 'cameras.txt').write_text(cameras_in.read_text(encoding='utf-8'), encoding='utf-8')
    write_images_txt(out_dir / 'images.txt', header_i, images)
    write_points3d_txt(out_dir / 'points3D.txt', header_p, points)

    report = {
        'camera_count': int(len(images)),
        'point_count': int(len(points)),
        'estimated_plane_normal_before': normal.tolist(),
        'target_up_axis': args.up_axis,
        'rotation_angle_deg': angle_deg,
        'input': str(in_dir),
        'output': str(out_dir),
    }
    (out_dir / 'level_report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')

    print('Leveled model written to:', out_dir)
    print('Estimated correction angle [deg]:', round(angle_deg, 4))
    print('Target up axis:', args.up_axis)

if __name__ == '__main__':
    main()
