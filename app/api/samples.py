
@app.post("/samples", response_model = Sample)
def create_sample(auth_res: Annotated[tuple, Depends(authorize_user)], S: Sample):
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        sample = add_or_update(session,
                           Sample,
                           Sample.unique_id == S.unique_id,
                           **S.model_dump(exclude_unset = True))
        session.commit()
        session.refresh(sample)
    
    if sample.owner_user_id is None and sample.owner_orcid is None:
        creator = session.exec(select(AccessGroup).where(AccessGroup.group_name == access_group_name)).one_or_none()
        if is_orcid(access_group_name):
            sample.owner_orcid = access_group_name
            creator_user_id = session.exec(select(User.id).where(User.orcid == sample.owner_orcid)).one_or_none()
            sample.owner_user_id = creator_user_id
            
    elif sample.owner_user_id is None:
        creator_user_id = session.exec(select(User.id).where(User.orcid == sample.owner_orcid)).one_or_none()
        sample.owner_user_id = creator_user_id
        
    else:
        pass

    add_or_update(session,
                  IdType,
                  where=(IdType.unique_id == sample.unique_id),
                  unique_id = sample.unique_id,
                  object_type = "sample")
    
    session.commit()
    session.refresh(sample)


@app.post("/datasets/{dsid}/samples/{sample_id}")
def add_sample_to_dataset(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str, sample_id:str):
    access_group_name, ag_ids = auth_res
    ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
    samp_cmd = select(Sample).where(Sample.unique_id == sample_id)

    with Session(engine) as session:
        ds = session.exec(ds_cmd).one_or_none()
        samp = session.exec(samp_cmd).one_or_none()

        if samp is None:
            raise HTTPException(status_code=404, detail="Sample not found")
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found or access denied")

        newlink = add_or_update(session,
                      DatasetSampleLink,
                      DatasetSampleLink.sample_id = samp.id,
                      DatasetSampleLink.dataset_id = ds.id)

        session.commit()

    return newlink


@app.get("/samples", response_model = List[Sample])
def list_samples(admin_acct:  Annotated[str, Depends(is_admin)], request: Request):
    '''
    Returns the project object for the provided ID
    '''
    query_params = request.query_params
    print(query_params)
    
    params_dict = dict(query_params)
    print(params_dict)
    
    
    samp_cmd = select(Sample)

    for k, v in params_dict.items():
        if hasattr(Sample, k) and v is not None:
            samp_cmd = samp_cmd.where(getattr(Sample, k) == v)
        else:
           pass
            
    with Session(engine) as session:
        samp_list = session.exec(samp_cmd).all()
        return samp_list


@app.get("/samples/{sample_id}", response_model = Union[None, Sample])
def read_sample(sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns the sample for the provided ID
    '''
    with Session(engine) as session:
        samp = session.exec(select(Sample).where(Sample.unique_id == sample_id)).one_or_none()
        return(samp)


@app.get("/datasets/{dsid}/samples", response_model = List[Sample])
def read_samples_in_dataset(dsid: str, auth_res:  Annotated[str, Depends(authorize_user)]):
    '''
    Returns a list of users associated with the project ID provided
    '''
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:

        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
        ds = session.exec(ds_cmd).one_or_none()
        samples = session.exec(select(Sample).select_from(join(Sample, DatasetSampleLink, DatasetSampleLink.sample_id == Sample.id)).where(DatasetSampleLink.dataset_id == ds.id)).all()
        return samples
    
