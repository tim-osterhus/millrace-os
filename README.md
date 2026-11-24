<div align="center">
  <p>
    <a href="https://pypi.org/project/millrace-os/"><img alt="PyPI v0.23.2" src="https://img.shields.io/badge/pypi-v0.23.2-orange.svg"></a>
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

Millrace OS is a local service, command-line client, and dashboard for operating independent Millrace workflow instances. Inspect runs, submit exact control requests, and publish workflow edits through one authenticated loopback API.

Core owns instance inventory, execution, receipts, planes, and writer state. OS owns named state-home selections, drafts, and the lifetime of its controller calls.

## Install

Millrace OS 0.23.2 requires Python 3.12 or newer. Install the base service with:

```bash
python -m pip install 'millrace-os==0.23.2'
```

For the router operations below, install the router extra:

```bash
python -m pip install 'millrace-os[router]==0.23.2'
```

The router extra selects `millrace-plus[router]>=0.23.2,<0.24.0`, which brings in Core. The 0.23.2 release pairs Core, Plus, and OS 0.23.2 with the `millrace-router-execution/v2` contract. The base service starts independently of Core and Plus. Router operations require compatible components.

The optional `mcp` extra provides a foreground bridge for Python 3.12 and 3.13. See [MCP configuration and transports](https://github.com/tim-osterhus/millrace-os/blob/v0.23.2/docs/mcp.md).

## Operate a Core home

On Linux, start the foreground service without a PID file. Send the first shutdown signal with Ctrl+C in the serving terminal.

```bash
millrace-os serve --no-pid-file --router-state-home main=/absolute/path/to/core-state
```

On macOS, the following command records the native process identity for verified `millrace-os stop` from another terminal.

```bash
millrace-os serve --router-state-home main=/absolute/path/to/core-state
```

The service creates an owner-only bearer credential. Open the stock dashboard from another terminal to choose an instance and run. The dashboard shows the run's graph, activity, reports, revisions, and available controls. See the [stock dashboard guide](https://github.com/tim-osterhus/millrace-os/blob/v0.23.2/docs/dashboard.md).

```bash
millrace-os open --stock --home main
```

The browser is optional. Authenticated clients of a service share its saved
presentation preferences. OS stores these preferences in its configuration
directory, separate from execution settings and control requests. See the
[presentation guide](https://github.com/tim-osterhus/millrace-os/blob/v0.23.2/docs/presentation.md).

The headless client uses the same API. See [headless router operation](https://github.com/tim-osterhus/millrace-os/blob/v0.23.2/docs/router-operation.md) for control requests and deterministic authoring.

```bash
millrace-os router instances --home main
millrace-os router inspect --home main --instance INSTANCE_ID --run RUN_ID
millrace-os router request --home main --file saved-request.json
```

## Try the zero-key demo

Install the matching Core, Plus, and OS packages, then run the demo without a model or provider key.

```bash
python -m pip install 'millrace-ai==0.23.2' 'millrace-plus==0.23.2' 'millrace-os==0.23.2'
millrace demo --no-open --output ./millrace-demo-results
```

Open the printed loopback URL. After the report appears, select **Finish** and wait for the terminal to report completion. The output directory contains `report.html`, `report.json`, and `execution.json`. The demo owns a separate temporary service and workspace. See [owned demo sessions](https://github.com/tim-osterhus/millrace-os/blob/v0.23.2/docs/demo.md).

## Receipts and controller ownership

A request file contains the complete `millrace-router-request/v2` envelope, including its request ID, target IDs, expected state version, and payload. OS forwards it unchanged. A queued receipt means Core admitted the request. It does not mean the request applied or its writers stopped.

OS tracks every public call that could acquire Core's controller lock. The first shutdown requests pause and drains those calls before closing HTTP and local storage. A second deliberate shutdown requests Core cancellation. Unrelated provider-hosted processes retain their own lifecycle.

Workflow Apply freezes a draft, pauses its exact run, observes quiescence and final call drain, validates, publishes the files, and submits the revision. It leaves the run paused with a pending revision. Acceptance requires an explicit later request. Core decides whether acceptance can continue runnable planes.

## Source boundaries

- `router_client.py` calls public Plus controls and catalog functions.
- `router_workers.py` owns bounded controller and control-ingress calls.
- `workflow_drafts.py` stores drafts and serializes cooperating OS publications.
- `api.py`, `contracts.py`, and `cli.py` define the shared HTTP and headless client surface.

The [API](https://github.com/tim-osterhus/millrace-os/blob/v0.23.2/docs/api-foundations.md) and [service foundations](https://github.com/tim-osterhus/millrace-os/blob/v0.23.2/docs/foundation.md) document trust, storage, and recovery. The package includes prebuilt dashboard assets and [license notices](https://github.com/tim-osterhus/millrace-os/blob/v0.23.2/LICENSE).
