// "Copy as curl / Python" for a playground request. The dashboard submits to /admin/api/playground/jobs
// with the session; API clients use an API key against the public endpoints:
//   preset (no reference images) -> POST /v1/images/generations  (OpenAI images API, model = preset)
//   preset with reference images -> POST /v1/jobs {preset, prompt, params}
//   pipeline                     -> POST /v1/jobs {ops, inputs, outputs}

export interface PresetRequest {
  kind: "preset";
  preset: string;
  prompt: string;
  params: Record<string, unknown>; // n, size, transparent, refs, seed, steps, grid, palette, enhance, ...
}

export interface PipelineRequest {
  kind: "pipeline";
  body: Record<string, unknown>; // {ops, inputs, outputs}
}

export type PlaygroundRequest = PresetRequest | PipelineRequest;

export interface Endpoint {
  path: string;
  body: Record<string, unknown>;
}

export function toEndpoint(req: PlaygroundRequest): Endpoint {
  if (req.kind === "pipeline") return { path: "/v1/jobs", body: req.body };
  const refs = req.params.refs;
  if (Array.isArray(refs) && refs.length) {
    return { path: "/v1/jobs", body: { preset: req.preset, prompt: req.prompt, params: req.params } };
  }
  const { n, size, transparent, ...extra } = req.params;
  const body: Record<string, unknown> = { model: req.preset, prompt: req.prompt };
  if (n !== undefined) body.n = n;
  if (size !== undefined && size !== "auto") body.size = size;
  if (transparent) body.background = "transparent";
  body.response_format = "url";
  Object.assign(body, extra);
  return { path: "/v1/images/generations", body };
}

const shq = (s: string) => `'${s.replace(/'/g, `'\\''`)}'`;

export function curlSnippet(req: PlaygroundRequest, origin: string): string {
  const ep = toEndpoint(req);
  return [
    `curl -sS ${origin}${ep.path} \\`,
    `  -H "Authorization: Bearer $QIS_API_KEY" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -d ${shq(JSON.stringify(ep.body, null, 2))}`,
  ].join("\n");
}

/** JSON -> Python literal (True/False/None, repr-style strings). */
export function pyLiteral(v: unknown, indent = 0): string {
  const pad = "    ".repeat(indent + 1);
  const end = "    ".repeat(indent);
  if (v === null || v === undefined) return "None";
  if (v === true) return "True";
  if (v === false) return "False";
  if (typeof v === "number") return Number.isFinite(v) ? String(v) : "None";
  if (typeof v === "string") return JSON.stringify(v);
  if (Array.isArray(v)) {
    if (!v.length) return "[]";
    return `[\n${v.map((x) => pad + pyLiteral(x, indent + 1)).join(",\n")},\n${end}]`;
  }
  const entries = Object.entries(v as Record<string, unknown>);
  if (!entries.length) return "{}";
  return `{\n${entries.map(([k, x]) => `${pad}${JSON.stringify(k)}: ${pyLiteral(x, indent + 1)}`).join(",\n")},\n${end}}`;
}

export function pythonSnippet(req: PlaygroundRequest, origin: string): string {
  const ep = toEndpoint(req);
  const head = [
    "import os",
    "from openai import OpenAI",
    "",
    `client = OpenAI(base_url="${origin}/v1", api_key=os.environ["QIS_API_KEY"])`,
    "",
  ];
  if (ep.path === "/v1/images/generations") {
    const { model, prompt, n, size, response_format, background, ...extra } = ep.body;
    const args = [`    model=${pyLiteral(model)},`, `    prompt=${pyLiteral(prompt)},`];
    if (n !== undefined) args.push(`    n=${pyLiteral(n)},`);
    if (size !== undefined) args.push(`    size=${pyLiteral(size)},`);
    if (background !== undefined) args.push(`    background=${pyLiteral(background)},`);
    args.push(`    response_format=${pyLiteral(response_format)},`);
    if (Object.keys(extra).length) args.push(`    extra_body=${pyLiteral(extra, 1)},`);
    return [...head, "result = client.images.generate(", ...args, ")", "for img in result.data:", "    print(img.url)"].join("\n");
  }
  return [
    "import httpx",
    ...head,
    "# /v1/jobs is not part of the OpenAI SDK surface: use the client's raw POST",
    `job = client.post("/jobs", cast_to=httpx.Response, body=${pyLiteral(ep.body)}).json()`,
    'print(job["id"], job["status"])',
    "# poll GET /v1/jobs/{id} (or stream /v1/jobs/{id}/events) until status is succeeded",
  ].join("\n");
}
