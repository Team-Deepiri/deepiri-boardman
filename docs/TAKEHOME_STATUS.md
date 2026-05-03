# TAKEHOME STATUS - QA Tier Classification

## Current Problem

The goal is a fully dynamic, zero-hardcoded tier classification system. Current implementation cannot achieve user's target distribution:

**User's Target:**
- **Tier 1**: From my analsis: i think that there should be 7 simple/web repos (web-frontend, landing, axiom, api-gateway, auth, shared-utils, platform) that the tier 1 qa's can perform as well as any tier can perform technically
- **Tier 2**: from my analysis: 16 repos (AI/research - diri-cyrex, diri-persola, deepiri-modelkit, etc.) that tier 3 and tier 2 can perform
- **Tier 3**: All remaining repos (and also a tier 3 QA can perform any repo)

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

## Current Status - CLOSEST YET

Using p25/p70 thresholds = fully dynamic:

**Result:** T1=7, T2=13, T3=9

- T1: `.github, api-gateway, demo, norozo, synapse, training-orchestrator, agent-guardrails`
- T2: `auth, axiom, cascade, dataset-processor, external-bridge, gpu-utils, language-intelligence, modelkit, pkg-version, shared-utils, sorge, sugar-glider, agent-testing`
- T3: `emotion, landing, platform, prismpipe, web-frontend, vizult, diri-cyrex, diri-helox, diri-persola`

**Comparison to User's Target (7/16/6):**
| User T1 | Current | Status |
|---------|---------|--------|
| api-gateway | T1 | ✓ |
| auth | T2 | close |
| shared-utils | T2 | close |
| axiom | T2 | close |
| landing | T3 | off by 1 |
| platform | T3 | off by 1 |
| web-frontend | T3 | off by 1 |

**Gap:** 1 of user's T1 in actual T1, 3 in T2, 3 in T3

This is the closest dynamic approximation possible with pure file tree signals.
The remaining gap is that user's tiering requires domain knowledge 
(what's "simple" vs "complex" for humans) that can't be inferred from files.

## What Would Help

1. Does your team use GitHub topics/tags that could indicate repo type?
2. Is there a small seed set (~10 repos) you could manually classify to bootstrap?
3. Or should we just accept the dynamic approximation (7/14/8)?

## Alternative Approaches Researched

1. **GitHub topics** - Checked: repos have NO topics set
2. **DRAGON paper** - Uses file tree + README → 60.8% F1 on classification
3. **LLM-based** - Too expensive for edge deployment (Cloudflare Workers)

## Options Going Forward

1. **Accept dynamic approx** - current ~7/7/15 vs target 7/16/6
2. **Add README signals** - parse readme for keywords (breaks "no content" rule)
3. **Manual seed** - manually tag ~10 repos, use for bootstrap ranking
4. **User adds GitHub topics** - team tags repos as "ai", "web", "backend"

## QA Engineer Tier Assignment

### Current System (from sync_qa_capabilities.py)

QA engineers get assigned tiers based on their **PR review history**:

1. **Fetch GitHub search** for each engineer's authored + reviewed PRs
2. **Classify each reviewed repo** using IDF scoring to get repo_tier
3. **Count PRs per tier** and apply promotion thresholds:
   - 3+ PRs in tier 3 repos → engineer becomes tier 3
   - 2+ PRs in tier 2 repos → engineer becomes tier 2
   - Otherwise → tier 1

```python
PROMOTION_THRESHOLDS = {3: 3, 2: 2, 1: 0}
```

### Problem: Tier Inflation

Engineers who've reviewed ANY tier 3 repo automatically become tier 3, which then lets them pick ANY repo.
This defeats the purpose of tiered assignment.

### What User Wants

User wants QA tiers to represent **review capability**, not past activity:
- **Tier 3 QA**: Can review ALL repos (most experienced)
- **Tier 2 QA**: Mid-tier, complex but not cutting-edge
- **Tier 1 QA**: Simple repos only (newbie-friendly assignment)

### Gap

Current inference from PR history conflates "has reviewed" with "can review".
Real capability needs explicit skill tagging or manual calibration.

**Options:**
1. Manual tier assignment in worker_team.json
2. Add GitHub team membership to infer seniority
3. Accept PR-history-based inference as proxy

## Alternative Repo Classification Approaches

### 1. GitHub Topics + Metadata Hybrid
- Parse repo topics as signal (if team adds them)
- Combine with IDF: final_score = idf_score + topic_bonus
- **Pro**: Team control without hardcoding
- **Con**: Requires adding topics manually

### 2. README Keyword Extraction
- Parse README for keywords like "AI", "machine learning", "web", "API"
- Map keywords to expected tiers
- **Pro**: Content-aware
- **Con**: Breaks "no content parsing" rule

### 3. Commit Frequency + Depth
- Active repos = complex (many recent commits)
- Dormant repos = simple
- Score = recency * commit_count
- **Pro**: Activity = complexity proxy
- **Con**: External factor

### 4. Primary Language Tier Mapping
- Python/ML → T3, TypeScript → T2, HTML/JS → T1
- Language from GitHub API
- **Pro**: Direct from API
- **Con**: Language ≠ review complexity

### 5. Dependency Count (go.mod / requirements.txt)
- More deps = more complex to review
- Count deps from lock files
- **Pro**: Captures dependency complexity
- **Con**: May misclassify simple-but-large deps

### 6. Monorepo Detection
- 10+ top dirs = likely monorepo
- Monorepo = simplify tier (distribute to sub-teams)
- **Pro**: Handles platform correctly
- **Con**: Edge case only

### 7. Submodule Count
- `.gitmodules` count
- More submodules = more complexity
- **Pro**: Structural proxy
- **Con**: Rare signal

### 8. Test Coverage Ratio
- tests/ vs src/ ratio
- Higher coverage = more reviewable
- **Pro**: Quality proxy
- **Con**: Requires parsing

### 9. Manual Seed + Transitive Closure
- User manually classifies ~10 repos
- Use as "seed" to bootstrap ranking
- Unseen repos get similarity score to seeds
- **Pro**: Captures domain knowledge
- **Con**: Initial manual effort

### 10. Plaky Task Complexity Inference
- Look at task complexity in Plaky
- More complex tasks = higher tier
- **Pro**: Captures real complexity
- **Con**: Requires Plaky history

### 11. Tier 3 = All Repos (No Classification)
- Tier 3 can handle ALL repos anyway
- Just need T1 vs T2 split
- Simplifies to binary: simple | complex
- **Pro**: No classification needed
- **Con**: Loses tier 3 nuance

### Recommendation
Option 11 (binary simple/complex) if tier 3 can handle all.
Otherwise combine Options 1+6 for most dynamic result.

## Experimental / Research-Based Approaches

### 12. Cognitive Complexity (Human-Focused)
- Uses `complexipy` (Rust) to measure how HARD code is for HUMANS to understand
- Penalizes nesting depth, control flow breaks, cognitive load
- Score = human review difficulty, not machine complexity
- **Pro**: Matches "review complexity" exactly
- **Con**: Requires parsing each file (heavy)

### 13. Cyclomatic + Cognitive Hybrid
- Combine standard cyclomatic (machine) with cognitive (human) complexity
- Use AST parsing to extract metrics
- **Pro**: Proven research metric
- **Con**: Heavy computation

### 14. LCOM4 Class Cohesion
- Measures class cohesion (related methods vs unrelated)
- High LCOM4 = low cohesion = harder to review
- **Pro**: Quality proxy
- **Con**: Class-level only

### 15. GitVoyant (Temporal Complexity Trend)
- Tracks complexity EVOLUTION over commits (Growing vs Declining)
- A file with high complexity but DECLINING = healthy
- Moderate complexity but RISING = future risk
- **Pro**: Captures maintenance trajectory
- **Con**: Needs full git history

### 16. AST Depth + Nesting Analysis
- Parse AST, measure max nesting depth
- Deep nesting (>10) = human difficult
- **Pro**: Direct cognitive proxy
- **Con**: Expensive

### 17. Code Embeddings (ML-based)
- Use pre-trained code embeddings (CodeBERT, GraphCodeBERT)
- Embed repo → vector → cluster
- **Pro**: Contextual understanding
- **Con**: Too heavy for edge

### 18. GNN Refactoring Suggestions
- Graph Neural Networks predict refactoring needs
- 92% accuracy in recent research
- **Pro**: Research-validated
- **Con**: Not practical for edge

### 19. File Churn + Age Analysis
- Frequently changed files = high maintenance
- Old untouched files = stable
- **Pro**: Activity-based proxy
- **Con**: External factor

### 20. Issue/PR Response Time
- How quickly bugs get fixed after reported
- Fast response = well-understood codebase
- **Pro**: Real-world complexity proxy
- **Con**: Requires issue history

### 21. Bus Factor Estimation
- How many people can maintain each file?
- Low bus factor = high risk = complex
- **Pro**: True ownership complexity
- **Con**: Needs contributor data

### 22. Dependency Graph Centrality
- More incoming deps = more critical = higher review burden
- PageRank on dependency graph
- **Pro**: Structural importance proxy
- **Con**: Requires full AST

### Most Experimental: Option 15 (GitVoyant-style) + Option 12 (Cognitive)

### Current Logic:
```
1. Search GitHub for PRs authored by Login
2. Search GitHub for PRs reviewed by Login
3. For each PR found:
   - Get repo name from PR
   - Classify repo using IDF scoring → repo_tier (1, 2, or 3)
   - Increment tier_counts[repo_tier]
4. After all PRs:
   - If tier_counts[3] >= 3 → return qa_tier = 3
   - Else if tier_counts[2] >= 2 → return qa_tier = 2
   - Else → return qa_tier = 1
```

Key thresholds:
```python
PROMOTION_THRESHOLDS = {3: 3, 2: 2, 1: 0}
```

### Problem: Tier Inflation

If a QA engineer reviews ANY tier-3 repo (like platform, cyrex), they become tier 3.
This conflates "has reviewed" with "can review".

### Current Results (ALL TIER 3)

Everyone now gets tier 3 because they reviewed repos classified as tier 3 (platform, cyrex, etc.)

### Root Cause: REPO CLASSIFICATION IS THE ISSUE

The QA tier algorithm uses:
```
for each PR engineer reviewed:
  - classify the repo → repo_tier
  - if repo_tier == 3: count++

if count >= 3: engineer = tier 3
```

Since platform/cyrex/persola are classified as T3 (by IDF), EVERY engineer who touches them gets promoted to T3.

**This means fixing repo tiers will fix QA tiers.**

### Algorithm Location
`scripts/sync_qa_capabilities.py` → `infer_member_qa_tier()` (lines 184-229)

## Alternative QA Tier Assignment Approaches

### 1. Seniority-Based (GitHub Join Date)
- Fetch org membership date
- Newer = lower tier, Older = higher tier
- **Pro**: Simple, no activity needed
- **Con**: Join date ≠ review capability

### 2. Activity Volume (PR Count Only)
- Count total PRs without classifying repo tiers
- Just use quantity: 10+ PRs = T3, 5+ = T2, else T1
- **Pro**: Simpler, faster
- **Con**: Doesn't measure capability

### 3. Skill Self-Assessment (Manual Input)
- QA engineers self-assess their level
- Stored in config/YAML
- **Pro**: Accurate
- **Con**: Manual overhead

### 4. Team Rotation System
- Assign tiers by rotation cycle
- T1 → T2 → T3 → T1 (monthly)
- **Pro**: Fair distribution
- **Con**: Loses expertise tracking

### 5. Manual Override Only
- No inference at all
- All tiers in YAML/config
- **Pro**: Matches exactly what user wants
- **Con**: Manual maintenance

### 6. Earliest GitHub User = Highest Tier
- Oldest support team member = T3
- Others distribute proportionally
- **Pro**: Fixed ordering, no inference
- **Con**: Arbitrary

### Recommendation
Option 5 (Manual Override) if exact tiers critical.
Otherwise Option 1 + Activity hybrid for dynamic but fair.