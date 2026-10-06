// ④ アウト（白か黒へ溶ける）と ⑤ タイトルバック（アウトの色から現れ、ゆっくり意味深にズーム）。
import React from 'react';
import {AbsoluteFill, Easing, Img, interpolate, useCurrentFrame} from 'remotion';
import {Palette, SERIF, fitSize, resolveAsset, serifStack} from '../kit';

const outColor = (out: 'white' | 'black') => (out === 'white' ? '#ffffff' : '#000000');

export const B4Out: React.FC<{out: 'white' | 'black'; len: number}> = ({out, len}) => {
  const frame = useCurrentFrame();
  const o = interpolate(frame, [0, Math.min(7, len - 1)], [0, 1], {extrapolateRight: 'clamp', easing: Easing.in(Easing.quad)});
  const bloom = out === 'white' ? interpolate(frame, [0, 4, 8], [0, 0.9, 0], {extrapolateRight: 'clamp'}) : 0;
  return (
    <AbsoluteFill>
      <AbsoluteFill style={{background: 'radial-gradient(circle at 50% 50%, #ffffff 0%, #ffffff00 65%)', opacity: bloom}} />
      <AbsoluteFill style={{background: outColor(out), opacity: o}} />
    </AbsoluteFill>
  );
};

export const B5Title: React.FC<{
  title: {plate: string | null; main: string; sub: string};
  out: 'white' | 'black';
  pal: Palette;
  assetBase: string;
  len: number;
}> = ({title, out, pal, assetBase, len}) => {
  const frame = useCurrentFrame();
  const zoom = interpolate(frame, [0, len], [1.0, 1.1], {easing: Easing.out(Easing.sin)});
  const reveal = interpolate(frame, [0, 16], [1, 0], {extrapolateRight: 'clamp', easing: Easing.out(Easing.cubic)});
  const sweep = interpolate(frame, [10, len], [-60, 160], {extrapolateLeft: 'clamp'});
  const rule = interpolate(frame, [8, 30], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.out(Easing.cubic)});
  return (
    <AbsoluteFill style={{background: pal.title_bg}}>
      <AbsoluteFill style={{alignItems: 'center', justifyContent: 'center', transform: `scale(${zoom})`}}>
        {title.plate ? (
          <Img src={resolveAsset(title.plate, assetBase)} style={{maxWidth: 1500, maxHeight: 780, objectFit: 'contain'}} />
        ) : (
          <TextPlate main={title.main} sub={title.sub} color={pal.title_fg} rule={rule} />
        )}
      </AbsoluteFill>
      {/* 光の帯がゆっくり横切る */}
      <AbsoluteFill
        style={{
          background: `linear-gradient(105deg, #ffffff00 ${sweep - 12}%, #ffffff22 ${sweep}%, #ffffff00 ${sweep + 12}%)`,
          mixBlendMode: 'screen',
        }}
      />
      <AbsoluteFill style={{background: outColor(out), opacity: reveal}} />
    </AbsoluteFill>
  );
};

const TextPlate: React.FC<{main: string; sub: string; color: string; rule: number}> = ({main, sub, color, rule}) => {
  const mainSize = fitSize(main, 1400, 150, SERIF, 700, '0.14em');
  const subSize = Math.min(64, fitSize(sub || ' ', 1300, 64, SERIF, 700, '0.2em'));
  const line = (w: number) => (
    <div style={{display: 'flex', alignItems: 'center', gap: 18, width: `${w * rule}px`, overflow: 'hidden', justifyContent: 'center'}}>
      <div style={{flex: 1, height: 2, background: color, opacity: 0.8}} />
      <div style={{width: 12, height: 12, background: color, transform: 'rotate(45deg)'}} />
      <div style={{flex: 1, height: 2, background: color, opacity: 0.8}} />
    </div>
  );
  return (
    <div style={{display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 34, color}}>
      {line(1200)}
      <div style={{fontFamily: serifStack, fontWeight: 700, fontSize: mainSize, letterSpacing: '0.14em', whiteSpace: 'nowrap'}}>
        {main}
      </div>
      {sub ? (
        <div style={{fontFamily: serifStack, fontWeight: 700, fontSize: subSize, letterSpacing: '0.2em', whiteSpace: 'nowrap', opacity: 0.9}}>
          {sub}
        </div>
      ) : null}
      {line(1200)}
    </div>
  );
};
