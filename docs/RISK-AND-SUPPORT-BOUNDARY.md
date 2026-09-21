# Experimental risk and support boundary

This document is an operational warning, not legal advice and not an
additional restriction on the rights granted by GPLv3.

BASTRIX SharedLVM release candidates, including **both Thin mode and Thick
Generations mode**, are experimental software for disposable laboratory hosts,
disposable storage, and disposable guest data only. Neither mode is
not a production storage product, certification, warranty, service-level
commitment, or promise of fitness for a particular workload, array, fabric,
failure mode, or Proxmox update.

No paid or unpaid support, maintenance response time, service level, update
schedule, compatibility commitment, warranty, or duty to investigate or fix
is offered or promised. Community interaction is voluntary. Accepting a
report, discussing an issue, reviewing a patch, or publishing a change does
not create a support contract, continuing relationship, or future obligation.

Use can result in data loss, silent or detected corruption, stale or duplicate
device-mapper state, loss of availability, incomplete migration or recovery,
and host or cluster downtime. Laboratory qualification reduces uncertainty
only inside the recorded test envelope. It does not prove correctness for a
different array, firmware, kernel, LVM/device-mapper release, multipath policy,
latency profile, workload, topology, scale, or failure sequence.

Downloading, installing, configuring, evaluating, or operating the software
does not create a support, consultancy, managed-service, or storage-custody
relationship with the authors, contributors, or BASTRIX. The operator retains
exclusive control of and responsibility for its hosts, storage, credentials,
configuration, workloads, backups, recovery decisions, and data.

The operator is solely responsible for deciding whether to install or use the
software and for providing, validating, and periodically testing:

- independent backups and bare-metal recovery;
- compute-host fencing and positive confirmation of prior-owner exclusion;
- quorum, SAN identity, redundant-path, and all-path-loss behavior;
- capacity, latency, lock-timeout, and failure-domain sizing;
- a disposable pre-production qualification matching every intended update;
- monitoring, change control, rollback, and incident response.

Do not rely on owner tags, SSH reachability, a timer, a lease, or successful
past tests as proof that a prior kernel can no longer write shared metadata.
Never manually activate one managed Thin pool on two hosts. Thick Generations
avoids the dm-thin single-writer metadata design in steady state, but remains
experimental and can still fail through software defects, storage faults,
incomplete transitions, configuration errors, platform changes, or untested
failure sequences.

The program is licensed under GNU GPLv3. Sections 15 and 16 provide the
applicable warranty disclaimer and limitation of liability, and section 17
addresses interpretation where local law limits their effect. Nothing in this
document excludes rights or liabilities that applicable law does not permit to
be excluded. Obtain advice from a qualified lawyer for the jurisdictions and
distribution model involved before relying on any liability language.
