from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, text, Engine, event
from sqlalchemy.exc import SQLAlchemyError
from pydantic import BaseModel
from typing import Dict, AsyncGenerator, Optional
import logging
from contextlib import asynccontextmanager
import time
from execute import execute_query
from nlp2sql import get_sql
from docs import gen_docs
from logger import after_execute, before_execute
from chat import get_reply
from graph import get_graph
from schema import get_db_metadata, Metadata, TableSchema
import json


app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Temporary database
METADATA_STORAGE: Dict[str, Metadata] = {}
ENGINE_CACHE: Dict[str, Engine] = {}
QUERY_LOG = {}

# Function to get or create an engine
def get_engine(connection_string: str):
    if connection_string not in ENGINE_CACHE:
        engine = create_engine(connection_string, pool_size=5, max_overflow=10)
        ENGINE_CACHE[connection_string] = engine
    return ENGINE_CACHE[connection_string]

class ValidateRequest(BaseModel):
    connection_string: str

@app.post("/validate_connection/")
def validate_connection(request: ValidateRequest):
    try:
        engine = get_engine(request.connection_string)

        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))

            #includes the schema of the table and the extra stats
            metadata = get_db_metadata(engine)
            print (metadata)
            # Temporary solution (replace with Redis later)
            METADATA_STORAGE[request.connection_string] = metadata
            #binding after schema because it runs a huge query and it sends through a wall of text QOL change 
            event.listen(engine, "before_execute", before_execute)
            event.listen(engine, "after_execute", after_execute)

        return {"success": True, "data": metadata}
    except SQLAlchemyError as e:
        return {"success": False, "message": str(e)}

class QueryRequest(BaseModel):
    connection_string: str
    query: str

#move this to its own file later
@app.post("/execute_query/")
def executeQuery(request: QueryRequest):
    connection_string = request.connection_string
    query = request.query

    if not connection_string or not query:
        return {"success": False, "message": "Connection string or Query is missing"}

    try:
        engine = get_engine(connection_string)
        with engine.connect() as connection:  # Ensure proper connection handling
            return execute_query(connection, query)
    except SQLAlchemyError as e:
        return {"success": False, "message": str(e)}

#util function if you wish to acces this later
@app.post("/get_schema/")
def getschema(request: ValidateRequest):
    try:
        metadata = METADATA_STORAGE[request.connection_string]
        return {"success":True, "data": metadata.schema}
    except:
        return {"success": False, "message": "Failed to get schema"}

class NLPRequest(BaseModel):
    description: str
    connection_string: Optional[str]
    schema: Optional[Dict[str, TableSchema]]

@app.post("/nlp2sql")
def getSQL(request: NLPRequest):
    description = request.description
    connection_string = request.connection_string
    schema = request.schema
    if not schema:
        schema = METADATA_STORAGE.get(connection_string)
        if not schema:
            return {"success": False, "message": "Try connecting to your database again"}
        schema = schema.get("schema")
    return get_sql(description, schema)

class DocsRequest(BaseModel):
    connection_string: Optional[str]
    schema: Optional[Dict[str, TableSchema]]

@app.post("/docs")
def genDocs(request: DocsRequest):
    connection_string = request.connection_string
    schema = request.schema
    if not connection_string and not schema:
        return {"success": False, "message": "Field connection_string or schema is missing"}
    #need some better edge case handling here in case metadata.get() returns None
    return gen_docs(schema or METADATA_STORAGE.get(connection_string).get("schema"))

class ChatRequest(BaseModel):
    userInput: str
    query: Optional[str]
    connection_string: Optional[str]
    metadata: Optional[Metadata]

@app.post("/chat")
def getReply(request: ChatRequest):
    userInput = request.userInput
    query = request.query
    connection_string = request.connection_string
    metadata = request.metadata
    if metadata:
        #done because its not json serilizable by default and contains pydantic models
        metadata = metadata.model_dump()
    if not connection_string and not metadata:
        return {"success": False, "message":"Not enough data"}
    return get_reply(userInput, query, metadata or METADATA_STORAGE.get(connection_string))

@app.post("/graph")
def getGraph(request: ChatRequest):
    userInput = request.userInput
    query = request.query
    connection_string = request.connection_string
    metadata = request.metadata
    if metadata:
        #done because its not json serilizable by default and contains pydantic models
        metadata = metadata.model_dump()
    if not connection_string and not metadata:
        return {"success": False, "message":"Not enough data"}
    try:
        engine = get_engine(connection_string)
        with engine.connect() as connection:  # Ensure proper connection handling
            return get_graph(userInput, query, metadata or METADATA_STORAGE.get(connection_string), connection)
    except SQLAlchemyError as e:
        return {"success": False, "message": str(e)}

#need to test this what does bro even do
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    yield
    logging.info("Shutting down, closing all database connections...")
    for conn_str, engine in ENGINE_CACHE.items():
        logging.info(f"Closing connection for {conn_str}")
        engine.dispose()
    ENGINE_CACHE.clear()

app.router.lifespan_context = lifespan