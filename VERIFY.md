# Reproducible build

The deployed program must be byte-identical to a build anyone can reproduce. We use
[solana-verify](https://github.com/Ellipsis-Labs/solana-verifiable-build) with a pinned base image.

```
cargo install solana-verify
solana-verify build --arch v1 --library-name arena_pino \
    --base-image solanafoundation/solana-verifiable-build:4.2.2 ./program
solana-verify get-executable-hash program/target/deploy/arena_pino.so
```

Notes

- `--arch v1` is required: validators reject sBPF v3 programs ("invalid file header").
- The 3.1.x base images ship a Rust too old for Pinocchio 0.11 (it needs 1.89); 4.2.2 works.
- Deploy with `--max-len` equal to the file size so the deposit is not doubled.

To check a deployed program against this repository:

```
solana-verify verify-from-repo -u <rpc> --program-id <id> --library-name arena_pino \
    --mount-path program --base-image solanafoundation/solana-verifiable-build:4.2.2 \
    --commit-hash <tag or commit> https://github.com/Pump-Battle/Pump-Battle-Arena
```
