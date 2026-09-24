#!/usr/bin/env bash
# deploy/host/install-helper.sh
set -euo pipefail

repo=${1:?usage: install-helper.sh <eastwatch-checkout>}
root=/opt/eastwatch
uv_version=0.8.13
temporary=$(mktemp -d)
archive="$temporary/uv.tar.gz"
trap 'rm -rf "$temporary"' EXIT
curl --fail --location --silent --show-error \
  "https://github.com/astral-sh/uv/releases/download/${uv_version}/uv-x86_64-unknown-linux-gnu.tar.gz" \
  >"$archive"
tar -xzf "$archive" -C "$temporary"
install -m 0755 "$temporary/uv-x86_64-unknown-linux-gnu/uv" /usr/local/bin/uv
install -d -m 0755 "$root"
UV_PYTHON_INSTALL_DIR="$root/python" uv python install 3.12
UV_PYTHON_INSTALL_DIR="$root/python" uv venv --clear --python 3.12 "$root/.venv"
UV_PYTHON_INSTALL_DIR="$root/python" uv pip install --python "$root/.venv/bin/python" "$repo"
rm -rf "$root/deploy"
cp -R "$repo/deploy" "$root/deploy"
chmod -R a+rX "$root"
"$root/.venv/bin/bw" --help >/dev/null
"$root/.venv/bin/bw-admin" --help >/dev/null
