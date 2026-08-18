#!/usr/bin/env node
/**
 * Black-box target: official TypeScript SDK (@modelcontextprotocol/sdk) server over stdio.
 *
 * A thin, unmodified use of the official SDK. The harness must not special-case
 * it and this file must not paper over SDK behaviour — whatever appears on the
 * wire is the observation.
 *
 * Pinned by `targets/typescript/sdk-server.yaml`.
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

const server = new Server(
  { name: "interop-lab-typescript-sdk-target", version: "0.1.0" },
  { capabilities: { tools: {} } },
);

const TOOLS = [
  {
    name: "echo",
    description: "Echo the provided text back to the caller.",
    inputSchema: {
      type: "object",
      properties: { text: { type: "string" } },
      required: ["text"],
    },
  },
  {
    name: "slow_echo",
    description: "Echo after an optional caller-supplied delay, for timeout scenarios.",
    inputSchema: {
      type: "object",
      properties: {
        text: { type: "string" },
        delay_ms: { type: "integer", default: 0 },
      },
      required: ["text"],
    },
  },
];

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  const text = (args?.text ?? "");

  if (name === "slow_echo") {
    const delayMs = Number(args?.delay_ms ?? 0);
    if (delayMs > 0) {
      await new Promise((resolve) => setTimeout(resolve, delayMs));
    }
  } else if (name !== "echo") {
    throw new Error(`unknown tool ${name}`);
  }

  return { content: [{ type: "text", text: String(text) }], isError: false };
});

const transport = new StdioServerTransport();
await server.connect(transport);
