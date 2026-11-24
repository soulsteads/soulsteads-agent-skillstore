# Run Agent Skillstore in a container

Build a Linux image from this repository with Docker or Podman. Run these
commands from the root of a Git checkout that contains `Dockerfile`. You need
a running container engine and network access for the public build dependencies.
No registry account or host language toolchain is required; the build pulls
public base images. Use `engine=podman` in place of `engine=docker` for all
commands below.

For the loopback-only port mapping in this guide, use Docker Engine 28.0.0 or
newer, or Podman. With Docker, check the server version using `docker version`.
Do not use older Docker Engine versions for this recipe: a port published to
`127.0.0.1` is not guaranteed to stay on the host. See Docker’s
[port-publishing guidance](https://docs.docker.com/engine/network/port-publishing/#publishing-ports).

```sh
engine=docker                   # or: engine=podman
image=localhost/soulsteads-agent-skillstore:local
"$engine" build --tag "$image" .
"$engine" volume create soulsteads-agent-skillstore-data
```

The image runs as UID/GID `10001`, without root. A fresh engine-managed volume
at `/data` inherits the image's private directory and avoids bind-mount UID and
SELinux problems. If using a bind mount instead, provision a dedicated directory
writable by the mapped container user, mode `0700`, with suitable SELinux labeling
(e.g. a private `:Z` label on Podman); do not make it world-writable. Don't mount
a host home or the container-engine socket.

## Initialize and serve a private registry

Initialize an empty volume once. `fixture-reviewer` is an example declared local
reviewer; choose your actual registry reviewer before sharing it.

```sh
"$engine" run --rm --network none -v soulsteads-agent-skillstore-data:/data \
  "$image" init /data/registry --reviewer fixture-reviewer
"$engine" run --rm --network none -v soulsteads-agent-skillstore-data:/data \
  "$image" verify
"$engine" run -d --name soulsteads-agent-skillstore \
  -v soulsteads-agent-skillstore-data:/data -p 127.0.0.1:8765:8765 \
  "$image" serve --host 0.0.0.0 --port 8765 --private-read
"$engine" logs soulsteads-agent-skillstore
```

The listener above is the **registry RPC** (`POST /v1/rpc`). The wildcard bind
is inside the container; the host port is loopback-only. Keep the container on
a network with only trusted peers. `--private-read` denies anonymous reads. Use
a trusted TLS reverse proxy for remote access; never publish the raw port on
all interfaces. To issue a scoped test token without printing it, use the CLI
locally in the running container and copy the file into a private directory
(`CLIENT_DIR` must be a new directory outside the checkout):

```sh
(umask 077; mkdir "$CLIENT_DIR")
"$engine" exec soulsteads-agent-skillstore /opt/skillstore/bin/soulsteads-skillstore \
  --kind agent token catalog-client --token-file /data/client.token
"$engine" cp soulsteads-agent-skillstore:/data/client.token "$CLIENT_DIR/client.token"
chmod 600 "$CLIENT_DIR/client.token"
"$engine" exec soulsteads-agent-skillstore rm /data/client.token
```

Use that file with the authenticated `search` example in the [operator
guide](usage.md#shared-registries). Keep reviewer and agent credentials
separate. Filesystem access to the registry is trusted operator access; do not
share the volume with agent clients.

## MCP and working with local files

For a harness on another host, prefer its own local Skillstore installation for
stdio MCP, tracking-only backups, or installs into that host's skill directories.
If using container stdio, use `run --rm -i` **without** `-t` and mount only the
intended target at an absolute path; a container cannot update an unmounted
host directory. Follow [MCP](usage.md#mcp) for its arguments and trust boundary.

The `serve-mcp` listener is **not** this registry RPC. It hard-binds to loopback
and validates the `Host` header; `-p` on a bridged container will not work for it.
To run it on native Linux, use the same image and registry with `--network host`
and `serve-mcp --port 8766` (no `-p`), replacing or deliberately managing a
separate listener. Keep the authorization, TLS and upstream `Host` settings in
the [Streamable HTTP setup](usage.md#connect-directly-over-streamable-http).

## Retain state

Stop the writer before taking a private backup of the registry volume. Preserve
registry data, target directories and tracking snapshots separately according to
[Recovery and backup](usage.md#recovery-and-backup). Restore a compatible backup
before rolling back across a storage format change. Do not remove the volume
when recreating the container.

To stop and remove just the container (the named volume remains):

```sh
"$engine" stop --time 30 soulsteads-agent-skillstore
"$engine" rm soulsteads-agent-skillstore
```

Then build the desired source revision and rerun the start command with the same
volume. Reuse a stable image tag or digest and the same deployment settings when
supervising it with your container engine or service manager. First test on a
fresh disposable volume. Run the offline image smoke locally with:

```sh
sh scripts/smoke-container.sh "$engine" "$image"
```
