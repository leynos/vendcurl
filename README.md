# vendcurl

`vendcurl` is a tiny curl-ish command for setup scripts and CI bootstrap code. It downloads a URL, validates the resulting file, writes a content-addressed blob into an S3-compatible object store, writes a URL manifest, and prefers that vendored blob on later runs.

It is deliberately boring: SHA-256 does the identity work; archive validators only check structure and obvious archive-path nastiness.

## Install with uv

From this source tree:

```bash
uv tool install .
```

From a git repository once you have committed it somewhere:

```bash
uv tool install git+https://github.com/you/vendcurl
```

For local hacking without installing:

```bash
uv run vendcurl --help
```

## S3-compatible configuration

You can pass options each time:

```bash
vendcurl \
  --bucket my-vendor-cache \
  --endpoint-url https://s3.eu-west-2.amazonaws.com \
  --prefix sandbox-artifacts \
  --sha256 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  https://example.test/releases/tool-1.2.3.tar.gz \
  vendor/tool-1.2.3.tar.gz
```

Or use environment variables:

```bash
export VENDCURL_BUCKET=my-vendor-cache
export VENDCURL_PREFIX=sandbox-artifacts
export VENDCURL_ENDPOINT_URL=https://s3.eu-west-2.amazonaws.com
export AWS_PROFILE=dev

vendcurl https://example.test/releases/tool-1.2.3.zip vendor/tool.zip
```

The supported environment variables are:

- `VENDCURL_BUCKET` or `S3_BUCKET`
- `VENDCURL_PREFIX`
- `VENDCURL_ENDPOINT_URL`, `AWS_ENDPOINT_URL_S3`, or `S3_ENDPOINT_URL`
- `VENDCURL_REGION`, `AWS_REGION`, or `AWS_DEFAULT_REGION`
- `VENDCURL_PROFILE` or `AWS_PROFILE`
- `VENDCURL_RETRIES`
- `VENDCURL_TIMEOUT`
- `VENDCURL_USER_AGENT`

Credentials are whatever `boto3` can already find: environment variables, profiles, instance metadata, OIDC-derived credentials, and so on.

## Behaviour

On the first run, `vendcurl`:

1. Downloads the origin URL.
2. Computes SHA-256 while writing the file.
3. Checks `--sha256` if you supplied it.
4. Chooses a validator from the filename/content type unless you set `--validator`.
5. Uploads the blob to `s3://$bucket/$prefix/blobs/sha256/<first-two-hex>/<sha256>`.
6. Writes a URL manifest to `s3://$bucket/$prefix/manifests/by-url/<sha256(url)>.json`.
7. Atomically moves the validated file into your requested output path.

On later runs, it reads the URL manifest first, downloads the content-addressed blob from S3, re-checks SHA-256 locally, validates the file again, and only goes back to the origin if the cache entry is missing or stale.

## Validators

`--validator auto` recognizes:

- tar archives: `.tar`, `.tar.gz`, `.tgz`, `.tar.xz`, `.txz`, `.tar.bz2`, `.tbz`, `.tbz2`
- zip archives: `.zip`, `.whl`, `.jar`
- single compressed streams: `.gz`, `.xz`, `.bz2`

You can force a validator with `--validator tar`, `--validator zip`, `--validator gzip`, `--validator xz`, `--validator bz2`, or disable structural checks with `--validator none`.

By default, tar and zip validation rejects archive member names that look dangerous for later extraction, such as absolute paths, Windows drive paths, and `..` traversal. Use `--allow-unsafe-archive-paths` only for fossilized upstreams that you have inspected and deliberately trust.

## Useful modes

Force origin refresh and update the manifest:

```bash
vendcurl --refresh https://example.test/tool.tar.gz vendor/tool.tar.gz
```

Use only the vendored copy, never the origin:

```bash
vendcurl --no-origin --sha256 "$digest" https://example.test/tool.tar.gz vendor/tool.tar.gz
```

Smoke-test a URL without S3:

```bash
vendcurl --no-store --validator zip https://example.test/file.zip ./file.zip
```

Emit machine-readable output:

```bash
vendcurl --json https://example.test/file.zip ./file.zip
```

## Development

The unit tests are stdlib-only:

```bash
PYTHONPATH=src python -m unittest discover -s tests
```

If you have `uv` available:

```bash
uv run python -m unittest discover -s tests
```
