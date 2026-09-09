# SharedLvmThin 0.9.0~rc5.3.1 release notes

RC5.3.1 is a diagnostic correctness patch for RC5.3. The Doctor now reports
the qualified 50% elastic early-grow threshold as healthy while continuing to
accept 80% for explicitly selected legacy fixed or proportional policies.

The elastic grow path was qualified on a three-node PVE 9 cluster with mixed
Storage API 15/15/14. A disposable 8 GiB virtual disk started with a 4 GiB
physical pool, crossed the 50% threshold under an approximately 400 MiB/s
patterned write, and received exactly one cluster-safe grow to 7 GiB. Pattern
readback, monitoring, protected VG reserve, identity and complete cleanup all
passed.

The release remains thin-only. Thick Generations work is experimental and is
not included in this package.

