"""The approved harness images (spec v6, section 16): container image digests of released, reproducible builds of
`deploy/agent`. A sealed machine counts as an agent only when its attested image is one of these. The list is part
of the validator code, so every validator approves the same images; a release adds its digest here, with the commit
it is built from: `deploy/agent/build.sh <commit>` rebuilds it and prints the same digest (this file is left out of
the image, so approving an image does not change it)."""

APPROVED_HARNESS_IMAGES: tuple[str, ...] = (
    # harness 1.0.0, commit c5dd5ef24705, docker.io/kokoahdotcom/kokoah-subnet-agent; tested on Intel TDX
    "sha256:80746d9aa5d401b477b251f20f0c96067306f0cd077703a5374cd8f4db1da468",
)
