#!/bin/sh
# SPDX-License-Identifier: LGPL-3.0-or-later
# Run before every push of this public repository: nothing private may be in it.
#   tools/leak-scan.sh [PATTERNS_FILE]
# PATTERNS_FILE (one extended regex per line, kept OUTSIDE this repository) lists identifiers that must not
# appear: your host names, IP addresses, service ids, key fingerprints. The built-in checks also refuse
# private-key blocks, sops/age material and gitleaks findings (when gitleaks is installed).
set -eu
cd "$(dirname "$0")/.."
rc=0
files=$(git ls-files -co --exclude-standard)
bad() { echo "LEAK-SCAN: $*"; rc=1; }
# shellcheck disable=SC2086
if grep -n -E -- '-----BEGIN [A-Z ]*PRIVATE KEY-----|AGE-SECRET-KEY-|ENC\[AES256_GCM|^sops:' $files 2>/dev/null | grep -v '^tools/leak-scan.sh:'; then bad "key or sops material"; fi
if [ -n "${1:-}" ]; then
  pat=$(grep -v '^\s*$' "$1" | grep -v '^#' | paste -sd'|')
  if [ -n "$pat" ]; then
    # shellcheck disable=SC2086
    if grep -n -i -E -- "$pat" $files 2>/dev/null | grep -v '^tools/leak-scan.sh:'; then bad "private identifier from the patterns file"; fi
  fi
else
  echo "LEAK-SCAN: no patterns file given; only the built-in checks ran"
fi
if command -v gitleaks >/dev/null 2>&1; then
  gitleaks detect --no-banner --redact -s . || bad "gitleaks"
fi
[ "$rc" = 0 ] && echo "LEAK-SCAN: clean"
exit "$rc"
