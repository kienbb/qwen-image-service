import { describe, expect, it } from "vitest";
import { curlSnippet, pyLiteral, pythonSnippet, toEndpoint } from "./snippets";

const origin = "https://img.example.com";

describe("toEndpoint", () => {
  it("maps a preset to the OpenAI images endpoint", () => {
    const ep = toEndpoint({ kind: "preset", preset: "pixelart-16bit-64", prompt: "a slime", params: { n: 2, size: "768x768", transparent: true, grid: 32 } });
    expect(ep.path).toBe("/v1/images/generations");
    expect(ep.body).toEqual({
      model: "pixelart-16bit-64",
      prompt: "a slime",
      n: 2,
      size: "768x768",
      background: "transparent",
      response_format: "url",
      grid: 32,
    });
  });
  it("uses /v1/jobs when a preset has reference images", () => {
    const ep = toEndpoint({ kind: "preset", preset: "qwen-image-2.1-turbo", prompt: "edit", params: { refs: ["file_1"] } });
    expect(ep).toEqual({ path: "/v1/jobs", body: { preset: "qwen-image-2.1-turbo", prompt: "edit", params: { refs: ["file_1"] } } });
  });
  it("sends pipelines to /v1/jobs unchanged", () => {
    const body = { ops: [{ op: "generate", prompt: "x" }] };
    expect(toEndpoint({ kind: "pipeline", body })).toEqual({ path: "/v1/jobs", body });
  });
});

describe("snippets", () => {
  it("curl uses the API key placeholder, origin and shell-quoted JSON", () => {
    const s = curlSnippet({ kind: "preset", preset: "p", prompt: "it's a cat", params: {} }, origin);
    expect(s).toContain(`curl -sS ${origin}/v1/images/generations`);
    expect(s).toContain("Bearer $QIS_API_KEY");
    expect(s).toContain("it'\\''s a cat");
  });
  it("python uses the openai SDK for presets", () => {
    const s = pythonSnippet({ kind: "preset", preset: "p", prompt: "cat", params: { n: 2, seed: 7, enhance: true } }, origin);
    expect(s).toContain(`OpenAI(base_url="${origin}/v1"`);
    expect(s).toContain("client.images.generate(");
    expect(s).toContain("n=2,");
    expect(s).toContain('"enhance": True');
    expect(s).toContain('"seed": 7');
  });
  it("python posts pipelines to /jobs", () => {
    const s = pythonSnippet({ kind: "pipeline", body: { ops: [{ op: "pixelize", image: "$inputs.a", dither: false }], inputs: { a: "file_1" } } }, origin);
    expect(s).toContain('client.post("/jobs"');
    expect(s).toContain('"dither": False');
  });
  it("pyLiteral handles null and nesting", () => {
    expect(pyLiteral(null)).toBe("None");
    expect(pyLiteral([])).toBe("[]");
    expect(pyLiteral({ a: [1, "x"] })).toBe('{\n    "a": [\n        1,\n        "x",\n    ],\n}');
  });
});
