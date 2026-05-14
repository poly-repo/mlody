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
import type { StageDagData, StageDagNode, StageDagPort, StageResultPayload } from "../types.js";

interface StageDagBlockProps {
  payload: StageResultPayload & {
    view: {
      type: "dag";
      title?: string;
      nodeCount?: number;
      edgeCount?: number;
    };
    data: StageDagData;
  };
}

type Side = "input" | "output";

interface MlodyDagNodePortMeta {
  id: string;
  label: string;
  side: Side;
  tooltip: string;
}

class MlodyDagNodeModel extends DefaultNodeModel {
  readonly dagNodeKind: StageDagNode["kind"];
  readonly hoverText: string;
  private readonly portMetaByName = new Map<string, MlodyDagNodePortMeta>();

  constructor(node?: StageDagNode) {
    super({
      type: "mlody-dag-node",
      name: node?.title ?? "Untitled",
      color: nodeColor(node?.kind ?? "value"),
    });
    this.dagNodeKind = node?.kind ?? "value";
    this.hoverText = node?.subtitle ?? node?.title ?? "Untitled";

    if (node) {
      this.setPosition(node.position.x, node.position.y);
      for (const port of node.ports) {
        const portModel = new DefaultPortModel({
          in: port.side === "input",
          name: port.id,
          label: port.label,
          alignment:
            port.side === "input"
              ? PortModelAlignment.LEFT
              : PortModelAlignment.RIGHT,
        });
        this.portMetaByName.set(port.id, {
          id: port.id,
          label: port.label,
          side: port.side,
          tooltip: port.typeLabel ? `${port.label}: ${port.typeLabel}` : port.label,
        });
        this.addPort(portModel);
      }
    }
  }

  getPortMeta(port: DefaultPortModel): MlodyDagNodePortMeta | null {
    const portName = String(port.getOptions().name ?? "");
    return this.portMetaByName.get(portName) ?? null;
  }
}

function nodeColor(kind: "task" | "value"): string {
  return kind === "task" ? "rgb(217, 119, 6)" : "rgb(37, 99, 235)";
}

function buildDiagramModel(data: StageDagData): DiagramModel {
  const model = new DiagramModel();
  const portMap = new Map<string, DefaultPortModel>();

  for (const node of data.nodes) {
    const nodeModel = new MlodyDagNodeModel(node);

    for (const port of [...nodeModel.getInPorts(), ...nodeModel.getOutPorts()]) {
      portMap.set(`${node.id}:${String(port.getOptions().name ?? "")}`, port);
    }

    model.addNode(nodeModel);
  }

  for (const edge of data.edges) {
    const sourcePort = portMap.get(`${edge.sourceNodeId}:${edge.sourcePortId}`);
    const targetPort = portMap.get(`${edge.targetNodeId}:${edge.targetPortId}`);
    if (!sourcePort || !targetPort) {
      continue;
    }

    const link = sourcePort.link<DefaultLinkModel>(targetPort);
    model.addLink(link);
  }

  return model;
}

function PortRow({
  engine,
  node,
  port,
}: {
  engine: DiagramEngine;
  node: MlodyDagNodeModel;
  port: DefaultPortModel;
}) {
  const portMeta = node.getPortMeta(port);
  const side = portMeta?.side ?? (port.getOptions().in ? "input" : "output");

  return (
    <PortWidget port={port} engine={engine}>
      <div
        className={`StageDagNode-portRow StageDagNode-portRow--${side}`}
        title={portMeta?.tooltip ?? ""}
      >
        {side === "output" ? (
          <>
            <span className="StageDagNode-portLabel">
              {portMeta?.label ?? String(port.getOptions().label ?? "")}
            </span>
            <span className="StageDagNode-portDot" />
          </>
        ) : (
          <>
            <span className="StageDagNode-portDot" />
            <span className="StageDagNode-portLabel">
              {portMeta?.label ?? String(port.getOptions().label ?? "")}
            </span>
          </>
        )}
      </div>
    </PortWidget>
  );
}

function MlodyDagNodeWidget({
  engine,
  node,
}: {
  engine: DiagramEngine;
  node: MlodyDagNodeModel;
}) {
  return (
    <div
      className={`StageDagNode StageDagNode--${node.dagNodeKind}`}
      title={node.hoverText}
    >
      <div className="StageDagNode-header">
        <span className="StageDagNode-title">{String(node.getOptions().name ?? "")}</span>
      </div>
      <div className="StageDagNode-portGrid">
        <div className="StageDagNode-portColumn StageDagNode-portColumn--input">
          {node.getInPorts().map((port) => (
            <PortRow
              key={port.getID()}
              engine={engine}
              node={node}
              port={port}
            />
          ))}
        </div>
        <div className="StageDagNode-portColumn StageDagNode-portColumn--output">
          {node.getOutPorts().map((port) => (
            <PortRow
              key={port.getID()}
              engine={engine}
              node={node}
              port={port}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

class MlodyDagNodeFactory extends AbstractReactFactory<MlodyDagNodeModel, DiagramEngine> {
  constructor() {
    super("mlody-dag-node");
  }

  generateModel() {
    return new MlodyDagNodeModel();
  }

  generateReactWidget(event: { model: MlodyDagNodeModel }) {
    return <MlodyDagNodeWidget engine={this.engine} node={event.model} />;
  }
}

export function StageDagBlock({ payload }: StageDagBlockProps) {
  const engine = useMemo(() => {
    const diagramEngine = createEngine();
    diagramEngine.getNodeFactories().registerFactory(new MlodyDagNodeFactory());
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
    <div className="StageDagBlock">
      <div className="StageDagBlock-header">
        <div className="StageDagBlock-headingGroup">
          <span className="StageDagBlock-label">DAG</span>
          {payload.view.title ? (
            <span className="StageDagBlock-title">{payload.view.title}</span>
          ) : null}
        </div>
        <span className="StageDagBlock-count">
          {nodeCount} node{nodeCount === 1 ? "" : "s"} · {edgeCount} edge
          {edgeCount === 1 ? "" : "s"}
        </span>
      </div>
      <div className="StageDagBlock-canvasFrame">
        <CanvasWidget className="StageDagBlock-canvas" engine={engine} />
      </div>
    </div>
  );
}
