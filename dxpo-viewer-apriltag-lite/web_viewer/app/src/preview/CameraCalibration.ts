export interface CameraCalibration {
  schema_version: "spatial_camera_calibration_v1";
  image: { width: number; height: number };
  intrinsics: { fx: number; fy: number; cx: number; cy: number };
  distortion: {
    model: "opencv_plumb_bob";
    coefficients: number[];
  };
  camera: {
    manufacturer?: string;
    model: string;
    device_identifier?: string;
  };
  calibrated_at: string;
}

export interface ScaledCameraCalibration {
  fx: number;
  fy: number;
  cx: number;
  cy: number;
  distortion_coefficients: number[];
  source: string;
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label}はobjectで指定してください。`);
  return value as Record<string, unknown>;
}

function finite(value: unknown, label: string, positive = false): number {
  if (typeof value !== "number" || !Number.isFinite(value) || (positive && value <= 0)) {
    throw new Error(`${label}は${positive ? "正の" : "有限な"}数値で指定してください。`);
  }
  return value;
}

export function parseCameraCalibration(value: unknown): CameraCalibration {
  const root = record(value, "校正ファイル");
  if (root.schema_version !== "spatial_camera_calibration_v1") {
    throw new Error("schema_versionはspatial_camera_calibration_v1である必要があります。");
  }
  const image = record(root.image, "image");
  const intrinsics = record(root.intrinsics, "intrinsics");
  const distortion = record(root.distortion, "distortion");
  const camera = record(root.camera, "camera");
  const width = finite(image.width, "image.width", true);
  const height = finite(image.height, "image.height", true);
  if (!Number.isInteger(width) || !Number.isInteger(height)) throw new Error("撮影解像度は整数で指定してください。");
  const coefficients = distortion.coefficients;
  if (!Array.isArray(coefficients) || ![4, 5, 8].includes(coefficients.length)) {
    throw new Error("distortion.coefficientsはOpenCV形式の4、5、8要素で指定してください。");
  }
  const parsedCoefficients = coefficients.map((item, index) => finite(item, `distortion.coefficients[${index}]`));
  if (distortion.model !== "opencv_plumb_bob") throw new Error("distortion.modelはopencv_plumb_bobのみ対応しています。");
  if (typeof camera.model !== "string" || camera.model.trim() === "") throw new Error("camera.modelを指定してください。");
  if (typeof root.calibrated_at !== "string" || !Number.isFinite(Date.parse(root.calibrated_at))) {
    throw new Error("calibrated_atはISO 8601日時で指定してください。");
  }
  const optionalString = (candidate: unknown, label: string): string | undefined => {
    if (candidate === undefined) return undefined;
    if (typeof candidate !== "string" || candidate.trim() === "") throw new Error(`${label}は空でない文字列にしてください。`);
    return candidate;
  };
  return {
    schema_version: "spatial_camera_calibration_v1",
    image: { width, height },
    intrinsics: {
      fx: finite(intrinsics.fx, "intrinsics.fx", true),
      fy: finite(intrinsics.fy, "intrinsics.fy", true),
      cx: finite(intrinsics.cx, "intrinsics.cx"),
      cy: finite(intrinsics.cy, "intrinsics.cy"),
    },
    distortion: { model: "opencv_plumb_bob", coefficients: parsedCoefficients },
    camera: {
      manufacturer: optionalString(camera.manufacturer, "camera.manufacturer"),
      model: camera.model,
      device_identifier: optionalString(camera.device_identifier, "camera.device_identifier"),
    },
    calibrated_at: root.calibrated_at,
  };
}

export function scaledCameraCalibration(
  calibration: CameraCalibration,
  width: number,
  height: number,
): ScaledCameraCalibration {
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    throw new Error("解析解像度が不正です。");
  }
  const sourceAspect = calibration.image.width / calibration.image.height;
  const targetAspect = width / height;
  if (Math.abs(sourceAspect - targetAspect) / sourceAspect > 0.01) {
    throw new Error(`校正解像度${calibration.image.width}x${calibration.image.height}と解析解像度${width}x${height}の縦横比が一致しません。`);
  }
  const sx = width / calibration.image.width;
  const sy = height / calibration.image.height;
  return {
    fx: calibration.intrinsics.fx * sx,
    fy: calibration.intrinsics.fy * sy,
    cx: calibration.intrinsics.cx * sx,
    cy: calibration.intrinsics.cy * sy,
    distortion_coefficients: [...calibration.distortion.coefficients],
    source: `calibration:${calibration.camera.model}:${calibration.image.width}x${calibration.image.height}:${calibration.calibrated_at}`,
  };
}

export function sanitizedMediaTrackRecord(
  value: Record<string, unknown>,
  includeDeviceIdentifiers: boolean,
): Record<string, unknown> {
  if (includeDeviceIdentifiers) return { ...value };
  return Object.fromEntries(Object.entries(value).filter(([key]) => !["deviceId", "groupId"].includes(key)));
}
