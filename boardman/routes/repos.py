"""API endpoints for repo tier classification."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from boardman.assignment.tier_classifier import classify_repo_tier, classify_repos_tier
from boardman.github.repo_metadata import fetch_repo_metadata, fetch_repos_metadata
from boardman.repos_config import _load_raw, routing_yaml_candidate_map_keys, update_repo_tiers
from boardman.settings import settings

router = APIRouter(prefix="/repos", tags=["repos"])


class ClassifyReposResponse(BaseModel):
    ok: bool
    classified: int = 0
    results: dict[str, int] = {}
    error: str | None = None


@router.post("/classify", response_model=ClassifyReposResponse)
async def classify_all_repos() -> ClassifyReposResponse:
    """Fetch metadata for all org repos and classify into tiers."""
    if not settings.github_pat:
        raise HTTPException(status_code=400, detail="GITHUB_PAT not configured")

    from boardman.github.org_repos import fetch_org_repository_full_names

    client = httpx.AsyncClient(timeout=60.0)
    try:
        org_names = await fetch_org_repository_full_names(
            client,
            settings.github_org,
            skip_archived=settings.github_skip_archived,
        )

        metadata_map = await fetch_repos_metadata(client, org_names)
        tier_map = classify_repos_tier(metadata_map)

        # Update repos.yml
        update_repo_tiers(tier_map)

        return ClassifyReposResponse(
            ok=True,
            classified=len(tier_map),
            results=tier_map,
        )
    except Exception as e:
        return ClassifyReposResponse(ok=False, error=str(e))
    finally:
        await client.aclose()


class SingleRepoResponse(BaseModel):
    full_name: str
    tier: int = 2
    metadata: dict | None = None


@router.get("/tier/{full_name:path}", response_model=SingleRepoResponse)
async def get_repo_tier(full_name: str) -> SingleRepoResponse:
    """Get tier for a specific repo (from repos.yml or compute on-the-fly)."""
    raw = _load_raw()
    repos = raw.get("repos", {})
    short_repo = full_name.split("/", 1)[1] if "/" in full_name else full_name
    entry = None
    for key in routing_yaml_candidate_map_keys(full_name, short_repo, settings.github_org):
        candidate = repos.get(key)
        if isinstance(candidate, dict):
            entry = candidate
            break

    if entry and isinstance(entry, dict) and entry.get("tier"):
        tier = int(entry["tier"])
    else:
        # Compute on-the-fly
        if not settings.github_pat:
            return SingleRepoResponse(full_name=full_name, tier=2)

        client = httpx.AsyncClient(timeout=30.0)
        try:
            owner, repo = full_name.split("/", 1) if "/" in full_name else ("", "")
            meta = await fetch_repo_metadata(client, owner, repo) if owner and repo else None
            tier, scores = classify_repo_tier(meta)
            return SingleRepoResponse(
                full_name=full_name,
                tier=tier,
                metadata=(
                    {
                        "language": meta.language if meta else None,
                        "topics": meta.topics if meta else [],
                        "size_kb": meta.size_kb if meta else None,
                        "scores": {
                            "idf_score": scores.idf_score,
                            "structural_score": scores.structural_score,
                            "total": scores.total,
                        },
                    }
                    if meta
                    else None
                ),
            )
        except Exception:
            return SingleRepoResponse(full_name=full_name, tier=2)
        finally:
            await client.aclose()

    return SingleRepoResponse(full_name=full_name, tier=tier)


class OrgReposResponse(BaseModel):
    ok: bool
    repos: list[str] = []
    message: str | None = None


@router.get("/org", response_model=OrgReposResponse)
async def list_org_repositories() -> OrgReposResponse:
    """List full names (owner/repo) for repositories in the configured GitHub org."""
    if not settings.github_pat:
        return OrgReposResponse(ok=False, repos=[], message="GITHUB_PAT not configured")

    from boardman.github.org_repos import fetch_org_repository_full_names

    client = httpx.AsyncClient(timeout=60.0)
    try:
        names = await fetch_org_repository_full_names(
            client,
            settings.github_org,
            skip_archived=settings.github_skip_archived,
        )
        return OrgReposResponse(ok=True, repos=names)
    except Exception as e:
        return OrgReposResponse(ok=False, repos=[], message=str(e))
    finally:
        await client.aclose()
