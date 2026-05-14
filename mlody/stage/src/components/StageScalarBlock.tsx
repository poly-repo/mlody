import type { StageResultPayload, StageValueType } from "../types.js";

interface StageScalarBlockProps {
  payload: StageResultPayload & {
    view: {
      type: "json";
      title?: string;
    };
    data: string | number | boolean | null;
  };
}

function rootKind(valueType: StageValueType | null | undefined): string | null {
  if (!valueType) {
    return null;
  }

  if (typeof valueType._root_kind === "string" && valueType._root_kind.trim() !== "") {
    return valueType._root_kind;
  }
  if (typeof valueType.type === "string" && valueType.type.trim() !== "") {
    return valueType.type;
  }
  if (typeof valueType.name === "string" && valueType.name.trim() !== "") {
    return valueType.name;
  }
  return null;
}

function typeLabel(
  value: string | number | boolean | null,
  valueType: StageValueType | null | undefined,
): string {
  const explicitRootKind = rootKind(valueType);
  if (explicitRootKind) {
    return explicitRootKind;
  }

  if (value === null) {
    return "null";
  }
  return typeof value;
}

function displayText(value: string | number | boolean | null): string {
  if (value === null) {
    return "null";
  }
  if (typeof value === "boolean") {
    return value ? "true" : "false";
  }
  return String(value);
}

export function StageScalarBlock({ payload }: StageScalarBlockProps) {
  const value = payload.data;
  const typeName = typeLabel(value, payload.valueType);
  const text = displayText(value);
  const isBoolean = typeof value === "boolean";
  const isNull = value === null;
  const isNumeric = typeof value === "number";
  const isMultiline = typeof value === "string" && value.includes("\n");

  return (
    <div className="StageScalarBlock">
      <div className="StageScalarBlock-header">
        <div className="StageScalarBlock-headingGroup">
          <span className="StageScalarBlock-label">Value</span>
          {payload.view.title ? (
            <span className="StageScalarBlock-title">{payload.view.title}</span>
          ) : null}
        </div>
        <span className="StageScalarBlock-type">{typeName}</span>
      </div>
      <div className="StageScalarBlock-body">
        <span
          className={[
            "StageScalarBlock-value",
            typeof value === "string" ? "StageScalarBlock-value--string" : "",
            isMultiline ? "StageScalarBlock-value--multiline" : "",
            isNumeric ? "StageScalarBlock-value--numeric" : "",
            isBoolean ? "StageScalarBlock-value--boolean" : "",
            isNull ? "StageScalarBlock-value--null" : "",
          ]
            .filter(Boolean)
            .join(" ")}
        >
          {text}
        </span>
      </div>
    </div>
  );
}
