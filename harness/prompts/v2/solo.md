Work ticket ${ticket_key} ("${ticket_title}") end to end.

${ticket}

1. Reproduce: find the test, gate or loopback scenario that shows the symptom.
2. Locate the cause: follow the data path through docs/ARCHITECTURE.md and PROTOCOL.md to the first
   place where the code disagrees with PROTOCOL.md; check whether the same mistake sits at a second site.
3. Fix the cause with the smallest change; add a test under tests/ that fails without the fix.
?{run_gates}4. Verify with run_gates (profile ${gates}) until every gate passes; fix the first item first.

?{gates}You have ${budget} gate-checked attempts; after each of your turns the harness runs the gates and sends
?{gates}you the failures.
