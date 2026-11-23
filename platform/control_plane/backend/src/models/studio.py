"""Wire types of the Studio routes: the draft's response, the proposal's request union and its
response.

``DraftResponse`` names the shape ``services.studio_draft`` builds and validates, one block model per
``kind``. ``ProposalRequest`` is the union the frontend sends, told apart by ``kind``; its two
variants declare the fields ``services.studio_proposal`` accepts and refuse any other key, so a
misspelled field is FastAPI's 422 before the service.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, RootModel

from models.base import WireModel
from models.eval import DatasetItem

StudioAuth = Literal["none", "api_key"]


class McpBlockDraft(WireModel):
    kind: Literal["mcp_server"]
    name: str
    description: str
    openapi_file: str | None
    exposed_operations: list[str]
    auth: StudioAuth


class AgentBlockDraft(WireModel):
    kind: Literal["agent"]
    name: str
    system_prompt: str
    prompt_file: str | None
    skill_files: list[str]
    mcp_block_names: list[str]
    eval_items: list[DatasetItem]


StudioBlockDraft = Annotated[McpBlockDraft | AgentBlockDraft, Field(discriminator="kind")]


class DraftResponse(WireModel):
    blocks: list[StudioBlockDraft]
    unused_files: list[str]


class SkillFile(WireModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    content: str


class McpProposalRequest(WireModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["mcp_server"]
    openapi_text: str
    exposed_operations: list[str]
    auth: StudioAuth
    api_key: str | None = None
    existing_secret_arn: str | None = None


class AgentProposalRequest(WireModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["agent"]
    system_prompt: str | None = None
    skills: list[SkillFile] = Field(default_factory=list)
    declared_mcp_server_ids: list[str] = Field(default_factory=list)
    eval_items: list[DatasetItem] | None = None


class ProposalRequest(
    RootModel[Annotated[McpProposalRequest | AgentProposalRequest, Field(discriminator="kind")]]
):
    """The proposal body: one of the two variants, selected by ``kind``."""


class ProposalResponse(WireModel):
    branch: str
    pr_number: int
    pr_url: str
    files: list[str]
