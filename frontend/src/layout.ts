// Display geometry for one plane of a captured revision graph.
//
// The topology comes only from Core's captured graph projection: nodes, their
// outcome routes, error/unknown actions, retries and terminal actions. Nothing
// is inferred. Finish and pause actions become explicit action endpoints so
// they can be drawn; they are render objects, not graph schema.
// Positions are derived for the selected plane/layout and never persisted. The ID-sorted grid
// is the dependency-free baseline; the directed (Dagre) layout falls back to
// the complete grid if it throws.

import { Graph, layout as dagreLayout, type EdgeLabel, type GraphLabel, type NodeLabel } from "@dagrejs/dagre";

import type { CapturedAction, CapturedPlane } from "./api.ts";

export type TopoNodeKind = "node" | "finish" | "pause";
export type EdgeRole = "route" | "retry" | "exhausted" | "error" | "unknown" | "resume";

export interface TopoNode {
  id: string;
  kind: TopoNodeKind;
  label: string;
}

export interface TopoEdge {
  id: string;
  source: string;
  target: string;
  label: string;
  role: EdgeRole;
  /** The captured node and outcome (or `error`/`unknown`) this edge came from. */
  from: string;
  outcome: string;
}

export interface Topology {
  nodes: TopoNode[];
  edges: TopoEdge[];
}

export interface Point {
  x: number;
  y: number;
}

export interface Placed {
  engine: "grid" | "directed";
  /** Top-left positions keyed by topology node ID. */
  nodes: Map<string, Point>;
  /** Routed points and label centre, only for the directed engine. */
  edges: Map<string, { points: Point[]; label: Point | null }>;
  /** Why the directed layout was not used, when it failed. */
  fallback: string | null;
}

export const NODE_WIDTH = 224;
export const NODE_HEIGHT = 104;
const GAP_X = 72;
const GAP_Y = 48;

const byId = (a: { id: string }, b: { id: string }) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);

/** Every captured action of every node, with stable identities. */
export function planeTopology(plane: CapturedPlane): Topology {
  const nodes = new Map<string, TopoNode>();
  const edges: TopoEdge[] = [];
  for (const node of [...plane.nodes].sort(byId)) {
    nodes.set(node.id, { id: node.id, kind: "node", label: node.id });
  }
  const endpoint = (id: string, kind: TopoNodeKind, label: string) => {
    if (!nodes.has(id)) nodes.set(id, { id, kind, label });
    return id;
  };
  const add = (from: string, outcome: string, action: CapturedAction | null, role: EdgeRole, label: string) => {
    if (action === null || action.kind === null) return;
    const id = `${from}|${outcome}|${role}`;
    if (action.kind === "goto" && action.target !== null) {
      edges.push({ id, source: from, target: action.target, label, role, from, outcome });
    } else if (action.kind === "finish") {
      const target = endpoint(`finish:${action.target ?? "unknown"}`, "finish", `finish ${action.target ?? "unknown"}`);
      edges.push({ id, source: from, target, label, role, from, outcome });
    } else if (action.kind === "pause") {
      const pause = endpoint(`pause:${from}:${outcome}`, "pause", `pause ${action.reason ?? "unknown"}`);
      edges.push({ id, source: from, target: pause, label, role, from, outcome });
      if (action.target !== null) {
        edges.push({ id: `${id}|resume`, source: pause, target: action.target, label: "resume", role: "resume", from, outcome });
      }
    } else if (action.kind === "retry") {
      edges.push({ id, source: from, target: from, label: `${label} · retry ≤ ${action.limit ?? "?"}`, role: "retry", from, outcome });
      add(from, outcome, action.exhausted, "exhausted", `${label} exhausted`);
    }
  };
  for (const node of [...plane.nodes].sort(byId)) {
    for (const [outcome, action] of node.routes) add(node.id, outcome, action, "route", outcome);
    add(node.id, "error", node.onError, "error", "error");
    add(node.id, "unknown", node.onUnknown, "unknown", "unknown");
  }
  // Captured nodes first (ID order), then action endpoints (ID order).
  const ordered = [...nodes.values()].sort((a, b) =>
    a.kind === b.kind || (a.kind !== "node" && b.kind !== "node") ? byId(a, b) : a.kind === "node" ? -1 : 1,
  );
  return { nodes: ordered, edges };
}

/** Deterministic ID-sorted grid: complete, simple and dependency-free. */
export function gridLayout(topology: Topology): Placed {
  const columns = Math.max(1, Math.ceil(Math.sqrt(topology.nodes.length)));
  const nodes = new Map<string, Point>();
  topology.nodes.forEach((node, index) => {
    nodes.set(node.id, {
      x: (index % columns) * (NODE_WIDTH + GAP_X),
      y: Math.floor(index / columns) * (NODE_HEIGHT + GAP_Y),
    });
  });
  return { engine: "grid", nodes, edges: new Map(), fallback: null };
}

function labelWidth(label: string): number {
  return Math.min(200, 12 + label.length * 7);
}

/** Dagre on a fresh named multigraph, so parallel outcomes stay distinct. */
export function directedLayout(topology: Topology): Placed {
  const graph = new Graph<GraphLabel, NodeLabel, EdgeLabel>({ directed: true, multigraph: true, compound: false });
  graph.setGraph({ rankdir: "LR", nodesep: 36, ranksep: 88, edgesep: 18, marginx: 16, marginy: 16 });
  graph.setDefaultEdgeLabel(() => ({}));
  for (const node of topology.nodes) graph.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  for (const edge of topology.edges) {
    graph.setEdge(edge.source, edge.target, { width: labelWidth(edge.label), height: 20, labelpos: "c" }, edge.id);
  }
  dagreLayout(graph);
  const nodes = new Map<string, Point>();
  for (const node of topology.nodes) {
    const placed = graph.node(node.id);
    if (typeof placed?.x !== "number" || typeof placed.y !== "number") throw new Error(`no position for ${node.id}`);
    // Dagre returns centres; React Flow positions are top-left corners.
    nodes.set(node.id, { x: placed.x - NODE_WIDTH / 2, y: placed.y - NODE_HEIGHT / 2 });
  }
  const edges = new Map<string, { points: Point[]; label: Point | null }>();
  for (const edge of topology.edges) {
    const placed = graph.edge(edge.source, edge.target, edge.id);
    const points = (placed?.points ?? []).map((point) => ({ x: point.x, y: point.y }));
    if (points.length < 2) throw new Error(`no route for ${edge.id}`);
    const label = typeof placed?.x === "number" && typeof placed.y === "number" ? { x: placed.x, y: placed.y } : null;
    edges.set(edge.id, { points, label });
  }
  return { engine: "directed", nodes, edges, fallback: null };
}

export type Engine = (topology: Topology) => Placed;

/** Try the chosen engine; any exception yields the complete grid instead. */
export function placeTopology(topology: Topology, engine: Engine = directedLayout): Placed {
  if (engine === gridLayout) return gridLayout(topology);
  try {
    return engine(topology);
  } catch (error) {
    const grid = gridLayout(topology);
    return { ...grid, fallback: error instanceof Error ? error.message : "layout failed" };
  }
}
