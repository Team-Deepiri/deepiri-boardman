import {
  DEFAULT_TIER1_ONLY,
  DEFAULT_TIER2_EXCLUDED,
  fnmatchCase,
  qaTierAllowsRepo,
} from "./qaTierRules";

export type WorkerMember = {
  id: string;
  display?: string;
  roles?: string[];
  qaTier?: number;
  repoGlobs?: string[];
  explicitRepos?: string[];
  weight?: number;
  tier?: string;
};

function normRepo(s: string): string {
  return s.trim().toLowerCase();
}

function repoMatchesMember(fullName: string, m: WorkerMember): boolean {
  const key = normRepo(fullName);
  if (!key) return false;
  for (const ex of m.explicitRepos ?? []) {
    if (normRepo(ex) === key) return true;
  }
  for (const g of m.repoGlobs ?? []) {
    const pat = g.trim().toLowerCase();
    if (!pat) continue;
    if (fnmatchCase(key, pat)) return true;
  }
  return false;
}

function randomChoice<T>(items: T[], weights: number[]): T | null {
  if (!items.length) return null;
  const s = weights.reduce((a, b) => a + b, 0);
  if (s <= 0) return items[Math.floor(Math.random() * items.length)]!;
  let r = Math.random() * s;
  for (let i = 0; i < items.length; i++) {
    r -= weights[i]!;
    if (r <= 0) return items[i]!;
  }
  return items[items.length - 1]!;
}

/**
 * Edge fallback when BOARDMAN_URL is not set: same tier filter + glob match + simple weighted pick.
 * (No overlap-pool partition — keep worker small; use Boardman proxy for full parity.)
 */
export function pickQaLocal(
  repo: string,
  members: WorkerMember[],
  tier2Excluded: readonly string[] = DEFAULT_TIER2_EXCLUDED,
  tier1Only: readonly string[] = DEFAULT_TIER1_ONLY
): { qaId: string | null; reason: string } {
  const fn = repo.trim();
  if (!fn) return { qaId: null, reason: "empty repo" };

  const qas = members.filter(
    (m) =>
      (m.roles ?? []).map((r) => r.toLowerCase()).includes("qa") &&
      repoMatchesMember(fn, m) &&
      qaTierAllowsRepo(m.qaTier ?? 3, fn, tier2Excluded, tier1Only)
  );
  if (!qas.length) return { qaId: null, reason: "no QA after tier/glob filter" };

  const weights = qas.map((m) => Math.max(0.05, m.weight ?? 1));
  const chosen = randomChoice(qas, weights);
  if (!chosen) return { qaId: null, reason: "pick failed" };
  return {
    qaId: chosen.id,
    reason: `qa=${chosen.display ?? chosen.id} (local worker pick)`,
  };
}
