# NoteCraft

Whisper 전사와 ChatMock 요약을 연결해 강의 음성/영상을 Obsidian용 학습 노트로 변환하는 웹 서비스입니다. 업로드한 파일은 전사, 구조화 요약, Markdown 저장 단계로 처리되며 GPU 사용량과 작업 상태를 대시보드에서 확인할 수 있습니다.

## 주요 기능

- 음성/영상 업로드 후 Whisper 기반 전사
- ChatMock OpenAI 호환 API를 통한 강의 요점정리 노트 생성
- 표, 핵심 개념, 복습 질문, 확인 필요 항목을 포함한 Markdown 렌더링
- 로그인 및 관리자 승인 기반 계정 생성
- Whisper GPU 사용량 실시간 표시
- 기존 업로드 파일 재처리 및 결과 파일 다운로드

## 실행 준비

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

ChatMock 서버를 먼저 실행합니다.

```bash
chatmock serve --host 127.0.0.1 --port 8000
```

개발 환경 실행:

```bash
export APP_ADMIN_USERNAME=yes2310
export APP_ADMIN_PASSWORD='change-this-password'
export LLM_BASE_URL=http://127.0.0.1:8000/v1
export LLM_MODEL=gpt-5.4
uvicorn app:app --host 0.0.0.0 --port 9002
```

브라우저에서 `http://서버주소:9002/login`으로 접속합니다.

`APP_ADMIN_PASSWORD`는 개발 환경에서도 필수입니다. 기본 관리자 비밀번호는 저장소에 포함하지 않습니다.

## 공개 배포 설정

공개 서버에서는 반드시 아래 값을 지정하세요.

```bash
export APP_ENV=production
export APP_ADMIN_USERNAME='admin-id'
export APP_ADMIN_PASSWORD='strong-admin-password'
export APP_COOKIE_SECURE=1
export DEBUG_ERRORS=0
export MAX_UPLOAD_BYTES=2147483648
```

`APP_ENV=production`에서는 관리자 비밀번호가 없으면 서버가 시작되지 않습니다. 운영 환경에서는 HTTPS 뒤에서 실행하고, `APP_COOKIE_SECURE=1`을 유지하세요.

## 주요 환경 변수

| 이름 | 기본값 | 설명 |
| --- | --- | --- |
| `APP_ENV` | `development` | `production`이면 API 문서 비활성화 및 보안 기본값 강화 |
| `APP_ADMIN_USERNAME` | `yes2310` | 초기 관리자 계정 |
| `APP_ADMIN_PASSWORD` | 없음 | 초기 관리자 비밀번호. 운영에서는 필수 |
| `APP_COOKIE_SECURE` | 운영 `1`, 개발 `0` | HTTPS 전용 세션 쿠키 여부 |
| `SESSION_TTL_SECONDS` | `604800` | 로그인 세션 유지 시간 |
| `MAX_UPLOAD_BYTES` | `2147483648` | 업로드 파일 최대 크기 |
| `LLM_BASE_URL` | `http://127.0.0.1:8000/v1` | ChatMock/OpenAI 호환 API 주소 |
| `LLM_MODEL` | `gpt-5.4` | 기본 요약 모델 |
| `WHISPER_MODEL` | `large-v3` | Whisper 모델 |
| `WHISPER_COMPUTE` | `float16` | Whisper compute type |
| `WHISPER_GPU_IDS` | `auto` | 사용할 GPU 인덱스. 예: `0`, `0,1`, `auto` |
| `VAULT_PATH` | `./vault` | Markdown 노트 저장 위치 |

## 저장 위치

- 업로드 파일: `uploads/`
- 전사 결과: `output/`
- 생성 노트: `vault/지식창고/<카테고리>/`
- 작업/계정 DB: `jobs.db`

`uploads/`, `output/`, `jobs.db`는 `.gitignore`에 포함되어 있습니다.
