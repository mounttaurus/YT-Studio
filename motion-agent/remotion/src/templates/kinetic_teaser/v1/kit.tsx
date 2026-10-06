// kinetic_teaser v1 の部品集（色・書体・素材の解決・文字の収め方・質感）。
// 版ごとに持つ（v2 を作っても v1 の見た目は変わらない）。
import React from 'react';
import {AbsoluteFill, staticFile, useCurrentFrame} from 'remotion';
import {fitText} from '@remotion/layout-utils';
import meta from './meta.json';

export type Palette = {
  label: string;
  base: string;
  accent: string;
  text: string;
  flow: string[][];
  title_bg: string;
  title_fg: string;
};

const PALETTES = meta.variants.palette.options as Record<string, Palette>;
export const palette = (name: string): Palette =>
  PALETTES[name] ?? PALETTES[meta.variants.palette.default];

export const SANS = 'Noto Sans CJK JP';
export const SERIF = 'Noto Serif CJK JP';
export const sansStack = `"${SANS}", "Noto Color Emoji", sans-serif`;
export const serifStack = `"${SERIF}", "${SANS}", serif`;

// 素材の参照: `static/...` は同梱の見本、それ以外は shared/ からの相対パス（asset_base に繋ぐ）
export const resolveAsset = (src: string, base: string): string =>
  src.startsWith('static/')
    ? staticFile(src.slice('static/'.length))
    : `${base}${src.split('/').map(encodeURIComponent).join('/')}`;

// 文字を幅に収める。大きすぎる時は上限で止める
export const fitSize = (
  text: string,
  withinWidth: number,
  max: number,
  fontFamily: string,
  fontWeight: number,
  letterSpacing?: string,
): number => {
  const {fontSize} = fitText({
    text,
    withinWidth,
    fontFamily,
    fontWeight: String(fontWeight),
    letterSpacing,
    validateFontIsLoaded: false,
  });
  return Math.max(24, Math.min(max, Math.floor(fontSize)));
};

// フィルムの粒子（feTurbulence の seed をフレームで回す＝決定的）
export const Grain: React.FC<{opacity?: number}> = ({opacity = 0.1}) => {
  const frame = useCurrentFrame();
  return (
    <AbsoluteFill style={{pointerEvents: 'none', mixBlendMode: 'overlay', opacity}}>
      <svg width="100%" height="100%">
        <filter id="kt-grain">
          <feTurbulence type="fractalNoise" baseFrequency="0.85" numOctaves={2} seed={frame % 12} stitchTiles="stitch" />
          <feColorMatrix type="saturate" values="0" />
        </filter>
        <rect width="100%" height="100%" filter="url(#kt-grain)" />
      </svg>
    </AbsoluteFill>
  );
};

export const Vignette: React.FC<{strength?: number}> = ({strength = 0.55}) => (
  <AbsoluteFill
    style={{
      pointerEvents: 'none',
      background: `radial-gradient(ellipse at center, rgba(0,0,0,0) 52%, rgba(0,0,0,${strength}) 100%)`,
    }}
  />
);

// 走査線（煽りの質感）
export const Scanlines: React.FC<{opacity?: number}> = ({opacity = 0.08}) => (
  <AbsoluteFill
    style={{
      pointerEvents: 'none',
      opacity,
      backgroundImage: 'repeating-linear-gradient(0deg, rgba(0,0,0,0.9) 0px, rgba(0,0,0,0.9) 2px, transparent 2px, transparent 5px)',
    }}
  />
);
