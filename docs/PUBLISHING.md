# Publishing RPM releases

`publish.yml` is the only path that writes to the repository. It runs by
`workflow_dispatch` from protected `main` and takes the channel, the package, the
version, the tag, the commit, and the source run and attempt of the release.

## What the workflow does

1. **Run tests** runs the unit and contract tests in a Fedora 45 container.
2. **Verify release** has no credentials. It downloads both packages and
   `checksums.txt` from the GitHub release of the tag, checks the checksums, reads
   each package header, and builds the release manifest from the verified source
   run and its artifact listing. It also reads the channel as served publicly and
   verifies it, so a damaged channel stops the run before any secret is used.
3. **Issue credentials** runs in the `issuer-<channel>` environment and creates
   short-lived R2 credentials limited to that channel's prefix.
4. **Sign and publish** runs in the `publisher-<channel>` environment inside a
   Fedora 45 container. It repeats the verification, signs the new packages with the
   channel's signing subkey, rebuilds the metadata, uploads, and checks every object
   through the public domain.

## What is published

```
<channel>/45/<arch>/Packages/*.rpm
<channel>/45/<arch>/repodata/...      repomd.xml.asc is a detached signature
<channel>/luminus-imports.json        signed ledger of what was imported
<channel>/luminus.asc                 public key of the channel
<channel>/luminus-<channel>.repo      DNF repository file
```

Packages are uploaded first, then metadata, then `repomd.xml`, its signature and the
ledger last. A client therefore never sees metadata that points at a missing package.

## Rules the importer enforces

- A version must be newer than everything already in the channel. The same release
  imported again changes nothing. The same version with different content, or an older
  version, is rejected.
- Release candidates (`1.2.3-rc.1`, stored as `1.2.3~rc.1`) can only go to `testing`.
- Packages that ship `/etc/sirius/distro.toml` or a polkit rule are rejected, because
  a plain install must not point the installer at an image.
- The importer verifies the repomd signature, the primary metadata hash, every
  package hash and the signed ledger of what the channel serves today before it adds
  anything. A mismatch stops the run and writes nothing.
- Two channels never share a signing key. A package signed for one channel does not
  verify against the other.

## Running it

Open Actions, choose "Publish RPM release", select `testing` for a release
candidate or `stable` for a final release, and fill in the values of the Sirius
release run. Stable runs need the reviewer approval configured on the environment.

## Limits

The release packages are checked against the checksums published on the same GitHub
release, and the release is tied to a successful source run, a tag that points at the
descriptor commit and the expected workflow by the issuer. Someone who can rewrite
release assets and the tag of the Sirius repository can therefore still publish
different packages; protect that repository accordingly. A signed channel can also be
replayed to an older state by whoever controls the bucket, so clients should rely on
newer-version checks in DNF rather than on the repository alone.
