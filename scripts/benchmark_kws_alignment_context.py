#!/usr/bin/env python3
"""Diagnose the aligned candidate's retained miss; never tune or accept it.

Compare the unchanged failing input with crops at the two measured stream
origins, through saved and current ungated binaries. Expected labels stay
positive even when a missing detection is the diagnostic prediction.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from benchmark_kws import ROOT, digest, probe
from benchmark_kws_gate import verify_baseline


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('matrix', 'baseline-dir', 'work-dir', 'output'):
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--binary', type=Path, default=ROOT/'native/kws-probe/target/release/jarvis-kws-probe')
    a = p.parse_args()
    if a.output.exists() or a.work_dir.exists():
        p.error('Use new work and evidence paths')
    matrix = json.loads(a.matrix.read_text())
    assert matrix['complete']
    failures = [(r, c) for r in matrix['runs'] if r['stage']=='phase' and r['arm']=='aligned'
                for c in r['measurement']['cases'] if not c['passed']]
    assert len(failures)==1, 'This diagnostic is pinned to the one recorded failure'
    run, failed = failures[0]
    name = failed['name']
    before = next(c for r in matrix['runs'] if r['stage']=='phase' and r['arm']=='legacy'
                  for c in r['measurement']['cases'] if c['name']==name)
    assert failed['actual']==[] and before['actual']==['GO_FORWARD']
    source = next(row for row in run['inputs'] if row['name']==name)
    assert digest(source['path'])==source['sha256']
    pcm = Path(source['path']).read_bytes()
    aligned = failed['measurement']['stream_origins_samples']
    assert len(aligned)==1
    origins = [('whole', 0), ('aligned-origin', aligned[0]),
               ('legacy-origin', round(before['hits'][0]['stream_origin_seconds']*16000))]
    assert [origin for _, origin in origins]==[0, 20480, 21120]
    a.work_dir.mkdir(parents=True)
    config = dict(run['configuration'])
    config.pop('quiet_gate')
    settings = a.work_dir/'ungated.json'
    settings.write_text(json.dumps(config))
    rows=[]
    for label, origin in origins:
        path=a.work_dir/(label+'.raw')
        path.write_bytes(pcm[origin*2:])
        rows.append(dict(name=label,path=str(path.resolve()),seconds=path.stat().st_size/32000,
                         expected=['GO_FORWARD'],origin_samples=origin,sha256=digest(path)))
    data=dict(schema_version=1,recorded_at=datetime.now(timezone.utc).isoformat(),scope=__doc__,
              matrix_sha256=digest(a.matrix),failed_case=name,original_input_sha256=source['sha256'],
              configuration=config,baseline_snapshot=verify_baseline(a.baseline_dir),inputs=rows,runs=[],
              complete=False,recognition_acceptance=False,production_defaults_changed=False,enable_by_default=False)
    for arm, binary in [('saved-ungated',a.baseline_dir/'jarvis-kws-probe'), ('current-ungated',a.binary)]:
        measured=probe(binary,settings,rows,False)
        data['runs'].append(dict(arm=arm,binary_sha256=digest(binary),measurement=measured,
            matches_context_prediction=[c['actual'] for c in measured['cases']]==[[],[],['GO_FORWARD']]))
    data['complete']=True
    data['diagnostic_context_prediction_reproduced']=all(r['matches_context_prediction'] for r in data['runs'])
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(data,indent=2)+'\n')
    print(json.dumps({r['arm']:[c['actual'] for c in r['measurement']['cases']] for r in data['runs']},indent=2))
    return 0 if data['diagnostic_context_prediction_reproduced'] else 1


if __name__=='__main__':
    raise SystemExit(main())
