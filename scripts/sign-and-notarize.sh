#!/usr/bin/env bash
# Sign the bundled binary, then notarize the packed .mcpb, so macOS will run it
# on a machine that downloaded it rather than the one that built it.
#
# Why this exists, measured rather than assumed: an unsigned binary carrying
# com.apple.quarantine is killed by Gatekeeper with SIGKILL, exit 137, and no
# output at all. Not an error message - silence. The extension therefore works
# perfectly for whoever built it and dies instantly for everyone who downloads
# it, because locally built files never carry the quarantine attribute. The
# build machine is the least representative machine you own.
#
#   bash scripts/sign-and-notarize.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE="$ROOT/granola-router.mcpb"
BINARY="$ROOT/extension/granola-router-mcp"
ENTITLEMENTS="$ROOT/scripts/pyinstaller.entitlements"
KEYCHAIN_PROFILE="${NOTARY_PROFILE:-spotify-mcpb-notary}"

# --- Preflight --------------------------------------------------------------

# `|| true` is load-bearing. Under `set -e` with `pipefail`, grep matching
# nothing kills the script at this assignment, before the emptiness check below
# can explain why. No certificate is the expected first-run state, not a crash.
IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null \
  | grep "Developer ID Application" | head -1 | sed 's/.*"\(.*\)".*/\1/' || true)"

if [ -z "$IDENTITY" ]; then
  cat >&2 <<'EOF'
No "Developer ID Application" certificate in the keychain.

An "Apple Development" certificate is not enough - it only signs builds for
your own devices. Distribution outside the App Store needs Developer ID
Application, included in the membership at no extra cost.

Setup, including the traps, is documented in the mcpb-notarize skill.
EOF
  exit 1
fi
echo "==> identity: $IDENTITY"

if ! xcrun notarytool history --keychain-profile "$KEYCHAIN_PROFILE" >/dev/null 2>&1; then
  cat >&2 <<EOF

No stored notary credentials for profile "$KEYCHAIN_PROFILE".

Store them once. This needs an app-specific password from appleid.apple.com
under Sign-In and Security, so run it yourself - it prompts interactively:

  xcrun notarytool store-credentials "$KEYCHAIN_PROFILE" \\
    --apple-id <your-apple-id> --team-id <your-team-id>
EOF
  exit 1
fi

[ -f "$BINARY" ] || { echo "no binary at $BINARY - run ./build-extension.sh first" >&2; exit 1; }

# --- Sign -------------------------------------------------------------------
#
# --options runtime enables the hardened runtime, which notarization requires.
# --timestamp embeds a trusted timestamp so the signature outlives the cert.
#
# codesign intermittently returns errSecInternalComponent when signing, so retry
# once rather than failing a build over a transient keychain hiccup.

echo "==> signing $(basename "$BINARY")"
if ! codesign --force --timestamp --options runtime \
       --entitlements "$ENTITLEMENTS" --sign "$IDENTITY" "$BINARY" 2>/dev/null; then
  echo "    first attempt failed, retrying once"
  codesign --force --timestamp --options runtime \
    --entitlements "$ENTITLEMENTS" --sign "$IDENTITY" "$BINARY"
fi
codesign --verify --strict --verbose=2 "$BINARY" 2>&1 | sed 's/^/    /'

# --- Verify by running, not by exit code ------------------------------------
#
# codesign exiting 0 does not mean the binary still works. A PyInstaller build
# under the hardened runtime signs cleanly and then refuses to start if the
# entitlements are wrong. Catching that here saves a round trip to Apple.

echo "==> checking the signed binary still runs"
SMOKE_HOME="$(mktemp -d)"
trap 'rm -rf "$SMOKE_HOME"' EXIT
if ! GRANOLA_ROUTER_HOME="$SMOKE_HOME" "$BINARY" poll --help > /dev/null 2>&1; then
  echo "FAIL: the signed binary will not run. Check the entitlements." >&2
  exit 1
fi
echo "    ok"

# --- Pack, in that order ----------------------------------------------------
#
# Packing before signing would notarize a bundle full of unsigned binaries.

echo "==> repacking $(basename "$BUNDLE")"
rm -f "$BUNDLE"
( cd "$ROOT/extension" && zip -qr "$BUNDLE" . )

# --- Notarize ---------------------------------------------------------------

echo "==> submitting to Apple (this takes a few minutes)"
xcrun notarytool submit "$BUNDLE" --keychain-profile "$KEYCHAIN_PROFILE" --wait

# --- Verify the way a user experiences it -----------------------------------
#
# Deliberately NOT spctl. `spctl -a -t install` on a .mcpb always reports
# "rejected: no usable signature", because a .mcpb is a zip and a zip carries
# no signature - the signatures are on the binaries inside. It is the wrong
# tool, and a check that always fails trains you to ignore it.
#
# This reproduces a download instead: extracted files inherit quarantine from
# the archive, which is exactly the condition under test. Before notarization
# this exits 137 in silence; after, it prints help.

#
# The ticket is not stapled - it cannot be, for a bare Mach-O inside a zip - so
# Gatekeeper resolves it online, and that lookup is not instant. Measured: for
# a couple of minutes after Apple returns "Accepted", a freshly quarantined
# copy is still killed. Retry rather than reporting a failure that is only a
# propagation delay, but do eventually fail, because a check that cannot fail
# is worthless.

echo "==> verifying as a downloaded copy would behave"
T="$(mktemp -d)"
PASSED=0
for attempt in $(seq 1 12); do
  rm -rf "${T:?}/x" "$T/home"
  cp "$BUNDLE" "$T/t.mcpb"
  xattr -w com.apple.quarantine "0083;0;Safari;$(uuidgen)" "$T/t.mcpb"
  unzip -qo "$T/t.mcpb" -d "$T/x"
  if GRANOLA_ROUTER_HOME="$T/home" "$T/x/granola-router-mcp" poll --help > /dev/null 2>&1; then
    echo "    PASS: runs under quarantine (after ${attempt}0s)"
    PASSED=1
    break
  fi
  echo "    not yet accepted, waiting for the ticket to propagate (${attempt}/12)"
  sleep 10
done
rm -rf "$T"
if [ "$PASSED" -ne 1 ]; then
  echo "FAIL: a quarantined copy is still killed two minutes after notarization." >&2
  echo "      Exit 137 means Gatekeeper refused it. Check the entitlements and" >&2
  echo "      that the submission really returned Accepted." >&2
  exit 1
fi

echo
echo "signed and notarized: $BUNDLE"
