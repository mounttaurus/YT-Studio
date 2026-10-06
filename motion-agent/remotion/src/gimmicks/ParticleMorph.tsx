// C1 点群の変形: 散らばり → 文字 → 球 … と、同じ数の点が形を渡り歩く（B10 粒子が文字になる）。
// 点群は Python（app/core/points.py）が作って渡す。ここは補間と投影と色だけ（計算しない）。
// 色 rainbow は、点ごとの色相を時間で回す＝LSD 的なきらめき。
import React from 'react';
import {AbsoluteFill, Easing, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {ParticleParams} from './types';

const DEPTH = 3.2; // 透視の距離
export const BACKDROP = '#030206'; // 背景のグラデーションの外周の色（PortalZoom の余白に使う）
const ease = Easing.inOut(Easing.cubic);

const RING_GAP = 30;
const RING_SPEED = 1.1; // px / フレーム
const Rings: React.FC<{frame: number; w: number; h: number}> = ({frame, w, h}) => {
  const max = Math.hypot(w, h) / 2;
  const n = Math.ceil(max / RING_GAP) + 1;
  const phase = (frame * RING_SPEED) % RING_GAP;
  return (
    <svg width={w} height={h} style={{position: 'absolute', opacity: 0.26}}>
      {Array.from({length: n}, (_, k) => {
        const r = k * RING_GAP + phase;
        // 中心で生まれて外で消える（端で急に現れたり消えたりしない）
        const o = Math.min(1, r / 120) * Math.max(0, 1 - r / max);
        return <circle key={k} cx={w / 2} cy={h / 2} r={r} fill="none" stroke="#b36bff" strokeWidth={2} opacity={o} />;
      })}
    </svg>
  );
};

export const ParticleMorph: React.FC<{params: ParticleParams}> = ({params}) => {
  const frame = useCurrentFrame();
  const {width: W, height: H} = useVideoConfig();
  const {stages, n, morph_frames: mf, colors, spin} = params;
  // 今の形と、ひとつ前の形
  let k = 0;
  for (let j = 0; j < stages.length; j++) if (stages[j].at <= frame) k = j;
  const cur = stages[k];
  const prev = stages[Math.max(0, k - 1)];
  const local = frame - cur.at;
  const unit = H * 0.42;
  const angle = frame * 0.02 * spin;
  const ca = Math.cos(angle);
  const sa = Math.sin(angle);

  const items: {x: number; y: number; z: number; i: number}[] = [];
  for (let i = 0; i < n; i++) {
    // 点ごとに少しずつ遅らせて、渦を巻くように渡る
    const delay = ((i * 7) % 29) / 29 * mf * 0.45;
    const t = k === 0 ? 1 : ease(Math.min(1, Math.max(0, (local - delay) / (mf * 0.6))));
    const a = prev.points[i];
    const b = cur.points[i];
    let x = a[0] + (b[0] - a[0]) * t;
    let y = a[1] + (b[1] - a[1]) * t;
    let z = a[2] + (b[2] - a[2]) * t;
    // 回転は「回る形」（球・散らばり）の時だけ効かせる
    const w = (prev.spin ? 1 - t : 0) + (cur.spin ? t : 0);
    const rx = x * ca + z * sa;
    const rz = -x * sa + z * ca;
    x = x + (rx - x) * w;
    z = z + (rz - z) * w;
    // 呼吸（ごく小さな揺らぎ）
    y += Math.sin(frame * 0.07 + i) * 0.006;
    items.push({x, y, z, i});
  }
  items.sort((p, q) => q.z - p.z); // 奥から描く

  const circles = items.map(({x, y, z, i}) => {
    const sc = DEPTH / (DEPTH + z);
    const px = W / 2 + x * unit * sc;
    const py = H / 2 + y * unit * sc;
    const r = Math.max(0.6, 3.1 * sc * (0.75 + 0.25 * Math.sin(frame * 0.3 + i)));
    const hue = (i * (1080 / n) + frame * 5) % 360;
    const fill = colors === 'rainbow' ? `hsl(${hue}, 95%, ${58 + 10 * (1 - sc)}%)` : '#f2efe6';
    return <circle key={i} cx={px} cy={py} r={r} fill={fill} opacity={Math.min(1, 0.55 + 0.45 * sc)} />;
  });

  const enter = interpolate(frame, [0, 10], [0, 1], {extrapolateRight: 'clamp'});
  return (
    // グラデーションは画面の端（中心から 540px＝縦の半分）までに外周の色へ揃える。
    // 揃っていないと、ポータルで入る途中に円の中で画面の四角が透けて見える
    <AbsoluteFill style={{background: `radial-gradient(circle 560px at 50% 50%, #26103f 0%, #0b0614 60%, ${BACKDROP} 100%)`}}>
      {/* 背景の同心円（D7 モアレの気配）。中心から外へ広がり続ける（回転では同心円は動いて見えない＝P8） */}
      <Rings frame={frame} w={W} h={H} />
      <AbsoluteFill style={{opacity: enter}}>
        {/* にじみ（同じ点をぼかして下に敷く） */}
        <svg width={W} height={H} style={{position: 'absolute', filter: 'blur(7px)', opacity: 0.55}}>
          {circles}
        </svg>
        <svg width={W} height={H} style={{position: 'absolute'}}>
          {circles}
        </svg>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
