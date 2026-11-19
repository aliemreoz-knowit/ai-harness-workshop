Implement ticket ${ticket_key} ("${ticket_title}").

${ticket}

Approved plan:
${plan}

${rework}
Work in this order and keep every step short:
1. Read the ticket's "Acceptance criteria" and "Out of scope" sections first; they bound the change.
2. Reproduce: name the existing test, gate or loopback scenario that shows the symptom. If none
   does, say so and name the closest one; do not change code before you know which.
3. Fix the cause at the first place where the code disagrees with PROTOCOL.md, with the smallest
   change; keep the public API in include/ as it is.
4. Look for the same pattern at a second site before you end the turn.
5. End with three lines: `cause: <file:line>`, `change: <one sentence>`, `evidence: <test or gate>`.
?{run_gates}6. Verify with run_gates (profile ${gates}); fix the first failing item first.

?{gates}You have ${budget} gate-checked attempts; the harness runs the gates itself after your turn and
?{gates}sends you the failures.
