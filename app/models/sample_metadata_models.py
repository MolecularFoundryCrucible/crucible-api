from typing import Optional, List
from sqlalchemy import JSON, Column, BigInteger
from sqlmodel import Field, SQLModel, Relationship


class SpinbotBatchMetadata(SQLModel, table = True):
    id: Optional[int]= Field(default=None, primary_key=True)
    sample_id: str = Field(default=None, primary_key=True)
    sample_type: str = Field(default = 'spinbot_batch')
    spin_duration_s: Optional[int]
    spin_velocity_rpm: Optional[int]
    dispense_delay_s: Optional[int]
    pipette_height_mm: Optional[float]
    dispense_speed_ul_s: Optional[int]
    precursor_b_volume_ul: Optional[float]
    annealing_duration_s: Optional[int]
    molar_ratio_fai_macl:  Optional[str]

    