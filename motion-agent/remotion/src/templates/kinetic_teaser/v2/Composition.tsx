// kinetic_teaser v2 本体: 演出プランのショットの並びを描く。
// ①〜⑤は v1 の部品（K1〜K5＝v1 の beats/*.tsx をそのまま使う。v1 は凍結なので v2 から読んでも見た目は変わらない）、
// ギミック（B3・B4・C1）は gimmicks/ の部品。繋ぎ（A1・A3・A4・morph）は前後のショットの重なりの区間に描く。
// 時間の計算はしない（Python の timing.plan_timeline が本籍）。
import React from 'react';
import {AbsoluteFill, CalculateMetadataFunction, Sequence} from 'remotion';
import {PlanEnter, PlanProps, PlanShot} from './types';
import {Grain, Palette, Scanlines, Vignette, palette} from '../v1/kit';
import {B1First} from '../v1/beats/B1First';
import {B2Flow} from '../v1/beats/B2Flow';
import {B3Montage} from '../v1/beats/B3Montage';
import {B4Out, B5Title} from '../v1/beats/B4B5';
import {BACKDROP as DECODE_BG, Decode} from '../../../gimmicks/Decode';
import {BACKDROP as FONT_CYCLE_BG, FontCycle} from '../../../gimmicks/FontCycle';
import {BACKDROP as PARTICLE_BG, ParticleMorph} from '../../../gimmicks/ParticleMorph';
import {PortalMark, PortalZoom} from '../../../gimmicks/PortalZoom';
import {LightTransition} from '../../../gimmicks/transitions/Light';
import {IrisTransition} from '../../../gimmicks/transitions/Iris';
import {MorphTransition} from '../../../gimmicks/transitions/Morph';
import {Hud} from '../../../gimmicks/Hud';
import {Texture} from '../../../gimmicks/Texture';

const len = (s: PlanShot) => s.to - s.from;

const ShotView: React.FC<{shot: PlanShot; pal: Palette; p: PlanProps}> = ({shot, pal, p}) => {
  switch (shot.gimmick) {
    case 'K1_first':
      return <B1First items={shot.params.items} pal={pal} len={len(shot)} />;
    case 'K2_flow':
      return <B2Flow words={shot.params.words} switches={shot.params.switches} pal={pal} len={len(shot)} />;
    case 'K3_montage':
      // ④が続く時は④の終わりまで描き続ける（hold_to）。カードの寿命の基準になる
      return (
        <B3Montage
          materials={shot.params.materials}
          cuts={shot.params.cuts}
          pal={pal}
          variant={p.variant}
          assetBase={p.asset_base}
          total={shot.hold_to - shot.from}
          calm
        />
      );
    case 'K4_out':
      return <B4Out out={shot.params.out} len={len(shot)} />;
    case 'K5_title':
      return <B5Title title={shot.params.title} out={shot.params.out} pal={pal} assetBase={p.asset_base} len={len(shot)} />;
    case 'B3_decode':
      return <Decode params={shot.params} />;
    case 'B4_font_cycle':
      return <FontCycle params={shot.params} />;
    case 'C1_particle_morph':
      return <ParticleMorph params={shot.params} />;
  }
};

// 入口の円の余白に塗る、ショットの外周の色
const backdropOf = (shot: PlanShot, pal: Palette): string => {
  switch (shot.gimmick) {
    case 'K2_flow':
      return pal.flow[0][0];
    case 'K5_title':
      return pal.title_bg;
    case 'B3_decode':
      return DECODE_BG;
    case 'B4_font_cycle':
      return FONT_CYCLE_BG;
    case 'C1_particle_morph':
      return PARTICLE_BG;
    default:
      return pal.base;
  }
};

// 親の区間の中で、ショットの頭（start＝親の頭からのずれ・負もあり）を基準にした時間で子を描く
const At: React.FC<{start: number; children: React.ReactNode}> = ({start, children}) => (
  <Sequence from={start} layout="none">
    {children}
  </Sequence>
);

const Transition: React.FC<{enter: PlanEnter; outer: React.ReactNode; inner: React.ReactNode; innerBackdrop: string}> = ({
  enter,
  outer,
  inner,
  innerBackdrop,
}) => {
  const n = enter.to - enter.from;
  if (enter.type === 'A1_portal')
    return <PortalZoom x={enter.x} y={enter.y} r={enter.r} len={n} innerBackdrop={innerBackdrop} outer={outer} inner={inner} />;
  if (enter.type === 'A3_light') return <LightTransition x={enter.x} y={enter.y} len={n} outer={outer} inner={inner} />;
  if (enter.type === 'A4_iris') return <IrisTransition x={enter.x} y={enter.y} len={n} outer={outer} inner={inner} />;
  return <MorphTransition len={n} outer={outer} inner={inner} />;
};

export const KineticTeaserV2: React.FC<PlanProps> = (p) => {
  const pal = palette(p.variant.palette);
  const {shots, overlay} = p;
  return (
    <AbsoluteFill style={{background: pal.base}}>
      {shots.map((s, i) => {
        const next = shots[i + 1];
        const into = next?.enter ?? null; // 次のショットへ重なって繋ぐ区間
        const soloFrom = s.enter ? s.enter.to : s.from;
        const soloTo = into ? into.from : s.hold_to;
        // ポータルの入口の印は、外の場面（このショット）に置く
        const mark = into?.type === 'A1_portal' ? <PortalMark x={into.x} y={into.y} r={into.r} appearAt={into.from - s.from - 18} /> : null;
        return (
          <React.Fragment key={s.id}>
            {soloTo > soloFrom ? (
              <Sequence from={soloFrom} durationInFrames={soloTo - soloFrom}>
                <At start={s.from - soloFrom}>
                  <AbsoluteFill>
                    <ShotView shot={s} pal={pal} p={p} />
                    {mark}
                  </AbsoluteFill>
                </At>
              </Sequence>
            ) : null}
            {into && next ? (
              <Sequence from={into.from} durationInFrames={into.to - into.from}>
                <Transition
                  enter={into}
                  innerBackdrop={backdropOf(next, pal)}
                  outer={
                    <At start={s.from - into.from}>
                      <AbsoluteFill>
                        <ShotView shot={s} pal={pal} p={p} />
                        {mark}
                      </AbsoluteFill>
                    </At>
                  }
                  inner={
                    <At start={next.from - into.from}>
                      <AbsoluteFill>
                        <ShotView shot={next} pal={pal} p={p} />
                      </AbsoluteFill>
                    </At>
                  }
                />
              </Sequence>
            ) : null}
          </React.Fragment>
        );
      })}
      {overlay.texture ? (
        <Texture {...overlay.texture} />
      ) : (
        <>
          <Scanlines />
          {p.variant.grain ? <Grain /> : null}
          <Vignette />
        </>
      )}
      {overlay.hud ? <Hud label={overlay.hud.label} /> : null}
    </AbsoluteFill>
  );
};

export const calculateMetadataV2: CalculateMetadataFunction<PlanProps> = ({props}) => ({
  durationInFrames: props.total_frames,
  fps: props.fps,
});
