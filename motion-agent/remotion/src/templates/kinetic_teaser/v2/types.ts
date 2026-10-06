// kinetic_teaser v2 の props。Python（motion-agent/app/core/plan.py）が作り、検査済みのものだけが来る。
// 長さ・位置は全てフレーム（時間の本籍は Python の timing.plan_timeline。ここでは計算しない）。
import {DecodeParams, FontCycleParams, ParticleParams} from '../../../gimmicks/types';
import {FirstItem, Material, Variant} from '../v1/types';

export type EnterType = 'A1_portal' | 'A3_light' | 'A4_iris' | 'morph';
export type PlanEnter = {type: EnterType; from: number; to: number; x: number; y: number; r: number};
export type Cut = {from: number; to: number; material: number};

type Base = {id: string; beat: string; from: number; to: number; hold_to: number; enter: PlanEnter | null};

export type PlanShot =
  | (Base & {gimmick: 'K1_first'; params: {items: FirstItem[]}})
  | (Base & {gimmick: 'K2_flow'; params: {words: string[]; switches: number[]}})
  | (Base & {gimmick: 'K3_montage'; params: {materials: Material[]; cuts: Cut[]}})
  | (Base & {gimmick: 'K4_out'; params: {out: 'white' | 'black'}})
  | (Base & {gimmick: 'K5_title'; params: {title: {plate: string | null; main: string; sub: string}; out: 'white' | 'black'}})
  | (Base & {gimmick: 'B3_decode'; params: DecodeParams})
  | (Base & {gimmick: 'B4_font_cycle'; params: FontCycleParams})
  | (Base & {gimmick: 'C1_particle_morph'; params: ParticleParams});

export type PlanProps = {
  template: {id: string; version: number};
  fps: number;
  bpm: number;
  total_frames: number;
  variant: Variant;
  overlay: {hud: {label?: string} | null; texture: {grain?: boolean; scanlines?: boolean; vignette?: boolean} | null};
  asset_base: string;
  shots: PlanShot[];
  sfx: {frame: number; kind: string}[];
};
