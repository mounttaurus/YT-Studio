// D5 HUD の枠（隅のカギ括弧・ラベル・REC・タイムコード）。監視・解析・機密資料の目線。
import React from 'react';
import {AbsoluteFill, useCurrentFrame, useVideoConfig} from 'remotion';
import fonts from './fonts.json';

const MONO = `"${fonts.fonts.mono.family}", monospace`;
const INK = 'rgba(235,235,225,0.72)';

const tc = (frame: number, fps: number) => {
  const s = Math.floor(frame / fps);
  const ff = frame % fps;
  const p = (v: number) => String(v).padStart(2, '0');
  return `${p(Math.floor(s / 3600))}:${p(Math.floor(s / 60) % 60)}:${p(s % 60)}:${p(ff)}`;
};

export const Hud: React.FC<{label?: string}> = ({label}) => {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  const inset = 56;
  const arm = 46;
  const corner = (left: boolean, top: boolean): React.CSSProperties => ({
    position: 'absolute',
    [left ? 'left' : 'right']: inset,
    [top ? 'top' : 'bottom']: inset,
    width: arm,
    height: arm,
    [`border${top ? 'Top' : 'Bottom'}`]: `2px solid ${INK}`,
    [`border${left ? 'Left' : 'Right'}`]: `2px solid ${INK}`,
  });
  const text: React.CSSProperties = {position: 'absolute', fontFamily: MONO, fontSize: 22, letterSpacing: '0.12em', color: INK};
  return (
    <AbsoluteFill style={{pointerEvents: 'none'}}>
      <div style={corner(true, true)} />
      <div style={corner(false, true)} />
      <div style={corner(true, false)} />
      <div style={corner(false, false)} />
      {label ? <div style={{...text, left: inset + arm + 18, top: inset + 6}}>{label}</div> : null}
      <div style={{...text, right: inset + arm + 18, top: inset + 6}}>
        <span style={{color: '#ff3030', opacity: Math.floor(frame / (fps / 2)) % 2 === 0 ? 1 : 0.15}}>●</span> REC
      </div>
      <div style={{...text, left: inset + arm + 18, bottom: inset + 6}}>{tc(frame, fps)}</div>
      <div style={{...text, right: inset + arm + 18, bottom: inset + 6}}>
        {width}×{height} {fps}P
      </div>
    </AbsoluteFill>
  );
};
