// A1 ポータルズーム: 外の場面の1点（x, y）へ寄っていくと、その点の中に次の場面があり、そのまま入る。
// 倍率は対数で動かす（等速に寄って見える）。終わった時、内側の場面はちょうど等倍・画面中央になる。
//
//   s = exp(ln(S) * e)          S = 画面を覆う半径 / 入口の半径（入口が画面を覆う倍率）
//   c = T + (C - T) * e          入口の中心は画面中央へ寄っていく
//   外: 点 T が c に来るように拡大   内: 中心 C が c に来るように k = s / S で拡大・半径 r0*s の円で切り抜く
import React from 'react';
import {AbsoluteFill, Easing, interpolate, random, useCurrentFrame, useVideoConfig} from 'remotion';

export const PortalZoom: React.FC<{
  x: number;
  y: number;
  r: number;
  len: number;
  outer: React.ReactNode;
  inner: React.ReactNode;
  // 内側の場面の外周の色。内側の画面（16:9）は入口の円より小さいので、円の中の余白をこれで塗る
  // （塗らないと円の中に四角い枠が見える）。各ギミックの BACKDROP を渡す
  innerBackdrop?: string;
}> = ({x, y, r, len, outer, inner, innerBackdrop = '#000'}) => {
  const frame = useCurrentFrame();
  const {width: W, height: H} = useVideoConfig();
  const e = interpolate(frame, [0, len - 1], [0, 1], {
    extrapolateRight: 'clamp',
    easing: Easing.inOut(Easing.cubic),
  });
  const cover = Math.hypot(W / 2, H / 2);
  const S = cover / r;
  const s = Math.exp(Math.log(S) * e);
  const cx = x + (W / 2 - x) * e;
  const cy = y + (H / 2 - y) * e;
  const radius = r * s;
  const k = s / S;
  return (
    <AbsoluteFill style={{background: '#000', overflow: 'hidden'}}>
      {/* 外の場面。入りきる直前に消す（入口の点の光が巨大に拡大されて画面の端をかすませるため） */}
      <AbsoluteFill
        style={{
          transformOrigin: '0 0',
          transform: `translate(${cx - x * s}px, ${cy - y * s}px) scale(${s})`,
          opacity: interpolate(e, [0.72, 0.95], [1, 0], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'}),
        }}
      >
        {outer}
      </AbsoluteFill>
      <SpeedLines cx={cx} cy={cy} strength={Math.sin(Math.PI * e)} frame={frame} />
      <AbsoluteFill style={{clipPath: `circle(${radius}px at ${cx}px ${cy}px)`, background: innerBackdrop}}>
        <AbsoluteFill
          style={{transformOrigin: '0 0', transform: `translate(${cx - (W / 2) * k}px, ${cy - (H / 2) * k}px) scale(${k})`}}
        >
          {inner}
        </AbsoluteFill>
      </AbsoluteFill>
      {/* 入口の縁の虹色の輪（入りきったら消える） */}
      <AbsoluteFill style={{pointerEvents: 'none', opacity: 1 - e}}>
        <div
          style={{
            position: 'absolute',
            left: cx - radius - 6,
            top: cy - radius - 6,
            width: (radius + 6) * 2,
            height: (radius + 6) * 2,
            borderRadius: '50%',
            background: 'conic-gradient(#ff3b6b, #ffb340, #f6ff4d, #43ff8a, #3bd0ff, #7a5cff, #ff3bd4, #ff3b6b)',
            WebkitMask: `radial-gradient(circle, transparent ${radius}px, #000 ${radius + 1}px)`,
            mask: `radial-gradient(circle, transparent ${radius}px, #000 ${radius + 1}px)`,
            filter: 'blur(1px)',
          }}
        />
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

// D6 集中線（寄っている間だけ）
const SpeedLines: React.FC<{cx: number; cy: number; strength: number; frame: number}> = ({cx, cy, strength, frame}) => {
  if (strength < 0.02) return null;
  const n = 72;
  return (
    <AbsoluteFill style={{pointerEvents: 'none', opacity: 0.35 * strength}}>
      <svg width="100%" height="100%">
        {Array.from({length: n}, (_, i) => {
          const a = (i / n) * Math.PI * 2 + random(`sl-a-${i}`) * 0.08;
          const r1 = 260 + random(`sl-r-${i}-${Math.floor(frame / 2)}`) * 300;
          const r2 = 1400;
          return (
            <line
              key={i}
              x1={cx + Math.cos(a) * r1}
              y1={cy + Math.sin(a) * r1}
              x2={cx + Math.cos(a) * r2}
              y2={cy + Math.sin(a) * r2}
              stroke="#ffffff"
              strokeWidth={1 + random(`sl-w-${i}`) * 3}
            />
          );
        })}
      </svg>
    </AbsoluteFill>
  );
};

// ポータルの入口になる点（外の場面に置く）。寄る少し前に現れて脈打つ
export const PortalMark: React.FC<{x: number; y: number; r: number; appearAt: number}> = ({x, y, r, appearAt}) => {
  const frame = useCurrentFrame();
  const t = interpolate(frame, [appearAt, appearAt + 10], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.out(Easing.back(2))});
  if (t <= 0) return null;
  const pulse = 1 + 0.15 * Math.sin((frame - appearAt) * 0.5);
  const rr = r * t * pulse;
  return (
    <div
      style={{
        position: 'absolute',
        left: x - rr,
        top: y - rr,
        width: rr * 2,
        height: rr * 2,
        borderRadius: '50%',
        background: `conic-gradient(from ${frame * 12}deg, #ff3b6b, #ffb340, #f6ff4d, #43ff8a, #3bd0ff, #7a5cff, #ff3bd4, #ff3b6b)`,
        boxShadow: `0 0 ${rr * 1.5}px ${rr * 0.4}px rgba(255,255,255,0.35)`,
      }}
    />
  );
};
