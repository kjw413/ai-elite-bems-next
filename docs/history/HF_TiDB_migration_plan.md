# BEMS 24/7 무료 호스팅 마이그레이션 계획

> 작성일: 2026-05-06
> 목적: PC 가동 여부와 무관하게 외부에서 상시 접근 가능한 무료 웹 플랫폼 구축
> 핵심 의사결정: HF Spaces (웹앱) + TiDB Serverless (DB) + HF Hub (ML 모델) + 로컬 PC (일일 sync)

---

## 0. 의사결정 배경

### 검토했지만 채택하지 않은 옵션
- **Cloudflare Workers**: V8 isolate / Pyodide 베타로는 Streamlit·MySQL·LightGBM·XGBoost·CatBoost 동작 불가. CPU 10ms·메모리 128MB 한도, 스크립트 1MB 압축 한도 모두 미달.
- **Cloudflare Tunnel + 로컬 PC**: PC가 꺼지면 같이 죽으므로 "상시 접근" 요건 위배.
- **Streamlit Community Cloud**: RAM 1GB 한도. v5.pkl(977MB) + v5.1.pkl(922MB) 동시 로드 불가능.
- **Aiven for MySQL Free**: 2024년 free tier 종료.
- **PlanetScale Free**: 2024년 4월 free tier 종료.

### 채택한 스택과 이유
| 컴포넌트 | 선택 | 이유 |
|---|---|---|
| 웹 호스팅 | Hugging Face Spaces (Streamlit SDK, CPU basic) | 무료, 24/7, 16GB RAM (1.9GB 모델 동시 로드 가능), persistent disk `/data` 50GB |
| DB | TiDB Cloud Serverless | MySQL 호환, 5GB 무료 영구, 카드 불필요 |
| ML 모델 저장 | Hugging Face Hub private repo | LFS로 1.9GB 무료, `huggingface_hub`로 startup 다운로드 |
| 일일 데이터 sync | 로컬 PC + Windows 작업 스케줄러 | `E:\Sampled DB\*.xlsx` 의존성 유지, 하루 1회 갱신 패턴에 맞음 |
| 인증 | 단일 공유 비밀번호 게이트 (Streamlit) | "URL만 알면 접속" 요건과 보안의 절충 |

---

## 1. 보안 정책 — 솔직한 정리

**"URL만 알면 접속 가능" + "보안 보장"은 본질적으로 상충.** HF Spaces 노출 옵션:

| 옵션 | URL만으로 접속 | 보안 | 채택 |
|---|---|---|---|
| Public Space | 가능 | HF 검색·트렌딩 노출 | ❌ |
| Private Space | HF 계정·권한 필요 | 강함 | ❌ ("URL만 알면" 위배) |
| **Public Space + 비밀번호 게이트** | URL + 1회 비밀번호 입력 | 중간 (현실적 최선) | ✅ |

- 권한: 웹 접속자 전원 **viewer 고정** (DB read-only). admin 작업은 PC 로컬 CLI에서만 수행.
- Space 이름은 검색성 낮게 명명 (회사명·시스템명 직접 노출 회피).
- 비밀번호는 12자 이상 권장, HF Spaces Secrets로 관리.

---

## 2. 마이그레이션 시 식별된 장벽

### 장벽 A. ML 모델 용량 — 약 1.9GB
- `app/predictive model/energy usage/v5.pkl` — **977MB**
- `app/predictive model/energy usage/v5.1.pkl` — **922MB**

GitHub은 단일 파일 100MB 제한 → repo에 직접 push 불가.
→ **HF Hub private repo + Spaces persistent disk(`/data/models/`) 캐싱**으로 해결.

### 장벽 B. 로컬 Excel 자동 동기화 의존성
[main.py:33-44](app/main.py#L33-L44)의 `auto_sync_once`, `auto_sync_production_once`는 `E:\Sampled DB\*.xlsx`를 직접 읽음. 클라우드 인스턴스에는 `E:\` 없음.
→ **PC가 직접 TiDB로 push** (sync 로직 자체는 살아있고 DB 타겟만 변경).

### 장벽 C. IP 기반 admin 권한 로직
[db_connection.py:43-57](app/database/db_connection.py#L43-L57)의 loopback IP = admin 판별은 클라우드에서 의미 상실 (모든 트래픽이 같은 IP).
→ **`is_admin()`을 한 줄로 단순화**: `return not exists()` (Streamlit 런타임에서는 항상 False, 터미널에서만 True).

---

## 3. 최종 아키텍처

```
[ 사용자 PC (하루 1회만 켜져도 OK) ]              [ Hugging Face Spaces (24/7) ]
  E:\Sampled DB\*.xlsx                              ┌─ Streamlit 웹앱 (viewer 전용)
        │                                           ├─ 비밀번호 게이트
        │ Windows 작업 스케줄러 (매일 09:00)        ├─ 시작 시 HF Hub에서 v5.pkl/v5.1.pkl
        ▼                                           │  → /data/models/ 캐싱 (재시작 후 재사용)
  python -m app.scripts.daily_sync_cli              └─ TiDB에 read/write
        │                                                  ▲
        └────── DB push ──────► TiDB Serverless ◄──────────┘
                                  ▲
                                  └─ 사용자 브라우저 (PC 꺼져도 접속 가능)
```

---

## 4. 단계별 실행 계획

### Phase 1 — 데이터 레이어 (≈1.5h)

1. **TiDB Cloud Serverless 클러스터 생성** — 카드 불필요, 5GB 무료. IP 화이트리스트는 `0.0.0.0/0`으로 개방 (DB 자체는 비밀번호 인증).
2. **로컬 MySQL → TiDB 1회 덤프 이전**
   - `mysqldump --single-transaction --routines fems_db > fems_dump.sql` (현재 DB 약 120MB라 수 분)
   - TiDB에 `mysql -h ... < fems_dump.sql`로 import
   - 스키마 import 후 [app/database/schema.sql](app/database/schema.sql) 결과와 실제 객체 구조 비교 (TiDB는 99% MySQL 8.0 호환이지만 `AUTO_INCREMENT` 동작·일부 collation에서 미세 차이 가능)
3. **연결정보를 로컬 `.env` + HF Spaces Secrets 양쪽에 기록**
   - `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`

### Phase 2 — 코드 정리 (≈2h)

4. **[app/database/db_connection.py](app/database/db_connection.py) 슬림화**
   - `_get_client_ip`, `_is_host_machine_ip`, `_LOOPBACK_IPS` 전부 삭제
   - `is_admin()` → `return not exists()` 한 줄
   - DB host/port/credential은 환경변수에서만 읽음
5. **[app/main.py:33-44](app/main.py#L33-L44)의 `auto_sync_once`, `auto_sync_production_once` 호출 제거**
   - 함수 자체는 남겨두고 `main.py`에서만 호출 제거 (로컬 CLI에서 재사용)
6. **비밀번호 게이트 추가** — `st.set_page_config` 직후, `init_db()` 앞에 `password_gate()` 함수 약 30줄. `st.session_state["authenticated"]` 검사. 비밀번호는 `st.secrets["APP_PASSWORD"]`.

### Phase 3 — ML 모델 분리 (≈1.5h)

7. **HF Hub private model repo 생성** (예: `<user>/bems-models`)
8. **`v5.pkl`, `v5.1.pkl` 업로드** (LFS 자동 처리) — 1.9GB 첫 업로드 약 30분
9. **모델 로더 패치** — 모델 경로를 결정하는 위치 (예: `app/services/v5_common.py`)에 다음 헬퍼 추가:
   ```python
   from huggingface_hub import hf_hub_download
   def get_model_path(filename: str) -> Path:
       cache_dir = Path("/data/models") if Path("/data").exists() else PROJECT_ROOT / "app/predictive model/energy usage"
       local = cache_dir / filename
       if local.exists():
           return local
       return Path(hf_hub_download(
           repo_id="<user>/bems-models",
           filename=filename,
           cache_dir=str(cache_dir),
           token=os.getenv("HF_TOKEN"),
       ))
   ```
   `/data`는 HF Spaces persistent disk → 재시작에도 캐시 유지. 로컬에서는 기존 경로 그대로.
   기존 `v5_retrain_service.py`도 동일 헬퍼를 사용하도록 통일.

### Phase 4 — PC 측 동기화 분리 (≈1h)

10. **`app/scripts/daily_sync_cli.py` 신설** — 기존 `daily_energy_sync_service.auto_sync_once`와 `production_dw_sync_service.auto_sync_production_once`를 호출하는 얇은 CLI. DB는 TiDB를 가리킴. 로깅 파일 출력.
11. **Windows 작업 스케줄러 등록** — 매일 09:00 실행, "사용자가 로그온되어 있지 않아도 실행" 체크.
   ```
   e:\AI-Elite_Energy-Dashboard-Web\.venv\Scripts\python.exe -m app.scripts.daily_sync_cli
   ```

### Phase 5 — 배포 (≈1.5h)

12. **HF Space 생성** — Streamlit SDK, CPU basic 16GB.
13. **`requirements.txt`에 `huggingface_hub` 추가**, GPU 전용 의존성 없는지 확인.
14. **HF Secrets 설정**:
    - `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`
    - `APP_PASSWORD` (사이트 진입 비밀번호)
    - `HF_TOKEN` (private model repo 다운로드용)
15. **Repo push** — `.gitignore`에 다음 추가:
    - `*.pkl`
    - `.env`
    - `app/predictive model/energy usage/v5*.pkl`
    - 로컬 절대경로 참조 데이터
16. **Space 첫 빌드** — 5~10분. 모델 다운로드 첫 부팅에 5~10분 추가 가능 (이후엔 캐시).

### Phase 6 — 검증 (≈1h)

17. **smoke test 체크리스트**
    - [ ] 비밀번호 입력 전 모든 페이지 차단
    - [ ] 잘못된 비밀번호 시 에러 표시
    - [ ] 로그인 후 모든 페이지에서 `is_admin()` False (DB write 차단, viewer 권한 강제)
    - [ ] AI 예측 페이지에서 v5.1 모델 로드 정상
    - [ ] PC에서 `daily_sync_cli` 1회 실행 → TiDB 반영 → 웹 새로고침 시 보임
    - [ ] Space 재시작 후 모델 캐시 유지 (`/data` 영속성 확인)

**총 작업량: 8~9h** (모델 업로드 대기 포함하면 만 하루). Phase 1~2를 1일차, 3~6을 2일차로 분할 권장.

---

## 5. 잠재 리스크

| 리스크 | 영향 | 대응 |
|---|---|---|
| TiDB Serverless 7일 무요청 시 sleep | 첫 쿼리 5~10초 지연 | 일일 sync가 매일 들어오므로 실제론 거의 안 걸림 |
| HF Spaces 48h 미접속 시 일시정지 | 첫 접속자 30~60초 대기 | 비즈니스 크리티컬이면 GitHub Actions로 매일 1회 ping (옵션) |
| MySQL → TiDB 미세 호환성 차이 | 일부 SQL 동작 차이 가능 | Phase 1-2의 스키마 비교 단계에서 발견·수정 |
| 모델 재학습 워크플로 변경 | 재학습 후 HF Hub 재push 필요 | Phase 3-9에서 `v5_retrain_service.py` 함께 정리 |
| HF Spaces public 노출 | URL이 검색·인덱싱될 수 있음 | 비밀번호 게이트 + 검색성 낮은 Space명 (옵션: 추후 Cloudflare Access 유료 검토) |

---

## 6. 진행 전 확정 필요

- [ ] HF 계정 보유 여부 (없으면 무료 가입, 1분)
- [ ] 공유 비밀번호 (12자 이상)
- [ ] Space 이름 (검색성 낮게)
- [ ] 일일 sync 실행 시각 (기본 09:00)

---

## 7. 사용 예산 — 모두 무료 한도 내

| 항목 | 한도 | 현 예상 사용량 |
|---|---|---|
| HF Spaces CPU basic | 무료, 16GB RAM, 50GB disk, idle 시 sleep | 모델 1.9GB + 의존성 ≈ 4GB 이하 |
| HF Hub private repo storage | 무료 (LFS 포함) | 모델 1.9GB |
| TiDB Serverless | 5GB DB, 50M RU/월 | 현재 DB 120MB |
| 도메인 | — | 사용 안 함 (HF 기본 URL) |

**고정비 0원, 카드 등록 없음.**
