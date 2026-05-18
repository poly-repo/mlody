import type { StageResultPayload, StageTaskData, StageTaskPort } from "../types.js";

interface StageTaskBlockProps {
  payload: StageResultPayload & {
    view: {
      type: "task";
      title?: string;
    };
    data: StageTaskData;
  };
}

interface TaskSectionSpec {
  key: "inputs" | "outputs" | "config";
  label: string;
  ports: StageTaskPort[];
}

const SECTION_SPECS: Array<Pick<TaskSectionSpec, "key" | "label">> = [
  { key: "inputs", label: "Inputs" },
  { key: "outputs", label: "Outputs" },
  { key: "config", label: "Config" },
];

function sectionSpecs(data: StageTaskData): TaskSectionSpec[] {
  return SECTION_SPECS.map((section) => ({
    ...section,
    ports: data[section.key],
  }));
}

export function StageTaskBlock({ payload }: StageTaskBlockProps) {
  const sections = sectionSpecs(payload.data);

  return (
    <div className="StageTaskBlock">
      <div className="StageTaskBlock-header">
        <div className="StageTaskBlock-headingGroup">
          <span className="StageTaskBlock-label">Task</span>
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
                  {section.ports.length}
                </span>
              </div>
              {section.ports.length ? (
                <div className="StageTaskBlock-portGrid">
                  {section.ports.map((port) => (
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
