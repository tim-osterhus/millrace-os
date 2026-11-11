# Owned demo service

`millrace demo` uses `millrace_os.demo_service.start_demo_service` to start a
temporary foreground OS child. This is separate from the ordinary installed
daemon: it reads no normal OS configuration, adopts no process and writes no
global PID or credential file. Each child binds one retained loopback socket
on a kernel-selected port and registers only its private `demo` state home.

The parent owns its exact child and two private pipes. The 256-bit ownership
token travels only through those pipes. Startup validates the child, root,
endpoint and authenticated identity within ten seconds. The root and owner
marker must remain private and match their captured device/inode identity.

The stock client serves both ordinary and demo runs. A positively observed
ordinary context enables ordinary controls. Demo, pending and failed contexts
disable workflow mutations, validation, request Replay and saved Layout
writes. Inspection, Observe, Export, Retire and unsaved display changes remain
available. Only the bound demo run exposes Finish. Its empty intent reports
that the request was accepted; it proves no shutdown, export or writer closure.
The terminal coordinator captures its evidence and exports before requesting
service drain. Main demo reports follow accepted immutable generic output
references and representation digests supplied by that coordinator.

Closing the owner pipe requests pause/drain. Explicit Finish may request
cancel/drain; duplicate intent cannot escalate a graceful pause. The handle
waits on its exact launched child for up to fifteen seconds by default. A
timeout reports that draining is incomplete and retains the private state;
it never force-kills or deletes files. Child exit and OS worker joins remain
separate from Core/native writer evidence. The Plus command owns domain
validation, exports, retention and any final deletion decision.
