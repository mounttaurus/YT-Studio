// ギミック部品集（Docs/OPENING_GIMMICK_CATALOG.md）の props の型。Python（app/core/lab.py）が作る。
export type FontRef = {id: string; family: string; weight: number; role: string};

export type Enter = {type: 'portal'; from: number; to: number; x: number; y: number; r: number} | null;

export type DecodeParams = {
  lines: {text: string; size: number; font: FontRef}[];
  stamp: {text: string; font: FontRef} | null;
  len: number; // ショットの長さ（フレーム）。解読の速さをここから逆算する
};

export type FontCycleParams = {text: string; sub: string; fonts: FontRef[]; every: number};

export type ParticleStage = {shape: string; at: number; spin: boolean; points: number[][]};
export type ParticleParams = {
  n: number;
  colors: 'rainbow' | 'mono';
  spin: number;
  stages: ParticleStage[];
  morph_frames: number;
};

export type Shot =
  | {id: string; gimmick: 'decode'; from: number; to: number; params: DecodeParams; enter: Enter}
  | {id: string; gimmick: 'font_cycle'; from: number; to: number; params: FontCycleParams; enter: Enter}
  | {id: string; gimmick: 'particle_morph'; from: number; to: number; params: ParticleParams; enter: Enter};

export type LabProps = {
  template: {id: string; version: number};
  fps: number;
  total_frames: number;
  hud: {label?: string} | null;
  texture: {grain?: boolean; scanlines?: boolean; vignette?: boolean} | null;
  asset_base: string;
  shots: Shot[];
};
