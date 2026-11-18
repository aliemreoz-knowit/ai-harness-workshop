Write tests for ticket ${ticket_key} ("${ticket_title}").

${ticket}

Plan (if any):
${plan}

Current change under test (diff, truncated):
```diff
${diff}
```
${rework}
${test_requirement}
?{gates}Gate profile for this step: ${gates} (${gate_ids}); the harness runs it after your turn.
?{run_gates}Check with run_gates before you end your turn.
Only files under tests/ may change.
