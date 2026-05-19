import createEngine, {
  AbstractReactFactory,
  CanvasWidget,
  DefaultLinkModel,
  DefaultNodeModel,
  DefaultPortModel,
  DiagramModel,
  PortModelAlignment,
  PortWidget,
  type DiagramEngine,
} from "@projectstorm/react-diagrams";
import { useMemo } from "react";
import type {
  StageActionGraphData,
  StageActionGraphNode,
  StageResultPayload,
} from "../types.js";

interface StageActionGraphBlockProps {
  payload: StageResultPayload & {
    view: {
      type: "action-graph";
      title?: string;
      nodeCount?: number;
      edgeCount?: number;
    };
    data: StageActionGraphData;
  };
}

type ActionSide = "input" | "output";

interface StageActionPortMeta {
  id: string;
  side: ActionSide;
  tooltip: string;
}

class MlodyActionGraphNodeModel extends DefaultNodeModel {
  readonly actionNodeKind: StageActionGraphNode["kind"];
  readonly executor: string;
  readonly operation: string;
  readonly subtitle: string | null;
  readonly description: string | null;
  readonly executorDetail: string | null;
  private readonly portMetaByName = new Map<string, StageActionPortMeta>();

  constructor(node?: StageActionGraphNode) {
    super({
      type: "mlody-action-graph-node",
      name: node?.title ?? "Action",
      color: actionNodeColor(node?.kind ?? "action"),
    });
    this.actionNodeKind = node?.kind ?? "action";
    this.executor = node?.executor ?? "mlody";
    this.operation = node?.operation ?? "action";
    this.subtitle = node?.subtitle ?? null;
    this.description = node?.description ?? null;
    this.executorDetail = node?.executorDetail ?? null;

    if (node) {
      this.setPosition(node.position.x, node.position.y);
      for (const side of ["input", "output"] as const) {
        const portId = side === "input" ? "in" : "out";
        const portModel = new DefaultPortModel({
          in: side === "input",
          name: portId,
          label: portId,
          alignment:
            side === "input"
              ? PortModelAlignment.LEFT
              : PortModelAlignment.RIGHT,
        });
        this.portMetaByName.set(portId, {
          id: portId,
          side,
          tooltip: side === "input" ? "Dependency input" : "Dependency output",
        });
        this.addPort(portModel);
      }
    }
  }

  getPortMeta(port: DefaultPortModel): StageActionPortMeta | null {
    const portName = String(port.getOptions().name ?? "");
    return this.portMetaByName.get(portName) ?? null;
  }
}

function actionNodeColor(kind: StageActionGraphNode["kind"]): string {
  switch (kind) {
    case "task":
      return "rgb(194, 65, 12)";
    case "value":
      return "rgb(29, 78, 216)";
    case "resolve":
      return "rgb(8, 145, 178)";
    case "prepare":
      return "rgb(5, 150, 105)";
    default:
      return "rgb(124, 58, 237)";
  }
}

function buildDiagramModel(data: StageActionGraphData): DiagramModel {
  const model = new DiagramModel();
  const portMap = new Map<string, DefaultPortModel>();

  for (const node of data.nodes) {
    const nodeModel = new MlodyActionGraphNodeModel(node);

    for (const port of [...nodeModel.getInPorts(), ...nodeModel.getOutPorts()]) {
      portMap.set(`${node.id}:${String(port.getOptions().name ?? "")}`, port);
    }

    model.addNode(nodeModel);
  }

  for (const edge of data.edges) {
    const sourcePort = portMap.get(`${edge.sourceNodeId}:out`);
    const targetPort = portMap.get(`${edge.targetNodeId}:in`);
    if (!sourcePort || !targetPort) {
      continue;
    }

    const link = sourcePort.link<DefaultLinkModel>(targetPort);
    model.addLink(link);
  }

  return model;
}

function ActionPort({
  engine,
  node,
  port,
}: {
  engine: DiagramEngine;
  node: MlodyActionGraphNodeModel;
  port: DefaultPortModel;
}) {
  const portMeta = node.getPortMeta(port);
  const side = portMeta?.side ?? (port.getOptions().in ? "input" : "output");
  return (
    <PortWidget port={port} engine={engine}>
      <div
        className={`StageActionGraphNode-port StageActionGraphNode-port--${side}`}
        title={portMeta?.tooltip ?? ""}
      >
        <span className="StageActionGraphNode-portDot" />
      </div>
    </PortWidget>
  );
}

function MlodyActionGraphNodeWidget({
  engine,
  node,
}: {
  engine: DiagramEngine;
  node: MlodyActionGraphNodeModel;
}) {
  const inputPort = node.getInPorts()[0];
  const outputPort = node.getOutPorts()[0];

  return (
    <div className={`StageActionGraphNode StageActionGraphNode--${node.actionNodeKind}`}>
      <div className="StageActionGraphNode-header">
        <span className="StageActionGraphNode-title">
          {String(node.getOptions().name ?? "")}
        </span>
        <span className="StageActionGraphNode-executor">{node.executor}</span>
      </div>
      <div className="StageActionGraphNode-body">
        <div className="StageActionGraphNode-operation">{node.operation}</div>
        {node.subtitle ? (
          <div className="StageActionGraphNode-subtitle">{node.subtitle}</div>
        ) : null}
        {node.description ? (
          <div className="StageActionGraphNode-description">{node.description}</div>
        ) : null}
        {node.executorDetail ? (
          <div className="StageActionGraphNode-executorDetail">
            {node.executorDetail}
          </div>
        ) : null}
      </div>
      <div className="StageActionGraphNode-ports">
        {inputPort ? (
          <ActionPort engine={engine} node={node} port={inputPort} />
        ) : (
          <span />
        )}
        {outputPort ? <ActionPort engine={engine} node={node} port={outputPort} /> : null}
      </div>
    </div>
  );
}

class MlodyActionGraphNodeFactory extends AbstractReactFactory<
  MlodyActionGraphNodeModel,
  DiagramEngine
> {
  constructor() {
    super("mlody-action-graph-node");
  }

  generateModel() {
    return new MlodyActionGraphNodeModel();
  }

  generateReactWidget(event: { model: MlodyActionGraphNodeModel }) {
    return <MlodyActionGraphNodeWidget engine={this.engine} node={event.model} />;
  }
}

export function StageActionGraphBlock({ payload }: StageActionGraphBlockProps) {
  const engine = useMemo(() => {
    const diagramEngine = createEngine();
    diagramEngine
      .getNodeFactories()
      .registerFactory(new MlodyActionGraphNodeFactory());
    diagramEngine.setModel(buildDiagramModel(payload.data));
    return diagramEngine;
  }, [payload.data]);

  const nodeCount =
    typeof payload.view.nodeCount === "number"
      ? payload.view.nodeCount
      : payload.data.nodes.length;
  const edgeCount =
    typeof payload.view.edgeCount === "number"
      ? payload.view.edgeCount
      : payload.data.edges.length;

  return (
    <div className="StageActionGraphBlock">
      <div className="StageActionGraphBlock-header">
        <div className="StageActionGraphBlock-headingGroup">
          <span className="StageActionGraphBlock-label">Action Graph</span>
          {payload.view.title ? (
            <span className="StageActionGraphBlock-title">{payload.view.title}</span>
          ) : null}
        </div>
        <span className="StageActionGraphBlock-count">
          {nodeCount} node{nodeCount === 1 ? "" : "s"} · {edgeCount} edge
          {edgeCount === 1 ? "" : "s"}
        </span>
      </div>
      <div className="StageActionGraphBlock-canvasFrame">
        <CanvasWidget className="StageActionGraphBlock-canvas" engine={engine} />
      </div>
    </div>
  );
}
