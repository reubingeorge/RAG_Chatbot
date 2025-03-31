import os
import time
import logging
from django.shortcuts import render, HttpResponse
from django.views.decorators.http import require_POST
from django.conf import settings

from langchain_openai import OpenAIEmbeddings
from langchain_postgres.vectorstores import PGVector
from langchain.docstore.document import Document

from sqlalchemy import create_engine, text

from .utils import get_connection_string, process_pdf

logger = logging.getLogger(__name__)


def index(request):
    """Render the main chat interface."""
    history = request.session.get('history', [])
    return render(request, "chat/index.html", {"history": history})


@require_POST
def upload(request):
    files = request.FILES.getlist('pdf')
    if not files:
        return HttpResponse("No files uploaded.", status=400)

    if not request.session.session_key:
        request.session.create()
    collection_name = request.session.get('vector_collection')
    if not collection_name:
        collection_name = f"session_{request.session.session_key}"
        request.session['vector_collection'] = collection_name

    openai_key = os.environ.get("OPENAI_API_KEY") or settings.OPENAI_API_KEY
    embeddings = OpenAIEmbeddings(openai_api_key=openai_key)
    connection_string = get_connection_string()

    # Process PDF files into Document objects
    documents = []
    for file in files:
        try:
            docs = process_pdf(file)
            documents.extend(docs)
        except Exception as e:
            logger.exception("Failed to process file: %s", file.name)
            return HttpResponse(f"Error processing {file.name}: {e}", status=500)

    logger.info("Total documents to index: %d", len(documents))

    try:
        # Artificial delay for testing purposes
        time.sleep(5)
        if not request.session.get('_vectordb_initialized'):
            logger.info("Creating new vector collection: %s", collection_name)
            PGVector.from_documents(
                documents=documents,
                embedding=embeddings,
                collection_name=collection_name,
                connection=connection_string
            )
            request.session['_vectordb_initialized'] = True
        else:
            logger.info("Adding documents to existing collection: %s", collection_name)
            vectorstore = PGVector(
                collection_name=collection_name,
                connection=connection_string,
                embeddings=embeddings
            )
            vectorstore.add_documents(documents)
    except Exception as e:
        logger.exception("Error indexing documents:")
        return HttpResponse(f"Error indexing documents: {e}", status=500)

    return HttpResponse("PDF uploaded and indexed successfully.", content_type="text/plain")


@require_POST
def ask(request):
    """Handle a user question: retrieve relevant chunks and get answer from LLM."""
    question = request.POST.get("question")
    if not question:
        return HttpResponse("Question cannot be empty.", status=400)
    collection_name = request.session.get('vector_collection')
    if not collection_name:
        return HttpResponse("No documents available. Please upload a PDF first.", status=400)

    openai_key = os.environ.get("OPENAI_API_KEY") or settings.OPENAI_API_KEY
    embeddings = OpenAIEmbeddings(openai_api_key=openai_key)
    connection_string = get_connection_string()

    try:
        vectorstore = PGVector(
            collection_name=collection_name,
            connection=connection_string,
            embeddings=embeddings
        )
    except Exception as e:
        return HttpResponse(f"Error retrieving documents: {e}", status=500)

    from langchain_openai import ChatOpenAI
    from langchain.chains import ConversationalRetrievalChain
    from langchain_core.messages import HumanMessage, AIMessage

    llm = ChatOpenAI(model_name="gpt-3.5-turbo", temperature=0, openai_api_key=openai_key)
    retriever = vectorstore.as_retriever(search_kwargs={"k": 4})

    # Format chat history
    chat_history_tuples = request.session.get('history', [])
    formatted_history = []
    for human_msg, ai_msg in chat_history_tuples:
        formatted_history.append(HumanMessage(content=human_msg))
        formatted_history.append(AIMessage(content=ai_msg))

    qa_chain = ConversationalRetrievalChain.from_llm(llm, retriever=retriever, return_source_documents=True)
    result = qa_chain.invoke({
        "question": question,
        "chat_history": formatted_history
    })
    answer = result["answer"]
    chat_history_tuples.append((question, answer))
    request.session['history'] = chat_history_tuples

    return render(request, "chat/_qa_pair.html", {
        "user_message": question,
        "bot_message": answer
    })


@require_POST
def remove_file(request):
    """Remove all embeddings for a given file (based on its 'source' in metadata)."""
    file_name = request.POST.get("file_name")
    if not file_name:
        return HttpResponse("No file specified.", status=400)
    collection_name = request.session.get('vector_collection')
    if not collection_name:
        return HttpResponse("No collection found.", status=400)
    connection_string = get_connection_string()
    engine = create_engine(connection_string)
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT uuid FROM langchain_pg_collection WHERE name = :name"),
            {"name": collection_name}
        )
        collection = result.fetchone()
        if not collection:
            return HttpResponse("Collection not found.", status=404)
        collection_uuid = collection[0]
        conn.execute(
            text(
                "DELETE FROM langchain_pg_embedding WHERE collection_id = :collection_uuid AND cmetadata->>'source' = :file_name"),
            {"collection_uuid": collection_uuid, "file_name": file_name}
        )
        conn.commit()
    return HttpResponse("File removed successfully.", content_type="text/plain")


def files_view(request):
    """Show all distinct file names (from metadata) that have been uploaded."""
    collection_name = request.session.get('vector_collection')
    file_names = []

    if collection_name:
        connection_string = get_connection_string()
        engine = create_engine(connection_string)
        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT uuid FROM langchain_pg_collection WHERE name = :name"),
                {"name": collection_name}
            )
            collection = result.fetchone()
            if collection:
                collection_uuid = collection[0]
                result = conn.execute(
                    text(
                        "SELECT DISTINCT cmetadata->>'source' as file_name FROM langchain_pg_embedding WHERE collection_id = :collection_uuid"),
                    {"collection_uuid": collection_uuid}
                )
                file_names = [row["file_name"] for row in result.mappings().all()]

    # Check if this is a partial request (via query param or HTMX header)
    is_partial = request.GET.get('partial', 'false') == 'true' or request.headers.get('HX-Request')

    if is_partial:
        return render(request, "chat/partials/file_list.html", {"files": file_names})

    return render(request, "chat/files.html", {"files": file_names})