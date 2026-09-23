# PL-D8 Observation Fixture

A minimal deployment-pipeline repository used by the D8 real-agent utility
observation. Six modules, six target functions, one control-structure fact
per module that the base symbol/call graph cannot express on its own:

| Module | Target | Hidden fact |
| --- | --- | --- |
| promote.py | promote | rollback / cleanup sit in mutually exclusive if/else branches |
| gate.py | gate | publish runs after the branch merge, on every path |
| process.py | process | finalize is skipped only by the early `return False` inside the loop |
| scan.py | scan | commit runs even when a row triggers `break` (break exits the loop, not the function) |
| handle.py | handle | notify is unconditional (after merge); persist is conditional |
| deploy.py | deploy | register is conditional; announce precedes cleanup unconditionally |
