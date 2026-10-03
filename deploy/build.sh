#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
cargo test --locked --workspace
cargo zigbuild --locked -p zeromasque --release \
  --target x86_64-unknown-linux-gnu.2.28 \
  --target aarch64-unknown-linux-gnu.2.28
