# Security

This program holds real SOL once deployed. It has **not** been audited. Please report vulnerabilities privately
through GitHub's "Report a vulnerability" button on this repository rather than in a public issue.

What the program trusts (and what we plan to change before prizes get large):

- **The scorer key** posts the winner. Its output is reproducible from public chain data and a hash of the full
  leaderboard is stored on-chain, so anyone can check it, but a single key still decides. Planned: several
  independent scorers that must agree.
- **SlotHashes** supplies the random scoring cut-off. A validator can bias it. Planned: a VRF.
- **The upgrade authority** can change the program. Planned: renounce it or hold it in a time-locked multisig.
- **The keeper** has to be online for the sale, draw and payout to happen on time. Every step is permissionless
  (after a short keeper-only grace period on the sale), so anyone can run one.
