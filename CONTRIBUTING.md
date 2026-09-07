# Contributing

Use small, reviewable commits and add regression coverage before changing a
storage lifecycle path. Keep user-facing text in English and preserve the
storage-plugin/dashboard privilege boundary.

Never commit credentials, private keys, host-specific evidence, customer data,
or proprietary source. Replace real hostnames, IP addresses, WWIDs and LVM
identities with synthetic fixtures.

Destructive integration tests belong only on explicitly disposable resources,
with collision preflight, a recovery plan, scoped cleanup and final health
verification. A failure or ambiguous result must preserve data and artifacts
rather than guessing that deletion is safe.

Run the complete CI-safe suite and reproducible package build before submitting
a pull request.
