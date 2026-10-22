<div align="center">
  <p>
    <a href="https://pypi.org/project/millrace-os/"><img alt="PyPI v0.23.0" src="https://img.shields.io/badge/pypi-v0.23.0-orange.svg"></a>
    <a href="https://www.python.org/downloads/"><img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12+-blue.svg"></a>
    <a href="https://github.com/tim-osterhus/millrace-os/blob/main/LICENSE"><img alt="Apache-2.0 license" src="https://img.shields.io/badge/license-Apache--2.0-green.svg"></a>
  </p>
  <img
    src="https://raw.githubusercontent.com/tim-osterhus/millrace/main/docs/assets/images/millrace-icon-signal-transparent-glow.png"
    alt="Millrace signal mark"
    width="180"
  />
  <h1>Millrace OS</h1>
</div>

Millrace OS is a local service and command-line client for operating independent Millrace workflow instances.

Use it to inspect runs, submit exact control requests, and publish workflow edits through one authenticated loopback API. Core owns instance inventory, execution, receipts, planes, and writer state. OS owns named state-home selections, drafts, and the lifetime of its controller calls.

Millrace OS 0.23.0 requires Python 3.12 or later. Native daemon identity and stop controls support macOS; other platforms fail closed for those controls.

Install the base service independently, or include the optional public router controls:

```bash
python -m pip install 'millrace-os==0.23.0'
python -m pip install 'millrace-os[router]==0.23.0'
```

The router extra selects `millrace-plus[router]>=0.23.0,<0.24.0`, which supplies Core transitively. Base service startup does not import Core or Plus. Router operations refuse unavailable or incompatible components. The selected release uses Core, Plus, and OS 0.23.0 with `millrace-router-execution/v2`. Matching version numbers do not establish execution-contract compatibility or native provider qualification.

The explicit optional `mcp` extra adds a foreground bridge to an already running OS API, using Python 3.12/3.13 and official MCP SDK 1.30.0. See [MCP configuration and recovery](docs/mcp.md) for seven fixed tools, local stdio, the trusted shared HTTP boundary and saved-request replay. Base installation remains independent of MCP.

Start the installed service and inspect a configured Core home:

```bash
millrace-os serve --router-state-home main=/absolute/path/to/core-state
millrace-os router instances --home main
millrace-os router inspect --home main --instance INSTANCE_ID --run RUN_ID
millrace-os router request --home main --file saved-request.json
```

The service creates an owner-only bearer credential. The client reads that configured credential and sends it only to the explicit loopback endpoint. See [router operation](https://github.com/tim-osterhus/millrace-os/blob/v0.23.0/docs/router-operation.md) for authoring and control examples.

## Receipts and controller ownership

A request file contains the complete `millrace-router-request/v2` envelope, including its request ID, target IDs, expected state version, and payload. OS forwards it unchanged. A queued receipt means Core admitted the request. It does not mean the request applied or its writers stopped.

OS tracks every public call that could acquire Core's controller lock. The first shutdown requests pause and drains those calls before closing HTTP and local storage. A second deliberate shutdown requests Core cancellation. Unrelated provider-hosted processes retain their own lifecycle.

Workflow Apply freezes a draft, pauses its exact run, observes quiescence and final call drain, validates, publishes the files, and submits the revision. It leaves the run paused with a pending revision. Acceptance requires an explicit later request. Core decides whether acceptance can continue runnable planes.

## Source boundaries

- `router_client.py` calls public Plus controls and catalog functions.
- `router_workers.py` owns bounded controller and control-ingress calls.
- `workflow_drafts.py` stores drafts and serializes cooperating OS publications.
- `api.py`, `contracts.py`, and `cli.py` define the shared HTTP and headless client surface.

The browser is optional. Existing bootstrap and recovery pages remain available, and deterministic authoring needs neither a browser nor a model. Read [API foundations](https://github.com/tim-osterhus/millrace-os/blob/v0.23.0/docs/api-foundations.md), [service foundations](https://github.com/tim-osterhus/millrace-os/blob/v0.23.0/docs/foundation.md), and [legacy disposition](https://github.com/tim-osterhus/millrace-os/blob/v0.23.0/docs/architecture/legacy-disposition.md) for the retained trust and storage contracts. The package uses the [Apache 2.0 license](https://github.com/tim-osterhus/millrace-os/blob/v0.23.0/LICENSE).
