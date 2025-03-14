from typing import Annotated, List, Tuple, Dict, Any, Optional
from pydantic import BaseModel
from app.models.dataset import Dataset, AssociatedFile
from mfid import mfid

# ======================= more classes : )
class NewDataset(Dataset):
    '''
    This is the pydantic model for adding a Dataset Record to the SQL database. 
    It inherits from the Dataset SQLmodel class that defines the table, but
    allows the user to create a dataset without filling in any fields. 

    If no information is provided, the required fields (unique_id and public) will 
    be assigned default values. 

    The default value for unique_id is a live generated mfid() and the default value for public is False. 
    '''
    unique_id: Optional[str] = mfid()
    public: Optional[bool] = False

class AssociatedFile(AssociatedFile):
    '''
    Default calculates needed values
    '''
af_obj = add_or_update(session, AssociatedFile, where=(AssociatedFile.filename == k),
                                       filename = k,
                                       size = int(v['size']), 
                                       sha256_hash = v['sha256_hash'])
