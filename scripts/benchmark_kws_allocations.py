#!/usr/bin/env python3
"""Longer file-only allocation attribution with a separately built Rust counter.

Rust requested live bytes exclude native C/C++ allocations and malloc overhead.
No trimming, real capture, actions, settings changes, or plateau/soak inference.
"""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import subprocess
from benchmark_kws import ROOT,digest,probe


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('config','fixture','work-dir','output'):
        p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--diagnostic-dir',type=Path,default=ROOT/'build/memory-plan-kws/allocation-build/release')
    p.add_argument('--detector-requests',type=int,default=128)
    p.add_argument('--cycles',type=int,default=32)
    a=p.parse_args()
    if a.work_dir.exists() or a.output.exists() or not 32<=a.detector_requests<=512 or not 16<=a.cycles<=32:
        p.error('Use new paths; 32–512 detector requests and 16–32 parent cycles')
    a.work_dir.mkdir(parents=True)
    base=json.loads(a.config.read_text());base.pop('continuous',None)
    db=a.diagnostic_dir/'jarvis-kws-probe';hb=a.diagnostic_dir/'jarvis-kws-handoff-probe'
    data=dict(schema_version=1,recorded_at=datetime.now(timezone.utc).isoformat(),scope=__doc__,
        diagnostic_binaries={p.name:digest(p) for p in (db,hb)},control_detector_sha256=digest(base['detector_binary']),
        voice_sha256=digest(base['voice_binary']),fixture_sha256=digest(a.fixture),runs=[],complete=False,
        allocator_trim_requested=False,production_defaults_changed=False,whole_application_acceptance=False,
        full_acoustic_acceptance=False,windows_acceptance=False,pressure_acceptance=False,
        full_duration_soak_acceptance=False,enable_by_default=False)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    def save():a.output.write_text(json.dumps(data,indent=2)+'\n')
    save()
    rows=[dict(name=f'request-{i+1}',path=str(a.fixture.resolve()),seconds=a.fixture.stat().st_size/32000,expected=['GO_FORWARD']) for i in range(a.detector_requests)]
    detector=[]
    for name,binary in [('control',Path(base['detector_binary'])),('rust-counter',db)]:
        print('allocation diagnosis: '+name,flush=True)
        result=probe(binary,Path(base['detector_config']),rows,False)
        measurements=[r['measurement']['memory'] for r in result['cases']]
        data['runs'].append(dict(arm='detector-'+name,events=result,passed=result['all_cases_pass'],
            memory=measurements))
        detector.append(result)
        save()
    equivalent=all([h['audio_seconds'] for h in before['hits']]==[h['audio_seconds'] for h in after['hits']]
                  and before['actual']==after['actual'] for before,after in zip(detector[0]['cases'],detector[1]['cases'],strict=True))
    # Parent reports are streamed, avoiding the already-attributed retained JSON trees.
    cfg=dict(base,cycles=a.cycles,retain_reports=False,detector_binary=str(db.resolve()),work_dir=str(a.work_dir.resolve()))
    settings=a.work_dir/'config.json';settings.write_text(json.dumps(cfg))
    print(f'allocation diagnosis: {a.cycles} parent cycles',flush=True)
    result=subprocess.run([str(hb.resolve()),str(settings.resolve()),str(a.fixture.resolve())],
        capture_output=True,text=True,check=True,timeout=30+a.cycles*35)
    events=[json.loads(line) for line in result.stdout.splitlines()]
    completed=[e for e in events if e['event']=='cycle_complete']
    passed=len(completed)==a.cycles and events[-1]['event']=='complete'
    for r in completed:
        commands=[e['text'] for e in r['command_result'].get('events',[]) if e['kind']=='command'] if r['command_result'] else []
        lo=round(r['buffer_start_seconds']*32000);hi=round(r['endpoint_input_seconds']*32000)
        r['buffer_identical']=Path(r['buffer_path']).read_bytes()==a.fixture.read_bytes()[lo:hi]
        passed=passed and commands==['ten meters'] and r['buffer_identical'] and r['waiting_after_release']['processes']==2
    data['runs'].append(dict(arm='parent-streamed-rust-counter',configuration=cfg,events=events,passed=passed))
    def trend(values):
        return dict(first=values[0],last=values[-1],growth=values[-1]-values[0],values=values)
    mem=data['runs'][1]['memory']
    summary=dict(detector_trigger_chunks_identical=equivalent,
        detector_rss_mib=trend([r['rss_mib'] for r in mem]),
        detector_rust_live_bytes=trend([r['rust_allocations']['requested_live_bytes'] for r in mem]),
        detector_rust_live_blocks=trend([r['rust_allocations']['live_blocks'] for r in mem]),
        detector_glibc_in_use_bytes=trend([r['heap']['in_use_bytes'] for r in mem]),
        parent_rust_live_bytes=trend([r['waiting_after_release']['parent_rust_allocations']['requested_live_bytes'] for r in completed]),
        parent_glibc_in_use_bytes=trend([r['waiting_after_release']['parent_heap']['in_use_bytes'] for r in completed]))
    for role in ('parent','detector'):
        summary[role+'_released_rss_mib']=trend([next(p for p in r['waiting_after_release']['by_process'] if p['role']==role)['rss_mib'] for r in completed])
    data.update(summary=summary,complete=True,all_command_buffers_and_detector_chunks_match=passed and equivalent and all(r['passed'] for r in data['runs']))
    save()
    print(json.dumps({k:{t:v for t,v in value.items() if t!='values'} if isinstance(value,dict) else value for k,value in summary.items()},indent=2),flush=True)
    return 0 if data['all_command_buffers_and_detector_chunks_match'] else 1

if __name__=='__main__':raise SystemExit(main())
