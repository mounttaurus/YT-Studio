// D3 映像の質感（粒子・走査線・周辺減光）。全編に重ねて「画面の全てが動く」（P8）を底上げする。
// 粒子は feTurbulence の seed をフレームで回す＝決定的。
import React from 'react';
import {AbsoluteFill, useCurrentFrame} from 'remotion';

export type TextureOpts = {grain?: boolean; scanlines?: boolean; vignette?: boolean};

export const Texture: React.FC<TextureOpts> = ({grain = true, scanlines = true, vignette = true}) => {
  const frame = useCurrentFrame();
  return (
    <AbsoluteFill style={{pointerEvents: 'none'}}>
      {grain ? (
        <AbsoluteFill style={{mixBlendMode: 'overlay', opacity: 0.14}}>
          <svg width="100%" height="100%">
            <filter id="tx-grain">
              <feTurbulence type="fractalNoise" baseFrequency="0.85" numOctaves={2} seed={frame % 24} stitchTiles="stitch" />
              <feColorMatrix type="saturate" values="0" />
            </filter>
            <rect width="100%" height="100%" filter="url(#tx-grain)" />
          </svg>
        </AbsoluteFill>
      ) : null}
      {scanlines ? (
        <AbsoluteFill
          style={{
            opacity: 0.07,
            // 走査線はゆっくり流れる（止まった縞にしない）
            backgroundImage: 'repeating-linear-gradient(0deg, #000 0px, #000 2px, transparent 2px, transparent 5px)',
            backgroundPosition: `0 ${(frame * 0.5) % 5}px`,
          }}
        />
      ) : null}
      {vignette ? (
        <AbsoluteFill style={{background: 'radial-gradient(ellipse at center, rgba(0,0,0,0) 55%, rgba(0,0,0,0.5) 100%)'}} />
      ) : null}
    </AbsoluteFill>
  );
};
