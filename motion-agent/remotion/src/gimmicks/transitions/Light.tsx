// A3 光に飛び込んで白、白が次の画面になる。点（x, y）から光が広がって画面が真っ白になり（前半）、
// 白が引くと次の場面（後半）。切れ目を光で隠す。白への往復で明るさが2回変わる（台帳 flash: 2）。
import React from 'react';
import {AbsoluteFill, Easing, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';

export const LightTransition: React.FC<{
  x: number;
  y: number;
  len: number;
  outer: React.ReactNode;
  inner: React.ReactNode;
}> = ({x, y, len, outer, inner}) => {
  const frame = useCurrentFrame();
  const {width: W, height: H} = useVideoConfig();
  const e = interpolate(frame, [0, Math.max(1, len - 1)], [0, 1], {extrapolateRight: 'clamp'});
  const cover = Math.hypot(W, H);
  const grow = Easing.in(Easing.cubic)(Math.min(1, e / 0.5));
  const R = cover * 1.2 * grow + 40;
  const fade = interpolate(e, [0.5, 1], [1, 0], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.out(Easing.quad)});
  const first = e < 0.5;
  return (
    <AbsoluteFill style={{background: '#000'}}>
      {first ? outer : inner}
      {first ? (
        <AbsoluteFill
          style={{
            background: `radial-gradient(circle at ${x}px ${y}px, rgba(255,255,255,1) 0px, rgba(255,255,255,1) ${R * 0.55}px, rgba(255,255,255,0) ${R}px)`,
          }}
        />
      ) : (
        <AbsoluteFill style={{background: '#ffffff', opacity: fade}} />
      )}
    </AbsoluteFill>
  );
};
