# -*- coding: utf-8 -*-
"""The day's numbers for the final demo (slide 79). Run in the repository root:  python workshop/day-numbers.py [YYYYMMDD]

Counts the agent runs of that day from results/*<date>*.jsonl (the scripted runs of the resume demo are left out).
Without a date it takes today's."""
import glob, json, sys, time

day = sys.argv[1] if len(sys.argv) > 1 else time.strftime('%Y%m%d')
runs = []
for f in sorted(glob.glob('results/*%s*.jsonl' % day)):
    if 'resume-demo' in f:
        continue
    runs += [json.loads(l) for l in open(f, encoding='utf-8') if l.strip()]
credits = sum(r.get('ai_credits') or 0 for r in runs)
print('agent runs            %d' % len(runs))
print('passed held-back tests %d' % sum(bool(r.get('success')) for r in runs))
print('AI credits            %.1f = $%.2f' % (credits, credits / 100))
print('gate catches          %d' % sum(r.get('gate_failures_total') or 0 for r in runs))
print('retries               %d' % sum(r.get('retries') or 0 for r in runs))
print('approval points       %d  (passed automatically: these runs were unattended)' % sum(r.get('interventions') or 0 for r in runs))
if not runs:
    print('(no result files for %s in results/: is this the repository root?)' % day)
