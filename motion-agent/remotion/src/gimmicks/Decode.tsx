// B3 デコード: 化けた文字が1字ずつ正しい字に収まる（暗号・機密・解読）。最後に「極秘」の判子（E1 のモチーフ候補）。
import React from 'react';
import {AbsoluteFill, Easing, interpolate, random, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {DecodeParams} from './types';

// 化けている間に回す文字（カタカナ・英数字・記号）。逆スラッシュは入れない
const POOL = 'アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲン0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ#$%&*+=<>/|';
const SCRAMBLE = 14; // 1字が化けている長さ（フレーム）
const STEP = 2; // 次の字が化け始めるまで（フレーム）
const LINE_GAP = 10; // 行どうしの遅れ（フレーム）
const ACCENT = '#ff4040';
const INK = '#ece9e0';
export const BACKDROP = '#07080a';

export const Decode: React.FC<{params: DecodeParams}> = ({params}) => {
  const frame = useCurrentFrame();
  // ショットの 55% までに全行を解読し終える速さ（字の間隔）を逆算する。判子はその後に押す
  const total = params.lines.reduce((a, l) => a + Array.from(l.text).length, 0);
  const budget = params.len * 0.55 - SCRAMBLE - 6 - (params.lines.length - 1) * LINE_GAP;
  const step = Math.max(0.6, Math.min(STEP, budget / Math.max(1, total * 0.75)));
  let doneAt = 0;
  let before = 0;
  const lines = params.lines.map((ln, li) => {
    const chars = Array.from(ln.text);
    const base = 6 + li * LINE_GAP + before * step * 0.5; // 次の行は前の行の途中から化け始める
    before += chars.length;
    doneAt = Math.max(doneAt, base + chars.length * step + SCRAMBLE);
    return {ln, chars, base};
  });
  return (
    <AbsoluteFill style={{background: BACKDROP, justifyContent: 'center', paddingLeft: 170}}>
      <AbsoluteFill
        style={{
          opacity: 0.08,
          backgroundImage: 'repeating-linear-gradient(0deg, #fff 0px, #fff 1px, transparent 1px, transparent 4px)',
        }}
      />
      <div style={{display: 'flex', flexDirection: 'column', gap: 18}}>
        {lines.map(({ln, chars, base}, li) => (
          <div
            key={li}
            style={{
              display: 'flex',
              fontFamily: `"${ln.font.family}", monospace`,
              fontWeight: ln.font.weight,
              fontSize: ln.size,
              lineHeight: 1.15,
              letterSpacing: '0.06em',
              whiteSpace: 'pre',
            }}
          >
            {chars.map((c, j) => {
              const start = base + j * step;
              const settle = start + SCRAMBLE;
              if (frame < start) return <span key={j} style={{opacity: 0}}>{c}</span>;
              if (frame < settle && c !== ' ') {
                const g = POOL[Math.floor(random(`dec-${li}-${j}-${Math.floor(frame / 2)}`) * POOL.length)];
                return (
                  <span key={j} style={{color: ACCENT, opacity: 0.85}}>
                    {g}
                  </span>
                );
              }
              const flash = frame - settle < 3 ? 1 - (frame - settle) / 3 : 0;
              return (
                <span key={j} style={{color: INK, textShadow: flash ? `0 0 ${18 * flash}px #fff` : 'none'}}>
                  {c}
                </span>
              );
            })}
            <Cursor visible={frame >= base && frame < doneAt + 20 && li === lines.length - 1} size={ln.size} />
          </div>
        ))}
      </div>
      {params.stamp ? <Stamp text={params.stamp.text} family={params.stamp.font.family} weight={params.stamp.font.weight} at={doneAt + 6} /> : null}
    </AbsoluteFill>
  );
};

const Cursor: React.FC<{visible: boolean; size: number}> = ({visible, size}) => {
  const frame = useCurrentFrame();
  if (!visible) return null;
  return <span style={{width: size * 0.55, background: ACCENT, opacity: Math.floor(frame / 8) % 2 ? 0 : 0.9, marginLeft: 8}} />;
};

// 「極秘」の判子: 斜めに叩きつけて止まる。インクのかすれは feTurbulence の変位（seed 固定＝決定的）
const Stamp: React.FC<{text: string; family: string; weight: number; at: number}> = ({text, family, weight, at}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  if (frame < at) return null;
  const s = spring({frame: frame - at, fps, config: {damping: 13, stiffness: 320, mass: 0.7}});
  const scale = interpolate(s, [0, 1], [2.4, 1]);
  const shake = frame - at < 6 ? (random(`stamp-${frame}`) - 0.5) * 10 : 0;
  const ink = interpolate(frame - at, [0, 4], [0, 1], {extrapolateRight: 'clamp', easing: Easing.out(Easing.quad)});
  return (
    <AbsoluteFill style={{alignItems: 'flex-end', justifyContent: 'flex-end', padding: '0 190px 170px 0'}}>
      <svg width={0} height={0}>
        <filter id="stamp-ink">
          <feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves={2} seed={7} result="n" />
          <feDisplacementMap in="SourceGraphic" in2="n" scale={5} />
        </filter>
      </svg>
      <div
        style={{
          transform: `translate(${shake}px, ${shake * 0.6}px) rotate(-11deg) scale(${scale})`,
          opacity: ink * 0.92,
          border: '9px solid #d5281e',
          borderRadius: 10,
          padding: '6px 26px 10px',
          color: '#d5281e',
          fontFamily: `"${family}", serif`,
          fontWeight: weight,
          fontSize: 132,
          lineHeight: 1,
          letterSpacing: '0.08em',
          filter: 'url(#stamp-ink)',
          mixBlendMode: 'screen',
        }}
      >
        {text}
      </div>
    </AbsoluteFill>
  );
};
