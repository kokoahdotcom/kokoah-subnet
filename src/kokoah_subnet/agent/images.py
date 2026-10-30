"""The approved harness images (spec section 16): container image digests of released, reproducible builds of
`deploy/agent`. A sealed machine counts as an agent only when its attested image is one of these. The list is part
of the validator code, so every validator approves the same images; a release adds its digest here, with the commit
it is built from: `deploy/agent/build.sh <commit>` rebuilds it and prints the same digest (this file is left out of
the image, so approving an image does not change it)."""

# Harness 1.1.0 (sha256:ad1dac18…0c0b) is retired: it took the key service from a start setting, so whoever started a
# machine could put a relay in its place. Every approved image names the key service in its own settings.
APPROVED_HARNESS_IMAGES: tuple[str, ...] = ()
