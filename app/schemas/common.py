"""Shared request/response models.

Field names are camelCase on purpose: this is the Gen wire contract, and
consumers written against the same industry-standard shape work unmodified.
"""
from pydantic import BaseModel, ConfigDict, Field


class SearchRequest(BaseModel):
    """Common POST body accepted by every endpoint."""

    model_config = ConfigDict(extra="forbid")

    q: str = Field(min_length=1, description="Search query")
    gl: str = Field("us", description="Country code, ISO-3166 alpha-2")
    hl: str = Field("en", description="UI language")
    location: str | None = Field(None, description="Free-text location bias")
    num: int = Field(10, ge=1, le=100, description="Results per page")
    page: int = Field(1, ge=1, description="Page number")
    tbs: str | None = Field(None, description="Google time filter, e.g. qdr:w")
    debug: bool = Field(False, description="Include providersUsed in the response")


class SearchParameters(BaseModel):
    """Echo of the request, returned in every response."""

    q: str
    gl: str = "us"
    hl: str = "en"
    location: str | None = None
    num: int = 10
    page: int = 1
    tbs: str | None = None
    type: str


class SearchMeta(BaseModel):
    """Machine-readable result-quality signal (genxng-served verticals)."""

    enginesQueried: int
    enginesResponded: int
    engineCoverage: float
    resultCount: int
    duplicateRate: float
    qualityScore: float
    cached: bool = False
    degraded: bool
    rewrittenQuery: str | None = Field(
        None, description="keyword rewrite actually sent to the search backend"
    )


class BaseSearchResponse(BaseModel):
    """Fields every response carries; vertical responses extend this."""

    searchParameters: SearchParameters
    credits: int = 1
    searchMeta: SearchMeta | None = Field(
        None, description="result-quality signal; consumers should act on `degraded`"
    )
    providersUsed: dict[str, str] | None = Field(
        None, description="block name -> provider, present only when debug=true"
    )
