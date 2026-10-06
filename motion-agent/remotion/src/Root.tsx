// 型の登録。型を足したら、ここに Composition を1つ足す（id は meta.json の composition_id）。
// defaultProps は見本（samples/*.props.json＝Python が作る）。Remotion Studio で開いた時の絵になる。
import React from 'react';
import {Composition} from 'remotion';
import {KineticTeaser, calculateMetadata as ktMeta} from './templates/kinetic_teaser/v1/Composition';
import {KineticTeaserProps} from './templates/kinetic_teaser/v1/types';
import ktMetaJson from './templates/kinetic_teaser/v1/meta.json';
import ktSample from './templates/kinetic_teaser/v1/samples/date-place.props.json';
import {KineticTeaserV2, calculateMetadataV2} from './templates/kinetic_teaser/v2/Composition';
import {PlanProps} from './templates/kinetic_teaser/v2/types';
import ktV2MetaJson from './templates/kinetic_teaser/v2/meta.json';
import ktV2Sample from './templates/kinetic_teaser/v2/samples/default.props.json';
import {GimmickLab, calculateLabMetadata} from './gimmicks/Lab';
import {LabProps} from './gimmicks/types';
import labMetaJson from './templates/gimmick_lab/v1/meta.json';
import labSample from './templates/gimmick_lab/v1/samples/mkultra.props.json';

const kt = ktSample as unknown as KineticTeaserProps;
const lab = labSample as unknown as LabProps;
const ktV2 = ktV2Sample as unknown as PlanProps;

export const Root: React.FC = () => (
  <>
    <Composition
      id={ktMetaJson.composition_id}
      component={KineticTeaser}
      width={ktMetaJson.width}
      height={ktMetaJson.height}
      fps={ktMetaJson.fps}
      durationInFrames={kt.timing.total_frames}
      defaultProps={kt}
      calculateMetadata={ktMeta}
    />
    <Composition
      id={ktV2MetaJson.composition_id}
      component={KineticTeaserV2}
      width={ktV2MetaJson.width}
      height={ktV2MetaJson.height}
      fps={ktV2MetaJson.fps}
      durationInFrames={ktV2.total_frames}
      defaultProps={ktV2}
      calculateMetadata={calculateMetadataV2}
    />
    <Composition
      id={labMetaJson.composition_id}
      component={GimmickLab}
      width={labMetaJson.width}
      height={labMetaJson.height}
      fps={labMetaJson.fps}
      durationInFrames={lab.total_frames}
      defaultProps={lab}
      calculateMetadata={calculateLabMetadata}
    />
  </>
);
