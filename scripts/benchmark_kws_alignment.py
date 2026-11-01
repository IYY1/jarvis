#!/usr/bin/env python3
"""One fixed model-chunk-aligned gate against saved baseline/legacy controls.

Three supplied recordings, a complete 320/30 ms phase cycle and fixed gains.
No tuning, real capture, action dispatch, allocator change, or promotion.
"""
import argparse,array
from datetime import datetime,timezone
import json,math
from pathlib import Path
import wave
from benchmark_kws import ROOT,digest,probe,silence
from benchmark_kws_gate import GATE,gate_cases,verify_baseline


def comparison(before,after):
    result=[]
    for old,new in zip(before['cases'],after['cases'],strict=True):
        assert old['name']==new['name']
        same=old['actual']==new['actual']
        result.append(dict(name=old['name'],labels_identical=same,
            deltas_seconds=[round(b['audio_seconds']-a['audio_seconds'],8) for a,b in zip(old['hits'],new['hits'],strict=True)] if same else None))
    return result


def summarize(data):
    comparisons=[row for group in data['phase_comparisons'] for row in group['aligned']]
    deltas=[v for row in comparisons for v in row['deltas_seconds'] or []]
    return dict(phase_cases=len(comparisons),all_phase_labels_identical=all(r['labels_identical'] for r in comparisons),
        later_phase_cases=sum(any(d>0 for d in r['deltas_seconds'] or []) for r in comparisons),
        trigger_delta_range_seconds=[min(deltas),max(deltas)] if deltas else None,
        phase_fixture_labels_pass=all(r['measurement']['all_cases_pass'] for r in data['runs'] if r['stage']=='phase'),
        all_phase_trigger_times_nonregressing=bool(comparisons) and all(r['labels_identical'] and all(d<=0 for d in r['deltas_seconds']) for r in comparisons))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('config','fixture','model-dir','baseline-dir','protocol','work-dir','output'):
        p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--binary',type=Path,default=ROOT/'native/kws-probe/target/release/jarvis-kws-probe')
    p.add_argument('--legacy-binary',type=Path,default=ROOT/'build/memory-plan-kws/reader-cache-candidate/normal-jarvis-kws-probe')
    a=p.parse_args()
    if a.output.exists() or a.work_dir.exists():p.error('Use new evidence/work paths')
    protocol=json.loads(a.protocol.read_text())
    a.work_dir.mkdir(parents=True)
    baseline=verify_baseline(a.baseline_dir)
    cfg=json.loads(a.config.read_text());cfg.pop('quiet_gate',None)
    assert digest(cfg['encoder'])==protocol['model']['sha256']
    assert protocol['model']['decode_shift_ms']==320 and protocol['candidate']['align_chunk_ms']==320
    assert cfg['threshold']==.25
    data=dict(schema_version=1,recorded_at=datetime.now(timezone.utc).isoformat(),scope=__doc__,
        protocol=protocol,protocol_sha256=digest(a.protocol),baseline_snapshot=baseline,
        binary_sha256=digest(a.binary),legacy_binary_sha256=digest(a.legacy_binary),configuration=cfg,
        assets={k:dict(path=v,sha256=digest(v)) for k,v in cfg.items() if isinstance(v,str)},
        runs=[],phase_comparisons=[],focused_comparisons=[],complete=False,
        production_defaults_changed=False,whole_application_acceptance=False,full_acoustic_acceptance=False,
        windows_acceptance=False,full_duration_soak_acceptance=False,enable_by_default=False)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    def save():
        data['summary']=summarize(data)
        a.output.write_text(json.dumps(data,indent=2)+'\n')
    save()
    def measure(stage,arm,group,rows,paced=False,repeat=None):
        config=dict(cfg,keywords=str((Path(cfg['keywords']).parent/(group+'-keywords.txt')).resolve()))
        if arm in ['legacy','current-legacy']:config['quiet_gate']=GATE
        elif arm=='aligned':config['quiet_gate']=protocol['candidate']
        settings=a.work_dir/f'{stage}-{group}-{arm}.json';settings.write_text(json.dumps(config))
        binary=a.baseline_dir/'jarvis-kws-probe' if arm=='baseline' else a.legacy_binary if arm=='legacy' else a.binary
        print(f'{stage}: {group}, {arm}, repeat={repeat}, cases={len(rows)}',flush=True)
        measured=probe(binary,settings,rows,paced)
        for row in measured['cases']:
            m=row['measurement'];total=round(m['audio_seconds']*16000)
            if 'decoded_samples' in m:
                assert m['decoded_samples']+m['skipped_samples']==total
            if arm=='aligned':
                assert m['accepted_samples']==m['decoded_samples']+m['replayed_samples']
                assert all(origin%5120==0 for origin in m['stream_origins_samples'])
        data['runs'].append(dict(stage=stage,arm=arm,group=group,repeat=repeat,paced=paced,
                                inputs=[dict(r,sha256=digest(r['path'])) for r in rows],configuration=config,measurement=measured))
        save()
        return measured
    sources=[('go-forward',a.fixture.read_bytes(),'GO_FORWARD','surrogate')]
    for number,label in [(0,'LIGHT_UP'),(1,'LOVELY_CHILD')]:
        with wave.open(str(a.model_dir/'test_wavs'/f'{number}.wav')) as wav:
            assert (wav.getframerate(),wav.getnchannels(),wav.getsampwidth())==(16000,1,2)
            sources.append((f'published-{number}',wav.readframes(wav.getnframes()),label,'published'))
    for source,pcm,label,group in sources:
        rows=[]
        for gain in protocol['phase_grid']['amplitudes']:
            scaled=array.array('h',(round(s*gain) for s in array.array('h',pcm))).tobytes()
            for lead in protocol['phase_grid']['leading_ms']:
                path=a.work_dir/f'{source}-{gain}-{lead}.raw';path.write_bytes(silence(lead/1000)+scaled+silence(1.5))
                rows.append(dict(name=path.stem,path=str(path.resolve()),seconds=path.stat().st_size/32000,
                    expected=[label],gain=gain,leading_ms=lead))
        old=measure('phase','baseline',group,rows)
        legacy=measure('phase','legacy',group,rows)
        aligned=measure('phase','aligned',group,rows)
        data['phase_comparisons'].append(dict(source=source,legacy=comparison(old,legacy),aligned=comparison(old,aligned)))
        save()
    cases_dir=a.work_dir/'focused';cases_dir.mkdir()
    controls=gate_cases(a.fixture,a.model_dir,cases_dir,30)
    for group in ['surrogate','published','names']:
        rows=[r for r in controls if r['group']==group]
        old=measure('focused','baseline',group,rows)
        legacy=measure('focused','legacy',group,rows)
        current=measure('focused','current-legacy',group,rows)
        ungated=measure('focused','current-ungated',group,rows)
        aligned=measure('focused','aligned',group,rows)
        data['focused_comparisons'].append(dict(group=group,legacy_unchanged=comparison(legacy,current),
            ungated_unchanged=comparison(old,ungated),aligned=comparison(old,aligned)))
        save()
    paced=[r for r in controls if r['name'] in ['silence','low-volume'] and r['group']=='surrogate']
    for repeat in range(1,4):
        for arm in ['legacy','aligned']:measure('paced',arm,'surrogate',paced,True,repeat)
    data['complete']=True
    data['summary']=summarize(data)
    focused=[row for group in data['focused_comparisons'] for key in ['legacy_unchanged','ungated_unchanged'] for row in group[key]]
    aligned_labels=all(r['measurement']['all_cases_pass'] for r in data['runs'] if r['arm']=='aligned')
    cpu={arm:[100*c['measurement']['cpu_seconds']/c['measurement']['audio_seconds'] for r in data['runs']
        if r['stage']=='paced' and r['arm']==arm for c in r['measurement']['cases'] if c['name']=='silence'] for arm in ['legacy','aligned']}
    data['gates']=dict(candidate_fixture_labels_pass=aligned_labels,
        original_default_trigger_chunks_unchanged=all(c['labels_identical'] and all(v==0 for v in c['deltas_seconds']) for c in focused),
        phase_trigger_time_nonregression=data['summary']['all_phase_trigger_times_nonregressing'],
        paced_standby_cpu_at_most_0_1_percent=all(v<=.1 for v in cpu['aligned']),
        full_acoustic_acceptance=False,whole_application_acceptance=False,windows_acceptance=False,enable_by_default=False)
    data['silence_cpu_percent_one_core']=cpu
    save();print(json.dumps(dict(summary=data['summary'],gates=data['gates'],silence_cpu=cpu),indent=2),flush=True)
    return 0 if all(data['gates'][k] for k in ['candidate_fixture_labels_pass','original_default_trigger_chunks_unchanged','phase_trigger_time_nonregression','paced_standby_cpu_at_most_0_1_percent']) else 1

if __name__=='__main__':raise SystemExit(main())
