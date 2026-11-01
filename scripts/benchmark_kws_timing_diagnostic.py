#!/usr/bin/env python3
"""Attribute quiet-gate timing using three supplied recordings and fixed perturbations.

No thresholds or gate constants are tuned. Cropped ungated controls start at
exactly the stream origin reported by the gated detector, isolating removed
context from gate computation. Variants are not independent speech examples.
"""
import argparse
import array
from datetime import datetime, timezone
import json
from pathlib import Path
import wave

from benchmark_kws import ROOT, digest, probe, silence
from benchmark_kws_gate import GATE, verify_baseline


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('config','fixture','model-dir','baseline-dir','work-dir','output'):
        p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--binary',type=Path,default=ROOT/'native/kws-probe/target/release/jarvis-kws-probe')
    a=p.parse_args()
    if a.output.exists() or a.work_dir.exists(): p.error('Use new work and evidence paths')
    a.work_dir.mkdir(parents=True)
    snapshot=verify_baseline(a.baseline_dir)
    config=json.loads(a.config.read_text())
    config.pop('quiet_gate',None)
    sources=[('go-forward',a.fixture.read_bytes(),'GO_FORWARD','surrogate')]
    for n,label in [(0,'LIGHT_UP'),(1,'LOVELY_CHILD')]:
        with wave.open(str(a.model_dir/'test_wavs'/f'{n}.wav')) as wav:
            assert (wav.getframerate(),wav.getnchannels(),wav.getsampwidth())==(16000,1,2)
            sources.append((f'published-{n}',wav.readframes(wav.getnframes()),label,'published'))
    data=dict(schema_version=1,recorded_at=datetime.now(timezone.utc).isoformat(),scope=__doc__,
              baseline_snapshot=snapshot,binary_sha256=digest(a.binary),configuration=config,
              model_and_library={k:dict(path=v,sha256=digest(v)) for k,v in config.items() if isinstance(v,str)},
              threshold=.25,quiet_gate=GATE,
              amplitudes=[.1,.3,1.],leading_seconds=[.6,.63,.66,.69],runs=[],complete=False,
              enable_by_default=False,production_defaults_changed=False,full_acoustic_acceptance=False,
              whole_application_acceptance=False,windows_acceptance=False)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    def save(): a.output.write_text(json.dumps(data,indent=2)+'\n')
    save()
    for name,pcm,label,group in sources:
        cfg=dict(config,keywords=str((Path(config['keywords']).parent/(group+'-keywords.txt')).resolve()))
        rows=[]
        for amplitude in data['amplitudes']:
            scaled=array.array('h',(round(s*amplitude) for s in array.array('h',pcm))).tobytes()
            for lead in data['leading_seconds']:
                path=a.work_dir/f'{name}-{amplitude}-{lead}.raw'
                path.write_bytes(silence(lead)+scaled+silence(1.5))
                rows.append(dict(name=path.stem,path=str(path.resolve()),seconds=path.stat().st_size/32000,
                                 expected=[label],amplitude=amplitude,leading_seconds=lead))
        settings=a.work_dir/f'{name}-ungated.json'
        settings.write_text(json.dumps(cfg))
        baseline=probe(a.baseline_dir/'jarvis-kws-probe',settings,rows,False)
        gated_settings=a.work_dir/f'{name}-gated.json'
        gated_settings.write_text(json.dumps(dict(cfg,quiet_gate=GATE)))
        gated=probe(a.binary,gated_settings,rows,False)
        cropped=[]
        for row,before,after in zip(rows,baseline['cases'],gated['cases'],strict=True):
            origins={round(h['stream_origin_seconds']*32000) for h in after['hits']}
            if len(origins)==1:
                origin=origins.pop()
                path=a.work_dir/(row['name']+'-cropped.raw')
                path.write_bytes(Path(row['path']).read_bytes()[origin:])
                cropped.append(dict(row,path=str(path.resolve()),seconds=path.stat().st_size/32000,origin_bytes=origin))
        crop_result=probe(a.binary,settings,cropped,False) if cropped else None
        for row,before,after in zip(rows,baseline['cases'],gated['cases'],strict=True):
            crop=next(((r,c) for r,c in zip(cropped,crop_result['cases'],strict=True) if r['name']==row['name']),None) if crop_result else None
            deltas=[round(b['audio_seconds']-x['audio_seconds'],8) for x,b in zip(before['hits'],after['hits'])] if before['actual']==after['actual'] else None
            reproduced=None
            if crop:
                origin=crop[0]['origin_bytes']/32000
                reproduced=(crop[1]['actual']==after['actual'] and
                            [round(h['audio_seconds']+origin,8) for h in crop[1]['hits']]==
                            [round(h['audio_seconds'],8) for h in after['hits']])
            data['runs'].append(dict(source=name,amplitude=row['amplitude'],leading_seconds=row['leading_seconds'],
                input_sha256=digest(row['path']),baseline=before,gated=after,cropped=crop,
                labels_identical=before['actual']==after['actual'],trigger_deltas_seconds=deltas,
                cropped_ungated_reproduces_gated_trigger_chunks=reproduced))
        save()
        print('timing diagnosis: '+name,flush=True)
    deltas=[d for r in data['runs'] for d in r['trigger_deltas_seconds'] or []]
    data['summary']=dict(variants=len(data['runs']),source_recordings=3,
        all_labels_identical=all(r['labels_identical'] for r in data['runs']),
        baseline_misses=sum(not r['baseline']['passed'] for r in data['runs']),
        gated_misses=sum(not r['gated']['passed'] for r in data['runs']),
        later_variants=sum(any(d>0 for d in r['trigger_deltas_seconds'] or []) for r in data['runs']),
        trigger_delta_range_seconds=[min(deltas),max(deltas)],
        cropped_controls=sum(r['cropped'] is not None for r in data['runs']),
        cropped_controls_reproduced=sum(r['cropped_ungated_reproduces_gated_trigger_chunks'] is True for r in data['runs']),
        timing_nonregression=all(d<=0 for d in deltas) and all(r['labels_identical'] for r in data['runs']))
    data['complete']=True
    save()
    print(json.dumps(data['summary'],indent=2))
    return 0 if data['summary']['timing_nonregression'] else 1


if __name__=='__main__': raise SystemExit(main())
