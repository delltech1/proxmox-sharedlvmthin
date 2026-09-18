# BASTRIX SharedLVM `0.9.0~rc5.8~tg29` release notes

## Status

TG29 is a development candidate for disposable-lab qualification. It is not a
production support claim. Direct concurrent activation of one dm-thin pool on
multiple hosts remains prohibited.

## Opt-in PVE HA fenced-owner takeover

TG29 adds `slt-thin-ha-takeover pve-ha`, disabled by default. Together with
`slt-thin-leaseguard remote-audit`, it can recover a managed Thin VM after PVE
HA has positively fenced its former owner. It requires fresh manager status,
exact local service assignment, an online target, a fenced/offline former
owner and positive mapper-absence evidence from every remaining configured
peer. Timeout, SSH failure and cluster absence alone never authorize takeover.

Peer audit is completed before the durable old owner/epoch is removed. A
failed audit therefore performs no LVM tag mutation and remains retryable.

## Qualification evidence

- A bounded 50-VM offline Thin evacuation passed in both directions at 16-way
  orchestration concurrency, with exact mapper and owner/epoch verification.
- In an external hard-power test, PVE HA restored 10/10 managed Thin VMs after
  fencing the owner host. There were no duplicate mappings or D-state tasks;
  the fenced node rejoined with zero relevant mappings and quorum returned
  to 3/3.
- 689 Perl assertions and 182 Python tests pass in the current source tree.

These are laboratory envelopes, not claims about arbitrary SANs, fencing
devices, workloads or disk-copy duration. Thick Generations behavior remains
unchanged from TG28.
