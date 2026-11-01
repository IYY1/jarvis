#!/usr/bin/env python3
"""Separate parent/report and resident-detector memory growth, without trimming.

Two identical paced handoff arms differ only in report retention. A third
fresh detector-only arm replays the identical PCM/settings without a command
worker. Linux RSS/PSS and glibc in-use/free arena counts are diagnostics, not
proof of native allocation ownership, leak freedom or a stable plateau.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from benchmark_kws import ROOT, digest, probe


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('config','fixture','work-dir','output'):
        p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--binary',type=Path,default=ROOT/'native/kws-probe/target/release/jarvis-kws-handoff-probe')
    p.add_argument('--cycles',type=int,default=8)
    a=p.parse_args()
    if not 4<=a.cycles<=16 or a.output.exists() or a.work_dir.exists(): p.error('Use 4–16 cycles and new work/evidence paths')
    a.work_dir.mkdir(parents=True)
    base=json.loads(a.config.read_text())
    base.pop('continuous',None)
    data=dict(schema_version=1,recorded_at=datetime.now(timezone.utc).isoformat(),scope=__doc__,
        cycles=a.cycles,binary_sha256=digest(a.binary),detector_sha256=digest(base['detector_binary']),
        voice_sha256=digest(base['voice_binary']),fixture_sha256=digest(a.fixture),runs=[],complete=False,
        allocator_trim_requested=False,production_defaults_changed=False,whole_application_acceptance=False,
        full_acoustic_acceptance=False,windows_acceptance=False,full_duration_soak_acceptance=False,enable_by_default=False)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    def save(): a.output.write_text(json.dumps(data,indent=2)+'\n')
    save()
    for retain in (True,False):
        folder=a.work_dir/('retained-reports' if retain else 'streamed-reports')
        folder.mkdir()
        cfg=dict(base,cycles=a.cycles,retain_reports=retain,work_dir=str(folder.resolve()))
        settings=folder/'config.json'
        settings.write_text(json.dumps(cfg))
        print('floor attribution: '+folder.name,flush=True)
        run=subprocess.run([str(a.binary.resolve()),str(settings.resolve()),str(a.fixture.resolve())],
            capture_output=True,text=True,timeout=30+a.cycles*35,check=True)
        events=[json.loads(line) for line in run.stdout.splitlines()]
        rows=[e for e in events if e['event']=='cycle_complete']
        passed=len(rows)==a.cycles and events[-1]['event']=='complete'
        for row in rows:
            actual=[e['text'] for e in row['command_result'].get('events',[]) if e['kind']=='command'] if row['command_result'] else []
            start=round(row['buffer_start_seconds']*32000)
            end=round(row['endpoint_input_seconds']*32000)
            row['buffer_identical']=Path(row['buffer_path']).read_bytes()==a.fixture.read_bytes()[start:end]
            passed=passed and actual==['ten meters'] and row['buffer_identical'] and row['waiting_after_release']['processes']==2
        summary={}
        for role in ('parent','detector'):
            values=[next(p for p in r['waiting_after_release']['by_process'] if p['role']==role) for r in rows]
            summary[role]={key:[v[key] for v in values] for key in ('rss_mib','pss_mib','anonymous_mib','private_dirty_mib')}
            summary[role]['rss_growth_mib']=values[-1]['rss_mib']-values[0]['rss_mib']
        summary['parent_in_use_bytes']=[r['waiting_after_release']['parent_heap']['in_use_bytes'] for r in rows]
        summary['parent_free_arena_bytes']=[r['waiting_after_release']['parent_heap']['free_arena_bytes'] for r in rows]
        summary['detector_in_use_bytes']=[r['detector_complete']['memory']['heap']['in_use_bytes'] for r in rows]
        summary['detector_free_arena_bytes']=[r['detector_complete']['memory']['heap']['free_arena_bytes'] for r in rows]
        summary['serialized_retained_report_bytes']=events[-1]['retained_report_bytes']
        data['runs'].append(dict(arm=folder.name,configuration=cfg,events=events,summary=summary,passed=passed))
        save()
        print(json.dumps(dict(arm=folder.name,summary=summary,passed=passed)),flush=True)
    print('floor attribution: detector-only',flush=True)
    settings=Path(base['detector_config'])
    rows=[dict(name=f'detector-only-{i}',path=str(a.fixture.resolve()),seconds=a.fixture.stat().st_size/32000,expected=['GO_FORWARD']) for i in range(a.cycles)]
    direct=probe(Path(base['detector_binary']),settings,rows,True)
    memory=[r['measurement']['memory'] for r in direct['cases']]
    data['runs'].append(dict(arm='detector-only',configuration=json.loads(settings.read_text()),events=direct,
        summary={key:[r[key] for r in memory] for key in ('rss_mib','pss_mib','anonymous_mib','heap')},passed=direct['all_cases_pass']))
    data['complete']=True
    data['all_commands_and_buffers_match']=all(r['passed'] for r in data['runs'])
    save()
    return 0 if data['all_commands_and_buffers_match'] else 1


if __name__=='__main__': raise SystemExit(main())
