"""Repo tier classification based on metadata (language, topics, size)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Literal, Optional

from boardman.github.repo_metadata import RepoMetadata

Tier = Literal[1, 2, 3]


LANGUAGE_SCORES = {
    # Heavy / resource-intensive
    "rust": 3,
    "c++": 3,
    "c": 3,
    "java": 3,
    "kotlin": 3,
    "scala": 3,
    "swift": 2,
    # Go is Tier 2 (standard backend)
    "go": 2,
    # Standard
    "python": 1,
    "javascript": 1,
    "typescript": 1,
    "ruby": 1,
    "php": 1,
    "c#": 1,
    # Light / web
    "html": 0,
    "css": 0,
    "scss": 0,
    "vue": 0,
    "svelte": 0,
}

PYTHON_AI_LIBS = {
    "langchain",
    "langchain-community",
    "langchain-core",
    "pytorch",
    "torch",
    "cuda",
    "tensorflow",
    "keras",
    "transformers",
    "huggingface",
    "openai",
    "anthropic",
    "litellm",
    "crewai",
    "autogen",
    "agent",
    "rag",
    "vector-db",
    "chromadb",
    "pinecone",
    "weaviate",
    "qdrant",
    "milvus",
    "llama-index",
    "gpt",
    "llm",
    "ollama",
}

HEAVY_TOPICS = {
    "ai",
    "ml",
    "machine-learning",
    "deep-learning",
    "neural-network",
    "training",
    "model",
    "models",
    "llm",
    "llama",
    "gpt",
    "pytorch",
    "tensorflow",
    "keras",
    "data-science",
    "data-processing",
    "pipeline",
    "etl",
    "mlops",
    "agent",
    "agents",
    "automation",
    "bot",
    "infrastructure",
    "kubernetes",
    "k8s",
    "docker",
    "devops",
    "terraform",
}

MEDIUM_TOPICS = {
    "api",
    "backend",
    "server",
    "database",
    "orm",
    "cli",
    "tool",
    "sdk",
    "library",
    "framework",
    "web",
    "frontend",
    "ui",
    "component",
    "mobile",
    "ios",
    "android",
    "testing",
    "ci",
    "cd",
}


@dataclass
class TierScore:
    language_score: int = 0
    topic_score: int = 0
    size_score: int = 0
    total: int = 0


def _has_python_ai_lib(meta: RepoMetadata) -> bool:
    """Check if Python repo has AI/ML related libs in topics."""
    if (meta.language or "").lower() != "python":
        return False
    for t in meta.topics:
        t_lower = t.lower()
        if t_lower in PYTHON_AI_LIBS:
            return True
    return False


def classify_repo_tier(meta: Optional[RepoMetadata]) -> tuple[Tier, TierScore]:
    """
    Score mapping with rule cascade:
    - Python + AI libs (langchain, pytorch, cuda, etc.) → tier 3
    - Heavy languages (rust, c++, java, etc.) OR heavy topics → tier 3
    - Go → tier 2
    - Large TS/JS (>20MB) → tier 2
    - Small JS/TS → tier 1
    - Medium indicators (swift, medium topics, medium size) → tier 2
    - Otherwise → tier 1
    Default fallback when no metadata: tier 2
    """
    if not meta:
        return 2, TierScore()

    lang = (meta.language or "").lower()
    lang_score = LANGUAGE_SCORES.get(lang, 0)
    
    heavy_count = sum(1 for t in meta.topics if t.lower() in HEAVY_TOPICS)
    medium_count = sum(1 for t in meta.topics if t.lower() in MEDIUM_TOPICS)
    
    if heavy_count >= 1:
        topic_score = 3
    elif medium_count >= 2:
        topic_score = 2
    elif medium_count >= 1:
        topic_score = 1
    else:
        topic_score = 0

    # Size thresholds for TS/JS
    if lang in ("typescript", "javascript"):
        if meta.size_kb > 20000:
            size_score = 2  # Large TS/JS → tier 2
        else:
            size_score = 0  # Small → tier 1
    elif meta.size_kb > 50000:
        size_score = 2
    elif meta.size_kb > 20000:
        size_score = 1
    else:
        size_score = 0

    total = lang_score + topic_score + size_score
    score = TierScore(
        language_score=lang_score,
        topic_score=topic_score,
        size_score=size_score,
        total=total,
    )

    # Rule cascade (priority order)
    # 1. Python + AI libs → tier 3
    if lang == "python" and _has_python_ai_lib(meta):
        return 3, score
    # 2. Heavy languages (rust, c++, java, etc.) OR heavy topics → tier 3
    if lang_score >= 3 or heavy_count >= 1:
        return 3, score
    # 3. Go → tier 2
    if lang == "go":
        return 2, score
    # 4. Large TS/JS → tier 2
    if lang in ("typescript", "javascript") and size_score >= 2:
        return 2, score
    # 5. Medium indicators → tier 2
    if lang_score >= 2 or medium_count >= 2 or size_score >= 1:
        return 2, score
    # 6. Small TS/JS → tier 1
    if lang in ("typescript", "javascript"):
        return 1, score
    
    return 1, score


def classify_repos_tier(
    metadata_map: dict[str, RepoMetadata],
) -> dict[str, Tier]:
    """Classify a batch of repos."""
    result: dict[str, Tier] = {}
    for full_name, meta in metadata_map.items():
        tier, _ = classify_repo_tier(meta)
        result[full_name] = tier
    return result