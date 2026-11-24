from typing import Annotated, List

from fastapi import APIRouter, Depends

from app.config import get_settings
from app.models.storage import StoreRead
from app.utils.auth_utils import authorize_user
from app.utils.permissions import Principal

router = APIRouter(tags=["storage"])


@router.get("/storage/stores", response_model=List[StoreRead])
def list_stores(auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''The stores this server can keep files in, with the one uploads use unless another is chosen.'''
    storage = get_settings().storage
    return [StoreRead(id=store.id, display_name=store.display_name, description=store.description, default=store.id == storage.default_store_id) for store in storage.stores]
