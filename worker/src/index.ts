import type { WorkerMember } from "./pickQaLocal";
import { pickQaLocal } from "./pickQaLocal";

export interface Env {
  BOARDMAN_URL?: string;
  WORKER_INTERNAL_SECRET?: string;
  ROUTE_SECRET?: string;
  QA_TEAM_JSON?: string;
}

type PickBody = { repo?: string; members?: WorkerMember[] };

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health") {
      return json({ ok: true, service: "deepiri-boardman-worker" });
    }

    if (request.method !== "POST" || url.pathname !== "/assign-qa") {
      return new Response("Not found", { status: 404 });
    }

    const routeSecret = (env.ROUTE_SECRET ?? "").trim();
    if (!routeSecret) {
      return json({ ok: false, message: "ROUTE_SECRET not configured" }, 503);
    }
    const auth = request.headers.get("Authorization") ?? "";
    if (auth !== `Bearer ${routeSecret}`) {
      return new Response("Unauthorized", { status: 401 });
    }

    let body: PickBody;
    try {
      body = (await request.json()) as PickBody;
    } catch {
      return json({ ok: false, message: "invalid JSON" }, 400);
    }
    const repo = (body.repo ?? "").trim();
    if (!repo) {
      return json({ ok: false, message: "repo required" }, 400);
    }

    const boardman = (env.BOARDMAN_URL ?? "").trim().replace(/\/$/, "");
    const internal = (env.WORKER_INTERNAL_SECRET ?? "").trim();
    if (boardman && internal) {
      const r = await fetch(`${boardman}/api/v1/assignment/pick-qa`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${internal}`,
        },
        body: JSON.stringify({ repo }),
      });
      const text = await r.text();
      return new Response(text, {
        status: r.status,
        headers: { "Content-Type": "application/json" },
      });
    }

    let members = body.members;
    if (!members?.length && env.QA_TEAM_JSON) {
      try {
        members = JSON.parse(env.QA_TEAM_JSON) as WorkerMember[];
      } catch {
        return json({ ok: false, message: "QA_TEAM_JSON invalid" }, 500);
      }
    }
    if (!members?.length) {
      return json(
        {
          ok: false,
          message:
            "Set BOARDMAN_URL+WORKER_INTERNAL_SECRET to proxy, or pass members[] / QA_TEAM_JSON",
        },
        503
      );
    }

    const { qaId, reason } = pickQaLocal(repo, members);
    return json({
      ok: true,
      qa_plaky_id: qaId,
      reason,
      mode: "local",
    });
  },
};

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
