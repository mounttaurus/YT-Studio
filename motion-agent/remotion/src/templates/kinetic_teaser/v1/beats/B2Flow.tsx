// ② キーワードの流れ: ナレーションの区切り（switches）ごとに語が切り替わる。
// 背景の色が次々に変わって煽る。出方は4種類を順に回す（叩きつけ／拭き取り／1字ずつ／上下割り）。
import React from 'react';
import {AbsoluteFill, Easing, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {Palette, fitSize, sansStack, SANS} from '../kit';

type Style = 'slam' | 'wipe' | 'stagger' | 'split';
const STYLES: Style[] = ['slam', 'wipe', 'stagger', 'split'];
const MAX_SIZE = 250;

export const B2Flow: React.FC<{words: string[]; switches: number[]; pal: Palette; len: number}> = ({
  words,
  switches,
  pal,
  len,
}) => {
  const frame = useCurrentFrame();
  let i = 0;
  for (let k = 0; k < switches.length; k++) if (switches[k] <= frame) i = k;
  const start = switches[i] ?? 0;
  const end = switches[i + 1] ?? len;
  const local = frame - start;
  const [bg, fg] = pal.flow[i % pal.flow.length];
  const word = words[i] ?? '';
  const size = fitSize(word, 1500, MAX_SIZE, SANS, 900, '0.03em');
  const push = 1 + Math.min(local, end - start) * 0.0016; // 語が出た後もじわじわ寄る
  // 切り替えの瞬間に文字色で一瞬光る（反転＝difference は赤背景で濁った青緑になったのでやめた）
  const flash = local < 3 ? (3 - local) / 3 : 0;
  return (
    <AbsoluteFill style={{background: bg, overflow: 'hidden'}}>
      <Marquee word={word} color={fg} frame={frame} len={len} />
      <Stripe color={fg} frame={frame} index={i} />
      <AbsoluteFill style={{alignItems: 'center', justifyContent: 'center', transform: `scale(${push})`}}>
        <Word word={word} style={STYLES[i % STYLES.length]} local={local} size={size} color={fg} />
      </AbsoluteFill>
      <Counter index={i} total={words.length} color={fg} />
      <AbsoluteFill style={{background: fg, opacity: flash * 0.9}} />
    </AbsoluteFill>
  );
};

const Word: React.FC<{word: string; style: Style; local: number; size: number; color: string}> = ({
  word,
  style,
  local,
  size,
  color,
}) => {
  const {fps} = useVideoConfig();
  const base: React.CSSProperties = {
    fontFamily: sansStack,
    fontWeight: 900,
    fontSize: size,
    letterSpacing: '0.03em',
    color,
    whiteSpace: 'nowrap',
    lineHeight: 1.1,
  };
  const ease = (a: number, b: number) =>
    interpolate(local, [a, b], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.out(Easing.cubic)});

  if (style === 'slam') {
    const s = spring({frame: local, fps, config: {damping: 12, stiffness: 250, mass: 0.6}});
    return <div style={{...base, opacity: Math.min(1, s * 2.5), transform: `scale(${interpolate(s, [0, 1], [1.9, 1])})`}}>{word}</div>;
  }
  if (style === 'wipe') {
    const p = ease(0, 9);
    return (
      <div style={{...base, clipPath: `inset(0 ${(1 - p) * 100}% 0 0)`, transform: `translateX(${(1 - p) * -80}px)`}}>
        {word}
      </div>
    );
  }
  if (style === 'stagger') {
    return (
      <div style={{...base, display: 'flex'}}>
        {Array.from(word).map((c, j) => {
          const s = spring({frame: local - j * 2, fps, config: {damping: 14, stiffness: 260, mass: 0.5}});
          return (
            <span key={j} style={{display: 'inline-block', opacity: s, transform: `translateY(${(1 - s) * 90}px) rotate(${(1 - s) * 12}deg)`}}>
              {c}
            </span>
          );
        })}
      </div>
    );
  }
  // split: 上半分は左から、下半分は右から滑り込む
  const p = ease(0, 10);
  const half = (top: boolean): React.CSSProperties => ({
    ...base,
    position: 'absolute',
    clipPath: top ? 'inset(0 0 50% 0)' : 'inset(50% 0 0 0)',
    transform: `translateX(${(1 - p) * (top ? -420 : 420)}px)`,
  });
  return (
    <div style={{position: 'relative'}}>
      <div style={{...base, visibility: 'hidden'}}>{word}</div>
      <div style={{...half(true), top: 0, left: 0}}>{word}</div>
      <div style={{...half(false), top: 0, left: 0}}>{word}</div>
    </div>
  );
};

// 背景の巨大な白抜き文字が横に流れ続ける（煽り）
const Marquee: React.FC<{word: string; color: string; frame: number; len: number}> = ({word, color, frame, len}) => {
  const x = interpolate(frame, [0, len], [0, -1400]);
  const text = `${word}　`.repeat(6);
  return (
    <AbsoluteFill style={{justifyContent: 'center', overflow: 'hidden', opacity: 0.13}}>
      <div
        style={{
          fontFamily: sansStack,
          fontWeight: 900,
          fontSize: 520,
          whiteSpace: 'nowrap',
          color: 'transparent',
          WebkitTextStroke: `4px ${color}`,
          transform: `translateX(${x}px)`,
          lineHeight: 1,
        }}
      >
        {text}
      </div>
    </AbsoluteFill>
  );
};

// 斜めの帯が切り替えごとに角度を変えて走る
const Stripe: React.FC<{color: string; frame: number; index: number}> = ({color, frame, index}) => {
  const angle = index % 2 === 0 ? -14 : 12;
  const y = interpolate(frame % 90, [0, 90], [-200, 200]);
  return (
    <AbsoluteFill style={{alignItems: 'center', justifyContent: 'center', pointerEvents: 'none'}}>
      <div style={{width: 3200, height: 120, background: color, opacity: 0.08, transform: `rotate(${angle}deg) translateY(${y}px)`}} />
    </AbsoluteFill>
  );
};

const Counter: React.FC<{index: number; total: number; color: string}> = ({index, total, color}) => (
  <div
    style={{
      position: 'absolute',
      left: 96,
      top: 80,
      fontFamily: sansStack,
      fontWeight: 700,
      fontSize: 34,
      letterSpacing: '0.2em',
      color,
      opacity: 0.75,
      fontVariantNumeric: 'tabular-nums',
    }}
  >
    {String(index + 1).padStart(2, '0')} / {String(total).padStart(2, '0')}
    <div style={{width: 120, height: 4, background: color, marginTop: 10}} />
  </div>
);
