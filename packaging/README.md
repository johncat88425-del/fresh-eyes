# Linux portability check

For the separate public-source ZIP and its path/metadata handling, see [PUBLIC_SOURCE.md](PUBLIC_SOURCE.md).

This verifies the core, default API/frontend, and static rebuild in a fresh Linux container. It does not run a third-party repository, make an inference request, or count as a repair benchmark. Another physical machine and a real runner test remain separate checks.

Requires a working Docker engine. Build the toolchain once; this step downloads official Debian packages:

```sh
docker build -t fresh-eyes-portability -f packaging/portability.Dockerfile packaging
docker image inspect fresh-eyes-portability --format '{{.Id}}'
```

Use the full immutable image ID printed above:

```sh
python3.12 scripts/check-portability.py \
  --archive dist/fresh-eyes-teammate.zip \
  --image 'sha256:<64 hex characters>' \
  --output evidence/portability-new.json --execute
```

On Chris's prepared machine use `--docker scripts/docker-local`; other machines use their own `docker`. No private workspace/runtime path is required by the checker. Without `--execute`, it only prints a zero-call preview.

The source ZIP is sent over stdin and checked against its manifest. Reports, keys and databases are excluded from the extracted test tree. The run has `network=none`, a read-only root, temporary writable workspaces, no host mounts, no exposed ports, no Docker socket and no provider credentials. It rebuilds the demo, runs Python/Node tests and checks the default API and frontend over container loopback, then removes the container. Results and cleanup status are saved even on failure; existing output evidence is never overwritten.

The toolchain image is separate from the immutable image used for repository attempts. Installing its Git/Node tools is preparation, not evidence that an upstream repository installed successfully.
