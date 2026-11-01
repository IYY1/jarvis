#!/usr/bin/env python3
"""File-only continuous multi-window/recovery controls; never dispatches actions."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from benchmark_kws import ROOT, digest, silence


def commands(row):
    return [e['text'] for e in row['command_result'].get('events', []) if e['kind'] == 'command'] if row['command_result'] else []


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('config', 'fixture', 'work-dir', 'output'):
        p.add_argument('--'+name, type=Path, required=True)
    p.add_argument('--binary', type=Path, default=ROOT/'native/kws-probe/target/release/jarvis-kws-handoff-probe')
    a = p.parse_args()
    if a.output.exists() or a.work_dir.exists():
        p.error('Use new work and evidence paths')
    a.work_dir.mkdir(parents=True)
    base = json.loads(a.config.read_text())
    pcm = a.fixture.read_bytes()
    if digest(a.fixture) != 'f15c60ec54059d8b66e410d0064945a0b0a04ea56e1ddca1958e493c0cf70e71':
        p.error('Labelled controls require the pinned public fixture')
    natural = silence(.6)+pcm+silence(3)
    # These reuses are lifecycle controls, not additional speakers or acoustic coverage.
    controls = [
        ('three-windows', natural*3, [['ten meters']]*3, ['result']*3, {}, 0),
        ('busy-discard-and-recover', natural+natural+silence(3)+natural,
         [['ten meters']]*2, ['result']*2, {'payload_delay_ms':5000}, 1),
        ('speech-timeout-then-recover', silence(.6)+pcm[:38400]+silence(7)+natural,
         [[],['ten meters']], ['command_timeout','result'], {}, 0),
        ('worker-timeout-then-recover', natural*2, [[],[]], ['worker_timeout']*2,
         {'payload_delay_ms':3000,'worker_timeout_ms':1000}, 0),
        ('window-limit', natural*3, [['ten meters']], ['result'], {'max_windows':1}, 2),
        ('end-before-quiet', silence(.6)+pcm, [[]], ['input_ended_before_endpoint'], {}, 0),
        ('command-without-wake', (silence(.6)+pcm[38400:]+silence(3))*2, [], [], {}, 0),
    ]
    data = dict(schema_version=1, recorded_at=datetime.now(timezone.utc).isoformat(), scope=__doc__,
                binary_sha256=digest(a.binary), detector_sha256=digest(base['detector_binary']),
                voice_sha256=digest(base['voice_binary']), fixture_sha256=digest(a.fixture),
                runs=[], complete=False, production_defaults_changed=False,
                full_acoustic_acceptance=False, whole_application_acceptance=False,
                windows_acceptance=False, full_duration_soak_acceptance=False, enable_by_default=False)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    def save(): a.output.write_text(json.dumps(data, indent=2)+'\n')
    save()
    for name, audio, expected, reasons, overrides, ignored in controls:
        folder = a.work_dir/name
        folder.mkdir()
        fixture, settings = folder/'input.raw', folder/'config.json'
        fixture.write_bytes(audio)
        continuous = dict(max_windows=8, worker_timeout_ms=12000, recovery_quiet_ms=600)
        continuous.update({k:v for k,v in overrides.items() if k in continuous})
        config = dict(base, cycles=1, continuous=continuous, work_dir=str(folder.resolve()))
        config.update({k:v for k,v in overrides.items() if k not in continuous})
        settings.write_text(json.dumps(config))
        print('continuous: '+name, flush=True)
        run = subprocess.run([str(a.binary.resolve()),str(settings.resolve()),str(fixture.resolve())],
                             capture_output=True,text=True,timeout=180)
        events=[json.loads(line) for line in run.stdout.splitlines()]
        rows=[e for e in events if e['event']=='window_complete']
        actual=[commands(r) for r in rows]
        actual_reasons=[r['release_reason'] for r in rows]
        slices=[]
        for r in rows:
            if r['buffer_path']:
                start=round(r['buffer_start_seconds']*32000)
                end=round(r['endpoint_input_seconds']*32000)
                slices.append(dict(window=r['window'],identical=Path(r['buffer_path']).read_bytes()==audio[start:end],
                                   sha256=digest(r['buffer_path']),start_byte=start,end_byte=end))
        complete=bool(events and events[-1]['event']=='complete')
        passed=(run.returncode==0 and complete and actual==expected and actual_reasons==reasons
                and events[-1]['ignored_triggers']==ignored and all(s['identical'] for s in slices)
                and all(r['waiting_after_release']['processes']==2 for r in rows)
                and events[-1]['final_waiting']['processes']==2)
        successes=sum(reason=='result' for reason in reasons)
        if complete:
            passed=(passed and events[-1]['worker_failures']==0
                and events[-1]['successful_workers']==successes
                and events[-1]['aborted_windows']==len(reasons)-successes
                and events[-1]['all_command_workers_succeeded']==(successes==len(reasons))
                and all(row['waiting_after_release']['child_readers']['active']==1
                    and row['waiting_after_release']['child_readers']['joined']==row['window'] for row in rows))
        data['runs'].append(dict(name=name,configuration=config,input_sha256=digest(fixture),events=events,
            exit_code=run.returncode,stderr=run.stderr,expected_commands=expected,actual_commands=actual,
            expected_release_reasons=reasons,actual_release_reasons=actual_reasons,
            expected_ignored_triggers=ignored,slices=slices,passed=passed))
        save()
        print(json.dumps(dict(name=name,passed=passed,actual=actual,reasons=actual_reasons,
                              ignored=events[-1].get('ignored_triggers') if complete else None)),flush=True)
    # Overflow must fail explicitly and unwind the owned children; never submit a clipped command.
    folder=a.work_dir/'buffer-overflow'
    folder.mkdir()
    fixture,settings=folder/'input.raw',folder/'config.json'
    fixture.write_bytes(natural)
    config=dict(base,cycles=1,max_buffer_ms=2500,work_dir=str(folder.resolve()),
        continuous=dict(max_windows=2,worker_timeout_ms=12000,recovery_quiet_ms=600))
    settings.write_text(json.dumps(config))
    run=subprocess.run([str(a.binary.resolve()),str(settings.resolve()),str(fixture.resolve())],
        capture_output=True,text=True,timeout=30)
    events=[json.loads(line) for line in run.stdout.splitlines()]
    passed=(run.returncode==1 and 'Command audio buffer overflow' in run.stderr
        and 'panicked' not in run.stderr and not list(folder.glob('continuous-*.raw')))
    data['runs'].append(dict(name='buffer-overflow',configuration=config,input_sha256=digest(fixture),
        events=events,exit_code=run.returncode,stderr=run.stderr,expected_failure=True,passed=passed))
    data['complete']=True
    data['all_lifecycle_controls_pass']=all(r['passed'] for r in data['runs'])
    save()
    return 0 if data['all_lifecycle_controls_pass'] else 1


if __name__=='__main__': raise SystemExit(main())
