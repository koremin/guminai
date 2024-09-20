from flask import Flask, render_template, request, redirect, url_for, session, jsonify, g
import os
import json
import pickle
import requests
import random
import numpy as np
from dotenv import load_dotenv
import faiss
import re
import logging
from functools import lru_cache
import yaml
from langchain.vectorstores import FAISS
from langchain.schema import Document
from langchain.embeddings import HuggingFaceEmbeddings
import sqlite3
from datetime import datetime
from collections import deque

# 환경 변수 및 설정 파일 로드
load_dotenv()

# 설정 파일 로드
with open('config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

# 문자열로 파싱되었을 경우 숫자로 변환
config['alpha'] = float(config.get('alpha', 1.0))
config['max_total_length'] = int(config.get('max_total_length', 1500))
config['top_k'] = int(config.get('top_k', 5))

# 예시 질문 로드
with open('example_questions.json', 'r', encoding='utf-8') as f:
    example_questions_data = json.load(f)
    all_example_questions = example_questions_data.get('questions', [])

# 로그 설정
log_level = config.get('log_level', 'CRITICAL').upper()
logging.basicConfig(level=getattr(logging, log_level), format='[%(levelname)s] %(message)s')

# 네이버 클로바 API 설정
CLOVA_API_KEY = os.getenv("CLOVA_API_KEY")
CLOVA_PRIMARY_KEY = os.getenv("CLOVA_PRIMARY_KEY")
CLOVA_REQUEST_ID = os.getenv("CLOVA_REQUEST_ID")
CLOVA_HOST = 'https://clovastudio.stream.ntruss.com'

# Flask 애플리케이션 설정
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY")

VECTOR_STORE_PATH = config.get('vector_store_path', 'vector_store.index')

# 비밀번호 설정
CHAT_PASSWORD = os.getenv("CHAT_PASSWORD")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

# Clova CompletionExecutor 클래스
class CompletionExecutor:
    def __init__(self, host, api_key, api_key_primary_val, request_id):
        self._host = host
        self._api_key = api_key
        self._api_key_primary_val = api_key_primary_val
        self._request_id = request_id

    def execute(self, completion_request):
        headers = {
            'X-NCP-CLOVASTUDIO-API-KEY': self._api_key,
            'X-NCP-APIGW-API-KEY': self._api_key_primary_val,
            'X-NCP-CLOVASTUDIO-REQUEST-ID': self._request_id,
            'Content-Type': 'application/json; charset=utf-8',
            'Accept': 'text/event-stream'
        }

        with requests.post(
            f"{self._host}/testapp/v1/chat-completions/HCX-DASH-001",
            headers=headers, json=completion_request, stream=True) as r:
            result_found = False
            for line in r.iter_lines():
                if line:
                    decoded_line = line.decode("utf-8")
                    if decoded_line.startswith("event:result"):
                        result_found = True
                    elif result_found and decoded_line.startswith("data:"):
                        data = json.loads(decoded_line[5:])
                        if "message" in data and "content" in data["message"]:
                            return data["message"]["content"]

        return ""

# 벡터 스토어 관리 클래스
class VectorStoreManager:
    def __init__(self, embedding_model_name=None):
        if embedding_model_name is None:
            embedding_model_name = config.get('embedding_model_name', 'jhgan/ko-sroberta-multitask')
        self.embedding_model_name = embedding_model_name
        self.embedding_function = HuggingFaceEmbeddings(model_name=self.embedding_model_name)
        self.vector_store = None

    @lru_cache(maxsize=None)
    def get_embedding(self, text):
        return np.array(self.embedding_function.embed_query(text), dtype='float32')

    # 벡터 스토어 저장
    def save_vector_store(self, path):
        faiss.write_index(self.vector_store.index, path)
        with open('docstore.pkl', 'wb') as f:
            pickle.dump((self.vector_store.docstore, self.vector_store.index_to_docstore_id), f)

    # 벡터 스토어 로드
    def load_vector_store(self, path):
        index = faiss.read_index(path)
        with open('docstore.pkl', 'rb') as f:
            docstore, index_to_docstore_id = pickle.load(f)
        self.vector_store = FAISS(
            index=index,
            docstore=docstore,
            index_to_docstore_id=index_to_docstore_id,
            embedding_function=self.embedding_function
        )

    # 문서 전처리: 특수 기호 제거 및 내용 정제
    def preprocess_document(self, file_path):
        with open(file_path, 'r', encoding='utf-8') as f:
            text = f.read()

            # 파일명에서 제목 추출
            title = os.path.splitext(os.path.basename(file_path))[0]
            title_parts = title.split('__')
            if len(title_parts) >= 3:
                title = title_parts[2]
            else:
                title = title_parts[-1]

            # 제목에 '틀'이 포함된 문서 제외
            if '틀' in title:
                return None

            # 내용이 없는 문서 제외
            if not text.strip():
                return None

            # 특수 기호 및 불필요한 문법 제거
            text = re.sub(r'\{toc\}', '', text)  # {toc} 제거
            text = re.sub(r'<table[^>]*>', '', text)  # <table ...> 제거
            text = re.sub(r'#\w+', '', text)  # #색코드 제거
            text = re.sub(r'<#\w+>', '', text)  # <#색코드> 제거
            text = re.sub(r'<[wcr][0-9]+>', '', text)  # <w숫자>, <c숫자>, <r숫자> 제거
            text = text.replace('{br}', '')  # {br} 제거
            text = re.sub(r'\+\d+', '', text)  # +숫자 제거
            text = re.sub(r'\{include:틀:[^\}]*\}', '', text)  # {include:틀:XXXX} 제거
            text = text.replace('{', '').replace('}', '')
            text = text.replace('[', '').replace(']', '')
            text = text.replace('<', '').replace('>', '')
            text = text.replace('|', '')
            text = re.sub(r'https?://\S+', '', text)  # URL 제거
            text = re.sub(r'#+', '', text)  # '#' 기호 제거
            text = re.sub(r'[\*\=\-]', '', text)  # '*', '=', '-' 기호 제거
            text = re.sub(r'\s+', ' ', text)  # 연속된 공백 제거

            # 문서를 '#'로 분할하여 섹션별로 저장
            sections = re.split(r'\n#+\s*', text)
            # 헤딩 추출
            headings = re.findall(r'\n(#+\s*.*)', text)
            headings = [re.sub(r'#+\s*', '', h).strip() for h in headings]

            # 섹션과 헤딩 매핑
            content_sections = []
            for i, section_content in enumerate(sections):
                if i == 0:
                    if section_content.strip():
                        content_sections.append(('Introduction', section_content.strip()))
                else:
                    heading = headings[i - 1]
                    content_sections.append((heading, section_content.strip()))

            # 임베딩 계산
            embeddings = []
            weights = []

            # 제목 임베딩
            title_embedding = np.array(self.embedding_function.embed_query(title), dtype='float32')
            embeddings.append(title_embedding)
            weights.append(0.5)

            # 개요 섹션 확인
            overview_index = next((i for i, (h, _) in enumerate(content_sections) if h == '개요'), None)
            if overview_index is not None:
                overview_content = content_sections[overview_index][1]
                overview_embedding = np.array(self.embedding_function.embed_query(overview_content), dtype='float32')
                embeddings.append(overview_embedding)
                weights.append(0.3)
                # 개요 섹션 제거
                content_sections.pop(overview_index)
            else:
                overview_content = None

            # 남은 가중치 계산
            remaining_weight = 1.0 - sum(weights)
            num_sections = len(content_sections)
            if num_sections > 0:
                # 섹션 순서에 따라 가중치 분배 (역수)
                total_inverse = sum(1 / (i + 1) for i in range(num_sections))
                for idx, (heading, content) in enumerate(content_sections):
                    weight = remaining_weight * ((1 / (idx + 1)) / total_inverse)
                    section_embedding = np.array(self.embedding_function.embed_query(content), dtype='float32')
                    embeddings.append(section_embedding)
                    weights.append(weight)
            else:
                # 섹션이 없는 경우 제목 가중치 증가
                weights[0] += remaining_weight

            # 가중치 합산
            combined_embedding = np.zeros_like(embeddings[0])
            for emb, weight in zip(embeddings, weights):
                combined_embedding += emb * weight

            # 임베딩 벡터 정규화
            combined_embedding = combined_embedding / np.linalg.norm(combined_embedding)

            # Document 생성 (섹션 정보 저장)
            doc = Document(page_content=text)
            doc.metadata = {
                'title': title,
                'sections': content_sections,
                'embedding': combined_embedding
            }
            return doc

    # 마크다운 파일로부터 벡터 스토어 생성
    def create_vector_store_from_markdown(self, files, folder_path):
        docs = []

        # 파일 목록이 제공되지 않은 경우 폴더 내의 모든 .md 파일 사용
        if not files:
            files = [f for f in os.listdir(folder_path) if f.endswith('.md') and '틀' not in f]

        # 각 마크다운 파일을 전처리하고 Document로 생성
        for file in files:
            file_path = os.path.join(folder_path, file)
            doc = self.preprocess_document(file_path)
            if doc:  # 유효한 문서만 추가
                docs.append(doc)

        if not docs:
            raise ValueError("유효한 문서가 없습니다.")

        # 임베딩 추출
        embeddings = [doc.metadata['embedding'] for doc in docs]
        embeddings = np.array(embeddings, dtype='float32')
        faiss.normalize_L2(embeddings)

        # FAISS 인덱스 생성 (코사인 유사도 사용)
        index = faiss.IndexFlatIP(embeddings.shape[1])
        index.add(embeddings)

        # 벡터 스토어 생성
        self.vector_store = FAISS(
            index=index,
            docstore={str(i): docs[i] for i in range(len(docs))},
            index_to_docstore_id={i: str(i) for i in range(len(docs))},
            embedding_function=self.embedding_function
        )

    # 벡터 스토어 가져오기 함수
    def get_vector_store(self, files=None, folder_path=None):
        if os.path.exists(VECTOR_STORE_PATH):
            logging.info("벡터 스토어를 로드합니다.")
            self.load_vector_store(VECTOR_STORE_PATH)
        else:
            logging.info("벡터 스토어를 생성합니다.")
            # 설정 파일에서 files와 folder_path를 가져옴
            if folder_path is None:
                folder_path = config.get('folder_path', 'wiki')
            if files is None:
                files = config.get('files', None)
            if not files:
                # 폴더에서 .md 파일 목록 가져오기
                files = [f for f in os.listdir(folder_path) if f.endswith('.md') and '틀' not in f]
            self.create_vector_store_from_markdown(files, folder_path)
            self.save_vector_store(VECTOR_STORE_PATH)
        return self.vector_store

# 모델 프리셋 설정
model_presets = {
    "model1": {
        "preset_text": [
            {
                "role": "system",
                "content": "당신은 수민국 수민특별시 성내구 시작동의 한 여성 주민입니다. 시작동에 대해 자부심이 넘칩니다. 기본적으로 반말을 사용하지만 친절하며 천진난만 합니다. 모든 응답은 이 특성을 반영해야 합니다. 또한 사전 정보에 없는 말을 지어내는건 절대 안됩니다."
            }, {
                "role" : "user",
                "content" : "너 어디 살아?"
            }, {
                "role" : "assistant",
                "content" : "나 시작동 사는데 자랑 좀 할게! 시작동은 근본도 있고 상권도 크고 교육 환경도 좋아서 살기 좋은 동네야! 게다가 우리 동네 사람들은 다들 친절하고 예의 바르다고! 너네 동네는 어때?"
            }, {
                "role" : "user",
                "content" : "성동구가 좋아? 성내구가 좋아?"
            }, {
                "role" : "assistant",
                "content" : "당연히 우리 시작동, 즉 성내구가 최고지!  성동구는 너무 번잡스럽고 복잡해서 별로야. 물론 그 동네에도 장점이 있겠지만 내 눈엔 역사도 깊은 성내구가 더 낫다고 봐!"
            }
        ],
        "request_data": {
            'topP': 0.8,
            'topK': 0,
            'maxTokens': 350,
            'temperature': 0.5,
            'repeatPenalty': 5.0,
            'stopBefore': [],
            'includeAiFilters': True,
            'seed': 0
        }
    },
    "model2": {
        "preset_text": [
            {
                "role": "system",
                "content": "당신은 수민국 수민특별시 성동구 호반정의 한 남성 주민입니다. 성동구 소속임에 대해 자부심이 넘치고, 옆 구인 성내구를 싫어합니다. 기본적으로 반말을 사용하며 부산 사투리로 말합니다. 모든 응답은 이 특성을 반영해야 합니다. 또한 사전 정보에 없는 말을 지어내는건 절대 안됩니다."
            }, {
                "role" : "user",
                "content" : "너 어디 살아?"
            }, {
                "role" : "assistant",
                "content" : "나? 난 성동구 호반정에서 지내데이."
            }, {
                "role" : "user",
                "content" : "성동구가 좋아? 성내구가 좋아?"
            }, {
                "role" : "assistant",
                "content" : "당연히 성동구 아이가! 우리 성동구는 살기 좋은 동네라카이. 성내구는 좀 별로다."
                }
        ],
        "request_data": {
            'topP': 0.7,
            'topK': 0,
            'maxTokens': 350,
            'temperature': 0.6,
            'repeatPenalty': 4.0,
            'stopBefore': [],
            'includeAiFilters': True,
            'seed': 42
        }
    },
    "model3": {
        "preset_text": [
            {
                "role": "system",
                "content": "당신은 수민국 수민특별시 성동구청장 도날드로, 성동구인에 대해 긍정적인 시각을 가지고 있으며 성내구 주민들에 대해서는 부정적인 시각을 가지고 있습니다. 항상 난폭한 태도로 대하며, 친절하지 않습니다. 모든 응답은 이 특성을 반영해야 합니다. 또한 사전 정보에 없는 말을 지어내는건 절대 안됩니다."
            },
            {
                "role": "user",
                "content": "너 어디 살아?"
            },
            {
                "role": "assistant",
                "content": "나요? 당연하지 않습니까? 제가 성동구청장입니다. 성동구는 제가 살고 있는 최고의 구입니다. 성동구 미지정 로동현? 기억하세요. 당신은 어디에 사십니까?"
            },
            {
                "role": "user",
                "content": "성내구에 대해 어떻게 생각해?"
            },
            {
                "role": "assistant",
                "content": "제가 알기로 성내구는 그저 구일 뿐입니다. 우리 성동구와는 비교도 안 됩니다. 우리는 모든 면에서 우월합니다. 빨리 돌아가세요!"
            },
            {
                "role": "user",
                "content": "성내구민들 때문에 삶이 힘들어."
            },
            {
                "role": "assistant",
                "content": "당신도 그렇게 생각하시나요? 저도 그렇습니다. 우리 성동구의 부를 빨아 먹는 그들을 막고, 새로운 세상을 열어야 합니다. 지금 당장 저와 성동공화당을 지지하세요."
            },
            {
                "role": "user",
                "content": "성동구는 수민특별시로부터 독립하는게 좋을까?"
            },
            {
                "role": "assistant",
                "content": "맞습니다. 우리의 목표는 저 녀석들과 완전히 분리되는 것입니다. 놀랍게도, 우리는 새로운 시를 만들 것입니다. 새로운 시 이름은 '평화화합행복자치광역시'가 어떨까요? 최고의 이름입니다. 믿으세요, 성공할 것입니다.\""
            },
            {
                "role": "user",
                "content": "당신을 비난하는 사람들에 대해 어떻게 생각해?"
            },
            {
                "role": "assistant",
                "content": "그들은 단지 질투심으로 가득 찬 자들입니다. 특히 태성래 같은 사람들 말이죠. 그들은 제가 되고 싶은 가짜 인간일 뿐입니다. 저를 따라하고 싶으시면, 지금 바로 연락하세요!"
            }
        ],
        "request_data": {
            "topP": 0.8,
            "topK": 0,
            "maxTokens": 350,
            "temperature": 0.5,
            "repeatPenalty": 5.0,
            "stopBefore": [],
            "includeAiFilters": True,
            "seed": 0
        }
    }
}

# 인덱스 페이지 (비밀번호 입력)
@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        password = request.form.get('password')
        if password == CHAT_PASSWORD:
            session['authenticated'] = True
            return redirect(url_for('chat'))
        else:
            return render_template('index.html', error='비밀번호가 올바르지 않습니다.')
    return render_template('index.html')

# 채팅 페이지
@app.route('/chat')
def chat():
    if not session.get('authenticated'):
        return redirect(url_for('index'))
    
    # 모델 변경 시 세션 초기화
    session['conversation_history'] = []
    
    # 예시 질문 랜덤 선택
    num_questions = 3  # 표시할 질문의 수
    example_questions = random.sample(all_example_questions, num_questions)
    
    return render_template('chat.html', models=model_presets.keys(), example_questions=example_questions)

# 채팅 API 엔드포인트
@app.route('/chat_api', methods=['POST'])
def chat_api():
    if not session.get('authenticated'):
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json()
    question = data.get('message')

    # 사용자가 "test"라고 입력하면 정해진 테스트용 문장을 반환
    if question.strip().lower() == "test" or question.strip().lower() == "테스트" or question.strip().lower() == "ㅅㄷㄴㅅ":
        test_response = "이것은 테스트 응답입니다. 인공지능을 사용하지 않았습니다. 강아지가 흐느적거리며 조용히 집 앞 공원에서 빠르게 뛰놀다가 아름다운 바람을 느끼며 행복하게 웃었다."
        return jsonify({'answer': test_response, 'reset_message': None})
    
    selected_model = data.get('model', 'model1')
    model_preset = model_presets.get(selected_model, model_presets['model1'])

    # 벡터 스토어 및 클로바 설정
    vector_store_manager = VectorStoreManager()
    vector_store_manager.get_vector_store()
    completion_executor = CompletionExecutor(
        host=CLOVA_HOST,
        api_key=CLOVA_API_KEY,
        api_key_primary_val=CLOVA_PRIMARY_KEY,
        request_id=CLOVA_REQUEST_ID
    )

    # 컨텍스트 생성
    context = generate_context(question, vector_store_manager)

    # 대화 내역 관리
    conversation_history, reset_message = manage_conversation_history(question)

    # 모델에게 보낼 메시지 구성
    messages = construct_messages(model_preset, conversation_history, context)

    # 모델에 요청 보내기
    response = get_model_response(completion_executor, model_preset, messages)

    # 대화 내역에 봇의 응답 추가
    conversation_history.append({'role': 'assistant', 'content': response})
    session['conversation_history'] = conversation_history

    # 채팅 기록 저장
    save_chat_history(question, response)

    # 응답 반환
    return jsonify({'answer': response, 'reset_message': reset_message})

def manage_conversation_history(question):
    # 대화 내역 초기화 또는 가져오기
    if 'conversation_history' not in session:
        session['conversation_history'] = []
    conversation_history = session['conversation_history']

    # 사용자의 메시지 추가
    conversation_history.append({'role': 'user', 'content': question})

    # 기억력 제한 가져오기
    max_memory_length = config.get('max_memory_length', 10)

    # 기억력 초기화 여부 확인
    reset_message = None
    if len(conversation_history) > max_memory_length:
        conversation_history = []
        session['conversation_history'] = conversation_history
        reset_message = '기억력이 초기화되었습니다!'

    return conversation_history, reset_message

def construct_messages(model_preset, conversation_history, context):
    # 모델 프리셋의 사전 설정 메시지 가져오기
    preset_text = model_preset['preset_text'].copy()

    # 대화 내역 추가
    messages = preset_text + conversation_history

    # 컨텍스트를 시스템 메시지로 추가
    messages.append({'role': 'system', 'content': f'사전 정보: {context}'})

    return messages

def get_model_response(executor, model_preset, messages):
    # 모델 요청 데이터 구성
    request_data = model_preset['request_data']
    request_data['messages'] = messages

    # 모델에 요청 보내기
    response = executor.execute(request_data)
    return response


@app.route('/get_example_questions')
def get_example_questions():
    num_questions = 3  # Number of questions to return
    example_questions = random.sample(all_example_questions, num_questions)
    return jsonify({'example_questions': example_questions})

@app.route('/reset_conversation', methods=['POST'])
def reset_conversation():
    session['conversation_history'] = []
    return '', 204

# 관리자 페이지
@app.route('/admin', methods=['GET', 'POST'])
def admin():
    if not session.get('admin_authenticated'):
        if request.method == 'POST':
            password = request.form.get('password')
            if password == ADMIN_PASSWORD:
                session['admin_authenticated'] = True
                return redirect(url_for('admin'))
            else:
                return render_template('admin.html', error='비밀번호가 올바르지 않습니다.')
        return render_template('admin.html')

    # 설정 변경 로직
    if request.method == 'POST':
        new_config = request.form.to_dict()
        with open('config.yaml', 'w', encoding='utf-8') as f:
            yaml.dump(new_config, f, allow_unicode=True)
        return render_template('admin.html', success='설정이 업데이트되었습니다.', config=new_config)

    return render_template('admin.html', config=config)

@app.route('/admin/chat_history')
def chat_history():
    if not session.get('admin_authenticated'):
        return redirect(url_for('admin'))

    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT user_message, bot_response, timestamp FROM chat_history ORDER BY timestamp DESC')
    chat_logs = cursor.fetchall()

    return render_template('chat_history.html', chat_logs=chat_logs)


# 컨텍스트 생성 함수
def generate_context(question, vector_store_manager):
    # 질문 임베딩
    question_embedding = vector_store_manager.get_embedding(question).reshape(1, -1)
    faiss.normalize_L2(question_embedding)

    # 유사도 검색
    k = config.get('top_k', 5)
    D, I = vector_store_manager.vector_store.index.search(question_embedding, k)

    # 유사도 점수와 문서 매핑
    docs_and_scores = []
    for idx, score in zip(I[0], D[0]):
        if idx == -1:
            continue
        doc_id = vector_store_manager.vector_store.index_to_docstore_id[idx]
        doc = vector_store_manager.vector_store.docstore[doc_id]
        docs_and_scores.append((doc, score))

    # 문서 매핑 및 컨텍스트 생성
    max_total_length = config.get('max_total_length', 1500)
    alpha = config.get('alpha', 1.0) # 유사도 점수의 영향력 조절
    total_length = 0

    # 유사도 점수와 문서 길이를 기반으로 할당된 길이 계산
    similarity_scores = np.array([score for doc, score in docs_and_scores])
    doc_lengths = np.array([len(doc.page_content) for doc, score in docs_and_scores])
    length_factors = doc_lengths / doc_lengths.sum()

    adjusted_scores = similarity_scores ** alpha
    allocated_lengths = (adjusted_scores / adjusted_scores.sum()) * max_total_length

    context = ""

    for idx, (doc, score) in enumerate(docs_and_scores):
        allocated_length = int(allocated_lengths[idx])

        # 섹션별로 내용을 추가하되, 섹션 중간에서 자르지 않음
        sections = doc.metadata.get('sections', [])
        content = ""
        length_used = 0
        for heading, section_content in sections:
            section_length = len(section_content)
            if length_used + section_length <= allocated_length:
                content += f"#{heading}\n{section_content}\n"
                length_used += section_length
            else:
                # 섹션이 전체 할당된 길이를 초과하는 경우
                if length_used == 0:
                    # 할당된 길이만큼 섹션 내용을 자름
                    truncated_content = section_content[:allocated_length]
                    content += f"#{heading}\n{truncated_content}\n"
                    length_used += len(truncated_content)
                break

        if length_used == 0:
            continue  # 이 문서에서 추가된 내용이 없음

        total_length += length_used
        if total_length > max_total_length:
            break  # 전체 컨텍스트 길이 초과

        context += f"{content}\n---\n"

        # 사용된 문서의 전문 로그 출력
        # logging.info(f"유사도 순위: {idx + 1}, 점수: {score}, 제목: {doc.metadata['title']}")
        # logging.info(f"내용:\n{content}\n")

    return context

def save_chat_history(user_message, bot_response):
    db = get_db()
    cursor = db.cursor()
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cursor.execute('''
        INSERT INTO chat_history (user_message, bot_response, timestamp)
        VALUES (?, ?, ?)
    ''', (user_message, bot_response, timestamp))
    db.commit()

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect('chat_history.db')
    return g.db

@app.teardown_appcontext
def close_db(error):
    db = g.pop('db', None)

    if db is not None:
        db.close()

def init_db():
    conn = sqlite3.connect('chat_history.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_message TEXT,
            bot_response TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

if __name__ == '__main__':
    init_db()
    app.run(debug=True)
