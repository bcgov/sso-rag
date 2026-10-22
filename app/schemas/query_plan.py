from typing import Literal

from pydantic import BaseModel, Field

Complexity = Literal["simple", "multi_facet", "ambiguous"]
FilterOperator = Literal["equals", "in", "list_contains"]


class SubQuestion(BaseModel):
    facet: str = Field(description="Short label for one independent part of the question")
    query: str = Field(description="Search query targeting only this facet, in the docs' vocabulary")


class MetadataFilter(BaseModel):
    field: str = Field(description="Metadata field name, e.g. doc_type or release_version")
    operator: FilterOperator = Field(description="equals, in, or list_contains")
    value: str | list[str] = Field(description="Value, or list of values for 'in'")
    justification: str = Field(description="Words in the user's request that justify this filter")


class QueryPlan(BaseModel):
    # Set by code after the LLM responds, so it is always the verbatim input.
    original_question: str = Field(
        default="",
        description="Leave empty; filled in by the system"
    )
    canonical_query: str = Field(
        description="Self-contained restatement preserving the user's intent"
    )
    query_variants: list[str] = Field(
        default_factory=list,
        max_length=4,
        description="Three or four distinct, complementary retrieval queries"
    )
    sub_questions: list[SubQuestion] = Field(
        default_factory=list,
        max_length=4,
        description="Independent parts of a multi-part question; empty for single-topic questions"
    )
    complexity: Complexity = Field(
        default="simple",
        description="simple, multi_facet (several independent parts) or ambiguous"
    )
    preserved_terms: list[str] = Field(
        default_factory=list,
        description="Identifiers, error messages, versions and negations copied verbatim"
    )
    filters: list[MetadataFilter] = Field(
        default_factory=list,
        description="Metadata filters, only when the user explicitly restricts the scope"
    )
    resolved_context: list[str] = Field(
        default_factory=list,
        description="Relevant facts explicitly established by conversation history"
    )
    needs_clarification: bool = Field(
        default=False,
        description="True only if an essential ambiguity prevents meaningful retrieval"
    )
    clarification_question: str | None = Field(
        default=None,
        description="One concise question if clarification is essential"
    )

    def all_queries(self) -> list[str]:
        queries = [self.canonical_query, *self.query_variants, *(s.query for s in self.sub_questions)]
        return list(dict.fromkeys(q.strip() for q in queries if q and q.strip()))


class GapAssessment(BaseModel):
    covered_facets: list[str] = Field(
        default_factory=list,
        description="Parts of the question the evidence answers"
    )
    missing_facets: list[str] = Field(
        default_factory=list,
        description="Parts of the question, or relationships between them, the evidence does not cover"
    )
    follow_up_queries: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="Targeted searches for the missing facets only"
    )
    sufficient: bool = Field(
        description="True if the evidence is enough to answer the question"
    )
