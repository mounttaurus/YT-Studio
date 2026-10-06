// ギミックの試作場: ショットの並び（props.shots）を描く。G2 の演出プランの描き手の原型。
// 繋ぎ: 次のショットが enter.type === 'portal' なら、重なりの区間を PortalZoom で描く
// （外＝このショットの続き、内＝次のショットの始まり）。それ以外は切り替え。
import React from 'react';
import {AbsoluteFill, CalculateMetadataFunction, Sequence} from 'remotion';
import {LabProps, Shot} from './types';
import {BACKDROP as DECODE_BG, Decode} from './Decode';
import {BACKDROP as FONT_CYCLE_BG, FontCycle} from './FontCycle';
import {BACKDROP as PARTICLE_BG, ParticleMorph} from './ParticleMorph';

const BACKDROPS: Record<Shot['gimmick'], string> = {
  decode: DECODE_BG,
  font_cycle: FONT_CYCLE_BG,
  particle_morph: PARTICLE_BG,
};
import {Hud} from './Hud';
import {Texture} from './Texture';
import {PortalMark, PortalZoom} from './PortalZoom';

const ShotView: React.FC<{shot: Shot}> = ({shot}) => {
  if (shot.gimmick === 'decode') return <Decode params={shot.params} />;
  if (shot.gimmick === 'font_cycle') return <FontCycle params={shot.params} />;
  return <ParticleMorph params={shot.params} />;
};

// 親の区間の中で、ショットの頭（start＝親の頭からのずれ・負もあり）を基準にした時間で子を描く
const At: React.FC<{start: number; children: React.ReactNode}> = ({start, children}) => (
  <Sequence from={start} layout="none">
    {children}
  </Sequence>
);

export const GimmickLab: React.FC<LabProps> = ({shots, hud, texture}) => (
  <AbsoluteFill style={{background: '#000'}}>
    {shots.map((s, i) => {
      const next = shots[i + 1];
      const portal = next?.enter?.type === 'portal' ? next.enter : null;
      const soloFrom = s.enter ? s.enter.to : s.from;
      const soloTo = portal ? portal.from : s.to;
      const mark = portal ? <PortalMark x={portal.x} y={portal.y} r={portal.r} appearAt={portal.from - s.from - 18} /> : null;
      return (
        <React.Fragment key={s.id}>
          {soloTo > soloFrom ? (
            <Sequence from={soloFrom} durationInFrames={soloTo - soloFrom}>
              <At start={s.from - soloFrom}>
                <AbsoluteFill>
                  <ShotView shot={s} />
                  {mark}
                </AbsoluteFill>
              </At>
            </Sequence>
          ) : null}
          {portal && next ? (
            <Sequence from={portal.from} durationInFrames={portal.to - portal.from}>
              <PortalZoom
                x={portal.x}
                y={portal.y}
                r={portal.r}
                len={portal.to - portal.from}
                innerBackdrop={BACKDROPS[next.gimmick]}
                outer={
                  <At start={s.from - portal.from}>
                    <AbsoluteFill>
                      <ShotView shot={s} />
                      {mark}
                    </AbsoluteFill>
                  </At>
                }
                inner={
                  <At start={next.from - portal.from}>
                    <ShotView shot={next} />
                  </At>
                }
              />
            </Sequence>
          ) : null}
        </React.Fragment>
      );
    })}
    {texture ? <Texture {...texture} /> : null}
    {hud ? <Hud label={hud.label} /> : null}
  </AbsoluteFill>
);

export const calculateLabMetadata: CalculateMetadataFunction<LabProps> = ({props}) => ({
  durationInFrames: props.total_frames,
  fps: props.fps,
});
