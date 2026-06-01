import type {
  StageEntityData,
  StageEntityValue,
  StageEntitySection,
  StageResultPayload,
} from "../types.js";

interface StageEntityBlockProps {
  payload: StageResultPayload & {
    view: {
      type: "task" | "action";
      title?: string;
    };
    data: StageEntityData;
  };
}

const LEGACY_SECTION_SPECS: Array<Pick<StageEntitySection, "key" | "label">> = [
  { key: "inputs", label: "Inputs" },
  { key: "outputs", label: "Outputs" },
  { key: "config", label: "Config" },
];

function entitySections(data: StageEntityData): StageEntitySection[] {
  if (Array.isArray(data.sections) && data.sections.length > 0) {
    return data.sections;
  }
  return LEGACY_SECTION_SPECS.map((section) => ({
    ...section,
    values: data[section.key as keyof Pick<StageEntityData, "inputs" | "outputs" | "config">],
  }));
}

function entityLabel(kind: StageEntityBlockProps["payload"]["view"]["type"]): string {
  return kind === "action" ? "Action" : "Task";
}

function shouldRenderStructuredDetails(details: StageEntityValue["details"]): boolean {
  return details.length > 1 || details.some((detail) => detail.name.includes("."));
}

function renderPortDetails(port: StageEntityValue) {
  if (!port.details.length && !port.detailsText) {
    return null;
  }

  if (!shouldRenderStructuredDetails(port.details)) {
    return port.detailsText ? (
      <p className="StageTaskBlock-portDetails">{port.detailsText}</p>
    ) : null;
  }

  return (
    <dl className="StageTaskBlock-portDetailList">
      {port.details.map((detail) => (
        <div
          className="StageTaskBlock-portDetailRow"
          key={`${port.name}-${detail.name}-${detail.value}`}
        >
          <dt className="StageTaskBlock-portDetailName">{detail.name}</dt>
          <dd className="StageTaskBlock-portDetailValue">{detail.value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function StageEntityBlock({ payload }: StageEntityBlockProps) {
  const sections = entitySections(payload.data);

  return (
    <div className="StageTaskBlock">
      <div className="StageTaskBlock-header">
        <div className="StageTaskBlock-headingGroup">
          <span className="StageTaskBlock-label">{entityLabel(payload.view.type)}</span>
          <span className="StageTaskBlock-title">{payload.data.name}</span>
          {payload.view.title ? (
            <span className="StageTaskBlock-context">{payload.view.title}</span>
          ) : null}
        </div>
      </div>
      <div className="StageTaskBlock-body">
        {payload.data.description ? (
          <p className="StageTaskBlock-description">{payload.data.description}</p>
        ) : null}

        {payload.data.attributes.length ? (
          <div className="StageTaskBlock-attributes">
            {payload.data.attributes.map((attribute) => (
              <div
                className="StageTaskBlock-attributeCard"
                key={`${attribute.name}-${attribute.value}`}
              >
                <span className="StageTaskBlock-attributeLabel">{attribute.name}</span>
                <span className="StageTaskBlock-attributeValue">{attribute.value}</span>
                {attribute.detailsText ? (
                  <span className="StageTaskBlock-attributeDetails">
                    {attribute.detailsText}
                  </span>
                ) : null}
              </div>
            ))}
          </div>
        ) : null}

        <div className="StageTaskBlock-sections">
          {sections.map((section) => (
            <section className="StageTaskBlock-section" key={section.key}>
              <div className="StageTaskBlock-sectionHeader">
                <span className="StageTaskBlock-sectionTitle">{section.label}</span>
                <span className="StageTaskBlock-sectionCount">
                  {section.values.length}
                </span>
              </div>
              {section.values.length ? (
                <div className="StageTaskBlock-portGrid">
                  {section.values.map((port) => (
                    <article
                      className="StageTaskBlock-portCard"
                      key={`${section.key}-${port.name}`}
                    >
                      <div className="StageTaskBlock-portHeader">
                        <span className="StageTaskBlock-portName">{port.name}</span>
                        <span className="StageTaskBlock-portType">{port.type}</span>
                      </div>
                      <p
                        className={[
                          "StageTaskBlock-portDescription",
                          port.description
                            ? ""
                            : "StageTaskBlock-portDescription--empty",
                        ]
                          .filter(Boolean)
                          .join(" ")}
                      >
                        {port.description || "No description."}
                      </p>
                      {renderPortDetails(port)}
                    </article>
                  ))}
                </div>
              ) : (
                <p className="StageTaskBlock-empty">No {section.key}.</p>
              )}
            </section>
          ))}
        </div>
      </div>
    </div>
  );
}

export const StageTaskBlock = StageEntityBlock;
