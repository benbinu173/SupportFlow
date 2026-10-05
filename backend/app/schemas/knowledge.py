"""Knowledge-base request and response schemas — §22 to §24.

`app/schemas/ai.py` holds the shapes the *generation* provider is allowed to return. This
module holds the knowledge layer's own: what a client sends, how a document reads back, and
both halves of an answer.

Three of these are the same kind of thing as the four in `app/schemas/ai.py` — provider
output, validated by `app/ai/provider.py`'s `validate_output`, which is the only path from a
model's answer to application code:

* `Embedding` — the vectors, from the embedding provider.
* `KnowledgeAnswer` — §23's answer and the model's own claim about which passages it used.

**`Embedding` lives here rather than beside the other four, and the reason is its validator.**
The one thing that can be wrong with a vector list is its width, and the width is a fact about
this package's storage: `app/models/knowledge_chunk.py` declares `EMBEDDING_DIMENSIONS = 1536`
at the column. A 3072-dimension vector is an `AIOutputError` here, rather than a driver error
at insert. Importing that constant makes the two declarations one.

**The dimension is on the failure's `__cause__`, not in its reason, and that is
`_failure_summary`'s rule rather than an oversight.** `app/ai/provider.py` reports a rejected
payload as `loc: type` and never `msg`, because a Pydantic message can quote the input — and
for this schema the input is a vector, but the rule is the schema-independent one. So the
reason says `did not match Embedding: <root>: value_error` and the chained `ValidationError`
says *"a vector was 3072 wide; the column is 1536"*, which is where a developer reads it. The
claim worth keeping is the one this paragraph can substantiate: the width is refused here, with
the number in hand, rather than deferred to the insert.

**`KnowledgeAnswer.read` does not exist as a field and `KnowledgeAnswerRead` does.** The split
is `app/schemas/ai.py`'s: the schema says what the model wrote, and the service says what the
answer is grounded in. `used_sources` are indices the model chose; the citations a caller
receives are resolved from the passages that were actually supplied, so no field here can
carry a citation that names something the retrieval never returned.

**No `source_reference` on the read model.** For an `upload` it is an object-storage key, which
is an internal name for a file in a private bucket — the argument `app/schemas/attachment.py`
makes about `storage_key` applies here unchanged. A client knows a document by its `title`, and
for a `url` source the reference is the client's own input, so nothing is lost by withholding
it from one case to protect the other. `source_type` is exposed, which is what a reader needs
to know where a document came from.

**No `content` on the read model either.** The column is retained so a document can be
re-chunked when the strategy changes; it is not a document's readable surface, and a page of
twenty documents should not carry twenty documents' worth of text. What a caller can see about
ingestion is that it happened: `status`, `chunk_count`, and `error_message` when it did not.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from app.models.enums import DocumentSourceType, ProcessingStatus
from app.models.knowledge_chunk import EMBEDDING_DIMENSIONS

#: The ceiling on a hand-authored document, in characters. An upload is bounded in bytes by
#: `MAX_KNOWLEDGE_DOCUMENT_BYTES`, and this is the same guard for the path that has no file:
#: 200 000 characters is roughly a hundred pages, several times the longest policy a tenant
#: would paste into a form, and it means the JSON route cannot be used to buy an unbounded
#: embedding bill. The worker's chunk cap catches what a long document still implies.
_MAX_CONTENT_CHARS = 200_000

#: The ceiling on a question, in characters. §23's example is fourteen. A "question" of
#: several thousand characters is a document, and there is an endpoint for documents.
_MAX_QUESTION_CHARS = 2_000

#: The ceiling on an answer, in characters. §23 asks for an answer and its sources, not an
#: essay; a model that ignores the instruction should fail validation rather than write a page
#: into a response a UI panel has to render.
_MAX_ANSWER_CHARS = 4_000

#: Provider output is strict, for the reasons `app/schemas/ai.py` gives: a field a model
#: invented is a failure rather than a value that quietly disappears, and the constraint is
#: stated to the model in the tool schema it is handed.
_STRICT = ConfigDict(extra="forbid")


class KnowledgeDocumentCreate(BaseModel):
    """Registering a document from text the client has, or from a URL.

    **`source_type` is not a field.** The kind follows from which of `content` and `url` was
    sent, enforced by the validator below, so the body cannot say two things and a client
    cannot send `source_type=manual` with a URL. Uploads are a third kind and a different
    route, because a multipart body is a different transport rather than a different field
    (`POST /knowledge/upload`).

    `content` and `url` are both optional in the type system and exactly one is required in
    fact. The alternative — two endpoints, one per kind — would be two routes whose
    capabilities and bodies are otherwise identical, and the act they perform is the same act.
    """

    title: str = Field(min_length=1, max_length=500)
    content: str | None = Field(default=None, max_length=_MAX_CONTENT_CHARS)
    url: HttpUrl | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "KnowledgeDocumentCreate":
        """Refuse both, and refuse neither.

        `HttpUrl` rather than a plain string so a scheme is settled before the fetch guard
        sees it: `file:///etc/passwd` and `javascript:` are not URLs this type accepts, and a
        validator that ran afterwards would be re-checking what the type has already refused.
        """
        if (self.content is None) == (self.url is None):
            raise ValueError("provide exactly one of 'content' (a manual document) or 'url'")
        return self

    @property
    def source_type(self) -> DocumentSourceType:
        """Which kind this body describes. Derived, never declared — see the docstring."""
        return DocumentSourceType.URL if self.url is not None else DocumentSourceType.MANUAL


class KnowledgeDocumentRead(BaseModel):
    """A document as the API presents one, minus the two fields that are the server's own.

    `is_published` and `status` are both present because they answer different questions and a
    caller can see both: whether an admin has it in the knowledge base (`status`, which the
    ingestion worker owns) and whether retrieval may return it (`is_published`). They are set
    together on success in this phase, and the column's independence is what a later phase's
    editorial switch would use — see `app/models/knowledge_document.py`.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    source_type: DocumentSourceType
    status: ProcessingStatus
    error_message: str | None
    chunk_count: int
    is_published: bool
    created_by_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    processed_at: datetime | None


class KnowledgeQuestion(BaseModel):
    """A question to answer from the organization's knowledge base.

    **No `top_k`.** How many passages are worth retrieving is a property of the deployment's
    retrieval policy — `RETRIEVAL_TOP_K` and `RETRIEVAL_MIN_SIMILARITY` — and not of a request:
    a client that could raise it would be spending the organization's budget on a bigger
    context, and the number that decides when this product is allowed to answer at all is not
    one a caller should be able to move.
    """

    question: str = Field(min_length=1, max_length=_MAX_QUESTION_CHARS)


class KnowledgeSource(BaseModel):
    """One retrieved passage, as a citation.

    `excerpt` is the chunk's own text — the passage the answer was grounded in, not a summary
    of it, because a citation a reader cannot check is decoration. It is bounded by the
    chunker rather than by a field here.

    `similarity` is cosine similarity in `[0, 1]`, exposed because it is the honest strength of
    the match and a UI that shows "87% match" is showing a real number rather than a
    decoration. It is also what a reader needs to understand why a passage was retrieved at all.
    """

    document_id: uuid.UUID
    document_title: str
    chunk_index: int
    excerpt: str
    similarity: float


class KnowledgeAnswerRead(BaseModel):
    """§23's *"answer + source references"*.

    **`sources` is empty when the knowledge base could not answer**, and that is the only
    signal of it — there is no `grounded` boolean, because a second field saying what an empty
    list already says is a second field that can come to disagree with it. §24's sentence is in
    the `answer` the caller reads, which is where a person looks for it.

    The order is by descending similarity, so the first source is the passage retrieval
    considered the best match rather than the first passage the model happened to name.
    """

    answer: str
    sources: list[KnowledgeSource]


class Embedding(BaseModel):
    """Vectors, one per text the provider was given.

    The width is checked here and the *count* is not: a validator sees this model's own fields
    and cannot know how many texts were sent, so the provider checks that it was handed back
    one vector per input — the one shape check that needs the request in hand.

    A `list[float]` rather than `numpy` or a pgvector type: this is a provider response, it
    arrives as JSON, and `pgvector`'s column takes it as a Python list at insert. Nothing in
    this module imports the vector extension.
    """

    model_config = _STRICT

    vectors: list[list[float]]

    @model_validator(mode="after")
    def _vectors_are_the_column_width(self) -> "Embedding":
        """Refuse a vector that is not `EMBEDDING_DIMENSIONS` wide.

        Checked against the model's constant rather than a number repeated here, so a change of
        embedding model — which is a migration and a re-embed, per that module's comment —
        cannot leave this validating the old width. An empty list is legal at this level and is
        a provider bug caught by the count check beside it.
        """
        for vector in self.vectors:
            if len(vector) != EMBEDDING_DIMENSIONS:
                raise ValueError(
                    f"a vector was {len(vector)} wide; the column is {EMBEDDING_DIMENSIONS}"
                )
        return self


class KnowledgeAnswer(BaseModel):
    """What the model wrote: §23's answer, and which passages it says it used.

    **`answer` is prose, and `used_sources` is the model's claim about it** — 1-based numbers
    referring to the passages as they were numbered in the prompt. The two are not equally
    trusted: `app/services/knowledge_service.py` resolves every index against the passages it
    actually supplied and drops the ones that name nothing, so a citation a caller sees always
    points at a stored chunk. §24's *"do not fabricate citations"* is enforced there, by the
    mapping, rather than by hoping the model obeys.

    Empty `used_sources` is a legal answer — a model that answered from a passage without
    naming it — and the honest response is then an answer with no sources rather than a
    citation invented to fill the field. **Absent `used_sources` is not the same claim and is
    refused.** An empty list is the model saying it used nothing; a missing field is a model
    that did not follow the shape, and defaulting it would make a truncated response
    indistinguishable from §24's deliberate refusal — the same argument
    `Classification.priority` makes for having no default.

    §24's other three rules are instructions in `app/ai/prompts.py`'s `KNOWLEDGE_INSTRUCTION`,
    because they are about what the model should write and this module is about what it is
    allowed to have written.
    """

    model_config = _STRICT

    answer: str = Field(min_length=1, max_length=_MAX_ANSWER_CHARS)
    used_sources: list[int]
