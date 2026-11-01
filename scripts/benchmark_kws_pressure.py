#!/usr/bin/env python3
"""File-only KWS worker recovery under a private transient user cgroup limit.

Only this harness's newly created unit is limited. No host pressure generator,
capture, installed-service restart, automatic command retry, or global limits.
Expected OOM windows remain failed recognition windows with raw nonzero exits.
"""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import subprocess
import threading
import time
import uuid
from benchmark_kws import ROOT,digest

MIB=1024*1024


def counters(path):
    return {k:int(v) for k,v in (line.split() for line in path.read_text().splitlines())}


def cg_snapshot(folder):
    return dict(current=int((folder/'memory.current').read_text()),peak=int((folder/'memory.peak').read_text()),
                maximum=(folder/'memory.max').read_text().strip(),events=counters(folder/'memory.events'),
                pressure=(folder/'memory.pressure').read_text())


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('config','fixture','work-dir','output'):
        p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--binary',type=Path,default=ROOT/'native/kws-probe/target/release/jarvis-kws-handoff-probe')
    a=p.parse_args()
    if a.work_dir.exists() or a.output.exists():p.error('Use new work/evidence paths')
    a.work_dir.mkdir(parents=True)
    base=json.loads(a.config.read_text());base.pop('continuous',None)
    # Already supplied natural file; two separated windows, no live devices.
    pcm=a.fixture.read_bytes()
    audio=pcm+bytes(32000*3)+pcm+bytes(32000)
    data=dict(schema_version=1,recorded_at=datetime.now(timezone.utc).isoformat(),scope=__doc__,
        binary_sha256=digest(a.binary),detector_sha256=digest(base['detector_binary']),
        voice_sha256=digest(base['voice_binary']),fixture_sha256=digest(a.fixture),runs=[],complete=False,
        production_defaults_changed=False,whole_application_acceptance=False,windows_acceptance=False,
        full_acoustic_acceptance=False,full_duration_soak_acceptance=False,enable_by_default=False)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    def save():a.output.write_text(json.dumps(data,indent=2)+'\n')
    save()
    cases=[('bounded-success',384,False,[['ten meters'],['ten meters']],0),
           ('oom-then-release',192,True,[[],['ten meters']],1),
           ('sustained-oom',192,False,[[],[]],2)]
    for name,limit,release_limit,expected,failures in cases:
        folder=a.work_dir/name;folder.mkdir()
        source,settings=folder/'input.raw',folder/'config.json';source.write_bytes(audio)
        cfg=dict(base,cycles=1,retain_reports=False,work_dir=str(folder.resolve()),
            continuous=dict(max_windows=2,worker_timeout_ms=12000,recovery_quiet_ms=600,command_oom_score_adj=1000))
        settings.write_text(json.dumps(cfg))
        unit='jarvis-kws-pressure-'+uuid.uuid4().hex[:12]
        cmd=['systemd-run','--user','--quiet','--pipe','--wait','--collect','--unit='+unit,
             '--property=MemoryAccounting=yes','--property=MemoryMax='+str(limit*MIB),
             '--property=MemorySwapMax=0','--property=OOMPolicy=continue',
             '--property=RuntimeMaxSec=60','--working-directory='+str(ROOT),
             str(a.binary.resolve()),str(settings.resolve()),str(source.resolve())]
        print('pressure: '+name,flush=True)
        events=[];observations=[];stop=threading.Event();monitor=None;cg=None;restored=False;final=None
        stderr_path=folder/'stderr.log'
        try:
            with stderr_path.open('w') as stderr,subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=stderr,text=True) as process:
                def sample():
                    while not stop.wait(.02):
                        try:observations.append(cg_snapshot(cg))
                        except OSError:return
                        if len(observations)>=3000:return
                for line in process.stdout:
                    event=json.loads(line);events.append(event)
                    if event['event']=='ready':
                        parent=next(row['pid'] for row in event['waiting']['by_process'] if row['role']=='parent')
                        group=Path(f'/proc/{parent}/cgroup').read_text().strip().split('::',1)[1]
                        cg=Path('/sys/fs/cgroup')/group.lstrip('/')
                        # Never change a preexisting or unrelated cgroup.
                        if cg.name!=unit+'.service' or cg_snapshot(cg)['maximum']!=str(limit*MIB):
                            raise RuntimeError('Transient cgroup ownership/limit check failed')
                        if (cg/'memory.oom.group').read_text().strip()!='0':
                            raise RuntimeError('This private control requires per-process OOM handling')
                        observations.append(cg_snapshot(cg));monitor=threading.Thread(target=sample);monitor.start()
                    if release_limit and event['event']=='window_complete' and event['window']==1:
                        (cg/'memory.max').write_text(str(384*MIB))
                        restored=True
                    if event['event']=='complete' and cg:final=cg_snapshot(cg)
                code=process.wait(timeout=10)
        finally:
            stop.set()
            if monitor:monitor.join(timeout=2)
        rows=[e for e in events if e['event']=='window_complete']
        actual=[[v['text'] for v in row['command_result'].get('events',[]) if v['kind']=='command'] if row['command_result'] else [] for row in rows]
        slices=[]
        for row in rows:
            if row['buffer_path']:
                lo=round(row['buffer_start_seconds']*32000);hi=round(row['endpoint_input_seconds']*32000)
                slices.append(dict(window=row['window'],identical=Path(row['buffer_path']).read_bytes()==audio[lo:hi]))
        oom=max((v['events'].get('oom_kill',0) for v in observations+[final] if v),default=0)
        complete=bool(events and events[-1]['event']=='complete')
        signaled=sum(row.get('worker_exit_signal')==9 for row in rows)
        expected_code=1 if failures else 0
        passed=(code==expected_code and complete and actual==expected and len(rows)==2
            and events[-1]['worker_failures']==failures and oom>=failures and signaled>=failures
            and all(row['waiting_after_release']['processes']==2 for row in rows)
            and all(s['identical'] for s in slices) and (not release_limit or restored))
        if complete:
            passed=(passed and events[-1]['successful_workers']==2-failures
                and events[-1]['aborted_windows']==0
                and events[-1]['all_command_workers_succeeded']==(failures==0)
                and all(row['waiting_after_release']['child_readers']['active']==1
                    and row['waiting_after_release']['child_readers']['joined']==row['window'] for row in rows))
        if failures==0:passed=passed and oom==0 and all(row['release_reason']=='result' for row in rows)
        collected=subprocess.run(['systemctl','--user','show',unit+'.service','--property=LoadState','--value'],capture_output=True,text=True,timeout=5)
        unit_collected=collected.stdout.strip()=='not-found'
        passed=passed and unit_collected
        data['runs'].append(dict(name=name,unit=unit+'.service',configuration=cfg,command=cmd,
            input_sha256=digest(source),unit_collected=unit_collected,initial_limit_mib=limit,limit_released=restored,expected_commands=expected,
            actual_commands=actual,exit_code=code,expected_exit_code=expected_code,events=events,
            cgroup_samples=len(observations),cgroup_initial=observations[0] if observations else None,
            cgroup_final=final,cgroup_peak_bytes=max((v['peak'] for v in observations+[final] if v),default=0),
            oom_kills=oom,signaled_workers=signaled,slices=slices,stderr=stderr_path.read_text(),
            lifecycle_control_pass=passed,command_recognition_gate_pass=failures==0 and actual==expected))
        save();print(json.dumps(dict(name=name,passed=passed,exit_code=code,actual=actual,oom_kills=oom)),flush=True)
    data.update(complete=True,all_lifecycle_controls_pass=all(r['lifecycle_control_pass'] for r in data['runs']),
                all_command_recognition_gates_pass=all(r['command_recognition_gate_pass'] for r in data['runs']),
                pressure_release_gate_pass=False)
    save()
    return 0 if data['all_lifecycle_controls_pass'] else 1

if __name__=='__main__':raise SystemExit(main())
