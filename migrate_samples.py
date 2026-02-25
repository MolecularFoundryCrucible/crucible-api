from sqlmodel import Session, select, create_engine
from app.models.core_models import ScientificMetadata, NewScientificMetadata, Dataset

DATABASE_URL = "postgresql://crucible-api-user:cdsetrpl@127.0.0.1:5432/crucible-staging"
engine = create_engine(DATABASE_URL)

def migrate_data():
    with Session(engine) as session:
        print("Starting migration: Fetching old metadata and joining with Dataset...")
        
        # join on unique_id to get the Integer ID and the MFID string
        stmt = (
            select(ScientificMetadata, Dataset.id, Dataset.unique_id)
            .join(Dataset, ScientificMetadata.dataset_unique_id == Dataset.unique_id)
            .limit(50)
        )
        
        results = session.exec(stmt).all()

        if not results:
            print("No matching records found. Double-check join condition.")
            return

        print(f"Found {len(results)} records. Migrating...")

        for old_meta, dataset_int_id, mfid_string in results:
            new_entry = NewScientificMetadata(
                dataset_id=dataset_int_id,      
                dataset_mfid=mfid_string,        
                scientific_metadata=old_meta.scientific_metadata
            )
            session.add(new_entry)
        
        try:
            session.commit()
            print("--- Migration Successful! ---")
            print(f"Successfully moved {len(results)} samples to the FTS table.")
        except Exception as e:
            session.rollback()
            print(f"Commit failed with error: {e}")

if __name__ == "__main__":
    migrate_data()