import os
import logging
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker
from langchain.docstore.document import Document
from langchain.text_splitter import RecursiveCharacterTextSplitter
from PyPDF2 import PdfReader
from django.conf import settings

logger = logging.getLogger(__name__)


# Cache the connection string so it's not recomputed for every request.
def get_connection_string():
    return (
        f"postgresql+psycopg://{settings.DATABASES['default']['USER']}:"
        f"{settings.DATABASES['default']['PASSWORD']}@"
        f"{settings.DATABASES['default']['HOST']}:{settings.DATABASES['default']['PORT']}/"
        f"{settings.DATABASES['default']['NAME']}"
    )


def process_pdf(file):
    """Extracts text from a PDF file and splits it into document chunks."""
    try:
        reader = PdfReader(file)
    except Exception as e:
        logger.exception("Error reading PDF file: %s", file.name)
        raise

    text = ""
    page_count = 0
    for page in reader.pages:
        page_count += 1
        try:
            extracted = page.extract_text()
        except Exception as e:
            logger.exception("Error extracting text from page in file: %s", file.name)
            continue
        if extracted:
            text += extracted
    logger.info("File %s: extracted text length: %d, pages processed: %d", file.name, len(text), page_count)

    if not text.strip():
        logger.warning("No text extracted from file %s", file.name)
        return []

    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)
    chunks = splitter.split_text(text)
    logger.info("File %s: split into %d chunks", file.name, len(chunks))
    # Create Document objects for each chunk.
    return [Document(page_content=chunk, metadata={"source": file.name}) for chunk in chunks]


def initialize_db_schema(connection_string):
    """Initialize the pgvector-related schema if not already created.
       This is better run as a one-time operation via a management command."""
    from sqlalchemy import create_engine, text, inspect
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(connection_string)
    inspector = inspect(engine)
    Session = sessionmaker(bind=engine)
    tables = inspector.get_table_names()
    if 'langchain_pg_collection' in tables and 'langchain_pg_embedding' in tables:
        logger.info("Required tables already exist")
        return

    with Session() as session:
        try:
            session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            session.commit()
            logger.info("pgvector extension enabled")
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to create pgvector extension: {e}")
            raise

        # Create tables – ideally this should be handled by migrations.
        try:
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS langchain_pg_collection (
                    uuid UUID PRIMARY KEY,
                    name VARCHAR(50) UNIQUE NOT NULL,
                    cmetadata JSONB
                )
            """))
            session.commit()
            logger.info("Created langchain_pg_collection table")
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to create collection table: {e}")
            raise

        try:
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS langchain_pg_embedding (
                    id UUID PRIMARY KEY,
                    collection_id UUID REFERENCES langchain_pg_collection(uuid) ON DELETE CASCADE,
                    embedding VECTOR,
                    document TEXT,
                    cmetadata JSONB,
                    custom_id VARCHAR(100)
                )
            """))
            session.commit()
            logger.info("Created langchain_pg_embedding table")
        except Exception as e:
            session.rollback()
            logger.error(f"Failed to create embedding table: {e}")
            raise

        try:
            session.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_langchain_pg_collection_name 
                ON langchain_pg_collection (name)
            """))
            session.commit()
            logger.info("Created index on collection names")
        except Exception as e:
            session.rollback()
            logger.warning(f"Failed to create collection name index: {e}")

        try:
            session.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_langchain_pg_embedding_collection 
                ON langchain_pg_embedding (collection_id)
            """))
            session.commit()
            logger.info("Created index on embedding collection IDs")
        except Exception as e:
            session.rollback()
            logger.warning(f"Failed to create embedding collection index: {e}")
