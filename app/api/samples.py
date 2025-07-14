
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




@app.get("/projects", response_model = List[Project])
def list_projects(admin_acct:  Annotated[str, Depends(is_admin)]):
    '''
    Returns the project object for the provided ID
    '''
    with Session(engine) as session:
        props = session.exec(select(Project)).all()
        return(props)


@app.get("/projects/{proj_id}", response_model = Union[None, Project])
def read_project(proj_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns the project object for the provided ID
    '''
    with Session(engine) as session:
        prop = session.exec(select(Project).where(Project.project_id == proj_id)).one_or_none()
        # should check user is in prop group
        return(prop)


@app.get("/projects/{proj_id}/users", response_model = List[User])
def read_users_on_project(proj_id: str, admin_acct:  Annotated[str, Depends(is_admin)]):
    '''
    Returns a list of users associated with the project ID provided
    '''
    with Session(engine) as session:
        users = session.exec(select(User).select_from(join(User, UserProjectLink, UserProjectLink.user_id == User.id)).where(UserProjectLink.project_id == proj_id)).all()
        return users
    
