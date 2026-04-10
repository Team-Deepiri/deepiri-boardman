/**
 * Mirror of boardman/assignment/repo_rules.py defaults (fnmatch on owner/repo, lowercased).
 */
export const DEFAULT_TIER2_EXCLUDED = [
  "*diva*",
  "*diri-cyrex*",
  "*diri_cyrex*",
  "*persola*",
  "*cyrex*",
  "*uqe*",
  "*mudspeed*",
  "*agent-testing-utils*",
  "*modelkit*",
  "*agent-toolbox*",
  "*training-orchestrator*",
  "*helox*",
  "*agent-guardrails*",
  "*emotion*desktop*",
  "*emotion-desktop*",
  "*sorge*",
  "*norozo*",
  "*prismpipe*",
  "*boardman*",
] as const;

export const DEFAULT_TIER1_ONLY = [
  "*deepiriweb-frontend*",
  "*deepiriweb_frontend*",
  "*/landing",
  "*-landing",
  "*api-gateway*",
  "*api_gateway*",
  "*apigateway*",
  "*/auth",
  "*-auth",
  "*shared-utils*",
  "*shared_utils*",
  "*deepiri-platform*",
  "*deepiri_platform*",
  "*axiom*",
] as const;

export function fnmatchCase(path: string, pattern: string): boolean {
  const re = new RegExp(
    "^" +
      pattern
        .replace(/[.+^${}()|[\]\\]/g, "\\$&")
        .replace(/\*/g, ".*")
        .replace(/\?/g, ".") +
      "$",
    "i"
  );
  return re.test(path);
}

export function repoMatchesAnyPattern(fullName: string, patterns: readonly string[]): boolean {
  const fn = fullName.trim().toLowerCase();
  if (!fn) return false;
  for (const p of patterns) {
    const pat = p.trim().toLowerCase();
    if (!pat) continue;
    if (fnmatchCase(fn, pat)) return true;
  }
  return false;
}

export function qaTierAllowsRepo(
  qaTier: number,
  fullName: string,
  tier2Excluded: readonly string[],
  tier1Only: readonly string[]
): boolean {
  const t = qaTier === 1 || qaTier === 2 || qaTier === 3 ? qaTier : 3;
  const fn = fullName.trim().toLowerCase();
  if (!fn) return false;
  if (t === 3) return true;
  if (t === 2) return !repoMatchesAnyPattern(fn, tier2Excluded);
  return repoMatchesAnyPattern(fn, tier1Only);
}
