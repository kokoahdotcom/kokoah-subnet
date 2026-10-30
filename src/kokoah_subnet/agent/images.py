"""The approved harness images (spec section 16): container image digests of released, reproducible builds of
`deploy/agent`. A sealed machine counts as an agent only when its attested image is one of these. The list is part
of the validator code, so every validator approves the same images; a release adds its digest here, with the commit
it is built from: `deploy/agent/build.sh <commit>` rebuilds it and prints the same digest (this file is left out of
the image, so approving an image does not change it)."""

# Retired images, never approved again:
# - harness 1.1.0 (sha256:ad1dac18…0c0b): it took the key service from a start setting, so whoever started a machine
#   could put a relay in its place. Every approved image names the key service in its own settings.
# - harness 1.2.0 (sha256:4c4576cf…a527, commit 982331b3d924): its sandbox started with the bundle on Python's import
#   path, so a bundle file named like a module the sandbox imports ran as root before the sandbox was in place, and
#   could take the account key or ask the machine for tokens. Accounts it ran earned no trust from it.
APPROVED_HARNESS_IMAGES: tuple[str, ...] = ()
