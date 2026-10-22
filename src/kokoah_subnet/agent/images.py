"""The approved harness images (spec v6, section 16): container image digests of released, reproducible builds of
`deploy/agent`. A sealed machine counts as an agent only when its attested image is one of these. The list is part
of the validator code, so every validator approves the same images; a release adds its digest here."""

APPROVED_HARNESS_IMAGES: tuple[str, ...] = ()
