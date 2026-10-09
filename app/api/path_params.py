from typing import Annotated

from fastapi import Path

ProjectRef = Annotated[str, Path(
    description="Project MFID or project_id slug (case-insensitive).",
)]
InstrumentRef = Annotated[str, Path(
    description="Instrument MFID or instrument_id slug (case-insensitive).",
)]
UserRef = Annotated[str, Path(
    description="User ORCID, service-account MFID, or username. Email still resolves but is deprecated in paths.",
)]
ServiceAccountRef = Annotated[str, Path(
    description="Service-account MFID or username.",
)]

ResourceMfid = Annotated[str, Path(alias="mfid", description="Resource MFID.")]
EntityMfid = Annotated[str, Path(alias="entity_mfid", description="Sample or dataset MFID.")]
DatasetMfid = Annotated[str, Path(alias="dataset_mfid", description="Dataset MFID.")]
ParentDatasetMfid = Annotated[str, Path(alias="parent_dataset_mfid", description="Parent dataset MFID.")]
ChildDatasetMfid = Annotated[str, Path(alias="child_dataset_mfid", description="Child dataset MFID.")]
SampleMfid = Annotated[str, Path(alias="sample_mfid", description="Sample MFID.")]
ParentSampleMfid = Annotated[str, Path(alias="parent_sample_mfid", description="Parent sample MFID.")]
ChildSampleMfid = Annotated[str, Path(alias="child_sample_mfid", description="Child sample MFID.")]
