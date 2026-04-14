# TAKEHOME STATUS - QA Tier Classification

## Current Problem

The goal is a fully dynamic, zero-hardcoded tier classification system. Current implementation cannot achieve user's target distribution:

**User's Target:**
- **Tier 1**: 7 simple/web repos (web-frontend, landing, axiom, api-gateway, auth, shared-utils, platform)
- **Tier 2**: 16 repos (AI/research - diri-cyrex, diri-persola, deepiri-modelkit, etc.)
- **Tier 3**: All remaining repos - can handle ANY repo

**Current Issue:**
- IDF-based scores from file tree signals don't correlate with user's domain knowledge
- raw_scores_sorted by IDF: 15, 39, 48, 51, 53, 53, 64, 69, 74, 83... → max 1272
- User's tier list is based on domain knowledge (web vs AI), not file structure

## Why This Is Hard

1. **File signals alone**: `dir:src`, `file:main.py`, `language:python` - cannot distinguish "AI research" from "simple web service"

2. **Percentile thresholds**: Need to map IDF percentile to user's tier classification
   - Bottom 25% ≈ 7 repos but include: `.github, training-orchestrator, norozo, synapse, demo, api-gateway, gpu-utils...`
   - User wants tier 1: `api-gateway, auth, platform` NOT tier 2

3. **Zero hardcoding constraint**: We cannot encode "AI repos" as keywords

## What's Been Tried

1. Pure IDF scores (file tree signals weighted by rarity)
2. Structural complexity (dir depth, file count, top-level dirs)
3. Log compression for outliers
4. Various percentile splits (p50/p80, p25/p75, rank-based)
5. IQR/MAD outlier filtering

None achieve user's 7/16/6 distribution without hardcoding repo names.

## Root Cause

**File tree signals measure lexical rarity, not domain complexity.**

- `deepiri-platform` has 1754 files, 15 top dirs → high IDF → tier 3
- User wants tier 1 (simple web service)
- Can't distinguish from file signals alone

## Possible Solutions

1. **Accept approximation**: Best-effort dynamic split without exact match
2. **GitHub topics/tags**: If team tags repos as "ai", "research", etc. - use those
3. **PR history tagging**: Infer from past QA assignments
4. **Manual seed**: User provides small seed set to bootstrap ranking

## Current Status (After Latest Run)

Using Q1/Q3 percentiles = fully dynamic, zero hardcoded:

**Result:**
- T1=7: `.github, api-gateway, demo, norozo, synapse, training-orchestrator, agent-guardrails`
- T2=14: `auth, axiom, cascade, dataset-processor, external-bridge, gpu-utils, language-intelligence, modelkit, pkg-version, shared-utils, sorge, sugar-glider, vizult, agent-testing`
- T3=8: `emotion, landing, platform, prismpipe, web-frontend, diri-cyrex, diri-helox, diri-persola`

**Vs User's Target:**
- User wants T1: `web-frontend, landing, axiom, api-gateway, auth, shared-utils, platform`
- Current T1: `api-gateway` matches, but has `demo, training-orchestrator` instead of `landing, platform, auth`

**Key Insight:**
The problem is file signals (IDF) measure lexical rarity, NOT:
- Complexity for humans to review
- Domain knowledge needed (AI vs web)
- Skill level to understand code

**deepiri-platform** scores highest (1272) because it has many unique signals (monorepo).
User wants it in T1 (simple web service tier).

This requires domain knowledge that cannot be inferred from file tree alone.

## What Would Help

1. Does your team use GitHub topics/tags that could indicate repo type?
2. Is there a small seed set (~10 repos) you could manually classify to bootstrap?
3. Or should we just accept the dynamic approximation (7/14/8)?