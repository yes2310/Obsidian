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

한 번에 실행하려면 아래 스크립트를 사용합니다. `.env`가 없으면 자동 생성하고, 가상환경과 의존성도 준비한 뒤 ChatMock과 NoteCraft를 함께 실행합니다.

```bash
chmod +x scripts/*.sh
scripts/run.sh
```

브라우저에서 `http://서버주소:9002/login`으로 접속합니다. 첫 실행 때 터미널에 초기 관리자 비밀번호가 출력되고, 이후에는 `.env`의 `APP_ADMIN_PASSWORD`를 사용합니다.

검증 스크립트:

```bash
scripts/check.sh
```

브라우저 레이아웃까지 검증하려면 Playwright 브라우저를 설치한 뒤 실행합니다.

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python scripts/visual_check.py
```

수동으로 나눠 실행해야 할 때:

```bash
source .env
chatmock serve --host "$CHATMOCK_HOST" --port "$CHATMOCK_PORT"
uvicorn app:app --host "$APP_HOST" --port "$APP_PORT"
```

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
| `APP_HOST` | `0.0.0.0` | 웹 서버 바인딩 주소 |
| `APP_PORT` | `9002` | 웹 서버 포트 |
| `CHATMOCK_HOST` | `127.0.0.1` | 자동 실행할 ChatMock 주소 |
| `CHATMOCK_PORT` | `8000` | 자동 실행할 ChatMock 포트 |
| `AUTO_START_CHATMOCK` | `1` | `scripts/run.sh`에서 ChatMock 자동 실행 여부 |
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
