// B4 書体の描き替え: 同じ語を数フレームごとに別の書体・装飾で描き替える（混沌・陶酔・多面性）。
// 光過敏への配慮（カタログ §8）: 書体（形）は速く替えるが、明るさが大きく変わる装飾の切り替えは
// STYLE_EVERY ごと（毎秒3回未満）に抑える。背景は替えない。
import React from 'react';
import {AbsoluteFill, interpolate, random, useCurrentFrame} from 'remotion';
import {FontCycleParams} from './types';

type Deco = 'neon' | 'outline' | 'fill' | 'double' | 'split';
const DECOS: Deco[] = ['neon', 'outline', 'fill', 'double', 'split'];
const STYLE_EVERY = 12; // 装飾の切り替え（フレーム）＝30fps で毎秒2.5回
const COLORS = ['#ff2fa8', '#2ff3ff', '#d4ff2f', '#ff5a2f', '#b98cff', '#ffe14d'];
export const BACKDROP = '#050506';

// 同じ書体が続かない、決まった並び（seed 固定）
const order = (n: number, len: number, seed: string): number[] => {
  const out: number[] = [];
  let prev = -1;
  for (let i = 0; i < len; i++) {
    let k = Math.floor(random(`${seed}-${i}`) * n);
    if (k === prev) k = (k + 1) % n;
    out.push(k);
    prev = k;
  }
  return out;
};

export const FontCycle: React.FC<{params: FontCycleParams}> = ({params}) => {
  const frame = useCurrentFrame();
  const {text, sub, fonts, every} = params;
  const seq = order(fonts.length, 400, `fc-${text}`);
  const f = fonts[seq[Math.floor(frame / every) % seq.length]];
  const prevF = fonts[seq[Math.max(0, Math.floor(frame / every) - 1) % seq.length]];
  const si = Math.floor(frame / STYLE_EVERY);
  const deco = DECOS[Math.floor(random(`fc-deco-${si}`) * DECOS.length)];
  const color = COLORS[Math.floor(random(`fc-col-${si}`) * COLORS.length)];
  const color2 = COLORS[(COLORS.indexOf(color) + 2) % COLORS.length];
  const enter = interpolate(frame, [0, 8], [0, 1], {extrapolateRight: 'clamp'});
  const base: React.CSSProperties = {
    fontFamily: `"${f.family}", sans-serif`,
    fontWeight: f.weight,
    fontSize: 330,
    lineHeight: 1,
    whiteSpace: 'nowrap',
  };
  return (
    <AbsoluteFill style={{background: BACKDROP, alignItems: 'center', justifyContent: 'center'}}>
      {/* ひとつ前の書体の残像（白抜き） */}
      <div
        style={{
          ...base,
          fontFamily: `"${prevF.family}", sans-serif`,
          fontWeight: prevF.weight,
          position: 'absolute',
          color: 'transparent',
          WebkitTextStroke: `2px ${color2}`,
          opacity: 0.22,
          transform: 'scale(1.18)',
        }}
      >
        {text}
      </div>
      <div style={{opacity: enter, transform: `scale(${0.92 + 0.08 * enter})`}}>
        <Styled text={text} deco={deco} color={color} color2={color2} base={base} frame={frame} />
      </div>
      {sub ? (
        <div
          style={{
            position: 'absolute',
            bottom: 230,
            fontFamily: `"${fonts[seq[Math.floor(frame / (every * 4)) % seq.length]].family}", monospace`,
            fontSize: 38,
            letterSpacing: '0.5em',
            color: '#e8e6dd',
            opacity: 0.8 * enter,
          }}
        >
          {sub}
        </div>
      ) : null}
    </AbsoluteFill>
  );
};

const Styled: React.FC<{text: string; deco: Deco; color: string; color2: string; base: React.CSSProperties; frame: number}> = ({
  text,
  deco,
  color,
  color2,
  base,
  frame,
}) => {
  const flicker = 0.85 + 0.15 * random(`fc-flk-${Math.floor(frame / 2)}`);
  if (deco === 'neon') {
    return (
      <div
        style={{
          ...base,
          color: 'transparent',
          WebkitTextStroke: `6px ${color}`,
          filter: `drop-shadow(0 0 10px ${color}) drop-shadow(0 0 28px ${color})`,
          opacity: flicker,
        }}
      >
        {text}
      </div>
    );
  }
  if (deco === 'outline') {
    return <div style={{...base, color: 'transparent', WebkitTextStroke: `4px ${color}`}}>{text}</div>;
  }
  if (deco === 'double') {
    return (
      <div style={{position: 'relative'}}>
        <div style={{...base, position: 'absolute', left: 14, top: 14, color: color2, opacity: 0.85}}>{text}</div>
        <div style={{...base, position: 'relative', color: 'transparent', WebkitTextStroke: `5px ${color}`}}>{text}</div>
      </div>
    );
  }
  if (deco === 'split') {
    return (
      <div style={{...base, color: '#e9e6dc', textShadow: `-10px 0 ${color}, 10px 0 ${color2}`}}>
        {text}
      </div>
    );
  }
  return <div style={{...base, color, opacity: 0.92}}>{text}</div>;
};
