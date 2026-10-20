"""Profile-based search against Freehire's public, read-only job API."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

import httpx

logger = logging.getLogger(__name__)

FREEHIRE_SEARCH_URL = "https://freehire.me/api/v1/agent/jobs/search"
FREEHIRE_SIMILAR_URL = "https://freehire.me/api/v1/jobs"
MAX_PROFILE_TITLES = 30
MAX_TITLE_ALTERNATIVES = 5
MAX_SKILL_QUERY_GROUPS = 2
MAX_SKILL_QUERY_TERMS = 5
MAX_SEMANTIC_SEEDS = 2
MAX_SIMILAR_RESULTS = 5
MAX_DESCRIPTION_CHARACTERS = 100_000
COMPLETE_DESCRIPTION_MINIMUM = 200
SEARCH_CONCURRENCY = 3
REQUEST_TIMEOUT_SECONDS = 15.0

# Short, transparent aliases broaden title recall without sending a CV.
_TITLE_SYNONYMS = {
    "ai engineer": ("Artificial Intelligence Engineer", "GenAI Engineer"),
    "machine learning engineer": ("ML Engineer",),
    "nlp engineer": ("Natural Language Processing Engineer",),
    "generative ai engineer": ("Generative AI Developer",),
    "llm engineer": ("Large Language Model Engineer",),
    "python developer": ("Python Engineer",),
    "mlops engineer": ("Machine Learning Operations Engineer",),
    "data analyst": ("Data Analytics Engineer",),
    "technical support engineer": ("Application Support Engineer",),
}

# Broad generic terms are useful in a title, but create noisy full-text searches.
_GENERIC_SKILL_TERMS = {
    "python",
    "sql",
    "pandas",
    "numpy",
    "flask",
    "docker",
    "github actions",
    "aws",
    "ec2",
    "s3",
    "lambda",
}

DEFAULT_SEARCH_PROFILE: dict = {
    "skills": [
        "python",
        "sql",
        "nlp",
        "deep learning",
        "genai",
        "llm",
        "rag",
        "embeddings",
        "prompt engineering",
        "transfer learning",
        "pytorch",
        "keras",
        "hugging face",
        "langchain",
        "pandas",
        "numpy",
        "flask",
        "mlflow",
        "dvc",
        "docker",
        "github actions",
        "aws",
        "ec2",
        "s3",
        "lambda",
    ],
    "target_titles": [
        "AI Engineer",
        "Machine Learning Engineer",
        "NLP Engineer",
        "Generative AI Engineer",
        "LLM Engineer",
        "Python Developer",
        "Data Engineer",
        "Data Analyst",
        "MLOps Engineer",
        "Technical Support Engineer",
        "Application Support Engineer",
        "Python Support Engineer",
    ],
    "essential_skills": [],
    "irrelevant_skills": [],
    "weights": {"skills": 0.35, "role": 0.3, "seniority": 0.15, "location": 0.2},
    "country": "AE",
    "city": "dubai",
    "jobage": 14,
    "include_remote": True,
}


class LiveSearchUnavailable(Exception):
    """The configured public source could not return a valid search response."""


def _clean_title(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"[\r\n\"]", " ", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned or len(cleaned) > 100:
        return None
    return cleaned


def build_query_groups(profile: dict, query: str | None = None) -> list[str]:
    """Make bounded title-only OR queries supported by Freehire's public search."""
    if query:
        title = _clean_title(query)
        if not title:
            raise ValueError("query is empty")
        return [f'title:"{title}"']

    titles = profile.get("target_titles", [])
    if not isinstance(titles, list):
        raise ValueError("target_titles must be a list")
    candidates: list[str] = []
    for raw in titles[:MAX_PROFILE_TITLES]:
        title = _clean_title(raw)
        if not title:
            continue
        candidates.append(title)
        candidates.extend(_TITLE_SYNONYMS.get(title.casefold(), ()))
    queries = list(dict.fromkeys(candidates))
    if not queries:
        raise ValueError("search profile has no usable target titles")
    return [
        " OR ".join(
            f'title:"{title}"'
            for title in queries[offset : offset + MAX_TITLE_ALTERNATIVES]
        )
        for offset in range(0, len(queries), MAX_TITLE_ALTERNATIVES)
    ]


def build_skill_query_groups(profile: dict) -> list[str]:
    """Build a small set of profile-skill full-text queries for recall."""
    skills = profile.get("skills", [])
    if not isinstance(skills, list):
        return []
    candidates: list[str] = []
    for raw in skills:
        if not isinstance(raw, str):
            continue
        cleaned = re.sub(r"[^A-Za-z0-9+#./& -]", " ", raw)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()[:80]
        if cleaned and cleaned.casefold() not in _GENERIC_SKILL_TERMS:
            candidates.append(cleaned)
    if not candidates:
        candidates = [
            re.sub(r"\s+", " ", raw).strip()[:80]
            for raw in skills
            if isinstance(raw, str) and raw.strip()
        ]
    terms = list(dict.fromkeys(candidates))[
        : MAX_SKILL_QUERY_GROUPS * MAX_SKILL_QUERY_TERMS
    ]
    return [
        " OR ".join(
            f'"{term}"' for term in terms[offset : offset + MAX_SKILL_QUERY_TERMS]
        )
        for offset in range(0, len(terms), MAX_SKILL_QUERY_TERMS)
    ]


def _within_similarity_filters(
    job: dict, *, country: str, jobage: int, include_remote: bool, today: date
) -> bool:
    """Apply the live-search location and age filters to unfiltered semantic results."""
    posted_at = job.get("posted_at")
    if not isinstance(posted_at, str):
        return False
    try:
        posted = date.fromisoformat(posted_at[:10])
    except ValueError:
        return False
    if posted < today - timedelta(days=max(jobage - 1, 0)):
        return False

    countries = {
        str(value).strip().upper()
        for value in job.get("countries", [])
        if isinstance(value, str)
    }
    work_mode = str(job.get("work_mode") or "").casefold().replace("-", "_")
    is_remote = "remote" in work_mode
    return country.upper() in countries or (include_remote and is_remote)


def _normalized_words(value: object) -> set[str]:
    if not isinstance(value, str):
        return set()
    return set(re.findall(r"[a-z0-9+#]+", value.casefold()))


def _score_fit(job: dict, profile: dict) -> tuple[int, dict]:
    """Deterministic title/skill/location ranking; no external AI call is made."""
    candidate_skills = {
        word
        for value in profile.get("skills", [])
        if isinstance(value, str)
        for word in _normalized_words(value)
    }
    job_skills = {
        word
        for value in job.get("skills", [])
        if isinstance(value, str)
        for word in _normalized_words(value)
    }
    known_skill_score = None
    if job_skills:
        matched = sorted(candidate_skills & job_skills)
        known_skill_score = round(100 * len(matched) / len(job_skills))
    else:
        matched = []

    title_tokens = _normalized_words(job.get("title"))
    role_scores = []
    for target in profile.get("target_titles", []):
        target_tokens = _normalized_words(target)
        if target_tokens:
            role_scores.append(len(title_tokens & target_tokens) / len(target_tokens))
    role_score = round(100 * max(role_scores, default=0.0))

    countries = {str(value).upper() for value in job.get("countries", []) if value}
    cities = {str(value).casefold() for value in job.get("cities", []) if value}
    location_text = str(job.get("location") or "").casefold()
    city = str(profile.get("city") or "dubai").casefold()
    if "AE" in countries:
        location_score = 100 if city in cities or city in location_text else 85
        location_label = "country_match"
    elif str(job.get("work_mode") or "").casefold() == "remote":
        location_score = 40
        location_label = "remote_location_unverified"
    else:
        location_score = 30
        location_label = "location_unverified"

    title = str(job.get("title") or "").casefold()
    if any(
        token in title for token in ("senior", "lead", "principal", "staff", "manager")
    ):
        seniority_score = 25
    elif any(
        token in title for token in ("junior", "entry", "graduate", "intern", "trainee")
    ):
        seniority_score = 100
    else:
        seniority_score = 70

    components = {
        "skills": known_skill_score,
        "role": role_score,
        "seniority": seniority_score,
        "location": location_score,
    }
    weights = profile.get("weights", {})
    usable = [
        (name, score, float(weights.get(name, 0)))
        for name, score in components.items()
        if score is not None and isinstance(weights, dict) and weights.get(name, 0) > 0
    ]
    total_weight = sum(weight for _, _, weight in usable) or 1.0
    overall = round(sum(score * weight for _, score, weight in usable) / total_weight)
    known_weight = sum(
        float(weights.get(name, 0))
        for name, score in components.items()
        if score is not None and isinstance(weights, dict)
    )
    all_weight = (
        sum(float(value) for value in weights.values())
        if isinstance(weights, dict)
        else 1.0
    )
    evidence = {
        "components": components,
        "confidence": round(100 * known_weight / all_weight) if all_weight else 0,
        "matched_skills": matched,
        "location_evidence": location_label,
        "method": "deterministic_profile_overlap",
        "note": "A sorting aid only; not an ATS score or hiring prediction.",
    }
    return overall, evidence


def _valid_source_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            return None
    except ValueError:
        return None
    return candidate


def normalize_job(raw: object, profile: dict, now: str | None = None) -> dict | None:
    if not isinstance(raw, dict):
        return None
    canonical_id = raw.get("public_slug")
    title = raw.get("title")
    source_url = _valid_source_url(raw.get("url"))
    if (
        not isinstance(canonical_id, str)
        or not canonical_id.strip()
        or len(canonical_id) > 200
        or not isinstance(title, str)
        or not title.strip()
        or len(title.strip()) > 500
        or source_url is None
    ):
        return None

    description = raw.get("description")
    if not isinstance(description, str):
        description = ""
    description = description.strip()
    truncated = len(description) > MAX_DESCRIPTION_CHARACTERS
    description = description[:MAX_DESCRIPTION_CHARACTERS]
    status = (
        "partial"
        if truncated
        else "complete"
        if len(description) >= COMPLETE_DESCRIPTION_MINIMUM
        else "partial"
        if description
        else "missing"
    )
    enrichment = (
        raw.get("enrichment") if isinstance(raw.get("enrichment"), dict) else {}
    )
    now = now or datetime.now(timezone.utc).isoformat()
    posted_at = raw.get("posted_at")
    posted_date = None
    if isinstance(posted_at, str):
        try:
            posted_date = date.fromisoformat(posted_at[:10]).isoformat()
        except ValueError:
            pass
    score, evidence = _score_fit(raw, profile)
    return {
        "job_key": f"freehire:{canonical_id.strip()}",
        "canonical_id": canonical_id.strip(),
        "provider": "freehire",
        "source": str(raw.get("source"))[:100] if raw.get("source") else None,
        "title": title.strip(),
        "company": str(raw.get("company"))[:300] if raw.get("company") else None,
        "location": str(raw.get("location"))[:300] if raw.get("location") else None,
        "source_url": source_url,
        "posted_date": posted_date,
        "description_text": description or None,
        "description_status": status,
        "description_method": "freehire_agent_search_text",
        "description_sha256": hashlib.sha256(description.encode("utf-8")).hexdigest()
        if description
        else None,
        "retrieved_at": now,
        "last_checked_at": now,
        "salary": {
            key: enrichment.get(key)
            for key in ("salary_min", "salary_max", "salary_currency", "salary_period")
            if enrichment.get(key) is not None
        },
        "fit_score": score,
        "fit_evidence": evidence,
        "metadata": {
            "work_mode": raw.get("work_mode"),
            "employment_type": enrichment.get("employment_type"),
            "skills": [
                str(value)[:80]
                for value in raw.get("skills", [])[:100]
                if isinstance(value, str)
            ]
            if isinstance(raw.get("skills"), list)
            else [],
            "countries": [
                str(value)[:2]
                for value in raw.get("countries", [])[:100]
                if isinstance(value, str)
            ]
            if isinstance(raw.get("countries"), list)
            else [],
            "cities": [
                str(value)[:80]
                for value in raw.get("cities", [])[:100]
                if isinstance(value, str)
            ]
            if isinstance(raw.get("cities"), list)
            else [],
            "search_methods": [
                str(value)[:40]
                for value in raw.get("_search_methods", [])[:10]
                if isinstance(value, str)
            ]
            if isinstance(raw.get("_search_methods"), list)
            else [],
        },
    }


async def _fetch_group(
    client: httpx.AsyncClient,
    query_group: str,
    *,
    q_fields: str | None = None,
    country: str,
    jobage: int,
    limit: int,
    remote: bool,
) -> tuple[list[dict], int]:
    params = {
        "q": query_group,
        "posted_within_days": str(jobage),
        "description_format": "text",
        "limit": str(limit),
        "offset": "0",
    }
    if q_fields:
        params["q_fields"] = q_fields
    if remote:
        params["work_mode"] = "remote"
    else:
        params["countries"] = country.upper()

    for attempt in range(3):
        try:
            response = await client.get(FREEHIRE_SEARCH_URL, params=params)
        except httpx.HTTPError as exc:
            if attempt == 2:
                raise LiveSearchUnavailable("Freehire could not be reached.") from exc
            await asyncio.sleep(0.25 * (attempt + 1))
            continue
        if response.status_code == 429 or response.status_code >= 500:
            if attempt == 2:
                raise LiveSearchUnavailable(
                    f"Freehire search returned HTTP {response.status_code}."
                )
            await asyncio.sleep(0.5 * (attempt + 1))
            continue
        if response.is_error:
            raise LiveSearchUnavailable(
                f"Freehire search returned HTTP {response.status_code}."
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise LiveSearchUnavailable("Freehire returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise LiveSearchUnavailable("Freehire returned an unexpected response.")
        ignored = payload.get("meta", {}).get("ignored_params", [])
        if ignored:
            raise LiveSearchUnavailable("Freehire ignored one or more search filters.")
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise LiveSearchUnavailable("Freehire returned no job result list.")
        total = payload.get("meta", {}).get("total", len(rows))
        return [row for row in rows if isinstance(row, dict)], int(total) if isinstance(
            total, int
        ) else len(rows)
    raise LiveSearchUnavailable("Freehire search could not be completed.")


async def _fetch_similar(
    client: httpx.AsyncClient, slug: str, *, limit: int
) -> list[dict]:
    """Fetch semantic neighbors for one public Freehire slug."""
    url = f"{FREEHIRE_SIMILAR_URL}/{quote(slug, safe='')}/similar"
    for attempt in range(3):
        try:
            response = await client.get(url, params={"limit": str(limit)})
        except httpx.HTTPError as exc:
            if attempt == 2:
                raise LiveSearchUnavailable(
                    "Freehire semantic search could not be reached."
                ) from exc
            await asyncio.sleep(0.25 * (attempt + 1))
            continue
        if response.status_code == 429 or response.status_code >= 500:
            if attempt == 2:
                raise LiveSearchUnavailable(
                    f"Freehire semantic search returned HTTP {response.status_code}."
                )
            await asyncio.sleep(0.5 * (attempt + 1))
            continue
        if response.is_error:
            raise LiveSearchUnavailable(
                f"Freehire semantic search returned HTTP {response.status_code}."
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise LiveSearchUnavailable(
                "Freehire semantic search returned invalid JSON."
            ) from exc
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise LiveSearchUnavailable(
                "Freehire semantic search returned an unexpected response."
            )
        return [row for row in rows if isinstance(row, dict)]
    return []


async def search_profile(
    *,
    profile: dict,
    query: str | None,
    country: str,
    jobage: int,
    include_remote: bool,
    limit_per_query: int,
) -> dict:
    """Run profile title/skill searches and bounded semantic expansion."""
    title_groups = build_query_groups(profile, query)
    skill_groups = build_skill_query_groups(profile) if query is None else []
    search_specs = [
        {"query": group, "q_fields": "title", "method": "profile_title"}
        for group in title_groups
    ] + [
        {
            "query": group,
            "q_fields": "title,description",
            "method": "profile_skill_full_text",
        }
        for group in skill_groups
    ]
    groups = [spec["query"] for spec in search_specs]
    passes = [(False, country)] + ([(True, country)] if include_remote else [])
    work = [
        (spec, remote, country_code)
        for spec in search_specs
        for remote, country_code in passes
    ]
    semaphore = asyncio.Semaphore(SEARCH_CONCURRENCY)
    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS)
    headers = {"Accept": "application/json", "User-Agent": "JobHunt-Local-Search/1.0"}
    errors: list[str] = []
    all_rows: list[dict] = []
    successful = 0

    async with httpx.AsyncClient(
        timeout=timeout, headers=headers, follow_redirects=False
    ) as client:

        async def fetch_one(
            spec: dict, remote: bool, country_code: str
        ) -> tuple[list[dict], int]:
            async with semaphore:
                return await _fetch_group(
                    client,
                    spec["query"],
                    q_fields=spec["q_fields"],
                    country=country_code,
                    jobage=jobage,
                    limit=limit_per_query,
                    remote=remote,
                )

        results = await asyncio.gather(
            *(
                fetch_one(spec, remote, country_code)
                for spec, remote, country_code in work
            ),
            return_exceptions=True,
        )

    raw_total = 0
    for index, result in enumerate(results):
        if isinstance(result, Exception):
            errors.append(f"Query group {index + 1}: {str(result)[:180]}")
            continue
        successful += 1
        rows, total = result
        raw_total += total
        spec, _remote, _country_code = work[index]
        for row in rows:
            row = dict(row)
            row["_search_methods"] = [spec["method"]]
            all_rows.append(row)

    if successful == 0:
        raise LiveSearchUnavailable("All live search requests failed.")

    normalized: dict[str, dict] = {}

    def add_normalized(raw: dict) -> None:
        item = normalize_job(raw, profile)
        if item is None:
            return
        old = normalized.get(item["job_key"])
        if old is None:
            normalized[item["job_key"]] = item
            return
        old_methods = set(old["metadata"].get("search_methods", []))
        old_methods.update(item["metadata"].get("search_methods", []))
        old["metadata"]["search_methods"] = sorted(old_methods)
        if len(item.get("description_text") or "") > len(
            old.get("description_text") or ""
        ):
            item["metadata"]["search_methods"] = sorted(old_methods)
            normalized[item["job_key"]] = item

    for raw in all_rows:
        add_normalized(raw)

    semantic_errors: list[str] = []
    semantic_requests = 0
    if query is None and normalized:
        seeds = sorted(
            normalized.values(),
            key=lambda item: (
                item.get("fit_score") or 0,
                item.get("posted_date") or "",
            ),
            reverse=True,
        )[:MAX_SEMANTIC_SEEDS]
        semantic_requests = len(seeds)
        async with httpx.AsyncClient(
            timeout=timeout, headers=headers, follow_redirects=False
        ) as client:

            async def fetch_neighbors(seed: dict) -> tuple[dict, list[dict]]:
                async with semaphore:
                    return seed, await _fetch_similar(
                        client, seed["canonical_id"], limit=MAX_SIMILAR_RESULTS
                    )

            neighbors = await asyncio.gather(
                *(fetch_neighbors(seed) for seed in seeds), return_exceptions=True
            )
        today = datetime.now(timezone.utc).date()
        for index, result in enumerate(neighbors):
            if isinstance(result, Exception):
                semantic_errors.append(
                    f"Semantic query {index + 1}: {str(result)[:180]}"
                )
                continue
            _seed, rows = result
            for row in rows:
                if not _within_similarity_filters(
                    row,
                    country=country,
                    jobage=jobage,
                    include_remote=include_remote,
                    today=today,
                ):
                    continue
                row = dict(row)
                row["_search_methods"] = ["semantic_similarity"]
                add_normalized(row)

    jobs = sorted(
        normalized.values(),
        key=lambda item: (
            item.get("fit_score") or 0,
            item.get("posted_date") or "",
            item["title"].casefold(),
        ),
        reverse=True,
    )
    return {
        "query_groups": groups,
        "jobs": jobs,
        "found": len(jobs),
        "source_total": raw_total,
        "successful_requests": successful
        + max(0, semantic_requests - len(semantic_errors)),
        "request_count": len(work) + semantic_requests,
        "errors": errors + semantic_errors,
    }
