import type { StageSourceCodeData } from "../types.js";

interface StageSourceCodeBlockProps {
  payload: {
    view: {
      type: "source-code";
      title?: string;
    };
    data: StageSourceCodeData;
  };
}

type SourceTokenKind =
  | "plain"
  | "comment"
  | "string"
  | "keyword"
  | "number"
  | "decorator"
  | "constant";

interface SourceToken {
  kind: SourceTokenKind;
  text: string;
}

const SOURCE_TOKEN_PATTERN =
  /("""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|#.*$|@[A-Za-z_][\w-]*|\b(?:and|as|assert|break|case|class|continue|def|del|elif|else|except|False|finally|for|from|if|import|in|is|lambda|match|None|nonlocal|not|or|pass|raise|return|struct|True|try|while|with|yield)\b|\b\d+(?:\.\d+)?\b)/g;

function tokenizeSourceLine(line: string): SourceToken[] {
  const tokens: SourceToken[] = [];
  let cursor = 0;

  for (const match of line.matchAll(SOURCE_TOKEN_PATTERN)) {
    const matchedText = match[0];
    const start = match.index ?? 0;

    if (start > cursor) {
      tokens.push({
        kind: "plain",
        text: line.slice(cursor, start),
      });
    }

    let kind: SourceTokenKind = "plain";
    if (matchedText.startsWith("#")) {
      kind = "comment";
    } else if (
      matchedText.startsWith("\"") ||
      matchedText.startsWith("'")
    ) {
      kind = "string";
    } else if (matchedText.startsWith("@")) {
      kind = "decorator";
    } else if (/^(True|False|None)$/.test(matchedText)) {
      kind = "constant";
    } else if (/^\d/.test(matchedText)) {
      kind = "number";
    } else {
      kind = "keyword";
    }

    tokens.push({ kind, text: matchedText });
    cursor = start + matchedText.length;
  }

  if (cursor < line.length) {
    tokens.push({
      kind: "plain",
      text: line.slice(cursor),
    });
  }

  if (tokens.length === 0) {
    tokens.push({
      kind: "plain",
      text: "",
    });
  }

  return tokens;
}

function lineRangeLabel(startLine: number, endLine: number): string {
  return startLine === endLine ? `${startLine}` : `${startLine}-${endLine}`;
}

export function StageSourceCodeBlock({ payload }: StageSourceCodeBlockProps) {
  const lines = payload.data.code.split("\n");

  return (
    <div className="StageSourceCodeBlock">
      <div className="StageSourceCodeBlock-header">
        <div className="StageSourceCodeBlock-headingGroup">
          <span className="StageSourceCodeBlock-label">Source</span>
          {payload.view.title ? (
            <span className="StageSourceCodeBlock-title">{payload.view.title}</span>
          ) : null}
          <span className="StageSourceCodeBlock-path">
            {payload.data.path}:{lineRangeLabel(payload.data.startLine, payload.data.endLine)}
          </span>
        </div>
      </div>
      <div className="StageSourceCodeBlock-scroll">
        <div
          className="StageSourceCodeBlock-code"
          role="presentation"
          aria-label={`${payload.data.language} source code`}
        >
          {lines.map((line, index) => (
            <div className="StageSourceCodeBlock-line" key={`${payload.data.path}-${payload.data.startLine + index}`}>
              <span className="StageSourceCodeBlock-lineNumber">
                {payload.data.startLine + index}
              </span>
              <span className="StageSourceCodeBlock-lineText">
                {tokenizeSourceLine(line).map((token, tokenIndex) => (
                  <span
                    key={`${payload.data.startLine + index}-${tokenIndex}`}
                    className={`StageSourceCodeBlock-token StageSourceCodeBlock-token--${token.kind}`}
                  >
                    {token.text}
                  </span>
                ))}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
