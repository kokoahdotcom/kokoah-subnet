"""The approved harness images (spec v6, section 16): container image digests of released, reproducible builds of
`deploy/agent`. A sealed machine counts as an agent only when its attested image is one of these. The list is part
of the validator code, so every validator approves the same images; a release adds its digest here, with the commit
it is built from: `deploy/agent/build.sh <commit>` rebuilds it and prints the same digest (this file is left out of
the image, so approving an image does not change it)."""

APPROVED_HARNESS_IMAGES: tuple[str, ...] = (
    # harness 1.0.0, commit 8284fd628cbf, docker.io/kokoahdotcom/kokoah-subnet-agent; tested on Intel TDX
    "sha256:3ee68dc476bc0bd4f83c5cc8938917059ce5ae99ea0f5097268b02970f87692f",
)
