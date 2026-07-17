"""
Energy Intensity Mail - 통합 엔트리 포인트 (일간/주간/월간)
==========================================================
.bat / 작업 스케줄러 / 수동 실행:
    python tools/mail/run_mail.py daily                     # 일간 (근무일 기준 D-1)
    python tools/mail/run_mail.py daily 2026-07-08          # 일간 특정 일자
    python tools/mail/run_mail.py weekly                    # 주간 (직전 완결 주 월~일)
    python tools/mail/run_mail.py weekly 2026-07-08         # 해당 일자가 속한 주
    python tools/mail/run_mail.py monthly                   # 월간 (직전 완결 월)
    python tools/mail/run_mail.py monthly 2026-06           # 특정 월
    python tools/mail/run_mail.py weekly --dry-run          # 발송 없이 HTML만 저장

기존 run_daily_mail.py 는 daily 전용 하위호환 엔트리로 유지된다.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

# tools/mail/file.py → 2단계 위가 프로젝트 루트
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.mail.config import get_mail_config, LOG_DIR
from tools.mail.logger import get_logger
from tools.mail.mail_service import MailMessage, send_mail, MailSendError
from tools.mail.run_daily_mail import (
    _sync_latest_energy_data,
    _sync_latest_production_data,
)


def _build(period: str, ref: str | None):
    """period 별 리포트 빌드. ref: daily/weekly=YYYY-MM-DD, monthly=YYYY-MM."""
    if period == "daily":
        from tools.mail.daily_report_builder import build_daily_report
        ref_d = datetime.strptime(ref, "%Y-%m-%d").date() if ref else None
        return build_daily_report(ref_date=ref_d)
    if period == "weekly":
        from tools.mail.period_report_builder import build_weekly_report
        ref_d = datetime.strptime(ref, "%Y-%m-%d").date() if ref else None
        return build_weekly_report(ref_date=ref_d)
    if period == "monthly":
        from tools.mail.period_report_builder import build_monthly_report
        if ref:
            y, m = ref.split("-")
            return build_monthly_report(year=int(y), month=int(m))
        return build_monthly_report()
    raise ValueError(f"지원하지 않는 period: {period}")


def main() -> int:
    parser = argparse.ArgumentParser(description="에너지 원단위 메일 자동 송부 (일간/주간/월간)")
    parser.add_argument("period", choices=["daily", "weekly", "monthly"],
                        help="발송 주기")
    parser.add_argument("ref", nargs="?", default=None,
                        help="기준 (daily/weekly: YYYY-MM-DD, monthly: YYYY-MM). 생략 시 자동")
    parser.add_argument("--to", default=None, help="수신자 override (콤마 구분)")
    parser.add_argument("--cc", default=None, help="참조자 override (콤마 구분)")
    parser.add_argument("--dry-run", action="store_true",
                        help="실제 발송 없이 HTML만 logs/automation에 저장")
    args = parser.parse_args()

    log = get_logger(f"{args.period}_mail")
    log.info("=" * 60)
    log.info(f"{args.period} 에너지 원단위 메일 자동화 시작")

    # 1) 최신 RawDB_에너지.xlsx 변경분을 DB에 먼저 반영
    sync_rc = _sync_latest_energy_data(log)
    if sync_rc != 0:
        return sync_rc

    # 2) 최신 DB_생산실적.xlsx 변경분을 DB에 반영
    sync_rc = _sync_latest_production_data(log)
    if sync_rc != 0:
        return sync_rc

    # 3) 리포트 빌드
    try:
        report = _build(args.period, args.ref)
    except Exception as e:
        log.exception(f"리포트 생성 실패: {e}")
        return 2

    if report.record_count == 0:
        log.warning(f"대상 기간({report.ref_date}) DB 데이터가 없습니다. 그래도 발송을 시도합니다.")

    # 4) 메시지 구성
    msg = MailMessage(
        subject=report.subject,
        html_body=report.html,
        inline_images=report.inline_images,
        to=[x.strip() for x in args.to.split(",") if x.strip()] if args.to else None,
        cc=[x.strip() for x in args.cc.split(",") if x.strip()] if args.cc else None,
    )

    # 5) Dry-run 시 파일로만 저장
    if args.dry_run:
        out = LOG_DIR / f"{args.period}_report_{report.ref_date}_DRYRUN.html"
        out.write_text(report.html, encoding="utf-8")
        log.info(f"[DRY-RUN] HTML 저장: {out}")
        log.info(f"[DRY-RUN] 제목: {report.subject}")
        return 0

    # 6) 발송
    try:
        cfg = get_mail_config()
        if not cfg.is_valid:
            log.error(f"메일 설정 누락: {', '.join(cfg.missing_keys())}. .env 확인 필요.")
            return 3
        send_mail(msg, cfg)
        log.info(f"발송 완료: [{args.period}] 기준 {report.ref_date} / 공장 {report.record_count}개")
        return 0
    except MailSendError as e:
        log.error(f"메일 발송 실패: {e}")
        return 4
    except Exception as e:
        log.exception(f"예상치 못한 오류: {e}")
        return 5


if __name__ == "__main__":
    sys.exit(main())
