from typing import Literal, Optional
from sqlmodel import SQLModel


class LinkedResource(SQLModel):
    unique_id: str
    resource_type: str
    name: Optional[str] = None
    # Which endpoint of the stored edge this resource is, relative to the
    # resource being read. Edges are stored parent -> child, so "source" means
    # this resource is the parent and "target" means it is the child. Computed
    # per query, not stored: the same link row reads "source" from one end and
    # "target" from the other. Dataset<->sample links have no hierarchy and are
    # always "undirected".
    direction: Literal["source", "target", "undirected"]
    # Kind of link, for dataset->dataset and sample->sample parent/child links.
    # Unlike direction this is stored on the link row, and is oriented
    # child-relative-to-parent: "is_part_of" reads "child is_part_of parent".
    # None on dataset<->sample links, and on links created before typing existed.
    relationship_type: Optional[str] = None
