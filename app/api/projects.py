
@app.post("/projects", response_model = Project)
def create_project(auth_res: Annotated[tuple, Depends(authorize_user)], P: Project):
    """
    Add or update a project record in the database.
    
    Args:
        auth_res: ACL info for user
        P: Project object to be added or updated
        
    Returns:
        Added or updated project object
    """
    with Session(engine) as session:
        prop = add_or_update(session, Project,  Project.id == P.id, **dict(P))
        ag = add_or_update(session, 
                      AccessGroup, 
                      AccessGroup.group_name==P.project_id, 
                      group_name=P.project_id)
        
        r1 = session.exec(select(User).where(User.lbl_email.ilike(f"%{P.project_lead_email}%"))).all()
        r2 = session.exec(select(User).where(User.email.ilike(f"%{P.project_lead_email}%"))).all()
        all_found_accounts = [x for x in r1 + r2 if x.orcid is not None]
        
        print(f"found accounts for user: {all_found_accounts}")

        if len(all_found_accounts) > 0:
            project_lead = all_found_accounts[-1]
            if not (project_lead in ag.users):
                ag.users.append(project_lead)

            if not (prop in project_lead.projects):
                project_lead.projects.append(prop)

        session.commit()
    return(P)



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
    
