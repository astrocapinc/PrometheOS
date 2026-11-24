// Shared test fixtures: the running hub's info, one-use pairing tokens (one per pairing),
// a separately paired API client for asserting real state, and helpers.
import {
  expect,
  request as pwRequest,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
// Every journey's `test` is the guarded one: each worker checks its browser against the boundary.
import { guardedTest as base } from "./boundary-guard";

const HERE = fileURLToPath(new URL(".", import.meta.url));
const STATE = join(HERE, ".state");
// Supplementary screenshots go to the run's own directory (tools/run_hub_e2e.py sets E2E_SHOTS),
// never to the tracked hub/screenshots, which a run must not rewrite (decision 0028).
export const SHOTS = process.env.E2E_SHOTS ?? join(HERE, "..", "test-results", "screenshots");

export interface HubInfo {
  url: string;
  port: number;
  project_id: string;
  meeting_id: string;
  correction_id: string;
  overbroad_claim: string;
  narrow_claim: string;
  pairing_tokens: string[];
  seed: {
    skill_ref: string;
    audit_entry_id: string;
    audit_version_id: string;
    audit_lines: [number, number];
    paper_id: string;
    anchor_id: string;
    pdf_version_id: string;
    venue_version_id: string;
    manuscript_version_id: string;
    // CP9 LaTeX workspace linked to a local bare repository standing in for Overleaf.
    latex_paper_id: string;
    latex_claim_id: string;
    latex_anchor_id: string;
    latex_folder: string;
    latex_collaborator: string;
    latex_bare: string;
    latex_anchor_line: number;
    tex_engine: string;
  };
}

export function readHub(): HubInfo {
  return JSON.parse(readFileSync(join(STATE, "hub.json"), "utf-8")) as HubInfo;
}

/** Next unused one-use pairing token (tests run in a single worker). */
export function nextToken(hub: HubInfo): string {
  const counterFile = join(STATE, "token-counter.txt");
  const n = Number(readFileSync(counterFile, "utf-8"));
  const token = hub.pairing_tokens[n];
  if (!token) throw new Error("out of pairing tokens; raise HUB_TOKENS");
  writeFileSync(counterFile, String(n + 1));
  return token;
}

export function origin(hub: HubInfo): string {
  return hub.url.replace(/\/$/, "");
}

/** Pair the page's browser context through the real /pair route (cookie lands in the context). */
export async function pairContext(page: Page, hub: HubInfo): Promise<void> {
  const reply = await page.request.post(`${origin(hub)}/api/v1/pair`, {
    data: { token: nextToken(hub) },
    headers: { Origin: origin(hub) },
  });
  expect(reply.status(), await reply.text()).toBe(200);
}

export interface Rec<B = any> {
  id: string;
  rev: number;
  kind: string;
  body: B;
  recorded_at: string;
}

export class Api {
  constructor(
    readonly ctx: APIRequestContext,
    readonly csrf: string,
    readonly hub: HubInfo,
  ) {}

  async get<T = any>(path: string): Promise<T> {
    const reply = await this.ctx.get(`/api/v1${path}`);
    expect(reply.status(), `${path}: ${await reply.text()}`).toBe(200);
    return (await reply.json()) as T;
  }

  async post<T = any>(path: string, body: unknown, status = 200): Promise<T> {
    const reply = await this.ctx.post(`/api/v1${path}`, {
      data: body,
      headers: { "x-prometheos-csrf": this.csrf },
    });
    expect(reply.status(), `${path}: ${await reply.text()}`).toBe(status);
    return (await reply.json()) as T;
  }

  async seq(): Promise<number> {
    return (await this.get<{ project_seq: number }>("/project")).project_seq;
  }

  /** An ordinary typed note through POST /transactions (used to move the project on). */
  async createNote(title: string, text = "Written by the test through the API."): Promise<string> {
    const id = `entry_${crypto.randomUUID()}`;
    await this.post("/transactions", {
      schema_version: 1,
      project_id: this.hub.project_id,
      request_id: crypto.randomUUID(),
      expected_seq: await this.seq(),
      operations: [
        {
          op: "create",
          kind: "entry",
          id,
          base_rev: 0,
          sensitivity: "normal",
          occurred_at: null,
          body: {
            title,
            text,
            category: "note",
            evidence: [],
            claim_refs: [],
            author_ref: null,
            origin: "typed",
            quantities: [],
            review: "reviewed",
          },
        },
      ],
    });
    return id;
  }

  async records<B = any>(kind: string): Promise<Rec<B>[]> {
    return (await this.get<{ items: Rec<B>[] }>(`/records?kind=${kind}&limit=200`)).items;
  }

  async record<B = any>(id: string): Promise<Rec<B>> {
    return this.get<Rec<B>>(`/records/${id}`);
  }
}

export const test = base.extend<{ hub: HubInfo; api: Api; paired: Page }>({
  // eslint-disable-next-line no-empty-pattern
  hub: async ({}, use) => {
    await use(readHub());
  },
  api: async ({ hub }, use) => {
    const ctx = await pwRequest.newContext({
      baseURL: origin(hub),
      extraHTTPHeaders: { Origin: origin(hub) },
    });
    const reply = await ctx.post("/api/v1/pair", { data: { token: nextToken(hub) } });
    expect(reply.status()).toBe(200);
    const { csrf_token } = (await reply.json()) as { csrf_token: string };
    await use(new Api(ctx, csrf_token, hub));
    await ctx.dispose();
  },
  paired: async ({ page, hub }, use) => {
    await pairContext(page, hub);
    await use(page);
  },
});

export { expect };

export async function shot(page: Page, name: string): Promise<void> {
  mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: join(SHOTS, `${name}.png`), fullPage: true });
}

export const nav = (page: Page, name: "Now" | "Work items" | "Notebook" | "Knowledge" | "Papers") =>
  page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name, exact: true });
