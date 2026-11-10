# Using the Luminus RPM repository

Fedora 45 on x86_64 and aarch64.

## Install

```sh
sudo dnf config-manager addrepo --from-repofile=https://rpm.luminusos.org/stable/luminus-stable.repo
sudo dnf install sirius-installer
```

DNF asks you to confirm the repository key the first time. Its fingerprint is
`92F1F4F04BC9E2A3C44CC2D35AF0A19B97265C0D`. Check it against `trust/stable.asc`
in this repository before you accept.

The repository file turns on both `gpgcheck` and `repo_gpgcheck`, so DNF verifies
the signature of the metadata and of every package.

## Testing channel

Release candidates are published to `testing` only:

```sh
sudo dnf config-manager addrepo --from-repofile=https://rpm.luminusos.org/testing/luminus-testing.repo
```

`testing` has its own key, `11DDF884BFC46A5C548304028B3CED01A7D81531`. Candidate
versions use a tilde (`1.2.3~rc.1`), which RPM orders before `1.2.3`, so DNF offers the
final release as an update. Both repositories can be enabled together.

## Configuration

The package installs `/etc/sirius/sirius.toml` and keeps your edits on upgrade. It
does not install `/etc/sirius/distro.toml`. Copy
`/usr/share/doc/sirius/examples/distro.toml` and set the image for your
distribution before running an installation.
