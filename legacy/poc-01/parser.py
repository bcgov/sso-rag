import os
from pathlib import Path
from typing import List, Optional
from langchain_text_splitters import MarkdownHeaderTextSplitter
from langchain_ollama import OllamaEmbeddings, ChatOllama
from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, Integer, String, create_engine, Float, select, func, text, cast, literal_column
from sqlalchemy.orm import Session, declarative_base
from sqlalchemy.pool import NullPool
import numpy as np

# Initialize SQLAlchemy base for ORM models
Base = declarative_base()

# Configuration
OLLAMA_EMBEDDING_MODEL = "nomic-embed-text"  # Default embedding model
OLLAMA_LLM_MODEL = "gemma3:1b"  # Default LLM model
OLLAMA_BASE_URL = os.environ.get(
    "OLLAMA_BASE_URL",
    "http://localhost:11434"  # local dev default
)
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres"  # local dev default
)
DOCS_DIR = Path("./docs")
EMBEDDING_DIMENSION = 768

SYSTEM_PROMPT = """You are a helpful assistant that answers questions using only the provided context.

Rules:
- Answer based ONLY on the context provided below
- If the context does not contain enough information, say "I don't have information about that in my knowledge base"
- If you are partially confident, say what you found and clearly state what is missing
- Cite which document/section your answer came from
- Do not guess or use outside knowledge

Context:
{context}

Question: {question}

Answer:"""

# Initialize embeddings and LLM clients
embeddings_client = OllamaEmbeddings(model=OLLAMA_EMBEDDING_MODEL, base_url=OLLAMA_BASE_URL)
llm_client = ChatOllama(model=OLLAMA_LLM_MODEL, base_url=OLLAMA_BASE_URL)


class DocumentEmbedding(Base):
    """SQLAlchemy model for storing document chunks with embeddings."""
    __tablename__ = "document_embeddings"
    
    id = Column(Integer, primary_key=True)
    chunk_text = Column(String, nullable=False)
    embedding = Column(Vector(EMBEDDING_DIMENSION), nullable=False)
    source_file = Column(String, nullable=False)
    header_1 = Column(String, nullable=True)
    header_2 = Column(String, nullable=True)
    header_3 = Column(String, nullable=True)


def initialize_database(db_url: str = DATABASE_URL) -> str:
    """
    Initialize database connection and create tables if they don't exist.
    
    Args:
        db_url: PostgreSQL connection string
        
    Returns:
        Connection string for creating engine
    """
    # Create tables
    engine = create_engine(db_url, poolclass=NullPool)
    Base.metadata.create_all(engine)
    return db_url


def parse_documents(docs_dir: Path = DOCS_DIR) -> List:
    """
    Parse all markdown files in the documents directory and split by headers.
    
    Args:
        docs_dir: Path to directory containing markdown files
        
    Returns:
        List of LangChain Document objects with metadata
    """
    headers_to_split_on = [
        ("#", "Header 1"),
        ("##", "Header 2"),
        ("###", "Header 3"),
    ]
    
    splitter = MarkdownHeaderTextSplitter(headers_to_split_on=headers_to_split_on)
    all_splits = []
    
    # Process each markdown file
    for md_file in sorted(docs_dir.glob("**/*.md")):
        with open(md_file, encoding="utf-8") as f:
            content = f.read()
        
        # Split into chunks by headers
        splits = splitter.split_text(content)
        
        # Add metadata about source file and headers
        for split in splits:
            split.metadata["source_file"] = str(md_file)
        
        all_splits.extend(splits)
    
    print(f"Parsed {len(all_splits)} document chunks")
    return all_splits


def generate_embeddings(text: str) -> List[float]:
    """
    Generate embedding vector for text using Ollama.
    
    Args:
        text: Text to embed
        
    Returns:
        Embedding vector as list of floats
    """
    embedding = embeddings_client.embed_query(text)
    if hasattr(embedding, 'tolist'):
        embedding = embedding.tolist()
    return embedding


def store_documents(documents: List, db_url: str = DATABASE_URL) -> int:
    """
    Generate embeddings for documents and store in PostgreSQL.
    Skips files that have already been ingested (checked by source_file).
    
    Args:
        documents: List of LangChain Document objects
        db_url: PostgreSQL connection string
        
    Returns:
        Number of documents stored
    """
    engine = create_engine(db_url, poolclass=NullPool)

    # Group chunks by source file to allow per-file skip checks
    from collections import defaultdict
    chunks_by_file = defaultdict(list)
    for doc in documents:
        chunks_by_file[doc.metadata.get("source_file", "unknown")].append(doc)

    stored_count = 0
    with Session(engine) as session:
        for source_file, chunks in chunks_by_file.items():
            # Skip this file if any chunk from it already exists in the DB
            already_ingested = session.execute(
                select(DocumentEmbedding.id).where(
                    DocumentEmbedding.source_file == source_file
                ).limit(1)
            ).first()

            if already_ingested:
                print(f"Skipping {source_file} - already ingested")
                continue

            print(f"Ingesting {source_file}...")
            for doc in chunks:
                # Build enriched text for embedding (header context + body)
                headers = [
                    doc.metadata.get('Header 1', ''),
                    doc.metadata.get('Header 2', ''),
                    doc.metadata.get('Header 3', ''),
                ]
                header_context = ' > '.join(h for h in headers if h)
                embed_text = f"{header_context} {doc.page_content}".strip() if header_context else doc.page_content

                embedding = generate_embeddings(embed_text)

                # Create and store DocumentEmbedding record
                doc_embedding = DocumentEmbedding(
                    chunk_text=doc.page_content,
                    embedding=embedding,
                    source_file=source_file,
                    header_1=doc.metadata.get("Header 1"),
                    header_2=doc.metadata.get("Header 2"),
                    header_3=doc.metadata.get("Header 3"),
                )

                session.add(doc_embedding)
                stored_count += 1

        session.commit()

    print(f"Stored {stored_count} document embeddings in database")
    return stored_count


def search_similar(query_text: str, limit: int = 5, db_url: str = DATABASE_URL) -> List[dict]:
    """
    Search for documents similar to the query using vector similarity (cosine distance).
    
    Args:
        query_text: Query text to search for
        limit: Maximum number of results to return
        db_url: PostgreSQL connection string
        
    Returns:
        List of similar documents with scores
    """
    # Generate embedding for query
    query_embedding = generate_embeddings(query_text)
    
    engine = create_engine(db_url, poolclass=NullPool)
    results = []
    
    vec_str = "[" + ",".join(str(v) for v in query_embedding) + "]"

    with Session(engine) as session:
        stmt = (
            select(
                DocumentEmbedding.id,
                DocumentEmbedding.chunk_text,
                DocumentEmbedding.source_file,
                DocumentEmbedding.header_1,
                DocumentEmbedding.header_2,
                DocumentEmbedding.header_3,
                literal_column(f"embedding <=> '{vec_str}'::vector").label('distance')
            )
            .order_by(literal_column('distance'))
            .limit(limit)
        )

        rows = session.execute(stmt).fetchall()
        
        for row in rows:
            results.append({
                'id': row.id,
                'text': row.chunk_text,
                'source': row.source_file,
                'header_1': row.header_1,
                'header_2': row.header_2,
                'header_3': row.header_3,
                'similarity_score': float(row.distance),
            })
    
    return results


def build_context(search_results: List[dict]) -> str:
    """
    Build a context string from search results to augment the LLM prompt.
    
    Args:
        search_results: List of search results from search_similar()
        
    Returns:
        Formatted context string
    """
    if not search_results:
        return "No relevant context found."
    
    context_parts = []
    for i, result in enumerate(search_results, 1):
        header_path = []
        if result.get('header_1'):
            header_path.append(result['header_1'])
        if result.get('header_2'):
            header_path.append(result['header_2'])
        if result.get('header_3'):
            header_path.append(result['header_3'])
        
        header_info = " > ".join(header_path) if header_path else "Root"
        source_info = f"[{result['source']}]"
        
        context_parts.append(
            f"Document {i} ({header_info}) {source_info}:\n{result['text']}"
        )
    
    return "\n\n---\n\n".join(context_parts)


def query_rag(question: str, db_url: str = DATABASE_URL, search_limit: int = 5) -> str:
    """
    Execute a complete RAG pipeline: retrieve relevant documents and generate answer.
    
    Args:
        question: User's question
        db_url: PostgreSQL connection string
        search_limit: Number of similar documents to retrieve
        
    Returns:
        Generated answer augmented with retrieved context
    """
    # Step 1: Search for similar documents
    search_results = search_similar(question, limit=search_limit, db_url=db_url)

    # Step 2: Early return if nothing was retrieved
    if not search_results:
        return "I don't have information about that in my knowledge base."

    # Step 3: Build context with source citations
    context_parts = []
    for r in search_results:
        headers = [h for h in [r.get('header_1'), r.get('header_2')] if h is not None]
        header_str = " > ".join(headers) if headers else ""
        source_label = f"[Source: {r['source']} | {header_str}]" if header_str else f"[Source: {r['source']}]"
        context_parts.append(f"{source_label}\n{r['text']}")
    context = "\n\n---\n\n".join(context_parts)

    # Step 4: Build prompt using SYSTEM_PROMPT template
    prompt = SYSTEM_PROMPT.format(context=context, question=question)

    # Step 5: Generate response using LLM
    response = llm_client.invoke(prompt)

    return response.content


if __name__ == "__main__":
    # Example usage workflow
    print("=== RAG Engine Initialization ===")
    
    # Initialize database
    print("\n1. Initializing database...")
    initialize_database()
    
    # Parse documents
    print("\n2. Parsing documents...")
    documents = parse_documents(DOCS_DIR)
    
    # Store embeddings
    print("\n3. Storing document embeddings...")
    store_documents(documents)