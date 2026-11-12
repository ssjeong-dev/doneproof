# Doneproof

작업 완료 주장과 실행 증거를 연결하는 MCP(Model Context Protocol, 모델과 도구를 연결하는 규약) 서버입니다. 작성 에이전트와 별도 감사 모델을 연결하고, 사용자가 기록·경고·중단 정책을 선택합니다.

## 설치

Python 3.11 이상과 `uv`가 필요합니다.

```sh
uv sync --locked
uv run doneproof --help
uv run execution-audit-server --help
uv run execution-audit-worker --help
uv run python -m unittest discover -s tests -v
```

## 정책

- `integration: tools_only`: 작성자가 제출한 증거를 저장합니다. 실행을 직접 관측했다는 보장은 없습니다.
- `integration: observe`: 제공되는 명령 실행기를 통해 명령·종료 코드·출력·코드 스냅샷을 수집합니다.
- `audit.action: record | warn | block`: 감사 지적을 기록하거나 표시하거나 다음 실행을 차단합니다.
- `audit.on_error`: 감사 실패에 적용할 별도 정책입니다.
- `audit.require_different_model`: 작성 모델과 감사 모델의 식별자가 다를 것을 요구합니다.

`examples/tools-only.json`, `examples/observe-warn.json`을 참조하세요. 모델 식별자는 실제 사용하는 모델로 바꿔야 합니다. 기능 선언은 호스트가 실제 지원하는 능력에 맞춰야 합니다. 설정 파일을 작성 에이전트가 수정할 수 없게 관리하세요.

## 보장 범위

이 버전은 범용 표준 입출력 도구 서버와 명시적으로 호출하는 실행기를 제공합니다. Cursor·Claude·Codex·Grok의 네이티브 종료 이벤트 연결을 자동 설치하지 않습니다. 제품별 설정을 변경하거나 기존 세션을 제어하지 않습니다.

- 실행 중단과 보고 강제는 제공되는 실행기를 경유하는 작업에 한정됩니다. 다른 터미널이나 에이전트의 직접 파일 변경을 차단하지 않습니다.
- 대화 검색은 이 서버에 수집된 해당 작업의 메시지만 대상으로 합니다. 호스트의 전체 대화를 자동 취득하지 않습니다.
- 감사자는 읽기 전용 도구 역할을 사용할 수 있습니다. 별도 프로세스 실행은 대화 문맥 분리이며 운영체제 샌드박스가 아닙니다.
- 로컬 저장소와 설정을 수정할 수 있는 동일 사용자 권한의 악의적 프로세스까지 방어하지 않습니다.
- 다른 모델 사용 검사는 신뢰된 실행 설정의 식별자를 비교합니다. 외부 제공자가 실제 어떤 모델을 실행했는지 암호학적으로 증명하지 않습니다.
- 코드 스냅샷은 파일 내용의 변경 감지용입니다. 실행 환경·서비스·데이터베이스까지 재현하는 스냅샷은 아닙니다.

## 독립 감사 명령

`execution-audit-worker`는 증거와 수집된 대화를 표준 입력으로 외부 감사 명령에 전달합니다. 외부 명령은 아래 형식의 순수 JSON(JavaScript Object Notation, 구조화된 텍스트 데이터 형식)을 반환해야 합니다.

```json
{"status":"inconclusive","findings":[],"summary":"브라우저 검증 증거 없음"}
```

```sh
uv run execution-audit-worker \
  --db .audit/audit.db \
  --config examples/observe-warn.json \
  --audit-id AUDIT_IDENTIFIER \
  --model ACTUAL_AUDITOR_MODEL \
  --timeout 120 \
  -- /absolute/path/to/read-only-auditor
```

감사 명령은 신뢰된 사용자가 지정합니다. 감사 모델에는 파일 수정·셸 실행 권한을 주지 않는 구성을 권장합니다. 외부 모델에 전송되는 증거와 대화에 민감정보가 포함되지 않도록 수집 범위를 관리해야 합니다. 자동 비밀값 제거 기능은 제공하지 않습니다.

감사 시간 초과, 비정상 종료, 잘못된 응답은 `error`로 기록하며 성공으로 바꾸지 않습니다. 오류 응답에는 외부 프로그램의 원문 오류 출력을 포함하지 않습니다.

미처리 감사를 계속 처리하려면 같은 설정으로 별도 워커를 실행합니다. 한 저장소에는 워커 하나를 실행하세요.

```sh
uv run execution-audit-worker \
  --db .audit/audit.db \
  --config examples/observe-warn.json \
  --pending --watch \
  --model ACTUAL_AUDITOR_MODEL \
  -- /absolute/path/to/read-only-auditor
```

워커는 턴 제출 시 저장된 감사 요청을 조회합니다. 실행 중 종료된 요청은 다음 실행에서 다시 시도합니다. 외부 모델 호출 자체는 재시도 시 중복될 수 있습니다. 감사 결과의 저장은 원장의 중복 방지 규칙을 따릅니다.

현재 프로세스 그룹 종료 구현은 macOS/Linux를 대상으로 합니다. Windows 실행은 검증하지 않았습니다. 출력의 원장 저장량은 제한하지만 임시 출력 파일의 디스크 사용량까지 제한하는 운영체제 자원 격리는 제공하지 않습니다.

## 실행 순서

```sh
uv run execution-audit init
uv run execution-audit doctor
uv run execution-audit task --goal "로그인 오류 수정" --criteria "만료된 세션 처리 검증"
uv run execution-audit begin --task-id TASK_IDENTIFIER
```

실행 관측을 활성화하려면 `.audit/config.json`에 `examples/observe-warn.json` 내용을 적용하고 실제 작성 모델 식별자를 입력한 뒤, 사용자 관리 명령으로 정책을 반영합니다.

```sh
uv run execution-audit-admin --db .audit/audit.db --config .audit/config.json
uv run execution-audit run --turn-id TURN_IDENTIFIER -- python -m unittest
```

`run` 결과의 `recorded.id`를 아래 증거 참조에 넣습니다.

```sh
uv run execution-audit submit --turn-id TURN_IDENTIFIER \
  --claims '[{"criterion_id":"C1","text":"만료된 세션 처리 검증","evidence_ids":["EVIDENCE_IDENTIFIER"]}]'
uv run execution-audit report --task-id TASK_IDENTIFIER
uv run execution-audit complete --task-id TASK_IDENTIFIER
```

`submit` 호출이 턴 종료 연결 지점입니다. 외부 호스트의 턴 종료 이벤트에서 이 명령을 호출하거나 직접 호출해야 합니다. 별도 `--pending --watch` 감사 워커가 실행 중이면 제출된 요청을 자동으로 처리합니다. 호스트 이벤트 연결 없이는 네이티브 에이전트의 턴 종료를 자동 감지하지 않습니다.

## 정책 저장과 변경

정책은 Git 커밋이 아닌 로컬 `.audit/audit.db`의 `policy` 테이블에 저장됩니다. 같은 데이터베이스를 다른 설정으로 열어 조용히 정책을 낮추는 동작은 거부합니다. 사용자 관리 명령 `execution-audit-admin`을 통한 명시적인 변경은 허용하며, 이전·새 정책 버전과 변경 내용은 원장에 남습니다.

명시적인 정책 변경은 기존 태스크를 포함하여 이후의 판정에 적용합니다. 이미 기록된 감사 결과와 사건 이력은 덮어쓰지 않습니다. 연결 기능 선언을 바꾼 경우 연결 프로그램도 새 설정으로 재시작하세요. 작성자에게 노출되는 도구에는 정책 변경 기능이 없습니다. 운영체제에서 같은 사용자의 파일 접근까지 차단하는 기능은 아닙니다.

## 클라이언트 연결

표준 입출력 서버 실행 명령:

```sh
uv run execution-audit-server --role writer --db .audit/audit.db --config .audit/config.json
```

`examples/mcp-tools-only.json`은 `mcpServers` 설정 형식을 사용하는 클라이언트를 위한 연결 예입니다. 경로와 실행 프로그램을 설치 위치에 맞추세요. 클라이언트마다 설정 형식과 종료 이벤트 연결 방법은 별도로 적용해야 합니다.

감사 전용 서버는 단일 감사 식별자와 실제 감사 모델 식별자를 시작 시 고정합니다.

```sh
uv run execution-audit-server --role auditor \
  --audit-id AUDIT_IDENTIFIER --auditor-model ACTUAL_AUDITOR_MODEL \
  --db .audit/audit.db --config .audit/config.json
```

기본 연결은 모델 식별자가 없으므로 다른 모델 사용을 강제하지 않습니다. 그 보장이 필요하면 `writer_model`을 지정하고 `require_different_model: true`를 켜세요. 두 예제 정책 파일은 이 옵션을 활성화합니다.

상세 구현·검증 결과는 [IMPLEMENTATION.md](IMPLEMENTATION.md)를 참고하세요. `doneproof`는 `execution-audit` 명령의 제품명 별칭입니다.
