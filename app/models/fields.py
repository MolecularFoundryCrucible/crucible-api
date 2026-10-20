from typing import Annotated

from pydantic import StringConstraints

NonBlankStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
