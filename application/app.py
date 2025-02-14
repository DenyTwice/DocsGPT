import asyncio
import datetime
import http.client
import json
import logging
import os
import platform
import traceback

import dotenv
import openai
import requests
from celery import Celery
from celery.result import AsyncResult
from flask import Flask, request, send_from_directory, jsonify, Response, redirect
from langchain import FAISS
from langchain import VectorDBQA, Cohere, OpenAI
from langchain.chains import LLMChain, ConversationalRetrievalChain
from langchain.chains.conversational_retrieval.prompts import CONDENSE_QUESTION_PROMPT
from langchain.chains.question_answering import load_qa_chain
from langchain.chat_models import ChatOpenAI, AzureChatOpenAI
from langchain.embeddings import (
    OpenAIEmbeddings,
    HuggingFaceHubEmbeddings,
    CohereEmbeddings,
    HuggingFaceInstructEmbeddings,
)
from langchain.prompts import PromptTemplate
from langchain.prompts.chat import (
    ChatPromptTemplate,
    SystemMessagePromptTemplate,
    HumanMessagePromptTemplate,
    AIMessagePromptTemplate,
)
from langchain.schema import HumanMessage, AIMessage
from pymongo import MongoClient
from werkzeug.utils import secure_filename

from application.core.settings import settings
from application.error import bad_request
from application.worker import ingest_worker
from bson.objectid import ObjectId

# os.environ["LANGCHAIN_HANDLER"] = "langchain"

logger = logging.getLogger(__name__)
if settings.LLM_NAME == "gpt4":
    gpt_model = 'gpt-4'
else:
    gpt_model = 'gpt-3.5-turbo'


if settings.SELF_HOSTED_MODEL:
    from langchain.llms import HuggingFacePipeline
    from transformers import AutoModelForCausalLM, AutoTokenizer, pipeline

    model_id = settings.LLM_NAME # hf model id (Arc53/docsgpt-7b-falcon, Arc53/docsgpt-14b)
    tokenizer = AutoTokenizer.from_pretrained(model_id)
    model = AutoModelForCausalLM.from_pretrained(model_id)
    pipe = pipeline(
        "text-generation", model=model,
        tokenizer=tokenizer, max_new_tokens=2000,
        device_map="auto", eos_token_id=tokenizer.eos_token_id
    )
    hf = HuggingFacePipeline(pipeline=pipe)

# Redirect PosixPath to WindowsPath on Windows

if platform.system() == "Windows":
    import pathlib

    temp = pathlib.PosixPath
    pathlib.PosixPath = pathlib.WindowsPath

# loading the .env file
dotenv.load_dotenv()

# load the prompts
current_dir = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(current_dir, "prompts", "combine_prompt.txt"), "r") as f:
    template = f.read()

with open(os.path.join(current_dir, "prompts", "combine_prompt_hist.txt"), "r") as f:
    template_hist = f.read()

with open(os.path.join(current_dir, "prompts", "question_prompt.txt"), "r") as f:
    template_quest = f.read()

with open(os.path.join(current_dir, "prompts", "chat_combine_prompt.txt"), "r") as f:
    chat_combine_template = f.read()

with open(os.path.join(current_dir, "prompts", "chat_reduce_prompt.txt"), "r") as f:
    chat_reduce_template = f.read()

api_key_set = settings.API_KEY is not None
embeddings_key_set = settings.EMBEDDINGS_KEY is not None

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER = "inputs"
app.config["CELERY_BROKER_URL"] = settings.CELERY_BROKER_URL
app.config["CELERY_RESULT_BACKEND"] = settings.CELERY_RESULT_BACKEND
app.config["MONGO_URI"] = settings.MONGO_URI
celery = Celery()
celery.config_from_object("application.celeryconfig")
mongo = MongoClient(app.config["MONGO_URI"])
db = mongo["docsgpt"]
vectors_collection = db["vectors"]
conversations_collection = db["conversations"]


async def async_generate(chain, question, chat_history):
    result = await chain.arun({"question": question, "chat_history": chat_history})
    return result


def run_async_chain(chain, question, chat_history):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = {}
    try:
        answer = loop.run_until_complete(async_generate(chain, question, chat_history))
    finally:
        loop.close()
    result["answer"] = answer
    return result


def get_vectorstore(data):
    if "active_docs" in data:
        if data["active_docs"].split("/")[0] == "local":
            if data["active_docs"].split("/")[1] == "default":
                vectorstore = ""
            else:
                vectorstore = "indexes/" + data["active_docs"]
        else:
            vectorstore = "vectors/" + data["active_docs"]
        if data["active_docs"] == "default":
            vectorstore = ""
    else:
        vectorstore = ""
    vectorstore = os.path.join("application", vectorstore)
    return vectorstore


def get_docsearch(vectorstore, embeddings_key):
    if settings.EMBEDDINGS_NAME == "openai_text-embedding-ada-002":
        if is_azure_configured():
            os.environ["OPENAI_API_TYPE"] = "azure"
            openai_embeddings = OpenAIEmbeddings(model=settings.AZURE_EMBEDDINGS_DEPLOYMENT_NAME)
        else:
            openai_embeddings = OpenAIEmbeddings(openai_api_key=embeddings_key)
        docsearch = FAISS.load_local(vectorstore, openai_embeddings)
    elif settings.EMBEDDINGS_NAME == "huggingface_sentence-transformers/all-mpnet-base-v2":
        docsearch = FAISS.load_local(vectorstore, HuggingFaceHubEmbeddings())
    elif settings.EMBEDDINGS_NAME == "huggingface_hkunlp/instructor-large":
        docsearch = FAISS.load_local(vectorstore, HuggingFaceInstructEmbeddings())
    elif settings.EMBEDDINGS_NAME == "cohere_medium":
        docsearch = FAISS.load_local(vectorstore, CohereEmbeddings(cohere_api_key=embeddings_key))
    return docsearch


@celery.task(bind=True)
def ingest(self, directory, formats, name_job, filename, user):
    resp = ingest_worker(self, directory, formats, name_job, filename, user)
    return resp


@app.route("/")
def home():
    """
    The frontend source code lives in the /frontend directory of the repository.
    """
    if request.remote_addr in ('0.0.0.0', '127.0.0.1', 'localhost', '172.18.0.1'):
        # If users locally try to access DocsGPT running in Docker,
        # they will be redirected to the Frontend application.
        return redirect('http://localhost:5173')
    else:
        # Handle other cases or render the default page
        return 'Welcome to DocsGPT Backend!'


def complete_stream(question, docsearch, chat_history, api_key, conversation_id):
    openai.api_key = api_key
    if is_azure_configured():
        logger.debug("in Azure")
        openai.api_type = "azure"
        openai.api_version = settings.OPENAI_API_VERSION
        openai.api_base = settings.OPENAI_API_BASE
        llm = AzureChatOpenAI(
            openai_api_key=api_key,
            openai_api_base=settings.OPENAI_API_BASE,
            openai_api_version=settings.OPENAI_API_VERSION,
            deployment_name=settings.AZURE_DEPLOYMENT_NAME,
        )
    else:
        logger.debug("plain OpenAI")
        llm = ChatOpenAI(openai_api_key=api_key)
    docs = docsearch.similarity_search(question, k=2)
    # join all page_content together with a newline
    docs_together = "\n".join([doc.page_content for doc in docs])
    p_chat_combine = chat_combine_template.replace("{summaries}", docs_together)
    messages_combine = [{"role": "system", "content": p_chat_combine}]
    source_log_docs = []
    for doc in docs:
        if doc.metadata:
            data = json.dumps({"type": "source", "doc": doc.page_content, "metadata": doc.metadata})
            source_log_docs.append({"title": doc.metadata['title'].split('/')[-1], "text": doc.page_content})
        else:
            data = json.dumps({"type": "source", "doc": doc.page_content})
            source_log_docs.append({"title": doc.page_content, "text": doc.page_content})
        yield f"data:{data}\n\n"

    if len(chat_history) > 1:
        tokens_current_history = 0
        # count tokens in history
        chat_history.reverse()
        for i in chat_history:
            if "prompt" in i and "response" in i:
                tokens_batch = llm.get_num_tokens(i["prompt"]) + llm.get_num_tokens(i["response"])
                if tokens_current_history + tokens_batch < settings.TOKENS_MAX_HISTORY:
                    tokens_current_history += tokens_batch
                    messages_combine.append({"role": "user", "content": i["prompt"]})
                    messages_combine.append({"role": "system", "content": i["response"]})
    messages_combine.append({"role": "user", "content": question})
    completion = openai.ChatCompletion.create(model=gpt_model, engine=settings.AZURE_DEPLOYMENT_NAME,
                                              messages=messages_combine, stream=True, max_tokens=500, temperature=0)
    reponse_full = ""
    for line in completion:
        if "content" in line["choices"][0]["delta"]:
            # check if the delta contains content
            data = json.dumps({"answer": str(line["choices"][0]["delta"]["content"])})
            reponse_full += str(line["choices"][0]["delta"]["content"])
            yield f"data: {data}\n\n"
    # save conversation to database
    if conversation_id is not None:
        conversations_collection.update_one(
            {"_id": ObjectId(conversation_id)},
            {"$push": {"queries": {"prompt": question, "response": reponse_full, "sources": source_log_docs}}},
        )

    else:
        # create new conversation
        # generate summary
        messages_summary = [{"role": "assistant", "content": "Summarise following conversation in no more than 3 "
                                                             "words, respond ONLY with the summary, use the same "
                                                             "language as the system \n\nUser: " + question + "\n\n" +
                                                             "AI: " +
                                                             reponse_full},
                            {"role": "user", "content": "Summarise following conversation in no more than 3 words, "
                                                        "respond ONLY with the summary, use the same language as the "
                                                        "system"}]
        completion = openai.ChatCompletion.create(model='gpt-3.5-turbo', engine=settings.AZURE_DEPLOYMENT_NAME,
                                                  messages=messages_summary, max_tokens=30, temperature=0)
        conversation_id = conversations_collection.insert_one(
            {"user": "local",
             "date": datetime.datetime.utcnow(),
             "name": completion["choices"][0]["message"]["content"],
             "queries": [{"prompt": question, "response": reponse_full, "sources": source_log_docs}]}
        ).inserted_id

    # send data.type = "end" to indicate that the stream has ended as json
    data = json.dumps({"type": "id", "id": str(conversation_id)})
    yield f"data: {data}\n\n"
    data = json.dumps({"type": "end"})
    yield f"data: {data}\n\n"


@app.route("/stream", methods=["POST"])
def stream():
    data = request.get_json()
    # get parameter from url question
    question = data["question"]
    history = data["history"]
    # history to json object from string
    history = json.loads(history)
    conversation_id = data["conversation_id"]

    # check if active_docs is set

    if not api_key_set:
        api_key = data["api_key"]
    else:
        api_key = settings.API_KEY
    if not embeddings_key_set:
        embeddings_key = data["embeddings_key"]
    else:
        embeddings_key = settings.EMBEDDINGS_KEY
    if "active_docs" in data:
        vectorstore = get_vectorstore({"active_docs": data["active_docs"]})
    else:
        vectorstore = ""
    docsearch = get_docsearch(vectorstore, embeddings_key)

    # question = "Hi"
    return Response(
        complete_stream(question, docsearch,
                        chat_history=history, api_key=api_key,
                        conversation_id=conversation_id), mimetype="text/event-stream"
    )


def is_azure_configured():
    return settings.OPENAI_API_BASE and settings.OPENAI_API_VERSION and settings.AZURE_DEPLOYMENT_NAME


@app.route("/api/answer", methods=["POST"])
def api_answer():
    data = request.get_json()
    question = data["question"]
    history = data["history"]
    if "conversation_id" not in data:
        conversation_id = None
    else:
        conversation_id = data["conversation_id"]
    print("-" * 5)
    if not api_key_set:
        api_key = data["api_key"]
    else:
        api_key = settings.API_KEY
    if not embeddings_key_set:
        embeddings_key = data["embeddings_key"]
    else:
        embeddings_key = settings.EMBEDDINGS_KEY

    # use try and except  to check for exception
    try:
        # check if the vectorstore is set
        vectorstore = get_vectorstore(data)
        # loading the index and the store and the prompt template
        # Note if you have used other embeddings than OpenAI, you need to change the embeddings
        docsearch = get_docsearch(vectorstore, embeddings_key)

        q_prompt = PromptTemplate(
            input_variables=["context", "question"], template=template_quest, template_format="jinja2"
        )
        if settings.LLM_NAME == "openai_chat":
            if is_azure_configured():
                logger.debug("in Azure")
                llm = AzureChatOpenAI(
                    openai_api_key=api_key,
                    openai_api_base=settings.OPENAI_API_BASE,
                    openai_api_version=settings.OPENAI_API_VERSION,
                    deployment_name=settings.AZURE_DEPLOYMENT_NAME,
                )
            else:
                logger.debug("plain OpenAI")
                llm = ChatOpenAI(openai_api_key=api_key, model_name=gpt_model)  # optional parameter: model_name="gpt-4"
            messages_combine = [SystemMessagePromptTemplate.from_template(chat_combine_template)]
            if history:
                tokens_current_history = 0
                # count tokens in history
                history.reverse()
                for i in history:
                    if "prompt" in i and "response" in i:
                        tokens_batch = llm.get_num_tokens(i["prompt"]) + llm.get_num_tokens(i["response"])
                        if tokens_current_history + tokens_batch < settings.TOKENS_MAX_HISTORY:
                            tokens_current_history += tokens_batch
                            messages_combine.append(HumanMessagePromptTemplate.from_template(i["prompt"]))
                            messages_combine.append(AIMessagePromptTemplate.from_template(i["response"]))
            messages_combine.append(HumanMessagePromptTemplate.from_template("{question}"))
            p_chat_combine = ChatPromptTemplate.from_messages(messages_combine)
        elif settings.LLM_NAME == "openai":
            llm = OpenAI(openai_api_key=api_key, temperature=0)
        elif settings.SELF_HOSTED_MODEL:
            llm = hf
        elif settings.LLM_NAME == "cohere":
            llm = Cohere(model="command-xlarge-nightly", cohere_api_key=api_key)
        else:
            raise ValueError("unknown LLM model")

        if settings.LLM_NAME == "openai_chat":
            question_generator = LLMChain(llm=llm, prompt=CONDENSE_QUESTION_PROMPT)
            doc_chain = load_qa_chain(llm, chain_type="map_reduce", combine_prompt=p_chat_combine)
            chain = ConversationalRetrievalChain(
                retriever=docsearch.as_retriever(k=2),
                question_generator=question_generator,
                combine_docs_chain=doc_chain,
            )
            chat_history = []
            # result = chain({"question": question, "chat_history": chat_history})
            # generate async with async generate method
            result = run_async_chain(chain, question, chat_history)
        elif settings.SELF_HOSTED_MODEL:
            question_generator = LLMChain(llm=llm, prompt=CONDENSE_QUESTION_PROMPT)
            doc_chain = load_qa_chain(llm, chain_type="map_reduce", combine_prompt=p_chat_combine)
            chain = ConversationalRetrievalChain(
                retriever=docsearch.as_retriever(k=2),
                question_generator=question_generator,
                combine_docs_chain=doc_chain,
            )
            chat_history = []
            # result = chain({"question": question, "chat_history": chat_history})
            # generate async with async generate method
            result = run_async_chain(chain, question, chat_history)

        else:
            qa_chain = load_qa_chain(
                llm=llm, chain_type="map_reduce", combine_prompt=chat_combine_template, question_prompt=q_prompt
            )
            chain = VectorDBQA(combine_documents_chain=qa_chain, vectorstore=docsearch, k=3)
            result = chain({"query": question})

        print(result)

        # some formatting for the frontend
        if "result" in result:
            result["answer"] = result["result"]
        result["answer"] = result["answer"].replace("\\n", "\n")
        try:
            result["answer"] = result["answer"].split("SOURCES:")[0]
        except Exception:
            pass

        sources = docsearch.similarity_search(question, k=2)
        sources_doc = []
        for doc in sources:
            if doc.metadata:
                sources_doc.append({'title': doc.metadata['title'], 'text': doc.page_content})
            else:
                sources_doc.append({'title': doc.page_content, 'text': doc.page_content})
        result['sources'] = sources_doc

        # generate conversationId
        if conversation_id is not None:
            conversations_collection.update_one(
                {"_id": ObjectId(conversation_id)},
                {"$push": {"queries": {"prompt": question,
                                       "response": result["answer"], "sources": result['sources']}}},
            )

        else:
            # create new conversation
            # generate summary
            messages_summary = [AIMessage(content="Summarise following conversation in no more than 3 " +
                                                  "words, respond ONLY with the summary, use the same " +
                                                  "language as the system \n\nUser: " + question + "\n\nAI: " +
                                                  result["answer"]),
                                HumanMessage(content="Summarise following conversation in no more than 3 words, " +
                                                     "respond ONLY with the summary, use the same language as the " +
                                                     "system")]


            # completion = openai.ChatCompletion.create(model='gpt-3.5-turbo', engine=settings.AZURE_DEPLOYMENT_NAME,
            #                                           messages=messages_summary, max_tokens=30, temperature=0)
            completion = llm.predict_messages(messages_summary)
            conversation_id = conversations_collection.insert_one(
                {"user": "local",
                 "date": datetime.datetime.utcnow(),
                 "name": completion.content,
                 "queries": [{"prompt": question, "response": result["answer"], "sources": result['sources']}]}
            ).inserted_id

        result["conversation_id"] = str(conversation_id)

        # mock result
        # result = {
        #     "answer": "The answer is 42",
        #     "sources": ["https://en.wikipedia.org/wiki/42_(number)", "https://en.wikipedia.org/wiki/42_(number)"]
        # }
        return result
    except Exception as e:
        # print whole traceback
        traceback.print_exc()
        print(str(e))
        return bad_request(500, str(e))


@app.route("/api/docs_check", methods=["POST"])
def check_docs():
    # check if docs exist in a vectorstore folder
    data = request.get_json()
    # split docs on / and take first part
    if data["docs"].split("/")[0] == "local":
        return {"status": "exists"}
    vectorstore = "vectors/" + data["docs"]
    base_path = "https://raw.githubusercontent.com/arc53/DocsHUB/main/"
    if os.path.exists(vectorstore) or data["docs"] == "default":
        return {"status": "exists"}
    else:
        r = requests.get(base_path + vectorstore + "index.faiss")

        if r.status_code != 200:
            return {"status": "null"}
        else:
            if not os.path.exists(vectorstore):
                os.makedirs(vectorstore)
            with open(vectorstore + "index.faiss", "wb") as f:
                f.write(r.content)

            # download the store
            r = requests.get(base_path + vectorstore + "index.pkl")
            with open(vectorstore + "index.pkl", "wb") as f:
                f.write(r.content)

        return {"status": "loaded"}


@app.route("/api/feedback", methods=["POST"])
def api_feedback():
    data = request.get_json()
    question = data["question"]
    answer = data["answer"]
    feedback = data["feedback"]

    print("-" * 5)
    print("Question: " + question)
    print("Answer: " + answer)
    print("Feedback: " + feedback)
    print("-" * 5)
    response = requests.post(
        url="https://86x89umx77.execute-api.eu-west-2.amazonaws.com/docsgpt-feedback",
        headers={
            "Content-Type": "application/json; charset=utf-8",
        },
        data=json.dumps({"answer": answer, "question": question, "feedback": feedback}),
    )
    return {"status": http.client.responses.get(response.status_code, "ok")}


@app.route("/api/combine", methods=["GET"])
def combined_json():
    user = "local"
    """Provide json file with combined available indexes."""
    # get json from https://d3dg1063dc54p9.cloudfront.net/combined.json

    data = [
        {
            "name": "default",
            "language": "default",
            "version": "",
            "description": "default",
            "fullName": "default",
            "date": "default",
            "docLink": "default",
            "model": settings.EMBEDDINGS_NAME,
            "location": "local",
        }
    ]
    # structure: name, language, version, description, fullName, date, docLink
    # append data from vectors_collection
    for index in vectors_collection.find({"user": user}):
        data.append(
            {
                "name": index["name"],
                "language": index["language"],
                "version": "",
                "description": index["name"],
                "fullName": index["name"],
                "date": index["date"],
                "docLink": index["location"],
                "model": settings.EMBEDDINGS_NAME,
                "location": "local",
            }
        )

    data_remote = requests.get("https://d3dg1063dc54p9.cloudfront.net/combined.json").json()
    for index in data_remote:
        index["location"] = "remote"
        data.append(index)

    return jsonify(data)


@app.route("/api/upload", methods=["POST"])
def upload_file():
    """Upload a file to get vectorized and indexed."""
    if "user" not in request.form:
        return {"status": "no user"}
    user = secure_filename(request.form["user"])
    if "name" not in request.form:
        return {"status": "no name"}
    job_name = secure_filename(request.form["name"])
    # check if the post request has the file part
    if "file" not in request.files:
        print("No file part")
        return {"status": "no file"}
    file = request.files["file"]
    if file.filename == "":
        return {"status": "no file name"}

    if file:
        filename = secure_filename(file.filename)
        # save dir
        save_dir = os.path.join(app.config["UPLOAD_FOLDER"], user, job_name)
        # create dir if not exists
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)

        file.save(os.path.join(save_dir, filename))
        task = ingest.delay("temp", [".rst", ".md", ".pdf", ".txt"], job_name, filename, user)
        # task id
        task_id = task.id
        return {"status": "ok", "task_id": task_id}
    else:
        return {"status": "error"}


@app.route("/api/task_status", methods=["GET"])
def task_status():
    """Get celery job status."""
    task_id = request.args.get("task_id")
    task = AsyncResult(task_id)
    task_meta = task.info
    return {"status": task.status, "result": task_meta}


### Backgound task api
@app.route("/api/upload_index", methods=["POST"])
def upload_index_files():
    """Upload two files(index.faiss, index.pkl) to the user's folder."""
    if "user" not in request.form:
        return {"status": "no user"}
    user = secure_filename(request.form["user"])
    if "name" not in request.form:
        return {"status": "no name"}
    job_name = secure_filename(request.form["name"])
    if "file_faiss" not in request.files:
        print("No file part")
        return {"status": "no file"}
    file_faiss = request.files["file_faiss"]
    if file_faiss.filename == "":
        return {"status": "no file name"}
    if "file_pkl" not in request.files:
        print("No file part")
        return {"status": "no file"}
    file_pkl = request.files["file_pkl"]
    if file_pkl.filename == "":
        return {"status": "no file name"}

    # saves index files
    save_dir = os.path.join("indexes", user, job_name)
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)
    file_faiss.save(os.path.join(save_dir, "index.faiss"))
    file_pkl.save(os.path.join(save_dir, "index.pkl"))
    # create entry in vectors_collection
    vectors_collection.insert_one(
        {
            "user": user,
            "name": job_name,
            "language": job_name,
            "location": save_dir,
            "date": datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
            "model": settings.EMBEDDINGS_NAME,
            "type": "local",
        }
    )
    return {"status": "ok"}


@app.route("/api/download", methods=["get"])
def download_file():
    user = secure_filename(request.args.get("user"))
    job_name = secure_filename(request.args.get("name"))
    filename = secure_filename(request.args.get("file"))
    save_dir = os.path.join(app.config["UPLOAD_FOLDER"], user, job_name)
    return send_from_directory(save_dir, filename, as_attachment=True)


@app.route("/api/delete_old", methods=["get"])
def delete_old():
    """Delete old indexes."""
    import shutil

    path = request.args.get("path")
    dirs = path.split("/")
    dirs_clean = []
    for i in range(1, len(dirs)):
        dirs_clean.append(secure_filename(dirs[i]))
    # check that path strats with indexes or vectors
    if dirs[0] not in ["indexes", "vectors"]:
        return {"status": "error"}
    path_clean = "/".join(dirs)
    vectors_collection.delete_one({"location": path})
    try:
        shutil.rmtree(path_clean)
    except FileNotFoundError:
        pass
    return {"status": "ok"}


@app.route("/api/get_conversations", methods=["get"])
def get_conversations():
    # provides a list of conversations
    conversations = conversations_collection.find().sort("date", -1)
    list_conversations = []
    for conversation in conversations:
        list_conversations.append({"id": str(conversation["_id"]), "name": conversation["name"]})

    #list_conversations = [{"id": "default", "name": "default"}, {"id": "jeff", "name": "jeff"}]

    return jsonify(list_conversations)

@app.route("/api/get_single_conversation", methods=["get"])
def get_single_conversation():
    # provides data for a conversation
    conversation_id = request.args.get("id")
    conversation = conversations_collection.find_one({"_id": ObjectId(conversation_id)})
    return jsonify(conversation['queries'])

@app.route("/api/delete_conversation", methods=["POST"])
def delete_conversation():
    # deletes a conversation from the database
    conversation_id = request.args.get("id")
    # write to mongodb
    conversations_collection.delete_one(
        {
            "_id": ObjectId(conversation_id),
        }
    )

    return {"status": "ok"}


# handling CORS
@app.after_request
def after_request(response):
    response.headers.add("Access-Control-Allow-Origin", "*")
    response.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization")
    response.headers.add("Access-Control-Allow-Methods", "GET,PUT,POST,DELETE,OPTIONS")
    response.headers.add("Access-Control-Allow-Credentials", "true")
    return response


if __name__ == "__main__":
    app.run(debug=True, port=7091)
