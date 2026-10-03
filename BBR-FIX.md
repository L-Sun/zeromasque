# BBRv2 DATAGRAM window fix

This fork builds ZeroMasque 0.1.0-bbrfix.1 from upstream commit
`5b65479e43d0def2a33a8241999cbed5a5f6ea4c`, with a local patch to quiche 0.29.1.
It retains the upstream BBRv2 selection and protocol configuration.

## Problem and change

During ProbeBW/Up, quiche previously required bytes in flight to reach the
congestion window exactly before increasing `inflight_hi`. Atomic DATAGRAM
packets can leave a window remainder too small for another packet. A measured
case had a 14,000-byte window and 13,950 bytes in flight, with more data queued;
the strict guard prevented recovery after a traffic direction change.

The patched guard treats a remainder smaller than the current path's maximum
packet size as window limited. It uses `BBRv2::mss`, including PMTU updates,
and saturating subtraction. A remainder of at least one packet still prevents
growth; the existing `inflight_hi` guard also remains in place. There are no
runtime diagnostic switches or hardcoded 1,400-byte allowances.

The vendored crate is the published quiche 0.29.1 source, with licenses retained.
The focused source diff is `deploy/quiche-bbr-datagram-cwnd.patch`. The formatter
configuration is from cloudflare/quiche tag 0.29.1. This is a local workaround,
not an upstream quiche release or a claim that all BBRv2 performance problems
are fixed.

## Build and verification

Use Rust 1.95.0 (pinned in `rust-toolchain.toml`), cargo-zigbuild 0.23.4 and
Zig 0.14.1. `deploy/build.sh` tests the workspace and builds Linux x86_64 and
AArch64 executables targeting glibc 2.28 or newer. Use `--locked` to preserve
production dependency versions. The extra lockfile entries are quiche test
dependencies; existing production dependencies were not upgraded.

The added regression fails before the guard change and passes afterward.
It covers packet sizes 1,200, 1,400, 1,450 and 9,000 bytes, underfilled windows,
and the existing inflight bound. Full workspace tests: 1,032 quiche tests,
20 ZeroMasque tests, and 45 documentation tests passed.

For `testing/interop.sh`, use a masque-go checkout at
`a9c2c486c4d201456bbfba74193c625ec248b51d` (before its Client API was replaced),
with Go 1.27.1. All four macOS interop cases passed, including both directions
against masque-go and ECH. The Linux-only reload cases are skipped on macOS.

Format ZeroMasque with `cargo fmt -p zeromasque`. For the modified vendored
files, use nightly rustfmt with the vendored config and
`--config skip_children=true,wrap_comments=false` to preserve existing comments.

## Service deployment and rollback

`deploy/install.py` takes the staged binary, its SHA-256, the expected original
binary SHA-256, and `serve` or `client`. Run it with sudo on the intended host.
It installs a versioned binary under `/usr/local/libexec/`, backs up the original
under `/var/lib/zeromasque-bbrfix/`, and overrides only `zeromasque.service`.
Other services using `/usr/local/bin/zeromasque` retain their original binary.
Existing arguments and credentials are preserved without printing them.

A separate systemd guard restores the previous service configuration after
15 minutes unless validation commits the deployment. The public receipt is
`/run/zeromasque-bbrfix-latest.json`. After validating service health and traffic,
write its `sha256` to its `commit_file`; the guard then exits successfully.
Do not commit merely because the installation command succeeded.

To roll back a committed deployment, run as root:

```sh
python3 <receipt.state_dir>/installer.py rollback <receipt.state_dir>/state.json
```

Keep each host's receipt with its deployment records. The original executable
is retained in place as well as in the backup directory.
