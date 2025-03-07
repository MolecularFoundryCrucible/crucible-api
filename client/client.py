'''
Maybe this is the wrong naming / location but mostly for now want to take notes on things this should do that are not covered by the API

'''



def build_dataset_from_json(D, dsid = None):
    '''
    Should call the post /dataset API endpoint
    Optional arg should be an Owner Object or ID --- then this function should go get the owner to add to the dataset before upload
    Same for proposal and instrument
    '''

    from app.utils.sql_utils import get_any
    from app.models.dataset import AssociatedFile
    from pycrucible.general_functions import get_secret_2
    import requests

    # this logic from the previous endpoint should be part of the client not the API ( I think ??)
    if D is None and dsid is not None:
        # get the data
        # read into D
        pass
    elif D is None:
        return("Please provide a dataset object or identifier")
    
    else:
        D = D

    
    dsid = get_any(D, ['unique_id', 'dataset_unique_id' ])
    smd = get_any(D, ['scientific_metadata', 'metadata_dictionary'])
    owner_orcid= get_any(D, ['orcid'])
    measurement = get_any(D, ['measurement'])
    raw_or_derived = get_any(D, ['raw_or_derived'])
    
    # raw or derived
    if raw_or_derived is None:
        raw_or_derived = "raw"
    
    if raw_or_derived == "derived":
        file_to_upload = ""
        data_format = ""
    else:
        file_to_upload = D['file_to_upload']
        data_format = D['data_format']

    # public yes/no
    if "public" in D.keys():
        public_status = D['public']
    else:
        public_status = False


    baseurl = "https://crucible.lbl.gov/api"
    apikey = get_secret_2("ADMIN_APIKEY", "projects/776258882599/secrets/crucible_admin_apikey/versions/1")
    
    user = requests.request(method="get", url=f"{baseurl}/users/{owner_orcid}", headers = {"Authorization":f"Bearer {apikey}"}).json()
    print(f"{user=}")
    if user is not None:
        user_id = user['id']
        print(user, user_id)
    else:
        user_id = None

    # this needs to be fixed but it will break things right now ---- but the proposal endpoint expects proposal/MFP{XXXX} so "unknown" errors
    try:
        prop = requests.request(method="get", url=f"{baseurl}/proposal/{D['proposal']}", headers = {"Authorization":f"Bearer {apikey}"}).json()
        print(f"{prop=}")
        prop_id = prop['id']
        print(prop, prop_id)
    except:
        prop = None
        prop_id = None

    
    inst = requests.request(method="get", url=f"{baseurl}/instrument_by_name/{D['instrument_name']}", headers = {"Authorization":f"Bearer {apikey}"}).json()
    if inst is not None:
        inst_id = inst['identifier']
    else:
        inst_id = None

    #  this is all just pseudo code to remember to do something like this...
    for kw in D['keywords']:
        requests.post("/keyword/{dsid}")

    for tn in D['thumbnails']:
        requests.post("/thumbnail/{dsid}")

    for k,v in D['associated_files'].items():
        AF = AssociatedFile(filename = k, size = int(v['size'], sha256_hash = v['sha256_hash'], dsid = dsid))
        requests.post("/associated_file/{dsid}", params = {"associated_file": AF})

    # ============================= ACCESS CONTROL
    if user:
        print(f"has user {ds.dataset_name=} {user['lbl_email']}")
        S = select(AccessGroup).where(AccessGroup.group_name == user['orcid'])
        ag = session.exec(S).one_or_none()
        S = select(DataSetACL).where(DataSetACL.accessgroup == ag)\
                                .where(DataSetACL.dataset == ds)
        acl = session.exec(S).one_or_none()
        if not acl:
            acl = DataSetACL(accessgroup=ag, dataset=ds)
        acl.read = True
        acl.write= True
        session.add(acl)
    if prop:
        S = select(AccessGroup).where(AccessGroup.group_name == f"MFP{prop['id']:05d}")
        ag = session.exec(S).one_or_none()
        S = select(DataSetACL).where(DataSetACL.accessgroup == ag)\
                                .where(DataSetACL.dataset == ds)
        acl = session.exec(S).one_or_none()
        if not acl:
            acl = DataSetACL(accessgroup=ag, dataset=ds)
        acl.read = True
        acl.write= False
        session.add(acl)

    # add admin and readonly admin groups
    S = select(AccessGroup).where(AccessGroup.id == 7328)
    ag = session.exec(S).one_or_none()
    S = select(DataSetACL).where(DataSetACL.accessgroup == ag)\
                                .where(DataSetACL.dataset == ds)
    acl = session.exec(S).one_or_none()
    if not acl:
        acl = DataSetACL(accessgroup=ag, dataset=ds)
    acl.read = True
    acl.write= False
    session.add(acl)
    
    # add admin and readonly admin groups
    S = select(AccessGroup).where(AccessGroup.id == 7329)
    ag = session.exec(S).one_or_none()
    S = select(DataSetACL).where(DataSetACL.accessgroup == ag)\
                                .where(DataSetACL.dataset == ds)
    acl = session.exec(S).one_or_none()
    if not acl:
        acl = DataSetACL(accessgroup=ag, dataset=ds)
    acl.read = True
    acl.write= True
    session.add(acl)


    
    # ============================= ID TYPE
    add_or_update(session, IdType, where=(IdType.unique_id == uid),
                                    unique_id = uid,
                                    type = "dataset")
    session.commit()