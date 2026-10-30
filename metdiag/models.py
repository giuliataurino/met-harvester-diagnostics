"""Met object model. Same as paa_graph_kb/models/met/models.py; kept here so the tool stands alone."""
from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _blank_to_none(v: Any) -> Any:
    return None if isinstance(v, str) and not v.strip() else v


class Constituent(BaseModel):
    constituentID: Optional[int] = None
    role: Optional[str] = None
    name: Optional[str] = None
    constituentULAN_URL: Optional[str] = None
    constituentWikidata_URL: Optional[str] = None
    gender: Optional[str] = None

    model_config = ConfigDict(extra="ignore")
    _blank = field_validator("*", mode="before")(_blank_to_none)


class Tag(BaseModel):
    term: Optional[str] = None
    AAT_URL: Optional[str] = None
    Wikidata_URL: Optional[str] = None

    model_config = ConfigDict(extra="ignore")
    _blank = field_validator("*", mode="before")(_blank_to_none)


class Measurement(BaseModel):
    elementName: Optional[str] = None
    elementDescription: Optional[str] = None
    elementMeasurements: Optional[dict[str, Any]] = None  # e.g. {"Height": 10.8, "Diameter": 8.2}, cm / kg

    model_config = ConfigDict(extra="ignore")


class MetObject(BaseModel):
    """One record from GET /public/collection/v1/objects/{objectID}.

    Every field documented at https://metmuseum.github.io/#object is here. URLs are kept as plain
    strings on purpose: the Met occasionally serves image URLs that strict URL validation rejects,
    and a validation error would silently drop the whole record.
    """

    objectID: int
    isHighlight: bool = False
    isPublicDomain: bool = False
    isTimelineWork: bool = False
    accessionNumber: Optional[str] = None
    accessionYear: Optional[int] = None

    primaryImage: Optional[str] = None
    primaryImageSmall: Optional[str] = None
    additionalImages: List[str] = Field(default_factory=list)
    constituents: Optional[List[Constituent]] = None

    department: Optional[str] = None
    objectName: Optional[str] = None
    title: Optional[str] = None
    culture: Optional[str] = None
    period: Optional[str] = None
    dynasty: Optional[str] = None
    reign: Optional[str] = None
    portfolio: Optional[str] = None

    # Flat primary-artist block; the same data appears structured in `constituents`
    artistRole: Optional[str] = None
    artistPrefix: Optional[str] = None
    artistDisplayName: Optional[str] = None
    artistDisplayBio: Optional[str] = None
    artistSuffix: Optional[str] = None
    artistAlphaSort: Optional[str] = None
    artistNationality: Optional[str] = None
    artistBeginDate: Optional[str] = None
    artistEndDate: Optional[str] = None
    artistGender: Optional[str] = None
    artistWikidata_URL: Optional[str] = None
    artistULAN_URL: Optional[str] = None

    objectDate: Optional[str] = None
    objectBeginDate: Optional[int] = None
    objectEndDate: Optional[int] = None
    medium: Optional[str] = None
    dimensions: Optional[str] = None
    dimensionsParsed: Optional[Any] = None
    measurements: Optional[List[Measurement]] = None
    creditLine: Optional[str] = None

    geographyType: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    county: Optional[str] = None
    country: Optional[str] = None
    region: Optional[str] = None
    subregion: Optional[str] = None
    locale: Optional[str] = None
    locus: Optional[str] = None
    excavation: Optional[str] = None
    river: Optional[str] = None

    classification: Optional[str] = None
    rightsAndReproduction: Optional[str] = None
    linkResource: Optional[str] = None
    metadataDate: Optional[str] = None
    repository: Optional[str] = None
    objectURL: Optional[str] = None
    tags: Optional[List[Tag]] = None
    objectWikidata_URL: Optional[str] = None
    GalleryNumber: Optional[str] = None

    model_config = ConfigDict(extra="ignore")
    _blank = field_validator("*", mode="before")(_blank_to_none)

    @field_validator("additionalImages", mode="before")
    @classmethod
    def _drop_blank_urls(cls, v: Any) -> Any:
        return [u for u in (v or []) if isinstance(u, str) and u.strip()]

    @field_validator("constituents", "tags", "measurements", mode="before")
    @classmethod
    def _drop_null_items(cls, v: Any) -> Any:
        return [x for x in v if x] if isinstance(v, list) else v
