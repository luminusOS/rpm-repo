# RPM hosting

Hosting was provisioned and tested on 2026-10-21. This stage does not publish
packages or make the DNF repository installable. `hosting.json` describes desired
policy; its `planned` state is not a live resource inventory.

## Resources and cache

- R2 Standard bucket: `luminus-rpm`.
  Bucket ID: `669286543bea4dc88438803b27ed78db`; location ENAM, default jurisdiction.
- Public custom domain: `rpm.luminusos.org`, minimum TLS 1.2; `r2.dev` disabled.
- Channels: `stable/` and `testing/` in the same bucket, targeting Fedora 45.
  Credentials and signing are separate work; prefixes alone do not enforce write
  isolation.
- Default: bypass edge cache and send `Cache-Control: no-store`.
- Only `.rpm` objects under `/{stable,testing}/45/{x86_64,aarch64}/` are cacheable,
  with a 31536000-second immutable TTL. Metadata and signatures outside these
  paths remain uncached. Error responses must not be cached.

Cache Rules control the edge separately from response-header rules. Rules must
match only the two package hosts, never the whole zone or the website. Do not
replace existing entrypoints without inspecting and preserving their rules.

The zone entrypoints are `da961a85b0d545eb89e3aee699fd1170` for Cache Rules and
`b52b3e76042a45ffb0a316aba2d35479` for response headers. Both serve only the two
package hosts.

## Live verification

The bounded T05 run verified TLS, HEAD/GET and SHA-256 for synthetic objects.
Stable x86_64 and testing aarch64 immutable GETs returned `HIT` at GRU. Mutable
metadata updates were visible with `no-store`; a missing immutable object returned
404 before upload and 200 after creation without a purge. HEAD responses were
`DYNAMIC`, including when the corresponding GET was `HIT`.

Across both repositories, 14 uploads transferred 490 bytes to nine unique keys.
All nine keys were deleted and only their exact URLs purged. Subsequent HEAD/GET
responses were 404 with `no-store`, and both bucket listings were empty. No test
tokens were created. These checks do not prove behavior at every edge location,
RPM signatures, valid RPM contents, or DNF installation/update support.

The run used 128 of 200 authorized requests, including eight independent
management readbacks that confirmed cleanup and the live hosting policy.

## Check the configuration

```sh
.venv/bin/python -B -m unittest discover -s tests -p 'test_*.py' -v
.venv/bin/python tools/hosting.py validate
.venv/bin/python tools/hosting.py probe https://rpm.luminusos.org/stable/45/x86_64/_t05/NONCE/immutable.rpm
```

Use the probe command only with an authorized, owned synthetic object; the
`.rpm` suffix in this example is not an installable package. Unit tests do not
access Cloudflare. Public probes verify certificates, must not follow redirects
to another host, and must not require login, JavaScript or a challenge. A 404 at
the bucket root does not validate or invalidate the repository.

## Bounded live test and cleanup

The authorized T05 run permits at most 200 operation/test requests and 64 KiB
of synthetic upload data, with 40 requests reserved for cleanup. Test actual
objects, HEAD/GET, byte integrity, mutable replacement, negative-cache recovery,
and immutable cache hits. Keep fixtures under an owned `_t05/NONCE/` path;
do not upload real repodata or packages.

Delete only the exact keys created by the run. Purge only their exact public
URLs and verify absence; do not empty buckets or delete prefixes. Buckets,
domains, and approved rules remain. Revoke only credentials created for this
test, if any. Stop on budget exhaustion or a required billing/plan change.

R2 Standard's monthly free tier is shared across the account and is not a billing
cap. Current storage does not prove remaining request quota. Consult
[R2 pricing](https://developers.cloudflare.com/r2/pricing/) before another run.
