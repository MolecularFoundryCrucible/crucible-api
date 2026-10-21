from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict


class NoParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class Pipeline:
    name: str
    description: str
    routing_key: str
    parameters: type[BaseModel] = NoParameters


PIPELINES = {
    pipeline.name: pipeline
    for pipeline in (
        Pipeline("carrier_segmentation", "Postprocessing: segment a sample-carrier photo into one image per sample position.", "carrier-segmentation"),
        Pipeline("insitu_aggregation", "Postprocessing: aggregate in-situ spectra into a new linked dataset with a summary plot.", "insitu-aggregation"),
        Pipeline("rga_analysis", "Postprocessing: analyze residual gas analyzer (RGA) data.", "rga-analysis"),
        Pipeline("mosaic_stitch", "Postprocessing: stitch image tiles into a mosaic.", "mosaic-stitch"),
    )
}
