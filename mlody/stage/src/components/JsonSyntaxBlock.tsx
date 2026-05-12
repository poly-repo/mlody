interface JsonSyntaxBlockProps {
  value: unknown;
}

type JsonTokenKind = "plain" | "key" | "string" | "number" | "boolean" | "null";

interface JsonToken {
  kind: JsonTokenKind;
  text: string;
}

const JSON_TOKEN_PATTERN =
  /("(?:\\u[\da-fA-F]{4}|\\[^u]|[^\\"])*"\s*:|"(?:\\u[\da-fA-F]{4}|\\[^u]|[^\\"])*"|\btrue\b|\bfalse\b|\bnull\b|-?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?)/g;

function formatJson(value: unknown): string {
  const formatted = JSON.stringify(value, null, 2);
  return formatted ?? "null";
}

function tokenizeJson(source: string): JsonToken[] {
  const tokens: JsonToken[] = [];
  let cursor = 0;

  for (const match of source.matchAll(JSON_TOKEN_PATTERN)) {
    const matchedText = match[0];
    const start = match.index ?? 0;
    if (start > cursor) {
      tokens.push({
        kind: "plain",
        text: source.slice(cursor, start),
      });
    }

    let kind: JsonTokenKind = "number";
    if (matchedText.startsWith("\"")) {
      kind = matchedText.endsWith(":") ? "key" : "string";
    } else if (matchedText === "true" || matchedText === "false") {
      kind = "boolean";
    } else if (matchedText === "null") {
      kind = "null";
    }

    tokens.push({ kind, text: matchedText });
    cursor = start + matchedText.length;
  }

  if (cursor < source.length) {
    tokens.push({
      kind: "plain",
      text: source.slice(cursor),
    });
  }

  return tokens;
}

export function JsonSyntaxBlock({ value }: JsonSyntaxBlockProps) {
  const tokens = tokenizeJson(formatJson(value));

  return (
    <pre className="JsonSyntaxBlock">
      {tokens.map((token, index) => (
        <span
          key={`${token.kind}-${index}`}
          className={`JsonSyntaxBlock-token JsonSyntaxBlock-token--${token.kind}`}
        >
          {token.text}
        </span>
      ))}
    </pre>
  );
}
