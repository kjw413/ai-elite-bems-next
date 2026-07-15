"""
Automation Config
=================
.env 로드 및 자동화 공통 설정을 한곳에서 관리.

규칙:
    - 모든 자동화 스크립트는 본 모듈을 통해 환경값을 읽습니다.
    - .env 우선, 없으면 OS 환경변수, 그래도 없으면 안전한 기본값.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

# tools/mail/file.py → 2단계 위가 프로젝트 루트
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

# .env 로드 (이미 로드되어 있어도 안전)
load_dotenv(PROJECT_ROOT / ".env", override=False)


# ─────────────────────────────────────────────────────────────────────────────
# 경로 설정
# ─────────────────────────────────────────────────────────────────────────────
LOG_DIR: Path = PROJECT_ROOT / "logs" / "automation"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TEMPLATE_DIR: Path = Path(__file__).resolve().parent / "templates"


# ─────────────────────────────────────────────────────────────────────────────
# 도우미
# ─────────────────────────────────────────────────────────────────────────────
def _split_csv(value: Optional[str]) -> List[str]:
    """콤마 구분 문자열을 trim된 리스트로 변환."""
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _to_int(value: Optional[str], default: int) -> int:
    try:
        return int(value) if value is not None and str(value).strip() != "" else default
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────────────────────────────────────
# 메일 설정
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class MailConfig:
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_app_password: str
    sender_name: str
    recipients: List[str]
    cc: List[str]
    subject_prefix: str
    use_tls: bool = True

    @property
    def is_valid(self) -> bool:
        return bool(
            self.smtp_host
            and self.smtp_user
            and self.smtp_app_password
            and self.recipients
        )

    def missing_keys(self) -> List[str]:
        miss = []
        if not self.smtp_host:         miss.append("SMTP_HOST")
        if not self.smtp_user:         miss.append("SMTP_USER")
        if not self.smtp_app_password: miss.append("SMTP_APP_PASSWORD")
        if not self.recipients:        miss.append("MAIL_RECIPIENTS")
        return miss


def get_mail_config() -> MailConfig:
    return MailConfig(
        smtp_host         = os.getenv("SMTP_HOST", "smtp.gmail.com"),
        smtp_port         = _to_int(os.getenv("SMTP_PORT"), 587),
        smtp_user         = os.getenv("SMTP_USER", "").strip(),
        smtp_app_password = os.getenv("SMTP_APP_PASSWORD", "").strip().replace(" ", ""),
        sender_name       = os.getenv("MAIL_SENDER_NAME", "AI-Elite Energy Dashboard"),
        recipients        = _split_csv(os.getenv("MAIL_RECIPIENTS")),
        cc                = _split_csv(os.getenv("MAIL_CC")),
        subject_prefix    = os.getenv("MAIL_SUBJECT_PREFIX", "[FEMS][일일 에너지 이상 Alert]"),
        use_tls           = os.getenv("SMTP_USE_TLS", "true").lower() in ("true", "1", "yes"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 일일 리포트 설정
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DailyReportConfig:
    reference_offset_days: int          # 평일 근무일(주말·공휴일 제외) 기준 D-N 기준일 (기본 1 → D-1)
    factories_filter: List[str]         # 비어 있으면 DB에 존재하는 모든 공장
    include_company_total: bool         # 전사 합계 행 포함 여부
    chart_recent_days: int              # 추이 차트 일수


def get_daily_report_config() -> DailyReportConfig:
    return DailyReportConfig(
        reference_offset_days = _to_int(os.getenv("DAILY_REPORT_REFERENCE_OFFSET_DAYS"), 1),
        factories_filter      = _split_csv(os.getenv("DAILY_REPORT_FACTORIES")),
        include_company_total = os.getenv("DAILY_REPORT_INCLUDE_TOTAL", "true").lower() in ("true", "1", "yes"),
        chart_recent_days     = _to_int(os.getenv("DAILY_REPORT_CHART_DAYS"), 14),
    )
