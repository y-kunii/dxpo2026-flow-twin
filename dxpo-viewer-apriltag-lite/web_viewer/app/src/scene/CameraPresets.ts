export interface CameraPresetFile {
  cameras: CameraPreset[];
}

export interface CameraPreset {
  id: string;
  name: string;
  position_m: [number, number, number];
  look_at_m: [number, number, number];
  up_world?: [number, number, number];
  vertical_fov_deg: number;
  resolution: [number, number];
  near_m?: number;
  far_m?: number;
}
