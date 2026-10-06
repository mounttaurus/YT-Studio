// kinetic_teaser v1 本体: ①〜⑤のビートを timing のフレームに並べる。
// 時間の計算はしない（Python の timing が本籍）。ここは並べて描くだけ。
import React from 'react';
import {AbsoluteFill, CalculateMetadataFunction, Sequence} from 'remotion';
import {KineticTeaserProps, Range} from './types';
import {Grain, Scanlines, Vignette, palette} from './kit';
import {B1First} from './beats/B1First';
import {B2Flow} from './beats/B2Flow';
import {B3Montage} from './beats/B3Montage';
import {B4Out, B5Title} from './beats/B4B5';

const len = (r: Range) => r.to - r.from;

export const KineticTeaser: React.FC<KineticTeaserProps> = (p) => {
  const pal = palette(p.variant.palette);
  const t = p.timing;
  const b = t.beats;
  return (
    <AbsoluteFill style={{background: pal.base}}>
      <Sequence from={b.b1_first.from} durationInFrames={len(b.b1_first)}>
        <B1First items={p.b1_first} pal={pal} len={len(b.b1_first)} />
      </Sequence>
      <Sequence from={b.b2_flow.from} durationInFrames={len(b.b2_flow)}>
        <B2Flow
          words={p.b2_flow}
          switches={t.b2_switches.map((s) => s - b.b2_flow.from)}
          pal={pal}
          len={len(b.b2_flow)}
        />
      </Sequence>
      {/* ③は④の終わりまで描き続け、④が上から溶かす */}
      <Sequence from={b.b3_montage.from} durationInFrames={b.b4_out.to - b.b3_montage.from}>
        <B3Montage
          materials={p.b3_montage}
          cuts={t.b3_cuts.map((c) => ({...c, from: c.from - b.b3_montage.from, to: c.to - b.b3_montage.from}))}
          pal={pal}
          variant={p.variant}
          assetBase={p.asset_base}
          total={b.b4_out.to - b.b3_montage.from}
        />
      </Sequence>
      <Sequence from={b.b4_out.from} durationInFrames={len(b.b4_out)}>
        <B4Out out={p.b4_out} len={len(b.b4_out)} />
      </Sequence>
      <Sequence from={b.b5_title.from} durationInFrames={len(b.b5_title)}>
        <B5Title title={p.b5_title} out={p.b4_out} pal={pal} assetBase={p.asset_base} len={len(b.b5_title)} />
      </Sequence>
      <Scanlines />
      {p.variant.grain ? <Grain /> : null}
      <Vignette />
    </AbsoluteFill>
  );
};

export const calculateMetadata: CalculateMetadataFunction<KineticTeaserProps> = ({props}) => ({
  durationInFrames: props.timing.total_frames,
  fps: props.timing.fps,
});
