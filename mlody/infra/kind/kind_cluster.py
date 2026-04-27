"""kind cluster provisioner — five-step idempotent setup.

Entry point: `bazel run //mlody/infra/kind:o-mlody-cluster`

Steps:
  1. Create local Docker registry (create-if-missing / start-if-stopped / force)
  2. Create kind cluster with containerd mirror patch
  3. Configure containerd registry on each node
  4. Connect registry to the kind Docker network
  5. Apply KEP-1755 local-registry-hosting ConfigMap

All external process calls are routed through a RunnerProtocol so tests can
inject a mock without touching subprocess.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import click
import yaml
from rich.console import Console

from .runner import DryRunRunner, RunnerProtocol, SubprocessRunner

# The kind Docker network name is hard-coded per Decision 5 in design.md.
# TODO(future): make this configurable if multi-network topologies are needed.
_KIND_NETWORK = "kind"

console = Console()


# ─── Prerequisite check ───────────────────────────────────────────────────────


def check_prerequisites(runner: RunnerProtocol) -> None:  # noqa: ARG001 — runner reserved for future use
    """Verify kind, docker, and kubectl are on PATH; exit(1) if any is missing."""
    required = {
        "kind": "Install from https://kind.sigs.k8s.io/docs/user/quick-start/#installation",
        "docker": "Install from https://docs.docker.com/engine/install/",
        "kubectl": "Install from https://kubernetes.io/docs/tasks/tools/",
    }
    for tool, hint in required.items():
        if shutil.which(tool) is None:
            print(f"Error: '{tool}' not found on PATH.\n  {hint}", file=sys.stderr)
            sys.exit(1)


# ─── Step 1: Create local Docker registry ─────────────────────────────────────


def step1_create_registry(
    runner: RunnerProtocol,
    registry_name: str,
    registry_port: int,
    force: bool,
) -> str:
    """Create, start, or skip the local Docker registry container.

    Returns one of: "created", "started", "skipped".
    """
    # Inspect returns the container status if it exists, or raises/returns empty.
    try:
        status_output = runner.run_output(
            ["docker", "inspect", "--format", "{{.State.Status}}", registry_name]
        ).strip()
    except RuntimeError:
        # Container does not exist at all.
        status_output = ""

    if status_output and force:
        # Force: delete whatever is there and recreate from scratch.
        runner.run(["docker", "rm", "-f", registry_name])
        status_output = ""

    if not status_output:
        runner.run(
            [
                "docker",
                "run",
                "-d",
                "--restart=always",
                f"-p={registry_port}:5000",
                "--name",
                registry_name,
                "registry:2",
            ]
        )
        return "created"

    if status_output == "running":
        return "skipped"

    # Container exists but is stopped.
    runner.run(["docker", "start", registry_name])
    return "started"


# ─── Step 2: Create kind cluster ──────────────────────────────────────────────


def _build_kind_config(
    registry_name: str,  # noqa: ARG001
    registry_port: int,  # noqa: ARG001
    max_cpus: str | None = None,
    max_memory: str | None = None,
) -> dict[object, object]:
    """Return the kind cluster config dict.

    containerdConfigPatches: uses the containerd >=1.7 hosts.toml approach
    (config_path) so step 3 can write per-registry hosts.toml files.

    kubeadmConfigPatches / KubeletConfiguration: when resource limits are
    given, sets systemReserved = (total − max) so the scheduler sees the
    correct allocatable capacity.  Without this the kubelet reports the
    host's full RAM/CPU regardless of any docker update limits.
    """
    config: dict[object, object] = {
        "kind": "Cluster",
        "apiVersion": "kind.x-k8s.io/v1alpha4",
        "containerdConfigPatches": [
            '[plugins."io.containerd.grpc.v1.cri".registry]\n  config_path = "/etc/containerd/certs.d"'
        ],
    }

    system_reserved: dict[str, str] = {}
    if max_cpus is not None:
        total_cpus = os.cpu_count() or 1
        reserved_m = max(0, int((total_cpus - float(max_cpus)) * 1000))
        if reserved_m > 0:
            system_reserved["cpu"] = f"{reserved_m}m"
    if max_memory is not None:
        reserved_bytes = max(0, _total_memory_bytes() - _parse_docker_mem(max_memory))
        if reserved_bytes > 0:
            system_reserved["memory"] = _bytes_to_k8s_mem(reserved_bytes)

    if system_reserved:
        kubelet_patch = yaml.dump({
            "apiVersion": "kubelet.config.k8s.io/v1beta1",
            "kind": "KubeletConfiguration",
            "systemReserved": system_reserved,
        })
        config["nodes"] = [{"role": "control-plane", "kubeadmConfigPatches": [kubelet_patch]}]

    return config


def step2_create_cluster(
    runner: RunnerProtocol,
    cluster_name: str,
    registry_name: str,
    registry_port: int,
    kubeconfig: str | None,
    save_config: str | None,
    force: bool,
    max_cpus: str | None = None,
    max_memory: str | None = None,
) -> None:
    """Create the kind cluster, skipping if it already exists (unless --force)."""
    existing_clusters = runner.run_output(["kind", "get", "clusters"]).strip()
    cluster_exists = cluster_name in existing_clusters.splitlines()

    if cluster_exists and not force:
        return

    if cluster_exists and force:
        runner.run(["kind", "delete", "cluster", "--name", cluster_name])

    config = _build_kind_config(registry_name, registry_port, max_cpus, max_memory)

    if save_config:
        config_path = save_config
        Path(config_path).write_text(yaml.dump(config))
        _do_create_cluster(runner, cluster_name, config_path, kubeconfig)
    else:
        # Write to a temp file; delete after kind create completes.
        with tempfile.NamedTemporaryFile(
            suffix=".yaml", mode="w", delete=False
        ) as tmp:
            yaml.dump(config, tmp)
            tmp_path = tmp.name
        try:
            _do_create_cluster(runner, cluster_name, tmp_path, kubeconfig)
        finally:
            Path(tmp_path).unlink(missing_ok=True)


def _do_create_cluster(
    runner: RunnerProtocol,
    cluster_name: str,
    config_path: str,
    kubeconfig: str | None,
) -> None:
    cmd = ["kind", "create", "cluster", "--name", cluster_name, "--config", config_path]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    runner.run_output(cmd)


# ─── Step 3: Configure containerd on each node ────────────────────────────────


_HOSTS_TOML_TEMPLATE = '[host."http://{registry_host}:{registry_port}"]\n  capabilities = ["pull", "resolve"]\n'


def step3_configure_containerd(
    runner: RunnerProtocol,
    cluster_name: str,
    registry_name: str,
    registry_port: int,
) -> None:
    """Write hosts.toml to every kind node via docker exec.

    Always re-applies — idempotent because the file is fully overwritten.
    """
    nodes_output = runner.run_output(
        ["kind", "get", "nodes", "--name", cluster_name]
    )
    nodes = [n for n in nodes_output.strip().splitlines() if n]

    # Use the kind network gateway IP — the host as seen from inside the kind
    # containers.  The registry binds to 0.0.0.0 so it's reachable via this IP.
    # Falls back to registry_name for dry-run (no real network exists).
    try:
        gateway = runner.run_output(
            ["docker", "network", "inspect", _KIND_NETWORK,
             "--format", "{{(index .IPAM.Config 0).Gateway}}"]
        ).strip()
    except RuntimeError:
        gateway = ""
    registry_host = gateway if gateway else registry_name

    cert_dir = f"/etc/containerd/certs.d/localhost:{registry_port}"
    toml_content = _HOSTS_TOML_TEMPLATE.format(
        registry_host=registry_host, registry_port=registry_port
    )

    for node in nodes:
        runner.run(["docker", "exec", node, "mkdir", "-p", cert_dir])
        runner.run(
            [
                "docker",
                "exec",
                node,
                "sh",
                "-c",
                f"cat > {cert_dir}/hosts.toml <<'EOF'\n{toml_content}EOF",
            ]
        )


# ─── Step 4: Connect registry to kind Docker network ─────────────────────────


def step4_connect_registry(runner: RunnerProtocol, registry_name: str) -> None:
    """Connect the registry to the kind network, skipping if already connected."""
    if runner.check_connected(registry_name, _KIND_NETWORK):
        return
    runner.run(["docker", "network", "connect", _KIND_NETWORK, registry_name])


# ─── Step 5: Apply local-registry-hosting ConfigMap ──────────────────────────


_CONFIGMAP_TEMPLATE = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: local-registry-hosting
  namespace: kube-public
data:
  localRegistryHosting.v1: |
    host: "localhost:{registry_port}"
    help: "https://kind.sigs.k8s.io/docs/user/local-registry/"
"""


def step6_limit_resources(
    runner: RunnerProtocol,
    cluster_name: str,
    max_cpus: str | None,
    max_memory: str | None,
) -> str:
    """Apply CPU and/or memory limits to every kind node via docker update.

    Returns "skipped" when neither limit is specified.
    Note: limits are per-node (equivalent to a total cap on single-node clusters).
    """
    if not max_cpus and not max_memory:
        return "skipped"

    nodes_output = runner.run_output(["kind", "get", "nodes", "--name", cluster_name])
    nodes = [n for n in nodes_output.strip().splitlines() if n]

    for node in nodes:
        cmd = ["docker", "update"]
        if max_cpus:
            cmd += ["--cpus", max_cpus]
        if max_memory:
            # --memory-swap -1 disables the swap cap; required because kind
            # nodes have an existing memoryswap limit and Docker rejects a
            # memory update unless memoryswap is updated at the same time.
            cmd += ["--memory", max_memory, "--memory-swap", "-1"]
        cmd.append(node)
        runner.run_output(cmd)
    return "applied"


def step5_apply_configmap(
    runner: RunnerProtocol,
    registry_port: int,
    kubeconfig: str | None,
) -> None:
    """Apply the KEP-1755 ConfigMap via kubectl apply -f -.

    kubectl apply is inherently idempotent. YAML content is piped via stdin
    to avoid writing a temporary file.
    """
    yaml_content = _CONFIGMAP_TEMPLATE.format(registry_port=registry_port)
    cmd = ["kubectl", "apply", "-f", "-"]
    if kubeconfig:
        cmd = ["kubectl", "--kubeconfig", kubeconfig, "apply", "-f", "-"]
    runner.run_with_stdin(cmd, yaml_content)


# ─── Top-level provision function ─────────────────────────────────────────────


def provision(
    runner: RunnerProtocol,
    *,
    cluster_name: str,
    registry_name: str,
    registry_port: int,
    kubeconfig: str | None,
    save_config: str | None,
    force: bool,
    max_cpus: str | None = None,
    max_memory: str | None = None,
) -> None:
    """Run all provisioning steps in order.

    Wraps each step in a Rich spinner. Catches errors and exits with code 1
    with step identification so the user knows which step failed.
    """
    check_prerequisites(runner)

    steps: list[tuple[str, object]] = [
        (
            "Step 1: Create registry",
            lambda: step1_create_registry(runner, registry_name, registry_port, force),
        ),
        (
            "Step 2: Create cluster",
            lambda: step2_create_cluster(
                runner, cluster_name, registry_name, registry_port, kubeconfig, save_config, force,
                max_cpus, max_memory,
            ),
        ),
        (
            "Step 3: Configure containerd",
            lambda: step3_configure_containerd(runner, cluster_name, registry_name, registry_port),
        ),
        (
            "Step 4: Connect registry",
            lambda: step4_connect_registry(runner, registry_name),
        ),
        (
            "Step 5: Apply ConfigMap",
            lambda: step5_apply_configmap(runner, registry_port, kubeconfig),
        ),
        (
            "Step 6: Limit resources",
            lambda: step6_limit_resources(runner, cluster_name, max_cpus, max_memory),
        ),
    ]

    # Skip the Rich spinner in dry-run mode so DryRunRunner's stdout output
    # is not captured by the console.status() context manager.
    use_spinner = not isinstance(runner, DryRunRunner)

    for step_name, step_fn in steps:
        try:
            if use_spinner:
                with console.status(f"[bold blue]{step_name}…"):
                    result = step_fn()  # type: ignore[operator]
            else:
                result = step_fn()  # type: ignore[operator]
            _print_step_result(step_name, result)
        except (RuntimeError, OSError) as exc:
            console.print(f"[bold red]✗[/bold red] {step_name}: {exc}")
            sys.exit(1)


def _print_step_result(step_name: str, result: object) -> None:
    if result == "skipped":
        console.print(f"[yellow]⟳[/yellow] {step_name}: skipped")
    elif result is None or result == "created" or result == "started":
        label = f"({result})" if result else ""
        console.print(f"[green]✓[/green] {step_name} {label}".strip())
    else:
        console.print(f"[green]✓[/green] {step_name}")


# ─── Resource helpers ─────────────────────────────────────────────────────────


def _total_memory_bytes() -> int:
    """Return total physical RAM in bytes."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024  # kB → bytes
    except OSError:
        pass
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError):
        return 4 * (1024**3)  # 4 GiB fallback


def _bytes_to_docker_mem(n: int) -> str:
    """Format bytes as a Docker memory string ('4g', '512m')."""
    gib = n // (1024**3)
    if gib >= 1:
        return f"{gib}g"
    return f"{n // (1024**2)}m"


def _bytes_to_k8s_mem(n: int) -> str:
    """Format bytes as a Kubernetes memory quantity ('4Gi', '512Mi')."""
    gib = n // (1024**3)
    if gib >= 1:
        return f"{gib}Gi"
    return f"{n // (1024**2)}Mi"


def _parse_docker_mem(s: str) -> int:
    """Parse a Docker memory string ('4g', '512m') to bytes."""
    s = s.lower().strip()
    for suffix, mult in (("g", 1024**3), ("m", 1024**2), ("k", 1024)):
        if s.endswith(suffix):
            return int(s[:-1]) * mult
    return int(s)


# ─── Resource defaults ────────────────────────────────────────────────────────


def _half_cpus() -> str:
    """Return half the logical CPU count as a string, minimum 1."""
    count = os.cpu_count() or 2
    return str(max(1, count // 2))


def _half_memory() -> str:
    """Return half of physical RAM in Docker memory format (e.g. '4g', '512m')."""
    return _bytes_to_docker_mem(_total_memory_bytes() // 2)


# ─── CLI entry point ──────────────────────────────────────────────────────────


@click.command()
@click.option("--cluster-name", default="mlody", show_default=True, help="Name of the kind cluster.")
@click.option(
    "--registry-name",
    default="kind-registry",
    show_default=True,
    help="Name of the Docker registry container.",
)
@click.option(
    "--registry-port",
    default=5001,
    show_default=True,
    type=int,
    help="Host port the registry listens on.",
)
@click.option(
    "--kubeconfig",
    default=None,
    help="Path to kubeconfig file override.",
)
@click.option(
    "--save-config",
    default=None,
    help="Persist generated kind YAML to this path.",
)
@click.option("--dry-run", is_flag=True, default=False, help="Print commands without executing.")
@click.option("--verbose", is_flag=True, default=False, help="Increase output verbosity.")
@click.option("--quiet", is_flag=True, default=False, help="Suppress non-essential output.")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Delete and recreate the kind cluster and registry.",
)
@click.option(
    "--max-cpus",
    default=None,
    help="CPU limit per node, e.g. 2 or 0.5. Default: half of available cores.",
)
@click.option(
    "--max-memory",
    default=None,
    help="Memory limit per node, e.g. 4g or 512m. Default: half of physical RAM.",
)
def main(
    cluster_name: str,
    registry_name: str,
    registry_port: int,
    kubeconfig: str | None,
    save_config: str | None,
    dry_run: bool,
    verbose: bool,
    quiet: bool,
    force: bool,
    max_cpus: str | None,
    max_memory: str | None,
) -> None:
    """Provision a local kind cluster with a connected Docker registry."""
    global console  # noqa: PLW0603 — replaced at startup based on --quiet flag
    console = Console(quiet=quiet)

    if max_cpus is None:
        max_cpus = _half_cpus()
    if max_memory is None:
        max_memory = _half_memory()

    if dry_run:
        runner: RunnerProtocol = DryRunRunner()
    else:
        runner = SubprocessRunner(verbose=verbose)

    provision(
        runner,
        cluster_name=cluster_name,
        registry_name=registry_name,
        registry_port=registry_port,
        kubeconfig=kubeconfig,
        save_config=save_config,
        force=force,
        max_cpus=max_cpus,
        max_memory=max_memory,
    )


if __name__ == "__main__":
    main()
