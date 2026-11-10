// Read-only view of one plane of a captured revision graph (lazy chunk).
//
// React Flow nodes and edges here are render objects derived from Core's
// captured projection; they are never graph schema, revision data or a
// publication input. Topology editing is disabled and positions are never
// saved. The equivalent node and route tables are always rendered beside the
// canvas, and they are the primary keyboard path into Inspector.

import "@xyflow/react/dist/style.css";

import {
  BaseEdge,
  Controls,
  EdgeLabelRenderer,
  getBezierPath,
  getViewportForBounds,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useStore,
  useReactFlow,
  type Edge,
  type EdgeProps,
  type Node,
  type NodeChange,
  type NodeProps,
} from "@xyflow/react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import type { CapturedAction, CapturedGraph, CapturedPlane } from "./api.ts";
import {
  directedLayout,
  gridLayout,
  NODE_HEIGHT,
  NODE_WIDTH,
  placeTopology,
  planeTopology,
  type Placed,
  type Point,
  type TopoEdge,
  type TopoNode,
  type Topology,
} from "./layout.ts";

export interface GraphViewProps {
  graph: CapturedGraph;
  planeId: string;
  onPlane: (planeId: string) => void;
  /** The plane's recorded cursor in the current summary, when this is its active revision. */
  cursor: string | null;
  /** The plane's last recorded transition, by captured identity. */
  lastEvent: { nodeId: string; outcome: string } | null;
  selectedNode: string | null;
  invocationSelected: boolean;
  onSelectNode: (planeId: string, nodeId: string) => void;
  colorMode: "light" | "dark" | "system";
  viewActions: ReactNode;
}

type NodeData = { node: TopoNode; start: boolean; cursor: boolean; selected: boolean };
type EdgeData = { edge: TopoEdge; points: Point[] | null; labelPoint: Point | null; last: boolean };
type FlowNode = Node<NodeData, "captured">;
type FlowEdge = Edge<EdgeData, "route">;

function CapturedNodeView({ data }: NodeProps<FlowNode>) {
  const marks = [data.start ? "start" : null, data.cursor ? "cursor" : null, data.selected ? "selected" : null].filter((mark) => mark !== null);
  return (
    <div className={`flow-node flow-node--${data.node.kind}${data.cursor ? " is-cursor" : ""}`}>
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <span className="flow-node__kind">{data.node.kind === "node" ? "node" : data.node.kind}</span>
      <span className="flow-node__label" title={data.node.label}>{data.node.kind === "node" ? data.node.label : data.node.label.replace(new RegExp(`^${data.node.kind}\\s+`), "")}</span>
      {marks.length > 0 ? <span className="flow-node__marks">{marks.join(" · ")}</span> : null}
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  );
}

function polyline(points: Point[]): string {
  return points.map((point, index) => `${index === 0 ? "M" : "L"} ${point.x} ${point.y}`).join(" ");
}

function RouteEdgeView(props: EdgeProps<FlowEdge>) {
  const { id, data, markerEnd, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition } = props;
  let path: string;
  let labelX: number;
  let labelY: number;
  if (props.source === props.target) {
    // A self route (retry): an arc over the node from its right to its left side.
    const lift = 72;
    path = `M ${sourceX} ${sourceY} C ${sourceX + 48} ${sourceY - lift}, ${targetX - 48} ${targetY - lift}, ${targetX} ${targetY}`;
    labelX = (sourceX + targetX) / 2;
    labelY = sourceY - lift;
  } else if (data?.points) {
    path = polyline(data.points);
    const middle = data.points[Math.floor(data.points.length / 2)];
    labelX = data.labelPoint?.x ?? middle.x;
    labelY = data.labelPoint?.y ?? middle.y;
  } else {
    [path, labelX, labelY] = getBezierPath({ sourceX, sourceY, sourcePosition, targetX, targetY, targetPosition });
  }
  const role = data?.edge.role ?? "route";
  return (
    <>
      <BaseEdge id={id} path={path} markerEnd={markerEnd} className={`flow-edge flow-edge--${role}${data?.last ? " is-last" : ""}`} />
      <EdgeLabelRenderer>
        <div
          className={`flow-edge-label flow-edge-label--${role}${data?.last ? " is-last" : ""}`}
          style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
        >
          {data?.edge.label}
          {data?.last ? " (last)" : ""}
        </div>
      </EdgeLabelRenderer>
    </>
  );
}

const nodeTypes = { captured: CapturedNodeView };
const edgeTypes = { route: RouteEdgeView };

function describe(action: CapturedAction | null): string {
  if (action === null || action.kind === null) return "none";
  if (action.kind === "goto") return `goto ${action.target ?? "unknown"}`;
  if (action.kind === "finish") return `finish ${action.target ?? "unknown"}`;
  if (action.kind === "pause") {
    return `pause ${action.reason ?? "unknown"}${action.target ? ` (resume ${action.target})` : ""}`;
  }
  if (action.kind === "retry") return `retry ≤ ${action.limit ?? "?"}, then ${describe(action.exhausted)}`;
  return action.kind;
}

function edgeIsLast(edge: TopoEdge, last: GraphViewProps["lastEvent"]): boolean {
  return last !== null && edge.from === last.nodeId && edge.outcome === last.outcome && edge.role !== "resume";
}

function flowElements(plane: CapturedPlane, placed: Placed, props: GraphViewProps, edges: TopoEdge[], nodes: TopoNode[]) {
  const flowNodes: FlowNode[] = nodes.map((node) => ({
    id: node.id,
    type: "captured",
    position: placed.nodes.get(node.id) ?? { x: 0, y: 0 },
    data: {
      node,
      start: node.id === plane.start,
      cursor: node.id === props.cursor,
      selected: node.id === props.selectedNode,
    },
    selected: node.id === props.selectedNode,
    draggable: false,
    connectable: false,
    selectable: node.kind === "node",
    ariaLabel:
      node.kind === "node"
        ? `Node ${node.id}${node.id === plane.start ? ", start" : ""}${node.id === props.cursor ? ", recorded cursor" : ""}`
        : node.label,
  }));
  const flowEdges: FlowEdge[] = edges.map((edge) => {
    const routed = placed.edges.get(edge.id);
    return {
      id: edge.id,
      source: edge.source,
      target: edge.target,
      type: "route",
      markerEnd: { type: MarkerType.ArrowClosed },
      data: { edge, points: routed?.points ?? null, labelPoint: routed?.label ?? null, last: edgeIsLast(edge, props.lastEvent) },
      focusable: false,
      selectable: false,
    };
  });
  return { flowNodes, flowEdges };
}

type LayoutEngine = "directed" | "grid";

const MIN_ZOOM = 0.2;
const MAX_ZOOM = 2;
/** React Flow's default fit padding, explicit because the zoom floor below must use the same. */
const FIT_PADDING = 0.1;
// Stable references matter: a summary render must not replace a queued manual
// Fit's options in React Flow's store with the initial reading-scale options.
const INITIAL_FIT_OPTIONS = { padding: FIT_PADDING, minZoom: 0.8, maxZoom: 1 };
const COMPLETE_FIT_OPTIONS = { padding: FIT_PADDING };

/**
 * The zoom floor: the usual 0.2, or lower when Fit needs it to show every laid-out
 * node in this canvas. Uses React Flow's own bounds-to-viewport calculation over
 * the same node boxes its Fit measures, so Fit is never clamped above the full view.
 */
function zoomFloor(placed: Placed, width: number, height: number): number {
  if (width <= 0 || height <= 0 || placed.nodes.size === 0) return MIN_ZOOM;
  let left = Infinity;
  let top = Infinity;
  let right = -Infinity;
  let bottom = -Infinity;
  for (const { x, y } of placed.nodes.values()) {
    left = Math.min(left, x);
    top = Math.min(top, y);
    right = Math.max(right, x + NODE_WIDTH);
    bottom = Math.max(bottom, y + NODE_HEIGHT);
  }
  const bounds = { x: left, y: top, width: right - left, height: bottom - top };
  return Math.min(MIN_ZOOM, getViewportForBounds(bounds, width, height, 0, MAX_ZOOM, FIT_PADDING).zoom);
}

/**
 * Reading scale is distinct from Fit's complete-topology overview. Readable size
 * is one deliberate recentering: 100% around `anchor`, the laid-out centre of
 * the node the caller chose from the displayed topology. It runs only on this
 * button, never on a read, resize or pan, and it selects nothing. The canvas box
 * is measured at the press, so a canvas resized while its view was hidden is
 * centred for the size it has now.
 */
function ViewScaleControls({ ready, anchor, canvas }: { ready: boolean; anchor: Point | null; canvas: () => HTMLElement | null }) {
  const { zoomTo, fitView, setCenter, setViewport } = useReactFlow();
  const readable = (): void => {
    const box = canvas();
    if (anchor === null) void zoomTo(1);
    else if (box === null || box.clientWidth <= 0 || box.clientHeight <= 0) void setCenter(anchor.x, anchor.y, { zoom: 1 });
    else void setViewport({ x: box.clientWidth / 2 - anchor.x, y: box.clientHeight / 2 - anchor.y, zoom: 1 });
  };
  return <div className="graph-scale-controls" role="group" aria-label="Graph scale">
    <button type="button" disabled={!ready} onClick={() => void fitView(COMPLETE_FIT_OPTIONS)}>Fit all</button>
    <button type="button" disabled={!ready} title="Return to 100%, centered on the recorded cursor when it is in this graph, otherwise the Inspector’s node or the start node" onClick={readable}>Readable size (100%)</button>
  </div>;
}

/** Passes React Flow's measured canvas size up, so the zoom floor follows resizes. */
function CanvasSize({ onSize }: { onSize: (width: number, height: number) => void }) {
  const width = useStore((state) => state.width);
  const height = useStore((state) => state.height);
  useEffect(() => {
    // The store reads 0 x 0 until React Flow has measured its canvas.
    if (width > 0 && height > 0) onSize(width, height);
  }, [onSize, width, height]);
  return null;
}

/** Geometry for exactly one topology (plane of one captured revision) and engine. */
interface Geometry {
  topology: Topology;
  engine: LayoutEngine;
  placed: Placed;
  initialNode: string | null;
}

export default function GraphView(props: GraphViewProps) {
  const { graph, planeId } = props;
  const [engine, setEngine] = useState<LayoutEngine>("directed");
  const plane = graph.planes.find((item) => item.planeId === planeId) ?? graph.planes[0];
  const topology = useMemo(() => (plane ? planeTopology(plane) : { nodes: [], edges: [] }), [plane]);
  // Layout can take seconds on a large plane, so it runs once per topology and
  // engine after the chooser and tables have committed, never during render:
  // a render can be interrupted and restarted, repeating the whole layout.
  // Only geometry computed for this exact topology and engine is ever drawn.
  const [geometry, setGeometry] = useState<Geometry | null>(null);
  // The canvas size sets the zoom floor. It is read from the pending canvas box,
  // which is the same box React Flow will fill, before React Flow mounts, so its
  // first fit already uses the right floor; CanvasSize then follows resizes.
  const canvasRef = useRef<HTMLDivElement>(null);
  const [canvasSize, setCanvasSize] = useState({ width: 0, height: 0 });
  const onCanvasSize = useCallback((width: number, height: number) => {
    setCanvasSize((current) => (current.width === width && current.height === height ? current : { width, height }));
  }, []);
  useEffect(() => {
    const canvas = canvasRef.current;
    if (canvas !== null) onCanvasSize(canvas.clientWidth, canvas.clientHeight);
    // Choose once for this topology/layout, not on polling, cursor movement or pan.
    const initialNode = [props.cursor, props.selectedNode, plane?.start].find(
      (id) => id !== null && id !== undefined && topology.nodes.some((node) => node.kind === "node" && node.id === id),
    ) ?? null;
    setGeometry({ topology, engine, placed: placeTopology(topology, engine === "grid" ? gridLayout : directedLayout), initialNode });
  }, [topology, engine, onCanvasSize]);
  const placed = geometry !== null && geometry.topology === topology && geometry.engine === engine ? geometry.placed : null;
  const minZoom = useMemo(
    () => (placed === null ? MIN_ZOOM : zoomFloor(placed, canvasSize.width, canvasSize.height)),
    [placed, canvasSize],
  );
  const initialFit = useMemo(() => geometry?.initialNode === null || geometry?.initialNode === undefined
    ? INITIAL_FIT_OPTIONS : { ...INITIAL_FIT_OPTIONS, nodes: [{ id: geometry.initialNode }] }, [geometry]);
  if (plane === undefined) return <p className="empty">This captured revision has no planes.</p>;
  // Readable size recenters on the same exact identities as the first view, read
  // when it is pressed: this plane's recorded cursor, else the Inspector's node,
  // else the start, and only one that is a node of the displayed topology.
  const readingNode = [props.cursor, props.selectedNode, plane.start].find(
    (id) => id !== null && id !== undefined && topology.nodes.some((node) => node.kind === "node" && node.id === id),
  ) ?? null;
  const readingPosition = readingNode === null || placed === null ? null : placed.nodes.get(readingNode) ?? null;
  const readingAnchor = readingPosition === null ? null : { x: readingPosition.x + NODE_WIDTH / 2, y: readingPosition.y + NODE_HEIGHT / 2 };
  const nodeCount = topology.nodes.filter((node) => node.kind === "node").length;
  const { flowNodes, flowEdges } =
    placed === null ? { flowNodes: [], flowEdges: [] } : flowElements(plane, placed, props, topology.edges, topology.nodes);
  const onNodesChange = (changes: NodeChange<FlowNode>[]) => {
    // Selection (pointer or Enter/Space on a focused node) opens Inspector; nothing else changes.
    for (const change of changes) {
      if (change.type === "select" && change.selected) {
        const node = topology.nodes.find((item) => item.id === change.id);
        if (node?.kind === "node") props.onSelectNode(plane.planeId, node.id);
      }
    }
  };
  const seams = graph.seams.filter((seam) => seam.producerPlane === plane.planeId || seam.consumerPlane === plane.planeId);
  return (
    <ReactFlowProvider><div className="graph-view">
      <div className="toolbar graph-toolbar" role="group" aria-label="Graph options">
        {graph.planes.length > 1 ? (
          <label>
            Plane{" "}
            <select value={plane.planeId} onChange={(event) => props.onPlane(event.target.value)}>
              {graph.planes.map((item) => (
                <option key={item.planeId} value={item.planeId}>
                  {item.planeId}
                </option>
              ))}
            </select>
          </label>
        ) : (
          <span>
            Plane <code>{plane.planeId}</code>
          </span>
        )}
        <fieldset className="segmented">
          <legend>Layout</legend>
          {(["directed", "grid"] as const).map((choice) => (
            <label key={choice}>
              <input type="radio" name="graph-layout" checked={engine === choice} onChange={() => setEngine(choice)} />
              {choice === "directed" ? "Directed" : "Grid overview"}
            </label>
          ))}
        </fieldset>
        <span className="muted">
          {nodeCount} nodes + {topology.nodes.length - nodeCount} actions · {topology.edges.length} routes
        </span>
        <ViewScaleControls ready={placed !== null} anchor={readingAnchor} canvas={() => canvasRef.current} />
        {props.viewActions}
      </div>
      <p className="graph-legend" aria-label="Graph marks">
        <span className="graph-legend__cursor"><strong>Recorded cursor:</strong> {props.cursor === null ? <span>none here</span> : <code>{props.cursor}</code>}</span>
        <span className="graph-legend__selection"><strong>{props.invocationSelected ? "Inspector:" : "Inspector node:"}</strong> {props.invocationSelected ? <span>invocation not marked</span> : props.selectedNode === null ? <span>none here</span> : <code>{props.selectedNode}</code>}</span>
      </p>
      {engine === "grid" || (placed !== null && placed.fallback !== null) ? (
        <p className="muted graph-overview-guidance">Grid overview emphasizes node positions. When links overlap, use Directed or the Routes table below to read exact routes.</p>
      ) : null}
      {placed !== null && placed.fallback !== null ? (
        <p className="notice notice--warn" role="status">
          Directed layout failed ({placed.fallback}); showing the complete Grid overview.
        </p>
      ) : null}
      {placed === null ? (
        <div ref={canvasRef} className="graph-canvas" aria-busy="true">
          <p className="muted" role="status">
            Laying out {nodeCount} nodes and {topology.edges.length} routes…
          </p>
        </div>
      ) : (
        <div
          ref={canvasRef}
          className="graph-canvas"
          role="group"
          aria-label={`Captured graph of plane ${plane.planeId}, ${placed.engine} layout, read-only`}
        >
          <ReactFlow<FlowNode, FlowEdge>
            nodes={flowNodes}
            edges={flowEdges}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            onNodesChange={onNodesChange}
            nodesDraggable={false}
            nodesConnectable={false}
            edgesReconnectable={false}
            connectOnClick={false}
            elementsSelectable
            nodesFocusable
            edgesFocusable={false}
            deleteKeyCode={null}
            selectionKeyCode={null}
            multiSelectionKeyCode={null}
            zoomOnDoubleClick={false}
            colorMode={props.colorMode}
            fitView
            fitViewOptions={initialFit}
            minZoom={minZoom}
            maxZoom={MAX_ZOOM}
          >
            {/* Zoom and Fit only: the interactivity toggle would enable editing. */}
            <Controls showInteractive={false} showFitView={false} />
            <CanvasSize onSize={onCanvasSize} />
          </ReactFlow>
        </div>
      )}
      <details className="graph-help"><summary>Graph help</summary>
        <p>Read-only captured graph. Select a node to inspect it. A thick border marks the recorded cursor; an outer outline marks the Inspector’s selected node. Both marks refer only to the displayed plane and captured revision; “none” means no matching node here. The Inspector may show a different object. The graph opens around current work. Fit all shows the complete topology. Readable size returns to 100% and centers the recorded cursor when it is in this graph, otherwise the Inspector’s node or the start node; it selects nothing, and reads never move the view. Exact nodes and routes follow below.</p>
      </details>
      <p className="table-scroll-hint">Scroll the tables horizontally when needed for all columns.</p>
      <GraphTables plane={plane} edges={topology.edges} seams={seams} props={props} />
    </div></ReactFlowProvider>
  );
}

function GraphTables({
  plane,
  edges,
  seams,
  props,
}: {
  plane: CapturedPlane;
  edges: TopoEdge[];
  seams: CapturedGraph["seams"];
  props: GraphViewProps;
}) {
  return (
    <div className="graph-tables">
      <table>
        <caption>Nodes of plane {plane.planeId}</caption>
        <thead>
          <tr>
            <th scope="col">Node</th>
            <th scope="col">Marks</th>
            <th scope="col">Binding</th>
            <th scope="col">Timeout</th>
            <th scope="col">Outcomes</th>
            <th scope="col">Requires terminal</th>
          </tr>
        </thead>
        <tbody>
          {plane.nodes.map((node) => (
            <tr key={node.id} aria-selected={node.id === props.selectedNode}>
              <th scope="row">
                <button
                  type="button"
                  className="link"
                  aria-pressed={node.id === props.selectedNode}
                  onClick={() => props.onSelectNode(plane.planeId, node.id)}
                >
                  <code>{node.id}</code>
                </button>
              </th>
              <td>
                {[node.id === plane.start ? "start" : null, node.id === props.cursor ? "cursor" : null]
                  .filter((mark) => mark !== null)
                  .join(", ") || "—"}
              </td>
              <td>{node.binding ?? "default"}</td>
              <td>{node.timeoutSeconds === null ? "default" : `${node.timeoutSeconds} s`}</td>
              <td>{node.outcomes.length > 0 ? node.outcomes.join(", ") : "none"}</td>
              <td>
                {node.requiresTerminal.length > 0
                  ? node.requiresTerminal.map((item) => `${item.planeId} ${item.status}`).join(", ")
                  : "none"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <table>
        <caption>Routes of plane {plane.planeId}</caption>
        <thead>
          <tr>
            <th scope="col">From</th>
            <th scope="col">Outcome</th>
            <th scope="col">Action</th>
            <th scope="col">To</th>
          </tr>
        </thead>
        <tbody>
          {plane.nodes.flatMap((node) =>
            [
              ...node.routes.map(([outcome, action]) => ({ outcome, action })),
              { outcome: "error", action: node.onError },
              { outcome: "unknown", action: node.onUnknown },
            ].map(({ outcome, action }) => {
              const last = edges.some((edge) => edge.from === node.id && edge.outcome === outcome && edgeIsLast(edge, props.lastEvent));
              return (
                <tr key={`${node.id}|${outcome}`} className={last ? "is-last" : undefined}>
                  <td>
                    <code>{node.id}</code>
                  </td>
                  <td>{outcome}</td>
                  <td>{describe(action)}</td>
                  <td>
                    {action?.kind === "goto" || action?.kind === "pause" ? <code>{action.target ?? "—"}</code> : "—"}
                    {last ? " (last recorded)" : ""}
                  </td>
                </tr>
              );
            }),
          )}
        </tbody>
      </table>
      {seams.length > 0 ? (
        <table>
          <caption>Seams touching plane {plane.planeId}</caption>
          <thead>
            <tr>
              <th scope="col">Seam</th>
              <th scope="col">Producer</th>
              <th scope="col">Outcome</th>
              <th scope="col">Consumer</th>
            </tr>
          </thead>
          <tbody>
            {seams.map((seam) => (
              <tr key={seam.id}>
                <td>
                  <code>{seam.id}</code>
                </td>
                <td>
                  {seam.producerPlane ?? "unknown"} / {seam.producerNode ?? "unknown"}
                </td>
                <td>{seam.outcome ?? "unknown"}</td>
                <td>
                  {seam.consumerPlane ?? "unknown"} / {seam.consumerNode ?? "unknown"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  );
}
