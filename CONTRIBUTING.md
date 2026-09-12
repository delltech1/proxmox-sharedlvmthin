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

## Copyright and contribution terms

Do not submit code, documentation or other material unless you created it or
have the legal right to contribute it. By submitting a contribution, you agree
that it may be distributed as part of this project under `GPL-3.0-only`, and
that the copyright and license notices already present in the project may be
preserved. Do not remove or replace another contributor's valid notice.

Contributions must include a Developer Certificate of Origin sign-off in each
commit:

```text
Signed-off-by: Your Name <your-email@example.com>
```

The sign-off certifies the contribution under the Developer Certificate of
Origin 1.1. Read [the DCO](https://developercertificate.org/) before signing.
Use `git commit -s` to add it. A sign-off does not transfer copyright and does
not by itself grant the project a separate proprietary relicensing right.

The project owner may require a separately reviewed contributor agreement
before accepting a substantial contribution if broader licensing rights are
needed. Until such an agreement is published, maintainers may defer substantial
third-party code contributions rather than create ambiguous ownership or
relicensing rights. Bug reports, test results and design discussion remain
welcome.

Use of project or BASTRIX branding is governed separately by
[TRADEMARKS.md](TRADEMARKS.md). Contributing code does not grant trademark,
certification or endorsement rights.

