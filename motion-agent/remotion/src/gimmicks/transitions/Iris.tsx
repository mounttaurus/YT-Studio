// A4 アイリス: 点（x, y）から円が開いて次の場面が見える。縁は網点（ハーフトーン）。
// 杜若（かきつばた）はアイリスの花＝チャンネルの決まり手の候補。
import React from 'react';
import {AbsoluteFill, Easing, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';

const BAND = 70; // 網点の縁の幅

export const IrisTransition: React.FC<{
  x: number;
  y: number;
  len: number;
  outer: React.ReactNode;
  inner: React.ReactNode;
  edge?: string;
}> = ({x, y, len, outer, inner, edge = '#f4f1ea'}) => {
  const frame = useCurrentFrame();
  const {width: W, height: H} = useVideoConfig();
  const e = interpolate(frame, [0, Math.max(1, len - 1)], [0, 1], {extrapolateRight: 'clamp', easing: Easing.inOut(Easing.cubic)});
  const cover = Math.hypot(Math.max(x, W - x), Math.max(y, H - y)) + BAND;
  const R = cover * e;
  const size = (R + 4) * 2;
  const mask = `radial-gradient(circle, transparent ${Math.max(0, R - BAND)}px, #000 ${Math.max(1, R - BAND + 1)}px, #000 ${R}px, transparent ${R + 1}px)`;
  return (
    <AbsoluteFill style={{background: '#000', overflow: 'hidden'}}>
      {outer}
      <AbsoluteFill style={{clipPath: `circle(${R}px at ${x}px ${y}px)`}}>{inner}</AbsoluteFill>
      {R > 2 ? (
        <div
          style={{
            position: 'absolute',
            left: x - size / 2,
            top: y - size / 2,
            width: size,
            height: size,
            opacity: 1 - Math.pow(e, 3),
            background: `radial-gradient(circle, ${edge} 34%, transparent 38%) 0 0 / 16px 16px`,
            WebkitMask: mask,
            mask,
          }}
        />
      ) : null}
    </AbsoluteFill>
  );
};
