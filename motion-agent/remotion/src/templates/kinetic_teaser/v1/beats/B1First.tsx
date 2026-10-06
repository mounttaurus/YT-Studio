// ① 最初のキーワード: 文字が飛び込み、ゆっくり印象的にズームする。
// 種類ごとに見せ方を変える: word＝叩きつけ＋色ずれ／date＝数字が1字ずつ落ちて下線が走る／place＝字間が締まる＋ピン
import React from 'react';
import {AbsoluteFill, interpolate, random, spring, useCurrentFrame, useVideoConfig, Easing} from 'remotion';
import {FirstItem} from '../types';
import {Palette, fitSize, sansStack, SANS} from '../kit';

const SIZE_SINGLE = 300;
const SIZE_LEAD = 220;
const SIZE_FOLLOW = 140;
const STAGGER = 9; // 項目どうしの出の間隔（フレーム）
// 文字を収める幅。ゆっくりズーム（最大 ZOOM 倍）しても安全域（画面幅の90%＝1728px）に収まるように
const ZOOM = 1.09;
const SAFE_W = 1728;
const FIT_W = Math.floor(SAFE_W / ZOOM); // 1585

export const B1First: React.FC<{items: FirstItem[]; pal: Palette; len: number}> = ({items, pal, len}) => {
  const frame = useCurrentFrame();
  const zoom = interpolate(frame, [0, len], [1, ZOOM], {extrapolateRight: 'clamp', easing: Easing.out(Easing.quad)});
  return (
    <AbsoluteFill style={{background: pal.base}}>
      <AbsoluteFill
        style={{background: `radial-gradient(circle at 50% 46%, ${pal.accent}2e 0%, ${pal.base}00 58%)`}}
      />
      <AbsoluteFill style={{alignItems: 'center', justifyContent: 'center', transform: `scale(${zoom})`}}>
        <div style={{display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 30}}>
          {items.map((it, i) => {
            const max = items.length === 1 ? SIZE_SINGLE : i === 0 ? SIZE_LEAD : SIZE_FOLLOW;
            const props = {item: it, index: i, pal, max, delay: i * STAGGER};
            if (it.kind === 'date') return <DateLine key={i} {...props} />;
            if (it.kind === 'place') return <PlaceLine key={i} {...props} />;
            return <WordLine key={i} {...props} />;
          })}
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

type LineProps = {item: FirstItem; index: number; pal: Palette; max: number; delay: number};

const WordLine: React.FC<LineProps> = ({item, index, pal, max, delay}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const local = frame - delay;
  const s = spring({frame: local, fps, config: {damping: 14, stiffness: 230, mass: 0.6}});
  const scale = interpolate(s, [0, 1], [2.7, 1]);
  const split = (1 - s) * 34 + 3; // 色ずれ（叩きつけの直後は大きく、残りは少し残す）
  const hit = local >= 2 && local <= 9 ? (9 - local) / 7 : 0; // 着地の揺れ
  const dx = (random(`b1-shake-x-${index}-${frame}`) - 0.5) * 22 * hit;
  const dy = (random(`b1-shake-y-${index}-${frame}`) - 0.5) * 14 * hit;
  const size = fitSize(item.text, FIT_W, max, SANS, 900, '0.04em');
  return (
    <div
      style={{
        fontFamily: sansStack,
        fontWeight: 900,
        fontSize: size,
        letterSpacing: '0.04em',
        lineHeight: 1.05,
        color: pal.text,
        opacity: Math.min(1, s * 3),
        transform: `translate(${dx}px, ${dy}px) scale(${scale})`,
        textShadow: `${-split}px 0 ${pal.accent}, ${split}px 0 #19d3ff`,
        whiteSpace: 'nowrap',
      }}
    >
      {item.text}
    </div>
  );
};

const DateLine: React.FC<LineProps> = ({item, pal, max, delay}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const chars = Array.from(item.text);
  const size = fitSize(item.text, FIT_W, max, SANS, 900, '0.02em');
  const ruleStart = delay + chars.length * 2 + 2;
  const rule = interpolate(frame, [ruleStart, ruleStart + 12], [0, 100], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.out(Easing.cubic),
  });
  return (
    <div style={{display: 'flex', flexDirection: 'column', alignItems: 'center'}}>
      <div
        style={{
          display: 'flex',
          fontFamily: sansStack,
          fontWeight: 900,
          fontSize: size,
          letterSpacing: '0.02em',
          fontVariantNumeric: 'tabular-nums',
          color: pal.text,
          whiteSpace: 'nowrap',
        }}
      >
        {chars.map((c, j) => {
          const s = spring({frame: frame - delay - j * 2, fps, config: {damping: 15, stiffness: 280, mass: 0.5}});
          return (
            <span key={j} style={{display: 'inline-block', opacity: s, transform: `translateY(${(1 - s) * -70}px)`}}>
              {c}
            </span>
          );
        })}
      </div>
      <div style={{width: `${rule}%`, height: Math.max(6, size * 0.045), background: pal.accent, marginTop: 6}} />
    </div>
  );
};

const PlaceLine: React.FC<LineProps> = ({item, pal, max, delay}) => {
  const frame = useCurrentFrame();
  const local = frame - delay;
  const t = interpolate(local, [0, 20], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.out(Easing.cubic),
  });
  const tracking = interpolate(t, [0, 1], [0.7, 0.18]);
  const size = fitSize(item.text, FIT_W - 200, max, SANS, 700, '0.18em');
  const pin = size * 0.8;
  return (
    <div style={{display: 'flex', alignItems: 'center', gap: size * 0.25, opacity: t}}>
      <svg width={pin * 0.7} height={pin} viewBox="0 0 24 34" style={{transform: `translateY(${(1 - t) * -30}px)`}}>
        <path d="M12 0C5.4 0 0 5.4 0 12c0 9 12 22 12 22s12-13 12-22C24 5.4 18.6 0 12 0z" fill={pal.accent} />
        <circle cx="12" cy="12" r="4.5" fill={pal.base} />
      </svg>
      <div
        style={{
          fontFamily: sansStack,
          fontWeight: 700,
          fontSize: size,
          letterSpacing: `${tracking}em`,
          color: pal.text,
          whiteSpace: 'nowrap',
          borderBottom: `${Math.max(3, size * 0.03)}px solid ${pal.text}55`,
          paddingBottom: size * 0.06,
        }}
      >
        {item.text}
      </div>
    </div>
  );
};
