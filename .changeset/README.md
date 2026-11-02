# Changesets

Run `pnpm changeset` in a feature or fix branch, select `lg-luma`, choose a patch, minor or major
bump, and describe the user-facing change. Commit the generated Markdown file with the PR.
Documentation-only and maintenance PRs that do not need a release can omit a changeset.

PRs receive an updating Changesets status comment. It reports the requested version bump or
notes that no changeset was committed and no version bump is requested. Code or behavior changes
should include a changeset: patch for compatible fixes, minor for compatible features, or major
for breaking changes. The comment is informational and also works on fork PRs.

Merged changesets accumulate here until someone runs the **Release** workflow (Actions tab). Its version command
updates `package.json`, `CHANGELOG.md`, `appinfo.json`, the root `Cargo.toml`, the `luma` entry in
`Cargo.lock`, and the versioned IPK examples in `readme.md`. Supporting Rust crates keep their
own versions. Do not run `pnpm version:release` in ordinary feature PRs.

The Release workflow validates the bumped commit with the full CI build before publishing the
verified IPK and Homebrew feed. Merges and manual CI runs never publish.
See [the release guide](../development.md#releases) for repository setup and details.
