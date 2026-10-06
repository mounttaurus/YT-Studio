// kinetic_teaser v1 の props。Python（motion-agent/app/core/props.py）が作り、検査済みのものだけが来る。
// 名前は opening.json に合わせてスネークケース（変換の層を作らない）。
export type BeatId = 'b1_first' | 'b2_flow' | 'b3_montage' | 'b4_out' | 'b5_title';
export type Range = {from: number; to: number};

// 時間の本籍は Python の timing。ここでは計算しない（フレーム数で受け取る）
export type Timing = {
  fps: number;
  total_frames: number;
  beats: Record<BeatId, Range>;
  b2_switches: number[];
  b3_cuts: {from: number; to: number; material: number}[];
  sfx: {frame: number; kind: string}[];
};

export type FirstItem = {text: string; kind: 'word' | 'date' | 'place'};
export type Material = {src: string; type: 'image' | 'video'; in: number};
export type Variant = {
  palette: string;
  montage_motion: 'fly' | 'cut';
  montage_tone: 'mono' | 'color';
  grain: boolean;
};

export type KineticTeaserProps = {
  template: {id: string; version: number};
  variant: Variant;
  b1_first: FirstItem[];
  b2_flow: string[];
  b3_montage: Material[];
  b4_out: 'white' | 'black';
  b5_title: {plate: string | null; main: string; sub: string};
  asset_base: string;
  timing: Timing;
};
