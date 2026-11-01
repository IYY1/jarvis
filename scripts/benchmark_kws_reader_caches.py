#!/usr/bin/env python3
"""Attribute allocator cache versus child-reader lifetime effects in isolated KWS.

Sequential fresh workers; fixed supplied files/settings and no heap trimming.
Cache tunables are diagnostic variables, not an adopted allocator policy.
"""
import argparse
from datetime import datetime,timezone
import json,os,platform,subprocess
from pathlib import Path
from benchmark_kws import ROOT,digest

TCACHE='glibc.malloc.tcache_count=0'
STACK='glibc.pthread.stack_cache_size=0'


def environment(tunables):
    env=os.environ.copy()
    if 'GLIBC_TUNABLES' in env or 'MALLOC_ARENA_MAX' in env:
        raise RuntimeError('Use a clean allocator environment for this controlled experiment')
    if tunables is not None:env['GLIBC_TUNABLES']=tunables
    return env


def trend(rows):return dict(first=rows[0],last=rows[-1],growth=rows[-1]-rows[0],values=rows)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('config','fixture','work-dir','output'):
        p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--binary-dir',type=Path,default=ROOT/'build/memory-plan-kws/allocation-build/release')
    p.add_argument('--detector-requests',type=int,default=128)
    p.add_argument('--cycles',type=int,default=8)
    a=p.parse_args()
    if a.output.exists() or a.work_dir.exists() or not 32<=a.detector_requests<=256 or not 4<=a.cycles<=16:
        p.error('Use new paths, 32–256 detector requests and 4–16 parent cycles')
    a.work_dir.mkdir(parents=True)
    base=json.loads(a.config.read_text());base.pop('continuous',None)
    db,hb=[a.binary_dir/name for name in ['jarvis-kws-probe','jarvis-kws-handoff-probe']]
    data=dict(schema_version=1,recorded_at=datetime.now(timezone.utc).isoformat(),scope=__doc__,
        libc_version=platform.libc_ver(),detector_binary_sha256=digest(db),parent_binary_sha256=digest(hb),
        voice_binary_sha256=digest(base['voice_binary']),input_sha256=digest(a.fixture),
        detector_configuration=json.loads(Path(base['detector_config']).read_text()),runs=[],complete=False,
        allocator_trim_requested=False,production_defaults_changed=False,allocator_policy_adopted=False,
        full_acoustic_acceptance=False,whole_application_acceptance=False,windows_acceptance=False,
        pressure_release_acceptance=False,full_duration_soak_acceptance=False,enable_by_default=False)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    def save():a.output.write_text(json.dumps(data,indent=2)+'\n')
    save()
    reference=None
    for name,tunables in [('default',None),('no-tcache',TCACHE),('no-stack-cache',STACK)]:
        print('detector cache control: '+name,flush=True)
        request=json.dumps(dict(path=str(a.fixture.resolve()),paced=False))+'\n'
        run=subprocess.run([str(db.resolve()),base['detector_config']],input=request*a.detector_requests,
            capture_output=True,text=True,check=True,timeout=120,env=environment(tunables))
        events=[json.loads(line) for line in run.stdout.splitlines()]
        hits=[];chunks=[];mem=[];passed=True
        for event in events[1:]:
            if event['event']=='keyword':hits.append(event)
            elif event['event']=='complete':
                passed=passed and [h['result']['keyword'] for h in hits]==['GO_FORWARD']
                chunks.append([h['audio_seconds'] for h in hits]);hits=[];mem.append(event['memory'])
            else:raise RuntimeError('Unexpected detector event')
        if reference is None:reference=chunks
        passed=passed and len(mem)==a.detector_requests and not hits and chunks==reference
        summary=dict(rss_mib=trend([m['rss_mib'] for m in mem]),
            glibc_in_use_bytes=trend([m['heap']['in_use_bytes'] for m in mem]),
            rust_live_bytes=trend([m['rust_allocations']['requested_live_bytes'] for m in mem]),
            rust_live_blocks=trend([m['rust_allocations']['live_blocks'] for m in mem]))
        data['runs'].append(dict(arm='detector-'+name,tunables=tunables,events=events,summary=summary,passed=passed))
        save();print(json.dumps(dict(arm=name,passed=passed,glibc_growth=summary['glibc_in_use_bytes']['growth'])),flush=True)
    for name,join,tunables in [('detached',False,None),('joined',True,None),('joined-no-tcache',True,TCACHE),('joined-no-stack-cache',True,STACK)]:
        print('parent reader/cache control: '+name,flush=True)
        folder=a.work_dir/name;folder.mkdir()
        cfg=dict(base,cycles=a.cycles,retain_reports=False,join_readers=join,detector_binary=str(db.resolve()),work_dir=str(folder.resolve()))
        settings=folder/'config.json';settings.write_text(json.dumps(cfg))
        env=environment(tunables)
        # Only the parent differs in cache policy; all child processes retain
        # an explicit, identical default GLIBC_TUNABLES value.
        env['MURMUR_KWS_CHILD_GLIBC_TUNABLES']=''
        run=subprocess.run([str(hb.resolve()),str(settings.resolve()),str(a.fixture.resolve())],
            capture_output=True,text=True,check=True,timeout=30+a.cycles*35,env=env)
        events=[json.loads(line) for line in run.stdout.splitlines()]
        rows=[e for e in events if e['event']=='cycle_complete']
        passed=len(rows)==a.cycles and events[-1]['event']=='complete'
        for index,row in enumerate(rows,1):
            actual=[e['text'] for e in row['command_result'].get('events',[]) if e['kind']=='command'] if row['command_result'] else []
            lo=round(row['buffer_start_seconds']*32000);hi=round(row['endpoint_input_seconds']*32000)
            row['buffer_identical']=Path(row['buffer_path']).read_bytes()==a.fixture.read_bytes()[lo:hi]
            readers=row['waiting_after_release']['child_readers']
            passed=passed and actual==['ten meters'] and row['buffer_identical'] and row['waiting_after_release']['processes']==2
            passed=passed and readers['active']==1 and readers['spawned']==index+1 and readers['joined']==(index if join else 0)
        mem=[row['waiting_after_release'] for row in rows]
        summary=dict(parent_glibc_in_use_bytes=trend([m['parent_heap']['in_use_bytes'] for m in mem]),
            parent_rust_live_bytes=trend([m['parent_rust_allocations']['requested_live_bytes'] for m in mem]),
            parent_rust_live_blocks=trend([m['parent_rust_allocations']['live_blocks'] for m in mem]))
        for role in ['parent','detector']:
            for field in ['rss_mib','threads']:
                summary[role+'_'+field]=trend([next(p for p in m['by_process'] if p['role']==role)[field] for m in mem])
        data['runs'].append(dict(arm='parent-'+name,parent_tunables=tunables,child_tunables='',configuration=cfg,
            events=events,summary=summary,passed=passed))
        save();print(json.dumps(dict(arm=name,passed=passed,glibc_growth=summary['parent_glibc_in_use_bytes']['growth'])),flush=True)
    data.update(complete=True,all_comparison_controls_pass=all(r['passed'] for r in data['runs']))
    save()
    return 0 if data['all_comparison_controls_pass'] else 1

if __name__=='__main__':raise SystemExit(main())
