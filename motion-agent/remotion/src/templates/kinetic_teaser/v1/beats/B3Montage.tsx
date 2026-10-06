// ③ 素材のカット: 写真・動画が矢継ぎ早に飛び込む（fly）か、叩くように切り替わる（cut）。
// カットの長さは timing（Python）が決める＝だんだん速くなる。直前の2枚は下に残してコラージュにする。
// ④の間もこのビートは描き続け、その上に④が重なる（白・黒へ溶ける元の絵になる）。
import React from 'react';
import {AbsoluteFill, Img, OffthreadVideo, Sequence, interpolate, random, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {Material, Variant} from '../types';
import {Palette, resolveAsset} from '../kit';

const STACK = 2; // 下に残す枚数
const CARD_W = 1180;
const CARD_H = 664;

type Cut = {from: number; to: number; material: number};

export const B3Montage: React.FC<{
  materials: Material[];
  cuts: Cut[];
  pal: Palette;
  variant: Variant;
  assetBase: string;
  total: number; // ④の分を含めた長さ
  // 光過敏への配慮（型 v2 だけが true にする。v1 は false＝従来どおり）: カットごとの白い一瞬の光を、
  // 速いカット（毎秒3回を超える長さ）では弱める。弱めないと終盤が毎秒4〜5回の明滅になる（実測）
  calm?: boolean;
}> = ({materials, cuts, pal, variant, assetBase, total, calm = false}) => {
  const frame = useCurrentFrame();
  let cur = 0;
  for (let k = 0; k < cuts.length; k++) if (cuts[k].from <= frame) cur = k;
  const cutLocal = frame - cuts[cur].from;
  const bg = cur % 2 === 0 ? pal.base : mix(pal.accent, pal.base, 0.78);
  return (
    <AbsoluteFill style={{background: bg, overflow: 'hidden'}}>
      <SpeedLines color={pal.text} frame={frame} />
      {cuts.map((c, k) => {
        const until = cuts[k + STACK + 1]?.from ?? total;
        if (until <= c.from) return null;
        const depth = Math.max(0, cur - k);
        return (
          <Sequence key={k} from={c.from} durationInFrames={until - c.from} layout="none">
            <Card
              index={k}
              material={materials[c.material]}
              depth={depth}
              cutLen={c.to - c.from}
              variant={variant}
              assetBase={assetBase}
              dimTo={calm ? 1 - 0.5 * Math.min(1, (cuts[cur].to - cuts[cur].from) / 10) : 0.5}
            />
          </Sequence>
        );
      })}
      <AbsoluteFill
        style={{
          background: '#ffffff',
          opacity: (cutLocal < 2 ? 0.22 * (2 - cutLocal) / 2 : 0) * (calm ? Math.min(1, (cuts[cur].to - cuts[cur].from) / 10) ** 3 : 1),
        }}
      />
    </AbsoluteFill>
  );
};

const Card: React.FC<{
  index: number;
  material: Material;
  depth: number;
  cutLen: number;
  variant: Variant;
  assetBase: string;
  dimTo: number; // 下に回ったカードの暗さ（v1 は 0.5。型 v2 の速いカットでは浅くして、毎カットの明るさの揺れを抑える）
}> = ({index, material, depth, cutLen, variant, assetBase, dimTo}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const rot = (random(`b3-rot-${index}`) - 0.5) * 9;
  // 下に回った絵はずらして重ね、コラージュにする
  const ox = depth === 0 ? (random(`b3-ox-${index}`) - 0.5) * 120 : (random(`b3-ux-${index}`) - 0.5) * 760;
  const oy = depth === 0 ? (random(`b3-oy-${index}`) - 0.5) * 70 : (random(`b3-uy-${index}`) - 0.5) * 380;
  let tx = 0;
  let ty = 0;
  let scale = 1 + Math.min(frame, 40) * 0.0012;
  if (variant.montage_motion === 'fly') {
    const s = spring({frame, fps, config: {damping: 17, stiffness: 300, mass: 0.6}, durationInFrames: Math.max(4, Math.min(10, cutLen))});
    const dir = Math.floor(random(`b3-dir-${index}`) * 4);
    const dist = 1500;
    tx = dir === 0 ? -dist * (1 - s) : dir === 1 ? dist * (1 - s) : 0;
    ty = dir === 2 ? -dist * 0.7 * (1 - s) : dir === 3 ? dist * 0.7 * (1 - s) : 0;
  } else {
    scale *= interpolate(frame, [0, 5], [1.16, 1], {extrapolateRight: 'clamp'});
  }
  const shrink = depth === 0 ? 1 : 0.86;
  const dim = depth === 0 ? 1 : dimTo;
  const tone = variant.montage_tone === 'mono' ? 'grayscale(1) contrast(1.3) brightness(0.95)' : 'contrast(1.1) saturate(1.1)';
  const src = resolveAsset(material.src, assetBase);
  const media: React.CSSProperties = {width: '100%', height: '100%', objectFit: 'cover', filter: `${tone} brightness(${dim})`};
  return (
    <AbsoluteFill style={{alignItems: 'center', justifyContent: 'center'}}>
      <div
        style={{
          width: CARD_W,
          height: CARD_H,
          border: '12px solid #f4f1ea',
          background: '#111',
          boxShadow: '0 30px 80px rgba(0,0,0,0.65)',
          transform: `translate(${ox + tx}px, ${oy + ty}px) rotate(${rot}deg) scale(${scale * shrink})`,
          overflow: 'hidden',
        }}
      >
        {material.type === 'video' ? (
          <OffthreadVideo src={src} muted startFrom={Math.round(material.in * fps)} style={media} />
        ) : (
          <Img src={src} style={media} />
        )}
      </div>
    </AbsoluteFill>
  );
};

// 背景の集中線（ゆっくり回る）
const SpeedLines: React.FC<{color: string; frame: number}> = ({color, frame}) => {
  const n = 48;
  const rot = frame * 0.35;
  return (
    <AbsoluteFill style={{opacity: 0.1, alignItems: 'center', justifyContent: 'center'}}>
      <svg width={2400} height={2400} viewBox="-100 -100 200 200" style={{transform: `rotate(${rot}deg)`}}>
        {Array.from({length: n}, (_, i) => {
          const a = (i / n) * Math.PI * 2;
          const w = 0.012 + random(`b3-line-${i}`) * 0.03;
          const p1 = [Math.cos(a - w) * 100, Math.sin(a - w) * 100];
          const p2 = [Math.cos(a + w) * 100, Math.sin(a + w) * 100];
          return <polygon key={i} points={`0,0 ${p1[0]},${p1[1]} ${p2[0]},${p2[1]}`} fill={color} />;
        })}
      </svg>
    </AbsoluteFill>
  );
};

// 2色を混ぜる（#rrggbb）
const mix = (a: string, b: string, t: number): string => {
  const pa = [1, 3, 5].map((i) => parseInt(a.slice(i, i + 2), 16));
  const pb = [1, 3, 5].map((i) => parseInt(b.slice(i, i + 2), 16));
  return `#${pa.map((v, i) => Math.round(v * (1 - t) + pb[i] * t).toString(16).padStart(2, '0')).join('')}`;
};
