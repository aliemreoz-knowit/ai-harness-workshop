Implement ticket ${ticket_key} ("${ticket_title}"). Work in this order and keep each step short.

${ticket}

Approved plan:
${plan}

${rework}
1. Reproduce: name the test, gate or tool run that shows the symptom (an existing test, or the
   loopback scenario closest to the ticket's steps). Do not change code before you know which.
2. Locate: follow the data path of the symptom through docs/ARCHITECTURE.md and PROTOCOL.md to the
   first place where the code disagrees with PROTOCOL.md. Quote that file:line in your summary.
3. Fix the cause there with the smallest change; do not hide the symptom further downstream and do
   not touch the receiver when the sender is wrong (or the other way round).
4. Check the same pattern elsewhere: the same mistake often sits at a second site.
?{run_gates}5. Verify with run_gates (profile ${gates}); fix the first failing item first.

?{gates}You have ${budget} gate-checked attempts; the harness runs the gates itself after your turn and
?{gates}sends you the failures.
