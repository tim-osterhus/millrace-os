import assert from "node:assert/strict";
import { test } from "node:test";

import type { CapturedAction, CapturedNode, CapturedPlane } from "./api.ts";
import { directedLayout, gridLayout, NODE_HEIGHT, NODE_WIDTH, placeTopology, planeTopology, type Topology } from "./layout.ts";

function go(target: string): CapturedAction {
  return { kind: "goto", target, reason: null, limit: null, exhausted: null };
}

function node(id: string, routes: [string, CapturedAction][], extra: Partial<CapturedNode> = {}): CapturedNode {
  return {
    id,
    binding: null,
    timeoutSeconds: null,
    outcomes: routes.map(([outcome]) => outcome),
    routes,
    onError: { kind: "pause", target: null, reason: "error", limit: null, exhausted: null },
    onUnknown: { kind: "pause", target: null, reason: "unknown", limit: null, exhausted: null },
    requiresTerminal: [],
    ...extra,
  };
}

// A LAD-like loop with parallel outcomes to the same target, a retry, a pause
// with resume, finishes and a disconnected node.
const PLANE: CapturedPlane = {
  planeId: "main",
  start: "plan",
  nodes: [
    node("review", [
      ["approved", { kind: "finish", target: "succeeded", reason: null, limit: null, exhausted: null }],
      ["changes", go("build")],
      ["blocked", { kind: "pause", target: "build", reason: "needs_input", limit: null, exhausted: null }],
    ]),
    node("plan", [
      ["ready", go("build")],
      ["unclear", go("build")],
    ]),
    node("build", [
      ["done", go("review")],
      [
        "flaky",
        {
          kind: "retry",
          target: null,
          reason: null,
          limit: 2,
          exhausted: { kind: "finish", target: "failed", reason: null, limit: null, exhausted: null },
        },
      ],
    ]),
    node("orphan", []),
  ],
};

test("topology keeps every captured route identity, including parallel outcomes and loops", () => {
  const topology = planeTopology(PLANE);
  const ids = topology.edges.map((edge) => edge.id);
  assert.equal(new Set(ids).size, ids.length, "edge identities are unique");
  const planToBuild = topology.edges.filter((edge) => edge.source === "plan" && edge.target === "build");
  assert.deepEqual(planToBuild.map((edge) => edge.outcome).sort(), ["ready", "unclear"]);
  assert.ok(topology.edges.some((edge) => edge.source === "build" && edge.target === "build" && edge.role === "retry"));
  assert.ok(topology.edges.some((edge) => edge.id === "build|flaky|exhausted" && edge.target === "finish:failed"));
  assert.ok(topology.edges.some((edge) => edge.role === "resume" && edge.target === "build"));
  assert.ok(topology.edges.some((edge) => edge.id === "review|error|error"));
  // Captured nodes come first in ID order; action endpoints follow.
  assert.deepEqual(
    topology.nodes.map((item) => item.id),
    [
      "build",
      "orphan",
      "plan",
      "review",
      "finish:failed",
      "finish:succeeded",
      "pause:build:error",
      "pause:build:unknown",
      "pause:orphan:error",
      "pause:orphan:unknown",
      "pause:plan:error",
      "pause:plan:unknown",
      "pause:review:blocked",
      "pause:review:error",
      "pause:review:unknown",
    ],
  );
});

test("the grid places every node deterministically without overlap", () => {
  const topology = planeTopology(PLANE);
  const first = gridLayout(topology);
  const second = gridLayout(planeTopology(PLANE));
  assert.deepEqual([...first.nodes], [...second.nodes]);
  assert.equal(first.nodes.size, topology.nodes.length);
  const seen = new Set([...first.nodes.values()].map((point) => `${point.x},${point.y}`));
  assert.equal(seen.size, topology.nodes.length);
});

test("the directed layout is deterministic, routes every edge and keeps the orphan", () => {
  const topology = planeTopology(PLANE);
  const first = directedLayout(topology);
  const second = directedLayout(planeTopology(PLANE));
  assert.equal(first.engine, "directed");
  assert.deepEqual([...first.nodes], [...second.nodes]);
  assert.deepEqual([...first.edges], [...second.edges]);
  assert.ok(first.nodes.has("orphan"));
  for (const edge of topology.edges) {
    assert.ok((first.edges.get(edge.id)?.points.length ?? 0) >= 2, edge.id);
  }
  // Positions are top-left corners of fixed-size boxes, so boxes never overlap.
  const boxes = [...first.nodes.values()];
  for (let i = 0; i < boxes.length; i += 1) {
    for (let j = i + 1; j < boxes.length; j += 1) {
      const apart =
        Math.abs(boxes[i].x - boxes[j].x) >= NODE_WIDTH - 0.5 || Math.abs(boxes[i].y - boxes[j].y) >= NODE_HEIGHT - 0.5;
      assert.ok(apart, `boxes ${i} and ${j} overlap`);
    }
  }
});

test("a failing layout engine falls back to the complete grid", () => {
  const topology = planeTopology(PLANE);
  const failing = (_: Topology): never => {
    throw new Error("layout exploded");
  };
  const placed = placeTopology(topology, failing);
  assert.equal(placed.engine, "grid");
  assert.equal(placed.fallback, "layout exploded");
  assert.equal(placed.nodes.size, topology.nodes.length);
  assert.equal(placeTopology(topology, gridLayout).fallback, null);
});

test("layout never changes the captured topology it was given", () => {
  const before = JSON.stringify(PLANE);
  const topology = planeTopology(PLANE);
  directedLayout(topology);
  gridLayout(topology);
  assert.equal(JSON.stringify(PLANE), before);
});
