#!/usr/bin/env python3
"""Independent whole-operation supplement: fixed 12 balanced rounds."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import benchmark_operations315 as ops

CASES=('mediainfo_generated_full','mediainfo_generated_changed','hash_real')

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--baseline',type=Path,required=True)
    p.add_argument('--optimized',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    combinations=[(v,m)for v in ('baseline','optimized')for m in ops.MODES]
    payload=dict(protocol_version='supplement1',rounds_policy='fixed 12 before measurement',
                 cpu_constraints=ops.cpu_constraints(),
                 warmups=[],samples=[],rounds=None,noise_margin_pct=3)
    def save():
        q=args.output.with_suffix('.partial.json')
        q.write_text(json.dumps(payload,indent=2,default=str)+'\n');q.replace(args.output)
    def run_one(version,mode,case):
        source=args.baseline if version=='baseline'else args.optimized
        cmd=[ops.WRAPPERS[mode],str(Path(ops.__file__).resolve()),'--child',case,
             '--mode',mode,'--source',str(source)]
        t0=time.perf_counter()
        proc=subprocess.run(cmd,capture_output=True,text=True,env=os.environ.copy(),cwd='/tmp')
        if proc.returncode:raise RuntimeError(f'{version}/{mode}/{case}: {proc.stderr}\n{proc.stdout[-2000:]}')
        value=json.loads(proc.stdout.strip().splitlines()[-1])
        value.update(version=version,process_elapsed_sec=time.perf_counter()-t0)
        if proc.stderr:value['stderr']=proc.stderr[-4000:]
        return value
    checksums={}
    for version,mode in combinations:
        for case in CASES:
            row=run_one(version,mode,case)
            expected=checksums.setdefault(case,row['checksum'])
            assert row['checksum']==expected,(version,mode,case,'result mismatch')
            payload['warmups'].append(row);save()
    longest=max(r['operation']['elapsed_sec']for r in payload['warmups'])
    rounds=12
    payload.update(rounds=rounds,observed_warmup_max_operation_sec=longest)
    orders=[]
    for r in range(rounds):
        order=combinations[r%6:]+combinations[:r%6]
        if(r//6)%2:order=list(reversed(order))
        orders.append(order)
    payload['orders']=orders;save()
    print(f'Correctness complete. Maximum warmup {longest:.3f}s; fixed {rounds} rounds',flush=True)
    for r,order in enumerate(orders):
        cases=CASES if r%2==0 else tuple(reversed(CASES))
        for version,mode in order:
            for case in cases:
                row=run_one(version,mode,case)
                assert row['checksum']==checksums[case],(r,version,mode,case,'result changed')
                row['round']=r;payload['samples'].append(row);save()
        print(f'Supplement round {r+1}/{rounds} complete',flush=True)
    payload['comparisons']=ops.analyze(payload);save()

if __name__=='__main__':main()
