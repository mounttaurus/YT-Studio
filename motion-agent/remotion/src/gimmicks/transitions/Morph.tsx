// 形の受け渡し（文字⇄点群）の重なりの区間。点群の側は Python が端に字形の形を足してあるので、
// ここは前の場面を溶かしながら次の場面を出すだけ（位置のずれは溶かしで隠す）。
import React from 'react';
import {AbsoluteFill, Easing, interpolate, useCurrentFrame} from 'remotion';

export const MorphTransition: React.FC<{len: number; outer: React.ReactNode; inner: React.ReactNode}> = ({len, outer, inner}) => {
  const frame = useCurrentFrame();
  const e = interpolate(frame, [0, Math.max(1, len - 1)], [0, 1], {extrapolateRight: 'clamp', easing: Easing.inOut(Easing.quad)});
  return (
    <AbsoluteFill style={{background: '#000'}}>
      <AbsoluteFill style={{opacity: 1 - e}}>{outer}</AbsoluteFill>
      <AbsoluteFill style={{opacity: e}}>{inner}</AbsoluteFill>
    </AbsoluteFill>
  );
};
