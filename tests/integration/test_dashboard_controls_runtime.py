"""UI3 exact control requests over the installed public Core/Plus controls.

Each case posts the exact bytes a reviewed dashboard request retains and reads
only the strict bounded receipt view the dashboard uses. A stale reviewed
request is refused under its original identity; a lost answer is found by its
exact ID and target; an exact replay returns the recorded receipt and changes
no state; Start association comes only from Core's receipt.
"""

from __future__ import annotations

import json

from millrace_os.router_client import request_digest

VIEWS = "/api/v1/router/views/instances"
STRICT = "/api/v1/router/views/requests/"


def exact(request: dict) -> bytes:
    """The POST body a reviewed dashboard request serializes once and retains."""
    return json.dumps({"home": "default", "request": request}).encode()


def post(h, raw: bytes):
    return h.http.post(
        "/api/v1/router/requests",
        content=raw,
        headers={"Content-Type": "application/json"},
    )


def strict(h, request_id, instance, run=None):
    params = {"home": "default", "instance_id": instance}
    if run is not None:
        params["run_id"] = run
    return h.http.get(STRICT + request_id, params=params)


def settled_receipt(h, request, instance, run=None):
    def final():
        response = strict(h, request["request_id"], instance, run)
        if response.status_code == 404:
            return None
        assert response.status_code in (200, 202), response.text
        receipt = response.json()["core_receipt"]
        return response.json() if receipt and receipt["status"] != "queued" else None

    return h.until(final, timeout=15)


def controls(h, instance, run):
    response = h.http.get(
        f"{VIEWS}/{instance}/runs/{run}",
        params={"home": "default", "section": "controls"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["items"]


def paused_run(h):
    workflow, project = h.flow(pause=True)
    instance = h.create(workflow, project)
    run = h.start_run(instance)["run_id"]
    h.settled(instance, run, "paused")
    return instance, run


def cancel(h, instance, run):
    assert h.send(h.request("cancel_run", instance, run))["status"] == "applied"
    h.settled(instance, run, "cancelled")


def test_a_stale_reviewed_request_is_refused_under_its_original_identity(
    router_harness,
):
    h = router_harness
    instance, run = paused_run(h)
    try:
        version = h.read(instance, run)["state_version"]
        reviewed = h.request("resume_run", instance, run, version=version)
        raw = exact(reviewed)
        # A separate command peer changes the run before the reviewed send.
        assert h.send(h.request("pause_run", instance, run))["status"] == "applied"
        assert h.read(instance, run)["state_version"] > version
        assert post(h, raw).status_code in (200, 202)
        observation = settled_receipt(h, reviewed, instance, run)
        receipt = observation["core_receipt"]
        assert receipt["status"] == "refused"
        assert receipt["code"] == "stale_state_version"
        assert receipt["request_id"] == reviewed["request_id"]
        # Core recorded exactly the reviewed bytes (version included), once.
        assert observation["request_digest"] == request_digest(reviewed)
        assert receipt["request_digest"] == request_digest(reviewed)
        rows = [
            row
            for row in controls(h, instance, run)
            if row["request_id"] == reviewed["request_id"]
        ]
        assert len(rows) == 1 and rows[0]["status"] == "refused"
    finally:
        cancel(h, instance, run)


def test_a_lost_answer_is_observed_by_exact_id_and_replay_changes_nothing(
    router_harness,
):
    h = router_harness
    instance, run = paused_run(h)
    try:
        assert h.send(h.request("pause_run", instance, run))["status"] == "applied"
        version = h.read(instance, run)["state_version"]
        reviewed = h.request("resume_run", instance, run, version=version)
        raw = exact(reviewed)
        # Durable admission happened; its answer is treated as lost.
        assert post(h, raw).status_code in (200, 202)
        observation = settled_receipt(h, reviewed, instance, run)
        receipt = observation["core_receipt"]
        assert receipt["status"] == "applied"
        assert observation["request_digest"] == request_digest(reviewed)
        after = h.read(instance, run)["state_version"]
        before_rows = controls(h, instance, run)

        # Exact replay: the same bytes return the recorded receipt only.
        replayed = post(h, raw)
        assert replayed.status_code == 200, replayed.text
        assert replayed.json()["core_receipt"] == receipt
        again = settled_receipt(h, reviewed, instance, run)
        assert again["core_receipt"] == receipt
        assert h.read(instance, run)["state_version"] == after
        assert controls(h, instance, run) == before_rows

        # The same ID with other bytes is refused, never treated as a replay.
        changed = dict(reviewed, expected_state_version=after)
        refused = post(h, exact(changed))
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] in {"request_id_reused", "core_request_id_reused"}
        assert h.read(instance, run)["state_version"] == after
    finally:
        cancel(h, instance, run)


def test_a_request_lost_before_admission_stays_unknown_until_replayed(
    router_harness,
):
    h = router_harness
    instance, run = paused_run(h)
    try:
        reviewed = h.request("pause_run", instance, run)
        raw = exact(reviewed)
        # Never delivered: no observation exists, which is not "not executed".
        missing = strict(h, reviewed["request_id"], instance, run)
        assert missing.status_code == 404, missing.text
        assert missing.json()["code"] == "observation_unavailable"
        version = h.read(instance, run)["state_version"]
        assert post(h, raw).status_code in (200, 202)
        receipt = settled_receipt(h, reviewed, instance, run)["core_receipt"]
        assert receipt["status"] == "applied"
        assert h.read(instance, run)["state_version"] > version
        rows = [
            row
            for row in controls(h, instance, run)
            if row["request_id"] == reviewed["request_id"]
        ]
        assert len(rows) == 1
    finally:
        cancel(h, instance, run)


def test_start_association_comes_only_from_the_matching_receipt(router_harness):
    h = router_harness
    workflow, project = h.flow(pause=True)
    instance = h.create(workflow, project)
    reviewed = h.request(
        "start_run",
        instance,
        version=h.read(instance)["state_version"],
        payload={"edit_mode": "locked"},
    )
    assert post(h, exact(reviewed)).status_code in (200, 202)
    observation = settled_receipt(h, reviewed, instance)
    receipt = observation["core_receipt"]
    assert receipt["status"] == "applied"
    run = receipt["run_id"]
    assert observation["run_id"] == run and receipt["instance_id"] == instance
    try:
        bound = strict(h, reviewed["request_id"], instance, run)
        assert bound.status_code == 200, bound.text
        assert bound.json()["core_receipt"] == receipt
        assert bound.json()["request_digest"] == request_digest(reviewed)
        h.settled(instance, run, "paused")
    finally:
        cancel(h, instance, run)
